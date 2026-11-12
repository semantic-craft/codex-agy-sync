"""Bidirectional workspace-to-project pairing registry, schema initializers, and session discovery."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping, Tuple

from ..codecs import decode_trajectory_metadata_proto, iter_codex_rollout_lines
from ..ledger import (
    AgyProjectId, CascadeId, CodexLegacyProjectId, CodexProjectId, CodexThreadId,
    StepIndex, SubagentLineage, WorkspacePath, WorkspaceRootUri,
    parse_workspace_identity, validate_session_id,
)
from .paths_and_locks import (
    HostEnvironmentPaths, _atomic_write_text, _deterministic_uuid, _open_readonly_sqlite,
)


def _ensure_agy_conversation_db_schema(conn: sqlite3.Connection) -> None:
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


def _ensure_agy_summaries_db_schema(conn: sqlite3.Connection) -> None:
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


def _ensure_codex_state_db_schema(conn: sqlite3.Connection) -> None:
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


FORBIDDEN_AGY_PROJECT_IDS: frozenset[str] = frozenset(
    {"", "default-cli-project", "outside-of-project", "null", "none"}
)


@dataclass(frozen=True)
class WorkspaceProjectBinding:
    """Verified bidirectional project identity for a single workspace folder."""

    workspace_path: WorkspacePath
    workspace_uri: WorkspaceRootUri
    workspace_name: str
    agy_project_id: AgyProjectId
    codex_project_id: CodexProjectId
    codex_legacy_project_id: CodexLegacyProjectId


@dataclass(frozen=True)
class PairedSessionDescriptor:
    """Summary of a discovered or paired conversation across Antigravity and Codex."""

    workspace: WorkspaceProjectBinding
    cascade_id: CascadeId | None
    codex_thread_id: CodexThreadId | None
    title: str
    agy_step_count: int
    codex_turn_count: int
    parent_cascade_id: CascadeId | None = None
    parent_codex_thread_id: CodexThreadId | None = None
    lineage: SubagentLineage = SubagentLineage()


def _parse_agy_trajectory_metadata_lineage(raw_blob: bytes) -> dict[str, Any]:
    return decode_trajectory_metadata_proto(raw_blob)


def _parse_codex_thread_source_lineage(raw_source: str | None) -> tuple[bool, str, int, str, str]:
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


def _safe_valid_session_id(raw_id: object, label: str) -> str:
    text = str(raw_id or "").strip()
    if not text:
        return ""
    try:
        return validate_session_id(text, label=label)
    except ValueError:
        return ""


def _count_codex_rollout_user_turns(raw_rpath: object) -> int:
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
            1 for row in iter_codex_rollout_lines(cand)
            if '"role":"user"' in row.raw_json or '"role": "user"' in row.raw_json
        )
        return max(1, count)
    except Exception:
        return 1


def _build_combined_lineage(agy_info: Mapping[str, Any], codex_info: Mapping[str, Any]) -> SubagentLineage:
    p_cid: CascadeId | None = agy_info.get("parent_cascade_id")
    r_cid: CascadeId | None = agy_info.get("root_cascade_id") or p_cid
    p_tid: CodexThreadId | None = codex_info.get("parent_codex_thread_id")
    spawn_idx: StepIndex | None = agy_info.get("spawn_step_index")
    depth = max(
        0, int(agy_info.get("nesting_depth") or 0), int(codex_info.get("nesting_depth") or 0),
        1 if spawn_idx is not None else 0,
    )
    return SubagentLineage(
        parent_cascade_id=p_cid, root_cascade_id=r_cid, parent_codex_thread_id=p_tid,
        nesting_depth=depth,
        agent_type_name=str(agy_info.get("agent_type_name") or codex_info.get("agent_type_name") or "").strip(),
        agent_role=str(agy_info.get("agent_role") or codex_info.get("agent_role") or "").strip(),
        spawn_step_index=spawn_idx,
    )


_EMPTY_META_LINEAGE: dict[str, Any] = {
    "parent_conversation_id": "", "root_conversation_id": "", "workspace_uris": [],
    "agent_type_name": "", "agent_role": "", "nesting_depth": 0, "project_id": "",
}


class ProjectRegistry:
    """Owns bidirectional workspace-to-project resolution and session discovery."""

    def __init__(self, paths: HostEnvironmentPaths) -> None:
        self._paths = paths

    def _extract_agy_project_folder_uris(self, data: Mapping[str, Any]) -> list[str]:
        uris: list[str] = []
        resources = data.get("projectResources", {}).get("resources", [])
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

    def _find_readonly_agy_project_id(self, ws_uri: WorkspaceRootUri) -> AgyProjectId | None:
        projects_dir = self._paths.gemini_config_dir / "projects"
        if not projects_dir.is_dir():
            return None
        for pfile in sorted(projects_dir.glob("*.json")):
            try:
                data = json.loads(pfile.read_text(encoding="utf-8"))
            except Exception:
                continue
            cand_id = str(data.get("id") or pfile.stem).strip()
            if not cand_id or cand_id in FORBIDDEN_AGY_PROJECT_IDS:
                continue
            if self._uris_match_workspace(self._extract_agy_project_folder_uris(data), ws_uri):
                return AgyProjectId(cand_id)
        return None

    def _ensure_agy_project(self, ws_path: WorkspacePath, ws_uri: WorkspaceRootUri, ws_name: str) -> AgyProjectId:
        _ = ws_path
        existing = self._find_readonly_agy_project_id(ws_uri)
        if existing is not None:
            return existing
        projects_dir = self._paths.gemini_config_dir / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        new_pid = _deterministic_uuid("agy-project", str(ws_uri))
        project_doc = {
            "id": new_pid, "name": ws_name,
            "projectResources": {"resources": [{"folderUri": str(ws_uri)}]},
            "permissionGrants": {"v2Migrated": True}, "settings": {}, "isWorkspaceOnly": False,
        }
        _atomic_write_text(projects_dir / f"{new_pid}.json", json.dumps(project_doc, ensure_ascii=False, indent=2) + "\n")
        return AgyProjectId(new_pid)

    def _find_readonly_codex_project_id(self, ws_path: WorkspacePath) -> str | None:
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        if not state_db.is_file():
            return None
        try:
            conn = _open_readonly_sqlite(state_db)
            try:
                for pid_val, raw_root in conn.execute("SELECT project_id, path FROM project_roots").fetchall():
                    try:
                        norm_p, _ = parse_workspace_identity(str(raw_root))
                    except ValueError:
                        continue
                    if norm_p == ws_path and str(pid_val or "").strip():
                        return str(pid_val).strip()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return None
        return None

    def _load_codex_global_state(self) -> tuple[Path, dict[str, Any]]:
        global_state_path = self._paths.codex_home_dir / ".codex-global-state.json"
        if global_state_path.is_file():
            try:
                loaded = json.loads(global_state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return global_state_path, loaded
            except Exception:
                pass
        return global_state_path, {}

    def _find_legacy_codex_project_id(self, gs_data: Mapping[str, Any], ws_path: WorkspacePath) -> str | None:
        local_projects = gs_data.get("local-projects")
        if not isinstance(local_projects, dict):
            return None
        for lpid, lentry in local_projects.items():
            if not isinstance(lentry, dict) or not str(lpid).strip():
                continue
            roots = lentry.get("rootPaths") or []
            if isinstance(roots, list):
                for rp in roots:
                    try:
                        if parse_workspace_identity(str(rp))[0] == ws_path:
                            return str(lpid).strip()
                    except ValueError:
                        continue
        return None

    def _ensure_codex_project(
        self, ws_path: WorkspacePath, ws_uri: WorkspaceRootUri, ws_name: str,
    ) -> tuple[CodexProjectId, CodexLegacyProjectId]:
        self._paths.codex_home_dir.mkdir(parents=True, exist_ok=True)
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        now_ms = int(time.time() * 1000)
        ws_str = str(ws_path)
        existing_pid = self._find_readonly_codex_project_id(ws_path)
        global_state_path, gs_data = self._load_codex_global_state()
        legacy_pid = self._find_legacy_codex_project_id(gs_data, ws_path) or _deterministic_uuid(
            "codex-legacy-project", str(ws_uri)
        )

        if not existing_pid:
            existing_pid = _deterministic_uuid("codex-project", str(ws_uri))
            conn = sqlite3.connect(str(state_db), timeout=10.0)
            try:
                _ensure_codex_state_db_schema(conn)
                max_pos_row = conn.execute("SELECT COALESCE(MAX(position), -1) FROM projects").fetchone()
                next_pos = int(max_pos_row[0]) + 1 if max_pos_row else 0
                conn.execute(
                    "INSERT OR IGNORE INTO projects (id, name, metadata, position, created_at_ms, updated_at_ms) VALUES (?, ?, '{}', ?, ?, ?)",
                    (existing_pid, ws_name, next_pos, now_ms, now_ms),
                )
                conn.execute("INSERT OR REPLACE INTO project_roots (project_id, position, path) VALUES (?, 0, ?)", (existing_pid, ws_str))
                conn.execute(
                    "INSERT OR REPLACE INTO project_idempotency_keys (key, project_id, created_at_ms) VALUES (?, ?, ?)",
                    (f"codex-agy-sync:project:{ws_uri}", existing_pid, now_ms),
                )
                conn.commit()
            finally:
                conn.close()

        local_projects = gs_data.get("local-projects")
        if not isinstance(local_projects, dict):
            local_projects = {}
            gs_data["local-projects"] = local_projects
        local_projects[legacy_pid] = {
            "id": legacy_pid, "name": ws_name, "rootPaths": [ws_str],
            "createdAt": local_projects.get(legacy_pid, {}).get("createdAt", now_ms), "updatedAt": now_ms,
        }
        host_map = gs_data.get("app-server-project-id-by-legacy-project-id-by-host")
        if not isinstance(host_map, dict):
            host_map = {}
            gs_data["app-server-project-id-by-legacy-project-id-by-host"] = host_map
        host_key = f"local:{self._paths.codex_home_dir}"
        if not isinstance(host_map.get(host_key), dict):
            host_map[host_key] = {}
        host_map[host_key][legacy_pid] = existing_pid

        project_order = gs_data.get("project-order")
        if not isinstance(project_order, list):
            project_order = []
            gs_data["project-order"] = project_order
        if legacy_pid not in project_order:
            project_order.append(legacy_pid)

        _atomic_write_text(global_state_path, json.dumps(gs_data, ensure_ascii=False, indent=2) + "\n")
        return CodexProjectId(existing_pid), CodexLegacyProjectId(legacy_pid)

    def ensure_workspace_binding(self, workspace: str | Path, prefer_online_rpc: bool = True) -> WorkspaceProjectBinding:
        _ = prefer_online_rpc
        ws_path, ws_uri = parse_workspace_identity(workspace)
        ws_name = ws_path.name or "workspace"
        agy_pid = self._ensure_agy_project(ws_path, ws_uri, ws_name)
        codex_pid, codex_legacy_pid = self._ensure_codex_project(ws_path, ws_uri, ws_name)
        return WorkspaceProjectBinding(ws_path, ws_uri, ws_name, agy_pid, codex_pid, codex_legacy_pid)

    def resolve_readonly_binding(self, workspace: str | Path) -> WorkspaceProjectBinding:
        ws_path, ws_uri = parse_workspace_identity(workspace)
        ws_name = ws_path.name or "workspace"
        agy_pid = self._find_readonly_agy_project_id(ws_uri) or AgyProjectId(_deterministic_uuid("agy-project", str(ws_uri)))
        codex_pid_str = self._find_readonly_codex_project_id(ws_path) or _deterministic_uuid("codex-project", str(ws_uri))
        _, gs_data = self._load_codex_global_state()
        legacy_pid_str = self._find_legacy_codex_project_id(gs_data, ws_path) or _deterministic_uuid(
            "codex-legacy-project", str(ws_uri)
        )
        return WorkspaceProjectBinding(
            ws_path, ws_uri, ws_name, agy_pid, CodexProjectId(codex_pid_str), CodexLegacyProjectId(legacy_pid_str),
        )

    def ensure_agy_binding(self, workspace: str | Path) -> WorkspaceProjectBinding:
        ro = self.resolve_readonly_binding(workspace)
        return dataclasses.replace(ro, agy_project_id=self._ensure_agy_project(ro.workspace_path, ro.workspace_uri, ro.workspace_name))

    def ensure_codex_binding(self, workspace: str | Path) -> WorkspaceProjectBinding:
        ro = self.resolve_readonly_binding(workspace)
        codex_pid, codex_legacy_pid = self._ensure_codex_project(ro.workspace_path, ro.workspace_uri, ro.workspace_name)
        return dataclasses.replace(ro, codex_project_id=codex_pid, codex_legacy_project_id=codex_legacy_pid)

    def _discover_agy_subagent_sidecars(self) -> dict[str, dict[str, Any]]:
        sidecars: dict[str, dict[str, Any]] = {}
        brain_dir = self._paths.agy_data_dir / "brain"
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
            child_cid = _safe_valid_session_id(raw.get("conversationId") or sidecar_file.stem, "child_cascade_id")
            if not child_cid or (parent_raw and not _safe_valid_session_id(parent_raw, "parent_cascade_id")):
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

    @staticmethod
    def _uris_match_workspace(raw_uris: list[str], target_uri: WorkspaceRootUri) -> bool:
        for raw_u in raw_uris:
            try:
                if parse_workspace_identity(str(raw_u))[1] == target_uri:
                    return True
            except ValueError:
                continue
        return False

    def _scan_agy_summaries_db(
        self, binding: WorkspaceProjectBinding, sidecars: Mapping[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        agy_sessions: dict[str, dict[str, Any]] = {}
        sum_db = self._paths.agy_data_dir / "conversation_summaries.db"
        if not sum_db.is_file():
            return agy_sessions
        try:
            conn = _open_readonly_sqlite(sum_db)
            try:
                rows = conn.execute(
                    "SELECT conversation_id, title, step_count, workspace_uris, project_id, agent_name, "
                    "parent_conversation_id, nesting_depth FROM conversation_summaries ORDER BY last_modified_time DESC"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return agy_sessions

        for cid, title, scount, uris_raw, pid, agent_name, parent_cid, nesting_depth in rows:
            cid_str = _safe_valid_session_id(cid, "cascade_id")
            if not cid_str:
                continue
            sc_info = sidecars.get(cid_str, {})
            matches_ws = str(pid or "") == str(binding.agy_project_id)
            if not matches_ws:
                cand_uris = list(sc_info.get("workspace_uris", []))
                if uris_raw:
                    try:
                        parsed_u = json.loads(uris_raw) if str(uris_raw).startswith("[") else [str(uris_raw)]
                        cand_uris.extend(str(u) for u in parsed_u) if isinstance(parsed_u, list) else None
                    except Exception:
                        cand_uris.append(str(uris_raw))
                matches_ws = self._uris_match_workspace(cand_uris, binding.workspace_uri)
            if not matches_ws:
                continue
            eff_parent = _safe_valid_session_id(parent_cid or sc_info.get("parent_cascade_id"), "parent_cascade_id")
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

    def _enrich_agy_session_from_conv_db(
        self, db_file: Path, binding: WorkspaceProjectBinding,
        sidecars: Mapping[str, dict[str, Any]], agy_sessions: dict[str, dict[str, Any]],
    ) -> None:
        cid_str = _safe_valid_session_id(db_file.stem, "cascade_id")
        if not cid_str:
            return
        sc_info = sidecars.get(cid_str, {})
        try:
            conn = _open_readonly_sqlite(db_file)
            try:
                tblob = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
                meta = _parse_agy_trajectory_metadata_lineage(bytes(tblob[0])) if (tblob and tblob[0]) else _EMPTY_META_LINEAGE
                if cid_str in agy_sessions:
                    entry = agy_sessions[cid_str]
                    eff_parent = _safe_valid_session_id(
                        str(entry["parent_cascade_id"] or "") or meta["parent_conversation_id"] or sc_info.get("parent_cascade_id"),
                        "parent_cascade_id",
                    )
                    eff_root = _safe_valid_session_id(meta["root_conversation_id"] or eff_parent, "root_cascade_id")
                    entry["parent_cascade_id"] = CascadeId(eff_parent) if eff_parent else None
                    entry["root_cascade_id"] = CascadeId(eff_root) if eff_root else None
                    entry["nesting_depth"] = max(0, int(entry["nesting_depth"]), int(meta["nesting_depth"]), 1 if sc_info else 0)
                    entry["agent_type_name"] = (entry["agent_type_name"] or meta["agent_type_name"] or str(sc_info.get("agent_type_name") or "")).strip()
                    entry["agent_role"] = (entry["agent_role"] or meta["agent_role"] or str(sc_info.get("agent_role") or "")).strip()
                    return

                eff_parent = _safe_valid_session_id(meta["parent_conversation_id"] or sc_info.get("parent_cascade_id"), "parent_cascade_id")
                matches_ws = (
                    meta["project_id"] == str(binding.agy_project_id)
                    or self._uris_match_workspace(list(meta["workspace_uris"]) + list(sc_info.get("workspace_uris", [])), binding.workspace_uri)
                    or bool(eff_parent and eff_parent in agy_sessions)
                )
                if not matches_ws:
                    return
                scount_row = conn.execute("SELECT COUNT(*) FROM steps").fetchone()
                eff_root = _safe_valid_session_id(meta["root_conversation_id"] or eff_parent, "root_cascade_id")
                eff_role = (meta["agent_role"] or str(sc_info.get("agent_role") or "")).strip()
                agy_sessions[cid_str] = {
                    "cascade_id": CascadeId(cid_str), "title": eff_role or "Untitled Session",
                    "step_count": int(scount_row[0]) if scount_row else 0,
                    "parent_cascade_id": CascadeId(eff_parent) if eff_parent else None,
                    "root_cascade_id": CascadeId(eff_root) if eff_root else None,
                    "nesting_depth": max(0, int(meta["nesting_depth"]), 1 if sc_info else 0),
                    "agent_type_name": (meta["agent_type_name"] or str(sc_info.get("agent_type_name") or "")).strip(),
                    "agent_role": eff_role, "spawn_step_index": sc_info.get("spawn_step_index"),
                }
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return

    def _scan_codex_state_sessions(self, binding: WorkspaceProjectBinding) -> dict[str, dict[str, Any]]:
        codex_sessions: dict[str, dict[str, Any]] = {}
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        if not state_db.is_file():
            return codex_sessions
        try:
            conn = _open_readonly_sqlite(state_db)
            try:
                rows = conn.execute(
                    "SELECT id, title, preview, cwd, project_id, attached_thread_parent_id, rollout_path, "
                    "agent_nickname, agent_role, source FROM threads WHERE archived = 0 ORDER BY recency_at_ms DESC, updated_at DESC"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return codex_sessions

        for tid, title, preview, cwd, pid, parent_tid, rpath, agent_nickname, agent_role, raw_source in rows:
            tid_str = _safe_valid_session_id(tid, "codex_thread_id")
            if not tid_str:
                continue
            matches_ws = str(pid or "") == str(binding.codex_project_id)
            if not matches_ws and cwd:
                try:
                    matches_ws = parse_workspace_identity(str(cwd))[0] == binding.workspace_path
                except ValueError:
                    matches_ws = False
            if not matches_ws:
                continue
            src_is_sub, src_parent_tid, src_depth, src_type, src_nick = _parse_codex_thread_source_lineage(
                str(raw_source) if raw_source is not None else None
            )
            parent_tid_str = _safe_valid_session_id(str(parent_tid or "").strip() or src_parent_tid, "parent_codex_thread_id")
            eff_c_type = str(agent_role or "").strip() or src_type
            eff_c_role = str(agent_nickname or "").strip() or src_nick
            raw_src_lc = str(raw_source or "").strip().lower()
            c_depth = (
                max(1, src_depth) if src_is_sub
                else (0 if raw_src_lc in ("desktop", "vscode", "cli", "exec") else (1 if (parent_tid_str and (eff_c_type or eff_c_role)) else 0))
            )
            codex_sessions[tid_str] = {
                "codex_thread_id": CodexThreadId(tid_str),
                "title": str(title or preview or "").strip() or "Untitled Session",
                "turn_count": _count_codex_rollout_user_turns(rpath),
                "parent_codex_thread_id": CodexThreadId(parent_tid_str) if (parent_tid_str and c_depth > 0) else None,
                "nesting_depth": c_depth, "agent_type_name": eff_c_type, "agent_role": eff_c_role,
            }
        return codex_sessions

    def _load_sorted_workspace_cursor_pairs(self, workspace_uri: WorkspaceRootUri) -> list[tuple[str, str]]:
        cursors_dir = self._paths.sync_state_dir / "cursors"
        if not cursors_dir.is_dir():
            return []
        records: list[tuple[tuple[str, str], str, str]] = []
        for cf in cursors_dir.glob("*.json"):
            try:
                raw = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("workspace_uri") != str(workspace_uri):
                continue
            cid_str = _safe_valid_session_id(raw.get("cascade_id"), "cascade_id")
            tid_str = _safe_valid_session_id(raw.get("codex_thread_id"), "codex_thread_id")
            if cid_str and tid_str:
                records.append(((str(raw.get("updated_at_iso") or ""), cf.name), cid_str, tid_str))
        records.sort(key=lambda item: item[0], reverse=True)
        return [(cid_str, tid_str) for _, cid_str, tid_str in records]

    def list_workspace_sessions(
        self, binding: WorkspaceProjectBinding, *, include_subagents: bool = False,
    ) -> Tuple[PairedSessionDescriptor, ...]:
        sidecars = self._discover_agy_subagent_sidecars()
        agy_sessions = self._scan_agy_summaries_db(binding, sidecars)
        conv_dir = self._paths.agy_data_dir / "conversations"
        if conv_dir.is_dir():
            for db_file in sorted(conv_dir.glob("*.db")):
                self._enrich_agy_session_from_conv_db(db_file, binding, sidecars, agy_sessions)

        codex_sessions = self._scan_codex_state_sessions(binding)
        descriptors: list[PairedSessionDescriptor] = []
        paired_agy: set[str] = set()
        paired_codex: set[str] = set()

        for cid_str, tid_str in self._load_sorted_workspace_cursor_pairs(binding.workspace_uri):
            if cid_str in paired_agy or tid_str in paired_codex:
                continue
            paired_agy.add(cid_str)
            paired_codex.add(tid_str)
            agy_info, codex_info = agy_sessions.get(cid_str, {}), codex_sessions.get(tid_str, {})
            lineage = _build_combined_lineage(agy_info, codex_info)
            if include_subagents or not lineage.is_subagent:
                descriptors.append(PairedSessionDescriptor(
                    binding, CascadeId(cid_str), CodexThreadId(tid_str),
                    str(agy_info.get("title") or codex_info.get("title") or "Synced Session"),
                    int(agy_info.get("step_count", 0)), int(codex_info.get("turn_count", 0)),
                    lineage.parent_cascade_id, lineage.parent_codex_thread_id, lineage,
                ))

        for cid_str, ainfo in agy_sessions.items():
            if cid_str not in paired_agy:
                lineage = _build_combined_lineage(ainfo, {})
                if include_subagents or not lineage.is_subagent:
                    descriptors.append(PairedSessionDescriptor(
                        binding, ainfo["cascade_id"], None, ainfo["title"], ainfo["step_count"], 0,
                        parent_cascade_id=lineage.parent_cascade_id, lineage=lineage,
                    ))

        for tid_str, cinfo in codex_sessions.items():
            if tid_str not in paired_codex:
                lineage = _build_combined_lineage({}, cinfo)
                if include_subagents or not lineage.is_subagent:
                    descriptors.append(PairedSessionDescriptor(
                        binding, None, cinfo["codex_thread_id"], cinfo["title"], 0, cinfo["turn_count"],
                        parent_codex_thread_id=lineage.parent_codex_thread_id, lineage=lineage,
                    ))

        return tuple(descriptors)
