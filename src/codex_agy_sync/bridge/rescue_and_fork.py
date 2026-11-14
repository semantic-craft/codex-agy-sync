"""Interrupted-turn reconciliation (`B2`) and live active-source detection."""

from __future__ import annotations

from ..events import ContentDigest, HostKind, RolloutLineNo, StepIndex, TaskBoundaryEvent
from ..ledger import TrajectoryLedger, rescue_interrupted_ledger, rescue_interrupted_turn
from .paths_and_locks import HostLockSnapshot, SyncBusyLockError, now_iso
from .rpc import ProbedSyncLedgers
from .state_machine import SessionSyncCursor, SyncDirection

__all__ = [
    "SyncBusyLockError",
    "is_source_actively_running",
    "reconcile_and_rescue_ledgers",
]


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
                agy_wm.boundary_digest, codex_wm.boundary_digest, now_iso(), False, "",
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
        is_pre_agy = eff_cursor.pre_rescue_source_host != HostKind.CODEX.value
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
