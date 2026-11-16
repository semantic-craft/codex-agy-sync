"""Loopback online read RPC clients (`GetCascadeTrajectory` Proto + `codex app-server` WS) and `A2` dual-mode routing."""

from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass
import http.client
from pathlib import Path
from typing import Any, Callable, Mapping, Tuple

from .._wire import encode_string_field, iter_wire_fields, read_varint
from ..codecs import (
    AgyRawStepRow, decode_agy_trajectory, decode_codex_rollout,
    decode_codex_rpc_thread_to_ledger, read_codex_rollout_lines,
    splice_rollout_completed_prefix,
)
from ..events import CascadeId, CodexThreadId, HostKind, StepIndex
from ..ledger import TrajectoryLedger
from ..workspace import validate_session_id
from .paths_and_locks import (
    AgyDaemonEndpoint, CodexDaemonEndpoint, HostLockSnapshot, PresenceWalGuard,
    TransportMode, has_live_unix_socket,
)
from .project_rpc import OnlineRpcUnavailableError, UnixWebSocketClient, open_codex_ws_rpc
from .registry import WorkspaceProjectBinding
from .state_machine import HostTransportPort, SessionSyncCursor, SyncDirection

_ACTIVE_TURN_STATUSES = frozenset({"active", "running", "inprogress", "in_progress"})
_NOT_FOUND_MARKERS = ("not found", "no rollout", "unknown thread", "does not exist")


@dataclass(frozen=True)
class AgyRpcReadResult:
    """Decoded `LanguageServerService/GetCascadeTrajectory` binary protobuf payload."""

    raw_steps: Tuple[AgyRawStepRow, ...]
    cascade_run_status: int
    trajectory_metadata_blob: bytes = b""
    source_enum: int = 0

    @property
    def is_active(self) -> bool:
        return self.cascade_run_status in {2, 3, 4}


@dataclass(frozen=True)
class CodexRpcReadResult:
    """Decoded `codex app-server` `thread/read` JSON-RPC v2 response."""

    thread: Mapping[str, Any]
    is_active: bool


def _decode_agy_rpc_step(idx: int, step_blob: bytes) -> AgyRawStepRow:
    step_type, status, has_sub, meta_blob = 0, 0, False, b""
    for fn, wt, ival, raw in iter_wire_fields(step_blob):
        if fn == 1 and wt == 0:
            step_type = ival
        elif fn == 4 and wt == 0:
            status = ival
        elif fn == 5 and wt == 2:
            meta_blob = raw
        elif fn == 6 and wt == 2:
            has_sub = True
    return AgyRawStepRow(StepIndex(idx), step_type, status, has_sub, meta_blob, step_blob, 1)


def _has_agy_trajectory_content(traj_blob: bytes) -> bool:
    pos, n = 0, len(traj_blob)
    while pos < n:
        tag_start = pos
        tag, pos = read_varint(traj_blob, pos)
        fn, wt = tag >> 3, tag & 0x07
        if fn <= 0:
            raise ValueError(f"Invalid protobuf field number {fn} at offset {tag_start}.")
        if wt == 0:
            _, pos = read_varint(traj_blob, pos)
        elif wt in (1, 5):
            width = 8 if wt == 1 else 4
            if pos + width > n:
                raise ValueError(f"Truncated fixed{width * 8} protobuf field.")
            pos += width
        elif wt == 2:
            ln, pos = read_varint(traj_blob, pos)
            if pos + ln > n:
                raise ValueError("Truncated length-delimited protobuf field.")
            if fn == 2 or (fn == 1 and ln > 0):
                return True
            pos += ln
        else:
            raise ValueError(f"Unsupported protobuf wire type {wt} at offset {tag_start}.")
    return False


def _decode_agy_trajectory_response(
    resp_bytes: bytes, cascade_id: CascadeId, *, include_steps: bool = True,
) -> AgyRpcReadResult:
    trajectory_blob, run_status = b"", 1
    for fn, wt, ival, raw in iter_wire_fields(resp_bytes):
        if fn == 1 and wt == 2:
            trajectory_blob = raw
        elif fn == 2 and wt == 0:
            run_status = ival
    if not trajectory_blob:
        raise FileNotFoundError(f"Antigravity cascade {cascade_id} returned empty trajectory.")
    if not include_steps:
        if not _has_agy_trajectory_content(trajectory_blob):
            raise FileNotFoundError(f"Antigravity cascade {cascade_id} returned empty trajectory.")
        return AgyRpcReadResult((), run_status)

    traj_id_bytes, tmeta_blob, source_enum = b"", b"", 0
    raw_steps: list[AgyRawStepRow] = []
    for fn, wt, ival, raw in iter_wire_fields(trajectory_blob):
        if fn == 1 and wt == 2:
            traj_id_bytes = raw
        elif fn == 2 and wt == 2:
            raw_steps.append(_decode_agy_rpc_step(len(raw_steps), raw))
        elif fn == 7 and wt == 2:
            tmeta_blob = raw
        elif fn == 8 and wt == 0:
            source_enum = ival
    if not raw_steps and not traj_id_bytes:
        raise FileNotFoundError(f"Antigravity cascade {cascade_id} returned empty trajectory.")
    return AgyRpcReadResult(tuple(raw_steps), run_status, tmeta_blob, source_enum)


def _send_agy_proto_once(
    conn: http.client.HTTPConnection, path: str, body: bytes, headers: Mapping[str, str], timeout_s: float,
) -> tuple[int, bytes]:
    conn.timeout = timeout_s
    if conn.sock is None:
        conn.connect()
    else:
        conn.sock.settimeout(timeout_s)
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    if resp.will_close:
        conn.close()
    return int(resp.status), data


def _post_agy_proto(
    endpoint: AgyDaemonEndpoint, path: str, body: bytes, timeout_s: float, conn: http.client.HTTPConnection | None,
) -> tuple[int, bytes]:
    headers = {
        "Content-Type": "application/proto",
        "Connect-Protocol-Version": "1",
        "Connection": "keep-alive" if conn is not None else "close",
        **endpoint.csrf_secret.as_loopback_header(),
    }
    if conn is None:
        ephemeral = http.client.HTTPConnection("127.0.0.1", endpoint.http_port, timeout=timeout_s)
        try:
            return _send_agy_proto_once(ephemeral, path, body, headers, timeout_s)
        finally:
            ephemeral.close()
    was_reused = conn.sock is not None
    try:
        return _send_agy_proto_once(conn, path, body, headers, timeout_s)
    except (http.client.HTTPException, ConnectionError, OSError):
        conn.close()
        if not was_reused:
            raise
    try:
        conn.connect()
        return _send_agy_proto_once(conn, path, body, headers, timeout_s)
    except (http.client.HTTPException, ConnectionError, OSError):
        conn.close()
        raise


def fetch_agy_trajectory_via_rpc(
    endpoint: AgyDaemonEndpoint | None,
    cascade_id: CascadeId,
    *,
    timeout_s: float = 2.0,
    include_steps: bool = True,
    conn: http.client.HTTPConnection | None = None,
) -> AgyRpcReadResult:
    """Read an Antigravity trajectory lock-free via loopback Connect RPC (`GetCascadeTrajectory`)."""
    validate_session_id(str(cascade_id), label="cascade_id")
    if endpoint is None or endpoint.http_port <= 0:
        raise OnlineRpcUnavailableError("No live Antigravity LanguageServer HTTP endpoint available.")

    path = "/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory"
    try:
        status, resp_bytes = _post_agy_proto(endpoint, path, encode_string_field(1, str(cascade_id)), timeout_s, conn)
    except (http.client.HTTPException, TimeoutError, OSError):
        raise OnlineRpcUnavailableError("Antigravity GetCascadeTrajectory RPC connection failed.") from None

    if status != 200:
        err_body = resp_bytes[:4096].decode("utf-8", errors="ignore").lower()
        if status == 404 or "not_found" in err_body or "not found" in err_body:
            raise FileNotFoundError(f"Antigravity cascade {cascade_id} not found via LanguageServer RPC.")
        raise OnlineRpcUnavailableError(f"Antigravity GetCascadeTrajectory RPC failed with HTTP {status}.")

    try:
        return _decode_agy_trajectory_response(resp_bytes, cascade_id, include_steps=include_steps)
    except ValueError:
        raise OnlineRpcUnavailableError("Invalid protobuf payload from Antigravity GetCascadeTrajectory RPC.") from None


def _decode_agy_all_run_statuses(resp_bytes: bytes) -> dict[str, int]:
    statuses: dict[str, int] = {}
    for fn, wt, _, raw in iter_wire_fields(resp_bytes):
        if fn != 1 or wt != 2:
            continue
        cid, summary_raw = "", b""
        for efn, ewt, _, eraw in iter_wire_fields(raw):
            if efn == 1 and ewt == 2:
                cid = eraw.decode("utf-8")
            elif efn == 2 and ewt == 2:
                summary_raw = eraw
        if cid:
            status = 0
            for sfn, swt, sival, _ in iter_wire_fields(summary_raw):
                if sfn == 5 and swt == 0:
                    status = sival
            statuses[cid] = status
    return statuses


def fetch_agy_run_statuses_via_rpc(
    endpoint: AgyDaemonEndpoint | None,
    *,
    timeout_s: float = 2.0,
    conn: http.client.HTTPConnection | None = None,
) -> dict[str, int]:
    """Fetch all cascade run statuses lock-free via loopback Connect RPC (`GetAllCascadeTrajectories`)."""
    if endpoint is None or endpoint.http_port <= 0:
        raise OnlineRpcUnavailableError("No live Antigravity LanguageServer HTTP endpoint available.")
    path = "/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories"
    try:
        status, resp_bytes = _post_agy_proto(endpoint, path, b"", timeout_s, conn)
    except (http.client.HTTPException, TimeoutError, OSError):
        raise OnlineRpcUnavailableError("Antigravity GetAllCascadeTrajectories RPC connection failed.") from None
    if status != 200:
        raise OnlineRpcUnavailableError(f"Antigravity GetAllCascadeTrajectories RPC failed with HTTP {status}.")
    try:
        return _decode_agy_all_run_statuses(resp_bytes)
    except ValueError:
        raise OnlineRpcUnavailableError("Invalid protobuf payload from Antigravity GetAllCascadeTrajectories RPC.") from None


def extract_codex_rpc_thread(resp_msg: Mapping[str, Any], thread_id: CodexThreadId) -> CodexRpcReadResult:
    if "error" in resp_msg:
        err_obj = resp_msg["error"]
        err_msg = str(err_obj.get("message") if isinstance(err_obj, Mapping) else err_obj)
        if any(marker in err_msg.lower() for marker in _NOT_FOUND_MARKERS):
            raise FileNotFoundError(f"Codex thread {thread_id} not found via app-server RPC.")
        raise OnlineRpcUnavailableError(f"Codex thread/read RPC error: {err_msg}")

    result = resp_msg.get("result")
    thread = result.get("thread") if isinstance(result, Mapping) else None
    if not isinstance(thread, dict):
        raise FileNotFoundError(f"Codex thread {thread_id} not found via app-server RPC.")

    raw_status = thread.get("status")
    status_type = str(raw_status.get("type") if isinstance(raw_status, Mapping) else (raw_status or "")).lower()
    has_active_turn = any(
        isinstance(t, Mapping)
        and str((t.get("status", {}).get("type") if isinstance(t.get("status"), Mapping) else t.get("status")) or "").lower()
        in _ACTIVE_TURN_STATUSES
        for t in (thread.get("turns") or ())
    )
    return CodexRpcReadResult(thread=thread, is_active=(status_type in _ACTIVE_TURN_STATUSES) or has_active_turn)


def fetch_codex_thread_via_rpc(
    endpoint: CodexDaemonEndpoint | None, thread_id: CodexThreadId, *, timeout_s: float = 2.0,
) -> CodexRpcReadResult:
    """Read a Codex thread lock-free via `codex app-server` Unix-socket WebSocket (`thread/read`)."""
    validate_session_id(str(thread_id), label="codex_thread_id")
    with open_codex_ws_rpc(endpoint, timeout_s=timeout_s) as ws:
        ws.send_json({"id": 2, "method": "thread/read", "params": {"threadId": str(thread_id), "includeTurns": True}})
        return extract_codex_rpc_thread(ws.wait_for_id(2), thread_id)


def read_agy_ledger_dual_mode(
    binding: WorkspaceProjectBinding, cascade_id: CascadeId,
    lock_snapshot: HostLockSnapshot, mode: TransportMode, *,
    read_disk_fn: Callable[[], TrajectoryLedger], read_title_fn: Callable[[CascadeId], str | None],
) -> tuple[TrajectoryLedger, TransportMode, bool]:
    validate_session_id(str(cascade_id), label="cascade_id")
    if mode == TransportMode.OFFLINE_STORAGE:
        return read_disk_fn(), TransportMode.OFFLINE_STORAGE, False

    def _from_rpc() -> tuple[TrajectoryLedger, TransportMode, bool]:
        rpc_res = fetch_agy_trajectory_via_rpc(lock_snapshot.agy_daemon, cascade_id)
        ledger = decode_agy_trajectory(
            binding.workspace_path, binding.workspace_uri, rpc_res.raw_steps,
            explicit_title_override=read_title_fn(cascade_id),
        )
        if rpc_res.is_active and not ledger.has_inflight_turn:
            ledger = dataclasses.replace(ledger, has_inflight_turn=True)
        return ledger, TransportMode.ONLINE_RPC, rpc_res.is_active

    if mode == TransportMode.ONLINE_RPC:
        return _from_rpc()
    if lock_snapshot.agy_daemon is not None and lock_snapshot.agy_daemon.http_port > 0:
        try:
            return _from_rpc()
        except OnlineRpcUnavailableError:
            pass
        except FileNotFoundError:
            return read_disk_fn(), TransportMode.OFFLINE_STORAGE, False
    return read_disk_fn(), TransportMode.OFFLINE_STORAGE, False


def read_codex_ledger_dual_mode(
    codex_home_dir: Path, binding: WorkspaceProjectBinding,
    thread_id: CodexThreadId, lock_snapshot: HostLockSnapshot, mode: TransportMode, *,
    read_disk_fn: Callable[[], TrajectoryLedger], read_optional_title_fn: Callable[[CodexThreadId], str | None],
    resolve_rollout_fn: Callable[[str, Path], Path | None],
) -> tuple[TrajectoryLedger, TransportMode, bool]:
    validate_session_id(str(thread_id), label="codex_thread_id")
    if mode == TransportMode.OFFLINE_STORAGE:
        return read_disk_fn(), TransportMode.OFFLINE_STORAGE, False

    def _from_rpc() -> tuple[TrajectoryLedger, TransportMode, bool]:
        rpc_res = fetch_codex_thread_via_rpc(lock_snapshot.codex_daemon, thread_id)
        rpc_title = str(rpc_res.thread.get("name") or rpc_res.thread.get("title") or "").strip() or read_optional_title_fn(thread_id)
        rpc_ledger = decode_codex_rpc_thread_to_ledger(
            binding.workspace_path, binding.workspace_uri, rpc_res.thread, explicit_title_override=rpc_title,
        )
        raw_path = str(rpc_res.thread.get("path") or "").strip()
        disk_ledger: TrajectoryLedger | None = None
        if raw_path:
            if (cand := resolve_rollout_fn(raw_path, codex_home_dir.resolve(strict=False))) is not None:
                disk_ledger = decode_codex_rollout(
                    binding.workspace_path, binding.workspace_uri, read_codex_rollout_lines(cand), explicit_title_override=rpc_title,
                )
        else:
            with contextlib.suppress(FileNotFoundError, OSError):
                disk_ledger = read_disk_fn()
        if disk_ledger is not None:
            rpc_ledger = splice_rollout_completed_prefix(
                disk_ledger, rpc_ledger, rpc_is_active=rpc_res.is_active, explicit_title_override=rpc_title,
            )
        elif rpc_res.is_active and not rpc_ledger.has_inflight_turn:
            rpc_ledger = dataclasses.replace(rpc_ledger, has_inflight_turn=True)
        return rpc_ledger, TransportMode.ONLINE_RPC, rpc_res.is_active

    if mode == TransportMode.ONLINE_RPC:
        return _from_rpc()
    if lock_snapshot.codex_daemon is not None and has_live_unix_socket(lock_snapshot.codex_daemon.socket_path):
        try:
            return _from_rpc()
        except OnlineRpcUnavailableError:
            pass
        except FileNotFoundError:
            return read_disk_fn(), TransportMode.OFFLINE_STORAGE, False
    return read_disk_fn(), TransportMode.OFFLINE_STORAGE, False


@dataclass(frozen=True)
class ProbedSyncLedgers:
    """Resolved dual-mode read state and provenance for a `SyncBridge.sync_session` call."""

    cascade_id: CascadeId | None
    codex_thread_id: CodexThreadId | None
    probe_cid: CascadeId | None
    probe_tid: CodexThreadId | None
    initial_locks: HostLockSnapshot
    agy_ledger: TrajectoryLedger | None
    codex_ledger: TrajectoryLedger | None
    agy_read_transport: TransportMode | None
    codex_read_transport: TransportMode | None
    agy_rpc_active: bool = False
    codex_rpc_active: bool = False
    rpc_required_unavailable: bool = False

    def used_online_rpc_for(self, host: HostKind) -> bool:
        return (self.agy_read_transport if host == HostKind.ANTIGRAVITY else self.codex_read_transport) == TransportMode.ONLINE_RPC

    @property
    def any_online_rpc_used(self) -> bool:
        return TransportMode.ONLINE_RPC in (self.agy_read_transport, self.codex_read_transport)

    def resolve_receipt_transport(
        self, requested_mode: TransportMode, *, source_host: HostKind | None = None, is_deferred: bool = False,
    ) -> TransportMode:
        if source_host is not None:
            return TransportMode.ONLINE_RPC if self.used_online_rpc_for(source_host) else TransportMode.OFFLINE_STORAGE
        if is_deferred and (requested_mode == TransportMode.ONLINE_RPC or self.any_online_rpc_used):
            return TransportMode.ONLINE_RPC
        return TransportMode.ONLINE_RPC if self.any_online_rpc_used else TransportMode.OFFLINE_STORAGE

    def satisfies_online_rpc_mode(self, requested_mode: TransportMode, source_host: HostKind | None) -> bool:
        if requested_mode != TransportMode.ONLINE_RPC:
            return True
        if self.rpc_required_unavailable:
            return False
        return self.used_online_rpc_for(source_host) if source_host is not None else self.any_online_rpc_used


def probe_sync_ledgers(
    transport: HostTransportPort, guard: PresenceWalGuard, binding: WorkspaceProjectBinding, cursor: SessionSyncCursor | None,
    cascade_id: CascadeId | None, codex_thread_id: CodexThreadId | None, direction: SyncDirection,
    mode: TransportMode, *, probe_cid: CascadeId | None, probe_tid: CodexThreadId | None,
) -> ProbedSyncLedgers:
    initial_locks = guard.probe_locks(probe_cid, probe_tid)
    agy_mode = (TransportMode.ONLINE_RPC if direction == SyncDirection.AGY_TO_CODEX else TransportMode.AUTO) if mode == TransportMode.ONLINE_RPC else mode
    codex_mode = (TransportMode.ONLINE_RPC if direction == SyncDirection.CODEX_TO_AGY else TransportMode.AUTO) if mode == TransportMode.ONLINE_RPC else mode
    agy_ledger: TrajectoryLedger | None = None
    codex_ledger: TrajectoryLedger | None = None
    agy_tr: TransportMode | None = None
    codex_tr: TransportMode | None = None
    agy_active = codex_active = rpc_unavail = False

    if (read_cid := cascade_id or (probe_cid if cursor is None else None)) is not None:
        try:
            agy_ledger, agy_tr, agy_active = transport.read_agy_ledger_with_meta(binding, read_cid, initial_locks, agy_mode)
            cascade_id = read_cid
        except FileNotFoundError:
            pass
        except OnlineRpcUnavailableError:
            rpc_unavail = rpc_unavail or (agy_mode == TransportMode.ONLINE_RPC)

    if (read_tid := codex_thread_id or (probe_tid if cursor is None else None)) is not None:
        try:
            codex_ledger, codex_tr, codex_active = transport.read_codex_ledger_with_meta(binding, read_tid, initial_locks, codex_mode)
            codex_thread_id = read_tid
        except FileNotFoundError:
            pass
        except OnlineRpcUnavailableError:
            rpc_unavail = rpc_unavail or (codex_mode == TransportMode.ONLINE_RPC)

    return ProbedSyncLedgers(
        cascade_id, codex_thread_id, probe_cid, probe_tid, initial_locks,
        agy_ledger, codex_ledger, agy_tr, codex_tr, agy_active, codex_active, rpc_unavail,
    )
