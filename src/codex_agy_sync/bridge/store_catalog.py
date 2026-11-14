"""SQLite DDL initializers, canonical lineage/URI parsers, and read-only host store scanners."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from ..codecs import decode_trajectory_metadata_proto, iter_codex_rollout_lines
from ..events import CascadeId, CodexThreadId, StepIndex, SubagentLineage
from ..workspace import (
    AgyProjectId,
    CodexProjectId,
    WorkspacePath,
    WorkspaceRootUri,
    parse_workspace_identity,
    validate_session_id,
)
from .paths_and_locks import HostEnvironmentPaths, open_readonly_sqlite, validated_child_path

CODEX_ROOT_CLIENT_SOURCES = frozenset({"desktop", "vscode", "cli", "exec"})

_EMPTY_META_LINEAGE: dict[str, Any] = {
    "parent_conversation_id": "",
    "root_conversation_id": "",
    "workspace_uris": [],
    "agent_type_name": "",
    "agent_role": "",
    "nesting_depth": 0,
    "project_id": "",
}


def ensure_agy_conversation_db_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS `trajectory_meta` (
            `trajectory_id` text, `cascade_id` text, `trajectory_type` integer, `source` integer,
            PRIMARY KEY (`trajectory_id`)
        );
        CREATE TABLE IF NOT EXISTS `steps` (
            `idx` integer, `step_type` integer NOT NULL DEFAULT 0, `status` integer NOT NULL DEFAULT 0,
            `has_subtrajectory` numeric NOT NULL DEFAULT false, `metadata` blob, `error_details` blob,
            `permissions` blob, `task_details` blob, `render_info` blob, `step_payload` blob,
            `step_format` integer NOT NULL DEFAULT 0, PRIMARY KEY (`idx`)
        );
        CREATE INDEX IF NOT EXISTS `idx_steps_status` ON `steps`(`status`);
        CREATE INDEX IF NOT EXISTS `idx_steps_step_type` ON `steps`(`step_type`);
        CREATE TABLE IF NOT EXISTS `gen_metadata` (`idx` integer, `data` blob, `size` integer NOT NULL DEFAULT 0, PRIMARY KEY (`idx`));
        CREATE TABLE IF NOT EXISTS `executor_metadata` (`idx` integer, `data` blob, PRIMARY KEY (`idx`));
        CREATE TABLE IF NOT EXISTS `parent_references` (`idx` integer, `data` blob, PRIMARY KEY (`idx`));
        CREATE TABLE IF NOT EXISTS `trajectory_metadata_blob` (`id` text DEFAULT "main", `data` blob, PRIMARY KEY (`id`));
        CREATE TABLE IF NOT EXISTS `battle_mode_infos` (`idx` integer, `data` blob, PRIMARY KEY (`idx`));
        """
    )
    if int((conn.execute("PRAGMA user_version").fetchone() or (0,))[0] or 0) == 0:
        conn.execute("PRAGMA user_version = 1")


def ensure_agy_summaries_db_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS `conversation_summaries` (
            `conversation_id` text, `title` text NOT NULL DEFAULT "", `preview` text NOT NULL DEFAULT "",
            `step_count` integer NOT NULL DEFAULT 0, `last_modified_time` datetime NOT NULL,
            `workspace_uris` text NOT NULL, `status` text NOT NULL DEFAULT "", `source` text NOT NULL DEFAULT "",
            `project_id` text NOT NULL DEFAULT "", `agent_name` text NOT NULL DEFAULT "",
            `parent_conversation_id` text NOT NULL DEFAULT "", `nesting_depth` integer NOT NULL DEFAULT 0,
            `battle_id` text NOT NULL DEFAULT "", `winning_conversation_id` text NOT NULL DEFAULT "",
            `not_fully_idle` numeric NOT NULL DEFAULT false, `killed` numeric NOT NULL DEFAULT false,
            `last_user_input_time` datetime NOT NULL, `last_user_input_step_index` integer NOT NULL DEFAULT -1,
            `app_data_dir` text NOT NULL DEFAULT "", `raw_summary` blob, `group_id` text NOT NULL DEFAULT "",
            PRIMARY KEY (`conversation_id`)
        );
        CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_user_input_time` ON `conversation_summaries`(`last_user_input_time`);
        CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_modified_time` ON `conversation_summaries`(`last_modified_time`);
        """
    )


def ensure_codex_state_db_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_roots (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, path TEXT NOT NULL, PRIMARY KEY (project_id, position)
        );
        CREATE TABLE IF NOT EXISTS project_idempotency_keys (
            key TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,
            cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
            approval_mode TEXT NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
            has_user_event INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
            archived_at INTEGER, git_sha TEXT, git_branch TEXT, git_origin_url TEXT,
            cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '',
            agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT 'enabled',
            model TEXT, reasoning_effort TEXT, agent_path TEXT, created_at_ms INTEGER,
            updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT '',
            recency_at INTEGER NOT NULL DEFAULT 0, recency_at_ms INTEGER NOT NULL DEFAULT 0,
            history_mode TEXT NOT NULL DEFAULT 'legacy', name TEXT, is_pinned INTEGER NOT NULL DEFAULT 0,
            thread_section_id TEXT, section_position INTEGER, section_entered_at_ms INTEGER,
            project_id TEXT, originator TEXT, daybreak_enabled BOOLEAN,
            creator_user_id TEXT, creator_account_id TEXT, attached_thread_parent_id TEXT
        );
        """
    )


def readonly_fetchone(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...] | None:
    if not db_path.is_file():
        return None
    try:
        with contextlib.closing(open_readonly_sqlite(db_path)) as conn:
            row = conn.execute(sql, params).fetchone()
        return tuple(row) if row is not None else None
    except sqlite3.OperationalError:
        return None


def safe_valid_session_id(raw_id: object, label: str) -> str:
    text = str(raw_id or "").strip()
    if not text:
        return ""
    try:
        return validate_session_id(text, label=label)
    except ValueError:
        return ""


def parse_summary_workspace_uris(raw_uris: object) -> list[str]:
    text = str(raw_uris or "").strip()
    if not text:
        return []
    if text.startswith("["):
        with contextlib.suppress(ValueError):
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(u).strip() for u in parsed if str(u).strip()]
    return [text]


def extract_agy_project_folder_uris(data: Mapping[str, Any]) -> list[str]:
    uris: list[str] = []
    if not isinstance(data, Mapping):
        return uris
    project_resources = data.get("projectResources")
    resources = project_resources.get("resources", []) if isinstance(project_resources, dict) else []
    if isinstance(resources, list):
        for res in resources:
            if not isinstance(res, dict):
                continue
            if isinstance(res.get("folderUri"), str):
                uris.append(res["folderUri"])
            git_folder = res.get("gitFolder")
            if isinstance(git_folder, dict) and isinstance(git_folder.get("folderUri"), str):
                uris.append(git_folder["folderUri"])
    return uris


def uris_match_workspace(raw_uris: Sequence[str], target_uri: WorkspaceRootUri) -> bool:
    for raw_u in raw_uris:
        try:
            if parse_workspace_identity(str(raw_u))[1] == target_uri:
                return True
        except ValueError:
            continue
    return False


def compute_codex_nesting_depth(
    raw_source: str | None,
    src_is_sub: bool,
    src_depth: int,
    parent_tid_str: str,
    eff_c_type: str,
    eff_c_role: str,
) -> int:
    """Resolve Codex thread `nesting_depth` while keeping root client forks (`desktop`/`vscode`/`cli`/`exec`) at `0`."""
    if src_is_sub:
        return max(1, src_depth)
    if str(raw_source or "").strip().lower() in CODEX_ROOT_CLIENT_SOURCES:
        return 0
    return 1 if (parent_tid_str and (eff_c_type or eff_c_role)) else 0


def parse_codex_thread_source_lineage(raw_source: str | None) -> tuple[bool, str, int, str, str]:
    text = str(raw_source or "").strip()
    if not text.startswith("{"):
        return False, "", 0, "", ""
    try:
        parsed = json.loads(text)
    except Exception:
        return False, "", 0, "", ""
    if not isinstance(parsed, dict) or "subagent" not in parsed:
        return False, "", 0, "", ""
    sub = parsed.get("subagent")
    if isinstance(sub, str):
        return True, "", 1, sub.strip(), ""
    if isinstance(sub, dict):
        ts = sub.get("thread_spawn")
        if isinstance(ts, dict):
            p_tid = str(ts.get("parent_thread_id") or "").strip()
            try:
                depth = max(1, int(ts.get("depth") or 1))
            except (TypeError, ValueError):
                depth = 1
            return True, p_tid, depth, str(ts.get("agent_role") or "").strip(), str(ts.get("agent_nickname") or "").strip()
        if "other" in sub:
            return True, "", 1, str(sub.get("other") or "").strip(), ""
    return True, "", 1, "", ""


def build_combined_lineage(agy_info: Mapping[str, Any], codex_info: Mapping[str, Any]) -> SubagentLineage:
    p_cid: CascadeId | None = agy_info.get("parent_cascade_id")
    r_cid: CascadeId | None = agy_info.get("root_cascade_id") or p_cid
    p_tid: CodexThreadId | None = codex_info.get("parent_codex_thread_id")
    spawn_idx: StepIndex | None = agy_info.get("spawn_step_index")
    depth = max(
        0,
        int(agy_info.get("nesting_depth") or 0),
        int(codex_info.get("nesting_depth") or 0),
        1 if spawn_idx is not None else 0,
    )
    return SubagentLineage(
        parent_cascade_id=p_cid,
        root_cascade_id=r_cid,
        parent_codex_thread_id=p_tid,
        nesting_depth=depth,
        agent_type_name=str(agy_info.get("agent_type_name") or codex_info.get("agent_type_name") or "").strip(),
        agent_role=str(agy_info.get("agent_role") or codex_info.get("agent_role") or "").strip(),
        spawn_step_index=spawn_idx,
    )


def count_codex_rollout_user_turns(raw_rpath: object) -> int:
    if not raw_rpath:
        return 1
    cand = Path(str(raw_rpath))
    if not cand.is_file():
        zst_cand = Path(f"{cand}.zst")
        cand = zst_cand if zst_cand.is_file() else cand
    if not cand.is_file():
        return 1
    try:
        count = sum(
            1
            for row in iter_codex_rollout_lines(cand)
            if '"role":"user"' in row.raw_json or '"role": "user"' in row.raw_json
        )
        return max(1, count)
    except Exception:
        return 1


def discover_agy_subagent_sidecars(paths: HostEnvironmentPaths) -> dict[str, dict[str, Any]]:
    sidecars: dict[str, dict[str, Any]] = {}
    brain_dir = paths.agy_data_dir / "brain"
    if not brain_dir.is_dir():
        return sidecars
    for sidecar_file in sorted(brain_dir.glob("*/.system_generated/subagents/*.json")):
        try:
            raw = json.loads(sidecar_file.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        parent_raw = sidecar_file.parts[-4] if len(sidecar_file.parts) >= 4 else ""
        child_cid = safe_valid_session_id(raw.get("conversationId") or sidecar_file.stem, "child_cascade_id")
        if not child_cid or (parent_raw and not safe_valid_session_id(parent_raw, "parent_cascade_id")):
            continue
        desc = raw.get("subagentDescriptor")
        spawn_raw = raw.get("spawnStepIndex")
        ws_uris = raw.get("workspaceUris")
        sidecars[child_cid] = {
            "parent_cascade_id": parent_raw,
            "child_cascade_id": child_cid,
            "agent_type_name": str(desc.get("typeName") or "").strip() if isinstance(desc, dict) else "",
            "agent_role": str(desc.get("role") or "").strip() if isinstance(desc, dict) else "",
            "spawn_step_index": StepIndex(int(spawn_raw)) if isinstance(spawn_raw, int) and spawn_raw >= 0 else None,
            "workspace_uris": [str(u) for u in ws_uris] if isinstance(ws_uris, list) else [],
        }
    return sidecars


def scan_agy_summaries_db(
    paths: HostEnvironmentPaths,
    agy_project_id: AgyProjectId,
    workspace_uri: WorkspaceRootUri,
    sidecars: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    agy_sessions: dict[str, dict[str, Any]] = {}
    sum_db = paths.agy_data_dir / "conversation_summaries.db"
    if not sum_db.is_file():
        return agy_sessions
    try:
        with contextlib.closing(open_readonly_sqlite(sum_db)) as conn:
            rows = conn.execute(
                "SELECT conversation_id, title, step_count, workspace_uris, project_id, agent_name, "
                "parent_conversation_id, nesting_depth FROM conversation_summaries ORDER BY last_modified_time DESC"
            ).fetchall()
    except sqlite3.OperationalError:
        return agy_sessions

    for cid, title, scount, uris_raw, pid, agent_name, parent_cid, nesting_depth in rows:
        cid_str = safe_valid_session_id(cid, "cascade_id")
        if not cid_str:
            continue
        sc_info = sidecars.get(cid_str, {})
        matches_ws = str(pid or "") == str(agy_project_id)
        if not matches_ws:
            cand_uris = list(sc_info.get("workspace_uris", [])) + parse_summary_workspace_uris(uris_raw)
            matches_ws = uris_match_workspace(cand_uris, workspace_uri)
        if not matches_ws:
            continue
        eff_parent = safe_valid_session_id(parent_cid or sc_info.get("parent_cascade_id"), "parent_cascade_id")
        agy_sessions[cid_str] = {
            "cascade_id": CascadeId(cid_str),
            "title": str(title or "").strip() or "Untitled Session",
            "step_count": int(scount or 0),
            "parent_cascade_id": CascadeId(eff_parent) if eff_parent else None,
            "root_cascade_id": None,
            "nesting_depth": max(0, int(nesting_depth or 0), 1 if sc_info else 0),
            "agent_type_name": str(sc_info.get("agent_type_name") or "").strip(),
            "agent_role": str(sc_info.get("agent_role") or agent_name or "").strip(),
            "spawn_step_index": sc_info.get("spawn_step_index"),
        }
    return agy_sessions


def enrich_agy_session_from_conv_db(
    db_file: Path,
    agy_project_id: AgyProjectId,
    workspace_uri: WorkspaceRootUri,
    sidecars: Mapping[str, dict[str, Any]],
    agy_sessions: dict[str, dict[str, Any]],
) -> None:
    cid_str = safe_valid_session_id(db_file.stem, "cascade_id")
    if not cid_str:
        return
    sc_info = sidecars.get(cid_str, {})
    try:
        with contextlib.closing(open_readonly_sqlite(db_file)) as conn:
            tblob = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
            meta = decode_trajectory_metadata_proto(bytes(tblob[0])) if (tblob and tblob[0]) else _EMPTY_META_LINEAGE
            if cid_str in agy_sessions:
                entry = agy_sessions[cid_str]
                eff_parent = safe_valid_session_id(
                    str(entry["parent_cascade_id"] or "") or meta["parent_conversation_id"] or sc_info.get("parent_cascade_id"),
                    "parent_cascade_id",
                )
                eff_root = safe_valid_session_id(meta["root_conversation_id"] or eff_parent, "root_cascade_id")
                entry["parent_cascade_id"] = CascadeId(eff_parent) if eff_parent else None
                entry["root_cascade_id"] = CascadeId(eff_root) if eff_root else None
                entry["nesting_depth"] = max(0, int(entry["nesting_depth"]), int(meta["nesting_depth"]), 1 if sc_info else 0)
                entry["agent_type_name"] = (entry["agent_type_name"] or meta["agent_type_name"] or str(sc_info.get("agent_type_name") or "")).strip()
                entry["agent_role"] = (entry["agent_role"] or meta["agent_role"] or str(sc_info.get("agent_role") or "")).strip()
                return

            eff_parent = safe_valid_session_id(meta["parent_conversation_id"] or sc_info.get("parent_cascade_id"), "parent_cascade_id")
            matches_ws = (
                meta["project_id"] == str(agy_project_id)
                or uris_match_workspace(list(meta["workspace_uris"]) + list(sc_info.get("workspace_uris", [])), workspace_uri)
                or bool(eff_parent and eff_parent in agy_sessions)
            )
            if not matches_ws:
                return
            scount_row = conn.execute("SELECT COUNT(*) FROM steps").fetchone()
            eff_root = safe_valid_session_id(meta["root_conversation_id"] or eff_parent, "root_cascade_id")
            eff_role = (meta["agent_role"] or str(sc_info.get("agent_role") or "")).strip()
            agy_sessions[cid_str] = {
                "cascade_id": CascadeId(cid_str),
                "title": eff_role or "Untitled Session",
                "step_count": int(scount_row[0]) if scount_row else 0,
                "parent_cascade_id": CascadeId(eff_parent) if eff_parent else None,
                "root_cascade_id": CascadeId(eff_root) if eff_root else None,
                "nesting_depth": max(0, int(meta["nesting_depth"]), 1 if sc_info else 0),
                "agent_type_name": (meta["agent_type_name"] or str(sc_info.get("agent_type_name") or "")).strip(),
                "agent_role": eff_role,
                "spawn_step_index": sc_info.get("spawn_step_index"),
            }
    except sqlite3.OperationalError:
        return


def scan_codex_state_sessions(
    paths: HostEnvironmentPaths,
    codex_project_id: CodexProjectId,
    workspace_path: WorkspacePath,
) -> dict[str, dict[str, Any]]:
    codex_sessions: dict[str, dict[str, Any]] = {}
    state_db = paths.codex_home_dir / "state_5.sqlite"
    if not state_db.is_file():
        return codex_sessions
    try:
        with contextlib.closing(open_readonly_sqlite(state_db)) as conn:
            rows = conn.execute(
                "SELECT id, title, preview, cwd, project_id, attached_thread_parent_id, rollout_path, "
                "agent_nickname, agent_role, source FROM threads WHERE archived = 0 ORDER BY recency_at_ms DESC, updated_at DESC"
            ).fetchall()
    except sqlite3.OperationalError:
        return codex_sessions

    for tid, title, preview, cwd, pid, parent_tid, rpath, agent_nickname, agent_role, raw_source in rows:
        tid_str = safe_valid_session_id(tid, "codex_thread_id")
        if not tid_str:
            continue
        matches_ws = str(pid or "") == str(codex_project_id)
        if not matches_ws and cwd:
            with contextlib.suppress(ValueError):
                matches_ws = parse_workspace_identity(str(cwd))[0] == workspace_path
        if not matches_ws:
            continue
        raw_src = str(raw_source) if raw_source is not None else None
        src_is_sub, src_parent_tid, src_depth, src_type, src_nick = parse_codex_thread_source_lineage(raw_src)
        parent_tid_str = safe_valid_session_id(str(parent_tid or "").strip() or src_parent_tid, "parent_codex_thread_id")
        eff_c_type = str(agent_role or "").strip() or src_type
        eff_c_role = str(agent_nickname or "").strip() or src_nick
        c_depth = compute_codex_nesting_depth(raw_src, src_is_sub, src_depth, parent_tid_str, eff_c_type, eff_c_role)
        codex_sessions[tid_str] = {
            "codex_thread_id": CodexThreadId(tid_str),
            "title": str(title or preview or "").strip() or "Untitled Session",
            "turn_count": count_codex_rollout_user_turns(rpath),
            "parent_codex_thread_id": CodexThreadId(parent_tid_str) if (parent_tid_str and c_depth > 0) else None,
            "nesting_depth": c_depth,
            "agent_type_name": eff_c_type,
            "agent_role": eff_c_role,
        }
    return codex_sessions


def read_agy_session_lineage(
    paths: HostEnvironmentPaths,
    cid_key: str,
    *,
    sidecars: Mapping[str, dict[str, Any]] | None = None,
) -> tuple[str, str, int, str, str, StepIndex | None, bool]:
    sc_map = discover_agy_subagent_sidecars(paths) if sidecars is None else sidecars
    sc_info = sc_map.get(cid_key, {})
    parent_cid = str(sc_info.get("parent_cascade_id") or "").strip()
    root_cid = ""
    depth = 0
    type_name = str(sc_info.get("agent_type_name") or "").strip()
    role = str(sc_info.get("agent_role") or "").strip()
    spawn_idx: StepIndex | None = sc_info.get("spawn_step_index")
    sum_row = readonly_fetchone(
        paths.agy_data_dir / "conversation_summaries.db",
        "SELECT agent_name, parent_conversation_id, nesting_depth FROM conversation_summaries WHERE conversation_id = ?",
        (cid_key,),
    )
    if sum_row:
        role = role or str(sum_row[0] or "").strip()
        parent_cid = parent_cid or str(sum_row[1] or "").strip()
        depth = max(depth, int(sum_row[2] or 0))  # type: ignore[arg-type]
    tblob = readonly_fetchone(
        validated_child_path(paths.agy_data_dir, "conversations", f"{cid_key}.db", label="cascade_id"),
        "SELECT data FROM trajectory_metadata_blob WHERE id = 'main'",
    )
    if tblob and tblob[0]:
        meta = decode_trajectory_metadata_proto(bytes(tblob[0]))  # type: ignore[arg-type]
        parent_cid = parent_cid or str(meta["parent_conversation_id"] or "")
        root_cid = root_cid or str(meta["root_conversation_id"] or "")
        type_name = type_name or str(meta["agent_type_name"] or "")
        role = role or str(meta["agent_role"] or "")
        depth = max(depth, int(meta["nesting_depth"] or 0))
    return parent_cid, root_cid, depth, type_name, role, spawn_idx, bool(sc_info)


def read_codex_session_lineage(paths: HostEnvironmentPaths, tid_key: str) -> tuple[str, int, str, str]:
    row = readonly_fetchone(
        paths.codex_home_dir / "state_5.sqlite",
        "SELECT attached_thread_parent_id, agent_role, agent_nickname, source FROM threads WHERE id = ?",
        (tid_key,),
    )
    if not row:
        return "", 0, "", ""
    raw_src = str(row[3]) if row[3] is not None else None
    src_is_sub, src_parent_tid, src_depth, src_type, src_nick = parse_codex_thread_source_lineage(raw_src)
    c_parent = str(row[0] or "").strip() or src_parent_tid
    c_type = str(row[1] or "").strip() or src_type
    c_nick = str(row[2] or "").strip() or src_nick
    c_depth = compute_codex_nesting_depth(raw_src, src_is_sub, src_depth, c_parent, c_type, c_nick)
    return (c_parent if c_depth > 0 else ""), c_depth, c_type, c_nick
