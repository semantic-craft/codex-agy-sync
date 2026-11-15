"""Loopback online project registration RPC helpers for Antigravity and Codex."""

from __future__ import annotations

import base64
import contextlib
import json
import os
from pathlib import Path
import socket
import struct
import time
from typing import Any, Iterator, Mapping, Protocol
import urllib.error
import urllib.request
import uuid

from .._wire import encode_bytes_field, encode_string_field, encode_varint_field
from ..workspace import (
    PACKAGE_VERSION,
    AgyProjectId,
    WorkspacePath,
    WorkspaceRootUri,
    parse_workspace_identity,
)
from .paths_and_locks import (
    AgyDaemonEndpoint,
    CodexDaemonEndpoint,
    HostEnvironmentPaths,
    PresenceWalGuard,
    has_live_unix_socket,
    unix_socket_connect_path,
)
from .store_catalog import find_readonly_agy_project_id


class OnlineRpcUnavailableError(RuntimeError):
    """Raised when an online loopback RPC endpoint is unreachable or fails transport."""


class AgyProjectLookup(Protocol):
    """Read-only lookup interface for registered Antigravity projects."""

    def find_readonly_agy_project_id(self, ws_uri: WorkspaceRootUri) -> AgyProjectId | None: ...


def _xor_websocket_mask(payload: bytes, mask_key: bytes) -> bytes:
    if not (plen := len(payload)):
        return b""
    repeated = (mask_key * ((plen // 4) + 1))[:plen]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(plen, "big")


class UnixWebSocketClient:
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
            if plen > 67_108_864 or len(fragments) + plen > 67_108_864:
                raise OnlineRpcUnavailableError("Codex WebSocket frame exceeds 64 MiB limit.")
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


@contextlib.contextmanager
def open_codex_ws_rpc(endpoint: CodexDaemonEndpoint | None, *, timeout_s: float = 2.0) -> Iterator[UnixWebSocketClient]:
    """Connect and run `initialize`/`initialized` handshake with `codex app-server` over AF_UNIX."""
    if endpoint is None or not has_live_unix_socket(endpoint.socket_path):
        raise OnlineRpcUnavailableError("No live Codex app-server Unix socket available.")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    ws = UnixWebSocketClient(sock)
    try:
        sock.connect(unix_socket_connect_path(endpoint.socket_path))
        ws.handshake()
        ws.send_json({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "codex-agy-sync", "version": PACKAGE_VERSION},
                "capabilities": {"experimentalApi": True},
            },
        })
        if "error" in ws.wait_for_id(1):
            raise OnlineRpcUnavailableError("Codex app-server initialize RPC failed.")
        ws.send_json({"method": "initialized", "params": {}})
        yield ws
    except (TimeoutError, OSError):
        raise OnlineRpcUnavailableError("Codex app-server Unix socket connection failed.") from None
    finally:
        with contextlib.suppress(OSError):
            ws.send_frame(0x8, b"")
        sock.close()


def _detect_git_default_branch(ws_path: WorkspacePath) -> str | None:
    git_entry = ws_path / ".git"
    if not git_entry.exists():
        return None
    git_dir: Path | None = git_entry if git_entry.is_dir() else None
    if git_dir is None and git_entry.is_file():
        with contextlib.suppress(OSError):
            first_line = git_entry.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            if first_line.lower().startswith("gitdir:"):
                raw_dir = first_line.split(":", 1)[1].strip()
                cand = Path(raw_dir)
                git_dir = cand if cand.is_absolute() else (ws_path / cand).resolve()
    if git_dir is not None and (head_file := git_dir / "HEAD").is_file():
        with contextlib.suppress(OSError):
            head_text = head_file.read_text(encoding="utf-8", errors="ignore").strip()
            if head_text.startswith("ref: refs/heads/"):
                branch = head_text[len("ref: refs/heads/"):].strip()
                if branch:
                    return branch
    return "main"


def encode_agy_create_project_request(
    workspace: str | Path,
    *,
    project_id: str | None = None,
    name: str | None = None,
    backend: int = 1,
    is_workspace_only: bool = False,
) -> bytes:
    """Encode an `exa.language_server_pb.CreateProjectRequest` wire payload."""
    ws_path, ws_uri = parse_workspace_identity(workspace)
    pid = str(project_id).strip() if project_id else str(uuid.uuid4())
    proj_name = str(name) if name is not None else (ws_path.name or str(ws_path))
    branch = _detect_git_default_branch(ws_path)
    if branch is not None:
        git_folder_bytes = (
            encode_string_field(1, str(ws_uri))
            + encode_string_field(2, branch)
            + encode_varint_field(3, 1)
        )
        resource_bytes = encode_bytes_field(3, git_folder_bytes)
    else:
        resource_bytes = encode_bytes_field(1, str(ws_uri).encode("utf-8"))
    resources_bytes = encode_bytes_field(1, resource_bytes)
    perm_grants_bytes = encode_varint_field(3, 1)
    project_bytes = (
        encode_string_field(1, pid)
        + encode_string_field(2, proj_name)
        + encode_bytes_field(6, resources_bytes)
        + encode_bytes_field(9, perm_grants_bytes)
        + encode_varint_field(12, 1 if is_workspace_only else 0)
        + encode_varint_field(14, int(backend))
    )
    return encode_bytes_field(1, project_bytes) + encode_varint_field(2, int(backend))


def _lookup_agy_project(
    paths: HostEnvironmentPaths, ws_uri: WorkspaceRootUri, registry: AgyProjectLookup | None,
) -> AgyProjectId | None:
    if registry is not None:
        return registry.find_readonly_agy_project_id(ws_uri)
    return find_readonly_agy_project_id(paths, ws_uri)


def _wait_for_agy_project_id(
    paths: HostEnvironmentPaths,
    ws_uri: WorkspaceRootUri,
    registry: AgyProjectLookup | None,
    *,
    timeout: float,
) -> AgyProjectId | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        found = _lookup_agy_project(paths, ws_uri, registry)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _post_agy_create_project(endpoint: AgyDaemonEndpoint, payload: bytes, *, timeout: float) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{endpoint.http_port}/exa.language_server_pb.LanguageServerService/CreateProject"
    headers = {
        "Content-Type": "application/proto",
        "Connect-Protocol-Version": "1",
        **endpoint.csrf_secret.as_loopback_header(),
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read()
        return 200
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(Exception):
            exc.close()
        if exc.code == 409:
            return 409
        raise OnlineRpcUnavailableError(f"Antigravity CreateProject RPC failed with HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise OnlineRpcUnavailableError("Antigravity CreateProject RPC connection failed.") from None


def _existing_agy_project_names(paths: HostEnvironmentPaths) -> set[str]:
    projects_dir = paths.gemini_config_dir / "projects"
    if not projects_dir.is_dir():
        return set()
    names: set[str] = set()
    with contextlib.suppress(OSError):
        for pfile in sorted(projects_dir.glob("*.json")):
            if not pfile.is_file():
                continue
            with contextlib.suppress(OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                payload = json.loads(pfile.read_text(encoding="utf-8"))
                if isinstance(payload, Mapping) and (name := str(payload.get("name") or "").strip()):
                    names.add(name)
    return names


def _agy_project_name_candidates(ws_path: Path, project_id: str, existing_names: set[str]) -> list[str]:
    base_name = ws_path.name or str(ws_path)
    parent_name = ws_path.parent.name.strip()
    fallback = f"{base_name} ({project_id[:8]})"
    raw = [base_name]
    if parent_name:
        raw.append(f"{base_name} ({parent_name})")
    raw.append(fallback)
    deduped = list(dict.fromkeys(raw))
    return [cand for cand in deduped if cand == fallback or cand not in existing_names]


def register_agy_project_via_rpc(
    paths: HostEnvironmentPaths,
    workspace: str | Path,
    *,
    registry: AgyProjectLookup | None = None,
    timeout: float = 5.0,
) -> str:
    """Register `workspace` in live Antigravity LanguageServer via `CreateProject` RPC."""
    ws_path, ws_uri = parse_workspace_identity(workspace)
    endpoint = PresenceWalGuard(paths).discover_agy_daemon()
    if endpoint is None or endpoint.http_port <= 0:
        raise OnlineRpcUnavailableError("No live Antigravity LanguageServer HTTP endpoint available.")
    existing = _lookup_agy_project(paths, ws_uri, registry)
    if existing is not None:
        return str(existing)
    project_id = str(uuid.uuid4())
    candidates = _agy_project_name_candidates(ws_path, project_id, _existing_agy_project_names(paths))
    for candidate in candidates:
        payload = encode_agy_create_project_request(ws_path, project_id=project_id, name=candidate)
        status = _post_agy_create_project(endpoint, payload, timeout=timeout)
        if status == 200:
            confirmed = _wait_for_agy_project_id(paths, ws_uri, registry, timeout=min(timeout, 0.25))
            return str(confirmed) if confirmed is not None else project_id
        concurrent_existing = _lookup_agy_project(paths, ws_uri, registry)
        if concurrent_existing is not None:
            return str(concurrent_existing)
    raise OnlineRpcUnavailableError("Antigravity CreateProject RPC failed with HTTP 409.")


def _codex_project_matches_workspace(project: Any, target_path: WorkspacePath) -> str | None:
    if not isinstance(project, Mapping):
        return None
    proj_id = str(project.get("id") or "").strip()
    if not proj_id:
        return None
    roots = project.get("roots")
    if not isinstance(roots, (list, tuple)):
        return None
    for root in roots:
        raw = root.get("path") if isinstance(root, Mapping) else root
        if not raw:
            continue
        try:
            norm_path, _ = parse_workspace_identity(str(raw))
        except ValueError:
            continue
        if norm_path == target_path:
            return proj_id
    return None


def _find_codex_project_via_ws(
    ws: UnixWebSocketClient, target_path: WorkspacePath, *, start_req_id: int = 2,
) -> tuple[str | None, int]:
    req_id = start_req_id
    cursor: str | None = None
    for _ in range(64):
        ws.send_json({"id": req_id, "method": "project/list", "params": {"cursor": cursor, "limit": 100}})
        resp = ws.wait_for_id(req_id)
        req_id += 1
        if "error" in resp:
            raise OnlineRpcUnavailableError("Codex project/list RPC failed.")
        result = resp.get("result")
        if not isinstance(result, Mapping):
            raise OnlineRpcUnavailableError("Invalid Codex project/list RPC result.")
        items = result.get("data") or result.get("projects") or result.get("items") or ()
        for item in items:
            if (matched_id := _codex_project_matches_workspace(item, target_path)) is not None:
                return matched_id, req_id
        next_cursor = result.get("nextCursor")
        if not next_cursor or str(next_cursor) == str(cursor):
            break
        cursor = str(next_cursor)
    return None, req_id


def register_codex_project_via_rpc(
    paths: HostEnvironmentPaths,
    workspace: str | Path,
    *,
    timeout: float = 5.0,
) -> str:
    """Register `workspace` in live `codex app-server` via `project/list` and `project/create` RPC."""
    ws_path, _ = parse_workspace_identity(workspace)
    endpoint = PresenceWalGuard(paths).discover_codex_daemon()
    with open_codex_ws_rpc(endpoint, timeout_s=timeout) as ws:
        existing_id, next_id = _find_codex_project_via_ws(ws, ws_path, start_req_id=2)
        if existing_id is not None:
            return existing_id
        ws.send_json({
            "id": next_id,
            "method": "project/create",
            "params": {
                "idempotencyKey": str(uuid.uuid4()),
                "name": ws_path.name or str(ws_path),
                "roots": [{"path": str(ws_path)}],
                "metadata": None,
            },
        })
        create_resp = ws.wait_for_id(next_id)
        if "error" in create_resp:
            raise OnlineRpcUnavailableError("Codex project/create RPC failed.")
        result = create_resp.get("result")
        proj = result.get("project") if isinstance(result, Mapping) else None
        proj_id = str(proj.get("id") or "").strip() if isinstance(proj, Mapping) else ""
        if not proj_id:
            raise OnlineRpcUnavailableError("Codex project/create RPC did not return project id.")
        return proj_id
