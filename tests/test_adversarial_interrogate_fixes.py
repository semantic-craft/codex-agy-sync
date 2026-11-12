from __future__ import annotations

import base64
import compression.zstd as zstd
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest import mock

from codex_agy_sync import (
    INTERRUPTED_TURN_TEXT, AssistantTextEvent, CascadeId, CodexThreadId, EmptySessionError, EventOriginAnchor,
    FileEditPayload, ForkResolutionPolicy, HostEnvironmentPaths, HostKind, MediaAttachment,
    OpaqueToolPayload, RunCommandPayload, StepIndex, SyncBridge, SyncBusyLockError, SyncDirection,
    ToolCallEvent, ToolCallId, ToolResultEvent, TrajectoryLedger, TransportMode,
    TurnId, UserInputEvent, normalize_tool_file_path, parse_workspace_identity,
)
from codex_agy_sync.bridge.paths_and_locks import _is_sqlite_shm_wal_locked, _open_nonblocking_ex_flock
from codex_agy_sync.cli import main as cli_main
from codex_agy_sync.codecs import (
    decode_agy_steps_to_ledger, decode_codex_rpc_thread_to_ledger, encode_events_to_agy_steps,
    encode_ledger_to_codex_rollout, read_codex_rollout_lines,
)
from codex_agy_sync.codecs.tool_payloads import decode_unified_diff_block, encode_unified_diff_block
from tests._helpers import BridgeSandboxBase


def _render_rollout_jsonl(ledger: TrajectoryLedger, tid: CodexThreadId) -> str:
    return "".join(f"{ln.raw_json}\n" for ln in encode_ledger_to_codex_rollout(ledger, session_id=str(tid)))


def _turn_events(
    host: HostKind, turn_id: str, user_text: str, reply_text: str | None = None, *,
    ts_base: str = "2026-11-12T10:00:0", tool_call_id: str | None = None,
    tool_output: str | None = None, media: tuple[MediaAttachment, ...] = (),
) -> list[UserInputEvent | ToolCallEvent | ToolResultEvent | AssistantTextEvent]:
    anchor = lambda sfx: EventOriginAnchor(origin_host=host, turn_id=TurnId(turn_id), timestamp_iso=f"{ts_base}{sfx}Z")
    events: list[UserInputEvent | ToolCallEvent | ToolResultEvent | AssistantTextEvent] = [
        UserInputEvent(kind="user_input", anchor=anchor("0"), query_text=user_text, media=media)
    ]
    if tool_call_id is not None:
        cmd_payload = RunCommandPayload(kind="run_command", command_line="pytest -q", cwd="/repo", exit_code=0 if tool_output is not None else None)
        events.append(ToolCallEvent(
            kind="tool_call", anchor=anchor("1"), call_id=ToolCallId(tool_call_id),
            tool_name="run_command", tool_summary="Run pytest", payload=cmd_payload,
        ))
        if tool_output is not None:
            events.append(ToolResultEvent(
                kind="tool_result", anchor=anchor("2"), call_id=ToolCallId(tool_call_id),
                tool_name="run_command", output_text=tool_output, is_error=False, payload=cmd_payload,
            ))
    if reply_text is not None:
        events.append(AssistantTextEvent(kind="assistant_text", anchor=anchor("3"), text=reply_text))
    return events


class TestAdversarialInterrogateFixes(BridgeSandboxBase):
    def _seed_agy_session(self, cid: CascadeId, events: list[object], *, title: str | None = None) -> TrajectoryLedger:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        ledger = TrajectoryLedger.from_events(
            self.ws_path, self.ws_uri, events, recompute_agy_step_indices=True, explicit_title_override=title,  # type: ignore[arg-type]
        )
        self.bridge._transport.apply_to_agy(
            binding, cid, list(ledger.events), ledger, self.bridge.guard.probe_locks(cid, None), TransportMode.OFFLINE_STORAGE,
        )
        return ledger

    def test_fix1_inflight_empty_and_deleted_peer_sessions(self) -> None:
        self.bridge.pair_workspace(self.workspace_dir)
        cid_inflight = CascadeId("66660001-1111-4111-8111-111111111111")
        self._seed_agy_session(
            cid_inflight,
            _turn_events(HostKind.ANTIGRAVITY, "turn-1", "Run suite", reply_text=None, tool_call_id="call-inf-1"),
        )
        for direction in (SyncDirection.AUTO_INCREMENTAL, SyncDirection.AGY_TO_CODEX):
            rec = self.bridge.sync_session(self.workspace_dir, cascade_id=cid_inflight, direction=direction)
            self.assertEqual(rec.transition_kind, "deferred_busy")

        cid_empty = CascadeId("66660002-1111-4111-8111-111111111111")
        self._seed_agy_session(cid_empty, [], title="Empty Idle Session")
        with self.assertRaises(EmptySessionError):
            self.bridge.sync_session(self.workspace_dir, cascade_id=cid_empty)

        cid_valid = CascadeId("66660003-1111-4111-8111-111111111111")
        self._seed_agy_session(cid_valid, _turn_events(HostKind.ANTIGRAVITY, "turn-1", "Hello", "Completed turn 1"))

        batch_receipts = self.bridge.sync_workspace(self.workspace_dir)
        by_cid = {r.cascade_id: r.transition_kind for r in batch_receipts}
        self.assertNotIn(cid_empty, by_cid)
        self.assertEqual(by_cid[cid_inflight], "deferred_busy")
        self.assertEqual(by_cid[cid_valid], "initial_import")
        tid_valid = next(r.codex_thread_id for r in batch_receipts if r.cascade_id == cid_valid)

        state_db = self.paths.codex_home_dir / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(str(state_db))) as conn:
            rpath_row = conn.execute("SELECT rollout_path FROM threads WHERE id = ?", (str(tid_valid),)).fetchone()
            assert rpath_row is not None
            Path(str(rpath_row[0])).unlink()
            conn.execute("DELETE FROM threads WHERE id = ?", (str(tid_valid),))
            conn.commit()

        remat_codex = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid_valid, codex_thread_id=tid_valid, direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(remat_codex.transition_kind, "initial_import")

        (self.paths.agy_data_dir / "conversations" / f"{cid_valid}.db").unlink()
        remat_fork = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid_valid, codex_thread_id=tid_valid,
            direction=SyncDirection.AUTO_INCREMENTAL, fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION,
        )
        self.assertEqual(remat_fork.transition_kind, "diverged_forked")
        self.assertTrue((self.paths.agy_data_dir / "conversations" / f"{remat_fork.cascade_id}.db").is_file())

        (self.paths.agy_data_dir / "conversations" / f"{remat_fork.cascade_id}.db").unlink()
        with contextlib.closing(sqlite3.connect(str(state_db))) as conn2:
            conn2.execute("DELETE FROM threads WHERE id = ?", (str(remat_fork.codex_thread_id),))
            conn2.commit()
        for rfile in (self.paths.codex_home_dir / "sessions").rglob(f"*{remat_fork.codex_thread_id}*"):
            rfile.unlink(missing_ok=True)

        both_gone = self.bridge.sync_session(
            self.workspace_dir, cascade_id=remat_fork.cascade_id, codex_thread_id=remat_fork.codex_thread_id,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(both_gone.transition_kind, "no_op")

    def test_fix2_uri_authority_fragments_and_unified_diff_lines(self) -> None:
        with self.assertRaises(ValueError):
            parse_workspace_identity("file://remotehost/Users/Shared/repo")
        self.assertEqual(
            parse_workspace_identity(f"file://localhost{self.workspace_dir}?view=diff#L20"),
            (self.ws_path, self.ws_uri),
        )
        self.assertEqual(
            normalize_tool_file_path("[notes.md](file:///Users/Shared/a%23b.md?x=1#L10-L20)"),
            "/Users/Shared/a#b.md",
        )
        self.assertEqual(
            normalize_tool_file_path("file://fileserver/share/My%20Doc.md#L5"),
            "//fileserver/share/My Doc.md",
        )

        diff_text = "\n".join([
            "--- a/schema.sql", "+++ b/schema.sql", "@@ -1,3 +1,3 @@",
            " SELECT 1;", "--- legacy sql comment", "+++counter_line",
        ])
        decoded_diff = decode_unified_diff_block(encode_unified_diff_block(diff_text))
        self.assertEqual(decoded_diff, diff_text)

        edit_pl = FileEditPayload(kind="file_edit", path="/Users/Shared/schema.sql", edit_op="MODIFY", unified_diff=diff_text)
        edit_ev = ToolCallEvent(
            kind="tool_call",
            anchor=EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), timestamp_iso="2026-11-12T10:00:01Z", agy_step_index=StepIndex(1)),
            call_id=ToolCallId("call-diff-1"), tool_name="edit_file", tool_summary="Edit schema", payload=edit_pl,
        )
        res_ev = ToolResultEvent(
            kind="tool_result",
            anchor=EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), timestamp_iso="2026-11-12T10:00:02Z", agy_step_index=StepIndex(2)),
            call_id=ToolCallId("call-diff-1"), tool_name="edit_file", output_text="Applied diff", is_error=False, payload=edit_pl,
        )
        raw_steps = encode_events_to_agy_steps([
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), timestamp_iso="2026-11-12T10:00:00Z", agy_step_index=StepIndex(0)),
                query_text="Apply sql diff",
            ),
            edit_ev,
            res_ev,
        ])
        rt_ledger = decode_agy_steps_to_ledger(self.ws_path, self.ws_uri, raw_steps)
        rt_calls = [e for e in rt_ledger.events if isinstance(e, ToolCallEvent)]
        self.assertEqual(len(rt_calls), 1)
        assert isinstance(rt_calls[0].payload, FileEditPayload)
        self.assertEqual(rt_calls[0].payload.unified_diff, diff_text)

    def test_fix3_codex_zst_rollout_incremental_write_preserves_zstd_framing(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        tid = CodexThreadId("019a7800-0000-7000-8000-000000000033")
        t1_events = _turn_events(HostKind.CODEX, "turn-1", "Compress me", "Compressed turn 1")
        t1_ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t1_events)
        zst_dir = self.paths.codex_home_dir / "sessions" / "2026" / "11" / "12"
        zst_dir.mkdir(parents=True, exist_ok=True)
        zst_path = zst_dir / f"rollout-2026-11-12T10-00-00-{tid}.jsonl.zst"
        zst_path.write_bytes(zstd.compress(_render_rollout_jsonl(t1_ledger, tid).encode("utf-8")))
        with self.bridge.guard.acquire_offline_write_locks(HostKind.CODEX, None, tid):
            self.bridge._transport._write_codex_companion_stores(
                binding, tid, zst_path, t1_ledger, self.bridge._resolve_session_lineage(binding, None, tid), None,
            )

        rec1 = self.bridge.sync_session(self.workspace_dir, codex_thread_id=tid, direction=SyncDirection.CODEX_TO_AGY)
        self.assertEqual(rec1.transition_kind, "initial_import")

        t12_events = t1_events + _turn_events(
            HostKind.ANTIGRAVITY, "turn-2", "Append onto zst", "Compressed turn 2", ts_base="2026-11-12T10:05:0",
        )
        self._seed_agy_session(rec1.cascade_id, t12_events)
        rec2 = self.bridge.sync_session(
            self.workspace_dir, cascade_id=rec1.cascade_id, codex_thread_id=tid, direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(rec2.transition_kind, "incremental_append")
        self.assertEqual(zst_path.read_bytes()[:4], b"\x28\xb5\x2f\xfd")
        self.assertGreaterEqual(len(read_codex_rollout_lines(zst_path)), 4)
        inspected = self.bridge.inspect_session_ledger(self.workspace_dir, codex_thread_id=tid, mode=TransportMode.OFFLINE_STORAGE)
        self.assertEqual(len(inspected.completed_turn_watermarks), 2)

    def test_fix4_b2_rescued_session_active_lock_defer_source_resume_and_replace(self) -> None:
        self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("66660004-1111-4111-8111-111111111111")
        t1 = _turn_events(HostKind.ANTIGRAVITY, "turn-1", "First turn", "Done 1")
        t2_int = _turn_events(
            HostKind.ANTIGRAVITY, "turn-2", "Second interrupted", reply_text=None,
            ts_base="2026-11-12T10:02:0", tool_call_id="call-int-2",
        )
        self._seed_agy_session(cid, t1 + t2_int)
        rescued = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid, direction=SyncDirection.AGY_TO_CODEX, include_interrupted=True,
        )
        self.assertEqual(rescued.transition_kind, "initial_import")
        tid = rescued.codex_thread_id

        presence_dir = self.paths.agy_data_dir / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / f"{cid}.lock"
        lock_fd = _open_nonblocking_ex_flock(lock_file, create=True)
        try:
            busy_rec = self.bridge.sync_session(
                self.workspace_dir, cascade_id=cid, codex_thread_id=tid,
                direction=SyncDirection.AUTO_INCREMENTAL, include_interrupted=False,
            )
            self.assertEqual(busy_rec.transition_kind, "deferred_busy")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            lock_file.unlink(missing_ok=True)

        idle_noop = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid, codex_thread_id=tid,
            direction=SyncDirection.AUTO_INCREMENTAL, include_interrupted=False,
        )
        self.assertEqual(idle_noop.transition_kind, "no_op")

        t3_resumed = _turn_events(
            HostKind.ANTIGRAVITY, "turn-3", "Continue after interrupt", "Turn 3 completed cleanly", ts_base="2026-11-12T10:04:0",
        )
        self._seed_agy_session(cid, t1 + t2_int + t3_resumed)
        resumed_rec = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid, codex_thread_id=tid, direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(resumed_rec.transition_kind, "incremental_append")
        self.assertGreater(resumed_rec.events_applied, 0)
        self.assertEqual(
            self.bridge.sync_session(self.workspace_dir, cascade_id=cid, codex_thread_id=tid).transition_kind, "no_op",
        )

        cid_rep = CascadeId("66660005-1111-4111-8111-111111111111")
        self._seed_agy_session(cid_rep, t1 + t2_int)
        rep_rescued = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid_rep, direction=SyncDirection.AGY_TO_CODEX, include_interrupted=True,
        )
        t2_real = _turn_events(
            HostKind.ANTIGRAVITY, "turn-2", "Second interrupted", reply_text="Turn 2 actually finished",
            ts_base="2026-11-12T10:02:0", tool_call_id="call-int-2", tool_output="2 passed",
        )
        self._seed_agy_session(cid_rep, t1 + t2_real)
        rep_overwrite = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid_rep, codex_thread_id=rep_rescued.codex_thread_id,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(rep_overwrite.transition_kind, "incremental_append")
        codex_after = self.bridge.inspect_session_ledger(
            self.workspace_dir, codex_thread_id=rep_rescued.codex_thread_id, mode=TransportMode.OFFLINE_STORAGE,
        )
        codex_texts = [e.text for e in codex_after.events if isinstance(e, AssistantTextEvent)]
        self.assertIn("Turn 2 actually finished", codex_texts)
        self.assertNotIn(INTERRUPTED_TURN_TEXT, codex_texts)
        self.assertEqual(
            self.bridge.sync_session(self.workspace_dir, cascade_id=cid_rep, codex_thread_id=rep_rescued.codex_thread_id).transition_kind,
            "no_op",
        )

    def test_fix5_codex_rpc_decoder_multi_file_mcp_inflight_and_active_prefix_splice(self) -> None:
        rpc_thread = {
            "id": "019a7800-0000-7000-8000-000000000055", "name": "RPC Fidelity Thread",
            "turns": [
                {
                    "id": "turn-1",
                    "items": [
                        {"type": "userMessage", "content": [{"type": "text", "text": "Edit files and call MCP"}]},
                        {
                            "type": "fileChange", "id": "fc-1", "status": "completed",
                            "changes": [
                                {"path": "/repo/new.py", "kind": "add", "diff": "+x = 1\n"},
                                {"path": "/repo/old.py", "kind": {"type": "delete"}, "diff": "-y = 2\n"},
                            ],
                        },
                        {"type": "mcpToolCall", "id": "mcp-1", "server": "gh", "tool": "get_issue", "status": "completed", "arguments": {"n": 1}, "result": {"ok": True}},
                        {"type": "dynamicToolCall", "id": "dyn-1", "tool": "probe", "status": "completed", "arguments": {"t": "db"}, "result": "ok"},
                        {"type": "agentMessage", "text": "Completed all tools."},
                    ],
                },
                {
                    "id": "turn-2",
                    "items": [
                        {"type": "userMessage", "content": [{"type": "text", "text": "Stream tools"}]},
                        {"type": "fileChange", "id": "fc-inf", "status": "inProgress", "changes": [{"path": "/repo/a.py", "kind": "modify", "diff": "+z = 3\n"}]},
                        {"type": "webSearch", "id": "ws-inf", "query": "rfc 9562", "status": "inProgress"},
                        {"type": "mcpToolCall", "id": "mcp-inf", "server": "docs", "tool": "search", "status": "in_progress", "arguments": {"q": "wal"}},
                    ],
                },
            ],
        }
        decoded = decode_codex_rpc_thread_to_ledger(workspace_path=self.ws_path, workspace_uri=self.ws_uri, thread_obj=rpc_thread)
        self.assertEqual(len(decoded.completed_turn_watermarks), 1)
        self.assertTrue(decoded.has_inflight_turn)
        t1_calls = [e for e in decoded.events if isinstance(e, ToolCallEvent) and str(e.anchor.turn_id) == "turn-1"]
        self.assertEqual(len(t1_calls), 4)
        self.assertEqual(t1_calls[0].payload.edit_op, "ADD")  # type: ignore[union-attr]
        self.assertEqual((str(t1_calls[1].call_id), t1_calls[1].payload.edit_op), ("fc-1:1", "DELETE"))  # type: ignore[union-attr]
        self.assertIsInstance(t1_calls[2].payload, OpaqueToolPayload)
        self.assertIsInstance(t1_calls[3].payload, OpaqueToolPayload)
        self.assertEqual([e for e in decoded.events if isinstance(e, ToolResultEvent) and str(e.anchor.turn_id) == "turn-2"], [])

        binding = self.bridge.pair_workspace(self.workspace_dir)
        tid_stream = CodexThreadId("019a7800-0000-7000-8000-000000000056")
        t1_ev = _turn_events(HostKind.CODEX, "turn-1", "Initial offline turn", "Turn 1 reply", tool_call_id="call-t1", tool_output="ok-1")
        t1_led = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t1_ev)
        rdir = self.paths.codex_home_dir / "sessions" / "2026" / "11" / "12"
        rdir.mkdir(parents=True, exist_ok=True)
        rpath = rdir / f"rollout-2026-11-12T11-00-00-{tid_stream}.jsonl"
        rpath.write_text(_render_rollout_jsonl(t1_led, tid_stream), encoding="utf-8")
        with self.bridge.guard.acquire_offline_write_locks(HostKind.CODEX, None, tid_stream):
            self.bridge._transport._write_codex_companion_stores(
                binding, tid_stream, rpath, t1_led, self.bridge._resolve_session_lineage(binding, None, tid_stream), None,
            )

        rec_init = self.bridge.sync_session(
            self.workspace_dir, codex_thread_id=tid_stream, direction=SyncDirection.CODEX_TO_AGY, mode=TransportMode.OFFLINE_STORAGE,
        )
        t12_ev = t1_ev + _turn_events(
            HostKind.CODEX, "turn-2", "Second flushed turn", "Turn 2 reply",
            ts_base="2026-11-12T11:05:0", tool_call_id="call-t2", tool_output="ok-2",
        )
        t12_led = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t12_ev)
        rpath.write_text(_render_rollout_jsonl(t12_led, tid_stream), encoding="utf-8")

        sock_path = self.root / "codex-stream.sock"
        rpc_bridge = SyncBridge(paths=HostEnvironmentPaths(
            gemini_config_dir=self.paths.gemini_config_dir, agy_data_dir=self.paths.agy_data_dir,
            codex_home_dir=self.paths.codex_home_dir, sync_state_dir=self.paths.sync_state_dir,
            codex_app_server_socket=sock_path,
        ))

        def _serve_ws(conn: socket.socket) -> None:
            with conn:
                req = b""
                while b"\r\n\r\n" not in req:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    req += chunk
                ws_key = next(
                    ln.split(":", 1)[1].strip() for ln in req.decode("latin1").split("\r\n")
                    if ln.lower().startswith("sec-websocket-key:")
                )
                accept = base64.b64encode(hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
                conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii"))
                for _ in range(3):
                    hdr = conn.recv(2)
                    if len(hdr) < 2:
                        return
                    plen = hdr[1] & 0x7F
                    if plen == 126:
                        plen = int.from_bytes(conn.recv(2), "big")
                    elif plen == 127:
                        plen = int.from_bytes(conn.recv(8), "big")
                    mask = conn.recv(4)
                    raw_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(conn.recv(plen)))
                    msg = json.loads(raw_payload.decode("utf-8"))
                    if msg.get("method") == "initialized":
                        continue
                    resp = (
                        {"id": msg["id"], "result": {"userAgent": "codex-test"}}
                        if msg.get("method") == "initialize"
                        else {
                            "id": msg["id"],
                            "result": {
                                "thread": {
                                    "id": str(tid_stream), "path": str(rpath), "status": {"type": "active"},
                                    "turns": [
                                        {"id": "turn-1", "items": [{"type": "userMessage", "content": [{"type": "text", "text": "Initial offline turn"}]}, {"type": "agentMessage", "text": "Turn 1 reply"}]},
                                        {"id": "turn-2", "items": [{"type": "userMessage", "content": [{"type": "text", "text": "Second flushed turn"}]}, {"type": "agentMessage", "text": "Turn 2 reply"}]},
                                        {"id": "turn-3", "items": [{"type": "userMessage", "content": [{"type": "text", "text": "Streaming turn 3"}]}, {"type": "webSearch", "id": "ws-3", "query": "live", "status": "inProgress"}]},
                                    ],
                                }
                            },
                        }
                    )
                    body = json.dumps(resp).encode("utf-8")
                    hdr_out = bytes([0x81, len(body)]) if len(body) < 126 else bytes([0x81, 126]) + len(body).to_bytes(2, "big")
                    conn.sendall(hdr_out + body)

        stop_evt = threading.Event()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(4)
        srv.settimeout(0.2)

        def _accept_loop() -> None:
            while not stop_evt.is_set():
                try:
                    c, _ = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                threading.Thread(target=_serve_ws, args=(c,), daemon=True).start()

        t = threading.Thread(target=_accept_loop, daemon=True)
        t.start()
        try:
            live_rec = rpc_bridge.sync_session(
                self.workspace_dir, cascade_id=rec_init.cascade_id, codex_thread_id=tid_stream,
                direction=SyncDirection.AUTO_INCREMENTAL, mode=TransportMode.AUTO, include_interrupted=False,
            )
            self.assertEqual((live_rec.transition_kind, live_rec.transport_used), ("incremental_append", TransportMode.ONLINE_RPC))
            self.assertGreater(live_rec.events_applied, 0)
        finally:
            stop_evt.set()
            srv.close()
            t.join(timeout=2.0)
            sock_path.unlink(missing_ok=True)

    def test_fix6_standalone_cli_shm_wal_lock_detection_subprocess(self) -> None:
        self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("66660006-1111-4111-8111-111111111111")
        self._seed_agy_session(cid, _turn_events(HostKind.ANTIGRAVITY, "turn-1", "WAL check", "Ready"))
        tid = self.bridge.sync_session(self.workspace_dir, cascade_id=cid, direction=SyncDirection.AGY_TO_CODEX).codex_thread_id

        cid_blocked = CascadeId("66660016-1111-4111-8111-111111111111")
        self._seed_agy_session(cid_blocked, _turn_events(HostKind.ANTIGRAVITY, "turn-1", "Blocked by CLI WAL", "Reply"))

        state_db = self.paths.codex_home_dir / "state_5.sqlite"
        conv_db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        for db_p in (state_db, conv_db):
            with contextlib.closing(sqlite3.connect(str(db_p))) as c:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("CREATE TABLE IF NOT EXISTS _wal_probe (x INTEGER)")
                c.execute("INSERT INTO _wal_probe VALUES (1)")
                c.commit()

        idle_snap = self.bridge.guard.probe_locks(cid, tid)
        self.assertFalse(idle_snap.codex_wal_active or idle_snap.agy_wal_active)

        child_code = (
            "import sqlite3, sys\n"
            "conns = [sqlite3.connect(p, isolation_level=None) for p in sys.argv[1:3]]\n"
            "for c in conns:\n"
            "    c.execute('PRAGMA journal_mode=WAL')\n"
            "    c.execute('BEGIN IMMEDIATE')\n"
            "    c.execute('INSERT INTO _wal_probe VALUES (2)')\n"
            "sys.stdout.write('LOCKED\\n'); sys.stdout.flush(); sys.stdin.readline()\n"
            "for c in conns: c.execute('ROLLBACK'); c.close()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code, str(state_db), str(conv_db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert proc.stdout is not None
            self.assertEqual(proc.stdout.readline().strip(), "LOCKED")
            self.assertTrue(_is_sqlite_shm_wal_locked(state_db) and _is_sqlite_shm_wal_locked(conv_db))
            active_snap = self.bridge.guard.probe_locks(cid, tid)
            self.assertTrue(active_snap.codex_wal_active and active_snap.agy_wal_active)

            with self.assertRaises(SyncBusyLockError):
                self.bridge.pair_workspace(self.workspace_dir)
            deferred = self.bridge.sync_session(self.workspace_dir, cascade_id=cid_blocked, direction=SyncDirection.AGY_TO_CODEX)
            self.assertEqual(deferred.transition_kind, "deferred_busy")
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            proc.wait(timeout=5.0)

        released_snap = self.bridge.guard.probe_locks(cid, tid)
        self.assertFalse(released_snap.codex_wal_active or released_snap.agy_wal_active)

    def test_fix7_backup_guards_root_fork_lineage_and_symmetric_fork_receipt(self) -> None:
        fresh_ws = self.root / "unpaired-ws"
        fresh_ws.mkdir(parents=True, exist_ok=True)
        ro_b = self.bridge.registry.resolve_readonly_binding(fresh_ws)
        agy_proj_json = self.paths.gemini_config_dir / "projects" / f"{ro_b.agy_project_id}.json"
        from codex_agy_sync.bridge import registry as reg_mod

        real_write = reg_mod._atomic_write_text

        def _fail_gs(target: Path, content: str) -> None:
            if target.name == ".codex-global-state.json":
                raise OSError("Simulated global state write failure")
            real_write(target, content)

        with mock.patch("codex_agy_sync.bridge.registry._atomic_write_text", side_effect=_fail_gs):
            with self.assertRaises(OSError):
                self.bridge.pair_workspace(fresh_ws)
        self.assertFalse(agy_proj_json.exists())

        self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("66660007-1111-4111-8111-111111111111")
        t1 = _turn_events(HostKind.ANTIGRAVITY, "turn-1", "Common root", "Common reply")
        self._seed_agy_session(cid, t1)
        tid = self.bridge.sync_session(self.workspace_dir, cascade_id=cid, direction=SyncDirection.AGY_TO_CODEX).codex_thread_id

        annot_file = self.paths.agy_data_dir / "annotations" / f"{cid}.pbtxt"
        tx_file = self.paths.agy_data_dir / "brain" / str(cid) / ".system_generated" / "logs" / "transcript.jsonl"
        annot_file.unlink()
        tx_file.unlink()
        from codex_agy_sync.bridge import transport as tp_mod

        real_tp_write = tp_mod._atomic_write_text

        def _fail_tx(target: Path, content: str) -> None:
            if target.name == "transcript.jsonl":
                raise RuntimeError("boom")
            real_tp_write(target, content)

        with mock.patch("codex_agy_sync.bridge.transport._atomic_write_text", side_effect=_fail_tx):
            with self.assertRaises(RuntimeError):
                self.bridge.sync_session(self.workspace_dir, cascade_id=cid, codex_thread_id=tid)
        self.assertFalse(annot_file.exists())

        self._seed_agy_session(cid, t1 + _turn_events(HostKind.ANTIGRAVITY, "turn-2-agy", "AGY branch", "AGY reply", ts_base="2026-11-12T12:01:0"))
        t2_codex = t1 + _turn_events(HostKind.CODEX, "turn-2-codex", "Codex branch", "Codex reply", ts_base="2026-11-12T12:02:0")
        rpath, _ = self.bridge._transport._find_codex_rollout_and_title(tid)
        rpath.write_text(
            _render_rollout_jsonl(TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t2_codex), tid),
            encoding="utf-8",
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_main([
                "--gemini-config-dir", str(self.paths.gemini_config_dir),
                "--agy-data-dir", str(self.paths.agy_data_dir),
                "--codex-home-dir", str(self.paths.codex_home_dir),
                "--sync-state-dir", str(self.paths.sync_state_dir),
                "sync-session", "--workspace", str(self.workspace_dir),
                "--cascade-id", str(cid), "--codex-thread-id", str(tid),
                "--fork-policy", "fork_new_session",
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["transition_kind"], "diverged_forked")
        self.assertIsNotNone(payload["peer_fork_cascade_id"])
        self.assertIsNotNone(payload["peer_fork_codex_thread_id"])

        fork_tid = CodexThreadId(payload["codex_thread_id"] if payload["codex_thread_id"] != str(tid) else payload["peer_fork_codex_thread_id"])
        with contextlib.closing(sqlite3.connect(str(self.paths.codex_home_dir / "state_5.sqlite"))) as conn:
            row = conn.execute("SELECT attached_thread_parent_id, source FROM threads WHERE id = ?", (str(fork_tid),)).fetchone()
            assert row is not None
            self.assertEqual((row[0], row[1]), (str(tid), "desktop"))
            conn.execute("UPDATE threads SET agent_role = 'architect' WHERE id = ?", (str(fork_tid),))
            conn.commit()

        ro_bind = self.bridge.registry.resolve_readonly_binding(self.workspace_dir)
        fork_desc = next(s for s in self.bridge.registry.list_workspace_sessions(ro_bind, include_subagents=True) if s.codex_thread_id == fork_tid)
        self.assertFalse(fork_desc.lineage.is_subagent)
        self.assertEqual(fork_desc.lineage.nesting_depth, 0)
        self.assertIsNone(fork_desc.parent_codex_thread_id)

    def test_fix8_user_input_media_included_in_boundary_digest(self) -> None:
        led_a = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, _turn_events(
            HostKind.ANTIGRAVITY, "turn-1", "Compare screenshot", "Looks good",
            media=(MediaAttachment(mime_type="image/png", uri="file:///shot-a.png"),),
        ))
        led_b = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, _turn_events(
            HostKind.ANTIGRAVITY, "turn-1", "Compare screenshot", "Looks good",
            media=(MediaAttachment(mime_type="image/png", uri="file:///shot-b.png"),),
        ))
        led_n1 = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, _turn_events(HostKind.ANTIGRAVITY, "turn-1", "Compare screenshot", "Looks good"))
        led_n2 = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, _turn_events(HostKind.CODEX, "turn-1", "Compare screenshot", "Looks good"))
        wm_a, wm_b, wm_n1, wm_n2 = led_a.latest_watermark(), led_b.latest_watermark(), led_n1.latest_watermark(), led_n2.latest_watermark()
        assert wm_a and wm_b and wm_n1 and wm_n2
        self.assertNotEqual(wm_a.boundary_digest, wm_b.boundary_digest)
        self.assertNotEqual(wm_a.boundary_digest, wm_n1.boundary_digest)
        self.assertEqual(wm_n1.boundary_digest, wm_n2.boundary_digest)

    def test_fix9_acquire_offline_write_locks_unlink_toctou_falls_through(self) -> None:
        cid = CascadeId("66660009-1111-4111-8111-111111111111")
        presence_dir = self.paths.agy_data_dir / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        cand = presence_dir / f"{cid}.lock"
        cand.write_text("", encoding="utf-8")
        calls: list[tuple[Path, bool]] = []

        def _racy_open(lock_path: Path, *, create: bool = False) -> int:
            calls.append((lock_path, create))
            if lock_path == cand and not create:
                lock_path.unlink(missing_ok=True)
                raise FileNotFoundError(str(lock_path))
            return _open_nonblocking_ex_flock(lock_path, create=create)

        with mock.patch("codex_agy_sync.bridge.paths_and_locks._open_nonblocking_ex_flock", side_effect=_racy_open):
            with self.bridge.guard.acquire_offline_write_locks(HostKind.ANTIGRAVITY, cid, None) as snap:
                self.assertTrue(snap.can_write_agy_offline() and cand.exists())
        self.assertFalse(cand.exists())
        self.assertIn((cand, False), calls)
        self.assertIn((cand, True), calls)


if __name__ == "__main__":
    unittest.main()
