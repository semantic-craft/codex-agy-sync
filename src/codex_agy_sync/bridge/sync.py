"""Deep `SyncBridge` facade with unified transactional write execution, companion self-heal, and symmetric fork."""

from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Literal, Sequence, Tuple
import uuid

from ..ledger import (
    AgyGuiSafeSource, CanonicalEvent, CascadeId, CodexThreadId, CompletedTurnWatermark,
    ContentDigest, HostKind, RolloutLineNo, StepIndex, SubagentLineage,
    TrajectoryLedger, TurnId, WorkspaceRootUri, validate_session_id,
)
from .paths_and_locks import (
    BackupManifest, HostEnvironmentPaths, HostLockSnapshot, PresenceWalGuard, TransportMode,
    _atomic_write_text, _deterministic_uuid, _now_iso, _open_readonly_sqlite, _validated_child_path,
)
from .registry import (
    ProjectRegistry, WorkspaceProjectBinding,
    _parse_agy_trajectory_metadata_lineage, _parse_codex_thread_source_lineage, _safe_valid_session_id,
)
from .rescue_and_fork import (
    compute_codex_nesting_depth, expand_existing_with_sidecars,
    guarded_pair_workspace, is_source_actively_running, reconcile_and_rescue_ledgers,
)
from .rpc import ProbedSyncLedgers, probe_sync_ledgers
from .state_machine import (
    CursorForkStateMachine, DeferredBusyTransition, DivergedConflictTransition, EmptySessionError,
    ForkResolutionPolicy, IncrementalAppendTransition, InitialImportTransition, NoOpTransition,
    SessionSyncCursor, SyncDirection, _NULL_CID, _NULL_TID, generate_uuid7,
)
from .transport import DefaultHostTransport, HostTransportPort

_TransitionReceiptKind = Literal[
    "no_op", "initial_import", "incremental_append",
    "diverged_halted", "diverged_forked", "diverged_overwritten", "deferred_busy",
]
_WriteReceiptKind = Literal["initial_import", "incremental_append", "diverged_forked", "diverged_overwritten"]


@dataclass(frozen=True)
class SyncReceipt:
    """Auditable outcome returned by `SyncBridge.sync_session`."""

    workspace: WorkspaceProjectBinding
    cascade_id: CascadeId
    codex_thread_id: CodexThreadId
    transition_kind: _TransitionReceiptKind
    transport_used: TransportMode
    events_applied: int
    updated_cursor: SessionSyncCursor | None
    forked_from_cascade_id: CascadeId | None = None
    forked_from_codex_thread_id: CodexThreadId | None = None
    backup_manifest: BackupManifest | None = None
    peer_fork_cascade_id: CascadeId | None = None
    peer_fork_codex_thread_id: CodexThreadId | None = None


@dataclass(frozen=True)
class _WritePlan:
    target_host: HostKind
    target_cid: CascadeId
    target_tid: CodexThreadId
    events: Tuple[CanonicalEvent, ...]
    full_ledger: TrajectoryLedger
    source_ledger: TrajectoryLedger
    selected_transport: TransportMode
    receipt_transport: TransportMode
    transition_kind: _WriteReceiptKind
    lineage: SubagentLineage
    source_tag: AgyGuiSafeSource
    incremental: bool = False
    was_source_rescued: bool = False
    forked_from_cascade_id: CascadeId | None = None
    forked_from_codex_thread_id: CodexThreadId | None = None
    agy_turn_inflight: bool = False
    codex_turn_inflight: bool = False


def _readonly_fetchone(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...] | None:
    if not db_path.is_file():
        return None
    try:
        with contextlib.closing(_open_readonly_sqlite(db_path)) as conn:
            row = conn.execute(sql, params).fetchone()
        return tuple(row) if row is not None else None
    except sqlite3.OperationalError:
        return None


def _make_write_plan(
    binding: WorkspaceProjectBinding, target_host: HostKind, target_cid: CascadeId, target_tid: CodexThreadId,
    events: Sequence[CanonicalEvent], full_events: Sequence[CanonicalEvent], source_ledger: TrajectoryLedger,
    title: str, selected_transport: TransportMode, transition_kind: _WriteReceiptKind,
    lineage: SubagentLineage, source_tag: AgyGuiSafeSource, *,
    receipt_transport: TransportMode | None = None,
    incremental: bool = False, was_source_rescued: bool = False,
    forked_from_cascade_id: CascadeId | None = None, forked_from_codex_thread_id: CodexThreadId | None = None,
    agy_turn_inflight: bool = False, codex_turn_inflight: bool = False,
) -> _WritePlan:
    full_ledger = TrajectoryLedger.from_events(
        binding.workspace_path, binding.workspace_uri, full_events,
        explicit_title_override=title, recompute_agy_step_indices=(target_host == HostKind.ANTIGRAVITY),
    )
    eff_receipt_tr = receipt_transport if receipt_transport is not None else selected_transport
    return _WritePlan(
        target_host, target_cid, target_tid, tuple(events), full_ledger, source_ledger,
        selected_transport, eff_receipt_tr, transition_kind, lineage, source_tag, incremental,
        was_source_rescued, forked_from_cascade_id, forked_from_codex_thread_id,
        agy_turn_inflight, codex_turn_inflight,
    )


class SyncBridge:
    """Deep public facade for `codex-agy-sync`."""

    def __init__(self, paths: HostEnvironmentPaths | None = None, transport: HostTransportPort | None = None) -> None:
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
            self._paths.sync_state_dir, "cursors", f"{cascade_id}__{codex_thread_id}.json", label="cursor_path"
        )

    def _load_cursor(
        self, workspace_uri: WorkspaceRootUri, cascade_id: CascadeId | None, codex_thread_id: CodexThreadId | None,
    ) -> SessionSyncCursor | None:
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")
        cursors_dir = self._paths.sync_state_dir / "cursors"
        if not cursors_dir.is_dir() or (cascade_id is None and codex_thread_id is None):
            return None
        matches: list[tuple[tuple[str, str], SessionSyncCursor]] = []
        for cf in cursors_dir.glob("*.json"):
            try:
                raw = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("workspace_uri") != str(workspace_uri):
                continue
            raw_cid = _safe_valid_session_id(raw.get("cascade_id"), "cascade_id")
            raw_tid = _safe_valid_session_id(raw.get("codex_thread_id"), "codex_thread_id")
            if not (raw_cid and raw_tid):
                continue
            if (cascade_id is not None and raw_cid != str(cascade_id)) or (codex_thread_id is not None and raw_tid != str(codex_thread_id)):
                continue
            try:
                cur_obj = SessionSyncCursor(
                    WorkspaceRootUri(str(raw["workspace_uri"])), CascadeId(raw_cid), CodexThreadId(raw_tid),
                    TurnId(str(raw["last_synced_turn_id"])), StepIndex(int(raw["last_synced_agy_step_idx"])),
                    RolloutLineNo(int(raw["last_synced_codex_line_no"])), ContentDigest(str(raw["agy_boundary_digest"])),
                    ContentDigest(str(raw["codex_boundary_digest"])), str(raw.get("updated_at_iso") or ""),
                    bool(raw.get("rescued_inflight_turn", False)), str(raw.get("pre_rescue_source_host") or ""),
                )
            except Exception:
                continue
            matches.append(((cur_obj.updated_at_iso, cf.name), cur_obj))
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1] if matches else None

    def _save_cursor(self, cursor: SessionSyncCursor) -> None:
        cpath = self._cursor_path(cursor.cascade_id, cursor.codex_thread_id)
        _atomic_write_text(cpath, json.dumps(dataclasses.asdict(cursor), ensure_ascii=False, indent=2) + "\n")
        cursors_dir = self._paths.sync_state_dir / "cursors"
        ts_compact = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for cf in list(cursors_dir.glob("*.json")) if cursors_dir.is_dir() else ():
            if cf == cpath:
                continue
            try:
                raw = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(raw, dict) and raw.get("workspace_uri") == str(cursor.workspace_uri) and (
                raw.get("cascade_id") == str(cursor.cascade_id) or raw.get("codex_thread_id") == str(cursor.codex_thread_id)
            ):
                superseded_dir = cursors_dir / "superseded"
                superseded_dir.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    os.replace(cf, superseded_dir / f"{ts_compact}_{cf.name}")

    def _candidate_target_paths(
        self, target_host: HostKind, cascade_id: CascadeId | None, codex_thread_id: CodexThreadId | None,
        *, binding: WorkspaceProjectBinding | None = None, lineage: SubagentLineage | None = None,
    ) -> tuple[list[Path], list[tuple[Path, str]]]:
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")
        if target_host == HostKind.ANTIGRAVITY:
            files = [self._paths.agy_data_dir / "conversation_summaries.db", self._paths.agy_data_dir / "agyhub_summaries_proto.pb"]
            if binding is not None:
                files.append(self._paths.gemini_config_dir / "projects" / f"{binding.agy_project_id}.json")
            if cascade_id is not None:
                files.extend(
                    _validated_child_path(rdir, "brain", str(cascade_id), ".system_generated", "logs", "transcript.jsonl", label="cascade_id")
                    for rdir in (self._paths.agy_data_dir, self._paths.agy_cli_data_dir) if rdir is not None
                )
                files.extend([
                    _validated_child_path(self._paths.agy_data_dir, "conversations", f"{cascade_id}.db", label="cascade_id"),
                    _validated_child_path(self._paths.agy_data_dir, "annotations", f"{cascade_id}.pbtxt", label="cascade_id"),
                ])
                if lineage is not None and lineage.is_subagent and lineage.parent_cascade_id is not None:
                    files.append(_validated_child_path(
                        self._paths.agy_data_dir, "brain", str(lineage.parent_cascade_id), ".system_generated", "subagents", f"{cascade_id}.json", label="cascade_id"
                    ))
            return files, []
        files = [
            self._paths.codex_home_dir / "state_5.sqlite",
            self._paths.codex_home_dir / ".codex-global-state.json",
            self._paths.codex_home_dir / "session_index.jsonl",
        ]
        if codex_thread_id is None:
            return files, []
        for sub in ("sessions", "archived_sessions"):
            sroot = self._paths.codex_home_dir / sub
            if sroot.is_dir():
                resolved_sroot = sroot.resolve(strict=False)
                for pat in (f"*{codex_thread_id}.jsonl", f"*{codex_thread_id}.jsonl.zst"):
                    files.extend(m for m in sroot.rglob(pat) if m.resolve(strict=False).is_relative_to(resolved_sroot))
        return files, [
            (self._paths.codex_home_dir / "sessions", f"*{codex_thread_id}.jsonl"),
            (self._paths.codex_home_dir / "sessions", f"*{codex_thread_id}.jsonl.zst"),
        ]

    @staticmethod
    def _expand_existing_with_sidecars(candidates: Sequence[Path]) -> list[Path]:
        return expand_existing_with_sidecars(candidates)

    def _target_files_for_backup(
        self, target_host: HostKind, cascade_id: CascadeId | None, codex_thread_id: CodexThreadId | None,
    ) -> list[Path]:
        return expand_existing_with_sidecars(self._candidate_target_paths(target_host, cascade_id, codex_thread_id)[0])

    def pair_workspace(self, workspace: str | Path, *, prefer_online_rpc: bool = True) -> WorkspaceProjectBinding:
        """Ensure both Antigravity and Codex have paired, sidebar-visible project entries for `workspace`."""
        return guarded_pair_workspace(self._paths, self._guard, self._registry, workspace, prefer_online_rpc=prefer_online_rpc)

    def inspect_session_ledger(
        self, workspace: str | Path, *, cascade_id: CascadeId | None = None,
        codex_thread_id: CodexThreadId | None = None, mode: TransportMode = TransportMode.AUTO,
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

    def _read_agy_lineage_fields(self, cid_key: str) -> tuple[str, str, int, str, str, StepIndex | None, bool]:
        sc_info = self._registry._discover_agy_subagent_sidecars().get(cid_key, {})
        parent_cid, root_cid, depth = str(sc_info.get("parent_cascade_id") or "").strip(), "", 0
        type_name, role = str(sc_info.get("agent_type_name") or "").strip(), str(sc_info.get("agent_role") or "").strip()
        spawn_idx: StepIndex | None = sc_info.get("spawn_step_index")
        sum_row = _readonly_fetchone(
            self._paths.agy_data_dir / "conversation_summaries.db",
            "SELECT agent_name, parent_conversation_id, nesting_depth FROM conversation_summaries WHERE conversation_id = ?",
            (cid_key,),
        )
        if sum_row:
            role, parent_cid = (role or str(sum_row[0] or "").strip()), (parent_cid or str(sum_row[1] or "").strip())
            depth = max(depth, int(sum_row[2] or 0))  # type: ignore[arg-type]
        tblob = _readonly_fetchone(
            _validated_child_path(self._paths.agy_data_dir, "conversations", f"{cid_key}.db", label="cascade_id"),
            "SELECT data FROM trajectory_metadata_blob WHERE id = 'main'",
        )
        if tblob and tblob[0]:
            meta = _parse_agy_trajectory_metadata_lineage(bytes(tblob[0]))  # type: ignore[arg-type]
            parent_cid = parent_cid or str(meta["parent_conversation_id"] or "")
            root_cid = root_cid or str(meta["root_conversation_id"] or "")
            type_name, role = (type_name or str(meta["agent_type_name"] or "")), (role or str(meta["agent_role"] or ""))
            depth = max(depth, int(meta["nesting_depth"] or 0))
        return parent_cid, root_cid, depth, type_name, role, spawn_idx, bool(sc_info)

    def _read_codex_lineage_fields(self, tid_key: str) -> tuple[str, int, str, str]:
        row = _readonly_fetchone(
            self._paths.codex_home_dir / "state_5.sqlite",
            "SELECT attached_thread_parent_id, agent_role, agent_nickname, source FROM threads WHERE id = ?",
            (tid_key,),
        )
        if not row:
            return "", 0, "", ""
        raw_src = str(row[3]) if row[3] is not None else None
        src_is_sub, src_parent_tid, src_depth, src_type, src_nick = _parse_codex_thread_source_lineage(raw_src)
        c_parent, c_type, c_nick = (str(row[0] or "").strip() or src_parent_tid), (str(row[1] or "").strip() or src_type), (str(row[2] or "").strip() or src_nick)
        c_depth = compute_codex_nesting_depth(raw_src, src_is_sub, src_depth, c_parent, c_type, c_nick)
        return (c_parent if c_depth > 0 else ""), c_depth, c_type, c_nick

    def _resolve_session_lineage(
        self, binding: WorkspaceProjectBinding, cascade_id: CascadeId | None, codex_thread_id: CodexThreadId | None,
    ) -> SubagentLineage:
        p_cid, r_cid, agy_depth, t_name, role, spawn_idx, has_sc = (
            self._read_agy_lineage_fields(validate_session_id(str(cascade_id), label="cascade_id"))
            if cascade_id is not None else ("", "", 0, "", "", None, False)
        )
        p_tid, codex_depth, c_type, c_role = (
            self._read_codex_lineage_fields(validate_session_id(str(codex_thread_id), label="codex_thread_id"))
            if codex_thread_id is not None else ("", 0, "", "")
        )
        p_cid, r_cid, p_tid = _safe_valid_session_id(p_cid, "p_cid"), _safe_valid_session_id(r_cid, "r_cid"), _safe_valid_session_id(p_tid, "p_tid")
        depth = max(agy_depth, codex_depth, 1 if (has_sc or spawn_idx is not None) else 0)
        if depth > 0 or spawn_idx is not None:
            if p_cid and not p_tid:
                cur = self._load_cursor(binding.workspace_uri, CascadeId(p_cid), None)
                p_tid = str(cur.codex_thread_id) if cur else _deterministic_uuid("paired-codex-thread", p_cid)
            elif p_tid and not p_cid:
                cur = self._load_cursor(binding.workspace_uri, None, CodexThreadId(p_tid))
                p_cid = str(cur.cascade_id) if cur else _deterministic_uuid("paired-cascade", p_tid)
            r_cid = r_cid or p_cid
        return SubagentLineage(
            CascadeId(p_cid) if p_cid else None, CascadeId(r_cid) if r_cid else None,
            CodexThreadId(p_tid) if p_tid else None, depth, t_name or c_type, role or c_role, spawn_idx,
        )

    @staticmethod
    def _is_source_actively_running(
        host: HostKind, ledger: TrajectoryLedger | None, locks: HostLockSnapshot, *, rpc_active: bool = False,
    ) -> bool:
        return is_source_actively_running(host, ledger, locks, rpc_active=rpc_active)

    def _unpaired_det_cid(self, ws_uri: WorkspaceRootUri, tid: CodexThreadId) -> CascadeId | None:
        det = CascadeId(_deterministic_uuid("paired-cascade", str(tid)))
        return det if self._load_cursor(ws_uri, det, None) is None else None

    def _unpaired_det_tid(self, ws_uri: WorkspaceRootUri, cid: CascadeId) -> CodexThreadId | None:
        det = CodexThreadId(_deterministic_uuid("paired-codex-thread", str(cid)))
        return det if self._load_cursor(ws_uri, None, det) is None else None

    def _probe_and_read_ledgers(
        self, binding: WorkspaceProjectBinding, cursor: SessionSyncCursor | None,
        cascade_id: CascadeId | None, codex_thread_id: CodexThreadId | None,
        direction: SyncDirection, mode: TransportMode,
    ) -> ProbedSyncLedgers:
        probe_cid = cascade_id or (self._unpaired_det_cid(binding.workspace_uri, codex_thread_id) if codex_thread_id else None)
        probe_tid = codex_thread_id or (self._unpaired_det_tid(binding.workspace_uri, cascade_id) if cascade_id else None)
        return probe_sync_ledgers(
            self._transport, self._guard, binding, cursor, cascade_id, codex_thread_id,
            direction, mode, probe_cid=probe_cid, probe_tid=probe_tid,
        )

    def _apply_and_verify_target(
        self, binding: WorkspaceProjectBinding, held_locks: HostLockSnapshot, plan: _WritePlan,
    ) -> tuple[WorkspaceProjectBinding, CascadeId, CodexThreadId, CompletedTurnWatermark, CompletedTurnWatermark]:
        if plan.target_host == HostKind.ANTIGRAVITY:
            if plan.selected_transport == TransportMode.OFFLINE_STORAGE:
                binding = self._registry.ensure_agy_binding(binding.workspace_path)
            written_cid = self._transport.apply_to_agy(
                binding=binding, cascade_id=plan.target_cid, events=plan.events, full_ledger=plan.full_ledger,
                lock_snapshot=held_locks, mode=plan.selected_transport, parent_cascade_id=plan.lineage.parent_cascade_id,
                source_tag=plan.source_tag, incremental=plan.incremental, lineage=plan.lineage,
                forked_from_cascade_id=plan.forked_from_cascade_id,
            )
            agy_wm = self._transport.read_agy_ledger(binding, written_cid, held_locks, plan.selected_transport).latest_watermark()
            codex_wm, written_tid = plan.source_ledger.latest_watermark(), plan.target_tid
        else:
            if plan.selected_transport == TransportMode.OFFLINE_STORAGE:
                binding = self._registry.ensure_codex_binding(binding.workspace_path)
            written_tid = self._transport.apply_to_codex(
                binding=binding, thread_id=plan.target_tid, events=plan.events, full_ledger=plan.full_ledger,
                lock_snapshot=held_locks, mode=plan.selected_transport,
                parent_thread_id=plan.lineage.parent_codex_thread_id or plan.forked_from_codex_thread_id,
                incremental=plan.incremental, lineage=plan.lineage,
            )
            agy_wm, written_cid = plan.source_ledger.latest_watermark(), plan.target_cid
            codex_wm = self._transport.read_codex_ledger(binding, written_tid, held_locks, plan.selected_transport).latest_watermark()
        if agy_wm is None or codex_wm is None:
            raise RuntimeError(f"Missing completed turn watermark after {plan.target_host.value} write.")
        return binding, written_cid, written_tid, agy_wm, codex_wm

    def _execute_write_plan(
        self, binding: WorkspaceProjectBinding, cursor: SessionSyncCursor | None, plan: _WritePlan,
    ) -> SyncReceipt:
        with contextlib.ExitStack() as stack:
            backup: BackupManifest | None = None
            if plan.selected_transport != TransportMode.OFFLINE_STORAGE:
                held_locks = self._guard.probe_locks(
                    plan.target_cid, plan.target_tid,
                    agy_turn_inflight=plan.agy_turn_inflight, codex_turn_inflight=plan.codex_turn_inflight,
                )
            else:
                try:
                    held_locks = stack.enter_context(self._guard.acquire_offline_write_locks(
                        plan.target_host, plan.target_cid, plan.target_tid,
                        agy_turn_inflight=plan.agy_turn_inflight, codex_turn_inflight=plan.codex_turn_inflight,
                    ))
                except BlockingIOError:
                    return SyncReceipt(binding, plan.target_cid, plan.target_tid, "deferred_busy", plan.receipt_transport, 0, cursor)
                target_sid = str(plan.target_cid) if plan.target_host == HostKind.ANTIGRAVITY else str(plan.target_tid)
                candidates, cleanup_globs = self._candidate_target_paths(
                    plan.target_host, plan.target_cid, plan.target_tid, binding=binding, lineage=plan.lineage
                )
                backup = self._guard.create_offline_backup(
                    plan.target_host, target_sid, self._expand_existing_with_sidecars(candidates),
                    expected_new_files=candidates, cleanup_globs=cleanup_globs,
                )
            try:
                binding, written_cid, written_tid, agy_wm, codex_wm = self._apply_and_verify_target(binding, held_locks, plan)
                src_host = HostKind.CODEX if plan.target_host == HostKind.ANTIGRAVITY else HostKind.ANTIGRAVITY
                synced_turn = codex_wm.turn_id if plan.target_host == HostKind.ANTIGRAVITY else agy_wm.turn_id
                new_cursor = SessionSyncCursor(
                    binding.workspace_uri, written_cid, written_tid, synced_turn,
                    agy_wm.last_agy_step_index or StepIndex(0), codex_wm.last_codex_line_no or RolloutLineNo(1),
                    agy_wm.boundary_digest, codex_wm.boundary_digest, _now_iso(),
                    plan.was_source_rescued, src_host.value if plan.was_source_rescued else "",
                )
                self._save_cursor(new_cursor)
            except Exception:
                if backup is not None:
                    backup.restore()
                raise
            return SyncReceipt(
                binding, written_cid, written_tid, plan.transition_kind, plan.receipt_transport,
                len(plan.events), new_cursor, plan.forked_from_cascade_id, plan.forked_from_codex_thread_id, backup,
            )

    def _build_import_or_append_plan(
        self, binding: WorkspaceProjectBinding, transition: InitialImportTransition | IncrementalAppendTransition,
        cascade_id: CascadeId | None, codex_thread_id: CodexThreadId | None,
        agy_ledger: TrajectoryLedger | None, codex_ledger: TrajectoryLedger | None,
        lineage: SubagentLineage, source_tag: AgyGuiSafeSource, receipt_transport: TransportMode,
        agy_reconciled: bool, codex_reconciled: bool, was_agy_rescued: bool, was_codex_rescued: bool,
    ) -> _WritePlan:
        events_slice = transition.events_to_apply if isinstance(transition, InitialImportTransition) else transition.delta_events
        is_inc, is_agy = isinstance(transition, IncrementalAppendTransition), (transition.target_host == HostKind.ANTIGRAVITY)
        src_ledger, dst_ledger = (codex_ledger if is_agy else agy_ledger), (agy_ledger if is_agy else codex_ledger)
        assert src_ledger is not None
        if is_agy:
            assert codex_thread_id is not None
            target_cid = cascade_id or self._unpaired_det_cid(binding.workspace_uri, codex_thread_id) or CascadeId(str(uuid.uuid4()))
            target_tid = codex_thread_id
        else:
            assert cascade_id is not None
            target_cid = cascade_id
            target_tid = codex_thread_id or self._unpaired_det_tid(binding.workspace_uri, cascade_id) or CodexThreadId(generate_uuid7())
        full_evs = [*dst_ledger.events, *events_slice] if (dst_ledger and is_inc) else events_slice
        return _make_write_plan(
            binding, transition.target_host, target_cid, target_tid, events_slice, full_evs,
            src_ledger, src_ledger.title, transition.selected_transport, transition.kind,
            lineage, source_tag, receipt_transport=receipt_transport,
            incremental=(is_inc and not (agy_reconciled if is_agy else codex_reconciled)),
            was_source_rescued=(was_codex_rescued if is_agy else was_agy_rescued),
            agy_turn_inflight=bool(agy_ledger and agy_ledger.has_inflight_turn),
            codex_turn_inflight=bool(codex_ledger and codex_ledger.has_inflight_turn),
        )

    def _build_divergence_write_plan(
        self, binding: WorkspaceProjectBinding, transition: DivergedConflictTransition,
        orig_cid: CascadeId, orig_tid: CodexThreadId,
        agy_ledger: TrajectoryLedger | None, codex_ledger: TrajectoryLedger | None,
        lineage: SubagentLineage, source_tag: AgyGuiSafeSource, receipt_transport: TransportMode,
        fork_policy: ForkResolutionPolicy, was_agy_rescued: bool, was_codex_rescued: bool,
    ) -> _WritePlan:
        is_fork, is_agy = (fork_policy == ForkResolutionPolicy.FORK_NEW_SESSION), (transition.target_host == HostKind.ANTIGRAVITY)
        src_ledger = codex_ledger if is_agy else agy_ledger
        assert src_ledger is not None
        target_cid = CascadeId(transition.preallocated_fork_session_id or str(uuid.uuid4())) if (is_fork and is_agy) else orig_cid
        target_tid = CodexThreadId(transition.preallocated_fork_session_id or generate_uuid7()) if (is_fork and not is_agy) else orig_tid
        eff_lineage = (
            dataclasses.replace(lineage, parent_cascade_id=None, parent_codex_thread_id=None)
            if (is_fork and not lineage.is_subagent) else lineage
        )
        return _make_write_plan(
            binding, transition.target_host, target_cid, target_tid, transition.fork_events_to_apply,
            transition.fork_events_to_apply, src_ledger,
            f"{src_ledger.title} (Fork)" if is_fork else src_ledger.title,
            transition.selected_transport, "diverged_forked" if is_fork else "diverged_overwritten",
            eff_lineage, source_tag, receipt_transport=receipt_transport,
            was_source_rescued=(was_codex_rescued if is_agy else was_agy_rescued),
            forked_from_cascade_id=(orig_cid if (is_fork and is_agy and orig_cid != _NULL_CID) else None),
            forked_from_codex_thread_id=(orig_tid if (not is_agy and is_fork and orig_tid != _NULL_TID) else None),
            agy_turn_inflight=(False if (is_fork and is_agy) else bool(agy_ledger and agy_ledger.has_inflight_turn)),
            codex_turn_inflight=(False if (is_fork and not is_agy) else bool(codex_ledger and codex_ledger.has_inflight_turn)),
        )

    def _materialize_symmetric_fork_peer(
        self, binding: WorkspaceProjectBinding, primary_receipt: SyncReceipt,
        orig_cid: CascadeId, orig_tid: CodexThreadId,
        agy_ledger: TrajectoryLedger | None, codex_ledger: TrajectoryLedger | None,
        locks: HostLockSnapshot, lineage: SubagentLineage, source_tag: AgyGuiSafeSource,
        was_agy_rescued: bool, was_codex_rescued: bool,
    ) -> tuple[CascadeId | None, CodexThreadId | None]:
        to_agy = primary_receipt.forked_from_codex_thread_id is not None
        src_ledger = codex_ledger if to_agy else agy_ledger
        can_write = locks.can_write_agy_offline() if to_agy else locks.can_write_codex_offline()
        orphan_free = (
            self._load_cursor(binding.workspace_uri, None, orig_tid) is None
            if to_agy else self._load_cursor(binding.workspace_uri, orig_cid, None) is None
        )
        if not (
            (to_agy or primary_receipt.forked_from_cascade_id is not None)
            and src_ledger is not None and src_ledger.latest_watermark() is not None and can_write and orphan_free
        ):
            return None, None
        peer_evs = src_ledger.slice_after_watermark(None)
        peer_rec = self._execute_write_plan(binding, None, _make_write_plan(
            binding, HostKind.ANTIGRAVITY if to_agy else HostKind.CODEX,
            CascadeId(str(uuid.uuid4())) if to_agy else orig_cid,
            orig_tid if to_agy else CodexThreadId(generate_uuid7()),
            peer_evs, peer_evs, src_ledger, f"{src_ledger.title} (Fork)",
            TransportMode.OFFLINE_STORAGE, "diverged_forked",
            lineage if lineage.is_subagent else SubagentLineage(), source_tag,
            was_source_rescued=(was_codex_rescued if to_agy else was_agy_rescued),
            forked_from_cascade_id=(orig_cid if (to_agy and orig_cid != _NULL_CID) else None),
            forked_from_codex_thread_id=(orig_tid if (not to_agy and orig_tid != _NULL_TID) else None),
        ))
        return (peer_rec.cascade_id, peer_rec.codex_thread_id) if peer_rec.transition_kind == "diverged_forked" else (None, None)

    def _finalize_noop(
        self, binding: WorkspaceProjectBinding, cursor: SessionSyncCursor | None,
        transition: NoOpTransition, cascade_id: CascadeId | None, codex_thread_id: CodexThreadId | None,
        agy_ledger: TrajectoryLedger | None, codex_ledger: TrajectoryLedger | None,
        locks: HostLockSnapshot, mode: TransportMode, receipt_transport: TransportMode,
        source_tag: AgyGuiSafeSource, lineage: SubagentLineage,
    ) -> SyncReceipt:
        final_cid, final_tid = (cascade_id or transition.cursor.cascade_id), (codex_thread_id or transition.cursor.codex_thread_id)
        if agy_ledger is not None and codex_ledger is not None and hasattr(self._transport, "ensure_companion_stores_complete"):
            binding = self._transport.ensure_companion_stores_complete(  # type: ignore[attr-defined]
                binding, final_cid, final_tid, agy_ledger, codex_ledger,
                locks, mode, source_tag, lineage, self._guard, self._registry,
            )
        fixed_cursor = dataclasses.replace(
            transition.cursor, workspace_uri=binding.workspace_uri, cascade_id=final_cid, codex_thread_id=final_tid,
        )
        if cursor is None or dataclasses.replace(cursor, updated_at_iso="") != dataclasses.replace(fixed_cursor, updated_at_iso=""):
            self._save_cursor(fixed_cursor)
        return SyncReceipt(binding, final_cid, final_tid, "no_op", receipt_transport, 0, fixed_cursor)

    def _execute_diverged_transition(
        self, binding: WorkspaceProjectBinding, cursor: SessionSyncCursor | None,
        transition: DivergedConflictTransition, cascade_id: CascadeId | None, codex_thread_id: CodexThreadId | None,
        agy_ledger: TrajectoryLedger | None, codex_ledger: TrajectoryLedger | None,
        locks: HostLockSnapshot, direction: SyncDirection, fork_policy: ForkResolutionPolicy,
        lineage: SubagentLineage, source_tag: AgyGuiSafeSource, receipt_tr: TransportMode,
        was_agy_rescued: bool, was_codex_rescued: bool,
    ) -> SyncReceipt:
        orig_cid, orig_tid = (cascade_id or transition.cursor.cascade_id), (codex_thread_id or transition.cursor.codex_thread_id)
        if fork_policy == ForkResolutionPolicy.HALT_ON_DIVERGENCE:
            return SyncReceipt(binding, orig_cid, orig_tid, "diverged_halted", receipt_tr, 0, cursor)
        div_plan = self._build_divergence_write_plan(
            binding, transition, orig_cid, orig_tid, agy_ledger, codex_ledger,
            lineage, source_tag, receipt_tr, fork_policy, was_agy_rescued, was_codex_rescued,
        )
        receipt = self._execute_write_plan(binding, cursor, div_plan)
        if (
            receipt.transition_kind == "diverged_forked"
            and direction == SyncDirection.AUTO_INCREMENTAL
            and transition.selected_transport == TransportMode.OFFLINE_STORAGE
        ):
            peer_cid, peer_tid = self._materialize_symmetric_fork_peer(
                binding, receipt, orig_cid, orig_tid, agy_ledger, codex_ledger,
                locks, lineage, source_tag, was_agy_rescued, was_codex_rescued,
            )
            if peer_cid is not None or peer_tid is not None:
                receipt = dataclasses.replace(receipt, peer_fork_cascade_id=peer_cid, peer_fork_codex_thread_id=peer_tid)
        return receipt

    def sync_session(
        self, workspace: str | Path, *,
        cascade_id: CascadeId | None = None, codex_thread_id: CodexThreadId | None = None,
        direction: SyncDirection = SyncDirection.AUTO_INCREMENTAL, mode: TransportMode = TransportMode.AUTO,
        fork_policy: ForkResolutionPolicy = ForkResolutionPolicy.HALT_ON_DIVERGENCE,
        source_tag: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT, include_interrupted: bool = False,
    ) -> SyncReceipt:
        """Synchronize one paired session (or import an unpaired session) idempotently."""
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")
        binding = self._registry.resolve_readonly_binding(workspace)
        cursor = self._load_cursor(binding.workspace_uri, cascade_id, codex_thread_id)
        if cursor is not None:
            cascade_id, codex_thread_id = (cascade_id or cursor.cascade_id), (codex_thread_id or cursor.codex_thread_id)

        lineage = self._resolve_session_lineage(binding, cascade_id, codex_thread_id)
        probed = self._probe_and_read_ledgers(binding, cursor, cascade_id, codex_thread_id, direction, mode)
        cascade_id, codex_thread_id = probed.cascade_id, probed.codex_thread_id
        if probed.rpc_required_unavailable:
            return SyncReceipt(binding, cascade_id or _NULL_CID, codex_thread_id or _NULL_TID, "deferred_busy", TransportMode.ONLINE_RPC, 0, cursor)

        agy_ledger, codex_ledger, cursor, agy_rec, codex_rec, was_agy_rescued, was_codex_rescued, busy_src = (
            reconcile_and_rescue_ledgers(cursor, probed, direction, include_interrupted)
        )
        if busy_src:
            return SyncReceipt(binding, cascade_id or _NULL_CID, codex_thread_id or _NULL_TID, "deferred_busy", probed.resolve_receipt_transport(mode, is_deferred=True), 0, cursor)

        locks = self._guard.probe_locks(
            probed.probe_cid, probed.probe_tid,
            agy_turn_inflight=bool(agy_ledger and agy_ledger.has_inflight_turn),
            codex_turn_inflight=bool(codex_ledger and codex_ledger.has_inflight_turn),
        )
        transition = CursorForkStateMachine.evaluate(
            agy_ledger, codex_ledger, cursor, direction, locks, mode, fork_policy,
            online_rpc_write_supported=bool(getattr(self._transport, "supports_online_rpc_write", False)),
        )
        if isinstance(transition, NoOpTransition):
            if not probed.satisfies_online_rpc_mode(mode, None):
                return SyncReceipt(binding, cascade_id or _NULL_CID, codex_thread_id or _NULL_TID, "deferred_busy", TransportMode.ONLINE_RPC, 0, cursor)
            return self._finalize_noop(
                binding, cursor, transition, cascade_id, codex_thread_id, agy_ledger, codex_ledger,
                locks, mode, probed.resolve_receipt_transport(mode), source_tag, lineage,
            )
        if isinstance(transition, DeferredBusyTransition):
            return SyncReceipt(binding, cascade_id or _NULL_CID, codex_thread_id or _NULL_TID, "deferred_busy", probed.resolve_receipt_transport(mode, is_deferred=True), 0, cursor)

        if not probed.satisfies_online_rpc_mode(mode, transition.source_host):
            return SyncReceipt(binding, cascade_id or _NULL_CID, codex_thread_id or _NULL_TID, "deferred_busy", TransportMode.ONLINE_RPC, 0, cursor)
        receipt_tr = probed.resolve_receipt_transport(mode, source_host=transition.source_host)
        if isinstance(transition, (InitialImportTransition, IncrementalAppendTransition)):
            plan = self._build_import_or_append_plan(
                binding, transition, cascade_id, codex_thread_id, agy_ledger, codex_ledger,
                lineage, source_tag, receipt_tr, agy_rec, codex_rec, was_agy_rescued, was_codex_rescued,
            )
            return self._execute_write_plan(binding, cursor, plan)

        return self._execute_diverged_transition(
            binding, cursor, transition, cascade_id, codex_thread_id, agy_ledger, codex_ledger,
            locks, direction, fork_policy, lineage, source_tag, receipt_tr, was_agy_rescued, was_codex_rescued,
        )

    def sync_workspace(
        self, workspace: str | Path, *,
        direction: SyncDirection = SyncDirection.AUTO_INCREMENTAL, mode: TransportMode = TransportMode.AUTO,
        fork_policy: ForkResolutionPolicy = ForkResolutionPolicy.HALT_ON_DIVERGENCE,
        include_subagents: bool = False, include_interrupted: bool = False,
    ) -> Tuple[SyncReceipt, ...]:
        """Discover and synchronize all sessions in `workspace`."""
        binding = self._registry.resolve_readonly_binding(workspace)
        sessions = self._registry.list_workspace_sessions(binding, include_subagents=include_subagents)
        receipts: list[SyncReceipt] = []
        for desc in sessions:
            if (direction == SyncDirection.AGY_TO_CODEX and desc.cascade_id is None) or (
                direction == SyncDirection.CODEX_TO_AGY and desc.codex_thread_id is None
            ):
                continue
            try:
                receipts.append(self.sync_session(
                    binding.workspace_path, cascade_id=desc.cascade_id, codex_thread_id=desc.codex_thread_id,
                    direction=direction, mode=mode, fork_policy=fork_policy, include_interrupted=include_interrupted,
                ))
            except EmptySessionError:
                continue
        return tuple(receipts)
