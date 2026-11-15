from __future__ import annotations

import base64
import dataclasses
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock
import uuid

from codex_agy_sync import (
    CodexThreadId,
    DefaultHostTransport,
    HostEnvironmentPaths,
    HostKind,
    OnlineRpcUnavailableError,
    PresenceWalGuard,
    ProjectRegistry,
    SyncBusyLockError,
    SyncDirection,
    TrajectoryLedger,
    TransportMode,
    diagnose_workspace_binding,
    encode_agy_create_project_request,
    execute_handoff,
    guarded_pair_workspace,
    pair_workspace_with_mode,
    parse_workspace_identity,
    register_agy_project_via_rpc,
    register_codex_project_via_rpc,
)
from codex_agy_sync._wire import (
    encode_bytes_field,
    encode_string_field,
    encode_varint_field,
    iter_wire_fields,
)
from codex_agy_sync.events import AssistantTextEvent, EventOriginAnchor, TurnId, UserInputEvent


def _decode_proto_fields(blob: bytes) -> dict[int, list[tuple[int, int, bytes]]]:
    fields: dict[int, list[tuple[int, int, bytes]]] = {}
    for fn, wt, ival, raw in iter_wire_fields(blob):
        fields.setdefault(fn, []).append((wt, ival, raw))
    return fields


def _pack_ws_server_frame(opcode: int, payload: bytes) -> bytes:
    plen, first = len(payload), 0x80 | (opcode & 0x0F)
    if plen < 126:
        return struct.pack("!BB", first, plen) + payload
    if plen < 65536:
        return struct.pack("!BBH", first, 126, plen) + payload
    return struct.pack("!BBQ", first, 127, plen) + payload


def _read_ws_client_frame(conn: socket.socket, buf: bytearray) -> tuple[int, bool, bytes]:
    def _exact(n: int) -> bytes:
        while len(buf) < n:
            chunk = conn.recv(max(4096, n - len(buf)))
            if not chunk:
                raise EOFError("Client closed WebSocket.")
            buf.extend(chunk)
        out = bytes(buf[:n])
        del buf[:n]
        return out

    b0, b1 = _exact(2)
    opcode, masked, plen = (b0 & 0x0F), bool(b1 & 0x80), (b1 & 0x7F)
    if plen == 126:
        (plen,) = struct.unpack("!H", _exact(2))
    elif plen == 127:
        (plen,) = struct.unpack("!Q", _exact(8))
    mask = _exact(4) if masked else b""
    payload = _exact(plen) if plen else b""
    if masked and plen:
        rep = (mask * ((plen // 4) + 1))[:plen]
        payload = (int.from_bytes(payload, "big") ^ int.from_bytes(rep, "big")).to_bytes(plen, "big")
    return opcode, masked, payload


class _MockAgyServer:
    def __init__(self, paths: HostEnvironmentPaths, secret: str, *, status_code: int = 200) -> None:
        self.paths = paths
        self.secret = secret
        self.status_code = status_code
        self.captured: list[dict[str, object]] = []
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._hub_file: Path | None = None
        self.port = 0

    def __enter__(self) -> "_MockAgyServer":
        parent = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                _ = format, args

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length)
                req_rec: dict[str, object] = {
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "connect_ver": self.headers.get("Connect-Protocol-Version"),
                    "csrf": self.headers.get("x-codeium-csrf-token"),
                    "body": body,
                }
                parent.captured.append(req_rec)
                if parent.status_code != 200 or self.headers.get("x-codeium-csrf-token") != parent.secret:
                    self.send_response(parent.status_code if parent.status_code != 200 else 401)
                    self.end_headers()
                    return
                if self.path == "/exa.language_server_pb.LanguageServerService/CreateProject":
                    parent._handle_create_project(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/proto")
                    self.end_headers()
                    return
                if self.path == "/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory":
                    resp = parent._handle_get_trajectory(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/proto")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    return
                self.send_response(404)
                self.end_headers()

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        daemon_dir = self.paths.agy_data_dir / "daemon"
        daemon_dir.mkdir(parents=True, exist_ok=True)
        self._hub_file = daemon_dir / f"hub_{self.port}.json"
        self._hub_file.write_text(
            json.dumps({"pid": os.getpid(), "httpPort": self.port, "csrfToken": self.secret}),
            encoding="utf-8",
        )
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc_type, exc, tb
        if self._hub_file is not None:
            self._hub_file.unlink(missing_ok=True)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _handle_create_project(self, body: bytes) -> None:
        top = _decode_proto_fields(body)
        proj = _decode_proto_fields(top[1][0][2])
        pid = proj[1][0][2].decode("utf-8")
        pname = proj[2][0][2].decode("utf-8")
        resources_msg = _decode_proto_fields(proj[6][0][2])
        res_msg = _decode_proto_fields(resources_msg[1][0][2])
        if 3 in res_msg:
            gf = _decode_proto_fields(res_msg[3][0][2])
            res_doc: dict[str, object] = {
                "gitFolder": {
                    "folderUri": gf[1][0][2].decode("utf-8"),
                    "defaultBranch": gf[2][0][2].decode("utf-8"),
                }
            }
        else:
            res_doc = {"folderUri": res_msg[1][0][2].decode("utf-8")}
        projects_dir = self.paths.gemini_config_dir / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        (projects_dir / f"{pid}.json").write_text(
            json.dumps({"id": pid, "name": pname, "projectResources": {"resources": [res_doc]}}),
            encoding="utf-8",
        )

    def _handle_get_trajectory(self, body: bytes) -> bytes:
        cid = _decode_proto_fields(body)[1][0][2].decode("utf-8")
        conv_db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        raw_steps = (
            [r.step_payload_blob for r in DefaultHostTransport(self.paths).read_persisted_agy_steps(conv_db)]
            if conv_db.is_file()
            else []
        )
        traj_blob = encode_string_field(1, cid) + b"".join(encode_bytes_field(2, s) for s in raw_steps)
        return encode_bytes_field(1, traj_blob) + encode_varint_field(2, 1)


class _MockCodexWsServer:
    def __init__(
        self,
        paths: HostEnvironmentPaths,
        *,
        initial_pages: list[dict[str, object]] | None = None,
        create_error: str | None = None,
    ) -> None:
        self.paths = paths
        self.pages = initial_pages or [{"data": [], "nextCursor": None}]
        self.create_error = create_error
        self.methods_called: list[str] = []
        self.create_params: list[dict[str, object]] = []
        self.created_id = str(uuid.uuid4())
        self._short_dir: tempfile.TemporaryDirectory[str] | None = None
        self._srv: socket.socket | None = None
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._symlink_sock: Path | None = None

    def __enter__(self) -> "_MockCodexWsServer":
        self._short_dir = tempfile.TemporaryDirectory(prefix="p-")
        real_sock = Path(self._short_dir.name) / "c.sock"
        symlink_dir = self.paths.codex_home_dir / "app-server-control"
        symlink_dir.mkdir(parents=True, exist_ok=True)
        self._symlink_sock = symlink_dir / "app-server-control.sock"
        self._symlink_sock.symlink_to(real_sock)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(real_sock))
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc_type, exc, tb
        self._stop.set()
        if self._srv is not None:
            self._srv.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
        if self._symlink_sock is not None:
            self._symlink_sock.unlink(missing_ok=True)
        if self._short_dir is not None:
            self._short_dir.cleanup()

    def _accept_loop(self) -> None:
        assert self._srv is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        buf = bytearray()
        try:
            conn.settimeout(3.0)
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf.extend(chunk)
            hdr_bytes, rest = bytes(buf).split(b"\r\n\r\n", 1)
            buf = bytearray(rest)
            ws_key = ""
            for line in hdr_bytes.decode("latin1").splitlines():
                if line.lower().startswith("sec-websocket-key:"):
                    ws_key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
            ).decode("ascii")
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii")
            )
            page_idx = 0
            while True:
                op, _, raw = _read_ws_client_frame(conn, buf)
                if op == 0x8:
                    return
                if op != 0x1:
                    continue
                msg = json.loads(raw.decode("utf-8"))
                method = str(msg.get("method") or "")
                self.methods_called.append(method)
                if method == "initialize":
                    conn.sendall(_pack_ws_server_frame(0x1, json.dumps({"id": msg["id"], "result": {}}).encode("utf-8")))
                elif method == "project/list":
                    page = self.pages[min(page_idx, len(self.pages) - 1)]
                    page_idx += 1
                    conn.sendall(_pack_ws_server_frame(0x1, json.dumps({"id": msg["id"], "result": page}).encode("utf-8")))
                elif method == "project/create":
                    self.create_params.append(dict(msg.get("params") or {}))
                    if self.create_error:
                        payload = {"id": msg["id"], "error": {"code": -32000, "message": self.create_error}}
                    else:
                        p = dict(msg.get("params") or {})
                        payload = {
                            "id": msg["id"],
                            "result": {"project": {"id": self.created_id, "name": p.get("name"), "roots": p.get("roots")}},
                        }
                    conn.sendall(_pack_ws_server_frame(0x1, json.dumps(payload).encode("utf-8")))
        except (EOFError, OSError):
            return
        finally:
            conn.close()


class TestOnlinePairingRpc(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "ws-alpha"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.root / ".gemini" / "config",
            agy_data_dir=self.root / ".gemini" / "antigravity",
            agy_cli_data_dir=self.root / ".gemini" / "antigravity-cli",
            codex_home_dir=self.root / ("deep-" + ("x" * 60)) / ".codex",
            sync_state_dir=self.root / ".codex-agy-sync",
        )
        for p in (
            self.paths.gemini_config_dir,
            self.paths.agy_data_dir,
            self.paths.agy_cli_data_dir,
            self.paths.codex_home_dir,
            self.paths.sync_state_dir,
        ):
            assert p is not None
            p.mkdir(parents=True, exist_ok=True)
        self.ws_path, self.ws_uri = parse_workspace_identity(self.workspace)
        self.registry = ProjectRegistry(self.paths)
        self.guard = PresenceWalGuard(self.paths)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _activate_agy_locks(self) -> Path:
        presence_dir = self.paths.agy_data_dir / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        (presence_dir / "active.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        wal = self.paths.agy_data_dir / "summaries.db-wal"
        wal.write_bytes(b"agy-wal-bytes")
        return wal

    def _activate_codex_locks(self) -> Path:
        (self.paths.codex_home_dir / "daemon.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        wal = self.paths.codex_home_dir / "state_5.sqlite-wal"
        wal.write_bytes(b"codex-wal-bytes")
        return wal

    def test_a_encode_and_agy_create_project_rpc_idempotency(self) -> None:
        plain_ws = self.root / "plain-proj"
        plain_ws.mkdir()
        plain_req = encode_agy_create_project_request(plain_ws, project_id="11111111-1111-4111-8111-111111111111")
        top_plain = _decode_proto_fields(plain_req)
        self.assertEqual(top_plain[2][0][1], 1)
        proj_plain = _decode_proto_fields(top_plain[1][0][2])
        self.assertEqual(proj_plain[1][0][2].decode("utf-8"), "11111111-1111-4111-8111-111111111111")
        self.assertEqual(proj_plain[2][0][2].decode("utf-8"), "plain-proj")
        self.assertEqual(proj_plain[12][0][1], 0)
        self.assertEqual(proj_plain[14][0][1], 1)
        res_plain = _decode_proto_fields(_decode_proto_fields(proj_plain[6][0][2])[1][0][2])
        self.assertEqual(res_plain[1][0][2].decode("utf-8"), parse_workspace_identity(plain_ws)[1])
        perm_plain = _decode_proto_fields(proj_plain[9][0][2])
        self.assertEqual(perm_plain[3][0][1], 1)

        git_ws = self.root / "git-proj"
        (git_ws / ".git").mkdir(parents=True)
        (git_ws / ".git" / "HEAD").write_text("ref: refs/heads/feat/unit1\n", encoding="utf-8")
        git_req = encode_agy_create_project_request(git_ws)
        git_proj = _decode_proto_fields(_decode_proto_fields(git_req)[1][0][2])
        git_res = _decode_proto_fields(_decode_proto_fields(git_proj[6][0][2])[1][0][2])
        git_folder = _decode_proto_fields(git_res[3][0][2])
        self.assertEqual(git_folder[1][0][2].decode("utf-8"), parse_workspace_identity(git_ws)[1])
        self.assertEqual(git_folder[2][0][2].decode("utf-8"), "feat/unit1")
        self.assertEqual(git_folder[3][0][1], 1)

        secret = "unit1-ephemeral-secret-42"
        with (
            mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}),
            _MockAgyServer(self.paths, secret) as agy_srv,
        ):
            pid1 = register_agy_project_via_rpc(self.paths, git_ws, registry=self.registry)
            pid2 = register_agy_project_via_rpc(self.paths, git_ws, registry=self.registry)
            self.assertEqual(pid1, pid2)
            self.assertEqual(len(agy_srv.captured), 1)
            req = agy_srv.captured[0]
            self.assertEqual(req["path"], "/exa.language_server_pb.LanguageServerService/CreateProject")
            self.assertEqual(req["content_type"], "application/proto")
            self.assertEqual(req["connect_ver"], "1")
            self.assertEqual(req["csrf"], secret)

        with _MockAgyServer(self.paths, secret, status_code=500):
            with self.assertRaises(OnlineRpcUnavailableError) as err_ctx:
                register_agy_project_via_rpc(self.paths, plain_ws)
            self.assertNotIn(secret, str(err_ctx.exception))

    def test_b_codex_project_list_hit_and_project_create_rpc(self) -> None:
        existing_id = str(uuid.uuid4())
        pages = [
            {"data": [{"id": str(uuid.uuid4()), "roots": [{"path": str(self.root / "other")}]}], "nextCursor": "p2"},
            {"data": [{"id": existing_id, "roots": [{"path": str(self.ws_path)}]}], "nextCursor": None},
        ]
        with _MockCodexWsServer(self.paths, initial_pages=pages) as ws_srv:
            found_id = register_codex_project_via_rpc(self.paths, self.workspace)
            self.assertEqual(found_id, existing_id)
            self.assertEqual(ws_srv.methods_called.count("project/list"), 2)
            self.assertEqual(ws_srv.methods_called.count("project/create"), 0)

        with _MockCodexWsServer(self.paths) as ws_srv:
            created_id = register_codex_project_via_rpc(self.paths, self.workspace)
            self.assertEqual(created_id, ws_srv.created_id)
            self.assertEqual(ws_srv.methods_called.count("project/create"), 1)
            cp = ws_srv.create_params[0]
            self.assertTrue(uuid.UUID(str(cp["idempotencyKey"])))
            self.assertEqual(cp["name"], self.ws_path.name)
            self.assertEqual(cp["roots"], [{"path": str(self.ws_path)}])
            self.assertIsNone(cp["metadata"])

        with _MockCodexWsServer(self.paths, create_error="db locked"):
            with self.assertRaises(OnlineRpcUnavailableError):
                register_codex_project_via_rpc(self.paths, self.workspace)

    def test_c_dual_online_pairing_bypasses_locked_wal_and_backups(self) -> None:
        agy_wal = self._activate_agy_locks()
        codex_wal = self._activate_codex_locks()
        state_db = self.paths.codex_home_dir / "state_5.sqlite"
        with (
            _MockAgyServer(self.paths, "dual-secret"),
            _MockCodexWsServer(self.paths) as codex_srv,
            mock.patch.object(self.guard, "acquire_offline_write_locks") as lock_spy,
            mock.patch.object(self.guard, "create_offline_backup") as backup_spy,
        ):
            binding, mode = pair_workspace_with_mode(
                self.paths, self.guard, self.registry, self.workspace, prefer_online_rpc=True,
            )
            binding2 = guarded_pair_workspace(
                self.paths, self.guard, self.registry, self.workspace, prefer_online_rpc=True,
            )
        self.assertEqual(mode, "online_rpc")
        self.assertEqual(str(binding.codex_project_id), codex_srv.created_id)
        self.assertEqual(str(binding2.agy_project_id), str(binding.agy_project_id))
        lock_spy.assert_not_called()
        backup_spy.assert_not_called()
        self.assertFalse(state_db.exists())
        self.assertEqual(agy_wal.read_bytes(), b"agy-wal-bytes")
        self.assertEqual(codex_wal.read_bytes(), b"codex-wal-bytes")

    def test_d_hybrid_pairing_both_directions(self) -> None:
        ws_one = self.root / "hybrid-one"
        ws_one.mkdir()
        self._activate_agy_locks()
        with _MockAgyServer(self.paths, "hybrid-secret"):
            binding_one, mode_one = pair_workspace_with_mode(
                self.paths, self.guard, self.registry, ws_one, prefer_online_rpc=True,
            )
        self.assertEqual(mode_one, "hybrid")
        self.assertIsNotNone(self.registry.find_readonly_agy_project_id(binding_one.workspace_uri))
        self.assertIsNotNone(self.registry.find_readonly_codex_project_id(binding_one.workspace_path))

        ws_two = self.root / "hybrid-two"
        ws_two.mkdir()
        (self.paths.agy_data_dir / "presence" / "active.lock").unlink(missing_ok=True)
        (self.paths.agy_data_dir / "summaries.db-wal").unlink(missing_ok=True)
        self._activate_codex_locks()
        with _MockCodexWsServer(self.paths) as codex_srv:
            binding_two, mode_two = pair_workspace_with_mode(
                self.paths, self.guard, self.registry, ws_two, prefer_online_rpc=True,
            )
        self.assertEqual(mode_two, "hybrid")
        self.assertEqual(str(binding_two.codex_project_id), codex_srv.created_id)
        self.assertIsNotNone(self.registry.find_readonly_agy_project_id(binding_two.workspace_uri))

    def test_e_strict_offline_refuses_when_locks_held(self) -> None:
        self._activate_agy_locks()
        self._activate_codex_locks()
        with (
            _MockAgyServer(self.paths, "strict-secret") as agy_srv,
            _MockCodexWsServer(self.paths) as codex_srv,
            self.assertRaises(SyncBusyLockError),
        ):
            guarded_pair_workspace(self.paths, self.guard, self.registry, self.workspace, prefer_online_rpc=False)
        self.assertEqual(agy_srv.captured, [])
        self.assertEqual(codex_srv.methods_called, [])

    def test_f_rpc_down_with_active_lock_raises_busy_without_wal_write(self) -> None:
        agy_wal = self._activate_agy_locks()
        with _MockAgyServer(self.paths, "dead-secret", status_code=503):
            with self.assertRaises(SyncBusyLockError):
                guarded_pair_workspace(self.paths, self.guard, self.registry, self.workspace, prefer_online_rpc=True)
        self.assertEqual(agy_wal.read_bytes(), b"agy-wal-bytes")
        self.assertIsNone(self.registry.find_readonly_agy_project_id(self.ws_uri))

        codex_wal = self._activate_codex_locks()
        with self.assertRaises(SyncBusyLockError):
            guarded_pair_workspace(self.paths, self.guard, self.registry, self.workspace, prefer_online_rpc=True)
        self.assertEqual(codex_wal.read_bytes(), b"codex-wal-bytes")

    def test_g_execute_handoff_auto_pair_vs_pair_if_offline(self) -> None:
        codex_only = self.registry.ensure_codex_binding(self.workspace)
        tid = CodexThreadId("01960000-0000-7000-8000-000000000101")
        events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(HostKind.CODEX, TurnId("turn-1"), "2026-11-14T01:00:00Z"),
                query_text="Hand off after online auto-pair",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(HostKind.CODEX, TurnId("turn-1"), "2026-11-14T01:00:02Z"),
                text="Ready for AGY.",
            ),
        ]
        ledger = TrajectoryLedger.from_events(codex_only.workspace_path, codex_only.workspace_uri, events, explicit_title_override="Online Pair Handoff")
        DefaultHostTransport(self.paths).apply_to_codex(
            codex_only, tid, ledger.events, ledger, self.guard.probe_locks(None, tid), TransportMode.OFFLINE_STORAGE,
        )
        diag = diagnose_workspace_binding(self.paths, self.workspace)
        diag_text = "\n".join(diag.remediation_steps)
        self.assertIn("--auto-pair", diag_text)
        self.assertIn("--pair-if-offline", diag_text)
        self.assertIn("codex-agy-sync pair -w", diag_text)

        self._activate_agy_locks()
        with _MockAgyServer(self.paths, "handoff-secret"):
            with self.assertRaises(SyncBusyLockError):
                execute_handoff(
                    self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY,
                    source_id=str(tid), pair_if_offline=True,
                )
            receipt, mutated = execute_handoff(
                self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY,
                source_id=str(tid), auto_pair=True,
            )
        self.assertTrue(mutated)
        self.assertEqual(receipt.status, "ready")
        self.assertEqual(receipt.pairing_mode, "online_rpc")
        self.assertEqual(dataclasses.asdict(receipt)["pairing_mode"], "online_rpc")
        self.assertTrue(diagnose_workspace_binding(self.paths, self.workspace).is_paired())

    def test_p2_unix_ws_client_rejects_oversized_frame_above_64mib(self) -> None:
        from codex_agy_sync.bridge.project_rpc import UnixWebSocketClient

        srv_sock, cli_sock = socket.socketpair()
        try:
            cli_sock.settimeout(2.0)
            client = UnixWebSocketClient(cli_sock)
            oversized_header = struct.pack("!BBQ", 0x81, 127, 67_108_865)
            srv_sock.sendall(oversized_header)
            with self.assertRaises(OnlineRpcUnavailableError):
                client.recv_json()
        finally:
            cli_sock.close()
            srv_sock.close()
