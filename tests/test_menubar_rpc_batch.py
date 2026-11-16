"""Tests for batch GetAllCascadeTrajectories RPC and fast AGY subagent sidecar ID discovery."""

from __future__ import annotations

from contextlib import closing, contextmanager
import http.client, http.server, json, os, sqlite3, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch

from codex_agy_sync import (
    AgyDaemonEndpoint, AssistantTextEvent, CascadeId, CsrfSecret, DefaultHostTransport,
    EventOriginAnchor, HostEnvironmentPaths, HostKind, MenubarCandidateState, MenubarFeedSnapshot,
    PresenceWalGuard, SyncBridge, TrajectoryLedger, TransportMode, UserInputEvent,
    build_menubar_feed, fetch_agy_run_statuses_via_rpc,
)
from codex_agy_sync._wire import encode_bytes_field, encode_string_field, encode_varint_field, iter_wire_fields
from codex_agy_sync.bridge.rpc import _post_agy_proto
from codex_agy_sync.bridge.store_catalog import discover_agy_subagent_sidecar_ids, discover_agy_subagent_sidecars
from codex_agy_sync.workspace import TurnId

_NOW_EPOCH = 1_794_730_000.0
_BATCH_PATH = "/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories"
_SINGLE_PATH = "/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory"


def _encode_batch_response(entries: list[tuple[str, int | None]]) -> bytes:
    chunks: list[bytes] = []
    for cid, status in entries:
        summary = encode_varint_field(5, status) if status is not None else b""
        chunks.append(encode_bytes_field(1, encode_string_field(1, cid) + encode_bytes_field(2, summary)))
    return b"".join(chunks)


def _encode_single_response(cid: str, status: int) -> bytes:
    step_blob = encode_varint_field(1, 14) + encode_varint_field(4, 1)
    traj = encode_string_field(1, cid) + encode_bytes_field(2, step_blob)
    return encode_bytes_field(1, traj) + encode_varint_field(2, status)


class MenubarRpcBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="menubar-rpc-batch-")
        self.root = Path(self.temp.name)
        home = self.root / "home"
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=home / ".gemini",
            agy_data_dir=home / ".gemini" / "antigravity",
            codex_home_dir=home / ".codex",
            sync_state_dir=home / ".codex-agy-sync",
        )
        for d in (
            self.paths.gemini_config_dir / "projects",
            self.paths.agy_data_dir / "conversations",
            self.paths.codex_home_dir / "sessions",
            self.paths.sync_state_dir / "handoffs",
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.ws_a = (self.root / "ws-alpha").resolve()
        self.ws_a.mkdir(parents=True, exist_ok=True)
        self.bridge = SyncBridge(paths=self.paths)
        self.transport = DefaultHostTransport(self.paths)
        self.guard = PresenceWalGuard(self.paths)
        self.binding_a = self.bridge.pair_workspace(self.ws_a, prefer_online_rpc=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_agy(self, cid: str, title: str, *, mtime: float, inflight: bool = False) -> None:
        a1 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("t1"), "2026-11-15T08:00:00Z")
        events: list = [UserInputEvent(kind="user_input", anchor=a1, query_text="q"), AssistantTextEvent(kind="assistant_text", anchor=a1, text="a")]
        if inflight:
            a2 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("t2"), "2026-11-15T08:02:00Z")
            events.append(UserInputEvent(kind="user_input", anchor=a2, query_text="stopped"))
        ledger = TrajectoryLedger.from_events(self.binding_a.workspace_path, self.binding_a.workspace_uri, events, explicit_title_override=title)
        locks = self.guard.probe_locks(CascadeId(cid), None)
        self.transport.apply_to_agy(self.binding_a, CascadeId(cid), events, ledger, locks, TransportMode.OFFLINE_STORAGE)
        db_file = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        if inflight:
            with closing(sqlite3.connect(db_file)) as conn, conn:
                conn.execute("UPDATE steps SET status = 2 WHERE idx = (SELECT MAX(idx) FROM steps)")
        os.utime(db_file, (mtime, mtime))

    def _feed_with_ep(self, ep: AgyDaemonEndpoint) -> MenubarFeedSnapshot:
        with (
            patch.object(PresenceWalGuard, "discover_agy_daemon", return_value=ep),
            patch.object(PresenceWalGuard, "discover_codex_daemon", return_value=None),
        ):
            return build_menubar_feed(self.paths, now_epoch=_NOW_EPOCH)

    @contextmanager
    def _rpc_server(self, batch_status: int, batch_entries: list[tuple[str, int | None]], single_statuses: dict[str, int]):
        state: dict[str, object] = {"connections": 0, "requests": []}
        lock = threading.Lock()
        batch_body = _encode_batch_response(batch_entries)

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                with lock:
                    state["connections"] = int(state["connections"]) + 1  # type: ignore[arg-type]

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                raw_req = self.rfile.read(int(self.headers.get("Content-Length") or "0"))
                req_cid = next((raw.decode("utf-8") for fn, wt, _, raw in iter_wire_fields(raw_req) if fn == 1 and wt == 2), "")
                with lock:
                    state["requests"].append((self.path, str(self.headers.get("Connection") or ""), req_cid))  # type: ignore[attr-defined]
                if self.path == _BATCH_PATH:
                    code, body = (200, batch_body) if batch_status == 200 else (batch_status, b'{"code":"unimplemented"}')
                elif self.path == _SINGLE_PATH and req_cid in single_statuses:
                    code, body = 200, _encode_single_response(req_cid, single_statuses[req_cid])
                else:
                    code, body = 404, b'{"code":"not_found"}'
                self.send_response(code)
                self.send_header("Content-Type", "application/proto")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        srv.daemon_threads = True
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        ep = AgyDaemonEndpoint(os.getpid(), srv.server_address[1], srv.server_address[1] + 1, "1.0", CsrfSecret("tok"))
        try:
            yield ep, state
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2.0)

    def test_batch_rpc_classifies_statuses_with_single_call(self) -> None:
        c_idle = "10000000-0000-4000-8000-000000000001"
        c_run = "10000000-0000-4000-8000-000000000002"
        c_busy = "10000000-0000-4000-8000-000000000003"
        c_omit = "10000000-0000-4000-8000-000000000004"
        self._write_agy(c_idle, "Idle Stopped", mtime=_NOW_EPOCH - 60.0, inflight=True)
        self._write_agy(c_run, "Running", mtime=_NOW_EPOCH - 50.0)
        self._write_agy(c_busy, "Busy", mtime=_NOW_EPOCH - 40.0)
        self._write_agy(c_omit, "Omitted Status", mtime=_NOW_EPOCH - 30.0)

        with self._rpc_server(200, [(c_idle, 1), (c_run, 2), (c_busy, 4), (c_omit, None)], {}) as (ep, state):
            snap = self._feed_with_ep(ep)
            feed_reqs = list(state["requests"])  # type: ignore[arg-type]
            direct = fetch_agy_run_statuses_via_rpc(ep)

        assert snap.hero is not None
        self.assertEqual((snap.hero.session_id, snap.hero.state), (c_idle, MenubarCandidateState.INTERRUPTED_STOPPED))
        by_id = {it.session_id: it for it in snap.recent}
        self.assertEqual(by_id[c_run].state, MenubarCandidateState.ACTIVE_RUNNING)
        self.assertEqual(by_id[c_busy].state, MenubarCandidateState.ACTIVE_RUNNING)
        self.assertEqual(by_id[c_omit].state, MenubarCandidateState.COMPLETED_READY)
        self.assertEqual(direct, {c_idle: 1, c_run: 2, c_busy: 4, c_omit: 0})
        self.assertEqual(len(feed_reqs), 1)
        self.assertEqual((feed_reqs[0][0], feed_reqs[0][1].lower()), (_BATCH_PATH, "close"))

    def test_batch_rpc_404_falls_back_to_single_with_keepalive(self) -> None:
        c_idle = "20000000-0000-4000-8000-000000000001"
        c_run = "20000000-0000-4000-8000-000000000002"
        self._write_agy(c_idle, "Fallback Idle", mtime=_NOW_EPOCH - 40.0, inflight=True)

        with self._rpc_server(404, [], {c_idle: 1, c_run: 2}) as (ep, state):
            snap_one = self._feed_with_ep(ep)
            self.assertEqual(state["connections"], 2)
            self.assertEqual(state["requests"], [(_BATCH_PATH, "close", ""), (_SINGLE_PATH, "keep-alive", c_idle)])
            self._write_agy(c_run, "Fallback Run", mtime=_NOW_EPOCH - 20.0)
            state["requests"] = []
            snap_two = self._feed_with_ep(ep)
            reqs_two = list(state["requests"])  # type: ignore[arg-type]

        assert snap_one.hero is not None and snap_two.hero is not None
        self.assertEqual(snap_one.hero.state, MenubarCandidateState.INTERRUPTED_STOPPED)
        self.assertEqual(snap_two.hero.session_id, c_idle)
        self.assertEqual((snap_two.recent[0].session_id, snap_two.recent[0].state), (c_run, MenubarCandidateState.ACTIVE_RUNNING))
        self.assertEqual(reqs_two[0], (_BATCH_PATH, "close", ""))
        self.assertEqual({(p, conn.lower(), cid) for p, conn, cid in reqs_two[1:]}, {(_SINGLE_PATH, "keep-alive", c_idle), (_SINGLE_PATH, "keep-alive", c_run)})

    def test_missing_cascade_in_batch_map_triggers_single_fallback_only(self) -> None:
        c_done = "30000000-0000-4000-8000-000000000001"
        c_run = "30000000-0000-4000-8000-000000000002"
        c_miss = "30000000-0000-4000-8000-000000000003"
        self._write_agy(c_done, "Batch Done", mtime=_NOW_EPOCH - 90.0)
        self._write_agy(c_run, "Batch Run", mtime=_NOW_EPOCH - 60.0)
        self._write_agy(c_miss, "Missing From Batch", mtime=_NOW_EPOCH - 30.0, inflight=True)

        with self._rpc_server(200, [(c_done, 1), (c_run, 2)], {c_miss: 1}) as (ep, state):
            snap = self._feed_with_ep(ep)
            self.assertEqual(state["connections"], 2)
            self.assertEqual(state["requests"], [(_BATCH_PATH, "close", ""), (_SINGLE_PATH, "keep-alive", c_miss)])

        assert snap.hero is not None
        self.assertEqual((snap.hero.session_id, snap.hero.state), (c_miss, MenubarCandidateState.INTERRUPTED_STOPPED))
        by_id = {it.session_id: it for it in snap.recent}
        self.assertEqual(by_id[c_run].state, MenubarCandidateState.ACTIVE_RUNNING)
        self.assertEqual(by_id[c_done].state, MenubarCandidateState.COMPLETED_READY)

    def test_discover_agy_subagent_sidecar_ids_parity(self) -> None:
        brain = self.paths.agy_data_dir / "brain"
        valid_sub = brain / "aaaaaaaa-1111-4111-8111-111111111111" / ".system_generated" / "subagents"
        bad_parent_sub = brain / "bad..parent" / ".system_generated" / "subagents"
        valid_sub.mkdir(parents=True, exist_ok=True)
        bad_parent_sub.mkdir(parents=True, exist_ok=True)

        u1, u2 = "bbbbbbbb-2222-4222-8222-222222222222", "cccccccc-3333-4333-8333-333333333333"
        (valid_sub / f"{u1}.json").write_text(json.dumps({"conversationId": u1}), encoding="utf-8")
        (valid_sub / "worker-alpha.json").write_text(json.dumps({"conversationId": u2}), encoding="utf-8")
        (valid_sub / "custom-child.json").write_text("{}", encoding="utf-8")
        (valid_sub / "eeeeeeee-5555-4555-8555-555555555555.json").write_text("", encoding="utf-8")
        (valid_sub / "child.json").write_text(json.dumps({"conversationId": "../evil"}), encoding="utf-8")
        (valid_sub / "corrupt.json").write_text("{not-json", encoding="utf-8")
        (valid_sub / "list.json").write_text("[1, 2]", encoding="utf-8")
        (valid_sub / "ignore.txt").write_text("{}", encoding="utf-8")
        (valid_sub / "dir.json").mkdir()
        (bad_parent_sub / "dddddddd-4444-4444-8444-444444444444.json").write_text(
            json.dumps({"conversationId": "dddddddd-4444-4444-8444-444444444444"}), encoding="utf-8",
        )

        fast_ids = discover_agy_subagent_sidecar_ids(self.paths)
        self.assertEqual(fast_ids, set(discover_agy_subagent_sidecars(self.paths).keys()))
        self.assertEqual(fast_ids, {u1, u2, "custom-child"})

    def test_post_agy_proto_reused_conn_timeout_does_not_retry(self) -> None:
        hits: list[str] = []

        class _SlowHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, format: str, *args: object) -> None:
                return
            def do_POST(self) -> None:
                _ = self.rfile.read(int(self.headers.get("Content-Length") or "0"))
                hits.append(self.path)
                if self.path == "/slow":
                    time.sleep(0.3)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        ep = AgyDaemonEndpoint(os.getpid(), srv.server_address[1], srv.server_address[1] + 1, "1.0", CsrfSecret("tok"))
        conn = http.client.HTTPConnection("127.0.0.1", ep.http_port, timeout=1.0)
        try:
            self.assertEqual(_post_agy_proto(ep, "/fast", b"", 1.0, conn), (200, b"ok"))
            self.assertIsNotNone(conn.sock)
            with self.assertRaises(TimeoutError):
                _post_agy_proto(ep, "/slow", b"", 0.05, conn)
            self.assertIsNone(conn.sock)
            self.assertEqual(hits, ["/fast", "/slow"])
        finally:
            conn.close()
            srv.shutdown()
            srv.server_close()
            t.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
