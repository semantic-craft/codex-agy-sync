from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
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
from codex_agy_sync.cli import main as cli_main
from codex_agy_sync.codecs import (
    ACTIVE_STEP_STATUS_CODES,
    AgyRawStepRow,
    CodexRawRolloutLine,
    CortexStepTypeCode,
    GeminiStepWireTag,
    STEP_TAG_TO_SPEC,
    STEP_WIRE_TABLE,
    StepStatusCode,
    TOOL_CODEC_TABLE,
    TrajectoryWireCodec,
    decode_agy_trajectory,
    decode_cascade_summary_proto,
    decode_codex_rollout,
    decode_codex_rpc_thread_to_ledger,
    decode_summaries_state_proto,
    encode_agy_trajectory,
    encode_codex_rollout,
    encode_summaries_state_proto,
    read_codex_rollout_lines,
    serialize_agy_chat_tool_arguments,
    serialize_tool_payload_arguments_json,
)
from codex_agy_sync import (
    INTERRUPTED_TOOL_OUTPUT,
    INTERRUPTED_TURN_TEXT,
    rescue_interrupted_ledger,
    validate_session_id,
)
from codex_agy_sync._wire import (
    UnknownWireField,
    encode_bytes_field,
    encode_fixed32_field,
    encode_fixed64_field,
    encode_string_field,
    encode_varint,
    encode_varint_field,
    iter_wire_fields,
    parse_proto_fields,
    read_varint,
)
from codex_agy_sync.ledger import (
    _canonical_event_digest_payload,
    AgyGuiSafeSource,
    AgyProjectId,
    AnnotationPbtxtProjection,
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
    MediaAttachment,
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
    SubagentLineage,
    SummariesStateProjection,
    SystemEvent,
    TaskBoundaryEvent,
    ThinkingEvent,
    TokenUsageAccounting,
    ToolCallEvent,
    ToolCallId,
    ToolResultEvent,
    TrajectoryLedger,
    TurnId,
    UserInputEvent,
    WorkspaceRootUri,
    normalize_tool_file_path,
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

    def _assert_title_and_project_bindings_across_stores(
        self,
        cid: CascadeId,
        tid: CodexThreadId,
        expected_title: str,
    ) -> None:
        binding = self.bridge.registry.resolve_readonly_binding(self.workspace_dir)
        self.assertNotIn(str(binding.agy_project_id), FORBIDDEN_AGY_PROJECT_IDS)

        proj_json_path = self.paths.gemini_config_dir / "projects" / f"{binding.agy_project_id}.json"
        self.assertTrue(proj_json_path.is_file())
        proj_doc = json.loads(proj_json_path.read_text(encoding="utf-8"))
        self.assertEqual(proj_doc["id"], str(binding.agy_project_id))
        self.assertIn(
            str(binding.workspace_uri),
            self.bridge.registry._extract_agy_project_folder_uris(proj_doc),
        )

        annot_pbtxt = self.paths.agy_data_dir / "annotations" / f"{cid}.pbtxt"
        self.assertTrue(annot_pbtxt.is_file())
        pbtxt_match = re.search(
            r'title:\s*"((?:[^"\\]|\\.)*)"',
            annot_pbtxt.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(pbtxt_match)
        assert pbtxt_match is not None
        pbtxt_title = (
            pbtxt_match.group(1)
            .replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\\\", "\\")
            .strip()
        )
        self.assertEqual(pbtxt_title, expected_title)

        conv_conn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversations' / f'{cid}.db'}?mode=ro",
            uri=True,
        )
        try:
            tmeta_blob = conv_conn.execute(
                "SELECT data FROM trajectory_metadata_blob WHERE id = 'main'"
            ).fetchone()[0]
        finally:
            conv_conn.close()
        tmeta_unk: list[dict[str, object]] = []
        decoded_tmeta = decode_protobuf(
            bytes(tmeta_blob),
            "exa.cortex_pb.CortexTrajectoryMetadata",
            self.schema,
            tmeta_unk,
        )
        self.assertEqual(tmeta_unk, [])
        self.assertEqual(decoded_tmeta["project_id"], str(binding.agy_project_id))

        sum_conn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversation_summaries.db'}?mode=ro",
            uri=True,
        )
        try:
            sum_row = sum_conn.execute(
                "SELECT title, project_id, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                (str(cid),),
            ).fetchone()
        finally:
            sum_conn.close()
        self.assertIsNotNone(sum_row)
        assert sum_row is not None
        self.assertEqual(sum_row[0], expected_title)
        self.assertEqual(sum_row[1], str(binding.agy_project_id))
        sum_unk: list[dict[str, object]] = []
        decoded_sum = decode_protobuf(
            bytes(sum_row[2]),
            "exa.jetski_cortex_pb.CascadeTrajectorySummary",
            self.schema,
            sum_unk,
        )
        self.assertEqual(sum_unk, [])
        self.assertEqual(decoded_sum["annotations"]["title"], expected_title)
        self.assertEqual(decoded_sum["trajectory_metadata"]["project_id"], str(binding.agy_project_id))

        hub_pb = self.paths.agy_data_dir / "agyhub_summaries_proto.pb"
        self.assertTrue(hub_pb.is_file())
        hub_state = decode_summaries_state_proto(hub_pb.read_bytes())
        self.assertIn(cid, hub_state.summaries)
        hub_unk: list[dict[str, object]] = []
        decoded_hub_sum = decode_protobuf(
            hub_state.summaries[cid].summary_proto_bytes,
            "exa.jetski_cortex_pb.CascadeTrajectorySummary",
            self.schema,
            hub_unk,
        )
        self.assertEqual(hub_unk, [])
        self.assertEqual(decoded_hub_sum["annotations"]["title"], expected_title)
        self.assertEqual(decoded_hub_sum["trajectory_metadata"]["project_id"], str(binding.agy_project_id))

        state_conn = sqlite3.connect(
            f"file:{self.paths.codex_home_dir / 'state_5.sqlite'}?mode=ro",
            uri=True,
        )
        try:
            proj_row = state_conn.execute(
                "SELECT id FROM projects WHERE id = ?",
                (str(binding.codex_project_id),),
            ).fetchone()
            root_row = state_conn.execute(
                "SELECT path FROM project_roots WHERE project_id = ?",
                (str(binding.codex_project_id),),
            ).fetchone()
            thread_row = state_conn.execute(
                "SELECT title, name, project_id, cwd, archived FROM threads WHERE id = ?",
                (str(tid),),
            ).fetchone()
        finally:
            state_conn.close()
        self.assertIsNotNone(proj_row)
        self.assertIsNotNone(root_row)
        assert root_row is not None
        self.assertEqual(root_row[0], str(binding.workspace_path))
        self.assertIsNotNone(thread_row)
        assert thread_row is not None
        self.assertEqual(thread_row[0], expected_title)
        self.assertEqual(thread_row[1], expected_title)
        self.assertEqual(thread_row[2], str(binding.codex_project_id))
        self.assertEqual(thread_row[3], str(binding.workspace_path))
        self.assertEqual(int(thread_row[4]), 0)

        session_idx = self.paths.codex_home_dir / "session_index.jsonl"
        self.assertTrue(session_idx.is_file())
        idx_entries = [
            json.loads(line)
            for line in session_idx.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matched_idx = [entry for entry in idx_entries if entry.get("id") == str(tid)]
        self.assertEqual(len(matched_idx), 1)
        self.assertEqual(matched_idx[0]["thread_name"], expected_title)

        gs_path = self.paths.codex_home_dir / ".codex-global-state.json"
        self.assertTrue(gs_path.is_file())
        gs_doc = json.loads(gs_path.read_text(encoding="utf-8"))
        self.assertIn(str(binding.codex_legacy_project_id), gs_doc["local-projects"])
        self.assertEqual(
            gs_doc["thread-project-assignments"][str(tid)]["projectId"],
            str(binding.codex_legacy_project_id),
        )

    def test_real_session_isolated_roundtrip_agy_to_codex_and_back(self) -> None:
        src_cid = self._copy_real_agy_session_into_temp("eb95dc40-cb5e-4017-b4ff-f41223e1f701")
        orig_agy_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            cascade_id=src_cid,
        )

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
        orig_calls = [e for e in orig_agy_ledger.events if isinstance(e, ToolCallEvent)]
        rt_calls = [e for e in rt_agy_ledger.events if isinstance(e, ToolCallEvent)]
        orig_results = [e for e in orig_agy_ledger.events if isinstance(e, ToolResultEvent)]
        codex_results = [e for e in codex_ledger.events if isinstance(e, ToolResultEvent)]
        rt_results = [e for e in rt_agy_ledger.events if isinstance(e, ToolResultEvent)]

        self.assertEqual(len(rt_calls), 97)
        self.assertEqual(len(rt_results), 97)
        self.assertEqual(
            [
                (str(c.call_id), c.payload.kind if c.payload.kind != "opaque" else c.tool_name, c.payload.kind)
                for c in rt_calls
            ],
            [
                (str(c.call_id), c.payload.kind if c.payload.kind != "opaque" else c.tool_name, c.payload.kind)
                for c in orig_calls
            ],
        )
        for orig_call, rt_call in zip(orig_calls, rt_calls):
            self.assertEqual(
                _canonical_event_digest_payload(rt_call),
                _canonical_event_digest_payload(orig_call),
            )
            self.assertEqual(rt_call.payload, orig_call.payload)
            paired_call, paired_res = rt_agy_ledger.call_and_result(rt_call.call_id)
            self.assertEqual(paired_call.call_id, rt_call.call_id)
            self.assertIsNotNone(paired_res)

        cur_ledger = rt_agy_ledger
        for _ in range(2):
            hop_lines = encode_codex_rollout(
                cur_ledger.events,
                session_id=str(synced_tid),
                cwd=str(self.workspace_dir),
            )
            hop_codex = decode_codex_rollout(cur_ledger.workspace_path, cur_ledger.workspace_uri, hop_lines)
            hop_steps = encode_agy_trajectory(hop_codex.events)
            cur_ledger = decode_agy_trajectory(cur_ledger.workspace_path, cur_ledger.workspace_uri, hop_steps)
            hop_calls = [e for e in cur_ledger.events if isinstance(e, ToolCallEvent)]
            self.assertEqual([c.payload for c in hop_calls], [c.payload for c in orig_calls])
            self.assertEqual(
                [_canonical_event_digest_payload(c) for c in hop_calls],
                [_canonical_event_digest_payload(c) for c in orig_calls],
            )

        self.assertEqual(
            [_canonical_event_digest_payload(r) for r in codex_results],
            [_canonical_event_digest_payload(r) for r in orig_results],
        )
        self.assertEqual(
            [_canonical_event_digest_payload(r) for r in rt_results],
            [_canonical_event_digest_payload(r) for r in orig_results],
        )
        self.assertEqual(codex_ledger.title, orig_agy_ledger.title)
        self.assertEqual(rt_agy_ledger.title, orig_agy_ledger.title)

        transcript_path = (
            self.paths.agy_data_dir
            / "brain"
            / str(roundtrip_cid)
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        self.assertTrue(transcript_path.is_file())
        self._assert_title_and_project_bindings_across_stores(
            roundtrip_cid,
            synced_tid,
            orig_agy_ledger.title,
        )

    def test_real_session_isolated_roundtrip_codex_to_agy_and_back(self) -> None:
        src_tid = self._copy_real_codex_session_into_temp("01a1ae7d-d56f-7650-8336-aa848b9f2a5a")
        orig_codex_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            codex_thread_id=src_tid,
        )

        receipt_1 = self.bridge.sync_session(
            self.workspace_dir,
            codex_thread_id=src_tid,
            direction=SyncDirection.CODEX_TO_AGY,
        )
        self.assertEqual(receipt_1.transition_kind, "initial_import")
        synced_cid = receipt_1.cascade_id

        receipt_1_noop = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=synced_cid,
            codex_thread_id=src_tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(receipt_1_noop.transition_kind, "no_op")
        self.assertEqual(receipt_1_noop.events_applied, 0)

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
        orig_calls = [e for e in orig_codex_ledger.events if isinstance(e, ToolCallEvent)]
        agy_calls = [e for e in agy_ledger.events if isinstance(e, ToolCallEvent)]
        rt_calls = [e for e in rt_codex_ledger.events if isinstance(e, ToolCallEvent)]
        orig_results = [e for e in orig_codex_ledger.events if isinstance(e, ToolResultEvent)]
        agy_results = [e for e in agy_ledger.events if isinstance(e, ToolResultEvent)]
        rt_results = [e for e in rt_codex_ledger.events if isinstance(e, ToolResultEvent)]

        self.assertEqual(
            len([e for e in rt_codex_ledger.events if isinstance(e, ThinkingEvent)]),
            18,
        )
        self.assertEqual(len(rt_calls), 19)
        self.assertEqual(len(rt_results), 19)
        self.assertEqual(
            [(c.tool_name, c.payload) for c in rt_calls],
            [(c.tool_name, c.payload) for c in orig_calls],
        )
        self.assertEqual(
            [_canonical_event_digest_payload(c) for c in agy_calls],
            [_canonical_event_digest_payload(c) for c in orig_calls],
        )
        self.assertEqual(
            [_canonical_event_digest_payload(r) for r in agy_results],
            [_canonical_event_digest_payload(r) for r in orig_results],
        )
        self.assertEqual(
            [(str(r.call_id), r.output_text, r.is_error) for r in rt_results],
            [(str(r.call_id), r.output_text, r.is_error) for r in orig_results],
        )
        self.assertEqual(agy_ledger.title, orig_codex_ledger.title)
        self.assertEqual(rt_codex_ledger.title, orig_codex_ledger.title)
        self._assert_title_and_project_bindings_across_stores(
            synced_cid,
            roundtrip_tid,
            orig_codex_ledger.title,
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

    def test_interrogate_adversarial_regressions(self) -> None:
        # 1. Read-only inspect_session_ledger must not create host project stores on disk
        with self.assertRaises(FileNotFoundError):
            self.bridge.inspect_session_ledger(
                self.workspace_dir,
                cascade_id=CascadeId("00000000-0000-0000-0000-000000000000"),
            )
        self.assertFalse((self.paths.codex_home_dir / "state_5.sqlite").exists())

        # 2. _target_files_for_backup includes SQLite -wal and -shm sidecars
        (self.paths.agy_data_dir / "conversations").mkdir(parents=True, exist_ok=True)
        conv_db = self.paths.agy_data_dir / "conversations" / "wal-c1.db"
        conv_wal = self.paths.agy_data_dir / "conversations" / "wal-c1.db-wal"
        conv_db.write_text("db", encoding="utf-8")
        conv_wal.write_text("wal", encoding="utf-8")
        bfiles = self.bridge._target_files_for_backup(
            HostKind.ANTIGRAVITY, CascadeId("wal-c1"), CodexThreadId("wal-t1")
        )
        self.assertIn(conv_wal, bfiles)

        # 3. _probe_flock_locked detects active flock even on 0o400 read-only lock file
        lf = self.paths.agy_data_dir / "ro.lock"
        lf.write_text("", encoding="utf-8")
        fd = os.open(str(lf), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lf.chmod(0o400)
            self.assertTrue(self.bridge.guard._probe_flock_locked(lf))
        finally:
            lf.chmod(0o600)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        # 4. Codex status='failed' tool output maps to is_error=True and AGY status=7
        failed_line = json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c-err",
                    "status": "failed",
                    "output": "error: failed",
                },
            }
        )
        err_ledger = TrajectoryWireCodec.decode_codex_rollout_to_ledger(
            self.workspace_dir,
            "file:///tmp",
            [CodexRawRolloutLine(RolloutLineNo(1), failed_line)],
        )
        err_res = [e for e in err_ledger.events if isinstance(e, ToolResultEvent)][0]
        self.assertTrue(err_res.is_error)
        err_steps = TrajectoryWireCodec.encode_ledger_to_agy_steps(err_ledger)
        self.assertEqual([s for s in err_steps if s.step_type == 132][0].status, 7)

        # 5. Submessage unknown wire fields stay inside inner submessage and do not leak to outer Step
        ui_sub = encode_string_field(1, "Hello") + encode_string_field(20, "inner_unknown")
        outer_step = (
            encode_varint_field(1, 14)
            + encode_varint_field(4, 3)
            + encode_bytes_field(19, ui_sub)
        )
        unk_ledger = TrajectoryWireCodec.decode_agy_steps_to_ledger(
            self.workspace_dir,
            "file:///tmp",
            [AgyRawStepRow(StepIndex(0), 14, 3, False, b"", outer_step)],
        )
        reencoded = TrajectoryWireCodec.encode_ledger_to_agy_steps(unk_ledger)
        outer_tags = [fn for fn, _, _, _ in iter_wire_fields(reencoded[0].step_payload_blob)]
        self.assertIn(19, outer_tags)
        self.assertNotIn(20, outer_tags)

        # 6. Multi-session agyhub_summaries_proto.pb preservation
        binding = self.bridge.pair_workspace(self.workspace_dir)
        locks = self.bridge.guard.probe_locks(None, None)
        evs = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T01:00:00Z",
                ),
                query_text="Q",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T01:00:01Z",
                ),
                text="A",
            ),
        ]
        s_ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, evs)
        self.bridge._transport.apply_to_agy(
            binding,
            CascadeId("11111111-2222-3333-4444-555555555551"),
            evs,
            s_ledger,
            locks,
            TransportMode.OFFLINE_STORAGE,
        )
        self.bridge._transport.apply_to_agy(
            binding,
            CascadeId("11111111-2222-3333-4444-555555555552"),
            evs,
            s_ledger,
            locks,
            TransportMode.OFFLINE_STORAGE,
        )
        hub_bytes = (self.paths.agy_data_dir / "agyhub_summaries_proto.pb").read_bytes()
        hub_entries = [raw for fn, wt, _, raw in iter_wire_fields(hub_bytes) if fn == 1 and wt == 2]
        self.assertEqual(len(hub_entries), 2)

    def test_normalize_tool_file_path_percent_decoding_and_injection_guard(self) -> None:
        self.assertEqual(
            normalize_tool_file_path("file:///Users/test/My%20Project/a%23b%25c.py"),
            "/Users/test/My Project/a#b%c.py",
        )
        self.assertEqual(
            normalize_tool_file_path("`file:///Users/test/dir%20name/file.py` (lines 1-20)"),
            "/Users/test/dir name/file.py",
        )
        self.assertEqual(
            normalize_tool_file_path("file://localhost/tmp/space%20file.txt"),
            "/tmp/space file.txt",
        )
        self.assertEqual(
            normalize_tool_file_path("file:///C:/Users/test%20user/main.rs"),
            "C:/Users/test user/main.rs",
        )
        self.assertEqual(
            normalize_tool_file_path("/workspace/100%_coverage/50%off.py"),
            "/workspace/100%_coverage/50%off.py",
        )
        self.assertEqual(
            normalize_tool_file_path("src/100%_coverage.py"),
            "src/100%_coverage.py",
        )
        self.assertEqual(
            normalize_tool_file_path("file:///Users/alice/My Projects/app.py"),
            "/Users/alice/My Projects/app.py",
        )
        self.assertEqual(
            normalize_tool_file_path("file:///Users/alice/My%20Projects/app.py%0A"),
            "/Users/alice/My Projects/app.py",
        )
        self.assertEqual(
            normalize_tool_file_path("[app.py](file:///Users/alice/My Projects/app.py)"),
            "/Users/alice/My Projects/app.py",
        )
        injected = normalize_tool_file_path("file:///tmp/evil%0A***%20Delete%20File:%20/etc/passwd")
        self.assertNotIn("\n", injected)
        self.assertNotIn("\r", injected)
        self.assertNotIn("\x00", injected)
        self.assertEqual(injected, "/tmp/evil*** Delete File: /etc/passwd")
        injected_repo = normalize_tool_file_path("file:///repo/a.py%0A***%20Delete%20File:%20b.py")
        self.assertNotIn("\n", injected_repo)
        self.assertNotIn("\r", injected_repo)
        self.assertNotIn("\x00", injected_repo)
        self.assertEqual(injected_repo, "/repo/a.py*** Delete File: b.py")

        read_payload = ReadFilePayload(
            kind="read_file",
            path="file:///workspace/My%20Dir/100%25_test.py",
            start_line=1,
            end_line=10,
        )
        edit_payload = FileEditPayload(
            kind="file_edit",
            path="file:///workspace/My%20Dir/100%25_test.py",
            edit_op="MODIFY",
            unified_diff="@@ -1 +1 @@\n-print('ok')\n+print('done')",
        )
        events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T02:00:00Z",
                ),
                query_text="Edit file with spaces in URI",
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T02:00:01Z",
                ),
                call_id=ToolCallId("call-uri-1"),
                tool_name="read_file",
                tool_summary="Read file",
                payload=read_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T02:00:02Z",
                ),
                call_id=ToolCallId("call-uri-1"),
                tool_name="read_file",
                output_text="print('ok')",
                is_error=False,
                payload=read_payload,
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T02:00:03Z",
                ),
                call_id=ToolCallId("call-uri-2"),
                tool_name="edit_file",
                tool_summary="Edit file",
                payload=edit_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T02:00:04Z",
                ),
                call_id=ToolCallId("call-uri-2"),
                tool_name="edit_file",
                output_text="Edited",
                is_error=False,
                payload=edit_payload,
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T02:00:05Z",
                ),
                text="Updated file.",
            ),
        ]
        ws_path, ws_uri = parse_workspace_identity(self.workspace_dir)
        ledger = TrajectoryLedger.from_events(ws_path, ws_uri, events)
        agy_steps = TrajectoryWireCodec.encode_ledger_to_agy_steps(ledger)
        decoded_agy = TrajectoryWireCodec.decode_agy_steps_to_ledger(ws_path, ws_uri, agy_steps)
        read_calls = [
            ev for ev in decoded_agy.events
            if isinstance(ev, ToolCallEvent) and isinstance(ev.payload, ReadFilePayload)
        ]
        edit_calls = [
            ev for ev in decoded_agy.events
            if isinstance(ev, ToolCallEvent) and isinstance(ev.payload, FileEditPayload)
        ]
        self.assertEqual(read_calls[0].payload.path, "/workspace/My Dir/100%_test.py")
        self.assertEqual(edit_calls[0].payload.path, "/workspace/My Dir/100%_test.py")

        codex_lines = TrajectoryWireCodec.encode_ledger_to_codex_rollout(decoded_agy)
        decoded_codex = TrajectoryWireCodec.decode_codex_rollout_to_ledger(ws_path, ws_uri, codex_lines)
        codex_edit_calls = [
            ev for ev in decoded_codex.events
            if isinstance(ev, ToolCallEvent) and isinstance(ev.payload, FileEditPayload)
        ]
        self.assertEqual(codex_edit_calls[0].payload.path, "/workspace/My Dir/100%_test.py")

    def test_subagent_lineage_discovery_isolation_and_roundtrip(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        locks = self.bridge.guard.probe_locks(None, None)

        parent_cid = CascadeId("aaaaaaaa-1111-2222-3333-444444444444")
        child_cid = CascadeId("bbbbbbbb-1111-2222-3333-444444444444")

        parent_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    agy_step_index=StepIndex(0),
                    timestamp_iso="2026-11-10T03:00:00Z",
                ),
                query_text="Coordinate subagent work",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    agy_step_index=StepIndex(1),
                    timestamp_iso="2026-11-10T03:00:05Z",
                ),
                text="Dispatched subagent.",
            ),
        ]
        child_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    agy_step_index=StepIndex(0),
                    timestamp_iso="2026-11-10T03:00:01Z",
                ),
                query_text="Run subagent task",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    agy_step_index=StepIndex(1),
                    timestamp_iso="2026-11-10T03:00:04Z",
                ),
                text="Subagent completed task.",
            ),
        ]

        parent_ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            parent_events,
            explicit_title_override="Parent Coordinator Session",
        )
        child_ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            child_events,
            explicit_title_override="Child Worker Session",
        )
        child_lineage = SubagentLineage(
            parent_cascade_id=parent_cid,
            root_cascade_id=parent_cid,
            nesting_depth=1,
            agent_type_name="poteto-agent",
            agent_role="Phase 2 Builder",
            spawn_step_index=StepIndex(1),
        )

        self.bridge._transport.apply_to_agy(
            binding=binding,
            cascade_id=parent_cid,
            events=parent_events,
            full_ledger=parent_ledger,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )
        self.bridge._transport.apply_to_agy(
            binding=binding,
            cascade_id=child_cid,
            events=child_events,
            full_ledger=child_ledger,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
            parent_cascade_id=parent_cid,
            lineage=child_lineage,
        )

        sidecar_path = (
            self.paths.agy_data_dir
            / "brain"
            / str(parent_cid)
            / ".system_generated"
            / "subagents"
            / f"{child_cid}.json"
        )
        self.assertTrue(sidecar_path.is_file())
        sidecar_json = json.loads(sidecar_path.read_text(encoding="utf-8"))
        self.assertEqual(sidecar_json["conversationId"], str(child_cid))
        self.assertEqual(sidecar_json["subagentDescriptor"]["typeName"], "poteto-agent")
        self.assertEqual(sidecar_json["subagentDescriptor"]["role"], "Phase 2 Builder")
        self.assertEqual(sidecar_json["spawnStepIndex"], 1)

        top_level_sessions = self.bridge.registry.list_workspace_sessions(binding, include_subagents=False)
        top_cids = {str(s.cascade_id) for s in top_level_sessions if s.cascade_id}
        self.assertIn(str(parent_cid), top_cids)
        self.assertNotIn(str(child_cid), top_cids)

        all_sessions = self.bridge.registry.list_workspace_sessions(binding, include_subagents=True)
        by_cid = {str(s.cascade_id): s for s in all_sessions if s.cascade_id}
        self.assertIn(str(child_cid), by_cid)
        discovered_child = by_cid[str(child_cid)]
        self.assertTrue(discovered_child.lineage.is_subagent)
        self.assertEqual(discovered_child.lineage.parent_cascade_id, parent_cid)
        self.assertEqual(discovered_child.lineage.nesting_depth, 1)
        self.assertEqual(discovered_child.lineage.agent_type_name, "poteto-agent")
        self.assertEqual(discovered_child.lineage.agent_role, "Phase 2 Builder")

        parent_receipt = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=parent_cid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        child_receipt = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=child_cid,
            direction=SyncDirection.AGY_TO_CODEX,
        )

        state_db = self.paths.codex_home_dir / "state_5.sqlite"
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT attached_thread_parent_id, agent_role, agent_nickname FROM threads WHERE id = ?",
                (str(child_receipt.codex_thread_id),),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], str(parent_receipt.codex_thread_id))
        self.assertEqual(row[1], "poteto-agent")
        self.assertEqual(row[2], "Phase 2 Builder")

        reverse_child_cid = CascadeId("cccccccc-1111-2222-3333-444444444444")
        reverse_receipt = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=reverse_child_cid,
            codex_thread_id=child_receipt.codex_thread_id,
            direction=SyncDirection.CODEX_TO_AGY,
            fork_policy=ForkResolutionPolicy.BACKUP_AND_OVERWRITE_TARGET,
        )
        self.assertEqual(reverse_receipt.cascade_id, reverse_child_cid)
        sum_db = self.paths.agy_data_dir / "conversation_summaries.db"
        sconn = sqlite3.connect(f"file:{sum_db}?mode=ro", uri=True)
        try:
            srow = sconn.execute(
                "SELECT agent_name, parent_conversation_id, nesting_depth FROM conversation_summaries WHERE conversation_id = ?",
                (str(reverse_child_cid),),
            ).fetchone()
        finally:
            sconn.close()
        self.assertIsNotNone(srow)
        self.assertEqual(srow[0], "Phase 2 Builder")
        self.assertEqual(srow[1], str(parent_cid))
        self.assertEqual(int(srow[2]), 1)

    def test_validate_session_id_rejects_traversal_and_control_characters(self) -> None:
        valid_id = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(validate_session_id(valid_id, label="cascade_id"), valid_id)
        for bad in (
            "",
            " leading",
            "trailing ",
            "a" * 129,
            "../escape",
            "nested..id",
            "dir/child",
            "dir\\child",
            "bad\nid",
            "bad\x00id",
            "bad\x7fid",
        ):
            with self.assertRaises(ValueError):
                validate_session_id(bad, label="cascade_id")
        with self.assertRaises(ValueError):
            validate_session_id(None, label="cascade_id")  # type: ignore[arg-type]

    def test_subagent_lineage_distinguishes_root_fork_from_spawned_subagent(self) -> None:
        self.assertFalse(SubagentLineage().is_subagent)
        root_fork = SubagentLineage(
            parent_cascade_id=CascadeId("p"),
            parent_codex_thread_id=CodexThreadId("t"),
            nesting_depth=0,
        )
        self.assertFalse(root_fork.is_subagent)
        self.assertFalse(SubagentLineage(agent_type_name="poteto-agent", nesting_depth=0).is_subagent)
        self.assertFalse(SubagentLineage(agent_role="Reviewer", nesting_depth=0).is_subagent)
        self.assertFalse(
            SubagentLineage(
                agent_type_name="poteto-agent",
                agent_role="glaux-repair-worker",
                nesting_depth=0,
            ).is_subagent
        )
        self.assertTrue(SubagentLineage(parent_cascade_id=CascadeId("p"), nesting_depth=1).is_subagent)
        self.assertTrue(SubagentLineage(agent_type_name="poteto-agent", nesting_depth=1).is_subagent)
        self.assertTrue(SubagentLineage(spawn_step_index=StepIndex(3)).is_subagent)

    def test_resolve_canonical_title_collapses_multiline_whitespace(self) -> None:
        anchor = EventOriginAnchor(
            origin_host=HostKind.ANTIGRAVITY,
            turn_id=TurnId("turn-1"),
            timestamp_iso="2026-11-10T03:00:00Z",
        )
        events = [
            UserInputEvent(
                kind="user_input",
                anchor=anchor,
                query_text="First\r\n   line\twith   gaps",
            ),
            CheckpointEvent(
                kind="checkpoint",
                anchor=anchor,
                checkpoint_index=0,
                user_intent="checkpoint",
                session_summary="",
                code_change_summary="",
                conversation_title="Checkpoint\n  Multi\r\nTitle",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=anchor,
                text="Done.",
            ),
        ]
        self.assertEqual(resolve_canonical_title(events), "Checkpoint Multi Title")
        self.assertEqual(
            resolve_canonical_title(events, explicit_title_override="  Explicit\n\tTitle  "),
            "Explicit Title",
        )
        proj = AnnotationPbtxtProjection(
            cascade_id=CascadeId("11111111-2222-3333-4444-555555555555"),
            title="Checkpoint\n  Multi\r\nTitle",
            last_user_view_time_iso="2026-11-10T03:00:05Z",
        )
        self.assertIn('"Checkpoint Multi Title"', proj.render_pbtxt())

    def test_from_events_step_indexing_and_recompute_flag(self) -> None:
        ws_path, ws_uri = parse_workspace_identity(self.workspace_dir)
        events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T03:10:00Z",
                    agy_step_index=StepIndex(10),
                ),
                query_text="Think twice",
            ),
            ThinkingEvent(
                kind="thinking",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T03:10:01Z",
                ),
                summary_text="First thought",
            ),
            ThinkingEvent(
                kind="thinking",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T03:10:02Z",
                ),
                summary_text="Second thought",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T03:10:03Z",
                    agy_step_index=StepIndex(25),
                ),
                text="Answer",
            ),
        ]
        recomputed = TrajectoryLedger.from_events(
            ws_path,
            ws_uri,
            events,
            recompute_agy_step_indices=True,
        )
        step_indices = [int(ev.anchor.agy_step_index) for ev in recomputed.events if ev.anchor.agy_step_index is not None]
        self.assertEqual(step_indices, [0, 1, 2, 2])
        encoded_steps = TrajectoryWireCodec.encode_ledger_to_agy_steps(recomputed)
        self.assertEqual([int(row.idx) for row in encoded_steps], [0, 1, 2])

    def test_cross_host_boundary_digest_matches_and_detects_argument_mutation(self) -> None:
        ws_path, ws_uri = parse_workspace_identity(self.workspace_dir)

        def _build_ledger(cmd_line: str) -> TrajectoryLedger:
            run_pl = RunCommandPayload(
                kind="run_command",
                command_line=cmd_line,
                cwd=str(ws_path),
                exit_code=0,
            )
            return TrajectoryLedger.from_events(
                ws_path,
                ws_uri,
                [
                    SystemEvent(
                        kind="system_message",
                        anchor=EventOriginAnchor(
                            origin_host=HostKind.ANTIGRAVITY,
                            turn_id=TurnId("turn-1"),
                            timestamp_iso="2026-11-10T03:20:00Z",
                        ),
                        event_type="world_state",
                        message_text='{"ide":"agy"}',
                    ),
                    UserInputEvent(
                        kind="user_input",
                        anchor=EventOriginAnchor(
                            origin_host=HostKind.ANTIGRAVITY,
                            turn_id=TurnId("turn-1"),
                            timestamp_iso="2026-11-10T03:20:01Z",
                        ),
                        query_text="Run test suite",
                    ),
                    ToolCallEvent(
                        kind="tool_call",
                        anchor=EventOriginAnchor(
                            origin_host=HostKind.ANTIGRAVITY,
                            turn_id=TurnId("turn-1"),
                            timestamp_iso="2026-11-10T03:20:02Z",
                        ),
                        call_id=ToolCallId("call-cmd-1"),
                        tool_name="run_command",
                        tool_summary="Run pytest",
                        payload=run_pl,
                    ),
                    ToolResultEvent(
                        kind="tool_result",
                        anchor=EventOriginAnchor(
                            origin_host=HostKind.ANTIGRAVITY,
                            turn_id=TurnId("turn-1"),
                            timestamp_iso="2026-11-10T03:20:03Z",
                        ),
                        call_id=ToolCallId("call-cmd-1"),
                        tool_name="run_command",
                        output_text="1 passed",
                        is_error=False,
                        payload=run_pl,
                    ),
                    AssistantTextEvent(
                        kind="assistant_text",
                        anchor=EventOriginAnchor(
                            origin_host=HostKind.ANTIGRAVITY,
                            turn_id=TurnId("turn-1"),
                            timestamp_iso="2026-11-10T03:20:04Z",
                        ),
                        text="All green.",
                    ),
                ],
            )

        agy_ledger = _build_ledger("pytest -q")
        mutated_ledger = _build_ledger("rm -rf /important")
        agy_wm = agy_ledger.latest_watermark()
        mutated_wm = mutated_ledger.latest_watermark()
        self.assertIsNotNone(agy_wm)
        self.assertIsNotNone(mutated_wm)
        assert agy_wm is not None and mutated_wm is not None
        self.assertNotEqual(agy_wm.boundary_digest, mutated_wm.boundary_digest)

        codex_lines = TrajectoryWireCodec.encode_ledger_to_codex_rollout(
            agy_ledger,
            session_id="01970000-0000-7000-8000-000000000099",
        )
        codex_ledger = TrajectoryWireCodec.decode_codex_rollout_to_ledger(ws_path, ws_uri, codex_lines)
        codex_wm = codex_ledger.latest_watermark()
        self.assertIsNotNone(codex_wm)
        assert codex_wm is not None
        self.assertEqual(agy_wm.boundary_digest, codex_wm.boundary_digest)

    def test_delete_op_and_cross_host_tool_wire_schemas_roundtrip(self) -> None:
        ws_path, ws_uri = parse_workspace_identity(self.workspace_dir)
        del_payload = FileEditPayload(
            kind="file_edit",
            path="/workspace/obsolete.py",
            edit_op="DELETE",
            unified_diff="-old\n",
            diff_stats=(0, 1),
            is_artifact=True,
            artifact_summary="Remove obsolete module",
        )
        cmd_payload = RunCommandPayload(
            kind="run_command",
            command_line="pytest -q",
            cwd="/workspace/sub",
            exit_code=0,
            is_daemon=False,
            ran_in_sandbox=True,
            sandbox_policy="workspace-write",
            shell_name="zsh",
            timed_out=False,
        )
        read_payload = ReadFilePayload(
            kind="read_file",
            path="/workspace/app.py",
            start_line=5,
            end_line=15,
        )
        anchor = EventOriginAnchor(
            origin_host=HostKind.ANTIGRAVITY,
            turn_id=TurnId("turn-1"),
            timestamp_iso="2026-11-10T03:30:00Z",
        )
        ledger = TrajectoryLedger.from_events(
            ws_path,
            ws_uri,
            [
                UserInputEvent(kind="user_input", anchor=anchor, query_text="Clean up and test"),
                ToolCallEvent(
                    kind="tool_call",
                    anchor=anchor,
                    call_id=ToolCallId("call-del-1"),
                    tool_name="edit_file",
                    tool_summary="Delete obsolete.py",
                    payload=del_payload,
                ),
                ToolResultEvent(
                    kind="tool_result",
                    anchor=anchor,
                    call_id=ToolCallId("call-del-1"),
                    tool_name="edit_file",
                    output_text="Deleted",
                    is_error=False,
                    payload=del_payload,
                ),
                ToolCallEvent(
                    kind="tool_call",
                    anchor=anchor,
                    call_id=ToolCallId("call-cmd-2"),
                    tool_name="run_command",
                    tool_summary="Run pytest",
                    payload=cmd_payload,
                ),
                ToolResultEvent(
                    kind="tool_result",
                    anchor=anchor,
                    call_id=ToolCallId("call-cmd-2"),
                    tool_name="run_command",
                    output_text="ok",
                    is_error=False,
                    payload=cmd_payload,
                ),
                ToolCallEvent(
                    kind="tool_call",
                    anchor=anchor,
                    call_id=ToolCallId("call-read-3"),
                    tool_name="read_file",
                    tool_summary="Read app.py",
                    payload=read_payload,
                ),
                ToolResultEvent(
                    kind="tool_result",
                    anchor=anchor,
                    call_id=ToolCallId("call-read-3"),
                    tool_name="read_file",
                    output_text="line",
                    is_error=False,
                    payload=read_payload,
                ),
                AssistantTextEvent(kind="assistant_text", anchor=anchor, text="Completed."),
            ],
        )

        agy_steps = TrajectoryWireCodec.encode_ledger_to_agy_steps(ledger)
        chat_arg_dicts: list[dict[str, object]] = []
        for row in agy_steps:
            for fn, wt, _, raw in iter_wire_fields(row.step_payload_blob):
                if fn == 20 and wt == 2:
                    for p_fn, p_wt, _, p_raw in iter_wire_fields(raw):
                        if p_fn == 7 and p_wt == 2:
                            for c_fn, c_wt, _, c_raw in iter_wire_fields(p_raw):
                                if c_fn == 3 and c_wt == 2:
                                    chat_arg_dicts.append(json.loads(c_raw.decode("utf-8")))
        self.assertEqual(len(chat_arg_dicts), 3)
        del_chat_args, cmd_chat_args, read_chat_args = chat_arg_dicts
        self.assertEqual(del_chat_args["Path"], "/workspace/obsolete.py")
        self.assertEqual(del_chat_args["EditOp"], "DELETE")
        self.assertEqual(cmd_chat_args["CommandLine"], "pytest -q")
        self.assertEqual(cmd_chat_args["Cwd"], "/workspace/sub")
        self.assertEqual(read_chat_args["Path"], "/workspace/app.py")
        self.assertEqual(read_chat_args["StartLine"], 5)
        self.assertEqual(read_chat_args["EndLine"], 15)

        agy_decoded = TrajectoryWireCodec.decode_agy_steps_to_ledger(ws_path, ws_uri, agy_steps)
        agy_calls = [ev for ev in agy_decoded.events if isinstance(ev, ToolCallEvent)]
        self.assertEqual(agy_calls[0].payload, del_payload)
        self.assertEqual(agy_calls[1].payload, cmd_payload)

        codex_lines = TrajectoryWireCodec.encode_ledger_to_codex_rollout(
            ledger,
            session_id="01970000-0000-7000-8000-000000000088",
            cwd=str(ws_path),
        )
        meta_obj = json.loads(codex_lines[0].raw_json)
        self.assertEqual(meta_obj["payload"]["id"], "01970000-0000-7000-8000-000000000088")
        self.assertEqual(meta_obj["payload"]["cwd"], str(ws_path))

        parsed_lines = [json.loads(line.raw_json) for line in codex_lines]
        patch_items = [
            obj["payload"]
            for obj in parsed_lines
            if obj.get("type") == "response_item"
            and obj.get("payload", {}).get("type") == "custom_tool_call"
            and obj.get("payload", {}).get("name") == "apply_patch"
        ]
        self.assertEqual(len(patch_items), 1)
        self.assertIn("*** Begin Patch", patch_items[0]["input"])
        self.assertIn("*** Delete File: /workspace/obsolete.py", patch_items[0]["input"])
        self.assertIn("*** End Patch", patch_items[0]["input"])

        exec_items = [
            obj["payload"]
            for obj in parsed_lines
            if obj.get("type") == "response_item"
            and obj.get("payload", {}).get("type") == "function_call"
            and obj.get("payload", {}).get("name") == "exec_command"
        ]
        self.assertEqual(len(exec_items), 1)
        exec_args = json.loads(exec_items[0]["arguments"])
        self.assertEqual(exec_args["cmd"], "pytest -q")
        self.assertEqual(exec_args["workdir"], "/workspace/sub")

        codex_decoded = TrajectoryWireCodec.decode_codex_rollout_to_ledger(ws_path, ws_uri, codex_lines)
        codex_calls = [ev for ev in codex_decoded.events if isinstance(ev, ToolCallEvent)]
        self.assertEqual(codex_calls[0].payload, del_payload)
        self.assertEqual(codex_calls[1].payload, cmd_payload)

    def test_online_rpc_defers_and_status_is_strictly_readonly(self) -> None:
        self.paths.codex_home_dir.mkdir(parents=True, exist_ok=True)
        daemon_json = self.paths.codex_home_dir / "daemon.json"
        daemon_json.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        codex_wal = self.paths.codex_home_dir / "state_5.sqlite-wal"
        codex_wal.write_bytes(b"active-wal-frame")

        orig_cwd = Path.cwd()
        stdout_buf = io.StringIO()
        try:
            os.chdir(self.workspace_dir)
            with contextlib.redirect_stdout(stdout_buf):
                rc = cli_main(
                    [
                        "--gemini-config-dir",
                        str(self.paths.gemini_config_dir),
                        "--agy-data-dir",
                        str(self.paths.agy_data_dir),
                        "--codex-home-dir",
                        str(self.paths.codex_home_dir),
                        "--sync-state-dir",
                        str(self.paths.sync_state_dir),
                        "status",
                        "-w",
                        ".",
                    ]
                )
        finally:
            os.chdir(orig_cwd)

        self.assertEqual(rc, 0)
        status_payload = json.loads(stdout_buf.getvalue())
        self.assertEqual(
            status_payload["workspace"]["workspace_path"],
            str(self.workspace_dir.resolve()),
        )
        self.assertFalse((self.paths.codex_home_dir / "state_5.sqlite").exists())
        self.assertFalse((self.paths.codex_home_dir / ".codex-global-state.json").exists())
        self.assertEqual(list((self.paths.gemini_config_dir / "projects").glob("*.json")), [])

        with self.assertRaises(RuntimeError):
            self.bridge.pair_workspace(self.workspace_dir)

        daemon_json.unlink()
        codex_wal.unlink()

        src_tid = CodexThreadId("01a1ae7d-2222-7000-8000-000000000010")
        t1_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T05:00:00Z",
                ),
                query_text="Online guard test",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T05:00:02Z",
                ),
                text="Ready.",
            ),
        ]
        codex_binding = self.bridge.registry.ensure_codex_binding(self.workspace_dir)
        clean_locks = self.bridge.guard.probe_locks(None, src_tid)
        self.bridge._transport.apply_to_codex(
            codex_binding,
            src_tid,
            t1_events,
            TrajectoryLedger.from_events(codex_binding.workspace_path, codex_binding.workspace_uri, t1_events),
            clean_locks,
            TransportMode.OFFLINE_STORAGE,
        )

        target_cid = CascadeId("bbbb1111-2222-3333-4444-555555555555")
        (self.paths.agy_data_dir / "daemon").mkdir(parents=True, exist_ok=True)
        (self.paths.agy_data_dir / "daemon" / "hub_55000.json").write_text(
            json.dumps({"pid": os.getpid(), "csrfToken": "ephemeral-secret"}),
            encoding="utf-8",
        )
        (self.paths.agy_data_dir / "presence").mkdir(parents=True, exist_ok=True)
        lock_file = self.paths.agy_data_dir / "presence" / f"{target_cid}.lock"
        lock_file.write_text("", encoding="utf-8")
        lock_fd = os.open(str(lock_file), os.O_RDONLY)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deferred = self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=target_cid,
                codex_thread_id=src_tid,
                direction=SyncDirection.CODEX_TO_AGY,
                mode=TransportMode.ONLINE_RPC,
            )
            self.assertEqual(deferred.transition_kind, "deferred_busy")
            self.assertIsNone(deferred.backup_manifest)
            self.assertFalse((self.paths.agy_data_dir / "conversations" / f"{target_cid}.db").exists())
            self.assertEqual(list((self.paths.gemini_config_dir / "projects").glob("*.json")), [])

            held_locks = self.bridge.guard.probe_locks(target_cid, src_tid)
            sample_ledger = TrajectoryLedger.from_events(
                codex_binding.workspace_path, codex_binding.workspace_uri, t1_events
            )
            with self.assertRaises(RuntimeError):
                self.bridge._transport.apply_to_agy(
                    codex_binding,
                    target_cid,
                    t1_events,
                    sample_ledger,
                    held_locks,
                    TransportMode.ONLINE_RPC,
                )
            with self.assertRaises(RuntimeError):
                self.bridge._transport.apply_to_agy(
                    codex_binding,
                    target_cid,
                    t1_events,
                    sample_ledger,
                    held_locks,
                    TransportMode.OFFLINE_STORAGE,
                )
            with self.assertRaises(RuntimeError):
                self.bridge._transport.apply_to_codex(
                    codex_binding,
                    src_tid,
                    t1_events,
                    sample_ledger,
                    clean_locks,
                    TransportMode.ONLINE_RPC,
                )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_top_level_fork_preserves_sidebar_visibility_and_converges_once(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("1111aaaa-2222-3333-4444-555555555555")
        tid = CodexThreadId("01a1ae7d-3333-7000-8000-000000000001")

        t1_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T06:00:00Z",
                ),
                query_text="Shared ancestor turn",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T06:00:01Z",
                ),
                text="Shared ancestor reply",
            ),
        ]
        locks = self.bridge.guard.probe_locks(cid, tid)
        self.bridge._transport.apply_to_agy(
            binding,
            cid,
            t1_events,
            TrajectoryLedger.from_events(
                binding.workspace_path, binding.workspace_uri, t1_events, recompute_agy_step_indices=True
            ),
            locks,
            TransportMode.OFFLINE_STORAGE,
        )
        init_rec = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(init_rec.transition_kind, "initial_import")

        agy_t2 = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-2-agy"),
                    timestamp_iso="2026-11-10T06:01:00Z",
                ),
                query_text="Antigravity branch turn",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-2-agy"),
                    timestamp_iso="2026-11-10T06:01:01Z",
                ),
                text="Antigravity branch reply",
            ),
        ]
        codex_t2 = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2-codex"),
                    timestamp_iso="2026-11-10T06:01:05Z",
                ),
                query_text="Codex branch turn",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2-codex"),
                    timestamp_iso="2026-11-10T06:01:06Z",
                ),
                text="Codex branch reply",
            ),
        ]
        self.bridge._transport.apply_to_agy(
            binding,
            cid,
            agy_t2,
            TrajectoryLedger.from_events(
                binding.workspace_path,
                binding.workspace_uri,
                [*t1_events, *agy_t2],
                recompute_agy_step_indices=True,
            ),
            locks,
            TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )
        self.bridge._transport.apply_to_codex(
            binding,
            tid,
            codex_t2,
            TrajectoryLedger.from_events(
                binding.workspace_path,
                binding.workspace_uri,
                [*t1_events, *codex_t2],
            ),
            locks,
            TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )

        r1 = self.bridge.sync_workspace(
            self.workspace_dir,
            fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION,
        )
        fork_receipts = [r for r in r1 if r.transition_kind == "diverged_forked"]
        self.assertEqual(len(fork_receipts), 1)
        self.assertIsNotNone(fork_receipts[0].backup_manifest)
        self.assertEqual(fork_receipts[0].forked_from_codex_thread_id, tid)

        r2 = self.bridge.sync_workspace(
            self.workspace_dir,
            fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION,
        )
        self.assertEqual(len(r2), 2)
        self.assertTrue(all(r.transition_kind == "no_op" for r in r2))

        active_cursors = list((self.paths.sync_state_dir / "cursors").glob("*.json"))
        superseded_cursors = list((self.paths.sync_state_dir / "cursors" / "superseded").glob("*.json"))
        self.assertEqual(len(active_cursors), 2)
        self.assertGreaterEqual(len(superseded_cursors), 1)

        visible_sessions = self.bridge.registry.list_workspace_sessions(binding, include_subagents=False)
        self.assertEqual(len(visible_sessions), 2)
        for sess in visible_sessions:
            self.assertEqual(sess.lineage.nesting_depth, 0)
            self.assertFalse(sess.lineage.is_subagent)
            self.assertIsNone(sess.parent_cascade_id)
            self.assertIsNone(sess.parent_codex_thread_id)
        self.assertEqual(
            list((self.paths.agy_data_dir / "brain").glob("*/.system_generated/subagents/*.json")),
            [],
        )

        ws2 = self.root / "agent-fleet-c2a-fork"
        ws2.mkdir(parents=True, exist_ok=True)
        b2 = self.bridge.pair_workspace(ws2)
        cid2 = CascadeId("2222bbbb-2222-3333-4444-555555555555")
        tid2 = CodexThreadId("01a1ae7d-4444-7000-8000-000000000002")
        locks2 = self.bridge.guard.probe_locks(cid2, tid2)
        self.bridge._transport.apply_to_agy(
            b2,
            cid2,
            t1_events,
            TrajectoryLedger.from_events(
                b2.workspace_path, b2.workspace_uri, t1_events, recompute_agy_step_indices=True
            ),
            locks2,
            TransportMode.OFFLINE_STORAGE,
        )
        self.bridge.sync_session(
            ws2,
            cascade_id=cid2,
            codex_thread_id=tid2,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.bridge._transport.apply_to_agy(
            b2,
            cid2,
            agy_t2,
            TrajectoryLedger.from_events(
                b2.workspace_path, b2.workspace_uri, [*t1_events, *agy_t2], recompute_agy_step_indices=True
            ),
            locks2,
            TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )
        self.bridge._transport.apply_to_codex(
            b2,
            tid2,
            codex_t2,
            TrajectoryLedger.from_events(b2.workspace_path, b2.workspace_uri, [*t1_events, *codex_t2]),
            locks2,
            TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )
        c2a_fork = self.bridge.sync_session(
            ws2,
            cascade_id=cid2,
            codex_thread_id=tid2,
            direction=SyncDirection.CODEX_TO_AGY,
            fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION,
        )
        self.assertEqual(c2a_fork.transition_kind, "diverged_forked")
        self.assertIsNotNone(c2a_fork.backup_manifest)
        self.assertEqual(c2a_fork.forked_from_cascade_id, cid2)
        self.assertNotEqual(c2a_fork.cascade_id, cid2)

        fork_db = self.paths.agy_data_dir / "conversations" / f"{c2a_fork.cascade_id}.db"
        fconn = sqlite3.connect(f"file:{fork_db}?mode=ro", uri=True)
        try:
            pref_rows = fconn.execute("SELECT idx, data FROM parent_references ORDER BY idx ASC").fetchall()
        finally:
            fconn.close()
        self.assertEqual(pref_rows, [(0, encode_string_field(1, str(cid2)))])

        sconn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversation_summaries.db'}?mode=ro",
            uri=True,
        )
        try:
            srow = sconn.execute(
                "SELECT parent_conversation_id, nesting_depth FROM conversation_summaries WHERE conversation_id = ?",
                (str(c2a_fork.cascade_id),),
            ).fetchone()
        finally:
            sconn.close()
        self.assertEqual(srow, ("", 0))
        self.assertEqual(
            list((self.paths.agy_data_dir / "brain").glob("*/.system_generated/subagents/*.json")),
            [],
        )

        self.bridge.sync_workspace(ws2, fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION)
        ws2_noop = self.bridge.sync_workspace(ws2, fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION)
        self.assertEqual(len(ws2_noop), 2)
        self.assertTrue(all(r.transition_kind == "no_op" for r in ws2_noop))
        self.assertEqual(
            len(self.bridge.registry.list_workspace_sessions(b2, include_subagents=False)),
            2,
        )

    def test_session_id_traversal_rejected_and_backup_command_shell_quoted(self) -> None:
        evil_id = "../../../escaped_target"
        with self.assertRaises(ValueError):
            self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=CascadeId(evil_id),
                direction=SyncDirection.CODEX_TO_AGY,
            )
        with self.assertRaises(ValueError):
            self.bridge.inspect_session_ledger(
                self.workspace_dir,
                cascade_id=CascadeId(evil_id),
            )
        with self.assertRaises(ValueError):
            self.bridge.guard.probe_locks(CascadeId(evil_id), None)
        with self.assertRaises(ValueError):
            self.bridge.guard.create_offline_backup(HostKind.ANTIGRAVITY, evil_id, ())
        self.assertFalse((self.root / "escaped_target.db").exists())

        conv_dir = self.paths.agy_data_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        (conv_dir / "bad..session.db").write_bytes(b"not-a-valid-id")
        bad_sc_dir = self.paths.agy_data_dir / "brain" / "bad..parent" / ".system_generated" / "subagents"
        bad_sc_dir.mkdir(parents=True, exist_ok=True)
        (bad_sc_dir / "child.json").write_text(
            json.dumps({"conversationId": "../evil", "workspaceUris": [self.workspace_dir.as_uri()]}),
            encoding="utf-8",
        )
        ro_binding = self.bridge.registry.resolve_readonly_binding(self.workspace_dir)
        self.assertEqual(
            self.bridge.registry.list_workspace_sessions(ro_binding, include_subagents=True),
            (),
        )

        tricky_dir = self.root / "dir with space; echo pwned"
        tricky_dir.mkdir(parents=True, exist_ok=True)
        tricky_db = tricky_dir / "state.sqlite"
        tricky_wal = tricky_dir / "state.sqlite-wal"
        tricky_db.write_text("db-bytes", encoding="utf-8")
        tricky_wal.write_text("wal-bytes", encoding="utf-8")

        manifest = self.bridge.guard.create_offline_backup(
            HostKind.CODEX,
            "01a1ae7d-5555-7000-8000-000000000077",
            (tricky_db,),
        )
        self.assertIn(shlex.quote(str(tricky_db)), manifest.restoration_command)
        self.assertIn(shlex.quote(str(tricky_wal)), manifest.restoration_command)
        cmd_parts = manifest.restoration_command.split(" && ")
        self.assertGreaterEqual(len(cmd_parts), 2)
        expected_targets = {
            str(tricky_db),
            str(tricky_wal),
            str(tricky_dir / "state.sqlite-shm"),
        }
        for part in cmd_parts:
            tokens = shlex.split(part)
            self.assertIn(tokens[0], ("cp", "rm"))
            self.assertIn(tokens[-1], expected_targets)

    def test_incremental_codex_to_agy_step_indices_match_across_stores(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("3333cccc-2222-3333-4444-555555555555")
        tid = CodexThreadId("01a1ae7d-6666-7000-8000-000000000003")
        locks = self.bridge.guard.probe_locks(cid, tid)

        t1_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T07:00:00Z",
                ),
                query_text="Initial question",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T07:00:01Z",
                ),
                text="Initial answer",
            ),
        ]
        self.bridge._transport.apply_to_agy(
            binding,
            cid,
            t1_events,
            TrajectoryLedger.from_events(
                binding.workspace_path, binding.workspace_uri, t1_events, recompute_agy_step_indices=True
            ),
            locks,
            TransportMode.OFFLINE_STORAGE,
        )
        self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AGY_TO_CODEX,
        )

        read_payload = ReadFilePayload(kind="read_file", path="/workspace/README.md", start_line=1, end_line=5)
        t2_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T07:01:00Z",
                ),
                query_text="Followup in Codex",
            ),
            ThinkingEvent(
                kind="thinking",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T07:01:01Z",
                ),
                summary_text="Checking README",
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T07:01:02Z",
                ),
                call_id=ToolCallId("call-step-align-1"),
                tool_name="read_file",
                tool_summary="Read README",
                payload=read_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T07:01:03Z",
                ),
                call_id=ToolCallId("call-step-align-1"),
                tool_name="read_file",
                output_text="# Title",
                is_error=False,
                payload=read_payload,
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T07:01:04Z",
                ),
                text="Checked README.",
            ),
        ]
        self.bridge._transport.apply_to_codex(
            binding,
            tid,
            t2_events,
            TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, [*t1_events, *t2_events]),
            locks,
            TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )

        inc_rec = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.CODEX_TO_AGY,
        )
        self.assertEqual(inc_rec.transition_kind, "incremental_append")

        conv_conn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversations' / f'{cid}.db'}?mode=ro",
            uri=True,
        )
        try:
            step_rows = conv_conn.execute("SELECT idx, step_type FROM steps ORDER BY idx ASC").fetchall()
        finally:
            conv_conn.close()

        tx_path = self.paths.agy_data_dir / "brain" / str(cid) / ".system_generated" / "logs" / "transcript.jsonl"
        tx_indices = [
            int(json.loads(line)["step_index"])
            for line in tx_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        sum_conn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversation_summaries.db'}?mode=ro",
            uri=True,
        )
        try:
            sum_row = sum_conn.execute(
                "SELECT step_count, last_user_input_step_index FROM conversation_summaries WHERE conversation_id = ?",
                (str(cid),),
            ).fetchone()
        finally:
            sum_conn.close()

        self.assertIsNotNone(sum_row)
        assert sum_row is not None
        self.assertEqual([int(r[0]) for r in step_rows], tx_indices)
        self.assertEqual(int(sum_row[0]), len(step_rows))
        expected_last_user_idx = max(int(idx) for idx, st in step_rows if int(st) == 14)
        self.assertEqual(int(sum_row[1]), expected_last_user_idx)

    def test_pre_cursor_crash_retry_converges_to_noop(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("4444dddd-2222-3333-4444-555555555555")
        locks = self.bridge.guard.probe_locks(cid, None)

        read_payload = ReadFilePayload(kind="read_file", path="/workspace/a.py", start_line=1, end_line=10)
        t1_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T08:00:00Z",
                ),
                query_text="Crash recovery check",
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T08:00:01Z",
                ),
                call_id=ToolCallId("call-crash-1"),
                tool_name="read_file",
                tool_summary="Read a.py",
                payload=read_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T08:00:02Z",
                ),
                call_id=ToolCallId("call-crash-1"),
                tool_name="read_file",
                output_text="x = 1",
                is_error=False,
                payload=read_payload,
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T08:00:03Z",
                ),
                text="Read complete.",
            ),
        ]
        self.bridge._transport.apply_to_agy(
            binding,
            cid,
            t1_events,
            TrajectoryLedger.from_events(
                binding.workspace_path, binding.workspace_uri, t1_events, recompute_agy_step_indices=True
            ),
            locks,
            TransportMode.OFFLINE_STORAGE,
        )

        r_init = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        tid = r_init.codex_thread_id
        cpath = self.bridge._cursor_path(cid, tid)
        self.assertTrue(cpath.is_file())
        saved_t1_cursor = cpath.read_text(encoding="utf-8")

        cpath.unlink()
        retry_single = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(retry_single.transition_kind, "no_op")
        self.assertEqual(retry_single.events_applied, 0)
        self.assertTrue(cpath.is_file())

        cpath.unlink()
        retry_pair = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(retry_pair.transition_kind, "no_op")
        self.assertEqual(retry_pair.events_applied, 0)
        self.assertTrue(cpath.is_file())

        t2_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T08:01:00Z",
                ),
                query_text="Second turn before cursor crash",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T08:01:01Z",
                ),
                text="Second turn done.",
            ),
        ]
        self.bridge._transport.apply_to_agy(
            binding,
            cid,
            t2_events,
            TrajectoryLedger.from_events(
                binding.workspace_path,
                binding.workspace_uri,
                [*t1_events, *t2_events],
                recompute_agy_step_indices=True,
            ),
            locks,
            TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )
        r_inc = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(r_inc.transition_kind, "incremental_append")

        cpath.write_text(saved_t1_cursor, encoding="utf-8")
        retry_lagged = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(retry_lagged.transition_kind, "no_op")
        self.assertEqual(retry_lagged.events_applied, 0)
        restored_cursor = json.loads(cpath.read_text(encoding="utf-8"))
        self.assertEqual(restored_cursor["last_synced_turn_id"], "turn-2")

    def test_pragma_user_version_preserved_and_stale_codex_socket_ignored(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("5555eeee-2222-3333-4444-555555555555")
        locks = self.bridge.guard.probe_locks(cid, None)

        t1_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T09:00:00Z",
                ),
                query_text="Schema user_version check",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T09:00:01Z",
                ),
                text="Created.",
            ),
        ]
        self.bridge._transport.apply_to_agy(
            binding,
            cid,
            t1_events,
            TrajectoryLedger.from_events(
                binding.workspace_path, binding.workspace_uri, t1_events, recompute_agy_step_indices=True
            ),
            locks,
            TransportMode.OFFLINE_STORAGE,
        )
        conv_db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        conn = sqlite3.connect(str(conv_db))
        try:
            initial_uv = int(conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(initial_uv, 1)
            conn.execute("PRAGMA user_version = 7")
            conn.commit()
        finally:
            conn.close()

        t2_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T09:01:00Z",
                ),
                query_text="Preserve user_version",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T09:01:01Z",
                ),
                text="Preserved.",
            ),
        ]
        self.bridge._transport.apply_to_agy(
            binding,
            cid,
            t2_events,
            TrajectoryLedger.from_events(
                binding.workspace_path,
                binding.workspace_uri,
                [*t1_events, *t2_events],
                recompute_agy_step_indices=True,
            ),
            locks,
            TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )
        ro_conn = sqlite3.connect(f"file:{conv_db}?mode=ro", uri=True)
        try:
            preserved_uv = int(ro_conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            ro_conn.close()
        self.assertEqual(preserved_uv, 7)

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="cas-") as short_tmp:
            sroot = Path(short_tmp)
            spaths = HostEnvironmentPaths(
                gemini_config_dir=sroot / "g",
                agy_data_dir=sroot / "a",
                codex_home_dir=sroot / "c",
                sync_state_dir=sroot / "s",
            )
            spaths.codex_home_dir.mkdir(parents=True, exist_ok=True)
            sguard = SyncBridge(paths=spaths).guard
            sock_path = spaths.codex_home_dir / "app-server.sock"

            sock_path.write_text("not-a-socket", encoding="utf-8")
            self.assertIsNone(sguard._discover_codex_daemon())
            sock_path.unlink()

            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                srv.bind(str(sock_path))
                srv.listen(1)
                live_ep = sguard._discover_codex_daemon()
                self.assertIsNotNone(live_ep)
            finally:
                srv.close()

            self.assertTrue(sock_path.exists())
            self.assertIsNone(sguard._discover_codex_daemon())


class TestSharedWireAndPureSummaryProjections(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name) / "unit1-workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.ws_path, self.ws_uri = parse_workspace_identity(self.workspace_dir)
        repo_root = Path(__file__).resolve().parent.parent
        self.schema = json.loads((repo_root / "schema" / "antigravity_2_30_schema.json").read_text())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_wire_varint_and_parse_proto_fields_reject_truncation_and_overlong_varints(self) -> None:
        max_u64 = (1 << 64) - 1
        encoded_max = encode_varint(max_u64)
        self.assertEqual(len(encoded_max), 10)
        self.assertEqual(read_varint(encoded_max, 0), (max_u64, 10))

        for bad_val in (-1, 1 << 64):
            with self.assertRaises(ValueError):
                encode_varint(bad_val)

        for truncated in (b"", b"\x80", b"\xff\xff\xff\x80"):
            with self.assertRaises(ValueError):
                read_varint(truncated, 0)

        for overlong in (b"\x80" * 9 + b"\x02", b"\x80" * 10 + b"\x01", b"\x80" * 12 + b"\x00"):
            with self.assertRaises(ValueError):
                read_varint(overlong, 0)

        valid_msg = (
            encode_varint_field(1, 42)
            + encode_fixed64_field(2, b"12345678")
            + encode_bytes_field(3, b"abc")
            + encode_fixed32_field(4, b"wxyz")
        )
        fmap, unknowns = parse_proto_fields(valid_msg, known_tags={1, 3})
        self.assertEqual(fmap[1], [(0, 42)])
        self.assertEqual(fmap[3], [(2, b"abc")])
        self.assertEqual([u.field_tag for u in unknowns], [2, 4])

        malformed_payloads = (
            b"\x00\x01",
            b"\x09short",
            b"\x0dsh",
            b"\x12\x05abc",
            encode_varint((1 << 3) | 3),
            encode_varint((1 << 3) | 4),
            encode_varint((1 << 3) | 6),
            encode_varint((1 << 3) | 7),
            b"\x08" + (b"\x80" * 10) + b"\x01",
        )
        for bad_buf in malformed_payloads:
            with self.assertRaises(ValueError):
                parse_proto_fields(bad_buf)

    def test_cascade_summary_projection_replace_updates_summary_proto_bytes(self) -> None:
        events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T01:00:00Z",
                ),
                query_text="Initial session prompt",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T01:00:02Z",
                ),
                text="Initial reply",
            ),
        ]
        ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, events)
        cid = CascadeId("11111111-2222-3333-4444-555555555599")
        pid = AgyProjectId("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        summary_proj = ledger.project_agy_cascade_summary(cascade_id=cid, project_id=pid)
        orig_bytes = summary_proj.summary_proto_bytes

        mutated = dataclasses.replace(summary_proj, step_count=99, title="Updated")
        self.assertNotEqual(mutated.summary_proto_bytes, orig_bytes)
        self.assertEqual(mutated.annotations.title, "Updated")

        unknowns: list[str] = []
        decoded_raw = decode_protobuf(
            mutated.summary_proto_bytes,
            "exa.jetski_cortex_pb.CascadeTrajectorySummary",
            self.schema,
            unknowns,
        )
        self.assertEqual(unknowns, [])
        self.assertEqual(decoded_raw["step_count"], 99)
        self.assertEqual(decoded_raw["summary"], "Updated")
        self.assertEqual(decoded_raw["annotations"]["title"], "Updated")

        decoded_proj = decode_cascade_summary_proto(mutated.summary_proto_bytes, fallback_cascade_id=cid)
        self.assertEqual(decoded_proj.step_count, 99)
        self.assertEqual(decoded_proj.title, "Updated")

    def test_decode_summaries_state_proto_decodes_real_metadata_and_unknown_fields(self) -> None:
        cid = CascadeId("99999999-2222-3333-4444-555555555555")
        pid = AgyProjectId("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
        ws_uris = (
            WorkspaceRootUri("file:///workspace/alpha"),
            WorkspaceRootUri("file:///workspace/beta"),
        )
        annot = AnnotationPbtxtProjection(
            cascade_id=cid,
            title="Real Decoded Title",
            last_user_view_time_iso="2026-11-10T06:30:00Z",
            unknown_wire_fields=(UnknownWireField(field_tag=12, wire_type=0, raw_bytes=b"\x01"),),
        )
        summary_unknowns = (
            UnknownWireField(field_tag=99, wire_type=0, raw_bytes=b"\x2a"),
            UnknownWireField(field_tag=105, wire_type=2, raw_bytes=b"future-summary-ext"),
        )
        meta_unknowns = (UnknownWireField(field_tag=91, wire_type=2, raw_bytes=b"future-meta-ext"),)
        summary_proj = dataclasses.replace(
            TrajectoryLedger.from_events(self.ws_path, ws_uris[0], ()).project_agy_cascade_summary(
                cascade_id=cid,
                project_id=pid,
                viewed_at_iso="2026-11-10T06:30:00Z",
                source=AgyGuiSafeSource.JETBOX,
            ),
            workspace_uris=ws_uris,
            title="Real Decoded Title",
            preview="Real Decoded Title",
            step_count=17,
            status=2,
            created_at_iso="2026-11-10T06:00:00Z",
            updated_at_iso="2026-11-10T06:30:00Z",
            annotations=annot,
            trajectory_metadata_unknown_fields=meta_unknowns,
            unknown_wire_fields=summary_unknowns,
        )
        hub_unknowns = (UnknownWireField(field_tag=77, wire_type=2, raw_bytes=b"hub-future-ext"),)
        state = SummariesStateProjection(
            summaries={cid: summary_proj},
            file_mod_times={"/sandbox/conversations/9999.db": 1794300000},
            unknown_wire_fields=hub_unknowns,
        )
        raw_bytes = encode_summaries_state_proto(state)

        decoded_state = decode_summaries_state_proto(raw_bytes)
        self.assertEqual(decoded_state.file_mod_times, {"/sandbox/conversations/9999.db": 1794300000})
        self.assertEqual(decoded_state.unknown_wire_fields, hub_unknowns)
        self.assertIn(cid, decoded_state.summaries)

        got = decoded_state.summaries[cid]
        self.assertEqual(got.title, "Real Decoded Title")
        self.assertEqual(got.step_count, 17)
        self.assertEqual(got.status, 2)
        self.assertEqual(got.project_id, pid)
        self.assertEqual(got.workspace_uris, ws_uris)
        self.assertEqual(got.created_at_iso, "2026-11-10T06:00:00Z")
        self.assertEqual(got.updated_at_iso, "2026-11-10T06:30:00Z")
        self.assertEqual(got.source, AgyGuiSafeSource.JETBOX)
        self.assertEqual(got.unknown_wire_fields, summary_unknowns)
        self.assertEqual(got.trajectory_metadata_unknown_fields, meta_unknowns)
        self.assertEqual(got.annotations.unknown_wire_fields, annot.unknown_wire_fields)
        self.assertEqual(encode_summaries_state_proto(decoded_state), raw_bytes)


class TestUnit2CodecsAndFidelity(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name) / "unit2-workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.ws_path, self.ws_uri = parse_workspace_identity(self.workspace_dir)
        self.schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schema" / "antigravity_2_30_schema.json").read_text(
                encoding="utf-8"
            )
        )

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


class TestUnit3LineageAndInterruptedRescue(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace_dir = self.root / "unit3-workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.ws_path, self.ws_uri = parse_workspace_identity(self.workspace_dir)
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.root / "gemini-config",
            agy_data_dir=self.root / "antigravity",
            codex_home_dir=self.root / "codex",
            sync_state_dir=self.root / "sync-state",
        )
        self.bridge = SyncBridge(paths=self.paths)
        self.binding = self.bridge.pair_workspace(self.workspace_dir)
        self.locks = self.bridge.guard.probe_locks(None, None)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run_cli_json(self, *args: str) -> dict[str, object]:
        buf = io.StringIO()
        base = [
            "--gemini-config-dir",
            str(self.paths.gemini_config_dir),
            "--agy-data-dir",
            str(self.paths.agy_data_dir),
            "--codex-home-dir",
            str(self.paths.codex_home_dir),
            "--sync-state-dir",
            str(self.paths.sync_state_dir),
            *args,
        ]
        with contextlib.redirect_stdout(buf):
            rc = cli_main(base)
        self.assertEqual(rc, 0, msg=buf.getvalue())
        return json.loads(buf.getvalue())

    def test_3a_orthogonal_lineage_agy_root_agent_name_and_codex_source_shapes(self) -> None:
        root_cid = CascadeId("11111111-1111-4111-8111-111111111111")
        sub_cid = CascadeId("22222222-2222-4222-8222-222222222222")
        events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T01:00:00Z",
                ),
                query_text="Root session with agent_name",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-12T01:00:01Z",
                ),
                text="Completed root response",
            ),
        ]
        root_lineage = SubagentLineage(
            agent_type_name="poteto-agent",
            agent_role="glaux-repair-worker",
            nesting_depth=0,
        )
        self.assertFalse(root_lineage.is_subagent)
        base_ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, events)
        self.bridge._transport.apply_to_agy(
            binding=self.binding,
            cascade_id=root_cid,
            events=events,
            full_ledger=base_ledger,
            lock_snapshot=self.locks,
            mode=TransportMode.OFFLINE_STORAGE,
            lineage=root_lineage,
        )
        sum_conn = sqlite3.connect(str(self.paths.agy_data_dir / "conversation_summaries.db"))
        try:
            sum_conn.execute(
                "UPDATE conversation_summaries SET agent_name = ?, nesting_depth = 0, parent_conversation_id = '' WHERE conversation_id = ?",
                ("glaux-repair-worker", str(root_cid)),
            )
            sum_conn.commit()
        finally:
            sum_conn.close()

        sub_lineage = SubagentLineage(
            parent_cascade_id=root_cid,
            agent_type_name="poteto-agent",
            agent_role="ChildWorker",
            nesting_depth=1,
        )
        self.assertTrue(sub_lineage.is_subagent)
        self.bridge._transport.apply_to_agy(
            binding=self.binding,
            cascade_id=sub_cid,
            events=events,
            full_ledger=base_ledger,
            lock_snapshot=self.locks,
            mode=TransportMode.OFFLINE_STORAGE,
            lineage=sub_lineage,
        )

        tid_spawn = CodexThreadId("33333333-3333-4333-8333-333333333333")
        tid_review = CodexThreadId("44444444-4444-4444-8444-444444444444")
        tid_guardian = CodexThreadId("55555555-5555-4555-8555-555555555555")
        tid_root_fork = CodexThreadId("66666666-6666-4666-8666-666666666666")
        for tid in (tid_spawn, tid_review, tid_guardian, tid_root_fork):
            self.bridge._transport.apply_to_codex(
                binding=self.binding,
                thread_id=tid,
                events=events,
                full_ledger=base_ledger,
                lock_snapshot=self.locks,
                mode=TransportMode.OFFLINE_STORAGE,
            )
        state_conn = sqlite3.connect(str(self.paths.codex_home_dir / "state_5.sqlite"))
        try:
            state_conn.execute(
                "UPDATE threads SET source = ?, attached_thread_parent_id = '', agent_role = NULL, agent_nickname = NULL WHERE id = ?",
                (
                    json.dumps(
                        {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": str(tid_root_fork),
                                    "depth": 1,
                                    "agent_nickname": "SpawnNick",
                                    "agent_role": "poteto-agent",
                                }
                            }
                        }
                    ),
                    str(tid_spawn),
                ),
            )
            state_conn.execute(
                "UPDATE threads SET source = ?, attached_thread_parent_id = '' WHERE id = ?",
                (json.dumps({"subagent": "review"}), str(tid_review)),
            )
            state_conn.execute(
                "UPDATE threads SET source = ?, attached_thread_parent_id = '' WHERE id = ?",
                (json.dumps({"subagent": {"other": "guardian"}}), str(tid_guardian)),
            )
            state_conn.execute(
                "UPDATE threads SET source = 'vscode', attached_thread_parent_id = '99999999-9999-4999-8999-999999999999', agent_role = NULL, agent_nickname = NULL WHERE id = ?",
                (str(tid_root_fork),),
            )
            state_conn.commit()
        finally:
            state_conn.close()

        roots_only = self.bridge.registry.list_workspace_sessions(
            self.binding, include_subagents=False
        )
        root_agy_ids = {str(s.cascade_id) for s in roots_only if s.cascade_id is not None}
        root_codex_ids = {str(s.codex_thread_id) for s in roots_only if s.codex_thread_id is not None}
        self.assertIn(str(root_cid), root_agy_ids)
        self.assertNotIn(str(sub_cid), root_agy_ids)
        self.assertIn(str(tid_root_fork), root_codex_ids)
        self.assertTrue(
            root_codex_ids.isdisjoint({str(tid_spawn), str(tid_review), str(tid_guardian)})
        )

        with_subs = self.bridge.registry.list_workspace_sessions(
            self.binding, include_subagents=True
        )
        by_cid = {str(s.cascade_id): s for s in with_subs if s.cascade_id is not None}
        by_tid = {str(s.codex_thread_id): s for s in with_subs if s.codex_thread_id is not None}
        self.assertFalse(by_cid[str(root_cid)].lineage.is_subagent)
        self.assertTrue(by_cid[str(sub_cid)].lineage.is_subagent)
        self.assertFalse(by_tid[str(tid_root_fork)].lineage.is_subagent)
        for sub_tid in (tid_spawn, tid_review, tid_guardian):
            self.assertTrue(by_tid[str(sub_tid)].lineage.is_subagent)
            resolved = self.bridge._resolve_session_lineage(self.binding, None, sub_tid)
            self.assertTrue(resolved.is_subagent)

        r_root = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=root_cid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(r_root.transition_kind, "initial_import")
        r_sub = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=sub_cid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(r_sub.transition_kind, "initial_import")
        ro_conn = sqlite3.connect(
            f"file:{self.paths.codex_home_dir / 'state_5.sqlite'}?mode=ro", uri=True
        )
        try:
            root_src = ro_conn.execute(
                "SELECT source FROM threads WHERE id = ?", (str(r_root.codex_thread_id),)
            ).fetchone()[0]
            sub_src = ro_conn.execute(
                "SELECT source FROM threads WHERE id = ?", (str(r_sub.codex_thread_id),)
            ).fetchone()[0]
        finally:
            ro_conn.close()
        self.assertEqual(root_src, "desktop")
        parsed_sub_src = json.loads(sub_src)
        self.assertIn("subagent", parsed_sub_src)
        self.assertEqual(parsed_sub_src["subagent"]["thread_spawn"]["depth"], 1)

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


if __name__ == "__main__":
    unittest.main()
