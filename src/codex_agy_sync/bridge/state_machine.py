"""RFC 9562 monotonic UUIDv7 generator and pure `CursorForkStateMachine`."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
import threading
import time
from typing import Literal, Tuple, TypeAlias
import uuid

from ..ledger import (
    CanonicalEvent, CascadeId, CodexThreadId, CompletedTurnWatermark, ContentDigest,
    HostKind, RolloutLineNo, StepIndex, TrajectoryLedger, TurnId, WorkspaceRootUri,
)
from .paths_and_locks import HostLockSnapshot, TransportMode, _now_iso

_UUID7_LOCK = threading.Lock()
_UUID7_LAST_MS = 0
_UUID7_SEQ = 0
_NULL_CID = CascadeId("00000000-0000-0000-0000-000000000000")
_NULL_TID = CodexThreadId("00000000-0000-0000-0000-000000000000")


def generate_uuid7() -> str:
    """Generate a monotonic RFC 9562 UUIDv7 lowercase `8-4-4-4-12` string."""
    global _UUID7_LAST_MS, _UUID7_SEQ
    now_ms = time.time_ns() // 1_000_000
    with _UUID7_LOCK:
        if now_ms > _UUID7_LAST_MS:
            _UUID7_LAST_MS = now_ms
            _UUID7_SEQ = int.from_bytes(os.urandom(2), "big") & 0x07FF
        else:
            _UUID7_SEQ += 1
            if _UUID7_SEQ > 0x0FFF:
                _UUID7_LAST_MS += 1
                _UUID7_SEQ = int.from_bytes(os.urandom(2), "big") & 0x07FF
        ts_ms = _UUID7_LAST_MS & ((1 << 48) - 1)
        seq_12 = _UUID7_SEQ & 0x0FFF

    rand_62 = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    uuid_int = (ts_ms << 80) | (0x7 << 76) | (seq_12 << 64) | (0b10 << 62) | rand_62
    return str(uuid.UUID(int=uuid_int))


def _allocate_host_session_uuid(target_host: HostKind) -> str:
    return generate_uuid7() if target_host == HostKind.CODEX else str(uuid.uuid4())


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
    rescued_inflight_turn: bool = False
    pre_rescue_source_host: str = ""


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
                return DeferredBusyTransition("deferred_busy", HostKind.ANTIGRAVITY, "Antigravity target session has an in-flight turn.")
            if requested_mode == TransportMode.ONLINE_RPC:
                if lock_snapshot.agy_daemon is None:
                    return DeferredBusyTransition("deferred_busy", HostKind.ANTIGRAVITY, "Online RPC requested for Antigravity, but no live LanguageServer daemon is available.")
                if not online_rpc_write_supported:
                    return DeferredBusyTransition("deferred_busy", target_host, f"Online write RPC for {target_host.value} is not enabled; deferring to avoid raw-writing active WAL storage.")
                return TransportMode.ONLINE_RPC
            if lock_snapshot.can_write_agy_offline():
                return TransportMode.OFFLINE_STORAGE
            return DeferredBusyTransition("deferred_busy", HostKind.ANTIGRAVITY, "Antigravity session is locked by an active presence flock or WAL daemon.")

        if lock_snapshot.codex_turn_inflight:
            return DeferredBusyTransition("deferred_busy", HostKind.CODEX, "Codex target thread has an in-flight turn.")
        if requested_mode == TransportMode.ONLINE_RPC:
            if lock_snapshot.codex_daemon is None:
                return DeferredBusyTransition("deferred_busy", HostKind.CODEX, "Online RPC requested for Codex, but no live app-server socket is available.")
            if not online_rpc_write_supported:
                return DeferredBusyTransition("deferred_busy", target_host, f"Online write RPC for {target_host.value} is not enabled; deferring to avoid raw-writing active WAL storage.")
            return TransportMode.ONLINE_RPC
        if lock_snapshot.can_write_codex_offline():
            return TransportMode.OFFLINE_STORAGE
        return DeferredBusyTransition("deferred_busy", HostKind.CODEX, "Codex thread cannot be written safely right now due to active WAL lock.")

    @staticmethod
    def _find_watermark(ledger: TrajectoryLedger | None, turn_id: TurnId) -> CompletedTurnWatermark | None:
        if ledger is None:
            return None
        return next((wm for wm in ledger.completed_turn_watermarks if wm.turn_id == turn_id), None)

    @staticmethod
    def _plan_divergence(
        source_host: HostKind, source_ledger: TrajectoryLedger | None, cursor: SessionSyncCursor,
        ancestor_wm: CompletedTurnWatermark | None, agy_head: CompletedTurnWatermark | None,
        codex_head: CompletedTurnWatermark | None, reason: str, lock_snapshot: HostLockSnapshot,
        transport_mode: TransportMode, fork_policy: ForkResolutionPolicy, online_rpc_write_supported: bool,
    ) -> DivergedConflictTransition | DeferredBusyTransition:
        target_host = HostKind.ANTIGRAVITY if source_host == HostKind.CODEX else HostKind.CODEX
        sel = CursorForkStateMachine._select_transport_or_defer(
            target_host, lock_snapshot, transport_mode, online_rpc_write_supported=online_rpc_write_supported
        )
        if isinstance(sel, DeferredBusyTransition):
            return sel
        fork_events = source_ledger.slice_after_watermark(None) if source_ledger is not None else ()
        return DivergedConflictTransition(
            kind="diverged", source_host=source_host, target_host=target_host, cursor=cursor,
            common_ancestor_watermark=ancestor_wm, agy_head=agy_head, codex_head=codex_head,
            reason=reason, resolution_policy=fork_policy,
            preallocated_fork_session_id=_allocate_host_session_uuid(target_host),
            fork_events_to_apply=fork_events, selected_transport=sel,
        )

    @staticmethod
    def _build_initial_import(
        source_host: HostKind, target_host: HostKind, source_ledger: TrajectoryLedger | None,
        source_head: CompletedTurnWatermark | None, lock_snapshot: HostLockSnapshot,
        transport_mode: TransportMode, online_rpc_write_supported: bool,
    ) -> InitialImportTransition | DeferredBusyTransition:
        host_label = "Antigravity session" if source_host == HostKind.ANTIGRAVITY else "Codex thread"
        if source_ledger is None or source_head is None:
            if source_ledger is not None and source_ledger.has_inflight_turn:
                return DeferredBusyTransition("deferred_busy", source_host, f"Source {host_label} only has an uncompleted in-flight turn.")
            raise ValueError(f"Source {host_label} has no completed turns to sync.")
        sel = CursorForkStateMachine._select_transport_or_defer(
            target_host, lock_snapshot, transport_mode, online_rpc_write_supported=online_rpc_write_supported
        )
        if isinstance(sel, DeferredBusyTransition):
            return sel
        return InitialImportTransition(
            kind="initial_import", source_host=source_host, target_host=target_host,
            events_to_apply=source_ledger.slice_after_watermark(None), new_watermark=source_head, selected_transport=sel,
        )

    @staticmethod
    def _evaluate_without_cursor(
        agy_ledger: TrajectoryLedger | None, codex_ledger: TrajectoryLedger | None,
        agy_head: CompletedTurnWatermark | None, codex_head: CompletedTurnWatermark | None,
        direction: SyncDirection, lock_snapshot: HostLockSnapshot, transport_mode: TransportMode,
        fork_policy: ForkResolutionPolicy, online_rpc_write_supported: bool,
    ) -> CursorTransition:
        if codex_head is None and (direction == SyncDirection.AGY_TO_CODEX or (direction == SyncDirection.AUTO_INCREMENTAL and agy_head is not None)):
            return CursorForkStateMachine._build_initial_import(
                HostKind.ANTIGRAVITY, HostKind.CODEX, agy_ledger, agy_head, lock_snapshot, transport_mode, online_rpc_write_supported
            )
        if direction == SyncDirection.AGY_TO_CODEX and agy_head is None:
            return CursorForkStateMachine._build_initial_import(
                HostKind.ANTIGRAVITY, HostKind.CODEX, agy_ledger, agy_head, lock_snapshot, transport_mode, online_rpc_write_supported
            )
        if agy_head is None and (direction == SyncDirection.CODEX_TO_AGY or (direction == SyncDirection.AUTO_INCREMENTAL and codex_head is not None)):
            return CursorForkStateMachine._build_initial_import(
                HostKind.CODEX, HostKind.ANTIGRAVITY, codex_ledger, codex_head, lock_snapshot, transport_mode, online_rpc_write_supported
            )
        if direction == SyncDirection.CODEX_TO_AGY and codex_head is None:
            return CursorForkStateMachine._build_initial_import(
                HostKind.CODEX, HostKind.ANTIGRAVITY, codex_ledger, codex_head, lock_snapshot, transport_mode, online_rpc_write_supported
            )

        if agy_head is not None and codex_head is not None:
            ws_uri = agy_ledger.workspace_uri if agy_ledger else WorkspaceRootUri("file:///workspace")
            if agy_head.turn_id == codex_head.turn_id and agy_head.boundary_digest == codex_head.boundary_digest:
                return NoOpTransition("no_op", SessionSyncCursor(
                    ws_uri, _NULL_CID, _NULL_TID, agy_head.turn_id, agy_head.last_agy_step_index or StepIndex(0),
                    codex_head.last_codex_line_no or RolloutLineNo(1), agy_head.boundary_digest, codex_head.boundary_digest, _now_iso(),
                ))
            src_host = HostKind.CODEX if direction == SyncDirection.CODEX_TO_AGY else HostKind.ANTIGRAVITY
            src_ledger = codex_ledger if src_host == HostKind.CODEX else agy_ledger
            assert src_ledger is not None
            synth_cursor = SessionSyncCursor(
                src_ledger.workspace_uri, _NULL_CID, _NULL_TID, TurnId("turn-0"), StepIndex(0), RolloutLineNo(0),
                agy_head.boundary_digest, codex_head.boundary_digest, _now_iso(),
            )
            return CursorForkStateMachine._plan_divergence(
                src_host, src_ledger, synth_cursor, None, agy_head, codex_head,
                "Both sessions already contain independent non-empty histories without a prior sync cursor.",
                lock_snapshot, transport_mode, fork_policy, online_rpc_write_supported,
            )

        raise ValueError("Neither Antigravity nor Codex has any completed turns to synchronize.")

    @staticmethod
    def _converged_noop(
        cursor: SessionSyncCursor, agy_ledger: TrajectoryLedger | None, codex_ledger: TrajectoryLedger | None,
        agy_head: CompletedTurnWatermark, codex_head: CompletedTurnWatermark,
    ) -> NoOpTransition:
        inflight = (
            HostKind.ANTIGRAVITY if (agy_ledger and agy_ledger.has_inflight_turn)
            else (HostKind.CODEX if (codex_ledger and codex_ledger.has_inflight_turn) else None)
        )
        return NoOpTransition("no_op", SessionSyncCursor(
            cursor.workspace_uri, cursor.cascade_id, cursor.codex_thread_id, agy_head.turn_id,
            agy_head.last_agy_step_index or StepIndex(0), codex_head.last_codex_line_no or RolloutLineNo(1),
            agy_head.boundary_digest, codex_head.boundary_digest, _now_iso(),
        ), deferred_inflight_host=inflight)

    @staticmethod
    def evaluate(
        agy_ledger: TrajectoryLedger | None, codex_ledger: TrajectoryLedger | None,
        cursor: SessionSyncCursor | None, direction: SyncDirection, lock_snapshot: HostLockSnapshot,
        transport_mode: TransportMode, fork_policy: ForkResolutionPolicy, online_rpc_write_supported: bool = False,
    ) -> CursorTransition:
        """Compute the deterministic `CursorTransition` without performing I/O."""
        agy_head = agy_ledger.latest_watermark() if agy_ledger is not None else None
        codex_head = codex_ledger.latest_watermark() if codex_ledger is not None else None
        if cursor is None:
            return CursorForkStateMachine._evaluate_without_cursor(
                agy_ledger, codex_ledger, agy_head, codex_head,
                direction, lock_snapshot, transport_mode, fork_policy, online_rpc_write_supported,
            )

        agy_anchor_wm = CursorForkStateMachine._find_watermark(agy_ledger, cursor.last_synced_turn_id)
        codex_anchor_wm = CursorForkStateMachine._find_watermark(codex_ledger, cursor.last_synced_turn_id)
        if (agy_anchor_wm is None or agy_anchor_wm.boundary_digest != cursor.agy_boundary_digest) or (
            codex_anchor_wm is None or codex_anchor_wm.boundary_digest != cursor.codex_boundary_digest
        ):
            if agy_head and codex_head and agy_head.turn_id == codex_head.turn_id and agy_head.boundary_digest == codex_head.boundary_digest:
                return CursorForkStateMachine._converged_noop(cursor, agy_ledger, codex_ledger, agy_head, codex_head)
            src_host = HostKind.CODEX if direction == SyncDirection.CODEX_TO_AGY else HostKind.ANTIGRAVITY
            return CursorForkStateMachine._plan_divergence(
                src_host, codex_ledger if src_host == HostKind.CODEX else agy_ledger, cursor, None, agy_head, codex_head,
                "Watermark turn missing or boundary digest mutated since last sync.",
                lock_snapshot, transport_mode, fork_policy, online_rpc_write_supported,
            )

        assert agy_head is not None and codex_head is not None and agy_ledger is not None and codex_ledger is not None
        agy_advanced = agy_head.turn_id != cursor.last_synced_turn_id or int(agy_head.last_agy_step_index or 0) > int(cursor.last_synced_agy_step_idx)
        codex_advanced = codex_head.turn_id != cursor.last_synced_turn_id or int(codex_head.last_codex_line_no or 0) > int(cursor.last_synced_codex_line_no)

        if not agy_advanced and not codex_advanced:
            inflight = HostKind.ANTIGRAVITY if agy_ledger.has_inflight_turn else (HostKind.CODEX if codex_ledger.has_inflight_turn else None)
            return NoOpTransition("no_op", cursor, deferred_inflight_host=inflight)

        if agy_advanced and not codex_advanced:
            if direction == SyncDirection.CODEX_TO_AGY:
                return CursorForkStateMachine._plan_divergence(
                    HostKind.CODEX, codex_ledger, cursor, agy_anchor_wm, agy_head, codex_head,
                    "Target Antigravity session has uncommitted turns ahead of Codex source.",
                    lock_snapshot, transport_mode, fork_policy, online_rpc_write_supported,
                )
            sel = CursorForkStateMachine._select_transport_or_defer(HostKind.CODEX, lock_snapshot, transport_mode, online_rpc_write_supported)
            return sel if isinstance(sel, DeferredBusyTransition) else IncrementalAppendTransition(
                "incremental_append", HostKind.ANTIGRAVITY, HostKind.CODEX, cursor,
                agy_ledger.slice_after_watermark(cursor.last_synced_turn_id), agy_head, sel,
            )

        if codex_advanced and not agy_advanced:
            if direction == SyncDirection.AGY_TO_CODEX:
                return CursorForkStateMachine._plan_divergence(
                    HostKind.ANTIGRAVITY, agy_ledger, cursor, codex_anchor_wm, agy_head, codex_head,
                    "Target Codex thread has uncommitted turns ahead of Antigravity source.",
                    lock_snapshot, transport_mode, fork_policy, online_rpc_write_supported,
                )
            sel = CursorForkStateMachine._select_transport_or_defer(HostKind.ANTIGRAVITY, lock_snapshot, transport_mode, online_rpc_write_supported)
            return sel if isinstance(sel, DeferredBusyTransition) else IncrementalAppendTransition(
                "incremental_append", HostKind.CODEX, HostKind.ANTIGRAVITY, cursor,
                codex_ledger.slice_after_watermark(cursor.last_synced_turn_id), codex_head, sel,
            )

        if agy_head.turn_id == codex_head.turn_id and agy_head.boundary_digest == codex_head.boundary_digest:
            return CursorForkStateMachine._converged_noop(cursor, agy_ledger, codex_ledger, agy_head, codex_head)

        src_host = HostKind.CODEX if direction == SyncDirection.CODEX_TO_AGY else HostKind.ANTIGRAVITY
        return CursorForkStateMachine._plan_divergence(
            src_host, codex_ledger if src_host == HostKind.CODEX else agy_ledger, cursor, agy_anchor_wm, agy_head, codex_head,
            "Both Antigravity and Codex advanced independently past the last sync watermark.",
            lock_snapshot, transport_mode, fork_policy, online_rpc_write_supported,
        )
