"""Project pairing registry, WAL/presence lock guard, cursor/fork state machine, and `SyncBridge` facade.

Invariants encoded in this module.
1. `CsrfSecret` wraps the loopback `x-codeium-csrf-token` read from
   `~/.gemini/antigravity/daemon/hub_<port>.json` with `__repr__`, `__str__`, and
   `__format__` redacted so it can never leak into logs, tracebacks, or serialized state.
2. `HostEnvironmentPaths` resolves all directories dynamically from environment
   variables or `Path.home()`, never hardcoding a machine username.
3. `PresenceWalGuard` enforces `SQLITE_OPEN_READONLY` (`?mode=ro` + `PRAGMA query_only=ON`)
   on all SQLite reads, blocks offline writes whenever `presence/<cascade_id>.lock`
   is held (`flock(LOCK_EX | LOCK_NB)` raises `EWOULDBLOCK`), a live daemon holds an
   active `-wal` file, or a turn is in-flight, and produces a self-describing
   `BackupManifest` (grafted from Candidate 1) before any offline mutation.
4. `CursorForkStateMachine` is a pure state machine that compares source and target
   `CompletedTurnWatermark` heads against `SessionSyncCursor`, advancing only on
   completed turns and pre-planning fork/overwrite targets (`DivergedConflictTransition`,
   grafted from Candidate 1) when both hosts advanced independently (per
   `principle-model-the-domain` and `principle-make-operations-idempotent`).
5. `SyncBridge` is a deep facade hiding multi-store project registration, dual-mode
   RPC/SQLite selection, companion file projection, and atomic cursor persistence
   behind four high-level entrypoints.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import sqlite3
import stat
import time
from typing import Any, Literal, Mapping, Protocol, Sequence, Tuple, TypeAlias
import uuid

from ._wire import encode_string_field
from .codecs import (
    AgyRawStepRow,
    CodexRawRolloutLine,
    TrajectoryWireCodec,
    decode_cascade_summary_proto,
    decode_summaries_state_proto,
    decode_trajectory_metadata_proto,
    encode_trajectory_metadata_proto,
    parse_annotation_pbtxt_title,
)
from .ledger import (
    AgyGuiSafeSource,
    AgyProjectId,
    CanonicalEvent,
    CascadeId,
    CodexLegacyProjectId,
    CodexProjectId,
    CodexThreadId,
    CompletedTurnWatermark,
    ContentDigest,
    HostKind,
    RolloutLineNo,
    StepIndex,
    SubagentLineage,
    TrajectoryId,
    TrajectoryLedger,
    TurnId,
    WorkspacePath,
    WorkspaceRootUri,
    parse_workspace_identity,
    validate_session_id,
)

# ---------------------------------------------------------------------------
# Environment & Secret Boundary Types (per principle-boundary-discipline)
# ---------------------------------------------------------------------------


class CsrfSecret:
    """In-memory-only wrapper for `x-codeium-csrf-token` from `hub_<port>.json`.

    Invariant. `__repr__`, `__str__`, and `__format__` always return `"[REDACTED_CSRF]"`
    so the secret never appears in logs, exception messages, or snapshots.
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        cleaned = str(token or "").strip()
        if not cleaned:
            raise ValueError("CSRF token cannot be empty.")
        object.__setattr__(self, "_token", cleaned)

    def as_loopback_header(self) -> Mapping[str, str]:
        """Return `{"x-codeium-csrf-token": self._token}` for 127.0.0.1 loopback RPCs only."""
        return {"x-codeium-csrf-token": self._token}

    def __repr__(self) -> str:
        return "CsrfSecret('[REDACTED_CSRF]')"

    def __str__(self) -> str:
        return "[REDACTED_CSRF]"

    def __format__(self, format_spec: str) -> str:
        return "[REDACTED_CSRF]"


@dataclass(frozen=True)
class HostEnvironmentPaths:
    """Runtime-resolved host storage roots with zero hardcoded machine usernames."""

    gemini_config_dir: Path
    agy_data_dir: Path
    codex_home_dir: Path
    sync_state_dir: Path

    @classmethod
    def discover(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> HostEnvironmentPaths:
        """Resolve paths from `ANTIGRAVITY_DATA_DIR`, `GEMINI_CONFIG_DIR`, `CODEX_HOME`, or `Path.home()`."""
        source_env = env if env is not None else os.environ
        home = Path(source_env.get("HOME") or Path.home()).expanduser().resolve(strict=False)
        gemini_root = Path(source_env.get("GEMINI_HOME") or (home / ".gemini")).expanduser().resolve(strict=False)

        gemini_config_dir = Path(
            source_env.get("GEMINI_CONFIG_DIR") or (gemini_root / "config")
        ).expanduser().resolve(strict=False)
        agy_data_dir = Path(
            source_env.get("ANTIGRAVITY_DATA_DIR") or (gemini_root / "antigravity")
        ).expanduser().resolve(strict=False)
        codex_home_dir = Path(
            source_env.get("CODEX_HOME") or (home / ".codex")
        ).expanduser().resolve(strict=False)
        sync_state_dir = Path(
            source_env.get("CODEX_AGY_SYNC_STATE_DIR") or (gemini_root / "codex-agy-sync")
        ).expanduser().resolve(strict=False)

        return cls(
            gemini_config_dir=gemini_config_dir,
            agy_data_dir=agy_data_dir,
            codex_home_dir=codex_home_dir,
            sync_state_dir=sync_state_dir,
        )


def _atomic_write_text(target_path: Path, content: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, target_path)


def _atomic_write_bytes(target_path: Path, content: bytes) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    tmp_path.write_bytes(content)
    os.replace(tmp_path, target_path)


def _deterministic_uuid(namespace_label: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-agy-sync:{namespace_label}:{key}"))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validated_child_path(base_dir: Path, *parts: str, label: str = "path") -> Path:
    resolved_base = base_dir.resolve(strict=False)
    raw_candidate = base_dir.joinpath(*parts)
    resolved_candidate = raw_candidate.resolve(strict=False)
    if resolved_candidate == resolved_base or not resolved_candidate.is_relative_to(resolved_base):
        raise ValueError(f"Invalid {label}: {parts!r} escapes {base_dir}")
    return raw_candidate


def _open_readonly_sqlite(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve(strict=False).as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only = ON;")
    return conn


def _ensure_agy_conversation_db_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS `trajectory_meta` (
            `trajectory_id` text,
            `cascade_id` text,
            `trajectory_type` integer,
            `source` integer,
            PRIMARY KEY (`trajectory_id`)
        );
        CREATE TABLE IF NOT EXISTS `steps` (
            `idx` integer,
            `step_type` integer NOT NULL DEFAULT 0,
            `status` integer NOT NULL DEFAULT 0,
            `has_subtrajectory` numeric NOT NULL DEFAULT false,
            `metadata` blob,
            `error_details` blob,
            `permissions` blob,
            `task_details` blob,
            `render_info` blob,
            `step_payload` blob,
            `step_format` integer NOT NULL DEFAULT 0,
            PRIMARY KEY (`idx`)
        );
        CREATE INDEX IF NOT EXISTS `idx_steps_status` ON `steps`(`status`);
        CREATE INDEX IF NOT EXISTS `idx_steps_step_type` ON `steps`(`step_type`);
        CREATE TABLE IF NOT EXISTS `gen_metadata` (
            `idx` integer,
            `data` blob,
            `size` integer NOT NULL DEFAULT 0,
            PRIMARY KEY (`idx`)
        );
        CREATE TABLE IF NOT EXISTS `executor_metadata` (
            `idx` integer,
            `data` blob,
            PRIMARY KEY (`idx`)
        );
        CREATE TABLE IF NOT EXISTS `parent_references` (
            `idx` integer,
            `data` blob,
            PRIMARY KEY (`idx`)
        );
        CREATE TABLE IF NOT EXISTS `trajectory_metadata_blob` (
            `id` text DEFAULT "main",
            `data` blob,
            PRIMARY KEY (`id`)
        );
        CREATE TABLE IF NOT EXISTS `battle_mode_infos` (
            `idx` integer,
            `data` blob,
            PRIMARY KEY (`idx`)
        );
        """
    )
    cur_uv = int((conn.execute("PRAGMA user_version").fetchone() or (0,))[0] or 0)
    if cur_uv == 0:
        conn.execute("PRAGMA user_version = 1")


def _ensure_agy_summaries_db_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS `conversation_summaries` (
            `conversation_id` text,
            `title` text NOT NULL DEFAULT "",
            `preview` text NOT NULL DEFAULT "",
            `step_count` integer NOT NULL DEFAULT 0,
            `last_modified_time` datetime NOT NULL,
            `workspace_uris` text NOT NULL,
            `status` text NOT NULL DEFAULT "",
            `source` text NOT NULL DEFAULT "",
            `project_id` text NOT NULL DEFAULT "",
            `agent_name` text NOT NULL DEFAULT "",
            `parent_conversation_id` text NOT NULL DEFAULT "",
            `nesting_depth` integer NOT NULL DEFAULT 0,
            `battle_id` text NOT NULL DEFAULT "",
            `winning_conversation_id` text NOT NULL DEFAULT "",
            `not_fully_idle` numeric NOT NULL DEFAULT false,
            `killed` numeric NOT NULL DEFAULT false,
            `last_user_input_time` datetime NOT NULL,
            `last_user_input_step_index` integer NOT NULL DEFAULT -1,
            `app_data_dir` text NOT NULL DEFAULT "",
            `raw_summary` blob,
            `group_id` text NOT NULL DEFAULT "",
            PRIMARY KEY (`conversation_id`)
        );
        CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_user_input_time`
            ON `conversation_summaries`(`last_user_input_time`);
        CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_modified_time`
            ON `conversation_summaries`(`last_modified_time`);
        """
    )


def _ensure_codex_state_db_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_roots (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            path TEXT NOT NULL,
            PRIMARY KEY (project_id, position)
        );
        CREATE TABLE IF NOT EXISTS project_idempotency_keys (
            key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            source TEXT NOT NULL,
            model_provider TEXT NOT NULL,
            cwd TEXT NOT NULL,
            title TEXT NOT NULL,
            sandbox_policy TEXT NOT NULL,
            approval_mode TEXT NOT NULL,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            has_user_event INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            archived_at INTEGER,
            git_sha TEXT,
            git_branch TEXT,
            git_origin_url TEXT,
            cli_version TEXT NOT NULL DEFAULT '',
            first_user_message TEXT NOT NULL DEFAULT '',
            agent_nickname TEXT,
            agent_role TEXT,
            memory_mode TEXT NOT NULL DEFAULT 'enabled',
            model TEXT,
            reasoning_effort TEXT,
            agent_path TEXT,
            created_at_ms INTEGER,
            updated_at_ms INTEGER,
            thread_source TEXT,
            preview TEXT NOT NULL DEFAULT '',
            recency_at INTEGER NOT NULL DEFAULT 0,
            recency_at_ms INTEGER NOT NULL DEFAULT 0,
            history_mode TEXT NOT NULL DEFAULT 'legacy',
            name TEXT,
            is_pinned INTEGER NOT NULL DEFAULT 0,
            thread_section_id TEXT,
            section_position INTEGER,
            section_entered_at_ms INTEGER,
            project_id TEXT,
            originator TEXT,
            daybreak_enabled BOOLEAN,
            creator_user_id TEXT,
            creator_account_id TEXT,
            attached_thread_parent_id TEXT
        );
        """
    )


# ---------------------------------------------------------------------------
# Bidirectional Project Pairing Registry & Sidebar Invariants
# ---------------------------------------------------------------------------

FORBIDDEN_AGY_PROJECT_IDS: frozenset[str] = frozenset(
    {"", "default-cli-project", "outside-of-project", "null", "none"}
)


@dataclass(frozen=True)
class WorkspaceProjectBinding:
    """Verified bidirectional project identity for a single workspace folder.

    Invariants.
    - `agy_project_id` is never in `FORBIDDEN_AGY_PROJECT_IDS` and corresponds to
      `~/.gemini/config/projects/<agy_project_id>.json` with `folderUri == workspace_uri`.
    - `codex_project_id` exists in `state_5.sqlite` (`projects` + `project_roots`
      with `path == str(workspace_path)`) and is linked in `.codex-global-state.json`
      via `codex_legacy_project_id`.
    """

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
    """Decode subagent lineage fields (5, 6, 7, 8, 17, 18) from `CortexTrajectoryMetadata`."""
    return decode_trajectory_metadata_proto(raw_blob)


class ProjectRegistry:
    """Owns bidirectional workspace-to-project resolution and sidebar-visibility repair.

    Hides the 4 Antigravity stores (`projects/<id>.json`, `trajectory_metadata_blob`,
    `conversation_summaries.db`, `agyhub_summaries_proto.pb` + `annotations/<id>.pbtxt`)
    and the 2 Codex stores (`state_5.sqlite` + `.codex-global-state.json`).
    """

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

    def _ensure_agy_project(
        self,
        ws_path: WorkspacePath,
        ws_uri: WorkspaceRootUri,
        ws_name: str,
    ) -> AgyProjectId:
        projects_dir = self._paths.gemini_config_dir / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)

        for pf in sorted(projects_dir.glob("*.json")):
            try:
                raw = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            pid = str(raw.get("id") or pf.stem).strip()
            if pid in FORBIDDEN_AGY_PROJECT_IDS:
                continue
            for raw_uri in self._extract_agy_project_folder_uris(raw):
                try:
                    _, norm_uri = parse_workspace_identity(raw_uri)
                except ValueError:
                    continue
                if norm_uri == ws_uri:
                    return AgyProjectId(pid)

        new_pid = _deterministic_uuid("agy-project", str(ws_uri))
        project_doc = {
            "id": new_pid,
            "name": ws_name,
            "projectResources": {
                "resources": [
                    {
                        "folderUri": str(ws_uri),
                    }
                ]
            },
            "permissionGrants": {
                "v2Migrated": True,
            },
            "settings": {},
            "isWorkspaceOnly": False,
        }
        _atomic_write_text(
            projects_dir / f"{new_pid}.json",
            json.dumps(project_doc, ensure_ascii=False, indent=2) + "\n",
        )
        return AgyProjectId(new_pid)

    def _ensure_codex_project(
        self,
        ws_path: WorkspacePath,
        ws_uri: WorkspaceRootUri,
        ws_name: str,
    ) -> tuple[CodexProjectId, CodexLegacyProjectId]:
        self._paths.codex_home_dir.mkdir(parents=True, exist_ok=True)
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        global_state_path = self._paths.codex_home_dir / ".codex-global-state.json"

        now_ms = int(time.time() * 1000)
        ws_str = str(ws_path)
        idemp_key = f"codex-agy-sync:project:{ws_uri}"

        existing_pid: str | None = None
        if state_db.exists():
            try:
                ro_conn = _open_readonly_sqlite(state_db)
                try:
                    rows = ro_conn.execute("SELECT project_id, path FROM project_roots").fetchall()
                    for pid_val, raw_root in rows:
                        try:
                            norm_p, _ = parse_workspace_identity(str(raw_root))
                        except ValueError:
                            continue
                        if norm_p == ws_path and str(pid_val).strip():
                            existing_pid = str(pid_val).strip()
                            break
                finally:
                    ro_conn.close()
            except sqlite3.OperationalError:
                existing_pid = None

        gs_data: dict[str, Any] = {}
        if global_state_path.is_file():
            try:
                loaded = json.loads(global_state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    gs_data = loaded
            except Exception:
                gs_data = {}

        local_projects = gs_data.get("local-projects")
        if not isinstance(local_projects, dict):
            local_projects = {}
            gs_data["local-projects"] = local_projects

        legacy_pid: str | None = None
        for lpid, lentry in local_projects.items():
            if not isinstance(lentry, dict):
                continue
            roots = lentry.get("rootPaths") or []
            if isinstance(roots, list):
                for rp in roots:
                    try:
                        norm_p, _ = parse_workspace_identity(str(rp))
                    except ValueError:
                        continue
                    if norm_p == ws_path:
                        legacy_pid = str(lpid)
                        break
            if legacy_pid:
                break

        if not legacy_pid:
            legacy_pid = _deterministic_uuid("codex-legacy-project", str(ws_uri))

        if not existing_pid:
            existing_pid = _deterministic_uuid("codex-project", str(ws_uri))
            conn = sqlite3.connect(str(state_db), timeout=10.0)
            try:
                _ensure_codex_state_db_schema(conn)
                max_pos_row = conn.execute("SELECT COALESCE(MAX(position), -1) FROM projects").fetchone()
                next_pos = int(max_pos_row[0]) + 1 if max_pos_row else 0
                conn.execute(
                    """
                    INSERT OR IGNORE INTO projects (id, name, metadata, position, created_at_ms, updated_at_ms)
                    VALUES (?, ?, '{}', ?, ?, ?)
                    """,
                    (existing_pid, ws_name, next_pos, now_ms, now_ms),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO project_roots (project_id, position, path)
                    VALUES (?, 0, ?)
                    """,
                    (existing_pid, ws_str),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO project_idempotency_keys (key, project_id, created_at_ms)
                    VALUES (?, ?, ?)
                    """,
                    (idemp_key, existing_pid, now_ms),
                )
                conn.commit()
            finally:
                conn.close()

        local_projects[legacy_pid] = {
            "id": legacy_pid,
            "name": ws_name,
            "rootPaths": [ws_str],
            "createdAt": local_projects.get(legacy_pid, {}).get("createdAt", now_ms),
            "updatedAt": now_ms,
        }
        host_map = gs_data.get("app-server-project-id-by-legacy-project-id-by-host")
        if not isinstance(host_map, dict):
            host_map = {}
            gs_data["app-server-project-id-by-legacy-project-id-by-host"] = host_map
        host_key = f"local:{self._paths.codex_home_dir}"
        sub_map = host_map.get(host_key)
        if not isinstance(sub_map, dict):
            sub_map = {}
            host_map[host_key] = sub_map
        sub_map[legacy_pid] = existing_pid

        project_order = gs_data.get("project-order")
        if not isinstance(project_order, list):
            project_order = []
            gs_data["project-order"] = project_order
        if legacy_pid not in project_order:
            project_order.append(legacy_pid)

        _atomic_write_text(
            global_state_path,
            json.dumps(gs_data, ensure_ascii=False, indent=2) + "\n",
        )
        return CodexProjectId(existing_pid), CodexLegacyProjectId(legacy_pid)

    def ensure_workspace_binding(
        self,
        workspace: str | Path,
        prefer_online_rpc: bool = True,
    ) -> WorkspaceProjectBinding:
        """Resolve or idempotently register both hosts' project records for `workspace`."""
        _ = prefer_online_rpc
        ws_path, ws_uri = parse_workspace_identity(workspace)
        ws_name = ws_path.name or "workspace"
        agy_pid = self._ensure_agy_project(ws_path, ws_uri, ws_name)
        codex_pid, codex_legacy_pid = self._ensure_codex_project(ws_path, ws_uri, ws_name)
        return WorkspaceProjectBinding(
            workspace_path=ws_path,
            workspace_uri=ws_uri,
            workspace_name=ws_name,
            agy_project_id=agy_pid,
            codex_project_id=codex_pid,
            codex_legacy_project_id=codex_legacy_pid,
        )

    def resolve_readonly_binding(
        self,
        workspace: str | Path,
    ) -> WorkspaceProjectBinding:
        """Resolve workspace project IDs in memory without creating or mutating any files on disk."""
        ws_path, ws_uri = parse_workspace_identity(workspace)
        ws_name = ws_path.name or "workspace"
        agy_pid = AgyProjectId(_deterministic_uuid("agy-project", str(ws_uri)))
        projects_dir = self._paths.gemini_config_dir / "projects"
        if projects_dir.is_dir():
            matched_agy = False
            for pfile in sorted(projects_dir.glob("*.json")):
                try:
                    data = json.loads(pfile.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for folder_uri in self._extract_agy_project_folder_uris(data):
                    try:
                        _, norm_uri = parse_workspace_identity(folder_uri)
                    except ValueError:
                        continue
                    if norm_uri == ws_uri:
                        cand_id = str(data.get("id") or pfile.stem).strip()
                        if cand_id and cand_id not in FORBIDDEN_AGY_PROJECT_IDS:
                            agy_pid = AgyProjectId(cand_id)
                            matched_agy = True
                            break
                if matched_agy:
                    break

        codex_pid = CodexProjectId(_deterministic_uuid("codex-project", str(ws_uri)))
        codex_legacy_pid = CodexLegacyProjectId(_deterministic_uuid("codex-legacy-project", str(ws_uri)))
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        if state_db.is_file():
            try:
                conn = _open_readonly_sqlite(state_db)
                try:
                    rows = conn.execute("SELECT project_id, path FROM project_roots").fetchall()
                    for pid, rpath in rows:
                        try:
                            norm_p, _ = parse_workspace_identity(str(rpath))
                            if norm_p == ws_path and str(pid or "").strip():
                                codex_pid = CodexProjectId(str(pid).strip())
                                break
                        except ValueError:
                            continue
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                pass

        global_state_path = self._paths.codex_home_dir / ".codex-global-state.json"
        if global_state_path.is_file():
            try:
                gs_data = json.loads(global_state_path.read_text(encoding="utf-8"))
                if isinstance(gs_data, dict):
                    local_projects = gs_data.get("local-projects")
                    if isinstance(local_projects, dict):
                        for lpid, lentry in local_projects.items():
                            if not isinstance(lentry, dict):
                                continue
                            roots = lentry.get("rootPaths") or []
                            if isinstance(roots, list):
                                matched_legacy = False
                                for rp in roots:
                                    try:
                                        norm_p, _ = parse_workspace_identity(str(rp))
                                    except ValueError:
                                        continue
                                    if norm_p == ws_path and str(lpid).strip():
                                        codex_legacy_pid = CodexLegacyProjectId(str(lpid).strip())
                                        matched_legacy = True
                                        break
                                if matched_legacy:
                                    break
            except Exception:
                pass

        return WorkspaceProjectBinding(
            workspace_path=ws_path,
            workspace_uri=ws_uri,
            workspace_name=ws_name,
            agy_project_id=agy_pid,
            codex_project_id=codex_pid,
            codex_legacy_project_id=codex_legacy_pid,
        )

    def ensure_agy_binding(
        self,
        workspace: str | Path,
    ) -> WorkspaceProjectBinding:
        """Ensure Antigravity project registration while resolving Codex IDs read-only."""
        ro = self.resolve_readonly_binding(workspace)
        agy_pid = self._ensure_agy_project(ro.workspace_path, ro.workspace_uri, ro.workspace_name)
        return dataclasses.replace(ro, agy_project_id=agy_pid)

    def ensure_codex_binding(
        self,
        workspace: str | Path,
    ) -> WorkspaceProjectBinding:
        """Ensure Codex project registration while resolving Antigravity ID read-only."""
        ro = self.resolve_readonly_binding(workspace)
        codex_pid, codex_legacy_pid = self._ensure_codex_project(
            ro.workspace_path, ro.workspace_uri, ro.workspace_name
        )
        return dataclasses.replace(
            ro,
            codex_project_id=codex_pid,
            codex_legacy_project_id=codex_legacy_pid,
        )

    def _discover_agy_subagent_sidecars(self) -> dict[str, dict[str, Any]]:
        """Scan `brain/<parent_id>/.system_generated/subagents/<child_id>.json` sidecars."""
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
            parent_cid = sidecar_file.parts[-4] if len(sidecar_file.parts) >= 4 else ""
            child_cid = str(raw.get("conversationId") or sidecar_file.stem).strip()
            if not child_cid:
                continue
            try:
                validate_session_id(child_cid, label="child_cascade_id")
                if parent_cid:
                    validate_session_id(parent_cid, label="parent_cascade_id")
            except ValueError:
                continue
            desc = raw.get("subagentDescriptor")
            type_name = ""
            role = ""
            if isinstance(desc, dict):
                type_name = str(desc.get("typeName") or "").strip()
                role = str(desc.get("role") or "").strip()
            spawn_idx_raw = raw.get("spawnStepIndex")
            spawn_idx = StepIndex(int(spawn_idx_raw)) if isinstance(spawn_idx_raw, int) and spawn_idx_raw >= 0 else None
            ws_uris = raw.get("workspaceUris")
            uri_list = [str(u) for u in ws_uris] if isinstance(ws_uris, list) else []
            sidecars[child_cid] = {
                "parent_cascade_id": parent_cid,
                "child_cascade_id": child_cid,
                "agent_type_name": type_name,
                "agent_role": role,
                "spawn_step_index": spawn_idx,
                "workspace_uris": uri_list,
            }
        return sidecars

    def list_workspace_sessions(
        self,
        binding: WorkspaceProjectBinding,
        *,
        include_subagents: bool = False,
    ) -> Tuple[PairedSessionDescriptor, ...]:
        """List sessions belonging to `binding` across both hosts (excluding subagents by default)."""
        sidecars = self._discover_agy_subagent_sidecars()
        agy_sessions: dict[str, dict[str, Any]] = {}
        sum_db = self._paths.agy_data_dir / "conversation_summaries.db"
        if sum_db.is_file():
            try:
                conn = _open_readonly_sqlite(sum_db)
                try:
                    rows = conn.execute(
                        """
                        SELECT conversation_id, title, step_count, workspace_uris, project_id,
                               agent_name, parent_conversation_id, nesting_depth
                        FROM conversation_summaries
                        ORDER BY last_modified_time DESC
                        """
                    ).fetchall()
                    for cid, title, scount, uris_raw, pid, agent_name, parent_cid, nesting_depth in rows:
                        cid_str = str(cid or "").strip()
                        if not cid_str:
                            continue
                        try:
                            validate_session_id(cid_str, label="cascade_id")
                        except ValueError:
                            continue
                        sc_info = sidecars.get(cid_str, {})
                        matches_ws = str(pid or "") == str(binding.agy_project_id)
                        if not matches_ws:
                            candidate_uris: list[str] = list(sc_info.get("workspace_uris", []))
                            if uris_raw:
                                try:
                                    parsed_u = json.loads(uris_raw) if str(uris_raw).startswith("[") else [str(uris_raw)]
                                    if isinstance(parsed_u, list):
                                        candidate_uris.extend(str(u) for u in parsed_u)
                                except Exception:
                                    candidate_uris.append(str(uris_raw))
                            for u in candidate_uris:
                                try:
                                    _, norm_u = parse_workspace_identity(str(u))
                                    if norm_u == binding.workspace_uri:
                                        matches_ws = True
                                        break
                                except ValueError:
                                    continue
                        if matches_ws:
                            eff_parent = str(parent_cid or sc_info.get("parent_cascade_id") or "").strip()
                            if eff_parent:
                                try:
                                    validate_session_id(eff_parent, label="parent_cascade_id")
                                except ValueError:
                                    eff_parent = ""
                            eff_depth = max(0, int(nesting_depth or 0), 1 if sc_info else 0)
                            eff_role = str(sc_info.get("agent_role") or agent_name or "").strip()
                            eff_type = str(sc_info.get("agent_type_name") or "").strip()
                            agy_sessions[cid_str] = {
                                "cascade_id": CascadeId(cid_str),
                                "title": str(title or "").strip() or "Untitled Session",
                                "step_count": int(scount or 0),
                                "parent_cascade_id": CascadeId(eff_parent) if eff_parent else None,
                                "root_cascade_id": None,
                                "nesting_depth": eff_depth,
                                "agent_type_name": eff_type,
                                "agent_role": eff_role,
                                "spawn_step_index": sc_info.get("spawn_step_index"),
                            }
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                pass

        conv_dir = self._paths.agy_data_dir / "conversations"
        if conv_dir.is_dir():
            for db_file in sorted(conv_dir.glob("*.db")):
                cid_str = db_file.stem
                try:
                    validate_session_id(cid_str, label="cascade_id")
                except ValueError:
                    continue
                sc_info = sidecars.get(cid_str, {})
                try:
                    conn = _open_readonly_sqlite(db_file)
                    try:
                        tblob = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
                        meta_info = (
                            _parse_agy_trajectory_metadata_lineage(bytes(tblob[0]))
                            if (tblob and tblob[0])
                            else {
                                "parent_conversation_id": "",
                                "root_conversation_id": "",
                                "workspace_uris": [],
                                "agent_type_name": "",
                                "agent_role": "",
                                "nesting_depth": 0,
                                "project_id": "",
                            }
                        )
                        if cid_str in agy_sessions:
                            entry = agy_sessions[cid_str]
                            eff_parent_str = (
                                str(entry["parent_cascade_id"] or "")
                                or meta_info["parent_conversation_id"]
                                or str(sc_info.get("parent_cascade_id") or "")
                            ).strip()
                            if eff_parent_str:
                                try:
                                    validate_session_id(eff_parent_str, label="parent_cascade_id")
                                except ValueError:
                                    eff_parent_str = ""
                            eff_root_str = (meta_info["root_conversation_id"] or eff_parent_str).strip()
                            if eff_root_str:
                                try:
                                    validate_session_id(eff_root_str, label="root_cascade_id")
                                except ValueError:
                                    eff_root_str = ""
                            entry["parent_cascade_id"] = CascadeId(eff_parent_str) if eff_parent_str else None
                            entry["root_cascade_id"] = CascadeId(eff_root_str) if eff_root_str else None
                            entry["nesting_depth"] = max(
                                0,
                                int(entry["nesting_depth"]),
                                int(meta_info["nesting_depth"]),
                                1 if sc_info else 0,
                            )
                            entry["agent_type_name"] = (
                                entry["agent_type_name"]
                                or meta_info["agent_type_name"]
                                or str(sc_info.get("agent_type_name") or "")
                            ).strip()
                            entry["agent_role"] = (
                                entry["agent_role"]
                                or meta_info["agent_role"]
                                or str(sc_info.get("agent_role") or "")
                            ).strip()
                            continue

                        matches_ws = meta_info["project_id"] == str(binding.agy_project_id)
                        if not matches_ws:
                            for raw_u in list(meta_info["workspace_uris"]) + list(sc_info.get("workspace_uris", [])):
                                try:
                                    _, norm_u = parse_workspace_identity(raw_u)
                                    if norm_u == binding.workspace_uri:
                                        matches_ws = True
                                        break
                                except ValueError:
                                    pass
                        eff_parent_str = (
                            meta_info["parent_conversation_id"]
                            or str(sc_info.get("parent_cascade_id") or "")
                        ).strip()
                        if eff_parent_str:
                            try:
                                validate_session_id(eff_parent_str, label="parent_cascade_id")
                            except ValueError:
                                eff_parent_str = ""
                        if not matches_ws and eff_parent_str and eff_parent_str in agy_sessions:
                            matches_ws = True
                        if matches_ws:
                            scount_row = conn.execute("SELECT COUNT(*) FROM steps").fetchone()
                            eff_root_str = (meta_info["root_conversation_id"] or eff_parent_str).strip()
                            if eff_root_str:
                                try:
                                    validate_session_id(eff_root_str, label="root_cascade_id")
                                except ValueError:
                                    eff_root_str = ""
                            eff_depth = max(
                                0,
                                int(meta_info["nesting_depth"]),
                                1 if sc_info else 0,
                            )
                            eff_type = (
                                meta_info["agent_type_name"]
                                or str(sc_info.get("agent_type_name") or "")
                            ).strip()
                            eff_role = (
                                meta_info["agent_role"]
                                or str(sc_info.get("agent_role") or "")
                            ).strip()
                            agy_sessions[cid_str] = {
                                "cascade_id": CascadeId(cid_str),
                                "title": eff_role or "Untitled Session",
                                "step_count": int(scount_row[0]) if scount_row else 0,
                                "parent_cascade_id": CascadeId(eff_parent_str) if eff_parent_str else None,
                                "root_cascade_id": CascadeId(eff_root_str) if eff_root_str else None,
                                "nesting_depth": eff_depth,
                                "agent_type_name": eff_type,
                                "agent_role": eff_role,
                                "spawn_step_index": sc_info.get("spawn_step_index"),
                            }
                    finally:
                        conn.close()
                except sqlite3.OperationalError:
                    continue

        codex_sessions: dict[str, dict[str, Any]] = {}
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        if state_db.is_file():
            try:
                conn = _open_readonly_sqlite(state_db)
                try:
                    rows = conn.execute(
                        """
                        SELECT id, title, preview, cwd, project_id, attached_thread_parent_id,
                               rollout_path, agent_nickname, agent_role
                        FROM threads
                        WHERE archived = 0
                        ORDER BY recency_at_ms DESC, updated_at DESC
                        """
                    ).fetchall()
                    for tid, title, preview, cwd, pid, parent_tid, rpath, agent_nickname, agent_role in rows:
                        tid_str = str(tid or "").strip()
                        if not tid_str:
                            continue
                        try:
                            validate_session_id(tid_str, label="codex_thread_id")
                        except ValueError:
                            continue
                        matches_ws = str(pid or "") == str(binding.codex_project_id)
                        if not matches_ws and cwd:
                            try:
                                norm_p, _ = parse_workspace_identity(str(cwd))
                                if norm_p == binding.workspace_path:
                                    matches_ws = True
                            except ValueError:
                                pass
                        if matches_ws:
                            turn_count = 1
                            if rpath and Path(str(rpath)).is_file():
                                try:
                                    turn_count = max(
                                        1,
                                        sum(
                                            1
                                            for ln in Path(str(rpath)).read_text(encoding="utf-8", errors="replace").splitlines()
                                            if '"role":"user"' in ln or '"role": "user"' in ln
                                        ),
                                    )
                                except Exception:
                                    turn_count = 1
                            parent_tid_str = str(parent_tid or "").strip()
                            if parent_tid_str:
                                try:
                                    validate_session_id(parent_tid_str, label="parent_codex_thread_id")
                                except ValueError:
                                    parent_tid_str = ""
                            codex_sessions[tid_str] = {
                                "codex_thread_id": CodexThreadId(tid_str),
                                "title": str(title or preview or "").strip() or "Untitled Session",
                                "turn_count": turn_count,
                                "parent_codex_thread_id": CodexThreadId(parent_tid_str) if parent_tid_str else None,
                                "agent_type_name": str(agent_role or "").strip(),
                                "agent_role": str(agent_nickname or "").strip(),
                            }
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                pass

        descriptors: list[PairedSessionDescriptor] = []
        paired_agy: set[str] = set()
        paired_codex: set[str] = set()

        def _make_lineage(
            agy_info: Mapping[str, Any],
            codex_info: Mapping[str, Any],
        ) -> SubagentLineage:
            p_cid: CascadeId | None = agy_info.get("parent_cascade_id")
            r_cid: CascadeId | None = agy_info.get("root_cascade_id") or p_cid
            p_tid: CodexThreadId | None = codex_info.get("parent_codex_thread_id")
            eff_type = str(agy_info.get("agent_type_name") or codex_info.get("agent_type_name") or "").strip()
            eff_role = str(agy_info.get("agent_role") or codex_info.get("agent_role") or "").strip()
            spawn_idx: StepIndex | None = agy_info.get("spawn_step_index")
            depth = max(0, int(agy_info.get("nesting_depth") or 0))
            if depth == 0 and (bool(eff_type) or bool(eff_role) or spawn_idx is not None):
                depth = 1
            return SubagentLineage(
                parent_cascade_id=p_cid,
                root_cascade_id=r_cid,
                parent_codex_thread_id=p_tid,
                nesting_depth=depth,
                agent_type_name=eff_type,
                agent_role=eff_role,
                spawn_step_index=spawn_idx,
            )

        cursors_dir = self._paths.sync_state_dir / "cursors"
        if cursors_dir.is_dir():
            cursor_records: list[tuple[tuple[str, str], str, str]] = []
            for cf in cursors_dir.glob("*.json"):
                try:
                    raw = json.loads(cf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(raw, dict) or raw.get("workspace_uri") != str(binding.workspace_uri):
                    continue
                cid_str = str(raw.get("cascade_id") or "")
                tid_str = str(raw.get("codex_thread_id") or "")
                if not cid_str or not tid_str:
                    continue
                try:
                    validate_session_id(cid_str, label="cascade_id")
                    validate_session_id(tid_str, label="codex_thread_id")
                except ValueError:
                    continue
                cursor_records.append(((str(raw.get("updated_at_iso") or ""), cf.name), cid_str, tid_str))
            cursor_records.sort(key=lambda item: item[0], reverse=True)
            for _, cid_str, tid_str in cursor_records:
                if cid_str in paired_agy or tid_str in paired_codex:
                    continue
                paired_agy.add(cid_str)
                paired_codex.add(tid_str)
                agy_info = agy_sessions.get(cid_str, {})
                codex_info = codex_sessions.get(tid_str, {})
                lineage = _make_lineage(agy_info, codex_info)
                if not include_subagents and lineage.is_subagent:
                    continue
                descriptors.append(
                    PairedSessionDescriptor(
                        workspace=binding,
                        cascade_id=CascadeId(cid_str),
                        codex_thread_id=CodexThreadId(tid_str),
                        title=str(agy_info.get("title") or codex_info.get("title") or "Synced Session"),
                        agy_step_count=int(agy_info.get("step_count", 0)),
                        codex_turn_count=int(codex_info.get("turn_count", 0)),
                        parent_cascade_id=lineage.parent_cascade_id,
                        parent_codex_thread_id=lineage.parent_codex_thread_id,
                        lineage=lineage,
                    )
                )

        for cid_str, ainfo in agy_sessions.items():
            if cid_str in paired_agy:
                continue
            lineage = _make_lineage(ainfo, {})
            if not include_subagents and lineage.is_subagent:
                continue
            descriptors.append(
                PairedSessionDescriptor(
                    workspace=binding,
                    cascade_id=ainfo["cascade_id"],
                    codex_thread_id=None,
                    title=ainfo["title"],
                    agy_step_count=ainfo["step_count"],
                    codex_turn_count=0,
                    parent_cascade_id=lineage.parent_cascade_id,
                    lineage=lineage,
                )
            )

        for tid_str, cinfo in codex_sessions.items():
            if tid_str in paired_codex:
                continue
            lineage = _make_lineage({}, cinfo)
            if not include_subagents and lineage.is_subagent:
                continue
            descriptors.append(
                PairedSessionDescriptor(
                    workspace=binding,
                    cascade_id=None,
                    codex_thread_id=cinfo["codex_thread_id"],
                    title=cinfo["title"],
                    agy_step_count=0,
                    codex_turn_count=cinfo["turn_count"],
                    parent_codex_thread_id=lineage.parent_codex_thread_id,
                    lineage=lineage,
                )
            )

        return tuple(descriptors)


# ---------------------------------------------------------------------------
# Presence, WAL Concurrency Guard & Self-Describing `BackupManifest`
# ---------------------------------------------------------------------------


class TransportMode(str, Enum):
    """Selected execution mode for reading or writing a host session."""

    AUTO = "auto"
    ONLINE_RPC = "online_rpc"
    OFFLINE_STORAGE = "offline_storage"


@dataclass(frozen=True)
class AgyDaemonEndpoint:
    """Live Antigravity `LanguageServerService` endpoint discovered from `hub_<port>.json`."""

    pid: int
    http_port: int
    https_port: int
    ls_version: str
    csrf_secret: CsrfSecret


@dataclass(frozen=True)
class CodexDaemonEndpoint:
    """Live `codex app-server` JSON-RPC v2 Unix socket endpoint."""

    socket_path: Path
    pid: int | None = None


@dataclass(frozen=True)
class HostLockSnapshot:
    """Point-in-time concurrency and liveness probe for a session on both hosts."""

    agy_daemon: AgyDaemonEndpoint | None
    agy_presence_locked: bool
    agy_wal_active: bool
    agy_turn_inflight: bool
    codex_daemon: CodexDaemonEndpoint | None
    codex_wal_active: bool
    codex_turn_inflight: bool

    def can_write_agy_offline(self) -> bool:
        """True iff no live daemon, presence flock, or in-flight turn holds the cascade."""
        return (
            (self.agy_daemon is None)
            and (not self.agy_presence_locked)
            and (not self.agy_wal_active)
            and (not self.agy_turn_inflight)
        )

    def can_write_codex_offline(self) -> bool:
        """True iff no live `codex app-server` or active WAL/in-flight turn holds the thread."""
        return (
            (self.codex_daemon is None)
            and (not self.codex_wal_active)
            and (not self.codex_turn_inflight)
        )


@dataclass(frozen=True)
class BackupManifest:
    """Self-describing atomic backup manifest created before any offline mutation (grafted from Candidate 1)."""

    manifest_id: str
    created_at_iso: str
    target_host: HostKind
    target_session_id: str
    backup_dir: Path
    backed_up_files: Tuple[Path, ...]
    file_sha256_map: Mapping[str, str]
    restoration_command: str


class PresenceWalGuard:
    """Enforces read-only SQLite access, `flock` presence probing, and pre-write `BackupManifest` creation."""

    def __init__(self, paths: HostEnvironmentPaths) -> None:
        self._paths = paths

    def _is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _discover_agy_daemon(self) -> AgyDaemonEndpoint | None:
        daemon_dir = self._paths.agy_data_dir / "daemon"
        if not daemon_dir.is_dir():
            return None
        for hf in sorted(daemon_dir.glob("hub_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                raw = json.loads(hf.read_text(encoding="utf-8"))
                pid = int(raw.get("pid") or 0)
                tok = str(raw.get("csrfToken") or "").strip()
                if pid > 0 and tok and self._is_pid_alive(pid):
                    return AgyDaemonEndpoint(
                        pid=pid,
                        http_port=int(raw.get("httpPort") or raw.get("port") or 0),
                        https_port=int(raw.get("httpsPort") or 0),
                        ls_version=str(raw.get("lsVersion") or "2.30.0"),
                        csrf_secret=CsrfSecret(tok),
                    )
            except Exception:
                continue
        return None

    def _discover_codex_daemon(self) -> CodexDaemonEndpoint | None:
        daemon_json = self._paths.codex_home_dir / "daemon.json"
        if daemon_json.is_file():
            try:
                raw = json.loads(daemon_json.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    pid = int(raw.get("pid") or 0)
                    if pid > 0 and self._is_pid_alive(pid):
                        return CodexDaemonEndpoint(socket_path=daemon_json, pid=pid)
            except Exception:
                pass

        for candidate in (
            self._paths.codex_home_dir / "app-server.sock",
            self._paths.codex_home_dir / "run" / "app-server.sock",
        ):
            try:
                if not candidate.exists() or not stat.S_ISSOCK(candidate.lstat().st_mode):
                    continue
            except OSError:
                continue
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(0.2)
            try:
                sock.connect(str(candidate))
                return CodexDaemonEndpoint(socket_path=candidate)
            except OSError:
                continue
            finally:
                sock.close()
        return None

    def _probe_flock_locked(self, lock_file: Path) -> bool:
        if not lock_file.exists():
            return False
        try:
            fd = os.open(str(lock_file), os.O_RDONLY)
        except PermissionError:
            return True
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
        finally:
            os.close(fd)

    def probe_locks(
        self,
        cascade_id: CascadeId | None,
        codex_thread_id: CodexThreadId | None,
        *,
        agy_turn_inflight: bool = False,
        codex_turn_inflight: bool = False,
    ) -> HostLockSnapshot:
        """Probe `~/.gemini/antigravity/presence/<cascade_id>.lock` via non-blocking `flock`,
        check `-wal` files and daemon liveness (`hub_<port>.json` PID + loopback ping,
        Codex Unix domain socket).
        """
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")

        agy_daemon = self._discover_agy_daemon()
        codex_daemon = self._discover_codex_daemon()

        agy_presence_locked = False
        agy_wal_active = False
        sum_wal = self._paths.agy_data_dir / "conversation_summaries.db-wal"
        if sum_wal.exists() and sum_wal.stat().st_size > 0 and agy_daemon is not None:
            agy_wal_active = True
        if cascade_id is not None:
            lock_path = _validated_child_path(
                self._paths.agy_data_dir,
                "presence",
                f"{cascade_id}.lock",
                label="cascade_id",
            )
            agy_presence_locked = self._probe_flock_locked(lock_path)
            conv_wal = _validated_child_path(
                self._paths.agy_data_dir,
                "conversations",
                f"{cascade_id}.db-wal",
                label="cascade_id",
            )
            if conv_wal.exists() and conv_wal.stat().st_size > 0 and (agy_daemon is not None or agy_presence_locked):
                agy_wal_active = True

        codex_wal_active = False
        state_wal = self._paths.codex_home_dir / "state_5.sqlite-wal"
        if state_wal.exists() and state_wal.stat().st_size > 0 and codex_daemon is not None:
            codex_wal_active = True

        return HostLockSnapshot(
            agy_daemon=agy_daemon,
            agy_presence_locked=agy_presence_locked,
            agy_wal_active=agy_wal_active,
            agy_turn_inflight=agy_turn_inflight,
            codex_daemon=codex_daemon,
            codex_wal_active=codex_wal_active,
            codex_turn_inflight=codex_turn_inflight,
        )

    def sqlite_readonly_uri(self, db_path: Path) -> str:
        """Build a `file:<path>?mode=ro` SQLite URI for safe concurrent WAL reading."""
        return f"{db_path.resolve(strict=False).as_uri()}?mode=ro"

    def create_offline_backup(
        self,
        target_host: HostKind,
        target_session_id: str,
        target_files: Sequence[Path],
    ) -> BackupManifest:
        """Create an atomic timestamped backup under `sync_state_dir/backups/` and write `manifest.json`."""
        validate_session_id(str(target_session_id), label="target_session_id")
        ts_compact = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_id = f"backup-{target_host.value}-{target_session_id}-{ts_compact}"
        backup_dir = _validated_child_path(
            self._paths.sync_state_dir,
            "backups",
            manifest_id,
            label="target_session_id",
        )
        backup_dir.mkdir(parents=True, exist_ok=True)

        backed_up: list[Path] = []
        sha_map: dict[str, str] = {}
        restore_cmds: list[str] = []
        target_set = {p.resolve(strict=False) for p in target_files if p.is_file()}

        for idx, src_path in enumerate(target_files):
            if not src_path.is_file():
                continue
            dst_name = f"{idx:02d}_{src_path.name}"
            dst_path = backup_dir / dst_name
            shutil.copy2(src_path, dst_path)
            digest = hashlib.sha256(dst_path.read_bytes()).hexdigest()
            backed_up.append(dst_path)
            sha_map[str(src_path)] = digest
            restore_cmds.append(f"cp -p {shlex.quote(str(dst_path))} {shlex.quote(str(src_path))}")
            if src_path.suffix in (".db", ".sqlite"):
                for suffix in ("-wal", "-shm"):
                    sidecar = src_path.with_name(f"{src_path.name}{suffix}")
                    if sidecar.resolve(strict=False) not in target_set:
                        restore_cmds.append(f"rm -f {shlex.quote(str(sidecar))}")

        restoration_command = " && ".join(restore_cmds) if restore_cmds else ":"
        created_iso = _now_iso()
        manifest_payload = {
            "manifest_id": manifest_id,
            "created_at_iso": created_iso,
            "target_host": target_host.value,
            "target_session_id": target_session_id,
            "backup_dir": str(backup_dir),
            "backed_up_files": [str(p) for p in backed_up],
            "file_sha256_map": sha_map,
            "restoration_command": restoration_command,
        }
        _atomic_write_text(
            backup_dir / "manifest.json",
            json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n",
        )
        return BackupManifest(
            manifest_id=manifest_id,
            created_at_iso=created_iso,
            target_host=target_host,
            target_session_id=target_session_id,
            backup_dir=backup_dir,
            backed_up_files=tuple(backed_up),
            file_sha256_map=sha_map,
            restoration_command=restoration_command,
        )


# ---------------------------------------------------------------------------
# Incremental Cursor & Fork State Machine (`CursorForkStateMachine`)
# ---------------------------------------------------------------------------


class ForkResolutionPolicy(str, Enum):
    """Explicit operator policy when both hosts have advanced past the last sync cursor."""

    HALT_ON_DIVERGENCE = "halt_on_divergence"
    FORK_NEW_SESSION = "fork_new_session"
    BACKUP_AND_OVERWRITE_TARGET = "backup_and_overwrite_target"


class SyncDirection(str, Enum):
    """Direction of synchronization for a paired session."""

    AUTO_INCREMENTAL = "auto_incremental"
    AGY_TO_CODEX = "agy_to_codex"
    CODEX_TO_AGY = "codex_to_agy"


@dataclass(frozen=True)
class SessionSyncCursor:
    """Persisted watermark state for a paired `(CascadeId, CodexThreadId)` session."""

    workspace_uri: WorkspaceRootUri
    cascade_id: CascadeId
    codex_thread_id: CodexThreadId
    last_synced_turn_id: TurnId
    last_synced_agy_step_idx: StepIndex
    last_synced_codex_line_no: RolloutLineNo
    agy_boundary_digest: ContentDigest
    codex_boundary_digest: ContentDigest
    updated_at_iso: str


@dataclass(frozen=True)
class NoOpTransition:
    """Neither host has completed a new turn since `cursor`."""

    kind: Literal["no_op"]
    cursor: SessionSyncCursor
    deferred_inflight_host: HostKind | None = None


@dataclass(frozen=True)
class InitialImportTransition:
    """One host has completed turns and the peer session does not yet exist or is empty."""

    kind: Literal["initial_import"]
    source_host: HostKind
    target_host: HostKind
    events_to_apply: Tuple[CanonicalEvent, ...]
    new_watermark: CompletedTurnWatermark
    selected_transport: TransportMode


@dataclass(frozen=True)
class IncrementalAppendTransition:
    """Exactly one host has advanced past `cursor` while the target still matches `cursor`."""

    kind: Literal["incremental_append"]
    source_host: HostKind
    target_host: HostKind
    cursor: SessionSyncCursor
    delta_events: Tuple[CanonicalEvent, ...]
    new_watermark: CompletedTurnWatermark
    selected_transport: TransportMode


@dataclass(frozen=True)
class DivergedConflictTransition:
    """Both hosts advanced independently past `cursor` or boundary digests mismatched."""

    kind: Literal["diverged"]
    source_host: HostKind
    target_host: HostKind
    cursor: SessionSyncCursor
    common_ancestor_watermark: CompletedTurnWatermark | None
    agy_head: CompletedTurnWatermark | None
    codex_head: CompletedTurnWatermark | None
    reason: str
    resolution_policy: ForkResolutionPolicy
    preallocated_fork_session_id: str | None = None
    fork_events_to_apply: Tuple[CanonicalEvent, ...] = ()
    selected_transport: TransportMode = TransportMode.AUTO


@dataclass(frozen=True)
class DeferredBusyTransition:
    """Target host has an in-flight turn or holds a lock incompatible with the requested mode."""

    kind: Literal["deferred_busy"]
    blocking_host: HostKind
    reason: str
    retry_after_ms: int = 1500


CursorTransition: TypeAlias = (
    NoOpTransition
    | InitialImportTransition
    | IncrementalAppendTransition
    | DivergedConflictTransition
    | DeferredBusyTransition
)


class CursorForkStateMachine:
    """Pure state machine computing the sync transition from source/target ledgers and cursor."""

    @staticmethod
    def _select_transport_or_defer(
        target_host: HostKind,
        lock_snapshot: HostLockSnapshot,
        requested_mode: TransportMode,
        online_rpc_write_supported: bool = False,
    ) -> TransportMode | DeferredBusyTransition:
        if target_host == HostKind.ANTIGRAVITY:
            if lock_snapshot.agy_turn_inflight:
                return DeferredBusyTransition(
                    kind="deferred_busy",
                    blocking_host=HostKind.ANTIGRAVITY,
                    reason="Antigravity target session has an in-flight turn.",
                )
            if requested_mode == TransportMode.ONLINE_RPC:
                if lock_snapshot.agy_daemon is None:
                    return DeferredBusyTransition(
                        kind="deferred_busy",
                        blocking_host=HostKind.ANTIGRAVITY,
                        reason="Online RPC requested for Antigravity, but no live LanguageServer daemon is available.",
                    )
                if not online_rpc_write_supported:
                    return DeferredBusyTransition(
                        kind="deferred_busy",
                        blocking_host=target_host,
                        reason=f"Online write RPC for {target_host.value} is not enabled; deferring to avoid raw-writing active WAL storage.",
                    )
                return TransportMode.ONLINE_RPC
            if requested_mode == TransportMode.OFFLINE_STORAGE:
                if not lock_snapshot.can_write_agy_offline():
                    return DeferredBusyTransition(
                        kind="deferred_busy",
                        blocking_host=HostKind.ANTIGRAVITY,
                        reason="Offline write blocked by active Antigravity presence lock or WAL daemon.",
                    )
                return TransportMode.OFFLINE_STORAGE
            if lock_snapshot.can_write_agy_offline():
                return TransportMode.OFFLINE_STORAGE
            return DeferredBusyTransition(
                kind="deferred_busy",
                blocking_host=HostKind.ANTIGRAVITY,
                reason="Antigravity session is locked by an active presence flock or WAL daemon.",
            )

        if lock_snapshot.codex_turn_inflight:
            return DeferredBusyTransition(
                kind="deferred_busy",
                blocking_host=HostKind.CODEX,
                reason="Codex target thread has an in-flight turn.",
            )
        if requested_mode == TransportMode.ONLINE_RPC:
            if lock_snapshot.codex_daemon is None:
                return DeferredBusyTransition(
                    kind="deferred_busy",
                    blocking_host=HostKind.CODEX,
                    reason="Online RPC requested for Codex, but no live app-server socket is available.",
                )
            if not online_rpc_write_supported:
                return DeferredBusyTransition(
                    kind="deferred_busy",
                    blocking_host=target_host,
                    reason=f"Online write RPC for {target_host.value} is not enabled; deferring to avoid raw-writing active WAL storage.",
                )
            return TransportMode.ONLINE_RPC
        if requested_mode == TransportMode.OFFLINE_STORAGE:
            if not lock_snapshot.can_write_codex_offline():
                return DeferredBusyTransition(
                    kind="deferred_busy",
                    blocking_host=HostKind.CODEX,
                    reason="Offline write blocked by active Codex app-server WAL lock.",
                )
            return TransportMode.OFFLINE_STORAGE
        if lock_snapshot.can_write_codex_offline():
            return TransportMode.OFFLINE_STORAGE
        return DeferredBusyTransition(
            kind="deferred_busy",
            blocking_host=HostKind.CODEX,
            reason="Codex thread cannot be written safely right now due to active WAL lock.",
        )

    @staticmethod
    def _find_watermark(
        ledger: TrajectoryLedger | None,
        turn_id: TurnId,
    ) -> CompletedTurnWatermark | None:
        if ledger is None:
            return None
        for wm in ledger.completed_turn_watermarks:
            if wm.turn_id == turn_id:
                return wm
        return None

    @staticmethod
    def evaluate(
        agy_ledger: TrajectoryLedger | None,
        codex_ledger: TrajectoryLedger | None,
        cursor: SessionSyncCursor | None,
        direction: SyncDirection,
        lock_snapshot: HostLockSnapshot,
        transport_mode: TransportMode,
        fork_policy: ForkResolutionPolicy,
        online_rpc_write_supported: bool = False,
    ) -> CursorTransition:
        """Compute the deterministic `CursorTransition` without performing I/O."""
        agy_head = agy_ledger.latest_watermark() if agy_ledger is not None else None
        codex_head = codex_ledger.latest_watermark() if codex_ledger is not None else None

        if cursor is None:
            if direction == SyncDirection.AGY_TO_CODEX or (
                direction == SyncDirection.AUTO_INCREMENTAL and agy_head is not None and codex_head is None
            ):
                if agy_ledger is None or agy_head is None:
                    if agy_ledger is not None and agy_ledger.has_inflight_turn:
                        return DeferredBusyTransition(
                            kind="deferred_busy",
                            blocking_host=HostKind.ANTIGRAVITY,
                            reason="Source Antigravity session only has an uncompleted in-flight turn.",
                        )
                    raise ValueError("Source Antigravity session has no completed turns to sync.")
                if codex_head is None:
                    sel = CursorForkStateMachine._select_transport_or_defer(
                        HostKind.CODEX,
                        lock_snapshot,
                        transport_mode,
                        online_rpc_write_supported=online_rpc_write_supported,
                    )
                    if isinstance(sel, DeferredBusyTransition):
                        return sel
                    return InitialImportTransition(
                        kind="initial_import",
                        source_host=HostKind.ANTIGRAVITY,
                        target_host=HostKind.CODEX,
                        events_to_apply=agy_ledger.slice_after_watermark(None),
                        new_watermark=agy_head,
                        selected_transport=sel,
                    )

            if direction == SyncDirection.CODEX_TO_AGY or (
                direction == SyncDirection.AUTO_INCREMENTAL and codex_head is not None and agy_head is None
            ):
                if codex_ledger is None or codex_head is None:
                    if codex_ledger is not None and codex_ledger.has_inflight_turn:
                        return DeferredBusyTransition(
                            kind="deferred_busy",
                            blocking_host=HostKind.CODEX,
                            reason="Source Codex thread only has an uncompleted in-flight turn.",
                        )
                    raise ValueError("Source Codex thread has no completed turns to sync.")
                if agy_head is None:
                    sel = CursorForkStateMachine._select_transport_or_defer(
                        HostKind.ANTIGRAVITY,
                        lock_snapshot,
                        transport_mode,
                        online_rpc_write_supported=online_rpc_write_supported,
                    )
                    if isinstance(sel, DeferredBusyTransition):
                        return sel
                    return InitialImportTransition(
                        kind="initial_import",
                        source_host=HostKind.CODEX,
                        target_host=HostKind.ANTIGRAVITY,
                        events_to_apply=codex_ledger.slice_after_watermark(None),
                        new_watermark=codex_head,
                        selected_transport=sel,
                    )

            if agy_head is not None and codex_head is not None:
                if agy_head.turn_id == codex_head.turn_id and agy_head.boundary_digest == codex_head.boundary_digest:
                    synth_cursor = SessionSyncCursor(
                        workspace_uri=agy_ledger.workspace_uri if agy_ledger else WorkspaceRootUri("file:///workspace"),
                        cascade_id=CascadeId("00000000-0000-0000-0000-000000000000"),
                        codex_thread_id=CodexThreadId("00000000-0000-0000-0000-000000000000"),
                        last_synced_turn_id=agy_head.turn_id,
                        last_synced_agy_step_idx=agy_head.last_agy_step_index or StepIndex(0),
                        last_synced_codex_line_no=codex_head.last_codex_line_no or RolloutLineNo(1),
                        agy_boundary_digest=agy_head.boundary_digest,
                        codex_boundary_digest=codex_head.boundary_digest,
                        updated_at_iso=_now_iso(),
                    )
                    return NoOpTransition(kind="no_op", cursor=synth_cursor)

                src_host = HostKind.CODEX if direction == SyncDirection.CODEX_TO_AGY else HostKind.ANTIGRAVITY
                tgt_host = HostKind.ANTIGRAVITY if src_host == HostKind.CODEX else HostKind.CODEX
                src_ledger = codex_ledger if src_host == HostKind.CODEX else agy_ledger
                assert src_ledger is not None
                sel = CursorForkStateMachine._select_transport_or_defer(
                    tgt_host,
                    lock_snapshot,
                    transport_mode,
                    online_rpc_write_supported=online_rpc_write_supported,
                )
                if isinstance(sel, DeferredBusyTransition):
                    return sel
                synth_cursor = SessionSyncCursor(
                    workspace_uri=src_ledger.workspace_uri,
                    cascade_id=CascadeId("00000000-0000-0000-0000-000000000000"),
                    codex_thread_id=CodexThreadId("00000000-0000-0000-0000-000000000000"),
                    last_synced_turn_id=TurnId("turn-0"),
                    last_synced_agy_step_idx=StepIndex(0),
                    last_synced_codex_line_no=RolloutLineNo(0),
                    agy_boundary_digest=agy_head.boundary_digest,
                    codex_boundary_digest=codex_head.boundary_digest,
                    updated_at_iso=_now_iso(),
                )
                return DivergedConflictTransition(
                    kind="diverged",
                    source_host=src_host,
                    target_host=tgt_host,
                    cursor=synth_cursor,
                    common_ancestor_watermark=None,
                    agy_head=agy_head,
                    codex_head=codex_head,
                    reason="Both sessions already contain independent non-empty histories without a prior sync cursor.",
                    resolution_policy=fork_policy,
                    preallocated_fork_session_id=str(uuid.uuid4()),
                    fork_events_to_apply=src_ledger.slice_after_watermark(None),
                    selected_transport=sel,
                )

            raise ValueError("Neither Antigravity nor Codex has any completed turns to synchronize.")

        agy_anchor_wm = CursorForkStateMachine._find_watermark(agy_ledger, cursor.last_synced_turn_id)
        codex_anchor_wm = CursorForkStateMachine._find_watermark(codex_ledger, cursor.last_synced_turn_id)

        agy_rewritten = (
            agy_anchor_wm is None
            or agy_anchor_wm.boundary_digest != cursor.agy_boundary_digest
        )
        codex_rewritten = (
            codex_anchor_wm is None
            or codex_anchor_wm.boundary_digest != cursor.codex_boundary_digest
        )

        if agy_rewritten or codex_rewritten:
            if (
                agy_head is not None
                and codex_head is not None
                and agy_head.turn_id == codex_head.turn_id
                and agy_head.boundary_digest == codex_head.boundary_digest
            ):
                converged_cursor = SessionSyncCursor(
                    workspace_uri=cursor.workspace_uri,
                    cascade_id=cursor.cascade_id,
                    codex_thread_id=cursor.codex_thread_id,
                    last_synced_turn_id=agy_head.turn_id,
                    last_synced_agy_step_idx=agy_head.last_agy_step_index or StepIndex(0),
                    last_synced_codex_line_no=codex_head.last_codex_line_no or RolloutLineNo(1),
                    agy_boundary_digest=agy_head.boundary_digest,
                    codex_boundary_digest=codex_head.boundary_digest,
                    updated_at_iso=_now_iso(),
                )
                return NoOpTransition(kind="no_op", cursor=converged_cursor)
            src_host = HostKind.CODEX if direction == SyncDirection.CODEX_TO_AGY else HostKind.ANTIGRAVITY
            tgt_host = HostKind.ANTIGRAVITY if src_host == HostKind.CODEX else HostKind.CODEX
            src_ledger = codex_ledger if src_host == HostKind.CODEX else agy_ledger
            sel = CursorForkStateMachine._select_transport_or_defer(
                tgt_host,
                lock_snapshot,
                transport_mode,
                online_rpc_write_supported=online_rpc_write_supported,
            )
            if isinstance(sel, DeferredBusyTransition):
                return sel
            fork_events = src_ledger.slice_after_watermark(None) if src_ledger is not None else ()
            return DivergedConflictTransition(
                kind="diverged",
                source_host=src_host,
                target_host=tgt_host,
                cursor=cursor,
                common_ancestor_watermark=None,
                agy_head=agy_head,
                codex_head=codex_head,
                reason="Watermark turn missing or boundary digest mutated since last sync.",
                resolution_policy=fork_policy,
                preallocated_fork_session_id=str(uuid.uuid4()),
                fork_events_to_apply=fork_events,
                selected_transport=sel,
            )

        assert agy_head is not None and codex_head is not None
        assert agy_ledger is not None and codex_ledger is not None

        agy_advanced = (
            agy_head.turn_id != cursor.last_synced_turn_id
            or int(agy_head.last_agy_step_index or 0) > int(cursor.last_synced_agy_step_idx)
        )
        codex_advanced = (
            codex_head.turn_id != cursor.last_synced_turn_id
            or int(codex_head.last_codex_line_no or 0) > int(cursor.last_synced_codex_line_no)
        )

        if not agy_advanced and not codex_advanced:
            inflight_host: HostKind | None = None
            if agy_ledger.has_inflight_turn:
                inflight_host = HostKind.ANTIGRAVITY
            elif codex_ledger.has_inflight_turn:
                inflight_host = HostKind.CODEX
            return NoOpTransition(
                kind="no_op",
                cursor=cursor,
                deferred_inflight_host=inflight_host,
            )

        if agy_advanced and not codex_advanced:
            if direction == SyncDirection.CODEX_TO_AGY:
                sel = CursorForkStateMachine._select_transport_or_defer(
                    HostKind.ANTIGRAVITY,
                    lock_snapshot,
                    transport_mode,
                    online_rpc_write_supported=online_rpc_write_supported,
                )
                if isinstance(sel, DeferredBusyTransition):
                    return sel
                return DivergedConflictTransition(
                    kind="diverged",
                    source_host=HostKind.CODEX,
                    target_host=HostKind.ANTIGRAVITY,
                    cursor=cursor,
                    common_ancestor_watermark=agy_anchor_wm,
                    agy_head=agy_head,
                    codex_head=codex_head,
                    reason="Target Antigravity session has uncommitted turns ahead of Codex source.",
                    resolution_policy=fork_policy,
                    preallocated_fork_session_id=str(uuid.uuid4()),
                    fork_events_to_apply=codex_ledger.slice_after_watermark(None),
                    selected_transport=sel,
                )
            sel = CursorForkStateMachine._select_transport_or_defer(
                HostKind.CODEX,
                lock_snapshot,
                transport_mode,
                online_rpc_write_supported=online_rpc_write_supported,
            )
            if isinstance(sel, DeferredBusyTransition):
                return sel
            delta = agy_ledger.slice_after_watermark(cursor.last_synced_turn_id)
            return IncrementalAppendTransition(
                kind="incremental_append",
                source_host=HostKind.ANTIGRAVITY,
                target_host=HostKind.CODEX,
                cursor=cursor,
                delta_events=delta,
                new_watermark=agy_head,
                selected_transport=sel,
            )

        if codex_advanced and not agy_advanced:
            if direction == SyncDirection.AGY_TO_CODEX:
                sel = CursorForkStateMachine._select_transport_or_defer(
                    HostKind.CODEX,
                    lock_snapshot,
                    transport_mode,
                    online_rpc_write_supported=online_rpc_write_supported,
                )
                if isinstance(sel, DeferredBusyTransition):
                    return sel
                return DivergedConflictTransition(
                    kind="diverged",
                    source_host=HostKind.ANTIGRAVITY,
                    target_host=HostKind.CODEX,
                    cursor=cursor,
                    common_ancestor_watermark=codex_anchor_wm,
                    agy_head=agy_head,
                    codex_head=codex_head,
                    reason="Target Codex thread has uncommitted turns ahead of Antigravity source.",
                    resolution_policy=fork_policy,
                    preallocated_fork_session_id=str(uuid.uuid4()),
                    fork_events_to_apply=agy_ledger.slice_after_watermark(None),
                    selected_transport=sel,
                )
            sel = CursorForkStateMachine._select_transport_or_defer(
                HostKind.ANTIGRAVITY,
                lock_snapshot,
                transport_mode,
                online_rpc_write_supported=online_rpc_write_supported,
            )
            if isinstance(sel, DeferredBusyTransition):
                return sel
            delta = codex_ledger.slice_after_watermark(cursor.last_synced_turn_id)
            return IncrementalAppendTransition(
                kind="incremental_append",
                source_host=HostKind.CODEX,
                target_host=HostKind.ANTIGRAVITY,
                cursor=cursor,
                delta_events=delta,
                new_watermark=codex_head,
                selected_transport=sel,
            )

        if agy_head.turn_id == codex_head.turn_id and agy_head.boundary_digest == codex_head.boundary_digest:
            inflight_host_converged: HostKind | None = None
            if agy_ledger.has_inflight_turn:
                inflight_host_converged = HostKind.ANTIGRAVITY
            elif codex_ledger.has_inflight_turn:
                inflight_host_converged = HostKind.CODEX
            converged_cursor = SessionSyncCursor(
                workspace_uri=cursor.workspace_uri,
                cascade_id=cursor.cascade_id,
                codex_thread_id=cursor.codex_thread_id,
                last_synced_turn_id=agy_head.turn_id,
                last_synced_agy_step_idx=agy_head.last_agy_step_index or StepIndex(0),
                last_synced_codex_line_no=codex_head.last_codex_line_no or RolloutLineNo(1),
                agy_boundary_digest=agy_head.boundary_digest,
                codex_boundary_digest=codex_head.boundary_digest,
                updated_at_iso=_now_iso(),
            )
            return NoOpTransition(
                kind="no_op",
                cursor=converged_cursor,
                deferred_inflight_host=inflight_host_converged,
            )

        src_host = HostKind.CODEX if direction == SyncDirection.CODEX_TO_AGY else HostKind.ANTIGRAVITY
        tgt_host = HostKind.ANTIGRAVITY if src_host == HostKind.CODEX else HostKind.CODEX
        src_ledger = codex_ledger if src_host == HostKind.CODEX else agy_ledger
        sel = CursorForkStateMachine._select_transport_or_defer(
            tgt_host,
            lock_snapshot,
            transport_mode,
            online_rpc_write_supported=online_rpc_write_supported,
        )
        if isinstance(sel, DeferredBusyTransition):
            return sel
        return DivergedConflictTransition(
            kind="diverged",
            source_host=src_host,
            target_host=tgt_host,
            cursor=cursor,
            common_ancestor_watermark=agy_anchor_wm,
            agy_head=agy_head,
            codex_head=codex_head,
            reason="Both Antigravity and Codex advanced independently past the last sync watermark.",
            resolution_policy=fork_policy,
            preallocated_fork_session_id=str(uuid.uuid4()),
            fork_events_to_apply=src_ledger.slice_after_watermark(None),
            selected_transport=sel,
        )


# ---------------------------------------------------------------------------
# Dual-Mode Transport Port (`HostTransportPort`) & Default Implementation
# ---------------------------------------------------------------------------


class HostTransportPort(Protocol):
    """Port abstracting Online RPC (`LanguageServerService` / `codex app-server`) and Offline Storage."""

    def read_agy_ledger(
        self,
        binding: WorkspaceProjectBinding,
        cascade_id: CascadeId,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
    ) -> TrajectoryLedger:
        """Read an Antigravity cascade via `GetCascadeTrajectorySteps` RPC or `?mode=ro` SQLite."""
        ...

    def read_codex_ledger(
        self,
        binding: WorkspaceProjectBinding,
        thread_id: CodexThreadId,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
    ) -> TrajectoryLedger:
        """Read a Codex thread via `thread/read` JSON-RPC v2 or `rollout-*.jsonl`."""
        ...

    def apply_to_agy(
        self,
        binding: WorkspaceProjectBinding,
        cascade_id: CascadeId | None,
        events: Sequence[CanonicalEvent],
        full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
        parent_cascade_id: CascadeId | None = None,
        source_tag: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
        incremental: bool = False,
        lineage: SubagentLineage | None = None,
        forked_from_cascade_id: CascadeId | None = None,
    ) -> CascadeId:
        """Write delta events and companion projections to Antigravity."""
        ...

    def apply_to_codex(
        self,
        binding: WorkspaceProjectBinding,
        thread_id: CodexThreadId | None,
        events: Sequence[CanonicalEvent],
        full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
        parent_thread_id: CodexThreadId | None = None,
        incremental: bool = False,
        lineage: SubagentLineage | None = None,
    ) -> CodexThreadId:
        """Write delta events and thread index metadata to Codex."""
        ...


class DefaultHostTransport:
    """Production dual-mode transport reading via `?mode=ro` SQLite/JSONL and writing all host stores."""

    supports_online_rpc_write: bool = False

    def __init__(self, paths: HostEnvironmentPaths) -> None:
        self._paths = paths
        self._codec = TrajectoryWireCodec()

    def _read_agy_explicit_title(self, cascade_id: CascadeId) -> str | None:
        validate_session_id(str(cascade_id), label="cascade_id")
        pbtxt_title: str | None = None
        annot_path = _validated_child_path(
            self._paths.agy_data_dir,
            "annotations",
            f"{cascade_id}.pbtxt",
            label="cascade_id",
        )
        if annot_path.is_file():
            pbtxt_title = parse_annotation_pbtxt_title(
                annot_path.read_text(encoding="utf-8", errors="replace")
            )

        db_title: str | None = None
        summary_annot_title: str | None = None
        sum_db = self._paths.agy_data_dir / "conversation_summaries.db"
        if sum_db.is_file():
            try:
                conn = _open_readonly_sqlite(sum_db)
                try:
                    row = conn.execute(
                        "SELECT title, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                        (str(cascade_id),),
                    ).fetchone()
                    if row:
                        if row[0] and str(row[0]).strip():
                            db_title = str(row[0]).strip()
                        if row[1]:
                            try:
                                cand = decode_cascade_summary_proto(
                                    bytes(row[1]), fallback_cascade_id=cascade_id
                                ).annotations.title.strip()
                                if cand:
                                    summary_annot_title = cand
                            except ValueError:
                                pass
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                pass

        for candidate in (pbtxt_title, summary_annot_title, db_title):
            if candidate:
                return candidate
        return None

    def read_agy_ledger(
        self,
        binding: WorkspaceProjectBinding,
        cascade_id: CascadeId,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
    ) -> TrajectoryLedger:
        _ = (lock_snapshot, mode)
        validate_session_id(str(cascade_id), label="cascade_id")
        conv_db = _validated_child_path(
            self._paths.agy_data_dir,
            "conversations",
            f"{cascade_id}.db",
            label="cascade_id",
        )
        if not conv_db.is_file():
            raise FileNotFoundError(f"Antigravity conversation database not found: {conv_db}")

        conn = _open_readonly_sqlite(conv_db)
        try:
            rows = conn.execute(
                "SELECT idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format FROM steps ORDER BY idx ASC"
            ).fetchall()
        finally:
            conn.close()

        raw_rows = [
            AgyRawStepRow(
                idx=StepIndex(int(r[0])),
                step_type=int(r[1]),
                status=int(r[2]),
                has_subtrajectory=bool(r[3]),
                metadata_blob=bytes(r[4] or b""),
                step_payload_blob=bytes(r[5] or b""),
                step_format=int(r[6] or 1),
            )
            for r in rows
        ]
        title_override = self._read_agy_explicit_title(cascade_id)
        return self._codec.decode_agy_steps_to_ledger(
            workspace_path=binding.workspace_path,
            workspace_uri=binding.workspace_uri,
            raw_steps=raw_rows,
            explicit_title_override=title_override,
        )

    def _find_codex_rollout_and_title(self, thread_id: CodexThreadId) -> tuple[Path, str | None]:
        validate_session_id(str(thread_id), label="codex_thread_id")
        resolved_codex_home = self._paths.codex_home_dir.resolve(strict=False)
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        rollout_path: Path | None = None
        db_title: str | None = None

        if state_db.is_file():
            try:
                conn = _open_readonly_sqlite(state_db)
                try:
                    row = conn.execute(
                        "SELECT rollout_path, title, name FROM threads WHERE id = ?",
                        (str(thread_id),),
                    ).fetchone()
                    if row:
                        if row[0] and Path(str(row[0])).is_file():
                            cand_rollout = Path(str(row[0]))
                            if cand_rollout.resolve(strict=False).is_relative_to(resolved_codex_home):
                                rollout_path = cand_rollout
                        for tcand in (row[2], row[1]):
                            if tcand and str(tcand).strip():
                                db_title = str(tcand).strip()
                                break
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                pass

        session_idx = self._paths.codex_home_dir / "session_index.jsonl"
        if session_idx.is_file() and not db_title:
            try:
                for line in session_idx.read_text(encoding="utf-8", errors="replace").splitlines():
                    if str(thread_id) not in line:
                        continue
                    obj = json.loads(line)
                    if obj.get("id") == str(thread_id) and str(obj.get("thread_name") or "").strip():
                        db_title = str(obj["thread_name"]).strip()
            except Exception:
                pass

        if rollout_path is None:
            for sub in ("sessions", "archived_sessions"):
                search_root = self._paths.codex_home_dir / sub
                if search_root.is_dir():
                    resolved_search = search_root.resolve(strict=False)
                    matches = [
                        m
                        for m in sorted(search_root.rglob(f"*{thread_id}.jsonl"))
                        if m.resolve(strict=False).is_relative_to(resolved_search)
                    ]
                    if matches:
                        rollout_path = matches[-1]
                        break

        if rollout_path is None or not rollout_path.is_file():
            raise FileNotFoundError(f"Codex rollout JSONL file not found for thread {thread_id}")

        return rollout_path, db_title

    def read_codex_ledger(
        self,
        binding: WorkspaceProjectBinding,
        thread_id: CodexThreadId,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
    ) -> TrajectoryLedger:
        _ = (lock_snapshot, mode)
        validate_session_id(str(thread_id), label="codex_thread_id")
        rollout_path, db_title = self._find_codex_rollout_and_title(thread_id)
        raw_lines = [
            CodexRawRolloutLine(line_no=RolloutLineNo(idx + 1), raw_json=line)
            for idx, line in enumerate(
                rollout_path.read_text(encoding="utf-8", errors="replace").splitlines()
            )
            if line.strip()
        ]
        return self._codec.decode_codex_rollout_to_ledger(
            workspace_path=binding.workspace_path,
            workspace_uri=binding.workspace_uri,
            raw_lines=raw_lines,
            explicit_title_override=db_title,
        )

    def apply_to_agy(
        self,
        binding: WorkspaceProjectBinding,
        cascade_id: CascadeId | None,
        events: Sequence[CanonicalEvent],
        full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
        parent_cascade_id: CascadeId | None = None,
        source_tag: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
        incremental: bool = False,
        lineage: SubagentLineage | None = None,
        forked_from_cascade_id: CascadeId | None = None,
    ) -> CascadeId:
        if mode != TransportMode.OFFLINE_STORAGE:
            raise RuntimeError(
                f"apply_to_agy requires TransportMode.OFFLINE_STORAGE (got {mode.value}); refusing to raw-write active WAL storage."
            )
        if not lock_snapshot.can_write_agy_offline():
            raise RuntimeError(
                "Cannot raw-write Antigravity storage while presence lock, daemon WAL, or in-flight turn is active."
            )

        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if parent_cascade_id is not None:
            validate_session_id(str(parent_cascade_id), label="parent_cascade_id")
        if forked_from_cascade_id is not None:
            validate_session_id(str(forked_from_cascade_id), label="forked_from_cascade_id")

        target_cid = cascade_id or CascadeId(str(uuid.uuid4()))
        validate_session_id(str(target_cid), label="cascade_id")
        traj_id = TrajectoryId(_deterministic_uuid("agy-trajectory", str(target_cid)))

        effective_parent_cid = parent_cascade_id or (
            lineage.parent_cascade_id if lineage is not None else None
        )
        if effective_parent_cid is not None:
            validate_session_id(str(effective_parent_cid), label="parent_cascade_id")
        effective_root_cid = (
            (lineage.root_cascade_id if lineage is not None else None)
            or effective_parent_cid
        )
        if effective_root_cid is not None:
            validate_session_id(str(effective_root_cid), label="root_cascade_id")

        effective_depth = lineage.nesting_depth if lineage is not None else 0
        effective_type_name = lineage.agent_type_name if lineage is not None else ""
        effective_role = lineage.agent_role if lineage is not None else ""
        effective_lineage = SubagentLineage(
            parent_cascade_id=effective_parent_cid,
            root_cascade_id=effective_root_cid,
            parent_codex_thread_id=lineage.parent_codex_thread_id if lineage is not None else None,
            nesting_depth=effective_depth,
            agent_type_name=effective_type_name,
            agent_role=effective_role,
            spawn_step_index=lineage.spawn_step_index if lineage is not None else None,
        )

        conv_db = _validated_child_path(
            self._paths.agy_data_dir,
            "conversations",
            f"{target_cid}.db",
            label="cascade_id",
        )
        conv_db.parent.mkdir(parents=True, exist_ok=True)

        is_incremental = (
            incremental
            and conv_db.is_file()
            and len(events) > 0
        )

        conn = sqlite3.connect(str(conv_db), timeout=10.0)
        try:
            _ensure_agy_conversation_db_schema(conn)
            if is_incremental:
                max_row = conn.execute("SELECT COALESCE(MAX(idx), -1) FROM steps").fetchone()
                start_idx = StepIndex(int(max_row[0]) + 1 if max_row else 0)
                step_rows = self._codec.encode_events_to_agy_steps(
                    events,
                    starting_step_idx=start_idx,
                )
            else:
                conn.execute("DELETE FROM steps")
                step_rows = self._codec.encode_ledger_to_agy_steps(
                    full_ledger,
                    starting_step_idx=StepIndex(0),
                )

            for r in step_rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO steps (
                        idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(r.idx),
                        r.step_type,
                        r.status,
                        1 if r.has_subtrajectory else 0,
                        r.metadata_blob,
                        r.step_payload_blob,
                        r.step_format,
                    ),
                )

            conn.execute("DELETE FROM trajectory_meta")
            conn.execute(
                """
                INSERT INTO trajectory_meta (trajectory_id, cascade_id, trajectory_type, source)
                VALUES (?, ?, 1, ?)
                """,
                (str(traj_id), str(target_cid), source_tag.proto_enum_value),
            )

            created_iso = (
                full_ledger.events[0].anchor.timestamp_iso
                if full_ledger.events
                else _now_iso()
            )
            tmeta_blob = encode_trajectory_metadata_proto(
                project_id=binding.agy_project_id,
                workspace_uris=(binding.workspace_uri,),
                created_at_iso=created_iso,
                nesting_depth=effective_depth,
                parent_conversation_id=str(effective_parent_cid or ""),
                root_conversation_id=str(effective_root_cid or ""),
                agent_type_name=effective_type_name,
                agent_role=effective_role,
            )

            conn.execute(
                "INSERT OR REPLACE INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)",
                (bytes(tmeta_blob),),
            )
            if forked_from_cascade_id is not None:
                conn.execute("DELETE FROM parent_references")
                conn.execute(
                    "INSERT INTO parent_references (idx, data) VALUES (0, ?)",
                    (encode_string_field(1, str(forked_from_cascade_id)),),
                )
            conn.commit()
            persisted_rows = conn.execute(
                "SELECT idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format FROM steps ORDER BY idx ASC"
            ).fetchall()
        finally:
            conn.close()

        persisted_ledger = self._codec.decode_agy_steps_to_ledger(
            workspace_path=binding.workspace_path,
            workspace_uri=binding.workspace_uri,
            raw_steps=[
                AgyRawStepRow(
                    idx=StepIndex(int(r[0])),
                    step_type=int(r[1]),
                    status=int(r[2]),
                    has_subtrajectory=bool(r[3]),
                    metadata_blob=bytes(r[4] or b""),
                    step_payload_blob=bytes(r[5] or b""),
                    step_format=int(r[6] or 1),
                )
                for r in persisted_rows
            ],
            explicit_title_override=full_ledger.title,
        )

        viewed_iso = _now_iso()
        annot_proj = persisted_ledger.project_annotation_pbtxt(target_cid, viewed_iso)
        annot_path = _validated_child_path(
            self._paths.agy_data_dir,
            "annotations",
            f"{target_cid}.pbtxt",
            label="cascade_id",
        )
        _atomic_write_text(annot_path, annot_proj.render_pbtxt())

        transcript_rows = persisted_ledger.project_transcript_rows()
        transcript_path = _validated_child_path(
            self._paths.agy_data_dir,
            "brain",
            str(target_cid),
            ".system_generated",
            "logs",
            "transcript.jsonl",
            label="cascade_id",
        )
        transcript_text = "\n".join(row.to_json_line() for row in transcript_rows) + (
            "\n" if transcript_rows else ""
        )
        _atomic_write_text(transcript_path, transcript_text)

        summary_proj = persisted_ledger.project_agy_cascade_summary(
            cascade_id=target_cid,
            project_id=binding.agy_project_id,
            viewed_at_iso=viewed_iso,
            source=source_tag,
            lineage=effective_lineage,
        )
        if summary_proj.step_count != len(persisted_rows):
            summary_proj = dataclasses.replace(summary_proj, step_count=len(persisted_rows))

        if effective_parent_cid is not None and effective_lineage.is_subagent:
            sidecar_path = _validated_child_path(
                self._paths.agy_data_dir,
                "brain",
                str(effective_parent_cid),
                ".system_generated",
                "subagents",
                f"{target_cid}.json",
                label="cascade_id",
            )
            sidecar_doc: dict[str, Any] = {
                "conversationId": str(target_cid),
                "subagentDescriptor": {
                    "typeName": effective_type_name or "self",
                    "role": effective_role or summary_proj.title,
                },
                "state": "SUBAGENT_STATE_ALIVE",
                "workspaceUris": [str(binding.workspace_uri)],
            }
            if effective_lineage.spawn_step_index is not None:
                sidecar_doc["spawnStepIndex"] = int(effective_lineage.spawn_step_index)
            _atomic_write_text(
                sidecar_path,
                json.dumps(sidecar_doc, ensure_ascii=False, separators=(",", ":")) + "\n",
            )

        last_user_time = summary_proj.created_at_iso
        last_user_idx = -1
        for ev in persisted_ledger.events:
            if ev.kind == "user_input":
                last_user_time = ev.anchor.timestamp_iso or last_user_time
                if ev.anchor.agy_step_index is not None:
                    last_user_idx = int(ev.anchor.agy_step_index)

        sum_db = self._paths.agy_data_dir / "conversation_summaries.db"
        sconn = sqlite3.connect(str(sum_db), timeout=10.0)
        try:
            _ensure_agy_summaries_db_schema(sconn)
            sconn.execute(
                """
                INSERT OR REPLACE INTO conversation_summaries (
                    conversation_id, title, preview, step_count, last_modified_time,
                    workspace_uris, status, source, project_id, agent_name,
                    parent_conversation_id, nesting_depth, battle_id, winning_conversation_id,
                    not_fully_idle, killed, last_user_input_time, last_user_input_step_index,
                    app_data_dir, raw_summary, group_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'CASCADE_RUN_STATUS_IDLE', ?, ?, ?, ?, ?, '', '', 0, 0, ?, ?, ?, ?, '')
                """,
                (
                    str(target_cid),
                    summary_proj.title,
                    summary_proj.preview,
                    summary_proj.step_count,
                    summary_proj.updated_at_iso,
                    json.dumps([str(u) for u in summary_proj.workspace_uris]),
                    summary_proj.source.summary_column_name,
                    str(summary_proj.project_id),
                    summary_proj.agent_name,
                    str(effective_parent_cid or ""),
                    summary_proj.nesting_depth,
                    last_user_time,
                    last_user_idx,
                    str(self._paths.agy_data_dir),
                    summary_proj.summary_proto_bytes,
                ),
            )
            sconn.commit()
        finally:
            sconn.close()

        hub_pb_path = self._paths.agy_data_dir / "agyhub_summaries_proto.pb"
        existing_hub = None
        if hub_pb_path.is_file():
            existing_hub = decode_summaries_state_proto(hub_pb_path.read_bytes())
        conv_mtime = int(conv_db.stat().st_mtime) if conv_db.is_file() else int(time.time())
        hub_proj = persisted_ledger.project_agy_hub_summaries_state(
            existing_state=existing_hub,
            updated_summary=summary_proj,
            conv_db_path=str(conv_db),
            conv_db_mtime=conv_mtime,
        )
        _atomic_write_bytes(hub_pb_path, hub_proj.proto_bytes)

        return target_cid

    def apply_to_codex(
        self,
        binding: WorkspaceProjectBinding,
        thread_id: CodexThreadId | None,
        events: Sequence[CanonicalEvent],
        full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
        parent_thread_id: CodexThreadId | None = None,
        incremental: bool = False,
        lineage: SubagentLineage | None = None,
    ) -> CodexThreadId:
        if mode != TransportMode.OFFLINE_STORAGE:
            raise RuntimeError(
                f"apply_to_codex requires TransportMode.OFFLINE_STORAGE (got {mode.value}); refusing to raw-write active WAL storage."
            )
        if not lock_snapshot.can_write_codex_offline():
            raise RuntimeError(
                "Cannot raw-write Codex storage while app-server daemon, WAL lock, or in-flight turn is active."
            )

        if thread_id is not None:
            validate_session_id(str(thread_id), label="codex_thread_id")
        if parent_thread_id is not None:
            validate_session_id(str(parent_thread_id), label="parent_codex_thread_id")

        target_tid = thread_id or CodexThreadId(str(uuid.uuid4()))
        validate_session_id(str(target_tid), label="codex_thread_id")

        effective_parent_tid = parent_thread_id or (
            lineage.parent_codex_thread_id if lineage is not None else None
        )
        if effective_parent_tid is not None:
            validate_session_id(str(effective_parent_tid), label="parent_codex_thread_id")
        effective_agent_role = (
            (lineage.agent_type_name or None) if lineage is not None else None
        )
        effective_agent_nickname = (
            (lineage.agent_role or None) if lineage is not None else None
        )

        resolved_codex_home = self._paths.codex_home_dir.resolve(strict=False)
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        existing_rollout: Path | None = None
        if state_db.is_file() and thread_id is not None:
            try:
                rconn = _open_readonly_sqlite(state_db)
                try:
                    row = rconn.execute(
                        "SELECT rollout_path FROM threads WHERE id = ?",
                        (str(target_tid),),
                    ).fetchone()
                    if row and row[0]:
                        cand_rollout = Path(str(row[0]))
                        if cand_rollout.resolve(strict=False).is_relative_to(resolved_codex_home):
                            existing_rollout = cand_rollout
                finally:
                    rconn.close()
            except sqlite3.OperationalError:
                existing_rollout = None

        if existing_rollout is None:
            now_dt = datetime.now(tz=timezone.utc)
            ts_str = now_dt.strftime("%Y-%m-%dT%H-%M-%S")
            existing_rollout = _validated_child_path(
                self._paths.codex_home_dir,
                "sessions",
                now_dt.strftime("%Y"),
                now_dt.strftime("%m"),
                now_dt.strftime("%d"),
                f"rollout-{ts_str}-{target_tid}.jsonl",
                label="codex_thread_id",
            )
            existing_rollout.parent.mkdir(parents=True, exist_ok=True)

        is_incremental = (
            incremental
            and existing_rollout.is_file()
            and len(events) > 0
        )

        if is_incremental:
            prior_lines = [
                ln
                for ln in existing_rollout.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip()
            ]
            start_line = RolloutLineNo(len(prior_lines) + 1)
            delta_rollout = self._codec.encode_events_to_codex_rollout(
                events,
                starting_line_no=start_line,
                session_id=str(target_tid),
                cwd=str(binding.workspace_path),
            )
            combined_lines = prior_lines + [r.raw_json for r in delta_rollout]
            _atomic_write_text(existing_rollout, "\n".join(combined_lines) + "\n")
        else:
            full_rollout = self._codec.encode_ledger_to_codex_rollout(
                full_ledger,
                starting_line_no=RolloutLineNo(1),
                session_id=str(target_tid),
                cwd=str(binding.workspace_path),
            )
            _atomic_write_text(
                existing_rollout,
                "\n".join(r.raw_json for r in full_rollout) + ("\n" if full_rollout else ""),
            )

        thread_proj = full_ledger.project_codex_thread_summary(
            thread_id=target_tid,
            project_id=binding.codex_project_id,
        )
        created_sec = max(1, thread_proj.created_at_ms // 1000)
        updated_sec = max(1, thread_proj.updated_at_ms // 1000)
        recency_sec = updated_sec

        conn = sqlite3.connect(str(state_db), timeout=10.0)
        try:
            _ensure_codex_state_db_schema(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider,
                    cwd, title, sandbox_policy, approval_mode, tokens_used, has_user_event,
                    archived, cli_version, first_user_message, agent_nickname, agent_role,
                    created_at_ms, updated_at_ms, preview, recency_at, recency_at_ms,
                    name, project_id, originator, attached_thread_parent_id
                ) VALUES (?, ?, ?, ?, 'desktop', 'openai', ?, ?, 'workspace-write', 'never', 0, 1, 0, '0.166.0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'codex_vscode', ?)
                """,
                (
                    str(target_tid),
                    str(existing_rollout),
                    created_sec,
                    updated_sec,
                    str(thread_proj.cwd),
                    thread_proj.title,
                    thread_proj.preview,
                    effective_agent_nickname,
                    effective_agent_role,
                    thread_proj.created_at_ms,
                    thread_proj.updated_at_ms,
                    thread_proj.preview,
                    recency_sec,
                    thread_proj.updated_at_ms,
                    thread_proj.title,
                    str(binding.codex_project_id),
                    str(effective_parent_tid) if effective_parent_tid else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        session_idx_path = self._paths.codex_home_dir / "session_index.jsonl"
        existing_idx_lines: list[str] = []
        if session_idx_path.is_file():
            for ln in session_idx_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    obj = json.loads(ln)
                    if obj.get("id") == str(target_tid):
                        continue
                except Exception:
                    pass
                existing_idx_lines.append(ln)
        existing_idx_lines.append(
            json.dumps(
                {
                    "id": str(target_tid),
                    "thread_name": thread_proj.title,
                    "updated_at": _now_iso(),
                },
                ensure_ascii=False,
            )
        )
        _atomic_write_text(session_idx_path, "\n".join(existing_idx_lines) + "\n")

        global_state_path = self._paths.codex_home_dir / ".codex-global-state.json"
        gs_data: dict[str, Any] = {}
        if global_state_path.is_file():
            try:
                loaded = json.loads(global_state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    gs_data = loaded
            except Exception:
                gs_data = {}
        assignments = gs_data.get("thread-project-assignments")
        if not isinstance(assignments, dict):
            assignments = {}
            gs_data["thread-project-assignments"] = assignments
        assignments[str(target_tid)] = {
            "projectKind": "local",
            "projectId": str(binding.codex_legacy_project_id),
        }
        _atomic_write_text(
            global_state_path,
            json.dumps(gs_data, ensure_ascii=False, indent=2) + "\n",
        )

        return target_tid


# ---------------------------------------------------------------------------
# Deep Facade (`SyncBridge`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncReceipt:
    """Auditable outcome returned by `SyncBridge.sync_session`."""

    workspace: WorkspaceProjectBinding
    cascade_id: CascadeId
    codex_thread_id: CodexThreadId
    transition_kind: Literal[
        "no_op",
        "initial_import",
        "incremental_append",
        "diverged_halted",
        "diverged_forked",
        "diverged_overwritten",
        "deferred_busy",
    ]
    transport_used: TransportMode
    events_applied: int
    updated_cursor: SessionSyncCursor | None
    forked_from_cascade_id: CascadeId | None = None
    forked_from_codex_thread_id: CodexThreadId | None = None
    backup_manifest: BackupManifest | None = None


class SyncBridge:
    """Deep public facade for `codex-agy-sync`."""

    def __init__(
        self,
        paths: HostEnvironmentPaths | None = None,
        transport: HostTransportPort | None = None,
    ) -> None:
        self._paths = paths or HostEnvironmentPaths.discover()
        self._registry = ProjectRegistry(self._paths)
        self._guard = PresenceWalGuard(self._paths)
        self._transport: HostTransportPort = transport or DefaultHostTransport(self._paths)

    @property
    def paths(self) -> HostEnvironmentPaths:
        return self._paths

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def guard(self) -> PresenceWalGuard:
        return self._guard

    def _cursor_path(self, cascade_id: CascadeId, codex_thread_id: CodexThreadId) -> Path:
        validate_session_id(str(cascade_id), label="cascade_id")
        validate_session_id(str(codex_thread_id), label="codex_thread_id")
        return _validated_child_path(
            self._paths.sync_state_dir,
            "cursors",
            f"{cascade_id}__{codex_thread_id}.json",
            label="cursor_path",
        )

    def _load_cursor(
        self,
        workspace_uri: WorkspaceRootUri,
        cascade_id: CascadeId | None,
        codex_thread_id: CodexThreadId | None,
    ) -> SessionSyncCursor | None:
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")

        cursors_dir = self._paths.sync_state_dir / "cursors"
        if not cursors_dir.is_dir():
            return None
        matches: list[tuple[tuple[str, str], SessionSyncCursor]] = []
        for cf in cursors_dir.glob("*.json"):
            try:
                raw = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("workspace_uri") != str(workspace_uri):
                continue
            raw_cid = str(raw.get("cascade_id") or "")
            raw_tid = str(raw.get("codex_thread_id") or "")
            try:
                validate_session_id(raw_cid, label="cascade_id")
                validate_session_id(raw_tid, label="codex_thread_id")
            except ValueError:
                continue
            cid_match = cascade_id is not None and raw_cid == str(cascade_id)
            tid_match = codex_thread_id is not None and raw_tid == str(codex_thread_id)
            if (
                (cascade_id is not None and codex_thread_id is not None and cid_match and tid_match)
                or (cascade_id is not None and codex_thread_id is None and cid_match)
                or (codex_thread_id is not None and cascade_id is None and tid_match)
            ):
                try:
                    cur_obj = SessionSyncCursor(
                        workspace_uri=WorkspaceRootUri(str(raw["workspace_uri"])),
                        cascade_id=CascadeId(raw_cid),
                        codex_thread_id=CodexThreadId(raw_tid),
                        last_synced_turn_id=TurnId(str(raw["last_synced_turn_id"])),
                        last_synced_agy_step_idx=StepIndex(int(raw["last_synced_agy_step_idx"])),
                        last_synced_codex_line_no=RolloutLineNo(int(raw["last_synced_codex_line_no"])),
                        agy_boundary_digest=ContentDigest(str(raw["agy_boundary_digest"])),
                        codex_boundary_digest=ContentDigest(str(raw["codex_boundary_digest"])),
                        updated_at_iso=str(raw.get("updated_at_iso") or ""),
                    )
                except Exception:
                    continue
                matches.append(((cur_obj.updated_at_iso, cf.name), cur_obj))
        if not matches:
            return None
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1]

    def _save_cursor(self, cursor: SessionSyncCursor) -> None:
        cpath = self._cursor_path(cursor.cascade_id, cursor.codex_thread_id)
        payload = {
            "workspace_uri": str(cursor.workspace_uri),
            "cascade_id": str(cursor.cascade_id),
            "codex_thread_id": str(cursor.codex_thread_id),
            "last_synced_turn_id": str(cursor.last_synced_turn_id),
            "last_synced_agy_step_idx": int(cursor.last_synced_agy_step_idx),
            "last_synced_codex_line_no": int(cursor.last_synced_codex_line_no),
            "agy_boundary_digest": str(cursor.agy_boundary_digest),
            "codex_boundary_digest": str(cursor.codex_boundary_digest),
            "updated_at_iso": cursor.updated_at_iso,
        }
        _atomic_write_text(cpath, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

        cursors_dir = self._paths.sync_state_dir / "cursors"
        if cursors_dir.is_dir():
            ts_compact = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            for cf in list(cursors_dir.glob("*.json")):
                if cf == cpath:
                    continue
                try:
                    raw = json.loads(cf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(raw, dict) or raw.get("workspace_uri") != str(cursor.workspace_uri):
                    continue
                if raw.get("cascade_id") == str(cursor.cascade_id) or raw.get("codex_thread_id") == str(
                    cursor.codex_thread_id
                ):
                    superseded_dir = cursors_dir / "superseded"
                    superseded_dir.mkdir(parents=True, exist_ok=True)
                    dst = superseded_dir / f"{ts_compact}_{cf.name}"
                    try:
                        os.replace(cf, dst)
                    except OSError:
                        pass

    def _target_files_for_backup(
        self,
        target_host: HostKind,
        cascade_id: CascadeId | None,
        codex_thread_id: CodexThreadId | None,
    ) -> list[Path]:
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")

        if target_host == HostKind.ANTIGRAVITY:
            files = [
                self._paths.agy_data_dir / "conversation_summaries.db",
                self._paths.agy_data_dir / "agyhub_summaries_proto.pb",
            ]
            if cascade_id is not None:
                files.extend(
                    [
                        _validated_child_path(
                            self._paths.agy_data_dir,
                            "conversations",
                            f"{cascade_id}.db",
                            label="cascade_id",
                        ),
                        _validated_child_path(
                            self._paths.agy_data_dir,
                            "annotations",
                            f"{cascade_id}.pbtxt",
                            label="cascade_id",
                        ),
                        _validated_child_path(
                            self._paths.agy_data_dir,
                            "brain",
                            str(cascade_id),
                            ".system_generated",
                            "logs",
                            "transcript.jsonl",
                            label="cascade_id",
                        ),
                    ]
                )
        else:
            files = [
                self._paths.codex_home_dir / "state_5.sqlite",
                self._paths.codex_home_dir / ".codex-global-state.json",
                self._paths.codex_home_dir / "session_index.jsonl",
            ]
            if codex_thread_id is not None:
                for sub in ("sessions", "archived_sessions"):
                    sroot = self._paths.codex_home_dir / sub
                    if sroot.is_dir():
                        resolved_sroot = sroot.resolve(strict=False)
                        files.extend(
                            m
                            for m in sroot.rglob(f"*{codex_thread_id}.jsonl")
                            if m.resolve(strict=False).is_relative_to(resolved_sroot)
                        )

        expanded: list[Path] = []
        for p in files:
            if p.is_file():
                expanded.append(p)
                if p.suffix in (".db", ".sqlite"):
                    for sidecar_suffix in ("-wal", "-shm"):
                        sidecar = p.with_name(f"{p.name}{sidecar_suffix}")
                        if sidecar.is_file():
                            expanded.append(sidecar)
        return expanded

    def pair_workspace(
        self,
        workspace: str | Path,
        *,
        prefer_online_rpc: bool = True,
    ) -> WorkspaceProjectBinding:
        """Ensure both Antigravity and Codex have paired, sidebar-visible project entries for `workspace`."""
        locks = self._guard.probe_locks(None, None)
        if not locks.can_write_codex_offline():
            raise RuntimeError(
                "Cannot raw-write Codex state_5.sqlite while Codex daemon or WAL lock is active."
            )
        return self._registry.ensure_workspace_binding(workspace, prefer_online_rpc=prefer_online_rpc)

    def inspect_session_ledger(
        self,
        workspace: str | Path,
        *,
        cascade_id: CascadeId | None = None,
        codex_thread_id: CodexThreadId | None = None,
        mode: TransportMode = TransportMode.AUTO,
    ) -> TrajectoryLedger:
        """Read either host's trajectory into an indexed `TrajectoryLedger` without mutating state."""
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")
        binding = self._registry.resolve_readonly_binding(workspace)
        locks = self._guard.probe_locks(cascade_id, codex_thread_id)
        if cascade_id is not None:
            return self._transport.read_agy_ledger(binding, cascade_id, locks, mode)
        if codex_thread_id is not None:
            return self._transport.read_codex_ledger(binding, codex_thread_id, locks, mode)
        raise ValueError("Must provide either cascade_id or codex_thread_id to inspect_session_ledger.")

    def _resolve_session_lineage(
        self,
        binding: WorkspaceProjectBinding,
        cascade_id: CascadeId | None,
        codex_thread_id: CodexThreadId | None,
    ) -> SubagentLineage:
        parent_cid_str = ""
        root_cid_str = ""
        parent_tid_str = ""
        depth = 0
        type_name = ""
        role = ""
        spawn_idx: StepIndex | None = None
        sc_info: Mapping[str, Any] = {}

        if cascade_id is not None:
            cid_key = validate_session_id(str(cascade_id), label="cascade_id")
            sidecars = self._registry._discover_agy_subagent_sidecars()
            sc_info = sidecars.get(cid_key, {})
            if sc_info:
                parent_cid_str = str(sc_info.get("parent_cascade_id") or "").strip()
                type_name = str(sc_info.get("agent_type_name") or "").strip()
                role = str(sc_info.get("agent_role") or "").strip()
                spawn_idx = sc_info.get("spawn_step_index")

            sum_db = self._paths.agy_data_dir / "conversation_summaries.db"
            if sum_db.is_file():
                try:
                    conn = _open_readonly_sqlite(sum_db)
                    try:
                        row = conn.execute(
                            "SELECT agent_name, parent_conversation_id, nesting_depth FROM conversation_summaries WHERE conversation_id = ?",
                            (cid_key,),
                        ).fetchone()
                        if row:
                            if not role and row[0]:
                                role = str(row[0]).strip()
                            if not parent_cid_str and row[1]:
                                parent_cid_str = str(row[1]).strip()
                            depth = max(depth, int(row[2] or 0))
                    finally:
                        conn.close()
                except sqlite3.OperationalError:
                    pass

            conv_db = _validated_child_path(
                self._paths.agy_data_dir,
                "conversations",
                f"{cid_key}.db",
                label="cascade_id",
            )
            if conv_db.is_file():
                try:
                    conn = _open_readonly_sqlite(conv_db)
                    try:
                        tblob = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
                        if tblob and tblob[0]:
                            meta_info = _parse_agy_trajectory_metadata_lineage(bytes(tblob[0]))
                            if not parent_cid_str and meta_info["parent_conversation_id"]:
                                parent_cid_str = meta_info["parent_conversation_id"]
                            if not root_cid_str and meta_info["root_conversation_id"]:
                                root_cid_str = meta_info["root_conversation_id"]
                            if not type_name and meta_info["agent_type_name"]:
                                type_name = meta_info["agent_type_name"]
                            if not role and meta_info["agent_role"]:
                                role = meta_info["agent_role"]
                            depth = max(depth, int(meta_info["nesting_depth"]))
                    finally:
                        conn.close()
                except sqlite3.OperationalError:
                    pass

        if codex_thread_id is not None:
            tid_key = validate_session_id(str(codex_thread_id), label="codex_thread_id")
            state_db = self._paths.codex_home_dir / "state_5.sqlite"
            if state_db.is_file():
                try:
                    conn = _open_readonly_sqlite(state_db)
                    try:
                        row = conn.execute(
                            "SELECT attached_thread_parent_id, agent_role, agent_nickname FROM threads WHERE id = ?",
                            (tid_key,),
                        ).fetchone()
                        if row:
                            if row[0] and str(row[0]).strip():
                                parent_tid_str = str(row[0]).strip()
                            if not type_name and row[1] and str(row[1]).strip():
                                type_name = str(row[1]).strip()
                            if not role and row[2] and str(row[2]).strip():
                                role = str(row[2]).strip()
                    finally:
                        conn.close()
                except sqlite3.OperationalError:
                    pass

        for attr_name in ("parent_cid_str", "root_cid_str", "parent_tid_str"):
            val = locals()[attr_name]
            if val:
                try:
                    validate_session_id(val, label=attr_name)
                except ValueError:
                    if attr_name == "parent_cid_str":
                        parent_cid_str = ""
                    elif attr_name == "root_cid_str":
                        root_cid_str = ""
                    else:
                        parent_tid_str = ""

        if depth == 0 and (bool(sc_info) or bool(type_name) or bool(role) or spawn_idx is not None):
            depth = 1

        is_actual_subagent = depth > 0 or bool(type_name) or bool(role) or spawn_idx is not None
        if is_actual_subagent:
            if parent_cid_str and not parent_tid_str:
                p_cursor = self._load_cursor(binding.workspace_uri, CascadeId(parent_cid_str), None)
                if p_cursor is not None:
                    parent_tid_str = str(p_cursor.codex_thread_id)
                else:
                    parent_tid_str = _deterministic_uuid("paired-codex-thread", parent_cid_str)
            elif parent_tid_str and not parent_cid_str:
                p_cursor = self._load_cursor(binding.workspace_uri, None, CodexThreadId(parent_tid_str))
                if p_cursor is not None:
                    parent_cid_str = str(p_cursor.cascade_id)
                else:
                    parent_cid_str = _deterministic_uuid("paired-cascade", parent_tid_str)
            if parent_cid_str and not root_cid_str:
                root_cid_str = parent_cid_str

        return SubagentLineage(
            parent_cascade_id=CascadeId(parent_cid_str) if parent_cid_str else None,
            root_cascade_id=CascadeId(root_cid_str) if root_cid_str else None,
            parent_codex_thread_id=CodexThreadId(parent_tid_str) if parent_tid_str else None,
            nesting_depth=depth,
            agent_type_name=type_name,
            agent_role=role,
            spawn_step_index=spawn_idx,
        )

    def sync_session(
        self,
        workspace: str | Path,
        *,
        cascade_id: CascadeId | None = None,
        codex_thread_id: CodexThreadId | None = None,
        direction: SyncDirection = SyncDirection.AUTO_INCREMENTAL,
        mode: TransportMode = TransportMode.AUTO,
        fork_policy: ForkResolutionPolicy = ForkResolutionPolicy.HALT_ON_DIVERGENCE,
        source_tag: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
    ) -> SyncReceipt:
        """Synchronize one paired session (or import an unpaired session) idempotently."""
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")

        binding = self._registry.resolve_readonly_binding(workspace)
        cursor = self._load_cursor(binding.workspace_uri, cascade_id, codex_thread_id)
        if cursor is not None:
            if cascade_id is None:
                cascade_id = cursor.cascade_id
            if codex_thread_id is None:
                codex_thread_id = cursor.codex_thread_id

        lineage = self._resolve_session_lineage(binding, cascade_id, codex_thread_id)

        probe_cid: CascadeId | None = cascade_id
        if probe_cid is None and codex_thread_id is not None:
            det_cid = CascadeId(_deterministic_uuid("paired-cascade", str(codex_thread_id)))
            if self._load_cursor(binding.workspace_uri, det_cid, None) is None:
                probe_cid = det_cid

        probe_tid: CodexThreadId | None = codex_thread_id
        if probe_tid is None and cascade_id is not None:
            det_tid = CodexThreadId(_deterministic_uuid("paired-codex-thread", str(cascade_id)))
            if self._load_cursor(binding.workspace_uri, None, det_tid) is None:
                probe_tid = det_tid

        initial_locks = self._guard.probe_locks(probe_cid, probe_tid)

        agy_ledger: TrajectoryLedger | None = None
        if cascade_id is not None:
            try:
                agy_ledger = self._transport.read_agy_ledger(binding, cascade_id, initial_locks, mode)
            except FileNotFoundError:
                agy_ledger = None
        elif cursor is None and probe_cid is not None:
            try:
                agy_ledger = self._transport.read_agy_ledger(binding, probe_cid, initial_locks, mode)
                cascade_id = probe_cid
            except FileNotFoundError:
                agy_ledger = None

        codex_ledger: TrajectoryLedger | None = None
        if codex_thread_id is not None:
            try:
                codex_ledger = self._transport.read_codex_ledger(binding, codex_thread_id, initial_locks, mode)
            except FileNotFoundError:
                codex_ledger = None
        elif cursor is None and probe_tid is not None:
            try:
                codex_ledger = self._transport.read_codex_ledger(binding, probe_tid, initial_locks, mode)
                codex_thread_id = probe_tid
            except FileNotFoundError:
                codex_ledger = None

        locks = self._guard.probe_locks(
            probe_cid,
            probe_tid,
            agy_turn_inflight=bool(agy_ledger and agy_ledger.has_inflight_turn),
            codex_turn_inflight=bool(codex_ledger and codex_ledger.has_inflight_turn),
        )
        online_rpc_supported = bool(getattr(self._transport, "supports_online_rpc_write", False))

        transition = CursorForkStateMachine.evaluate(
            agy_ledger=agy_ledger,
            codex_ledger=codex_ledger,
            cursor=cursor,
            direction=direction,
            lock_snapshot=locks,
            transport_mode=mode,
            fork_policy=fork_policy,
            online_rpc_write_supported=online_rpc_supported,
        )

        if isinstance(transition, NoOpTransition):
            final_cid = cascade_id or transition.cursor.cascade_id
            final_tid = codex_thread_id or transition.cursor.codex_thread_id
            fixed_cursor = dataclasses.replace(
                transition.cursor,
                workspace_uri=binding.workspace_uri,
                cascade_id=final_cid,
                codex_thread_id=final_tid,
            )
            if cursor is None or (
                cursor.cascade_id,
                cursor.codex_thread_id,
                cursor.last_synced_turn_id,
                cursor.last_synced_agy_step_idx,
                cursor.last_synced_codex_line_no,
                cursor.agy_boundary_digest,
                cursor.codex_boundary_digest,
            ) != (
                fixed_cursor.cascade_id,
                fixed_cursor.codex_thread_id,
                fixed_cursor.last_synced_turn_id,
                fixed_cursor.last_synced_agy_step_idx,
                fixed_cursor.last_synced_codex_line_no,
                fixed_cursor.agy_boundary_digest,
                fixed_cursor.codex_boundary_digest,
            ):
                self._save_cursor(fixed_cursor)
            return SyncReceipt(
                workspace=binding,
                cascade_id=final_cid,
                codex_thread_id=final_tid,
                transition_kind="no_op",
                transport_used=mode,
                events_applied=0,
                updated_cursor=fixed_cursor,
            )

        if isinstance(transition, DeferredBusyTransition):
            return SyncReceipt(
                workspace=binding,
                cascade_id=cascade_id or CascadeId("00000000-0000-0000-0000-000000000000"),
                codex_thread_id=codex_thread_id or CodexThreadId("00000000-0000-0000-0000-000000000000"),
                transition_kind="deferred_busy",
                transport_used=mode,
                events_applied=0,
                updated_cursor=cursor,
            )

        if isinstance(transition, (InitialImportTransition, IncrementalAppendTransition)):
            events_slice = (
                transition.events_to_apply
                if isinstance(transition, InitialImportTransition)
                else transition.delta_events
            )
            if transition.target_host == HostKind.ANTIGRAVITY:
                assert codex_ledger is not None and codex_thread_id is not None
                if cascade_id is not None:
                    target_cid = cascade_id
                else:
                    det_cid = CascadeId(_deterministic_uuid("paired-cascade", str(codex_thread_id)))
                    target_cid = (
                        det_cid
                        if self._load_cursor(binding.workspace_uri, det_cid, None) is None
                        else CascadeId(str(uuid.uuid4()))
                    )
                backup_files = self._target_files_for_backup(HostKind.ANTIGRAVITY, target_cid, codex_thread_id)
                backup = (
                    self._guard.create_offline_backup(HostKind.ANTIGRAVITY, str(target_cid), backup_files)
                    if backup_files and transition.selected_transport == TransportMode.OFFLINE_STORAGE
                    else None
                )
                if transition.selected_transport == TransportMode.OFFLINE_STORAGE:
                    binding = self._registry.ensure_agy_binding(binding.workspace_path)
                if agy_ledger is not None and isinstance(transition, IncrementalAppendTransition):
                    combined_events = list(agy_ledger.events) + list(events_slice)
                    merged_for_agy = TrajectoryLedger.from_events(
                        binding.workspace_path,
                        binding.workspace_uri,
                        combined_events,
                        explicit_title_override=codex_ledger.title,
                        recompute_agy_step_indices=True,
                    )
                else:
                    merged_for_agy = TrajectoryLedger.from_events(
                        binding.workspace_path,
                        binding.workspace_uri,
                        events_slice,
                        explicit_title_override=codex_ledger.title,
                        recompute_agy_step_indices=True,
                    )
                written_cid = self._transport.apply_to_agy(
                    binding=binding,
                    cascade_id=target_cid,
                    events=events_slice,
                    full_ledger=merged_for_agy,
                    lock_snapshot=locks,
                    mode=transition.selected_transport,
                    parent_cascade_id=lineage.parent_cascade_id,
                    source_tag=source_tag,
                    incremental=isinstance(transition, IncrementalAppendTransition),
                    lineage=lineage,
                )
                refreshed_agy = self._transport.read_agy_ledger(
                    binding, written_cid, locks, transition.selected_transport
                )
                agy_wm = refreshed_agy.latest_watermark()
                codex_wm = codex_ledger.latest_watermark()
                assert agy_wm is not None and codex_wm is not None
                new_cursor = SessionSyncCursor(
                    workspace_uri=binding.workspace_uri,
                    cascade_id=written_cid,
                    codex_thread_id=codex_thread_id,
                    last_synced_turn_id=codex_wm.turn_id,
                    last_synced_agy_step_idx=agy_wm.last_agy_step_index or StepIndex(0),
                    last_synced_codex_line_no=codex_wm.last_codex_line_no or RolloutLineNo(1),
                    agy_boundary_digest=agy_wm.boundary_digest,
                    codex_boundary_digest=codex_wm.boundary_digest,
                    updated_at_iso=_now_iso(),
                )
                self._save_cursor(new_cursor)
                return SyncReceipt(
                    workspace=binding,
                    cascade_id=written_cid,
                    codex_thread_id=codex_thread_id,
                    transition_kind=transition.kind,
                    transport_used=transition.selected_transport,
                    events_applied=len(events_slice),
                    updated_cursor=new_cursor,
                    backup_manifest=backup,
                )

            assert agy_ledger is not None and cascade_id is not None
            if codex_thread_id is not None:
                target_tid = codex_thread_id
            else:
                det_tid = CodexThreadId(_deterministic_uuid("paired-codex-thread", str(cascade_id)))
                target_tid = (
                    det_tid
                    if self._load_cursor(binding.workspace_uri, None, det_tid) is None
                    else CodexThreadId(str(uuid.uuid4()))
                )
            backup_files = self._target_files_for_backup(HostKind.CODEX, cascade_id, target_tid)
            backup = (
                self._guard.create_offline_backup(HostKind.CODEX, str(target_tid), backup_files)
                if backup_files and transition.selected_transport == TransportMode.OFFLINE_STORAGE
                else None
            )
            if transition.selected_transport == TransportMode.OFFLINE_STORAGE:
                binding = self._registry.ensure_codex_binding(binding.workspace_path)
            if codex_ledger is not None and isinstance(transition, IncrementalAppendTransition):
                combined_events = list(codex_ledger.events) + list(events_slice)
                merged_for_codex = TrajectoryLedger.from_events(
                    binding.workspace_path,
                    binding.workspace_uri,
                    combined_events,
                    explicit_title_override=agy_ledger.title,
                )
            else:
                merged_for_codex = TrajectoryLedger.from_events(
                    binding.workspace_path,
                    binding.workspace_uri,
                    events_slice,
                    explicit_title_override=agy_ledger.title,
                )
            written_tid = self._transport.apply_to_codex(
                binding=binding,
                thread_id=target_tid,
                events=events_slice,
                full_ledger=merged_for_codex,
                lock_snapshot=locks,
                mode=transition.selected_transport,
                parent_thread_id=lineage.parent_codex_thread_id,
                incremental=isinstance(transition, IncrementalAppendTransition),
                lineage=lineage,
            )
            refreshed_codex = self._transport.read_codex_ledger(
                binding, written_tid, locks, transition.selected_transport
            )
            agy_wm = agy_ledger.latest_watermark()
            codex_wm = refreshed_codex.latest_watermark()
            assert agy_wm is not None and codex_wm is not None
            new_cursor = SessionSyncCursor(
                workspace_uri=binding.workspace_uri,
                cascade_id=cascade_id,
                codex_thread_id=written_tid,
                last_synced_turn_id=agy_wm.turn_id,
                last_synced_agy_step_idx=agy_wm.last_agy_step_index or StepIndex(0),
                last_synced_codex_line_no=codex_wm.last_codex_line_no or RolloutLineNo(1),
                agy_boundary_digest=agy_wm.boundary_digest,
                codex_boundary_digest=codex_wm.boundary_digest,
                updated_at_iso=_now_iso(),
            )
            self._save_cursor(new_cursor)
            return SyncReceipt(
                workspace=binding,
                cascade_id=cascade_id,
                codex_thread_id=written_tid,
                transition_kind=transition.kind,
                transport_used=transition.selected_transport,
                events_applied=len(events_slice),
                updated_cursor=new_cursor,
                backup_manifest=backup,
            )

        assert isinstance(transition, DivergedConflictTransition)
        orig_cid = cascade_id or transition.cursor.cascade_id
        orig_tid = codex_thread_id or transition.cursor.codex_thread_id

        if fork_policy == ForkResolutionPolicy.HALT_ON_DIVERGENCE:
            return SyncReceipt(
                workspace=binding,
                cascade_id=orig_cid,
                codex_thread_id=orig_tid,
                transition_kind="diverged_halted",
                transport_used=transition.selected_transport,
                events_applied=0,
                updated_cursor=cursor,
            )

        if fork_policy == ForkResolutionPolicy.FORK_NEW_SESSION:
            fork_id_str = transition.preallocated_fork_session_id or str(uuid.uuid4())
            if transition.target_host == HostKind.ANTIGRAVITY:
                assert codex_ledger is not None
                fork_cid = CascadeId(fork_id_str)
                backup_files = self._target_files_for_backup(HostKind.ANTIGRAVITY, fork_cid, orig_tid)
                backup = (
                    self._guard.create_offline_backup(HostKind.ANTIGRAVITY, str(fork_cid), backup_files)
                    if backup_files and transition.selected_transport == TransportMode.OFFLINE_STORAGE
                    else None
                )
                if transition.selected_transport == TransportMode.OFFLINE_STORAGE:
                    binding = self._registry.ensure_agy_binding(binding.workspace_path)
                fork_ledger = TrajectoryLedger.from_events(
                    binding.workspace_path,
                    binding.workspace_uri,
                    transition.fork_events_to_apply,
                    explicit_title_override=f"{codex_ledger.title} (Fork)",
                    recompute_agy_step_indices=True,
                )
                written_cid = self._transport.apply_to_agy(
                    binding=binding,
                    cascade_id=fork_cid,
                    events=transition.fork_events_to_apply,
                    full_ledger=fork_ledger,
                    lock_snapshot=locks,
                    mode=transition.selected_transport,
                    parent_cascade_id=lineage.parent_cascade_id,
                    source_tag=source_tag,
                    lineage=lineage,
                    forked_from_cascade_id=orig_cid,
                )
                refreshed_agy = self._transport.read_agy_ledger(
                    binding, written_cid, locks, transition.selected_transport
                )
                agy_wm = refreshed_agy.latest_watermark()
                codex_wm = codex_ledger.latest_watermark()
                assert agy_wm is not None and codex_wm is not None
                fork_cursor = SessionSyncCursor(
                    workspace_uri=binding.workspace_uri,
                    cascade_id=written_cid,
                    codex_thread_id=orig_tid,
                    last_synced_turn_id=codex_wm.turn_id,
                    last_synced_agy_step_idx=agy_wm.last_agy_step_index or StepIndex(0),
                    last_synced_codex_line_no=codex_wm.last_codex_line_no or RolloutLineNo(1),
                    agy_boundary_digest=agy_wm.boundary_digest,
                    codex_boundary_digest=codex_wm.boundary_digest,
                    updated_at_iso=_now_iso(),
                )
                self._save_cursor(fork_cursor)
                return SyncReceipt(
                    workspace=binding,
                    cascade_id=written_cid,
                    codex_thread_id=orig_tid,
                    transition_kind="diverged_forked",
                    transport_used=transition.selected_transport,
                    events_applied=len(transition.fork_events_to_apply),
                    updated_cursor=fork_cursor,
                    forked_from_cascade_id=orig_cid,
                    backup_manifest=backup,
                )

            assert agy_ledger is not None
            fork_tid = CodexThreadId(fork_id_str)
            backup_files = self._target_files_for_backup(HostKind.CODEX, orig_cid, fork_tid)
            backup = (
                self._guard.create_offline_backup(HostKind.CODEX, str(fork_tid), backup_files)
                if backup_files and transition.selected_transport == TransportMode.OFFLINE_STORAGE
                else None
            )
            if transition.selected_transport == TransportMode.OFFLINE_STORAGE:
                binding = self._registry.ensure_codex_binding(binding.workspace_path)
            fork_ledger = TrajectoryLedger.from_events(
                binding.workspace_path,
                binding.workspace_uri,
                transition.fork_events_to_apply,
                explicit_title_override=f"{agy_ledger.title} (Fork)",
            )
            written_tid = self._transport.apply_to_codex(
                binding=binding,
                thread_id=fork_tid,
                events=transition.fork_events_to_apply,
                full_ledger=fork_ledger,
                lock_snapshot=locks,
                mode=transition.selected_transport,
                parent_thread_id=lineage.parent_codex_thread_id,
                lineage=lineage,
            )
            refreshed_codex = self._transport.read_codex_ledger(
                binding, written_tid, locks, transition.selected_transport
            )
            agy_wm = agy_ledger.latest_watermark()
            codex_wm = refreshed_codex.latest_watermark()
            assert agy_wm is not None and codex_wm is not None
            fork_cursor = SessionSyncCursor(
                workspace_uri=binding.workspace_uri,
                cascade_id=orig_cid,
                codex_thread_id=written_tid,
                last_synced_turn_id=agy_wm.turn_id,
                last_synced_agy_step_idx=agy_wm.last_agy_step_index or StepIndex(0),
                last_synced_codex_line_no=codex_wm.last_codex_line_no or RolloutLineNo(1),
                agy_boundary_digest=agy_wm.boundary_digest,
                codex_boundary_digest=codex_wm.boundary_digest,
                updated_at_iso=_now_iso(),
            )
            self._save_cursor(fork_cursor)
            return SyncReceipt(
                workspace=binding,
                cascade_id=orig_cid,
                codex_thread_id=written_tid,
                transition_kind="diverged_forked",
                transport_used=transition.selected_transport,
                events_applied=len(transition.fork_events_to_apply),
                updated_cursor=fork_cursor,
                forked_from_codex_thread_id=orig_tid,
                backup_manifest=backup,
            )

        if transition.target_host == HostKind.ANTIGRAVITY:
            assert codex_ledger is not None
            backup_files = self._target_files_for_backup(HostKind.ANTIGRAVITY, orig_cid, orig_tid)
            backup = self._guard.create_offline_backup(HostKind.ANTIGRAVITY, str(orig_cid), backup_files)
            if transition.selected_transport == TransportMode.OFFLINE_STORAGE:
                binding = self._registry.ensure_agy_binding(binding.workspace_path)
            overwrite_for_agy = TrajectoryLedger.from_events(
                binding.workspace_path,
                binding.workspace_uri,
                transition.fork_events_to_apply,
                explicit_title_override=codex_ledger.title,
                recompute_agy_step_indices=True,
            )
            written_cid = self._transport.apply_to_agy(
                binding=binding,
                cascade_id=orig_cid,
                events=transition.fork_events_to_apply,
                full_ledger=overwrite_for_agy,
                lock_snapshot=locks,
                mode=transition.selected_transport,
                parent_cascade_id=lineage.parent_cascade_id,
                source_tag=source_tag,
                incremental=False,
                lineage=lineage,
            )
            refreshed_agy = self._transport.read_agy_ledger(
                binding, written_cid, locks, transition.selected_transport
            )
            agy_wm = refreshed_agy.latest_watermark()
            codex_wm = codex_ledger.latest_watermark()
            assert agy_wm is not None and codex_wm is not None
            ow_cursor = SessionSyncCursor(
                workspace_uri=binding.workspace_uri,
                cascade_id=written_cid,
                codex_thread_id=orig_tid,
                last_synced_turn_id=codex_wm.turn_id,
                last_synced_agy_step_idx=agy_wm.last_agy_step_index or StepIndex(0),
                last_synced_codex_line_no=codex_wm.last_codex_line_no or RolloutLineNo(1),
                agy_boundary_digest=agy_wm.boundary_digest,
                codex_boundary_digest=codex_wm.boundary_digest,
                updated_at_iso=_now_iso(),
            )
            self._save_cursor(ow_cursor)
            return SyncReceipt(
                workspace=binding,
                cascade_id=written_cid,
                codex_thread_id=orig_tid,
                transition_kind="diverged_overwritten",
                transport_used=transition.selected_transport,
                events_applied=len(transition.fork_events_to_apply),
                updated_cursor=ow_cursor,
                backup_manifest=backup,
            )

        assert agy_ledger is not None
        backup_files = self._target_files_for_backup(HostKind.CODEX, orig_cid, orig_tid)
        backup = self._guard.create_offline_backup(HostKind.CODEX, str(orig_tid), backup_files)
        if transition.selected_transport == TransportMode.OFFLINE_STORAGE:
            binding = self._registry.ensure_codex_binding(binding.workspace_path)
        overwrite_for_codex = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            transition.fork_events_to_apply,
            explicit_title_override=agy_ledger.title,
        )
        written_tid = self._transport.apply_to_codex(
            binding=binding,
            thread_id=orig_tid,
            events=transition.fork_events_to_apply,
            full_ledger=overwrite_for_codex,
            lock_snapshot=locks,
            mode=transition.selected_transport,
            parent_thread_id=lineage.parent_codex_thread_id,
            incremental=False,
            lineage=lineage,
        )
        refreshed_codex = self._transport.read_codex_ledger(
            binding, written_tid, locks, transition.selected_transport
        )
        agy_wm = agy_ledger.latest_watermark()
        codex_wm = refreshed_codex.latest_watermark()
        assert agy_wm is not None and codex_wm is not None
        ow_cursor = SessionSyncCursor(
            workspace_uri=binding.workspace_uri,
            cascade_id=orig_cid,
            codex_thread_id=written_tid,
            last_synced_turn_id=agy_wm.turn_id,
            last_synced_agy_step_idx=agy_wm.last_agy_step_index or StepIndex(0),
            last_synced_codex_line_no=codex_wm.last_codex_line_no or RolloutLineNo(1),
            agy_boundary_digest=agy_wm.boundary_digest,
            codex_boundary_digest=codex_wm.boundary_digest,
            updated_at_iso=_now_iso(),
        )
        self._save_cursor(ow_cursor)
        return SyncReceipt(
            workspace=binding,
            cascade_id=orig_cid,
            codex_thread_id=written_tid,
            transition_kind="diverged_overwritten",
            transport_used=transition.selected_transport,
            events_applied=len(transition.fork_events_to_apply),
            updated_cursor=ow_cursor,
            backup_manifest=backup,
        )

    def sync_workspace(
        self,
        workspace: str | Path,
        *,
        direction: SyncDirection = SyncDirection.AUTO_INCREMENTAL,
        mode: TransportMode = TransportMode.AUTO,
        fork_policy: ForkResolutionPolicy = ForkResolutionPolicy.HALT_ON_DIVERGENCE,
        include_subagents: bool = False,
    ) -> Tuple[SyncReceipt, ...]:
        """Discover and synchronize all sessions in `workspace`."""
        binding = self._registry.resolve_readonly_binding(workspace)
        sessions = self._registry.list_workspace_sessions(
            binding,
            include_subagents=include_subagents,
        )
        receipts: list[SyncReceipt] = []
        for desc in sessions:
            if direction == SyncDirection.AGY_TO_CODEX and desc.cascade_id is None:
                continue
            if direction == SyncDirection.CODEX_TO_AGY and desc.codex_thread_id is None:
                continue
            rec = self.sync_session(
                binding.workspace_path,
                cascade_id=desc.cascade_id,
                codex_thread_id=desc.codex_thread_id,
                direction=direction,
                mode=mode,
                fork_policy=fork_policy,
            )
            receipts.append(rec)
            if direction == SyncDirection.AUTO_INCREMENTAL and rec.transition_kind == "diverged_forked":
                if rec.forked_from_codex_thread_id is not None:
                    receipts.append(
                        self.sync_session(
                            binding.workspace_path,
                            codex_thread_id=rec.forked_from_codex_thread_id,
                            direction=SyncDirection.CODEX_TO_AGY,
                            mode=mode,
                            fork_policy=fork_policy,
                        )
                    )
                if rec.forked_from_cascade_id is not None:
                    receipts.append(
                        self.sync_session(
                            binding.workspace_path,
                            cascade_id=rec.forked_from_cascade_id,
                            direction=SyncDirection.AGY_TO_CODEX,
                            mode=mode,
                            fork_policy=fork_policy,
                        )
                    )
        return tuple(receipts)
