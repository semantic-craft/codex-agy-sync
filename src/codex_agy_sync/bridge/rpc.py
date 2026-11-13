"""Loopback online read RPC clients (`GetCascadeTrajectory` Proto + `codex app-server` WS) and `A2` dual-mode routing."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import struct
from typing import Any, Callable, Mapping, Tuple
import urllib.error
import urllib.request

from .._wire import encode_string_field, iter_wire_fields
from ..codecs import (
    AgyRawStepRow, TrajectoryWireCodec, decode_codex_rpc_thread_to_ledger,
    read_codex_rollout_lines, splice_rollout_completed_prefix,
)
from ..ledger import CascadeId, CodexThreadId, HostKind, StepIndex, TrajectoryLedger, validate_session_id
from .paths_and_locks import (
    AgyDaemonEndpoint, CodexDaemonEndpoint, HostLockSnapshot, PresenceWalGuard,
    TransportMode, has_live_unix_socket, unix_socket_connect_path,
)
from .registry import WorkspaceProjectBinding
from .state_machine import SessionSyncCursor, SyncDirection

_ACTIVE_TURN_STATUSES = frozenset({"active", "running", "inprogress", "in_progress"})
_NOT_FOUND_MARKERS = ("not found", "no rollout", "unknown thread", "does not exist")


class OnlineRpcUnavailableError(RuntimeError):
    """Raised when an online loopback RPC endpoint is unreachable or fails transport."""


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


def _decode_agy_trajectory_response(resp_bytes: bytes, cascade_id: CascadeId) -> AgyRpcReadResult:
    trajectory_blob, run_status = b"", 1
    for fn, wt, ival, raw in iter_wire_fields(resp_bytes):
        if fn == 1 and wt == 2:
            trajectory_blob = raw
        elif fn == 2 and wt == 0:
            run_status = ival
    if not trajectory_blob:
        raise FileNotFoundError(f"Antigravity cascade {cascade_id} returned empty trajectory.")

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


def fetch_agy_trajectory_via_rpc(
    endpoint: AgyDaemonEndpoint | None, cascade_id: CascadeId, *, timeout_s: float = 2.0,
) -> AgyRpcReadResult:
    """Read an Antigravity trajectory lock-free via loopback Connect RPC (`GetCascadeTrajectory`)."""
    validate_session_id(str(cascade_id), label="cascade_id")
    if endpoint is None or endpoint.http_port <= 0:
        raise OnlineRpcUnavailableError("No live Antigravity LanguageServer HTTP endpoint available.")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{endpoint.http_port}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory"
    headers = {"Content-Type": "application/proto", "Connect-Protocol-Version": "1", **endpoint.csrf_secret.as_loopback_header()}
    req = urllib.request.Request(url, data=encode_string_field(1, str(cascade_id)), headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            resp_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        err_body = ""
        with contextlib.suppress(Exception):
            err_body = exc.read(4096).decode("utf-8", errors="ignore").lower()
            exc.close()
        if exc.code == 404 or "not_found" in err_body or "not found" in err_body:
            raise FileNotFoundError(f"Antigravity cascade {cascade_id} not found via LanguageServer RPC.") from None
        raise OnlineRpcUnavailableError(f"Antigravity GetCascadeTrajectory RPC failed with HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise OnlineRpcUnavailableError("Antigravity GetCascadeTrajectory RPC connection failed.") from None

    try:
        return _decode_agy_trajectory_response(resp_bytes, cascade_id)
    except ValueError:
        raise OnlineRpcUnavailableError("Invalid protobuf payload from Antigravity GetCascadeTrajectory RPC.") from None


def _xor_websocket_mask(payload: bytes, mask_key: bytes) -> bytes:
    if not (plen := len(payload)):
        return b""
    repeated = (mask_key * ((plen // 4) + 1))[:plen]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(plen, "big")


class _UnixWebSocketClient:
    """Minimal stdlib RFC 6455 client over an `AF_UNIX` stream socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def handshake(self) -> None:
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        upgrade_req = (
            f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {ws_key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._sock.sendall(upgrade_req)
        while b"\r\n\r\n" not in self._buf:
            if len(self._buf) > 65536 or not (chunk := self._sock.recv(4096)):
                raise OnlineRpcUnavailableError("Codex WebSocket HTTP upgrade failed.")
            self._buf.extend(chunk)
        header_raw, remainder = bytes(self._buf).split(b"\r\n\r\n", 1)
        self._buf = bytearray(remainder)
        status_line = header_raw.splitlines()[0].decode("latin1", errors="replace") if header_raw else ""
        if not status_line.startswith("HTTP/1.1 101"):
            raise OnlineRpcUnavailableError("Codex socket rejected WebSocket 101 upgrade.")

    def _read_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            if not (chunk := self._sock.recv(max(4096, size - len(self._buf)))):
                raise OnlineRpcUnavailableError("Unexpected EOF reading Codex WebSocket frame.")
            self._buf.extend(chunk)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        plen, first = len(payload), (0x80 | (opcode & 0x0F))
        if plen < 126:
            hdr = struct.pack("!BB", first, 0x80 | plen)
        elif plen < 65536:
            hdr = struct.pack("!BBH", first, 0x80 | 126, plen)
        else:
            hdr = struct.pack("!BBQ", first, 0x80 | 127, plen)
        mask_key = os.urandom(4)
        self._sock.sendall(hdr + mask_key + _xor_websocket_mask(payload, mask_key))

    def send_json(self, obj: Mapping[str, Any]) -> None:
        self.send_frame(0x1, json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    def recv_json(self) -> dict[str, Any]:
        fragments = bytearray()
        while True:
            b0, b1 = self._read_exact(2)
            fin, opcode, masked, plen = bool(b0 & 0x80), (b0 & 0x0F), bool(b1 & 0x80), (b1 & 0x7F)
            if plen == 126:
                (plen,) = struct.unpack("!H", self._read_exact(2))
            elif plen == 127:
                (plen,) = struct.unpack("!Q", self._read_exact(8))
            mask_key = self._read_exact(4) if masked else b""
            payload = self._read_exact(plen) if plen > 0 else b""
            if masked and plen > 0:
                payload = _xor_websocket_mask(payload, mask_key)
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise OnlineRpcUnavailableError("Codex WebSocket closed by server.")
            fragments.extend(payload)
            if fin:
                try:
                    parsed = json.loads(fragments.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise OnlineRpcUnavailableError("Malformed JSON frame from Codex app-server.") from None
                if not isinstance(parsed, dict):
                    raise OnlineRpcUnavailableError("Non-object JSON-RPC message from Codex app-server.")
                return parsed

    def wait_for_id(self, target_id: int, *, max_messages: int = 128) -> dict[str, Any]:
        for _ in range(max_messages):
            if (msg := self.recv_json()).get("id") == target_id:
                return msg
        raise OnlineRpcUnavailableError(f"Timed out awaiting Codex RPC response id={target_id}.")


def _extract_codex_rpc_thread(resp_msg: Mapping[str, Any], thread_id: CodexThreadId) -> CodexRpcReadResult:
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
    if endpoint is None or not has_live_unix_socket(endpoint.socket_path):
        raise OnlineRpcUnavailableError("No live Codex app-server Unix socket available.")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    ws = _UnixWebSocketClient(sock)
    try:
        sock.connect(unix_socket_connect_path(endpoint.socket_path))
        ws.handshake()
        ws.send_json({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex-agy-sync", "version": "0.2.0"}, "capabilities": {"experimentalApi": True},
        }})
        if "error" in ws.wait_for_id(1):
            raise OnlineRpcUnavailableError("Codex app-server initialize RPC failed.")
        ws.send_json({"method": "initialized", "params": {}})
        ws.send_json({"id": 2, "method": "thread/read", "params": {"threadId": str(thread_id), "includeTurns": True}})
        return _extract_codex_rpc_thread(ws.wait_for_id(2), thread_id)
    except (TimeoutError, OSError):
        raise OnlineRpcUnavailableError("Codex app-server Unix socket connection failed.") from None
    finally:
        with contextlib.suppress(OSError):
            ws.send_frame(0x8, b"")
        sock.close()


def import_codex_rollout_via_rpc(
    endpoint: CodexDaemonEndpoint, rollout_path: Path, workspace_path: Path, project_id: str, title: str,
    *, timeout_s: float = 90.0,
) -> CodexThreadId:
    from .native_codex import import_codex_rollout_via_rpc as native_import

    return native_import(endpoint, rollout_path, workspace_path, project_id, title, timeout_s=timeout_s)


def read_agy_ledger_dual_mode(
    codec: TrajectoryWireCodec, binding: WorkspaceProjectBinding, cascade_id: CascadeId,
    lock_snapshot: HostLockSnapshot, mode: TransportMode, *,
    read_disk_fn: Callable[[], TrajectoryLedger], read_title_fn: Callable[[CascadeId], str | None],
) -> tuple[TrajectoryLedger, TransportMode, bool]:
    validate_session_id(str(cascade_id), label="cascade_id")
    if mode == TransportMode.OFFLINE_STORAGE:
        return read_disk_fn(), TransportMode.OFFLINE_STORAGE, False

    def _from_rpc() -> tuple[TrajectoryLedger, TransportMode, bool]:
        rpc_res = fetch_agy_trajectory_via_rpc(lock_snapshot.agy_daemon, cascade_id)
        ledger = codec.decode_agy_steps_to_ledger(
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
    codec: TrajectoryWireCodec, codex_home_dir: Path, binding: WorkspaceProjectBinding,
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
                disk_ledger = codec.decode_codex_rollout_to_ledger(
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
    transport: Any, guard: PresenceWalGuard, binding: WorkspaceProjectBinding, cursor: SessionSyncCursor | None,
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
            if hasattr(transport, "read_agy_ledger_with_meta"):
                agy_ledger, agy_tr, agy_active = transport.read_agy_ledger_with_meta(binding, read_cid, initial_locks, agy_mode)
            else:
                agy_ledger = transport.read_agy_ledger(binding, read_cid, initial_locks, agy_mode)
                agy_tr = TransportMode.ONLINE_RPC if agy_mode == TransportMode.ONLINE_RPC else TransportMode.OFFLINE_STORAGE
            cascade_id = read_cid
        except FileNotFoundError:
            pass
        except OnlineRpcUnavailableError:
            rpc_unavail = rpc_unavail or (agy_mode == TransportMode.ONLINE_RPC)

    if (read_tid := codex_thread_id or (probe_tid if cursor is None else None)) is not None:
        try:
            if hasattr(transport, "read_codex_ledger_with_meta"):
                codex_ledger, codex_tr, codex_active = transport.read_codex_ledger_with_meta(binding, read_tid, initial_locks, codex_mode)
            else:
                codex_ledger = transport.read_codex_ledger(binding, read_tid, initial_locks, codex_mode)
                codex_tr = TransportMode.ONLINE_RPC if codex_mode == TransportMode.ONLINE_RPC else TransportMode.OFFLINE_STORAGE
            codex_thread_id = read_tid
        except FileNotFoundError:
            pass
        except OnlineRpcUnavailableError:
            rpc_unavail = rpc_unavail or (codex_mode == TransportMode.ONLINE_RPC)

    return ProbedSyncLedgers(
        cascade_id, codex_thread_id, probe_cid, probe_tid, initial_locks,
        agy_ledger, codex_ledger, agy_tr, codex_tr, agy_active, codex_active, rpc_unavail,
    )
