"""Workspace discovery, Union-Find primitives, and sensitive path exclusions."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Sequence

from ..bridge import (
    HostEnvironmentPaths,
    extract_agy_project_folder_uris,
    open_readonly_sqlite,
    optional_select_columns_sql,
    parse_summary_workspace_uris,
    table_column_names,
)
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


def stat_with_wal_fingerprint(path: Path, *, include_wal: bool = True) -> tuple[tuple[int, int, int, int], float]:
    """Return `((mtime_ns, size, wal_mtime_ns, wal_size), mtime_sec)` for WAL-aware cache checks."""
    st = path.stat()
    wal_mtime_ns = 0
    wal_size = 0
    if include_wal:
        wal_path = path.with_name(f"{path.name}-wal")
        with contextlib.suppress(OSError):
            wst = wal_path.stat()
            wal_mtime_ns, wal_size = wst.st_mtime_ns, wst.st_size
    return (st.st_mtime_ns, st.st_size, wal_mtime_ns, wal_size), st.st_mtime


def _try_register_workspace(
    raw_spec: str | Path,
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
    seen_raw: set[str] | None = None,
) -> None:
    if seen_raw is not None:
        spec_key = str(raw_spec or "").strip()
        if not spec_key or spec_key in seen_raw:
            return
        seen_raw.add(spec_key)
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
    seen_raw: set[str] | None = None,
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
            _try_register_workspace(uri, homes, discovered, seen_raw)


AGY_PRELOAD_SUMMARY_COLS = (
    "conversation_id",
    "title",
    "step_count",
    "workspace_uris",
    "project_id",
    "agent_name",
    "parent_conversation_id",
    "nesting_depth",
    "last_modified_time",
    "raw_summary",
)
CODEX_PRELOAD_THREAD_COLS = (
    "id",
    "title",
    "name",
    "preview",
    "cwd",
    "project_id",
    "attached_thread_parent_id",
    "rollout_path",
    "agent_nickname",
    "agent_role",
    "source",
    "recency_at_ms",
    "updated_at_ms",
    "updated_at",
    "archived",
    "has_user_event",
)


@dataclass(frozen=True)
class PreloadedHostTables:
    """Single-open SQLite table snapshot shared across discovery and host catalog scanning."""

    agy_summary_rows: tuple[tuple[Any, ...], ...] = ()
    codex_project_roots: tuple[tuple[str, str], ...] = ()
    codex_thread_rows: tuple[tuple[Any, ...], ...] = ()
    codex_has_user_event_col: bool = False


def load_preloaded_host_tables(
    paths: HostEnvironmentPaths, *, include_raw_summary: bool = True,
) -> PreloadedHostTables:
    """Open `state_5.sqlite` and `conversation_summaries.db` at most once each for read-only indexing."""
    project_roots: list[tuple[str, str]] = []
    thread_rows: list[tuple[Any, ...]] = []
    has_user_event_col = False
    state_db = paths.codex_home_dir / "state_5.sqlite"
    if state_db.is_file():
        with contextlib.suppress(sqlite3.DatabaseError, OSError):
            with contextlib.closing(open_readonly_sqlite(state_db)) as conn:
                rcols = table_column_names(conn, "project_roots")
                if "path" in rcols:
                    rsql = f"SELECT {optional_select_columns_sql(rcols, ('project_id', 'path'))} FROM project_roots"
                    for pid_val, raw_root in conn.execute(rsql).fetchall():
                        project_roots.append((str(pid_val or "").strip(), str(raw_root or "")))
                tcols = table_column_names(conn, "threads")
                if "id" in tcols:
                    has_user_event_col = "has_user_event" in tcols
                    tsql = f"SELECT {optional_select_columns_sql(tcols, CODEX_PRELOAD_THREAD_COLS)} FROM threads"
                    thread_rows = [tuple(r) for r in conn.execute(tsql).fetchall()]

    agy_rows: list[tuple[Any, ...]] = []
    sum_db = paths.agy_data_dir / "conversation_summaries.db"
    if sum_db.is_file():
        with contextlib.suppress(sqlite3.DatabaseError, OSError):
            with contextlib.closing(open_readonly_sqlite(sum_db)) as conn:
                scols = table_column_names(conn, "conversation_summaries")
                if "conversation_id" in scols:
                    order_sql = " ORDER BY last_modified_time DESC" if "last_modified_time" in scols else ""
                    eff_scols = scols if include_raw_summary else (scols - {"raw_summary"})
                    ssql = f"SELECT {optional_select_columns_sql(eff_scols, AGY_PRELOAD_SUMMARY_COLS)} FROM conversation_summaries{order_sql}"
                    agy_rows = [tuple(r) for r in conn.execute(ssql).fetchall()]

    return PreloadedHostTables(
        agy_summary_rows=tuple(agy_rows),
        codex_project_roots=tuple(project_roots),
        codex_thread_rows=tuple(thread_rows),
        codex_has_user_event_col=has_user_event_col,
    )


def _collect_codex_and_summary_workspaces(
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
    preloaded: PreloadedHostTables,
    seen_raw: set[str] | None = None,
) -> None:
    for _, raw_root in preloaded.codex_project_roots:
        if raw_root:
            _try_register_workspace(raw_root, homes, discovered, seen_raw)
    for row in preloaded.codex_thread_rows:
        cwd, archived = row[4], row[14]
        if not archived and cwd:
            _try_register_workspace(str(cwd), homes, discovered, seen_raw)
    for row in preloaded.agy_summary_rows:
        uris_raw, pid = row[3], row[4]
        if str(pid or "").strip() in FORBIDDEN_AGY_PROJECT_IDS:
            continue
        for uri in parse_summary_workspace_uris(uris_raw):
            _try_register_workspace(uri, homes, discovered, seen_raw)


def _collect_sync_state_workspaces(
    paths: HostEnvironmentPaths,
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
    seen_raw: set[str] | None = None,
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
                    _try_register_workspace(str(raw[field_name]), homes, discovered, seen_raw)


def _collect_codex_global_state_workspaces(
    paths: HostEnvironmentPaths,
    homes: Sequence[Path],
    discovered: dict[WorkspacePath, WorkspaceRootUri],
    seen_raw: set[str] | None = None,
) -> None:
    gs_path = paths.codex_home_dir / ".codex-global-state.json"
    if not gs_path.is_file():
        return
    with contextlib.suppress(OSError, ValueError):
        gs = json.loads(gs_path.read_text(encoding="utf-8"))
        local_projs = gs.get("local-projects") if isinstance(gs, dict) else None
        if isinstance(local_projs, dict):
            for entry in local_projs.values():
                roots = entry.get("rootPaths") if isinstance(entry, dict) else None
                if isinstance(roots, list):
                    for rp in roots:
                        _try_register_workspace(str(rp), homes, discovered, seen_raw)


def discover_workspaces(
    paths: HostEnvironmentPaths,
    seed_workspaces: Sequence[str | Path] = (),
    *,
    preloaded: PreloadedHostTables | None = None,
) -> tuple[tuple[WorkspacePath, WorkspaceRootUri], ...]:
    tables = preloaded if preloaded is not None else load_preloaded_host_tables(paths)
    homes = candidate_home_dirs(paths)
    discovered: dict[WorkspacePath, WorkspaceRootUri] = {}
    seen_raw: set[str] = set()
    for seed in seed_workspaces:
        _try_register_workspace(seed, homes, discovered, seen_raw)
    _collect_agy_project_workspaces(paths, homes, discovered, seen_raw)
    _collect_codex_and_summary_workspaces(homes, discovered, tables, seen_raw)
    _collect_codex_global_state_workspaces(paths, homes, discovered, seen_raw)
    _collect_sync_state_workspaces(paths, homes, discovered, seen_raw)
    return tuple(sorted(discovered.items(), key=lambda pair: str(pair[0])))
