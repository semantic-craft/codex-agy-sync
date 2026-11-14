"""Bidirectional workspace-to-project pairing registry, diagnosis, and session discovery."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping, Sequence, Tuple

from ..events import SubagentLineage
from ..workspace import (
    FORBIDDEN_AGY_PROJECT_IDS,
    AgyProjectId,
    CascadeId,
    CodexLegacyProjectId,
    CodexProjectId,
    CodexThreadId,
    HostKind,
    WorkspacePath,
    WorkspaceRootUri,
    parse_workspace_identity,
)
from .paths_and_locks import (
    HostEnvironmentPaths,
    PresenceWalGuard,
    SyncBusyLockError,
    atomic_write_text,
    deterministic_uuid,
    expand_existing_with_sidecars,
    open_readonly_sqlite,
)
from .store_catalog import (
    build_combined_lineage,
    discover_agy_subagent_sidecars,
    enrich_agy_session_from_conv_db,
    ensure_agy_conversation_db_schema,
    ensure_agy_summaries_db_schema,
    ensure_codex_state_db_schema,
    extract_agy_project_folder_uris as _extract_agy_project_folder_uris,
    safe_valid_session_id,
    scan_agy_summaries_db,
    scan_codex_state_sessions,
    uris_match_workspace as _uris_match_workspace,
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


@dataclass(frozen=True)
class WorkspaceBindingDiagnosis:
    """Read-only report of Antigravity/Codex project pairing and live daemon status."""

    workspace_path: WorkspacePath
    workspace_uri: WorkspaceRootUri
    workspace_name: str
    agy_registered: bool
    codex_registered: bool
    agy_project_id: str | None
    codex_project_id: str | None
    agy_daemon_active: bool
    codex_daemon_active: bool
    remediation_steps: tuple[str, ...]

    def is_paired(self) -> bool:
        return self.agy_registered and self.codex_registered

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_path": str(self.workspace_path),
            "workspace_uri": str(self.workspace_uri),
            "workspace_name": self.workspace_name,
            "agy_registered": self.agy_registered,
            "codex_registered": self.codex_registered,
            "agy_project_id": self.agy_project_id,
            "codex_project_id": self.codex_project_id,
            "agy_daemon_active": self.agy_daemon_active,
            "codex_daemon_active": self.codex_daemon_active,
            "remediation_steps": list(self.remediation_steps),
        }


class UnpairedWorkspaceError(ValueError):
    """Raised when `handoff` requires bidirectional pairing that has not yet completed."""

    def __init__(self, diagnosis: WorkspaceBindingDiagnosis, message: str | None = None) -> None:
        self.diagnosis = diagnosis
        if message is None:
            missing = [
                label
                for flag, label in ((diagnosis.agy_registered, "Antigravity"), (diagnosis.codex_registered, "Codex"))
                if not flag
            ]
            missing_desc = " and ".join(missing) if missing else "both hosts"
            steps = "; ".join(diagnosis.remediation_steps)
            message = f"Workspace {str(diagnosis.workspace_path)!r} is not paired for {missing_desc}. {steps}".strip()
        super().__init__(message)

    def to_error_payload(self) -> dict[str, Any]:
        return {
            "error": "unpaired_workspace",
            "message": str(self),
            "diagnosis": self.diagnosis.to_dict(),
            "remediation": list(self.diagnosis.remediation_steps),
        }


class ProjectRegistry:
    """Owns bidirectional workspace-to-project resolution and session discovery."""

    def __init__(self, paths: HostEnvironmentPaths) -> None:
        self._paths = paths

    @staticmethod
    def extract_agy_project_folder_uris(data: Mapping[str, Any]) -> list[str]:
        return _extract_agy_project_folder_uris(data)

    @staticmethod
    def uris_match_workspace(raw_uris: Sequence[str], target_uri: WorkspaceRootUri) -> bool:
        return _uris_match_workspace(raw_uris, target_uri)

    def find_readonly_agy_project_id(self, ws_uri: WorkspaceRootUri) -> AgyProjectId | None:
        projects_dir = self._paths.gemini_config_dir / "projects"
        if not projects_dir.is_dir():
            return None
        for pfile in sorted(projects_dir.glob("*.json")):
            try:
                data = json.loads(pfile.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            cand_id = str(data.get("id") or pfile.stem).strip()
            if not cand_id or cand_id in FORBIDDEN_AGY_PROJECT_IDS:
                continue
            if _uris_match_workspace(_extract_agy_project_folder_uris(data), ws_uri):
                return AgyProjectId(cand_id)
        return None

    def _ensure_agy_project(self, ws_path: WorkspacePath, ws_uri: WorkspaceRootUri, ws_name: str) -> AgyProjectId:
        _ = ws_path
        existing = self.find_readonly_agy_project_id(ws_uri)
        if existing is not None:
            return existing
        projects_dir = self._paths.gemini_config_dir / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        new_pid = deterministic_uuid("agy-project", str(ws_uri))
        project_doc = {
            "id": new_pid,
            "name": ws_name,
            "projectResources": {"resources": [{"folderUri": str(ws_uri)}]},
            "permissionGrants": {"v2Migrated": True},
            "settings": {},
            "isWorkspaceOnly": False,
        }
        atomic_write_text(projects_dir / f"{new_pid}.json", json.dumps(project_doc, ensure_ascii=False, indent=2) + "\n")
        return AgyProjectId(new_pid)

    def find_readonly_codex_project_id(self, ws_path: WorkspacePath) -> str | None:
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        if not state_db.is_file():
            return None
        try:
            conn = open_readonly_sqlite(state_db)
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
        existing_pid = self.find_readonly_codex_project_id(ws_path)
        global_state_path, gs_data = self._load_codex_global_state()
        legacy_pid = self._find_legacy_codex_project_id(gs_data, ws_path) or deterministic_uuid(
            "codex-legacy-project", str(ws_uri)
        )

        if not existing_pid:
            existing_pid = deterministic_uuid("codex-project", str(ws_uri))
            conn = sqlite3.connect(str(state_db), timeout=10.0)
            try:
                ensure_codex_state_db_schema(conn)
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

        local_projects = gs_data.setdefault("local-projects", {})
        if not isinstance(local_projects, dict):
            local_projects = {}
            gs_data["local-projects"] = local_projects
        local_projects[legacy_pid] = {
            "id": legacy_pid,
            "name": ws_name,
            "rootPaths": [ws_str],
            "createdAt": local_projects.get(legacy_pid, {}).get("createdAt", now_ms),
            "updatedAt": now_ms,
        }
        host_map = gs_data.setdefault("app-server-project-id-by-legacy-project-id-by-host", {})
        if not isinstance(host_map, dict):
            host_map = {}
            gs_data["app-server-project-id-by-legacy-project-id-by-host"] = host_map
        host_key = f"local:{self._paths.codex_home_dir}"
        if not isinstance(host_map.get(host_key), dict):
            host_map[host_key] = {}
        host_map[host_key][legacy_pid] = existing_pid

        project_order = gs_data.setdefault("project-order", [])
        if not isinstance(project_order, list):
            project_order = []
            gs_data["project-order"] = project_order
        if legacy_pid not in project_order:
            project_order.append(legacy_pid)

        atomic_write_text(global_state_path, json.dumps(gs_data, ensure_ascii=False, indent=2) + "\n")
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
        agy_pid = self.find_readonly_agy_project_id(ws_uri) or AgyProjectId(deterministic_uuid("agy-project", str(ws_uri)))
        codex_pid_str = self.find_readonly_codex_project_id(ws_path) or deterministic_uuid("codex-project", str(ws_uri))
        _, gs_data = self._load_codex_global_state()
        legacy_pid_str = self._find_legacy_codex_project_id(gs_data, ws_path) or deterministic_uuid(
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

    def pair_workspace(
        self,
        workspace: str | Path,
        *,
        guard: PresenceWalGuard | None = None,
        prefer_online_rpc: bool = True,
    ) -> WorkspaceProjectBinding:
        eff_guard = guard if guard is not None else PresenceWalGuard(self._paths)
        return guarded_pair_workspace(self._paths, eff_guard, self, workspace, prefer_online_rpc=prefer_online_rpc)

    def load_sorted_workspace_cursor_pairs(self, workspace_uri: WorkspaceRootUri) -> list[tuple[str, str]]:
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
            cid_str = safe_valid_session_id(raw.get("cascade_id"), "cascade_id")
            tid_str = safe_valid_session_id(raw.get("codex_thread_id"), "codex_thread_id")
            if cid_str and tid_str:
                records.append(((str(raw.get("updated_at_iso") or ""), cf.name), cid_str, tid_str))
        records.sort(key=lambda item: item[0], reverse=True)
        return [(cid_str, tid_str) for _, cid_str, tid_str in records]

    def list_workspace_sessions(
        self, binding: WorkspaceProjectBinding, *, include_subagents: bool = False,
    ) -> Tuple[PairedSessionDescriptor, ...]:
        sidecars = discover_agy_subagent_sidecars(self._paths)
        agy_sessions = scan_agy_summaries_db(self._paths, binding.agy_project_id, binding.workspace_uri, sidecars)
        conv_dir = self._paths.agy_data_dir / "conversations"
        if conv_dir.is_dir():
            for db_file in sorted(conv_dir.glob("*.db")):
                enrich_agy_session_from_conv_db(
                    db_file, binding.agy_project_id, binding.workspace_uri, sidecars, agy_sessions
                )

        codex_sessions = scan_codex_state_sessions(self._paths, binding.codex_project_id, binding.workspace_path)
        descriptors: list[PairedSessionDescriptor] = []
        paired_agy: set[str] = set()
        paired_codex: set[str] = set()

        for cid_str, tid_str in self.load_sorted_workspace_cursor_pairs(binding.workspace_uri):
            if cid_str in paired_agy or tid_str in paired_codex:
                continue
            paired_agy.add(cid_str)
            paired_codex.add(tid_str)
            agy_info, codex_info = agy_sessions.get(cid_str, {}), codex_sessions.get(tid_str, {})
            lineage = build_combined_lineage(agy_info, codex_info)
            if include_subagents or not lineage.is_subagent:
                descriptors.append(PairedSessionDescriptor(
                    binding,
                    CascadeId(cid_str),
                    CodexThreadId(tid_str),
                    str(agy_info.get("title") or codex_info.get("title") or "Synced Session"),
                    int(agy_info.get("step_count", 0)),
                    int(codex_info.get("turn_count", 0)),
                    lineage.parent_cascade_id,
                    lineage.parent_codex_thread_id,
                    lineage,
                ))

        for cid_str, ainfo in agy_sessions.items():
            if cid_str not in paired_agy:
                lineage = build_combined_lineage(ainfo, {})
                if include_subagents or not lineage.is_subagent:
                    descriptors.append(PairedSessionDescriptor(
                        binding, ainfo["cascade_id"], None, ainfo["title"], ainfo["step_count"], 0,
                        parent_cascade_id=lineage.parent_cascade_id, lineage=lineage,
                    ))

        for tid_str, cinfo in codex_sessions.items():
            if tid_str not in paired_codex:
                lineage = build_combined_lineage({}, cinfo)
                if include_subagents or not lineage.is_subagent:
                    descriptors.append(PairedSessionDescriptor(
                        binding, None, cinfo["codex_thread_id"], cinfo["title"], 0, cinfo["turn_count"],
                        parent_codex_thread_id=lineage.parent_codex_thread_id, lineage=lineage,
                    ))

        return tuple(descriptors)


def diagnose_workspace_binding(paths: HostEnvironmentPaths, workspace: str | Path) -> WorkspaceBindingDiagnosis:
    """Inspect read-only workspace registrations and daemon presence without mutating host files."""
    ws_path, ws_uri = parse_workspace_identity(workspace)
    registry = ProjectRegistry(paths)
    guard = PresenceWalGuard(paths)
    agy_pid = registry.find_readonly_agy_project_id(ws_uri)
    codex_pid = registry.find_readonly_codex_project_id(ws_path)
    agy_active = guard.discover_agy_daemon() is not None
    codex_active = guard.discover_codex_daemon() is not None
    steps: list[str] = []
    if agy_pid is None and agy_active:
        steps.append(f'Open "{ws_path}" in Antigravity Desktop so it registers natively, or quit Antigravity Desktop before offline pairing.')
    if codex_pid is None and codex_active:
        steps.append(f'Open "{ws_path}" in Codex Desktop so it registers natively, or quit Codex Desktop before offline pairing.')
    if agy_pid is None or codex_pid is None:
        steps.append(f'Retry handoff with auto-pairing when missing host is offline: codex-agy-sync handoff -w "{ws_path}" --pair-if-offline')
        steps.append(f'Or pair explicitly offline: codex-agy-sync pair -w "{ws_path}"')
    return WorkspaceBindingDiagnosis(
        ws_path, ws_uri, ws_path.name or "workspace",
        agy_pid is not None, codex_pid is not None,
        str(agy_pid) if agy_pid is not None else None, codex_pid,
        agy_active, codex_active, tuple(steps),
    )


def _busy_pair_error(host: str, ws_str: str) -> SyncBusyLockError:
    if host == "sync":
        return SyncBusyLockError(
            "Another sync or pairing operation currently holds sync.lock; wait and retry.",
            host="sync",
            remediation=(f'Wait for running sync or pair processes to finish, then retry: codex-agy-sync pair -w "{ws_str}"',),
        )
    app_label = "Antigravity" if host == "antigravity" else "Codex"
    return SyncBusyLockError(
        f"Cannot register {app_label} project while {app_label} Desktop or WAL lock is active. "
        f"Open {ws_str!r} in {app_label} Desktop so it registers natively, or quit {app_label} Desktop and retry pairing.",
        host=host,
        remediation=(
            f'Open "{ws_str}" in {app_label} Desktop so it registers natively.',
            f'Or quit running Desktop apps and run: codex-agy-sync pair -w "{ws_str}"',
        ),
    )


def _classify_busy_pair_error(
    guard: PresenceWalGuard, ws_str: str, *, need_agy: bool, need_codex: bool,
) -> SyncBusyLockError:
    locks = guard.probe_locks(None, None)
    if need_codex and not locks.can_write_codex_offline():
        return _busy_pair_error("codex", ws_str)
    if need_agy and not locks.can_write_agy_offline():
        return _busy_pair_error("antigravity", ws_str)
    return _busy_pair_error("sync", ws_str)


def _pair_agy_only_offline(
    paths: HostEnvironmentPaths,
    guard: PresenceWalGuard,
    registry: ProjectRegistry,
    ro_binding: WorkspaceProjectBinding,
    workspace: str | Path,
) -> WorkspaceProjectBinding:
    ws_str = str(ro_binding.workspace_path)
    try:
        with guard.acquire_offline_write_locks(HostKind.ANTIGRAVITY, None, None):
            candidates = [paths.gemini_config_dir / "projects" / f"{ro_binding.agy_project_id}.json"]
            backup = guard.create_offline_backup(
                HostKind.ANTIGRAVITY,
                str(ro_binding.agy_project_id),
                expand_existing_with_sidecars(candidates),
                expected_new_files=candidates,
            )
            try:
                return registry.ensure_agy_binding(workspace)
            except Exception:
                backup.restore()
                raise
    except BlockingIOError as exc:
        raise _classify_busy_pair_error(guard, ws_str, need_agy=True, need_codex=False) from exc


def guarded_pair_workspace(
    paths: HostEnvironmentPaths,
    guard: PresenceWalGuard,
    registry: ProjectRegistry,
    workspace: str | Path,
    *,
    prefer_online_rpc: bool = True,
) -> WorkspaceProjectBinding:
    ro_binding = registry.resolve_readonly_binding(workspace)
    agy_registered = registry.find_readonly_agy_project_id(ro_binding.workspace_uri) is not None
    codex_registered = registry.find_readonly_codex_project_id(ro_binding.workspace_path) is not None
    if agy_registered and codex_registered:
        return ro_binding
    if codex_registered and not agy_registered:
        return _pair_agy_only_offline(paths, guard, registry, ro_binding, workspace)

    ws_str = str(ro_binding.workspace_path)
    try:
        with guard.acquire_offline_write_locks(HostKind.CODEX, None, None) as locks:
            if not agy_registered and not locks.can_write_agy_offline():
                raise _busy_pair_error("antigravity", ws_str)
            codex_files = [
                paths.codex_home_dir / "state_5.sqlite",
                paths.codex_home_dir / ".codex-global-state.json",
            ]
            candidates = codex_files if agy_registered else [
                paths.gemini_config_dir / "projects" / f"{ro_binding.agy_project_id}.json",
                *codex_files,
            ]
            backup = guard.create_offline_backup(
                HostKind.CODEX,
                str(ro_binding.codex_project_id),
                expand_existing_with_sidecars(candidates),
                expected_new_files=candidates,
            )
            try:
                if agy_registered:
                    return registry.ensure_codex_binding(workspace)
                return registry.ensure_workspace_binding(workspace, prefer_online_rpc=prefer_online_rpc)
            except Exception:
                backup.restore()
                raise
    except BlockingIOError as exc:
        raise _classify_busy_pair_error(guard, ws_str, need_agy=not agy_registered, need_codex=True) from exc
