from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import sqlite3

from codex_agy_sync.bridge import SyncDirection, TransportMode
from codex_agy_sync.codecs import (
    ACTIVE_STEP_STATUS_CODES, CortexStepTypeCode, StepStatusCode, decode_agy_trajectory,
    decode_codex_rpc_thread_to_ledger, encode_agy_trajectory, encode_codex_rollout,
)
from codex_agy_sync import INTERRUPTED_TOOL_OUTPUT, INTERRUPTED_TURN_TEXT
from codex_agy_sync.ledger import (
    AssistantTextEvent, CascadeId, CodexThreadId, EventOriginAnchor, HostKind,
    ReadFilePayload, RunCommandPayload, SystemEvent, ThinkingEvent, ToolCallEvent,
    ToolCallId, ToolResultEvent, TrajectoryLedger, TurnId, UserInputEvent,
)
from tests._helpers import BridgeSandboxBase


class TestInterruptedTurnRescue(BridgeSandboxBase):
    def setUp(self) -> None:
        super().setUp()
        self.binding = self.bridge.pair_workspace(self.workspace_dir)
        self.locks = self.bridge.guard.probe_locks(None, None)

    def test_3b_strict_turn_completion_and_historical_dangling_call_synthesis(self) -> None:
        t1 = EventOriginAnchor(
            origin_host=HostKind.ANTIGRAVITY,
            turn_id=TurnId("turn-1"),
            timestamp_iso="2026-11-12T02:00:00Z",
        )
        mid_tool_events = [
            UserInputEvent(kind="user_input", anchor=t1, query_text="Inspect file"),
            ThinkingEvent(kind="thinking", anchor=t1, summary_text="Reading file"),
            ToolCallEvent(
                kind="tool_call",
                anchor=t1,
                call_id=ToolCallId("call-1"),
                tool_name="read_file",
                tool_summary="Read README",
                payload=ReadFilePayload(kind="read_file", path="README.md"),
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=t1,
                call_id=ToolCallId("call-1"),
                tool_name="read_file",
                output_text="contents",
                is_error=False,
                payload=ReadFilePayload(kind="read_file", path="README.md"),
            ),
        ]
        mid_ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, mid_tool_events)
        self.assertTrue(mid_ledger.has_inflight_turn)
        self.assertEqual(mid_ledger.completed_turn_watermarks, ())

        complete_with_trailing_meta = [
            *mid_tool_events,
            AssistantTextEvent(kind="assistant_text", anchor=t1, text="File inspected."),
            SystemEvent(
                kind="system",
                anchor=t1,
                event_type="codex_event_msg:thread_settings_applied",
                message_text="{}",
            ),
        ]
        ok_ledger = TrajectoryLedger.from_events(
            self.ws_path, self.ws_uri, complete_with_trailing_meta
        )
        self.assertFalse(ok_ledger.has_inflight_turn)
        self.assertEqual(len(ok_ledger.completed_turn_watermarks), 1)

        base_rows = encode_agy_trajectory(
            [
                UserInputEvent(kind="user_input", anchor=t1, query_text="Stream test"),
                AssistantTextEvent(kind="assistant_text", anchor=t1, text="Partial stream..."),
            ]
        )
        for active_code in sorted(ACTIVE_STEP_STATUS_CODES):
            streaming_rows = [
                base_rows[0],
                dataclasses.replace(base_rows[1], status=active_code),
            ]
            dec = decode_agy_trajectory(self.ws_path, self.ws_uri, streaming_rows)
            self.assertTrue(dec.has_inflight_turn, msg=f"status={active_code}")
            self.assertEqual(dec.completed_turn_watermarks, ())

        for rpc_status in ("inProgress", "interrupted", "failed"):
            rpc_dec = decode_codex_rpc_thread_to_ledger(
                self.ws_path,
                self.ws_uri,
                {
                    "id": "rpc-inc",
                    "name": "RPC Incomplete",
                    "turns": [
                        {
                            "id": "turn-a",
                            "status": rpc_status,
                            "items": [
                                {
                                    "type": "userMessage",
                                    "id": "u1",
                                    "content": [{"type": "text", "text": "Run long job"}],
                                }
                            ],
                        }
                    ],
                },
            )
            self.assertTrue(rpc_dec.has_inflight_turn, msg=f"rpc_status={rpc_status}")
            self.assertEqual(rpc_dec.completed_turn_watermarks, ())

        t2 = EventOriginAnchor(
            origin_host=HostKind.CODEX,
            turn_id=TurnId("turn-2"),
            timestamp_iso="2026-11-12T02:05:00Z",
        )
        hist_events = [
            UserInputEvent(kind="user_input", anchor=t1, query_text="Turn 1 aborted tool"),
            ToolCallEvent(
                kind="tool_call",
                anchor=t1,
                call_id=ToolCallId("dangling-hist-1"),
                tool_name="run_command",
                tool_summary="Run tests",
                payload=RunCommandPayload(
                    kind="run_command", command_line="make test", cwd=str(self.workspace_dir)
                ),
            ),
            SystemEvent(
                kind="system",
                anchor=t1,
                event_type="codex_event_msg:turn_aborted",
                message_text="user_interrupt",
            ),
            UserInputEvent(kind="user_input", anchor=t2, query_text="Turn 2 recovery"),
            AssistantTextEvent(kind="assistant_text", anchor=t2, text="Recovered cleanly."),
        ]
        hist_ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, hist_events)
        self.assertFalse(hist_ledger.has_inflight_turn)
        self.assertEqual(len(hist_ledger.completed_turn_watermarks), 2)

        rollout_lines = encode_codex_rollout(
            hist_ledger.events, session_id="tid-hist", cwd=str(self.workspace_dir)
        )
        fn_outputs = [
            json.loads(line.raw_json)["payload"]
            for line in rollout_lines
            if json.loads(line.raw_json)["type"] == "response_item"
            and json.loads(line.raw_json)["payload"].get("type") == "function_call_output"
        ]
        self.assertEqual(len(fn_outputs), 1)
        self.assertEqual(fn_outputs[0]["call_id"], "dangling-hist-1")
        self.assertEqual(fn_outputs[0]["output"], INTERRUPTED_TOOL_OUTPUT)
        self.assertTrue(fn_outputs[0]["is_error"])

    def test_3c_b2_interrupted_rescue_agy_to_codex_idempotency_and_turn3_syncback(self) -> None:
        cid = CascadeId("77777777-7777-4777-8777-777777777777")
        t1 = EventOriginAnchor(
            origin_host=HostKind.ANTIGRAVITY,
            turn_id=TurnId("turn-1"),
            timestamp_iso="2026-11-12T03:00:00Z",
        )
        t2 = EventOriginAnchor(
            origin_host=HostKind.ANTIGRAVITY,
            turn_id=TurnId("turn-2"),
            timestamp_iso="2026-11-12T03:02:00Z",
        )
        events = [
            UserInputEvent(kind="user_input", anchor=t1, query_text="Turn 1"),
            AssistantTextEvent(kind="assistant_text", anchor=t1, text="Turn 1 done"),
            UserInputEvent(kind="user_input", anchor=t2, query_text="Turn 2 interrupted"),
            ThinkingEvent(kind="thinking", anchor=t2, summary_text="About to run command"),
            ToolCallEvent(
                kind="tool_call",
                anchor=t2,
                call_id=ToolCallId("call-int-2"),
                tool_name="run_command",
                tool_summary="Run pytest",
                payload=RunCommandPayload(
                    kind="run_command", command_line="pytest -q", cwd=str(self.workspace_dir)
                ),
            ),
        ]
        src_ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, events)
        self.assertTrue(src_ledger.has_inflight_turn)
        self.assertEqual(len(src_ledger.completed_turn_watermarks), 1)
        self.bridge._transport.apply_to_agy(
            binding=self.binding,
            cascade_id=cid,
            events=events,
            full_ledger=src_ledger,
            lock_snapshot=self.locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )

        r_default = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            direction=SyncDirection.AGY_TO_CODEX,
            include_interrupted=False,
        )
        self.assertEqual(r_default.transition_kind, "initial_import")
        self.assertEqual(r_default.events_applied, 2)
        assert r_default.updated_cursor is not None
        self.assertFalse(r_default.updated_cursor.rescued_inflight_turn)
        tid = r_default.codex_thread_id

        presence_dir = self.paths.agy_data_dir / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / f"{cid}.lock"
        lock_file.write_text("locked", encoding="utf-8")
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r_busy = self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=cid,
                codex_thread_id=tid,
                direction=SyncDirection.AUTO_INCREMENTAL,
                include_interrupted=True,
            )
            self.assertEqual(r_busy.transition_kind, "deferred_busy")
            self.assertEqual(r_busy.events_applied, 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            lock_file.unlink()

        cli_out = self._run_cli_json(
            "sync-session",
            "--workspace",
            str(self.workspace_dir),
            "--cascade-id",
            str(cid),
            "--codex-thread-id",
            str(tid),
            "--include-interrupted",
        )
        self.assertEqual(cli_out["transition_kind"], "incremental_append")
        cur_json = cli_out["updated_cursor"]
        assert isinstance(cur_json, dict)
        self.assertTrue(cur_json["rescued_inflight_turn"])
        self.assertEqual(cur_json["pre_rescue_source_host"], "antigravity")

        codex_rescued = self.bridge.inspect_session_ledger(
            self.workspace_dir, codex_thread_id=tid
        )
        self.assertFalse(codex_rescued.has_inflight_turn)
        self.assertEqual(len(codex_rescued.completed_turn_watermarks), 2)
        _, rescued_res = codex_rescued.call_and_result(ToolCallId("call-int-2"))
        self.assertIsNotNone(rescued_res)
        assert rescued_res is not None
        self.assertTrue(rescued_res.is_error)
        self.assertEqual(rescued_res.output_text, INTERRUPTED_TOOL_OUTPUT)
        self.assertEqual(
            [e.text for e in codex_rescued.events if isinstance(e, AssistantTextEvent)][-1],
            INTERRUPTED_TURN_TEXT,
        )

        for flag in (True, False):
            r_noop = self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=cid,
                codex_thread_id=tid,
                direction=SyncDirection.AUTO_INCREMENTAL,
                include_interrupted=flag,
            )
            self.assertEqual(r_noop.transition_kind, "no_op", msg=f"flag={flag}")
            self.assertEqual(r_noop.events_applied, 0)

        t3 = EventOriginAnchor(
            origin_host=HostKind.CODEX,
            turn_id=TurnId("turn-3"),
            timestamp_iso="2026-11-12T03:05:00Z",
        )
        t3_events = [
            *codex_rescued.events,
            UserInputEvent(kind="user_input", anchor=t3, query_text="Continue in Codex Turn 3"),
            AssistantTextEvent(kind="assistant_text", anchor=t3, text="Turn 3 finished in Codex"),
        ]
        self.bridge._transport.apply_to_codex(
            binding=self.binding,
            thread_id=tid,
            events=t3_events,
            full_ledger=TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t3_events),
            lock_snapshot=self.locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )
        r_back = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
            include_interrupted=False,
        )
        self.assertEqual(r_back.transition_kind, "incremental_append")
        assert r_back.updated_cursor is not None
        self.assertFalse(r_back.updated_cursor.rescued_inflight_turn)

        agy_after = self.bridge.inspect_session_ledger(self.workspace_dir, cascade_id=cid)
        self.assertFalse(agy_after.has_inflight_turn)
        self.assertEqual(len(agy_after.completed_turn_watermarks), 3)
        self.assertEqual(
            [e.query_text for e in agy_after.events if isinstance(e, UserInputEvent)],
            ["Turn 1", "Turn 2 interrupted", "Continue in Codex Turn 3"],
        )
        conv_conn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversations' / f'{cid}.db'}?mode=ro", uri=True
        )
        try:
            cmd_statuses = [
                row[0]
                for row in conv_conn.execute(
                    "SELECT status FROM steps WHERE step_type = ?",
                    (int(CortexStepTypeCode.GENERIC),),
                ).fetchall()
            ]
        finally:
            conv_conn.close()
        self.assertEqual(cmd_statuses, [int(StepStatusCode.CANCELED)])

    def test_3d_b2_interrupted_rescue_codex_to_agy_and_sync_workspace(self) -> None:
        tid = CodexThreadId("88888888-8888-4888-8888-888888888888")
        t1 = EventOriginAnchor(
            origin_host=HostKind.CODEX,
            turn_id=TurnId("turn-1"),
            timestamp_iso="2026-11-12T04:00:00Z",
        )
        events = [
            UserInputEvent(kind="user_input", anchor=t1, query_text="Codex interrupted turn 1"),
            ToolCallEvent(
                kind="tool_call",
                anchor=t1,
                call_id=ToolCallId("call-cx-1"),
                tool_name="run_command",
                tool_summary="Run cargo check",
                payload=RunCommandPayload(
                    kind="run_command", command_line="cargo check", cwd=str(self.workspace_dir)
                ),
            ),
            SystemEvent(
                kind="system",
                anchor=t1,
                event_type="codex_event_msg:turn_aborted",
                message_text="interrupted",
            ),
        ]
        src_ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, events)
        self.assertTrue(src_ledger.has_inflight_turn)
        self.bridge._transport.apply_to_codex(
            binding=self.binding,
            thread_id=tid,
            events=events,
            full_ledger=src_ledger,
            lock_snapshot=self.locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )

        r_skip = self.bridge.sync_session(
            self.workspace_dir,
            codex_thread_id=tid,
            direction=SyncDirection.CODEX_TO_AGY,
            include_interrupted=False,
        )
        self.assertEqual(r_skip.transition_kind, "deferred_busy")

        ws_out = self._run_cli_json(
            "sync-workspace",
            "--workspace",
            str(self.workspace_dir),
            "--include-interrupted",
        )
        receipts = ws_out["receipts"]
        assert isinstance(receipts, list)
        self.assertEqual(len(receipts), 1)
        rec = receipts[0]
        self.assertEqual(rec["transition_kind"], "initial_import")
        self.assertTrue(rec["updated_cursor"]["rescued_inflight_turn"])
        self.assertEqual(rec["updated_cursor"]["pre_rescue_source_host"], "codex")
        cid = CascadeId(str(rec["cascade_id"]))

        for flag in (True, False):
            r_noop = self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=cid,
                codex_thread_id=tid,
                direction=SyncDirection.AUTO_INCREMENTAL,
                include_interrupted=flag,
            )
            self.assertEqual(r_noop.transition_kind, "no_op")

        agy_rescued = self.bridge.inspect_session_ledger(self.workspace_dir, cascade_id=cid)
        conv_conn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversations' / f'{cid}.db'}?mode=ro", uri=True
        )
        try:
            cx_statuses = [
                row[0]
                for row in conv_conn.execute(
                    "SELECT status FROM steps WHERE step_type = ?",
                    (int(CortexStepTypeCode.GENERIC),),
                ).fetchall()
            ]
        finally:
            conv_conn.close()
        self.assertEqual(cx_statuses, [int(StepStatusCode.CANCELED)])
        t2 = EventOriginAnchor(
            origin_host=HostKind.ANTIGRAVITY,
            turn_id=TurnId("turn-2"),
            timestamp_iso="2026-11-12T04:03:00Z",
        )
        t2_events = [
            *agy_rescued.events,
            UserInputEvent(kind="user_input", anchor=t2, query_text="Resume in AGY Turn 2"),
            AssistantTextEvent(kind="assistant_text", anchor=t2, text="Turn 2 complete in AGY"),
        ]
        self.bridge._transport.apply_to_agy(
            binding=self.binding,
            cascade_id=cid,
            events=t2_events,
            full_ledger=TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t2_events),
            lock_snapshot=self.locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )
        r_back = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(r_back.transition_kind, "incremental_append")
        codex_after = self.bridge.inspect_session_ledger(
            self.workspace_dir, codex_thread_id=tid
        )
        self.assertFalse(codex_after.has_inflight_turn)
        self.assertEqual(len(codex_after.completed_turn_watermarks), 2)
        self.assertEqual(
            [e.query_text for e in codex_after.events if isinstance(e, UserInputEvent)],
            ["Codex interrupted turn 1", "Resume in AGY Turn 2"],
        )
