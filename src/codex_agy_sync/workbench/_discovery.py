"""Workspace discovery, Union-Find primitives, and sensitive path exclusions."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Iterable, Sequence
from urllib.parse import unquote, urlsplit

from ..bridge.paths_and_locks import HostEnvironmentPaths, _open_readonly_sqlite
from ..bridge.registry import FORBIDDEN_AGY_PROJECT_IDS, ProjectRegistry
from ..ledger import WorkspacePath, WorkspaceRootUri, parse_workspace_identity


class _UnionFind:
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


def _lexical_abs(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _is_path_equal_or_under(cand: Path, root: Path) -> bool:
    if cand == root or cand.is_relative_to(root):
        return True
    if sys.platform == "darwin":
        cand_parts = tuple(p.casefold() for p in cand.parts)
        root_parts = tuple(p.casefold() for p in root.parts)
        return len(cand_parts) >= len(root_parts) and cand_parts[: len(root_parts)] == root_parts
    return False


def _is_sensitive_excluded_path(candidate: Path, home_dir: Path) -> bool:
    """Return `True` when `candidate` or its resolved target sits under `Archive` or `.private_keys`."""
    cand_variants = (_lexical_abs(candidate), candidate.resolve(strict=False))
    home_abs = _lexical_abs(home_dir)
    for sub in ("Archive", ".private_keys"):
        raw_root = home_abs / sub
        res_root = raw_root.resolve(strict=False)
        for root in (raw_root, res_root):
            for cand in cand_variants:
                if _is_path_equal_or_under(cand, root):
                    return True
    return False


def _candidate_home_dirs(paths: HostEnvironmentPaths) -> tuple[Path, ...]:
    homes: list[Path] = [paths.codex_home_dir.parent, Path.home()]
    if env_home := (os.environ.get("HOME") or "").strip():
        homes.append(Path(env_home).expanduser())
    unique: list[Path] = []
    seen: set[Path] = set()
    for home in homes:
        for variant in (home, home.resolve(strict=False)):
            if variant not in seen:
                seen.add(variant)
                unique.append(variant)
    return tuple(unique)


def normalize_workspace_spec(raw_spec: str | Path) -> str:
    """Normalize a CLI or API workspace specifier while preserving `file://` URIs and lexical symlink paths."""
    text = str(raw_spec or "").strip()
    if not text:
        return ""
    if text.startswith("file://") or "://" in text:
        return text
    expanded = os.path.expanduser(os.path.expandvars(text))
    if expanded.startswith("~") or "$" in expanded:
        return expanded
    return os.path.abspath(expanded)


def _raw_spec_to_candidate_path(raw: str | Path) -> Path | None:
    text = normalize_workspace_spec(raw)
    if not text:
        return None
    if text.startswith("file://"):
        parsed = urlsplit(text)
        if parsed.netloc.lower() not in ("", "localhost"):
            return None
        unquoted = unquote(parsed.path)
        return Path(unquoted) if unquoted.startswith("/") else None
    if "://" in text:
        return None
    return Path(text)


def _is_excluded_for_homes(candidate: Path, homes: Sequence[Path]) -> bool:
    return any(_is_sensitive_excluded_path(candidate, home) for home in homes)


def _try_register_workspace(
    raw_spec: str | Path,
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
) -> None:
    raw_path = _raw_spec_to_candidate_path(raw_spec)
    if raw_path is None or _is_excluded_for_homes(raw_path, homes):
        return
    try:
        ws_path, ws_uri = parse_workspace_identity(raw_spec)
    except ValueError:
        return
    if _is_excluded_for_homes(ws_path, homes) or not ws_path.is_dir():
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
    registry = ProjectRegistry(paths)
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
        for uri in registry._extract_agy_project_folder_uris(data):
            _try_register_workspace(uri, homes, discovered)


def _collect_codex_and_summary_workspaces(
    paths: HostEnvironmentPaths,
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
) -> None:
    if (state_db := paths.codex_home_dir / "state_5.sqlite").is_file():
        with contextlib.suppress(sqlite3.DatabaseError, OSError):
            with contextlib.closing(_open_readonly_sqlite(state_db)) as conn:
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
            with contextlib.closing(_open_readonly_sqlite(sum_db)) as conn:
                rows = conn.execute("SELECT DISTINCT project_id, workspace_uris FROM conversation_summaries").fetchall()
            for pid, uris_raw in rows:
                if str(pid or "").strip() in FORBIDDEN_AGY_PROJECT_IDS:
                    continue
                text = str(uris_raw or "").strip()
                if text.startswith("["):
                    with contextlib.suppress(ValueError):
                        parsed = json.loads(text)
                        if isinstance(parsed, list):
                            for u in parsed:
                                _try_register_workspace(str(u), homes, discovered)
                            continue
                if text:
                    _try_register_workspace(text, homes, discovered)


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
    homes = _candidate_home_dirs(paths)
    discovered: dict[WorkspacePath, WorkspaceRootUri] = {}
    for seed in seed_workspaces:
        _try_register_workspace(seed, homes, discovered)
    _collect_agy_project_workspaces(paths, homes, discovered)
    _collect_codex_and_summary_workspaces(paths, homes, discovered)
    _collect_sync_state_workspaces(paths, homes, discovered)
    return tuple(sorted(discovered.items(), key=lambda pair: str(pair[0])))
