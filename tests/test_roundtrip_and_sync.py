from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from codex_agy_sync.bridge import (
    CsrfSecret,
    FORBIDDEN_AGY_PROJECT_IDS,
    ForkResolutionPolicy,
    HostEnvironmentPaths,
    SyncBridge,
    SyncDirection,
    TransportMode,
)
from codex_agy_sync.codecs import (
    AgyRawStepRow,
    CodexRawRolloutLine,
    STEP_TAG_TO_SPEC,
    STEP_WIRE_TABLE,
    TOOL_CODEC_TABLE,
    TrajectoryWireCodec,
)
from codex_agy_sync.ledger import (
    _encode_bytes_field,
    _encode_string_field,
    _encode_varint,
    _encode_varint_field,
    AgyGuiSafeSource,
    AgyProjectId,
    AskQuestionPayload,
    AssistantTextEvent,
    CascadeId,
    CheckpointEvent,
    CodexProjectId,
    CodexThreadId,
    DefineSubagentPayload,
    EventOriginAnchor,
    FileEditPayload,
    FinishPayload,
    GenerateImagePayload,
    HostKind,
    InvokeSubagentPayload,
    ListResourcesPayload,
    ManageSubagentsPayload,
    ManageTaskPayload,
    MomaSearchPayload,
    OpaqueToolPayload,
    QuestionItem,
    QuestionOption,
    ReadFilePayload,
    ReadResourcePayload,
    RolloutLineNo,
    RunCommandPayload,
    SchedulePayload,
    SearchWebDocument,
    SearchWebPayload,
    SendMessagePayload,
    StepIndex,
    TaskBoundaryEvent,
    ThinkingEvent,
    TokenUsageAccounting,
    ToolCallEvent,
    ToolCallId,
    ToolResultEvent,
    TrajectoryLedger,
    TurnId,
    UnknownWireField,
    UserInputEvent,
    parse_workspace_identity,
    resolve_canonical_title,
)
from scripts.verify_session_mapping import decode_protobuf


class TestLedgerBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name) / "sample-workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.ws_path, self.ws_uri = parse_workspace_identity(self.workspace_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_workspace_identity_normalizes_paths_and_uris(self) -> None:
        symlink_path = Path(self.temp_dir.name) / "ws-link"
        os.symlink(self.workspace_dir, symlink_path)

        from_str = parse_workspace_identity(str(self.workspace_dir) + "///")
        from_uri = parse_workspace_identity(self.workspace_dir.as_uri() + "/")
        from_symlink = parse_workspace_identity(symlink_path)

        self.assertEqual(from_str, from_uri)
        self.assertEqual(from_str, from_symlink)
        self.assertFalse(str(from_str[1]).endswith("/"))
        self.assertTrue(str(from_str[1]).startswith("file:///"))

        for invalid in ("", "   ", "/", "file://", "http://example.com/repo", "."):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    parse_workspace_identity(invalid)

    def test_gui_safe_source_excludes_cli_source_17(self) -> None:
        enum_codes = {src.proto_enum_value for src in AgyGuiSafeSource}
        self.assertNotIn(17, enum_codes)
        self.assertEqual(AgyGuiSafeSource.CASCADE_CLIENT.proto_enum_value, 1)
        self.assertEqual(
            AgyGuiSafeSource.CASCADE_CLIENT.summary_column_name,
            "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
        )
        self.assertEqual(AgyGuiSafeSource.JETBOX.proto_enum_value, 18)
        self.assertEqual(
            AgyGuiSafeSource.JETBOX.summary_column_name,
            "CORTEX_TRAJECTORY_SOURCE_JETBOX",
        )

    def test_resolve_canonical_title_precedence_order(self) -> None:
        anchor_t1 = EventOriginAnchor(
            origin_host=HostKind.ANTIGRAVITY,
            turn_id=TurnId("turn-1"),
            timestamp_iso="2026-11-10T01:00:00Z",
            agy_step_index=StepIndex(0),
        )
        events = [
            UserInputEvent(
                kind="user_input",
                anchor=anchor_t1,
                query_text="<USER_REQUEST>\nFirst user prompt line\nSecond line\n</USER_REQUEST>",
            ),
            CheckpointEvent(
                kind="checkpoint",
                anchor=anchor_t1,
                checkpoint_index=1,
                user_intent="intent 1",
                session_summary="summary 1",
                code_change_summary="",
                conversation_title="Earlier Checkpoint Title",
            ),
            CheckpointEvent(
                kind="checkpoint",
                anchor=anchor_t1,
                checkpoint_index=2,
                user_intent="intent 2",
                session_summary="summary 2",
                code_change_summary="",
                conversation_title="Latest Checkpoint Title",
            ),
        ]

        self.assertEqual(
            resolve_canonical_title(
                events,
                annotation_pbtxt_title="Pbtxt Title",
                cascade_summary_annotation_title="Summary Proto Title",
                db_summary_row_title="DB Row Title",
            ),
            "Pbtxt Title",
        )
        self.assertEqual(
            resolve_canonical_title(
                events,
                annotation_pbtxt_title="  ",
                cascade_summary_annotation_title="Summary Proto Title",
                db_summary_row_title="DB Row Title",
            ),
            "Summary Proto Title",
        )
        self.assertEqual(
            resolve_canonical_title(
                events,
                db_summary_row_title="DB Row Title",
            ),
            "DB Row Title",
        )
        self.assertEqual(
            resolve_canonical_title(events),
            "Latest Checkpoint Title",
        )
        self.assertEqual(
            resolve_canonical_title([events[0]]),
            "First user prompt line Second line",
        )
        self.assertEqual(resolve_canonical_title([]), "Untitled Session")

    def test_ledger_indices_watermarks_and_inflight_exclusion(self) -> None:
        t1 = TurnId("turn-1")
        t2 = TurnId("turn-2")
        call_1 = ToolCallId("call-read-1")
        call_2 = ToolCallId("call-run-2")

        events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T01:00:00Z",
                    agy_step_index=StepIndex(0),
                ),
                query_text="Inspect README and run check",
            ),
            ThinkingEvent(
                kind="thinking",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T01:00:01Z",
                    agy_step_index=StepIndex(1),
                ),
                summary_text="Planning to read file and run command.",
                token_usage=TokenUsageAccounting(input_tokens=100, output_tokens=25),
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T01:00:01Z",
                    agy_step_index=StepIndex(1),
                ),
                text="Reading README and running check now.",
                token_usage=TokenUsageAccounting(input_tokens=100, output_tokens=25),
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T01:00:01Z",
                    agy_step_index=StepIndex(1),
                ),
                call_id=call_1,
                tool_name="read_file",
                tool_summary="Read README",
                payload=ReadFilePayload(kind="read_file", path="README.md"),
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T01:00:01Z",
                    agy_step_index=StepIndex(1),
                ),
                call_id=call_2,
                tool_name="run_command",
                tool_summary="Run check",
                payload=RunCommandPayload(
                    kind="run_command",
                    command_line="./scripts/check",
                    cwd=str(self.ws_path),
                ),
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T01:00:02Z",
                    agy_step_index=StepIndex(2),
                ),
                call_id=call_1,
                tool_name="read_file",
                output_text="# README content",
                is_error=False,
                payload=ReadFilePayload(kind="read_file", path="README.md", file_num_lines=10),
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T01:00:03Z",
                    agy_step_index=StepIndex(3),
                ),
                call_id=call_2,
                tool_name="run_command",
                output_text="All checks passed.",
                is_error=False,
                payload=RunCommandPayload(
                    kind="run_command",
                    command_line="./scripts/check",
                    cwd=str(self.ws_path),
                    exit_code=0,
                ),
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T01:00:04Z",
                    agy_step_index=StepIndex(4),
                ),
                text="Checks succeeded.",
            ),
        ]

        ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, events)
        self.assertFalse(ledger.has_inflight_turn)
        self.assertEqual(len(ledger.completed_turn_watermarks), 1)

        step_1_events = ledger.events_for_step(StepIndex(1))
        self.assertEqual(len(step_1_events), 4)
        call_ev, res_ev = ledger.call_and_result(call_2)
        self.assertEqual(call_ev.tool_name, "run_command")
        self.assertIsNotNone(res_ev)
        assert res_ev is not None
        self.assertEqual(res_ev.output_text, "All checks passed.")

        wm1 = ledger.latest_watermark()
        self.assertIsNotNone(wm1)
        assert wm1 is not None
        self.assertEqual(wm1.turn_id, t1)
        self.assertEqual(wm1.last_agy_step_index, StepIndex(4))
        self.assertEqual(len(ledger.slice_after_watermark(None)), len(events))
        self.assertEqual(ledger.slice_after_watermark(t1), ())

        inflight_events = list(events) + [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t2,
                    timestamp_iso="2026-11-10T01:05:00Z",
                    agy_step_index=StepIndex(5),
                ),
                query_text="Start a second turn with an unresolved tool call",
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=t2,
                    timestamp_iso="2026-11-10T01:05:01Z",
                    agy_step_index=StepIndex(6),
                ),
                call_id=ToolCallId("call-unfinished-3"),
                tool_name="run_command",
                tool_summary="Long running build",
                payload=RunCommandPayload(
                    kind="run_command",
                    command_line="make all",
                    cwd=str(self.ws_path),
                ),
            ),
        ]
        inflight_ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, inflight_events)
        self.assertTrue(inflight_ledger.has_inflight_turn)
        self.assertEqual(len(inflight_ledger.completed_turn_watermarks), 1)
        self.assertEqual(inflight_ledger.slice_after_watermark(t1), ())
        self.assertEqual(len(inflight_ledger.slice_after_watermark(None)), len(events))

    def test_companion_projections_enforce_sidebar_and_schema_invariants(self) -> None:
        t1 = TurnId("turn-1")
        events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T02:00:00Z",
                    codex_line_no=RolloutLineNo(1),
                ),
                query_text="Verify companion projections",
            ),
            ThinkingEvent(
                kind="thinking",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T02:00:01Z",
                    codex_line_no=RolloutLineNo(2),
                ),
                summary_text="Checking projection requirements.",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T02:00:02Z",
                    codex_line_no=RolloutLineNo(3),
                ),
                text="Projections verified.",
            ),
            TaskBoundaryEvent(
                kind="task_boundary",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=t1,
                    timestamp_iso="2026-11-10T02:00:03Z",
                    codex_line_no=RolloutLineNo(4),
                ),
                boundary_kind="task_boundary",
                task_name="Projection Check",
                task_status="task_complete",
                task_summary="Completed projection check.",
            ),
        ]
        ledger = TrajectoryLedger.from_events(
            self.ws_path,
            self.ws_uri,
            events,
            explicit_title_override="Projection Verification Title",
        )
        cascade_id = CascadeId("11111111-2222-3333-4444-555555555555")
        project_id = AgyProjectId("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        codex_tid = CodexThreadId("01a1ae7d-0000-7000-8000-000000000001")
        codex_pid = CodexProjectId("01a1ae7d-0000-7000-8000-000000000002")

        transcript_rows = ledger.project_transcript_rows()
        self.assertEqual(len(transcript_rows), 3)
        self.assertEqual(transcript_rows[0].step_type_name, "USER_INPUT")
        self.assertEqual(transcript_rows[1].step_type_name, "PLANNER_RESPONSE")
        self.assertEqual(transcript_rows[1].thinking, "Checking projection requirements.")
        self.assertEqual(transcript_rows[1].content, "Projections verified.")
        self.assertEqual(transcript_rows[2].step_type_name, "TASK_BOUNDARY")

        annot = ledger.project_annotation_pbtxt(cascade_id, "2026-11-10T02:05:00Z")
        pbtxt_text = annot.render_pbtxt()
        self.assertIn('title: "Projection Verification Title"', pbtxt_text)
        self.assertIn("last_user_view_time {", pbtxt_text)
        self.assertIn("seconds:", pbtxt_text)

        summary = ledger.project_agy_cascade_summary(
            cascade_id=cascade_id,
            project_id=project_id,
            viewed_at_iso="2026-11-10T02:05:00Z",
            source=AgyGuiSafeSource.JETBOX,
        )
        self.assertEqual(summary.title, annot.title)
        self.assertEqual(summary.annotations.title, annot.title)
        self.assertEqual(summary.nesting_depth, 0)
        self.assertTrue(len(summary.summary_proto_bytes) > 0)

        repo_root = Path(__file__).resolve().parent.parent
        schema = json.loads((repo_root / "schema" / "antigravity_2_30_schema.json").read_text())
        unknown_fields: list[str] = []
        decoded_summary = decode_protobuf(
            summary.summary_proto_bytes,
            "exa.jetski_cortex_pb.CascadeTrajectorySummary",
            schema,
            unknown_fields,
        )
        self.assertEqual(unknown_fields, [])
        self.assertEqual(decoded_summary["annotations"]["title"], "Projection Verification Title")
        self.assertEqual(decoded_summary["trajectory_metadata"]["project_id"], str(project_id))

        hub_state = ledger.project_agy_hub_summaries_state(
            existing_state=None,
            updated_summary=summary,
            conv_db_path="/tmp/conversations/11111111-2222-3333-4444-555555555555.db",
            conv_db_mtime=1794200000,
        )
        decoded_hub = decode_protobuf(
            hub_state.proto_bytes,
            "jetbox_summaries_pb.SummariesState",
            schema,
            unknown_fields,
        )
        self.assertEqual(unknown_fields, [])
        self.assertEqual(len(decoded_hub["summaries"]), 1)

        codex_summary = ledger.project_codex_thread_summary(codex_tid, codex_pid)
        self.assertEqual(codex_summary.archived, 0)
        self.assertEqual(codex_summary.preview, "Verify companion projections")
        self.assertEqual(codex_summary.project_id, codex_pid)

        with self.assertRaises(ValueError):
            ledger.project_agy_cascade_summary(
                cascade_id=cascade_id,
                project_id=AgyProjectId("default-cli-project"),
            )


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
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=turn_id,
                    timestamp_iso="2026-11-10T03:00:00Z",
                ),
                query_text="Run all 16 tools plus opaque fallback",
            )
        ]
        for idx, payload in enumerate(payloads):
            call_id = ToolCallId(f"call-{idx}")
            tool_name = payload.tool_name if isinstance(payload, OpaqueToolPayload) else payload.kind
            events.append(
                ToolCallEvent(
                    kind="tool_call",
                    anchor=EventOriginAnchor(
                        origin_host=HostKind.ANTIGRAVITY,
                        turn_id=turn_id,
                        timestamp_iso="2026-11-10T03:00:01Z",
                    ),
                    call_id=call_id,
                    tool_name=tool_name,
                    tool_summary=f"Summary {idx}",
                    payload=payload,
                )
            )
            events.append(
                ToolResultEvent(
                    kind="tool_result",
                    anchor=EventOriginAnchor(
                        origin_host=HostKind.ANTIGRAVITY,
                        turn_id=turn_id,
                        timestamp_iso="2026-11-10T03:00:02Z",
                    ),
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
        unknown_tag_9999 = _encode_varint((9999 << 3) | 2) + _encode_varint(5) + b"hello"
        user_body = _encode_string_field(5, "Test unknown field preservation")
        step_blob = (
            _encode_varint_field(1, 14)
            + _encode_varint_field(4, 3)
            + _encode_bytes_field(19, user_body)
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


class TestBridgeAndRoundTripSync(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace_dir = self.root / "agent-fleet"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.root / "gemini-config",
            agy_data_dir=self.root / "antigravity",
            codex_home_dir=self.root / "codex",
            sync_state_dir=self.root / "sync-state",
        )
        self.bridge = SyncBridge(paths=self.paths)
        repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = repo_root
        self.schema = json.loads((repo_root / "schema" / "antigravity_2_30_schema.json").read_text())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _copy_real_agy_session_into_temp(self, cascade_id: str) -> CascadeId:
        real_agy = Path.home() / ".gemini" / "antigravity"
        real_conv = real_agy / "conversations" / f"{cascade_id}.db"
        if not real_conv.is_file():
            self.skipTest(f"Real Antigravity session {cascade_id} not found")

        (self.paths.agy_data_dir / "conversations").mkdir(parents=True, exist_ok=True)
        shutil.copy2(real_conv, self.paths.agy_data_dir / "conversations" / f"{cascade_id}.db")

        real_annot = real_agy / "annotations" / f"{cascade_id}.pbtxt"
        if real_annot.is_file():
            (self.paths.agy_data_dir / "annotations").mkdir(parents=True, exist_ok=True)
            shutil.copy2(real_annot, self.paths.agy_data_dir / "annotations" / f"{cascade_id}.pbtxt")

        real_sum = real_agy / "conversation_summaries.db"
        if real_sum.is_file():
            src_conn = sqlite3.connect(f"file:{real_sum}?mode=ro", uri=True)
            try:
                row = src_conn.execute(
                    "SELECT title, preview, step_count, last_modified_time, workspace_uris, status, source, project_id, last_user_input_time, last_user_input_step_index, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                    (cascade_id,),
                ).fetchall()
            finally:
                src_conn.close()
            if row:
                r = row[0]
                dst_sum = self.paths.agy_data_dir / "conversation_summaries.db"
                dconn = sqlite3.connect(str(dst_sum))
                try:
                    from codex_agy_sync.bridge import _ensure_agy_summaries_db_schema

                    _ensure_agy_summaries_db_schema(dconn)
                    dconn.execute(
                        """
                        INSERT OR REPLACE INTO conversation_summaries (
                            conversation_id, title, preview, step_count, last_modified_time,
                            workspace_uris, status, source, project_id, last_user_input_time,
                            last_user_input_step_index, raw_summary
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (cascade_id, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10]),
                    )
                    dconn.commit()
                finally:
                    dconn.close()
        return CascadeId(cascade_id)

    def _copy_real_codex_session_into_temp(self, thread_id: str) -> CodexThreadId:
        real_codex = Path.home() / ".codex"
        matches = list((real_codex / "sessions").rglob(f"*{thread_id}.jsonl"))
        if not matches:
            self.skipTest(f"Real Codex rollout for {thread_id} not found")

        dst_dir = self.paths.codex_home_dir / "sessions" / "2026" / "11" / "10"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_rollout = dst_dir / matches[0].name
        shutil.copy2(matches[0], dst_rollout)

        real_state = real_codex / "state_5.sqlite"
        title = "Copied Codex Session"
        preview = "Copied preview"
        if real_state.is_file():
            sconn = sqlite3.connect(f"file:{real_state}?mode=ro", uri=True)
            try:
                trow = sconn.execute(
                    "SELECT title, preview FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
                if trow:
                    title = str(trow[0] or title)
                    preview = str(trow[1] or preview)
            finally:
                sconn.close()

        binding = self.bridge.pair_workspace(self.workspace_dir)
        dst_state = self.paths.codex_home_dir / "state_5.sqlite"
        dconn = sqlite3.connect(str(dst_state))
        try:
            from codex_agy_sync.bridge import _ensure_codex_state_db_schema

            _ensure_codex_state_db_schema(dconn)
            dconn.execute(
                """
                INSERT OR REPLACE INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider,
                    cwd, title, sandbox_policy, approval_mode, tokens_used, has_user_event,
                    archived, cli_version, first_user_message, created_at_ms, updated_at_ms,
                    preview, recency_at, recency_at_ms, name, project_id, originator
                ) VALUES (?, ?, 1794200000, 1794200100, 'desktop', 'openai', ?, ?, 'workspace-write', 'never', 0, 1, 0, '0.166.0', ?, 1794200000000, 1794200100000, ?, 1794200100, 1794200100000, ?, ?, 'codex_vscode')
                """,
                (
                    thread_id,
                    str(dst_rollout),
                    str(binding.workspace_path),
                    title,
                    preview,
                    preview,
                    title,
                    str(binding.codex_project_id),
                ),
            )
            dconn.commit()
        finally:
            dconn.close()

        return CodexThreadId(thread_id)

    def test_csrf_secret_redaction_and_idempotent_project_pairing(self) -> None:
        secret = CsrfSecret("secret-token-value-12345")
        self.assertEqual(str(secret), "[REDACTED_CSRF]")
        self.assertEqual(repr(secret), "CsrfSecret('[REDACTED_CSRF]')")
        self.assertEqual(f"{secret}", "[REDACTED_CSRF]")
        self.assertEqual(
            dict(secret.as_loopback_header()),
            {"x-codeium-csrf-token": "secret-token-value-12345"},
        )
        with self.assertRaises(ValueError):
            CsrfSecret("   ")

        b1 = self.bridge.pair_workspace(self.workspace_dir)
        b2 = self.bridge.pair_workspace(self.workspace_dir.as_uri() + "/")
        self.assertEqual(b1, b2)
        self.assertNotIn(str(b1.agy_project_id), FORBIDDEN_AGY_PROJECT_IDS)

        agy_proj_file = self.paths.gemini_config_dir / "projects" / f"{b1.agy_project_id}.json"
        self.assertTrue(agy_proj_file.is_file())

        gs_file = self.paths.codex_home_dir / ".codex-global-state.json"
        self.assertTrue(gs_file.is_file())
        gs_data = json.loads(gs_file.read_text(encoding="utf-8"))
        self.assertIn(str(b1.codex_legacy_project_id), gs_data["local-projects"])

    def test_real_session_isolated_roundtrip_agy_to_codex_and_back(self) -> None:
        src_cid = self._copy_real_agy_session_into_temp("eb95dc40-cb5e-4017-b4ff-f41223e1f701")

        receipt_1 = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=src_cid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(receipt_1.transition_kind, "initial_import")
        self.assertGreater(receipt_1.events_applied, 200)
        synced_tid = receipt_1.codex_thread_id

        noop_receipt = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=src_cid,
            codex_thread_id=synced_tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(noop_receipt.transition_kind, "no_op")
        self.assertEqual(noop_receipt.events_applied, 0)

        codex_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            codex_thread_id=synced_tid,
        )
        self.assertEqual(
            len([e for e in codex_ledger.events if isinstance(e, ToolCallEvent)]),
            97,
        )
        self.assertEqual(
            len([e for e in codex_ledger.events if isinstance(e, ToolResultEvent)]),
            97,
        )

        roundtrip_cid = CascadeId("99999999-8888-7777-6666-555555555555")
        receipt_2 = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=roundtrip_cid,
            codex_thread_id=synced_tid,
            direction=SyncDirection.CODEX_TO_AGY,
        )
        self.assertEqual(receipt_2.transition_kind, "initial_import")
        self.assertEqual(receipt_2.cascade_id, roundtrip_cid)

        rt_agy_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            cascade_id=roundtrip_cid,
        )
        self.assertEqual(
            len([e for e in rt_agy_ledger.events if isinstance(e, ToolCallEvent)]),
            97,
        )
        self.assertEqual(
            len([e for e in rt_agy_ledger.events if isinstance(e, ToolResultEvent)]),
            97,
        )
        self.assertEqual(rt_agy_ledger.title, codex_ledger.title)

        annot_pbtxt = self.paths.agy_data_dir / "annotations" / f"{roundtrip_cid}.pbtxt"
        self.assertTrue(annot_pbtxt.is_file())
        transcript_path = (
            self.paths.agy_data_dir
            / "brain"
            / str(roundtrip_cid)
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        self.assertTrue(transcript_path.is_file())
        hub_pb = self.paths.agy_data_dir / "agyhub_summaries_proto.pb"
        self.assertTrue(hub_pb.is_file())

    def test_real_session_isolated_roundtrip_codex_to_agy_and_back(self) -> None:
        src_tid = self._copy_real_codex_session_into_temp("01a1ae7d-d56f-7650-8336-aa848b9f2a5a")

        receipt_1 = self.bridge.sync_session(
            self.workspace_dir,
            codex_thread_id=src_tid,
            direction=SyncDirection.CODEX_TO_AGY,
        )
        self.assertEqual(receipt_1.transition_kind, "initial_import")
        synced_cid = receipt_1.cascade_id

        agy_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            cascade_id=synced_cid,
        )
        self.assertEqual(
            len([e for e in agy_ledger.events if isinstance(e, ThinkingEvent)]),
            18,
        )
        self.assertEqual(
            len([e for e in agy_ledger.events if isinstance(e, ToolCallEvent)]),
            19,
        )
        self.assertEqual(
            len([e for e in agy_ledger.events if isinstance(e, ToolResultEvent)]),
            19,
        )

        roundtrip_tid = CodexThreadId("01a1ae7d-ffff-7000-8000-000000000099")
        receipt_2 = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=synced_cid,
            codex_thread_id=roundtrip_tid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(receipt_2.transition_kind, "initial_import")

        rt_codex_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            codex_thread_id=roundtrip_tid,
        )
        self.assertEqual(
            len([e for e in rt_codex_ledger.events if isinstance(e, ThinkingEvent)]),
            18,
        )
        self.assertEqual(
            len([e for e in rt_codex_ledger.events if isinstance(e, ToolCallEvent)]),
            19,
        )
        self.assertEqual(
            len([e for e in rt_codex_ledger.events if isinstance(e, ToolResultEvent)]),
            19,
        )

    def test_incremental_append_divergence_fork_overwrite_and_presence_lock(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("aaaa1111-2222-3333-4444-555555555555")
        tid = CodexThreadId("01a1ae7d-aaaa-7000-8000-111111111111")

        t1_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T04:00:00Z",
                ),
                query_text="Initial turn 1 prompt",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T04:00:01Z",
                ),
                text="Initial turn 1 response",
            ),
        ]
        t1_ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            t1_events,
            explicit_title_override="Incremental & Fork Test Session",
        )
        locks = self.bridge.guard.probe_locks(cid, tid)
        self.bridge._transport.apply_to_agy(
            binding=binding,
            cascade_id=cid,
            events=t1_events,
            full_ledger=t1_ledger,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )

        r_init = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(r_init.transition_kind, "initial_import")

        t2_events = list(t1_events) + [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T04:05:00Z",
                ),
                query_text="Incremental turn 2 prompt",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T04:05:01Z",
                ),
                text="Incremental turn 2 response",
            ),
        ]
        t2_ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            t2_events,
            explicit_title_override="Incremental & Fork Test Session",
        )
        self.bridge._transport.apply_to_agy(
            binding=binding,
            cascade_id=cid,
            events=t2_events,
            full_ledger=t2_ledger,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )

        r_inc = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(r_inc.transition_kind, "incremental_append")
        self.assertEqual(r_inc.events_applied, 2)

        agy_t3_events = list(t2_events) + [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-3-agy"),
                    timestamp_iso="2026-11-10T04:10:00Z",
                ),
                query_text="AGY independent turn 3",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-3-agy"),
                    timestamp_iso="2026-11-10T04:10:01Z",
                ),
                text="AGY independent response 3",
            ),
        ]
        codex_t3_events = list(t2_events) + [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-3-codex"),
                    timestamp_iso="2026-11-10T04:11:00Z",
                ),
                query_text="Codex independent turn 3",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-3-codex"),
                    timestamp_iso="2026-11-10T04:11:01Z",
                ),
                text="Codex independent response 3",
            ),
        ]
        self.bridge._transport.apply_to_agy(
            binding=binding,
            cascade_id=cid,
            events=agy_t3_events,
            full_ledger=TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, agy_t3_events),
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )
        self.bridge._transport.apply_to_codex(
            binding=binding,
            thread_id=tid,
            events=codex_t3_events,
            full_ledger=TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, codex_t3_events),
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )

        r_halt = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AGY_TO_CODEX,
            fork_policy=ForkResolutionPolicy.HALT_ON_DIVERGENCE,
        )
        self.assertEqual(r_halt.transition_kind, "diverged_halted")
        self.assertEqual(r_halt.events_applied, 0)

        r_fork = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AGY_TO_CODEX,
            fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION,
        )
        self.assertEqual(r_fork.transition_kind, "diverged_forked")
        self.assertEqual(r_fork.forked_from_codex_thread_id, tid)
        self.assertNotEqual(r_fork.codex_thread_id, tid)

        r_overwrite = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.CODEX_TO_AGY,
            fork_policy=ForkResolutionPolicy.BACKUP_AND_OVERWRITE_TARGET,
        )
        self.assertEqual(r_overwrite.transition_kind, "diverged_overwritten")
        self.assertIsNotNone(r_overwrite.backup_manifest)
        assert r_overwrite.backup_manifest is not None
        self.assertTrue((r_overwrite.backup_manifest.backup_dir / "manifest.json").is_file())

        presence_dir = self.paths.agy_data_dir / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / f"{cid}.lock"
        lock_file.write_text("locked", encoding="utf-8")
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            codex_t4_events = list(codex_t3_events) + [
                UserInputEvent(
                    kind="user_input",
                    anchor=EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=TurnId("turn-4-codex"),
                        timestamp_iso="2026-11-10T04:15:00Z",
                    ),
                    query_text="Turn 4 while AGY is flock locked",
                ),
                AssistantTextEvent(
                    kind="assistant_text",
                    anchor=EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=TurnId("turn-4-codex"),
                        timestamp_iso="2026-11-10T04:15:01Z",
                    ),
                    text="Response 4",
                ),
            ]
            self.bridge._transport.apply_to_codex(
                binding=binding,
                thread_id=tid,
                events=codex_t4_events,
                full_ledger=TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, codex_t4_events),
                lock_snapshot=locks,
                mode=TransportMode.OFFLINE_STORAGE,
            )
            r_busy = self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=cid,
                codex_thread_id=tid,
                direction=SyncDirection.CODEX_TO_AGY,
                mode=TransportMode.OFFLINE_STORAGE,
            )
            self.assertEqual(r_busy.transition_kind, "deferred_busy")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_cli_entrypoints_end_to_end(self) -> None:
        src_tid = self._copy_real_codex_session_into_temp("01a1ae7d-d56f-7650-8336-aa848b9f2a5a")
        cli_bin = self.repo_root / "bin" / "codex-agy-sync"
        base_args = [
            sys.executable,
            str(cli_bin),
            "--agy-data-dir",
            str(self.paths.agy_data_dir),
            "--gemini-config-dir",
            str(self.paths.gemini_config_dir),
            "--codex-home-dir",
            str(self.paths.codex_home_dir),
            "--sync-state-dir",
            str(self.paths.sync_state_dir),
        ]

        pair_res = subprocess.run(
            base_args + ["pair", "--workspace", str(self.workspace_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        pair_json = json.loads(pair_res.stdout)
        self.assertEqual(pair_json["workspace_name"], "agent-fleet")

        sync_res = subprocess.run(
            base_args
            + [
                "sync-workspace",
                "--workspace",
                str(self.workspace_dir),
                "--direction",
                "codex_to_agy",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        sync_json = json.loads(sync_res.stdout)
        self.assertEqual(sync_json["synced_count"], 1)
        synced_cid = sync_json["receipts"][0]["cascade_id"]

        inspect_res = subprocess.run(
            base_args
            + [
                "inspect",
                "--workspace",
                str(self.workspace_dir),
                "--cascade-id",
                synced_cid,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        inspect_json = json.loads(inspect_res.stdout)
        self.assertEqual(inspect_json["counts"]["tool_calls"], 19)
        self.assertEqual(inspect_json["counts"]["thinking_events"], 18)

        status_res = subprocess.run(
            base_args + ["status", "--workspace", str(self.workspace_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        status_json = json.loads(status_res.stdout)
        self.assertEqual(len(status_json["sessions"]), 1)
        self.assertEqual(status_json["sessions"][0]["codex_thread_id"], str(src_tid))


if __name__ == "__main__":
    unittest.main()
