from __future__ import annotations

import contextlib
import json
from pathlib import Path
import socket
import time
from typing import Any, Mapping

from .. import __version__
from ..codecs import read_codex_rollout_lines
from ..ledger import CodexThreadId, validate_session_id
from .paths_and_locks import CodexDaemonEndpoint, has_live_unix_socket, unix_socket_connect_path
from .rpc import OnlineRpcUnavailableError, _UnixWebSocketClient, _extract_codex_rpc_thread


class NativeCodexImportPending(OnlineRpcUnavailableError):
    """A fork may exist. Reconcile its actual ID before considering another import."""

    def __init__(self, message: str, thread_id: CodexThreadId | None = None) -> None:
        super().__init__(message)
        self.thread_id = thread_id


class _NativeCodexRpc:
    def __init__(self, sock: socket.socket, ws: _UnixWebSocketClient, deadline: float) -> None:
        self.sock, self.ws, self.deadline = sock, ws, deadline
        self.request_id = 0
        self.closed_threads: set[str] = set()

    def receive(self) -> dict[str, Any]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex native import deadline expired.")
        self.sock.settimeout(remaining)
        message = self.ws.recv_json()
        params = message.get("params") or {}
        if message.get("method") == "thread/closed" or (
            message.get("method") == "thread/status/changed" and params.get("status", {}).get("type") == "notLoaded"
        ):
            self.closed_threads.add(str(params.get("threadId")))
        return message

    def call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.request_id += 1
        self.ws.send_json({"id": self.request_id, "method": method, "params": params})
        while True:
            response = self.receive()
            if response.get("id") != self.request_id:
                continue
            if "error" in response:
                error = response["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise OnlineRpcUnavailableError(f"Codex {method} RPC failed with code {code if isinstance(code, int) else 'unknown'}.")
            result = response.get("result")
            if not isinstance(result, dict):
                raise OnlineRpcUnavailableError(f"Codex {method} returned a non-object result.")
            return result

    def wait_until_closed(self, thread_id: CodexThreadId, unsubscribe: Mapping[str, Any]) -> None:
        if unsubscribe.get("status") == "notLoaded":
            return
        while str(thread_id) not in self.closed_threads:
            self.receive()


def _configure_imported_thread(
    rpc: _NativeCodexRpc, thread_id: CodexThreadId, project_id: str, title: str, expect_items: bool,
) -> None:
    rpc.call("thread/metadata/update", {"threadId": str(thread_id), "projectId": project_id})
    rpc.call("thread/name/set", {"threadId": str(thread_id), "name": title})
    response = rpc.call("thread/read", {"threadId": str(thread_id), "includeTurns": True})
    thread = _extract_codex_rpc_thread({"result": response}, thread_id).thread
    if thread.get("id") != str(thread_id) or thread.get("projectId") != project_id:
        raise OnlineRpcUnavailableError("Codex imported thread identity or project did not match.")
    if expect_items and not any(turn.get("items") for turn in thread.get("turns", ())):
        raise OnlineRpcUnavailableError("Codex imported nonempty history without Desktop items.")


def import_codex_rollout_via_rpc(
    endpoint: CodexDaemonEndpoint, rollout_path: Path, workspace_path: Path, project_id: str, title: str,
    *, timeout_s: float = 90.0,
) -> CodexThreadId:
    """Return the actual imported ID only after native unload. Never retry an uncertain fork."""
    rows = [json.loads(row.raw_json) for row in read_codex_rollout_lines(rollout_path)]
    if not rows or rows[0].get("type") != "session_meta":
        raise ValueError("Codex import requires a rollout beginning with session_meta.")
    source_id = validate_session_id(str(rows[0]["payload"]["id"]), label="source_codex_thread_id")
    if not has_live_unix_socket(endpoint.socket_path):
        raise OnlineRpcUnavailableError("No live Codex app-server Unix socket available.")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    ws = _UnixWebSocketClient(sock)
    rpc = _NativeCodexRpc(sock, ws, time.monotonic() + timeout_s)
    thread_id: CodexThreadId | None = None
    fork_sent = False
    try:
        sock.connect(unix_socket_connect_path(endpoint.socket_path))
        ws.handshake()
        rpc.call("initialize", {"clientInfo": {"name": "codex-agy-sync", "version": __version__}, "capabilities": {"experimentalApi": True}})
        ws.send_json({"method": "initialized", "params": {}})
        fork_sent = True
        fork = rpc.call("thread/fork", {"threadId": source_id, "path": str(rollout_path.resolve()), "cwd": str(workspace_path.resolve()), "ephemeral": False})
        thread_id = CodexThreadId(validate_session_id(str(fork["thread"]["id"]), label="codex_thread_id"))
        try:
            _configure_imported_thread(rpc, thread_id, project_id, title, any(row.get("type") == "response_item" for row in rows))
        except (OnlineRpcUnavailableError, TimeoutError, OSError, KeyError, TypeError, ValueError):
            with contextlib.suppress(OnlineRpcUnavailableError, TimeoutError, OSError):
                rpc.call("thread/unsubscribe", {"threadId": str(thread_id)})
            raise
        unsubscribe = rpc.call("thread/unsubscribe", {"threadId": str(thread_id)})
        rpc.wait_until_closed(thread_id, unsubscribe)
        return thread_id
    except (OnlineRpcUnavailableError, TimeoutError, OSError, KeyError, TypeError, ValueError) as exc:
        if fork_sent:
            raise NativeCodexImportPending(f"Codex import requires reconciliation before retry: {exc}", thread_id) from exc
        raise OnlineRpcUnavailableError(f"Codex native import connection failed: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            ws.send_frame(0x8, b"")
        sock.close()
