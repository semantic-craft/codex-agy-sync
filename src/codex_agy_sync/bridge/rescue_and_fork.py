"""Interrupted-turn reconciliation (`B2`), guarded companion self-heal, and root-fork lineage helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from ..ledger import (
    AgyGuiSafeSource,
    CascadeId,
    CodexThreadId,
    ContentDigest,
    HostKind,
    RolloutLineNo,
    StepIndex,
    SubagentLineage,
    TaskBoundaryEvent,
    TrajectoryLedger,
    rescue_interrupted_ledger,
    rescue_interrupted_turn,
)
from .paths_and_locks import (
    HostEnvironmentPaths,
    HostLockSnapshot,
    PresenceWalGuard,
    TransportMode,
    _now_iso,
    _validated_child_path,
)
from .registry import ProjectRegistry, WorkspaceProjectBinding
from .rpc import ProbedSyncLedgers
from .state_machine import SessionSyncCursor, SyncDirection

if TYPE_CHECKING:
    from .transport import DefaultHostTransport

CODEX_ROOT_CLIENT_SOURCES = frozenset({"desktop", "vscode", "cli", "exec"})


class SyncBusyLockError(RuntimeError):
    """Raised when an explicit workspace-pairing or offline operation cannot acquire write locks."""

    def __init__(
        self,
        message: str,
        *,
        host: str | None = None,
        remediation: Sequence[str] = (),
    ) -> None:
        self.host = host
        self.remediation = tuple(remediation)
        super().__init__(message)

    def to_error_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": "sync_busy_lock", "message": str(self)}
        if self.host is not None:
            payload["host"] = self.host
        if self.remediation:
            payload["remediation"] = list(self.remediation)
        return payload


def compute_codex_nesting_depth(
    raw_source: str | None, src_is_sub: bool, src_depth: int, parent_tid_str: str, eff_c_type: str, eff_c_role: str,
) -> int:
    """Resolve Codex thread `nesting_depth` while keeping root client forks (`desktop`/`vscode`/`cli`/`exec`) at `0`."""
    if src_is_sub:
        return max(1, src_depth)
    if str(raw_source or "").strip().lower() in CODEX_ROOT_CLIENT_SOURCES:
        return 0
    return 1 if (parent_tid_str and (eff_c_type or eff_c_role)) else 0


def expand_existing_with_sidecars(candidates: Sequence[Path]) -> list[Path]:
    expanded: list[Path] = []
    for p in candidates:
        if p.is_file():
            expanded.append(p)
            if p.suffix in (".db", ".sqlite"):
                expanded.extend(sc for sfx in ("-wal", "-shm") if (sc := p.with_name(f"{p.name}{sfx}")).is_file())
    return expanded


def is_source_actively_running(
    host: HostKind, ledger: TrajectoryLedger | None, locks: HostLockSnapshot, *, rpc_active: bool = False,
) -> bool:
    if rpc_active:
        return True
    if host == HostKind.ANTIGRAVITY:
        return locks.agy_presence_locked or locks.agy_wal_active or bool(
            locks.agy_daemon is not None and ledger is not None and any(
                ext.extension_type == "agy_step_status" and dict(ext.metadata).get("status") in {"1", "2", "8"}
                for ev in ledger.events for ext in ev.anchor.host_extensions
            )
        )
    return locks.codex_wal_active or bool(
        locks.codex_daemon is not None and ledger is not None and any(
            isinstance(ev, TaskBoundaryEvent) and ev.task_status.lower() == "in_progress" for ev in ledger.events
        )
    )


def _reconcile_historical_unsealed_turns(
    ledger: TrajectoryLedger | None, cursor: SessionSyncCursor, expected_digest: ContentDigest,
) -> tuple[TrajectoryLedger | None, bool]:
    if ledger is None:
        return None, False
    wm = ledger.find_watermark(cursor.last_synced_turn_id)
    if wm is not None and wm.boundary_digest == expected_digest:
        return ledger, False
    cur_ledger = ledger
    changed = False
    for turn_id in list(cur_ledger.by_turn_id.keys()):
        sealed, did_seal = rescue_interrupted_turn(cur_ledger, turn_id)
        if did_seal:
            cur_ledger, changed = sealed, True
            chk = cur_ledger.find_watermark(cursor.last_synced_turn_id)
            if chk is not None and chk.boundary_digest == expected_digest:
                return cur_ledger, True
        if turn_id == cursor.last_synced_turn_id:
            break
    chk = cur_ledger.find_watermark(cursor.last_synced_turn_id)
    return (cur_ledger, True) if (changed and chk is not None and chk.boundary_digest == expected_digest) else (ledger, False)


def _reconcile_rescued_source_replacement(
    cursor: SessionSyncCursor, is_pre_agy: bool, cand_ledger: TrajectoryLedger, peer_ledger: TrajectoryLedger | None,
) -> tuple[TrajectoryLedger | None, SessionSyncCursor | None, bool]:
    if peer_ledger is None:
        return peer_ledger, cursor, False
    peer_head = peer_ledger.latest_watermark()
    peer_expected = cursor.codex_boundary_digest if is_pre_agy else cursor.agy_boundary_digest
    if peer_head is None or peer_head.turn_id != cursor.last_synced_turn_id or peer_head.boundary_digest != peer_expected:
        return peer_ledger, cursor, False
    cand_wms = cand_ledger.completed_turn_watermarks
    peer_wms = peer_ledger.completed_turn_watermarks
    cand_idx = next((i for i, w in enumerate(cand_wms) if w.turn_id == cursor.last_synced_turn_id), None)
    if cand_idx is None:
        return peer_ledger, cursor, False
    if cand_idx == 0 and len(peer_wms) == 1:
        return None, None, True
    if cand_idx > 0 and len(peer_wms) > cand_idx:
        cand_prev, peer_prev = cand_wms[cand_idx - 1], peer_wms[cand_idx - 1]
        if cand_prev.turn_id == peer_prev.turn_id and cand_prev.boundary_digest == peer_prev.boundary_digest:
            truncated_peer = TrajectoryLedger.from_events(
                peer_ledger.workspace_path, peer_ledger.workspace_uri,
                peer_ledger.events[: int(peer_prev.last_ledger_seq) + 1], explicit_title_override=peer_ledger.title,
            )
            agy_wm = cand_prev if is_pre_agy else peer_prev
            codex_wm = peer_prev if is_pre_agy else cand_prev
            rewound_cursor = SessionSyncCursor(
                cursor.workspace_uri, cursor.cascade_id, cursor.codex_thread_id, cand_prev.turn_id,
                agy_wm.last_agy_step_index or StepIndex(0), codex_wm.last_codex_line_no or RolloutLineNo(1),
                agy_wm.boundary_digest, codex_wm.boundary_digest, _now_iso(), False, "",
            )
            return truncated_peer, rewound_cursor, True
    return peer_ledger, cursor, False


def reconcile_and_rescue_ledgers(
    cursor: SessionSyncCursor | None,
    probed: ProbedSyncLedgers,
    direction: SyncDirection,
    include_interrupted: bool,
) -> tuple[
    TrajectoryLedger | None,
    TrajectoryLedger | None,
    SessionSyncCursor | None,
    bool,
    bool,
    bool,
    bool,
    bool,
]:
    agy_ledger, codex_ledger, initial_locks = probed.agy_ledger, probed.codex_ledger, probed.initial_locks
    agy_reconciled, codex_reconciled = False, False
    eff_cursor = cursor

    if eff_cursor is not None and eff_cursor.rescued_inflight_turn:
        pre_host_str = eff_cursor.pre_rescue_source_host
        is_pre_agy = pre_host_str != HostKind.CODEX.value
        cand_host = HostKind.ANTIGRAVITY if is_pre_agy else HostKind.CODEX
        cand_ledger = agy_ledger if is_pre_agy else codex_ledger
        peer_ledger = codex_ledger if is_pre_agy else agy_ledger
        cand_digest = eff_cursor.agy_boundary_digest if is_pre_agy else eff_cursor.codex_boundary_digest
        cand_active = probed.agy_rpc_active if is_pre_agy else probed.codex_rpc_active
        cand_wm = cand_ledger.find_watermark(eff_cursor.last_synced_turn_id) if cand_ledger is not None else None

        if cand_wm is None or cand_wm.boundary_digest != cand_digest:
            if is_source_actively_running(cand_host, cand_ledger, initial_locks, rpc_active=cand_active):
                return agy_ledger, codex_ledger, eff_cursor, False, False, False, False, True
            if cand_ledger is not None:
                sealed_cand, did_seal = rescue_interrupted_turn(cand_ledger, eff_cursor.last_synced_turn_id)
                sealed_wm = sealed_cand.find_watermark(eff_cursor.last_synced_turn_id) if did_seal else None
                if sealed_wm is not None and sealed_wm.boundary_digest == cand_digest:
                    if is_pre_agy:
                        agy_ledger, agy_reconciled = sealed_cand, True
                    else:
                        codex_ledger, codex_reconciled = sealed_cand, True
                elif not did_seal and cand_wm is not None:
                    new_peer, new_cur, did_rewind = _reconcile_rescued_source_replacement(
                        eff_cursor, is_pre_agy, cand_ledger, peer_ledger
                    )
                    if did_rewind:
                        eff_cursor = new_cur
                        if is_pre_agy:
                            codex_ledger, codex_reconciled = new_peer, True
                        else:
                            agy_ledger, agy_reconciled = new_peer, True

    if eff_cursor is not None:
        agy_ledger, agy_hist = _reconcile_historical_unsealed_turns(agy_ledger, eff_cursor, eff_cursor.agy_boundary_digest)
        codex_ledger, codex_hist = _reconcile_historical_unsealed_turns(codex_ledger, eff_cursor, eff_cursor.codex_boundary_digest)
        agy_reconciled = agy_reconciled or agy_hist
        codex_reconciled = codex_reconciled or codex_hist

    was_agy_rescued, was_codex_rescued = False, False
    if include_interrupted:
        if direction in (SyncDirection.AGY_TO_CODEX, SyncDirection.AUTO_INCREMENTAL) and agy_ledger and agy_ledger.has_inflight_turn:
            if is_source_actively_running(HostKind.ANTIGRAVITY, agy_ledger, initial_locks, rpc_active=probed.agy_rpc_active):
                return agy_ledger, codex_ledger, eff_cursor, agy_reconciled, codex_reconciled, False, False, True
            agy_ledger, was_agy_rescued = rescue_interrupted_ledger(agy_ledger)
        if direction in (SyncDirection.CODEX_TO_AGY, SyncDirection.AUTO_INCREMENTAL) and codex_ledger and codex_ledger.has_inflight_turn:
            if is_source_actively_running(HostKind.CODEX, codex_ledger, initial_locks, rpc_active=probed.codex_rpc_active):
                return agy_ledger, codex_ledger, eff_cursor, agy_reconciled, codex_reconciled, False, False, True
            codex_ledger, was_codex_rescued = rescue_interrupted_ledger(codex_ledger)
    return agy_ledger, codex_ledger, eff_cursor, agy_reconciled, codex_reconciled, was_agy_rescued, was_codex_rescued, False


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
    agy_registered = registry._find_readonly_agy_project_id(ro_binding.workspace_uri) is not None
    codex_registered = registry._find_readonly_codex_project_id(ro_binding.workspace_path) is not None
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


def ensure_companion_stores_complete_guarded(
    transport: DefaultHostTransport,
    binding: WorkspaceProjectBinding,
    cascade_id: CascadeId,
    codex_thread_id: CodexThreadId,
    agy_ledger: TrajectoryLedger | None,
    codex_ledger: TrajectoryLedger | None,
    locks: HostLockSnapshot,
    mode: TransportMode,
    source_tag: AgyGuiSafeSource,
    lineage: SubagentLineage,
    guard: PresenceWalGuard,
    registry: ProjectRegistry,
) -> WorkspaceProjectBinding:
    if mode == TransportMode.ONLINE_RPC or agy_ledger is None or codex_ledger is None:
        return binding
    paths = transport._paths
    conv_db = _validated_child_path(paths.agy_data_dir, "conversations", f"{cascade_id}.db", label="cascade_id")
    if conv_db.is_file() and locks.can_write_agy_offline() and transport._agy_companions_missing(binding, cascade_id, lineage):
        try:
            with guard.acquire_offline_write_locks(HostKind.ANTIGRAVITY, cascade_id, codex_thread_id):
                agy_cands = [
                    paths.gemini_config_dir / "projects" / f"{binding.agy_project_id}.json",
                    paths.agy_data_dir / "conversation_summaries.db",
                    paths.agy_data_dir / "agyhub_summaries_proto.pb",
                    _validated_child_path(paths.agy_data_dir, "annotations", f"{cascade_id}.pbtxt", label="cascade_id"),
                    *[
                        _validated_child_path(rdir, "brain", str(cascade_id), ".system_generated", "logs", "transcript.jsonl", label="cascade_id")
                        for rdir in (paths.agy_data_dir, paths.agy_cli_data_dir) if rdir is not None
                    ],
                ]
                if lineage.is_subagent and lineage.parent_cascade_id is not None:
                    agy_cands.append(_validated_child_path(
                        paths.agy_data_dir, "brain", str(lineage.parent_cascade_id), ".system_generated", "subagents", f"{cascade_id}.json", label="cascade_id"
                    ))
                backup = guard.create_offline_backup(
                    HostKind.ANTIGRAVITY, str(cascade_id), expand_existing_with_sidecars(agy_cands), expected_new_files=agy_cands
                )
                try:
                    binding = registry.ensure_agy_binding(binding.workspace_path)
                    raw_rows = transport._read_persisted_agy_steps(conv_db)
                    persisted = transport._codec.decode_agy_steps_to_ledger(
                        workspace_path=binding.workspace_path, workspace_uri=binding.workspace_uri,
                        raw_steps=raw_rows, explicit_title_override=agy_ledger.title,
                    )
                    from .transport import _resolve_effective_agy_lineage

                    eff_lin = _resolve_effective_agy_lineage(lineage.parent_cascade_id, lineage)
                    transport._write_agy_companion_stores(binding, cascade_id, persisted, len(raw_rows), conv_db, source_tag, eff_lin)
                except Exception:
                    backup.restore()
                    raise
        except BlockingIOError:
            pass

    if locks.can_write_codex_offline() and transport._codex_companions_missing(binding, codex_thread_id):
        try:
            with guard.acquire_offline_write_locks(HostKind.CODEX, cascade_id, codex_thread_id):
                rollout_path, _ = transport._find_codex_rollout_and_title(codex_thread_id)
                cx_cands = [
                    paths.codex_home_dir / "state_5.sqlite",
                    paths.codex_home_dir / ".codex-global-state.json",
                    paths.codex_home_dir / "session_index.jsonl",
                ]
                backup = guard.create_offline_backup(
                    HostKind.CODEX, str(codex_thread_id), expand_existing_with_sidecars(cx_cands), expected_new_files=cx_cands
                )
                try:
                    binding = registry.ensure_codex_binding(binding.workspace_path)
                    transport._write_codex_companion_stores(
                        binding, codex_thread_id, rollout_path, codex_ledger, lineage, lineage.parent_codex_thread_id
                    )
                except Exception:
                    backup.restore()
                    raise
        except (BlockingIOError, FileNotFoundError):
            pass
    return binding
