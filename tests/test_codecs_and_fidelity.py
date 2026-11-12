from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_agy_sync.codecs import (
    AgyRawStepRow, CodexRawRolloutLine, CortexStepTypeCode, GeminiStepWireTag,
    STEP_TAG_TO_SPEC, STEP_WIRE_TABLE, TOOL_CODEC_TABLE, TrajectoryWireCodec,
    decode_agy_trajectory, decode_codex_rollout, decode_codex_rpc_thread_to_ledger,
    encode_agy_trajectory, encode_codex_rollout, read_codex_rollout_lines,
    serialize_agy_chat_tool_arguments, serialize_tool_payload_arguments_json,
)
from codex_agy_sync._wire import (
    UnknownWireField, encode_bytes_field, encode_string_field, encode_varint,
    encode_varint_field,
)
from codex_agy_sync.ledger import (
    AskQuestionPayload, AssistantTextEvent, CheckpointEvent, DefineSubagentPayload,
    EventOriginAnchor, FileEditPayload, FinishPayload, GenerateImagePayload, HostKind,
    InvokeSubagentPayload, ListResourcesPayload, ManageSubagentsPayload, ManageTaskPayload,
    MediaAttachment, MomaSearchPayload, OpaqueToolPayload, QuestionItem, QuestionOption,
    ReadFilePayload, ReadResourcePayload, RolloutLineNo, RunCommandPayload, SchedulePayload,
    SearchWebDocument, SearchWebPayload, SendMessagePayload, StepIndex, SystemEvent,
    ThinkingEvent, ToolCallEvent, ToolCallId, ToolResultEvent, TrajectoryLedger, TurnId,
    UserInputEvent, parse_workspace_identity,
)
from scripts.verify_session_mapping import decode_protobuf


class TestCodecBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name) / "codec-workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.ws_path, self.ws_uri = parse_workspace_identity(self.workspace_dir)
        self.codec = TrajectoryWireCodec()
        repo_root = Path(__file__).resolve().parent.parent
        self.schema = json.loads((repo_root / "schema" / "antigravity_2_30_schema.json").read_text())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_step_and_tool_codec_tables_cover_full_catalog(self) -> None:
        self.assertEqual(len(STEP_WIRE_TABLE), 7)
        self.assertEqual(len(STEP_TAG_TO_SPEC), 7)
        self.assertEqual(len(TOOL_CODEC_TABLE), 16)

        payloads = [
            ReadFilePayload(kind="read_file", path="a.py", start_line=1, end_line=20, file_num_lines=50),
            FileEditPayload(
                kind="file_edit",
                path="b.py",
                edit_op="MODIFY",
                unified_diff="--- a/b.py\n+++ b/b.py\n@@ -1,2 +1,4 @@\n",
                diff_stats=(4, 2),
                is_artifact=False,
            ),
            RunCommandPayload(
                kind="run_command",
                command_line="pytest -q",
                cwd=str(self.ws_path),
                exit_code=0,
            ),
            SearchWebPayload(
                kind="search_web",
                query="protobuf wire format",
                domain_filter="protobuf.dev",
                summary="Summary markdown",
                documents=(SearchWebDocument(title="Protobuf Docs", url="https://protobuf.dev", snippet="Docs"),),
            ),
            InvokeSubagentPayload(
                kind="invoke_subagent",
                subagent_type="research",
                role="Codebase Researcher",
                prompt="Inspect schema",
                spawned_conversation_id="sub-123",
            ),
            DefineSubagentPayload(
                kind="define_subagent",
                name="auditor",
                tool_names=("read_file",),
            ),
            ManageSubagentsPayload(
                kind="manage_subagents",
                action="list",
                target_conversation_ids=("sub-123",),
            ),
            ManageTaskPayload(
                kind="manage_task",
                action="status",
                task_id="task-9",
                input_text="running",
            ),
            SendMessagePayload(
                kind="send_message",
                recipient_id="sub-123",
                message_body="Status check",
            ),
            SchedulePayload(
                kind="schedule",
                prompt="Wake up",
                duration_seconds=30,
                timer_condition="any",
            ),
            AskQuestionPayload(
                kind="ask_question",
                questions=(
                    QuestionItem(
                        question="Which mode?",
                        options=(
                            QuestionOption(label="(Recommended) Auto"),
                            QuestionOption(label="Offline"),
                        ),
                        is_multi_select=False,
                    ),
                ),
            ),
            ListResourcesPayload(
                kind="list_resources",
                server_name="context7",
                resource_uris=("res://1",),
            ),
            ReadResourcePayload(
                kind="read_resource",
                server_name="context7",
                uri="res://1",
                contents_text=("resource text",),
            ),
            GenerateImagePayload(
                kind="generate_image",
                prompt="Architecture diagram",
                image_paths=("arch.png",),
            ),
            MomaSearchPayload(
                kind="moma_search",
                query="jetski_cortex_pb",
                result_count=3,
            ),
            FinishPayload(
                kind="finish",
                arguments_json='{"final_response":"Completed task"}',
            ),
            OpaqueToolPayload(
                kind="opaque",
                tool_name="custom_mcp_tool",
                raw_arguments_json='{"k":"v"}',
                type_url="type.googleapis.com/custom.ToolMessage",
                raw_any_bytes=b"\x08\x2a",
            ),
        ]

        turn_id = TurnId("turn-tools")
        events = [
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso="2026-11-10T03:00:00Z"), query_text="Run all 16 tools plus opaque fallback")
        ]
        for idx, payload in enumerate(payloads):
            call_id = ToolCallId(f"call-{idx}")
            tool_name = payload.tool_name if isinstance(payload, OpaqueToolPayload) else payload.kind
            events.append(
                ToolCallEvent(
                    kind="tool_call",
                    anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso="2026-11-10T03:00:01Z"),
                    call_id=call_id,
                    tool_name=tool_name,
                    tool_summary=f"Summary {idx}",
                    payload=payload,
                )
            )
            events.append(
                ToolResultEvent(
                    kind="tool_result",
                    anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso="2026-11-10T03:00:02Z"),
                    call_id=call_id,
                    tool_name=tool_name,
                    output_text=f"Output {idx}",
                    is_error=False,
                    payload=payload,
                )
            )

        agy_steps = self.codec.encode_events_to_agy_steps(events)
        decoded_from_agy = self.codec.decode_agy_steps_to_ledger(
            self.ws_path,
            self.ws_uri,
            agy_steps,
        )
        decoded_calls = [ev for ev in decoded_from_agy.events if isinstance(ev, ToolCallEvent)]
        decoded_results = [ev for ev in decoded_from_agy.events if isinstance(ev, ToolResultEvent)]
        self.assertEqual(len(decoded_calls), len(payloads))
        self.assertEqual(len(decoded_results), len(payloads))

        file_edit_results = [
            ev.payload for ev in decoded_results if isinstance(ev.payload, FileEditPayload)
        ]
        self.assertEqual(len(file_edit_results), 1)
        self.assertEqual(file_edit_results[0].diff_stats[0], 4)

        run_cmd_results = [
            ev.payload for ev in decoded_results if isinstance(ev.payload, RunCommandPayload)
        ]
        self.assertEqual(len(run_cmd_results), 1)
        self.assertEqual(run_cmd_results[0].exit_code, 0)

        search_web_results = [
            ev.payload for ev in decoded_results if isinstance(ev.payload, SearchWebPayload)
        ]
        self.assertEqual(len(search_web_results), 1)
        self.assertEqual(search_web_results[0].documents[0].url, "https://protobuf.dev")

        ask_q_results = [
            ev.payload for ev in decoded_results if isinstance(ev.payload, AskQuestionPayload)
        ]
        self.assertEqual(len(ask_q_results), 1)
        self.assertEqual(len(ask_q_results[0].questions), 1)

        codex_lines = self.codec.encode_ledger_to_codex_rollout(decoded_from_agy)
        decoded_from_codex = self.codec.decode_codex_rollout_to_ledger(
            self.ws_path,
            self.ws_uri,
            codex_lines,
        )
        self.assertEqual(
            len([ev for ev in decoded_from_codex.events if isinstance(ev, ToolCallEvent)]),
            len(payloads),
        )
        self.assertEqual(
            len([ev for ev in decoded_from_codex.events if isinstance(ev, ToolResultEvent)]),
            len(payloads),
        )

    def test_unknown_wire_fields_survive_agy_roundtrip(self) -> None:
        unknown_tag_9999 = encode_varint((9999 << 3) | 2) + encode_varint(5) + b"hello"
        user_body = encode_string_field(5, "Test unknown field preservation")
        step_blob = (
            encode_varint_field(1, 14)
            + encode_varint_field(4, 3)
            + encode_bytes_field(19, user_body)
            + unknown_tag_9999
        )
        ledger = self.codec.decode_agy_steps_to_ledger(
            self.ws_path,
            self.ws_uri,
            [
                AgyRawStepRow(
                    idx=StepIndex(0),
                    step_type=14,
                    status=3,
                    has_subtrajectory=False,
                    metadata_blob=b"",
                    step_payload_blob=step_blob,
                    step_format=1,
                )
            ],
        )
        self.assertEqual(len(ledger.events), 1)
        self.assertEqual(
            ledger.events[0].anchor.unknown_wire_fields,
            (UnknownWireField(field_tag=9999, wire_type=2, raw_bytes=b"hello"),),
        )
        reencoded = self.codec.encode_ledger_to_agy_steps(ledger)
        self.assertEqual(len(reencoded), 1)
        self.assertIn(unknown_tag_9999, reencoded[0].step_payload_blob)

    def test_real_agy_session_161_steps_roundtrip(self) -> None:
        real_conv_db = (
            Path.home()
            / ".gemini"
            / "antigravity"
            / "conversations"
            / "eb95dc40-cb5e-4017-b4ff-f41223e1f701.db"
        )
        if not real_conv_db.exists():
            self.skipTest("Real Antigravity session database not present on this machine")

        conn = sqlite3.connect(f"file:{real_conv_db}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON;")
            rows = conn.execute(
                "SELECT idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format FROM steps ORDER BY idx ASC"
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), 161)

        raw_rows = [
            AgyRawStepRow(
                idx=StepIndex(int(r[0])),
                step_type=int(r[1]),
                status=int(r[2]),
                has_subtrajectory=bool(r[3]),
                metadata_blob=bytes(r[4] or b""),
                step_payload_blob=bytes(r[5] or b""),
                step_format=int(r[6] or 1),
            )
            for r in rows
        ]
        ledger = self.codec.decode_agy_steps_to_ledger(
            self.ws_path,
            self.ws_uri,
            raw_rows,
        )
        user_events = [ev for ev in ledger.events if isinstance(ev, UserInputEvent)]
        tool_calls = [ev for ev in ledger.events if isinstance(ev, ToolCallEvent)]
        tool_results = [ev for ev in ledger.events if isinstance(ev, ToolResultEvent)]
        checkpoints = [ev for ev in ledger.events if isinstance(ev, CheckpointEvent)]
        thinking_events = [ev for ev in ledger.events if isinstance(ev, ThinkingEvent)]

        self.assertEqual(len(user_events), 2)
        self.assertEqual(len(tool_calls), 97)
        self.assertEqual(len(tool_results), 97)
        self.assertEqual(len(checkpoints), 1)
        self.assertGreaterEqual(len(thinking_events), 40)

        for call_ev in tool_calls:
            c_ev, r_ev = ledger.call_and_result(call_ev.call_id)
            self.assertEqual(c_ev.call_id, call_ev.call_id)
            self.assertIsNotNone(r_ev)

        codex_lines = self.codec.encode_ledger_to_codex_rollout(ledger)
        ledger_from_codex = self.codec.decode_codex_rollout_to_ledger(
            self.ws_path,
            self.ws_uri,
            codex_lines,
        )
        self.assertEqual(
            len([ev for ev in ledger_from_codex.events if isinstance(ev, UserInputEvent)]),
            2,
        )
        self.assertEqual(
            len([ev for ev in ledger_from_codex.events if isinstance(ev, ToolCallEvent)]),
            97,
        )
        self.assertEqual(
            len([ev for ev in ledger_from_codex.events if isinstance(ev, ToolResultEvent)]),
            97,
        )

        reencoded_agy = self.codec.encode_ledger_to_agy_steps(ledger_from_codex)
        unknown_fields: list[str] = []
        for step_row in reencoded_agy:
            decode_protobuf(
                step_row.step_payload_blob,
                "gemini_coder.Step",
                self.schema,
                unknown_fields,
            )
        self.assertEqual(unknown_fields, [])

        ledger_roundtrip = self.codec.decode_agy_steps_to_ledger(
            self.ws_path,
            self.ws_uri,
            reencoded_agy,
        )
        self.assertEqual(
            len([ev for ev in ledger_roundtrip.events if isinstance(ev, ToolCallEvent)]),
            97,
        )
        self.assertEqual(
            len([ev for ev in ledger_roundtrip.events if isinstance(ev, ToolResultEvent)]),
            97,
        )

    def test_real_codex_session_rollout_roundtrip(self) -> None:
        codex_sessions_dir = Path.home() / ".codex" / "sessions"
        matches = list(codex_sessions_dir.rglob("*01a1ae7d-d56f-7650-8336-aa848b9f2a5a.jsonl"))
        if not matches:
            self.skipTest("Real Codex rollout file not present on this machine")

        raw_lines = [
            CodexRawRolloutLine(line_no=RolloutLineNo(idx + 1), raw_json=line)
            for idx, line in enumerate(matches[0].read_text(encoding="utf-8").splitlines())
            if line.strip()
        ]
        ledger = self.codec.decode_codex_rollout_to_ledger(
            self.ws_path,
            self.ws_uri,
            raw_lines,
        )
        user_events = [ev for ev in ledger.events if isinstance(ev, UserInputEvent)]
        assistant_events = [ev for ev in ledger.events if isinstance(ev, AssistantTextEvent)]
        thinking_events = [ev for ev in ledger.events if isinstance(ev, ThinkingEvent)]
        tool_calls = [ev for ev in ledger.events if isinstance(ev, ToolCallEvent)]
        tool_results = [ev for ev in ledger.events if isinstance(ev, ToolResultEvent)]

        self.assertGreaterEqual(len(user_events), 2)
        self.assertGreaterEqual(len(assistant_events), 2)
        self.assertEqual(len(thinking_events), 18)
        self.assertEqual(len(tool_calls), 19)
        self.assertEqual(len(tool_results), 19)
        self.assertTrue(
            any(
                any(
                    ext.extension_type in ("encrypted_content", "reasoning_meta")
                    and dict(ext.metadata).get("reasoning_type") in ("reasoning", "magpie_reasoning")
                    for ext in ev.anchor.host_extensions
                )
                for ev in thinking_events
            )
        )

        agy_steps = self.codec.encode_ledger_to_agy_steps(ledger)
        unknown_fields: list[str] = []
        for step_row in agy_steps:
            decode_protobuf(
                step_row.step_payload_blob,
                "gemini_coder.Step",
                self.schema,
                unknown_fields,
            )
        self.assertEqual(unknown_fields, [])

        ledger_from_agy = self.codec.decode_agy_steps_to_ledger(
            self.ws_path,
            self.ws_uri,
            agy_steps,
        )
        self.assertEqual(
            len([ev for ev in ledger_from_agy.events if isinstance(ev, UserInputEvent)]),
            len(user_events),
        )
        self.assertEqual(
            len([ev for ev in ledger_from_agy.events if isinstance(ev, AssistantTextEvent)]),
            len(assistant_events),
        )
        self.assertEqual(
            len([ev for ev in ledger_from_agy.events if isinstance(ev, ThinkingEvent)]),
            18,
        )
        self.assertEqual(
            len([ev for ev in ledger_from_agy.events if isinstance(ev, ToolCallEvent)]),
            19,
        )
        self.assertEqual(
            len([ev for ev in ledger_from_agy.events if isinstance(ev, ToolResultEvent)]),
            19,
        )

        reencoded_codex = self.codec.encode_ledger_to_codex_rollout(ledger)
        ledger_codex_rt = self.codec.decode_codex_rollout_to_ledger(
            self.ws_path,
            self.ws_uri,
            reencoded_codex,
        )
        self.assertEqual(
            len([ev for ev in ledger_codex_rt.events if isinstance(ev, ToolCallEvent)]),
            19,
        )
        self.assertEqual(
            len([ev for ev in ledger_codex_rt.events if isinstance(ev, ToolResultEvent)]),
            19,
        )
        self.assertTrue(
            any(
                any(
                    ext.extension_type in ("encrypted_content", "reasoning_meta")
                    and dict(ext.metadata).get("reasoning_type") in ("reasoning", "magpie_reasoning")
                    for ext in ev.anchor.host_extensions
                )
                for ev in ledger_codex_rt.events
                if isinstance(ev, ThinkingEvent)
            )
        )


class TestUnit2CodecsAndFidelity(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name) / "unit2-workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.ws_path, self.ws_uri = parse_workspace_identity(self.workspace_dir)
        repo_root = Path(__file__).resolve().parent.parent
        self.schema = json.loads((repo_root / "schema" / "antigravity_2_30_schema.json").read_text())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _anchor(self, turn: str = "turn-1", ts: str = "2026-11-10T08:00:00Z") -> EventOriginAnchor:
        return EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId(turn), timestamp_iso=ts)

    def test_2a_repeated_list_payloads_never_duplicate_across_three_roundtrips(self) -> None:
        cases = [
            ("manage_subagents", ManageSubagentsPayload(kind="manage_subagents", action="list", target_conversation_ids=("sub-1", "sub-2"))),
            ("manage_subagents", ManageSubagentsPayload(kind="manage_subagents", action="kill", target_conversation_ids=("sub-kill-1", "sub-kill-2"))),
            ("search_web", SearchWebPayload(kind="search_web", query="protobuf wire", summary="sum", documents=(SearchWebDocument(title="Doc 1", url="https://example.com/1", snippet="s1"), SearchWebDocument(title="Doc 2", url="https://example.com/2", snippet="s2")))),
            ("define_subagent", DefineSubagentPayload(kind="define_subagent", name="audit", tool_names=("read_file", "run_command"))),
            ("list_resources", ListResourcesPayload(kind="list_resources", server_name="ctx", resource_uris=("mcp://ctx/a", "mcp://ctx/b"))),
            ("read_resource", ReadResourcePayload(kind="read_resource", server_name="ctx", uri="mcp://ctx/a", contents_text=("chunk-1", "chunk-2"))),
            ("generate_image", GenerateImagePayload(kind="generate_image", prompt="icon", image_paths=("/sandbox/ref1.png", "/sandbox/ref2.png"), image_uri="file:///sandbox/out.png")),
        ]
        self.assertNotIn("target_conversation_ids", json.loads(serialize_tool_payload_arguments_json(cases[0][1])))
        self.assertEqual(json.loads(serialize_tool_payload_arguments_json(cases[1][1]))["target_conversation_ids"], ["sub-kill-1", "sub-kill-2"])
        self.assertNotIn("documents", json.loads(serialize_tool_payload_arguments_json(cases[2][1])))
        self.assertNotIn("resource_uris", json.loads(serialize_tool_payload_arguments_json(cases[4][1])))
        self.assertNotIn("contents_text", json.loads(serialize_tool_payload_arguments_json(cases[5][1])))

        events: list[object] = [UserInputEvent(kind="user_input", anchor=self._anchor(), query_text="Run 6 tools")]
        for idx, (tname, payload) in enumerate(cases):
            cid = ToolCallId(f"call-2a-{idx}")
            events.append(ToolCallEvent(kind="tool_call", anchor=self._anchor(), call_id=cid, tool_name=tname, tool_summary=tname, payload=payload))
            events.append(ToolResultEvent(kind="tool_result", anchor=self._anchor(), call_id=cid, tool_name=tname, output_text=f"out-{idx}", is_error=False, payload=payload))
        events.append(AssistantTextEvent(kind="assistant_text", anchor=self._anchor(), text="Done"))

        cur = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, events)  # type: ignore[arg-type]
        for _ in range(3):
            agy_steps = encode_agy_trajectory(cur.events)
            cur = decode_agy_trajectory(self.ws_path, self.ws_uri, agy_steps)
            codex_lines = encode_codex_rollout(cur.events, session_id="tid-2a", cwd=str(self.workspace_dir))
            cur = decode_codex_rollout(self.ws_path, self.ws_uri, codex_lines)
            got_calls = [e for e in cur.events if isinstance(e, ToolCallEvent)]
            self.assertEqual([c.payload for c in got_calls], [pl for _, pl in cases])

    def test_2b_unified_diff_headers_file_edit_delete_and_schedule_roundtrip(self) -> None:
        raw_diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,3 +1,3 @@\n keep_top\n-old_line\n+new_line\n"
        edit_pl = FileEditPayload(kind="file_edit", edit_op="MODIFY", path="src/foo.py", unified_diff=raw_diff, diff_stats=(1, 1))
        del_pl = FileEditPayload(kind="file_edit", edit_op="DELETE", path="src/obsolete.py", unified_diff="")
        sched_timer = SchedulePayload(kind="schedule", prompt="wake timer", duration_seconds=0, timer_condition="any")
        sched_cron = SchedulePayload(kind="schedule", prompt="wake cron", cron_expression="*/5 * * * *", is_daemon=True, exact_timing=True, max_iterations=5)

        self.assertNotIn("unified_diff", json.loads(serialize_agy_chat_tool_arguments(del_pl, has_result_step=True)))
        events = [
            UserInputEvent(kind="user_input", anchor=self._anchor(), query_text="Edit and schedule"),
            ToolCallEvent(kind="tool_call", anchor=self._anchor(), call_id=ToolCallId("c-mod"), tool_name="edit_file", tool_summary="mod", payload=edit_pl),
            ToolResultEvent(kind="tool_result", anchor=self._anchor(), call_id=ToolCallId("c-mod"), tool_name="edit_file", output_text="modified", is_error=False, payload=edit_pl),
            ToolCallEvent(kind="tool_call", anchor=self._anchor(), call_id=ToolCallId("c-del"), tool_name="edit_file", tool_summary="del", payload=del_pl),
            ToolResultEvent(kind="tool_result", anchor=self._anchor(), call_id=ToolCallId("c-del"), tool_name="edit_file", output_text="deleted", is_error=False, payload=del_pl),
            ToolCallEvent(kind="tool_call", anchor=self._anchor(), call_id=ToolCallId("c-st"), tool_name="schedule", tool_summary="timer", payload=sched_timer),
            ToolResultEvent(kind="tool_result", anchor=self._anchor(), call_id=ToolCallId("c-st"), tool_name="schedule", output_text="ok", is_error=False, payload=sched_timer),
            ToolCallEvent(kind="tool_call", anchor=self._anchor(), call_id=ToolCallId("c-sc"), tool_name="schedule", tool_summary="cron", payload=sched_cron),
            ToolResultEvent(kind="tool_result", anchor=self._anchor(), call_id=ToolCallId("c-sc"), tool_name="schedule", output_text="ok", is_error=False, payload=sched_cron),
            AssistantTextEvent(kind="assistant_text", anchor=self._anchor(), text="Completed"),
        ]
        agy_steps = encode_agy_trajectory(events)
        unknowns: list[str] = []
        for s in agy_steps:
            decode_protobuf(s.step_payload_blob, "gemini_coder.Step", self.schema, unknowns)
        self.assertEqual(unknowns, [])

        rt_agy = decode_agy_trajectory(self.ws_path, self.ws_uri, agy_steps)
        rt_codex = decode_codex_rollout(self.ws_path, self.ws_uri, encode_codex_rollout(rt_agy.events, session_id="tid-2b", cwd=str(self.workspace_dir)))
        rt_agy_2 = decode_agy_trajectory(self.ws_path, self.ws_uri, encode_agy_trajectory(rt_codex.events))
        got_payloads = [e.payload for e in rt_agy_2.events if isinstance(e, ToolCallEvent)]
        self.assertEqual(got_payloads, [edit_pl, del_pl, sched_timer, sched_cron])
        assert isinstance(got_payloads[0], FileEditPayload)
        self.assertEqual(got_payloads[0].unified_diff, raw_diff)

    def test_2c_user_input_scope_items_roundtrip_in_agy_wire(self) -> None:
        std_ev = UserInputEvent(
            kind="user_input",
            anchor=self._anchor(),
            query_text="Inspect files",
            scope_items=("file:///workspace/a.py", "src/b.py"),
        )
        std_steps = encode_agy_trajectory([std_ev, AssistantTextEvent(kind="assistant_text", anchor=self._anchor(), text="Ok")])
        unknowns: list[str] = []
        decode_protobuf(std_steps[0].step_payload_blob, "gemini_coder.Step", self.schema, unknowns)
        self.assertEqual(unknowns, [])

        decoded_std = decode_agy_trajectory(self.ws_path, self.ws_uri, std_steps)
        got_user = next(e for e in decoded_std.events if isinstance(e, UserInputEvent))
        self.assertEqual(got_user.scope_items, ("file:///workspace/a.py", "src/b.py"))
        self.assertEqual(decoded_std.completed_turn_watermarks[0].boundary_digest, TrajectoryLedger.from_events(self.ws_path, self.ws_uri, [std_ev, AssistantTextEvent(kind="assistant_text", anchor=self._anchor(), text="Ok")]).completed_turn_watermarks[0].boundary_digest)

        raw_ctx_hex = (encode_bytes_field(1, encode_string_field(1, "file:///workspace/c.py")) + encode_varint_field(9, 1)).hex()
        ctx_ev = dataclasses.replace(std_ev, scope_items=(f"ctxpb:{raw_ctx_hex}",))
        rt_ctx = decode_agy_trajectory(self.ws_path, self.ws_uri, encode_agy_trajectory([ctx_ev]))
        self.assertEqual(next(e for e in rt_ctx.events if isinstance(e, UserInputEvent)).scope_items, (f"ctxpb:{raw_ctx_hex}",))

    def test_2d_thinking_signature_and_metadata_unknown_fields_preserved(self) -> None:
        pl = ReadFilePayload(kind="read_file", path="src/app.py")
        events = [
            UserInputEvent(kind="user_input", anchor=self._anchor(), query_text="Read twice"),
            ToolCallEvent(kind="tool_call", anchor=self._anchor(), call_id=ToolCallId("sig-1"), tool_name="read_file", tool_summary="r1", payload=pl, thinking_signature=b"\x01\x02sig-alpha"),
            ToolCallEvent(kind="tool_call", anchor=self._anchor(), call_id=ToolCallId("sig-2"), tool_name="read_file", tool_summary="r2", payload=pl, thinking_signature=b"\x03\x04sig-beta"),
            ToolResultEvent(kind="tool_result", anchor=self._anchor(), call_id=ToolCallId("sig-1"), tool_name="read_file", output_text="a", is_error=False, payload=pl),
            ToolResultEvent(kind="tool_result", anchor=self._anchor(), call_id=ToolCallId("sig-2"), tool_name="read_file", output_text="b", is_error=False, payload=pl),
            AssistantTextEvent(kind="assistant_text", anchor=self._anchor(), text="Read complete"),
        ]
        agy_steps = list(encode_agy_trajectory(events))
        extra_meta = encode_bytes_field(5, encode_string_field(8888, "future-meta-flag"))
        agy_steps[0] = dataclasses.replace(
            agy_steps[0],
            metadata_blob=agy_steps[0].metadata_blob + encode_string_field(8888, "future-meta-flag"),
            step_payload_blob=agy_steps[0].step_payload_blob + extra_meta,
        )

        rt_1 = decode_agy_trajectory(self.ws_path, self.ws_uri, agy_steps)
        self.assertEqual([e.thinking_signature for e in rt_1.events if isinstance(e, ToolCallEvent)], [b"\x01\x02sig-alpha", b"\x03\x04sig-beta"])
        reencoded = encode_agy_trajectory(rt_1.events)
        self.assertIn(b"future-meta-flag", reencoded[0].step_payload_blob)

        rt_codex = decode_codex_rollout(self.ws_path, self.ws_uri, encode_codex_rollout(rt_1.events, session_id="tid-2d", cwd=str(self.workspace_dir)))
        rt_2 = decode_agy_trajectory(self.ws_path, self.ws_uri, encode_agy_trajectory(rt_codex.events))
        self.assertEqual([e.thinking_signature for e in rt_2.events if isinstance(e, ToolCallEvent)], [b"\x01\x02sig-alpha", b"\x03\x04sig-beta"])

    def test_2e_codex_input_image_and_manage_action_mappings(self) -> None:
        img = MediaAttachment(mime_type="image/png", uri="https://example.com/shot.png")
        sub_payloads = [
            ManageSubagentsPayload(kind="manage_subagents", action="list", target_conversation_ids=("s1",)),
            ManageSubagentsPayload(kind="manage_subagents", action="kill", target_conversation_ids=("s1",)),
            ManageSubagentsPayload(kind="manage_subagents", action="kill_all"),
        ]
        task_payloads = [
            ManageTaskPayload(kind="manage_task", action="list"),
            ManageTaskPayload(kind="manage_task", action="status", task_id="t-1"),
            ManageTaskPayload(kind="manage_task", action="send_input", task_id="t-1", input_text="yes\n"),
            ManageTaskPayload(kind="manage_task", action="kill", task_id="t-1"),
        ]
        events: list[object] = [UserInputEvent(kind="user_input", anchor=self._anchor(), query_text="Check image", media=(img,))]
        for idx, pl in enumerate([*sub_payloads, *task_payloads]):
            tname = "manage_subagents" if isinstance(pl, ManageSubagentsPayload) else "manage_task"
            cid = ToolCallId(f"m-{idx}")
            events.append(ToolCallEvent(kind="tool_call", anchor=self._anchor(), call_id=cid, tool_name=tname, tool_summary=tname, payload=pl))
            events.append(ToolResultEvent(kind="tool_result", anchor=self._anchor(), call_id=cid, tool_name=tname, output_text="ok", is_error=False, payload=pl))
        events.append(AssistantTextEvent(kind="assistant_text", anchor=self._anchor(), text="Managed"))

        lines = encode_codex_rollout(events, session_id="tid-2e", cwd=str(self.workspace_dir))  # type: ignore[arg-type]
        user_record = next(json.loads(l.raw_json) for l in lines if json.loads(l.raw_json)["type"] == "response_item" and json.loads(l.raw_json)["payload"].get("role") == "user")
        self.assertEqual(user_record["payload"]["content"][1]["image_url"], "https://example.com/shot.png")

        legacy_line = CodexRawRolloutLine(line_no=RolloutLineNo(99), raw_json=json.dumps({"timestamp": "2026-11-10T08:01:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_image", "mime_type": "image/jpeg", "uri": "https://example.com/legacy.jpg"}]}}))
        rt = decode_codex_rollout(self.ws_path, self.ws_uri, [*lines, legacy_line])
        users = [e for e in rt.events if isinstance(e, UserInputEvent)]
        self.assertEqual([users[0].media[0].uri, users[1].media[0].uri], ["https://example.com/shot.png", "https://example.com/legacy.jpg"])
        self.assertEqual([e.payload for e in rt.events if isinstance(e, ToolCallEvent)], [*sub_payloads, *task_payloads])

    def test_2f_agy_error_message_step_17_and_opaque_step_preserved(self) -> None:
        user_steps = list(encode_agy_trajectory([UserInputEvent(kind="user_input", anchor=self._anchor(), query_text="Trigger quota error")]))
        err_details = encode_string_field(1, "Quota exhausted") + encode_string_field(2, "RESOURCE_EXHAUSTED") + encode_string_field(3, "HTTP 429 from upstream")
        err_body = encode_bytes_field(3, err_details) + encode_varint_field(4, 1) + encode_varint_field(5, 1)
        err_meta = encode_varint_field(3, 4)
        err_proto = encode_varint_field(1, int(CortexStepTypeCode.ERROR_MESSAGE)) + encode_varint_field(4, 6) + encode_bytes_field(5, err_meta) + encode_bytes_field(int(GeminiStepWireTag.ERROR_MESSAGE_TAG), err_body)
        opaque_meta = encode_varint_field(3, 5)
        opaque_proto = encode_varint_field(1, 99) + encode_varint_field(4, 3) + encode_bytes_field(5, opaque_meta) + encode_bytes_field(90, b"opaque-body")

        rows = [
            user_steps[0],
            AgyRawStepRow(idx=StepIndex(1), step_type=int(CortexStepTypeCode.ERROR_MESSAGE), status=6, has_subtrajectory=False, metadata_blob=err_meta, step_payload_blob=err_proto),
            AgyRawStepRow(idx=StepIndex(2), step_type=99, status=3, has_subtrajectory=False, metadata_blob=opaque_meta, step_payload_blob=opaque_proto),
        ]
        ledger = decode_agy_trajectory(self.ws_path, self.ws_uri, rows)
        sys_evs = [e for e in ledger.events if isinstance(e, SystemEvent)]
        self.assertEqual([e.event_type for e in sys_evs], ["error_message", "opaque_agy_step:99"])
        self.assertIn("Quota exhausted", sys_evs[0].message_text)

        reencoded = encode_agy_trajectory(ledger.events)
        self.assertEqual([r.step_type for r in reencoded], [int(CortexStepTypeCode.USER_INPUT), int(CortexStepTypeCode.ERROR_MESSAGE), 99])
        unknowns: list[str] = []
        decode_protobuf(reencoded[1].step_payload_blob, "gemini_coder.Step", self.schema, unknowns)
        self.assertEqual(unknowns, [])

        via_codex = decode_codex_rollout(self.ws_path, self.ws_uri, encode_codex_rollout(ledger.events, session_id="tid-2f", cwd=str(self.workspace_dir)))
        reencoded_2 = encode_agy_trajectory(via_codex.events)
        self.assertEqual([r.step_type for r in reencoded_2 if r.step_type not in (101, 81)], [int(CortexStepTypeCode.USER_INPUT), int(CortexStepTypeCode.ERROR_MESSAGE), 99])

    def test_2g_codex_rollout_variants_zstd_rpc_and_legacy_user_dedup(self) -> None:
        raw_records = [
            {"timestamp": "2026-11-10T09:00:00Z", "type": "session_meta", "payload": {"id": "tid-2g", "cwd": str(self.workspace_dir)}},
            {"timestamp": "2026-11-10T09:00:01Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
            {"timestamp": "2026-11-10T09:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Line with \u2028 sep"}},
            {"timestamp": "2026-11-10T09:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Line with \u2028 sep"}]}},
            {"timestamp": "2026-11-10T09:00:02Z", "type": "event_msg", "payload": {"type": "system_message", "message": "System notice"}},
            {"timestamp": "2026-11-10T09:00:02Z", "type": "response_item", "payload": {"type": "additional_tools", "tools": ["mcp_x"]}},
            {"timestamp": "2026-11-10T09:00:03Z", "type": "response_item", "payload": {"type": "web_search_call", "id": "ws-1", "status": "completed", "action": {"type": "search", "query": "rfc 9000"}}},
            {"timestamp": "2026-11-10T09:00:04Z", "type": "response_item", "payload": {"type": "agent_message", "id": "am-1", "content": [{"type": "output_text", "text": "QUIC answer"}]}},
            {"timestamp": "2026-11-10T09:00:05Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}},
            {"timestamp": "2026-11-10T09:01:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2"}},
            {"timestamp": "2026-11-10T09:01:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Repeat prompt"}]}},
            {"timestamp": "2026-11-10T09:01:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Repeat prompt"}]}},
            {"timestamp": "2026-11-10T09:01:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Ack both"}]}},
            {"timestamp": "2026-11-10T09:01:04Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t2"}},
        ]
        jsonl_bytes = ("\n".join(json.dumps(r, ensure_ascii=False) for r in raw_records) + "\n").encode("utf-8")
        zst_path = Path(self.temp_dir.name) / "rollout-test.jsonl.zst"
        from compression import zstd  # type: ignore[attr-defined]

        zst_path.write_bytes(zstd.compress(jsonl_bytes))
        lines = read_codex_rollout_lines(zst_path)
        self.assertEqual(len(lines), len(raw_records))

        ledger = decode_codex_rollout(self.ws_path, self.ws_uri, lines)
        t1_users = [e for e in ledger.events if isinstance(e, UserInputEvent) and str(e.anchor.turn_id) == "turn-1"]
        t2_users = [e for e in ledger.events if isinstance(e, UserInputEvent) and str(e.anchor.turn_id) == "turn-2"]
        self.assertEqual(len(t1_users), 1)
        self.assertEqual(len(t2_users), 2)
        self.assertEqual([e.text for e in ledger.events if isinstance(e, AssistantTextEvent)], ["QUIC answer", "Ack both"])
        ws_call, ws_res = ledger.call_and_result(ToolCallId("ws-1"))
        self.assertEqual(ws_call.payload, SearchWebPayload(kind="search_web", query="rfc 9000"))
        self.assertIsNotNone(ws_res)
        self.assertEqual(len(ledger.completed_turn_watermarks), 2)

        rt = decode_codex_rollout(self.ws_path, self.ws_uri, encode_codex_rollout(ledger.events, session_id="tid-2g", cwd=str(self.workspace_dir)))
        self.assertIn("system_message", [e.event_type for e in rt.events if isinstance(e, SystemEvent)])
        self.assertIn("codex_response_item:additional_tools", [e.event_type for e in rt.events if isinstance(e, SystemEvent)])

        rpc_ledger = decode_codex_rpc_thread_to_ledger(
            self.ws_path,
            self.ws_uri,
            {"id": "rpc-1", "name": "RPC title", "turns": [{"id": "turn-a", "status": "completed", "items": [{"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "Hello RPC"}]}, {"type": "agentMessage", "id": "a1", "text": "Hi from RPC"}]}]},
        )
        self.assertEqual(rpc_ledger.title, "RPC title")
        self.assertEqual(len(rpc_ledger.completed_turn_watermarks), 1)
