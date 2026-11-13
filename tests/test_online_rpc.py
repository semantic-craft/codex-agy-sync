from __future__ import annotations

import base64
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

from codex_agy_sync.bridge import (
    HostEnvironmentPaths, OnlineRpcUnavailableError, SyncBridge, SyncDirection,
    TransportMode,
)
from codex_agy_sync.codecs import encode_agy_trajectory
from codex_agy_sync._wire import (
    encode_bytes_field, encode_string_field, encode_varint_field, iter_wire_fields,
)
from codex_agy_sync.ledger import (
    AssistantTextEvent, CascadeId, CodexThreadId, EventOriginAnchor, HostKind,
    ReadFilePayload, ThinkingEvent, ToolCallEvent, ToolCallId, ToolResultEvent,
    TrajectoryLedger, TurnId, UserInputEvent, parse_workspace_identity,
)


class TestUnit5OnlineRpcAndDocs(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace_dir = self.root / "unit5-workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.root / ".gemini" / "config",
            agy_data_dir=self.root / ".gemini" / "antigravity",
            agy_cli_data_dir=self.root / ".gemini" / "antigravity-cli",
            codex_home_dir=self.root / ("deep-sandbox-path-" + ("n" * 48)) / ".codex",
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
        self.ws_path, self.ws_uri = parse_workspace_identity(self.workspace_dir)
        self.bridge = SyncBridge(paths=self.paths)
        self.binding = self.bridge.pair_workspace(self.workspace_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _encode_rpc_trajectory_response(
        cid: CascadeId,
        events: list,
        *,
        run_status: int = 1,
        source_enum: int = 0,
    ) -> bytes:
        raw_rows = encode_agy_trajectory(events)
        traj_blob = (
            encode_string_field(1, str(cid))
            + b"".join(encode_bytes_field(2, r.step_payload_blob) for r in raw_rows)
            + encode_varint_field(8, source_enum)
        )
        return encode_bytes_field(1, traj_blob) + encode_varint_field(2, run_status)

    def test_5a_agy_loopback_connect_rpc_read_csrf_header_and_active_defer(self) -> None:
        cid_live = CascadeId("55550001-1111-4111-8111-111111111111")
        cid_running = CascadeId("55550002-1111-4111-8111-111111111111")
        cid_disk = CascadeId("55550003-1111-4111-8111-111111111111")
        cid_missing = CascadeId("55550004-1111-4111-8111-111111111111")
        secret_token = "unit5-csrf-ephemeral-secret-9988"

        read_payload = ReadFilePayload(kind="read_file", path="/workspace/src/main.py", start_line=1, end_line=20)
        live_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T10:00:00Z",
                ),
                query_text="Inspect live RPC trajectory",
            ),
            ThinkingEvent(
                kind="thinking",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T10:00:01Z",
                ),
                summary_text="Reading main.py from memory",
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T10:00:02Z",
                ),
                call_id=ToolCallId("call-rpc-5a"),
                tool_name="read_file",
                tool_summary="Read main.py",
                payload=read_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T10:00:03Z",
                ),
                call_id=ToolCallId("call-rpc-5a"),
                tool_name="read_file",
                output_text="def main() -> int:\n    return 0\n",
                is_error=False,
                payload=read_payload,
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T10:00:04Z",
                ),
                text="Live RPC turn completed cleanly.",
            ),
        ]
        running_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T10:05:00Z",
                ),
                query_text="Run long step",
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T10:05:01Z",
                ),
                call_id=ToolCallId("call-running-5a"),
                tool_name="read_file",
                tool_summary="Still running",
                payload=read_payload,
            ),
        ]
        disk_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T10:10:00Z",
                ),
                query_text="Disk fallback turn",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T10:10:01Z",
                ),
                text="Disk fallback reply.",
            ),
        ]
        offline_locks = self.bridge.guard.probe_locks(cid_disk, None)
        self.bridge._transport.apply_to_agy(
            self.binding,
            cid_disk,
            disk_events,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, disk_events, recompute_agy_step_indices=True),
            offline_locks,
            TransportMode.OFFLINE_STORAGE,
        )
        self.assertFalse((self.paths.agy_data_dir / "conversations" / f"{cid_live}.db").exists())

        server_state: dict[str, object] = {
            "force_status": 200,
            "captured": [],
            "blobs": {
                str(cid_live): self._encode_rpc_trajectory_response(cid_live, live_events, run_status=1),
                str(cid_running): self._encode_rpc_trajectory_response(cid_running, running_events, run_status=2),
            },
        }

        class _AgyRpcHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                req_cid = ""
                for fn, wt, _, raw in iter_wire_fields(body):
                    if fn == 1 and wt == 2:
                        req_cid = raw.decode("utf-8")
                captured_list = server_state["captured"]
                assert isinstance(captured_list, list)
                captured_list.append({
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "connect_ver": self.headers.get("Connect-Protocol-Version"),
                    "csrf": self.headers.get("x-codeium-csrf-token"),
                    "cascade_id": req_cid,
                })
                forced = int(server_state["force_status"])  # type: ignore[arg-type]
                if forced != 200:
                    self.send_response(forced)
                    self.end_headers()
                    self.wfile.write(b"rpc server fault")
                    return
                blobs = server_state["blobs"]
                assert isinstance(blobs, dict)
                payload = blobs.get(req_cid)
                if payload is None:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"code":"not_found","message":"cascade not found"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/proto")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AgyRpcHandler)
        srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        srv_thread.start()
        try:
            port = int(httpd.server_address[1])
            daemon_dir = self.paths.agy_data_dir / "daemon"
            daemon_dir.mkdir(parents=True, exist_ok=True)
            hub_live = daemon_dir / f"hub_{port}.json"
            hub_live.write_text(
                json.dumps({"pid": os.getpid(), "httpPort": port, "csrfToken": secret_token}),
                encoding="utf-8",
            )
            hub_stale = daemon_dir / "hub_1.json"
            hub_stale.write_text(
                json.dumps({"pid": os.getpid(), "httpPort": 1, "csrfToken": "unused-stale-port-token"}),
                encoding="utf-8",
            )
            os.utime(hub_stale, (hub_live.stat().st_mtime + 10.0, hub_live.stat().st_mtime + 10.0))

            with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}):
                inspected = self.bridge.inspect_session_ledger(
                    self.workspace_dir, cascade_id=cid_live, mode=TransportMode.ONLINE_RPC,
                )
                self.assertEqual(len(inspected.completed_turn_watermarks), 1)
                self.assertEqual([e.kind for e in inspected.events], [
                    "user_input", "thinking", "tool_call", "tool_result", "assistant_text",
                ])

                imported = self.bridge.sync_session(
                    self.workspace_dir,
                    cascade_id=cid_live,
                    direction=SyncDirection.AGY_TO_CODEX,
                    mode=TransportMode.AUTO,
                )
            self.assertEqual(imported.transition_kind, "initial_import")
            self.assertEqual(imported.transport_used, TransportMode.ONLINE_RPC)
            self.assertGreaterEqual(imported.events_applied, 5)

            captured_reqs = server_state["captured"]
            assert isinstance(captured_reqs, list)
            self.assertGreaterEqual(len(captured_reqs), 2)
            first_req = captured_reqs[0]
            self.assertEqual(first_req["path"], "/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory")
            self.assertEqual(first_req["content_type"], "application/proto")
            self.assertEqual(first_req["connect_ver"], "1")
            self.assertEqual(first_req["csrf"], secret_token)
            self.assertEqual(first_req["cascade_id"], str(cid_live))

            codex_ro = self.bridge.inspect_session_ledger(
                self.workspace_dir, codex_thread_id=imported.codex_thread_id, mode=TransportMode.OFFLINE_STORAGE,
            )
            self.assertEqual(len(codex_ro.completed_turn_watermarks), 1)

            running_rec = self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=cid_running,
                direction=SyncDirection.AGY_TO_CODEX,
                mode=TransportMode.AUTO,
                include_interrupted=True,
            )
            self.assertEqual(running_rec.transition_kind, "deferred_busy")
            self.assertEqual(running_rec.transport_used, TransportMode.ONLINE_RPC)

            with self.assertRaises(FileNotFoundError):
                self.bridge.inspect_session_ledger(
                    self.workspace_dir, cascade_id=cid_missing, mode=TransportMode.ONLINE_RPC,
                )

            server_state["force_status"] = 500
            with self.assertRaises(OnlineRpcUnavailableError) as exc_ctx:
                self.bridge.inspect_session_ledger(
                    self.workspace_dir, cascade_id=cid_live, mode=TransportMode.ONLINE_RPC,
                )
            self.assertNotIn(secret_token, str(exc_ctx.exception))
            self.assertNotIn(secret_token, repr(exc_ctx.exception))

            strict_rpc_defer = self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=cid_disk,
                direction=SyncDirection.AGY_TO_CODEX,
                mode=TransportMode.ONLINE_RPC,
            )
            self.assertEqual(strict_rpc_defer.transition_kind, "deferred_busy")
            self.assertEqual(strict_rpc_defer.transport_used, TransportMode.ONLINE_RPC)

            auto_fallback = self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=cid_disk,
                direction=SyncDirection.AGY_TO_CODEX,
                mode=TransportMode.AUTO,
            )
            self.assertEqual(auto_fallback.transition_kind, "initial_import")
            self.assertEqual(auto_fallback.transport_used, TransportMode.OFFLINE_STORAGE)
        finally:
            httpd.shutdown()
            httpd.server_close()
            srv_thread.join(timeout=2.0)

    @staticmethod
    def _ws_pack_server_frame(opcode: int, payload: bytes) -> bytes:
        plen = len(payload)
        first = 0x80 | (opcode & 0x0F)
        if plen < 126:
            return struct.pack("!BB", first, plen) + payload
        if plen < 65536:
            return struct.pack("!BBH", first, 126, plen) + payload
        return struct.pack("!BBQ", first, 127, plen) + payload

    @staticmethod
    def _ws_read_client_frame(conn: socket.socket, buf: bytearray) -> tuple[int, bool, bytes]:
        def _need(n: int) -> bytes:
            while len(buf) < n:
                chunk = conn.recv(max(4096, n - len(buf)))
                if not chunk:
                    raise EOFError("Client socket closed")
                buf.extend(chunk)
            out = bytes(buf[:n])
            del buf[:n]
            return out

        b0, b1 = _need(2)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        plen = b1 & 0x7F
        if plen == 126:
            (plen,) = struct.unpack("!H", _need(2))
        elif plen == 127:
            (plen,) = struct.unpack("!Q", _need(8))
        mask_key = _need(4) if masked else b""
        data = _need(plen) if plen > 0 else b""
        if masked and plen > 0:
            rep = (mask_key * ((plen // 4) + 1))[:plen]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(rep, "big")).to_bytes(plen, "big")
        return opcode, masked, data

    def test_5b_codex_unix_websocket_rpc_symlink_large_payload_and_a2_sync(self) -> None:
        tid_large = CodexThreadId("019a7700-1111-7000-8000-000000000001")
        tid_active = CodexThreadId("019a7700-2222-7000-8000-000000000002")
        large_reply = "L" * 72000

        ws_audit: dict[str, object] = {
            "all_masked": True,
            "pongs": [],
            "threads": {
                str(tid_large): {
                    "id": str(tid_large),
                    "name": "Large Live Codex Thread",
                    "status": {"type": "idle"},
                    "turns": [
                        {
                            "id": "turn-uuid-1",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "content": [{"type": "text", "text": "Generate >70KB analysis via RPC"}],
                                },
                                {
                                    "type": "reasoning",
                                    "summary": ["Synthesizing extended frame payload"],
                                },
                                {
                                    "type": "commandExecution",
                                    "id": "call-ws-cmd-1",
                                    "command": "echo ok",
                                    "aggregatedOutput": "ok\n",
                                    "exitCode": 0,
                                },
                                {
                                    "type": "agentMessage",
                                    "text": large_reply,
                                },
                            ],
                        }
                    ],
                },
                str(tid_active): {
                    "id": str(tid_active),
                    "name": "Streaming Active Codex Thread",
                    "status": {"type": "active"},
                    "turns": [
                        {
                            "id": "turn-uuid-active",
                            "status": {"type": "inProgress"},
                            "items": [
                                {
                                    "type": "userMessage",
                                    "content": [{"type": "text", "text": "Still streaming"}],
                                }
                            ],
                        }
                    ],
                },
            },
        }

        def _handle_ws_conn(conn: socket.socket) -> None:
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
                notif = self._ws_pack_server_frame(0x1, b'{"method":"server/hello","params":{}}')
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                    + notif
                )
                while True:
                    opcode, masked, data = self._ws_read_client_frame(conn, buf)
                    if not masked:
                        ws_audit["all_masked"] = False
                    if opcode == 0x8:
                        return
                    if opcode == 0xA:
                        pongs = ws_audit["pongs"]
                        assert isinstance(pongs, list)
                        pongs.append(data)
                        continue
                    if opcode != 0x1:
                        continue
                    msg = json.loads(data.decode("utf-8"))
                    method = msg.get("method")
                    if method == "initialize":
                        resp = json.dumps({"id": msg["id"], "result": {"serverInfo": {"name": "codex"}}}).encode("utf-8")
                        conn.sendall(self._ws_pack_server_frame(0x1, resp))
                    elif method == "thread/read":
                        conn.sendall(self._ws_pack_server_frame(0x9, b"ping-check"))
                        pong_op, pong_masked, pong_data = self._ws_read_client_frame(conn, buf)
                        if not pong_masked:
                            ws_audit["all_masked"] = False
                        if pong_op == 0xA:
                            pongs = ws_audit["pongs"]
                            assert isinstance(pongs, list)
                            pongs.append(pong_data)
                        req_tid = str((msg.get("params") or {}).get("threadId") or "")
                        threads_map = ws_audit["threads"]
                        assert isinstance(threads_map, dict)
                        t_obj = threads_map.get(req_tid)
                        if t_obj is None:
                            err = json.dumps({
                                "id": msg["id"],
                                "error": {"code": -32004, "message": "thread not found"},
                            }).encode("utf-8")
                            conn.sendall(self._ws_pack_server_frame(0x1, err))
                        else:
                            body = json.dumps({"id": msg["id"], "result": {"thread": t_obj}}).encode("utf-8")
                            conn.sendall(self._ws_pack_server_frame(0x1, body))
            except (EOFError, OSError):
                return
            finally:
                conn.close()

        stop_evt = threading.Event()
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="cas-u5-") as short_dir:
            real_sock_path = Path(short_dir) / "codex-daemon.sock"
            symlink_dir = self.paths.codex_home_dir / "app-server-control"
            symlink_dir.mkdir(parents=True, exist_ok=True)
            symlink_sock = symlink_dir / "app-server-control.sock"
            symlink_sock.symlink_to(real_sock_path)
            self.assertGreater(len(os.fsencode(str(symlink_sock))), 104)

            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(real_sock_path))
            srv.listen(8)
            srv.settimeout(0.2)

            def _accept_loop() -> None:
                while not stop_evt.is_set():
                    try:
                        client_conn, _ = srv.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    threading.Thread(target=_handle_ws_conn, args=(client_conn,), daemon=True).start()

            accept_thread = threading.Thread(target=_accept_loop, daemon=True)
            accept_thread.start()
            try:
                ep = self.bridge.guard._discover_codex_daemon()
                self.assertIsNotNone(ep)
                assert ep is not None
                self.assertEqual(ep.socket_path, symlink_sock)

                sync_rec = self.bridge.sync_session(
                    self.workspace_dir,
                    codex_thread_id=tid_large,
                    direction=SyncDirection.CODEX_TO_AGY,
                    mode=TransportMode.ONLINE_RPC,
                )
                self.assertEqual(sync_rec.transition_kind, "initial_import")
                self.assertEqual(sync_rec.transport_used, TransportMode.ONLINE_RPC)
                self.assertTrue(ws_audit["all_masked"])
                self.assertIn(b"ping-check", ws_audit["pongs"])  # type: ignore[arg-type]

                agy_ledger = self.bridge.inspect_session_ledger(
                    self.workspace_dir, cascade_id=sync_rec.cascade_id, mode=TransportMode.OFFLINE_STORAGE,
                )
                self.assertEqual(agy_ledger.title, "Large Live Codex Thread")
                assistant_texts = [e.text for e in agy_ledger.events if isinstance(e, AssistantTextEvent)]
                self.assertEqual(assistant_texts, [large_reply])

                active_rec = self.bridge.sync_session(
                    self.workspace_dir,
                    codex_thread_id=tid_active,
                    direction=SyncDirection.CODEX_TO_AGY,
                    mode=TransportMode.AUTO,
                    include_interrupted=True,
                )
                self.assertEqual(active_rec.transition_kind, "deferred_busy")
                self.assertEqual(active_rec.transport_used, TransportMode.ONLINE_RPC)
            finally:
                stop_evt.set()
                srv.close()
                accept_thread.join(timeout=2.0)
                if symlink_sock.is_symlink():
                    symlink_sock.unlink()

        cid_offline = CascadeId("55550009-1111-4111-8111-111111111111")
        off_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T11:00:00Z",
                ),
                query_text="Offline both sides check",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T11:00:01Z",
                ),
                text="Offline reply.",
            ),
        ]
        off_locks = self.bridge.guard.probe_locks(cid_offline, None)
        self.bridge._transport.apply_to_agy(
            self.binding,
            cid_offline,
            off_events,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, off_events, recompute_agy_step_indices=True),
            off_locks,
            TransportMode.OFFLINE_STORAGE,
        )
        off_rec = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid_offline,
            direction=SyncDirection.AGY_TO_CODEX,
            mode=TransportMode.AUTO,
        )
        self.assertEqual(off_rec.transition_kind, "initial_import")
        self.assertEqual(off_rec.transport_used, TransportMode.OFFLINE_STORAGE)
        self.assertNotEqual(off_rec.transport_used, TransportMode.AUTO)

        noop_off = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid_offline,
            codex_thread_id=off_rec.codex_thread_id,
            direction=SyncDirection.AUTO_INCREMENTAL,
            mode=TransportMode.AUTO,
        )
        self.assertEqual(noop_off.transition_kind, "no_op")
        self.assertEqual(noop_off.transport_used, TransportMode.OFFLINE_STORAGE)

        repo_root = Path(__file__).resolve().parent.parent
        for doc_rel in ("README.md", "NOTES.md", "docs/architecture.md", "lessons.md"):
            doc_txt = (repo_root / doc_rel).read_text(encoding="utf-8")
            self.assertNotIn("—", doc_txt, msg=f"Em-dash found in {doc_rel}")
