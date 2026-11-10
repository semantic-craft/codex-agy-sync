from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_agy_sync.ledger import (
    AgyGuiSafeSource,
    AgyProjectId,
    AssistantTextEvent,
    CascadeId,
    CheckpointEvent,
    CodexProjectId,
    CodexThreadId,
    EventOriginAnchor,
    HostKind,
    ReadFilePayload,
    RolloutLineNo,
    RunCommandPayload,
    StepIndex,
    TaskBoundaryEvent,
    ThinkingEvent,
    TokenUsageAccounting,
    ToolCallEvent,
    ToolCallId,
    ToolResultEvent,
    TrajectoryLedger,
    TurnId,
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


if __name__ == "__main__":
    unittest.main()
