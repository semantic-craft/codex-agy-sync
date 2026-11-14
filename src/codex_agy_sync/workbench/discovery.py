"""Workspace discovery, Union-Find primitives, and sensitive path exclusions."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Sequence

from ..bridge.paths_and_locks import HostEnvironmentPaths, open_readonly_sqlite
from ..bridge.store_catalog import extract_agy_project_folder_uris, parse_summary_workspace_uris
from ..workspace import (
    FORBIDDEN_AGY_PROJECT_IDS,
    WorkspacePath,
    WorkspaceRootUri,
    candidate_home_dirs,
    is_excluded_for_homes,
    parse_workspace_identity,
    raw_spec_to_candidate_path,
)


class UnionFind:
    def __init__(self, nodes: Iterable[tuple[str, str]]) -> None:
        self._parent: dict[tuple[str, str], tuple[str, str]] = {n: n for n in nodes}

    def find(self, item: tuple[str, str]) -> tuple[str, str]:
        parent = self._parent.setdefault(item, item)
        while parent != self._parent[parent]:
            self._parent[parent] = self._parent[self._parent[parent]]
            parent = self._parent[parent]
        self._parent[item] = parent
        return parent

    def union(self, left: tuple[str, str], right: tuple[str, str]) -> None:
        rl, rr = self.find(left), self.find(right)
        if rl != rr:
            self._parent[rr] = rl


def _try_register_workspace(
    raw_spec: str | Path,
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
) -> None:
    raw_path = raw_spec_to_candidate_path(raw_spec)
    if raw_path is None or is_excluded_for_homes(raw_path, homes):
        return
    try:
        ws_path, ws_uri = parse_workspace_identity(raw_spec)
    except ValueError:
        return
    if is_excluded_for_homes(ws_path, homes) or not ws_path.is_dir():
        return
    discovered.setdefault(ws_path, ws_uri)


def _collect_agy_project_workspaces(
    paths: HostEnvironmentPaths,
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
) -> None:
    projects_dir = paths.gemini_config_dir / "projects"
    if not projects_dir.is_dir():
        return
    for pfile in sorted(projects_dir.glob("*.json")):
        try:
            data = json.loads(pfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        cand_id = str(data.get("id") or pfile.stem).strip()
        if cand_id in FORBIDDEN_AGY_PROJECT_IDS:
            continue
        if not isinstance(data.get("projectResources"), dict):
            continue
        for uri in extract_agy_project_folder_uris(data):
            _try_register_workspace(uri, homes, discovered)


def _collect_codex_and_summary_workspaces(
    paths: HostEnvironmentPaths,
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
) -> None:
    if (state_db := paths.codex_home_dir / "state_5.sqlite").is_file():
        with contextlib.suppress(sqlite3.DatabaseError, OSError):
            with contextlib.closing(open_readonly_sqlite(state_db)) as conn:
                for (raw_root,) in conn.execute("SELECT DISTINCT path FROM project_roots").fetchall():
                    if raw_root:
                        _try_register_workspace(str(raw_root), homes, discovered)
                for (raw_cwd,) in conn.execute("SELECT DISTINCT cwd FROM threads WHERE archived = 0").fetchall():
                    if raw_cwd:
                        _try_register_workspace(str(raw_cwd), homes, discovered)
    if (gs_path := paths.codex_home_dir / ".codex-global-state.json").is_file():
        with contextlib.suppress(OSError, ValueError):
            gs = json.loads(gs_path.read_text(encoding="utf-8"))
            local_projs = gs.get("local-projects") if isinstance(gs, dict) else None
            if isinstance(local_projs, dict):
                for entry in local_projs.values():
                    roots = entry.get("rootPaths") if isinstance(entry, dict) else None
                    if isinstance(roots, list):
                        for rp in roots:
                            _try_register_workspace(str(rp), homes, discovered)
    if (sum_db := paths.agy_data_dir / "conversation_summaries.db").is_file():
        with contextlib.suppress(sqlite3.DatabaseError, OSError):
            with contextlib.closing(open_readonly_sqlite(sum_db)) as conn:
                rows = conn.execute("SELECT DISTINCT project_id, workspace_uris FROM conversation_summaries").fetchall()
            for pid, uris_raw in rows:
                if str(pid or "").strip() in FORBIDDEN_AGY_PROJECT_IDS:
                    continue
                for uri in parse_summary_workspace_uris(uris_raw):
                    _try_register_workspace(uri, homes, discovered)


def _collect_sync_state_workspaces(
    paths: HostEnvironmentPaths,
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
) -> None:
    for sub, field_name in (("cursors", "workspace_uri"), ("handoffs", "workspace")):
        target_dir = paths.sync_state_dir / sub
        if not target_dir.is_dir():
            continue
        for jf in sorted(target_dir.glob("*.json")):
            if not jf.is_file():
                continue
            with contextlib.suppress(OSError, ValueError):
                raw = json.loads(jf.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get(field_name):
                    _try_register_workspace(str(raw[field_name]), homes, discovered)


def discover_workspaces(
    paths: HostEnvironmentPaths,
    seed_workspaces: Sequence[str | Path] = (),
) -> tuple[tuple[WorkspacePath, WorkspaceRootUri], ...]:
    homes = candidate_home_dirs(paths)
    discovered: dict[WorkspacePath, WorkspaceRootUri] = {}
    for seed in seed_workspaces:
        _try_register_workspace(seed, homes, discovered)
    _collect_agy_project_workspaces(paths, homes, discovered)
    _collect_codex_and_summary_workspaces(paths, homes, discovered)
    _collect_sync_state_workspaces(paths, homes, discovered)
    return tuple(sorted(discovered.items(), key=lambda pair: str(pair[0])))
