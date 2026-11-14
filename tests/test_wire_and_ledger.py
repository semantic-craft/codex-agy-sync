from __future__ import annotations

import dataclasses
import http.server
import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_agy_sync.codecs import (
    decode_cascade_summary_proto, decode_codex_rollout, decode_summaries_state_proto,
    encode_agy_trajectory, encode_cascade_summary_proto, encode_codex_rollout,
    encode_summaries_state_proto, render_annotation_pbtxt,
)
from codex_agy_sync import validate_session_id
from codex_agy_sync._wire import (
    UnknownWireField, encode_bytes_field, encode_fixed32_field, encode_fixed64_field,
    encode_varint, encode_varint_field, parse_proto_fields, read_varint,
)
from codex_agy_sync.ledger import (
    AgyGuiSafeSource, AgyProjectId, AnnotationPbtxtProjection, AssistantTextEvent, CascadeId,
    CheckpointEvent, CodexProjectId, CodexThreadId, EventOriginAnchor, HostKind,
    ReadFilePayload, RolloutLineNo, RunCommandPayload, StepIndex, SummariesStateProjection,
    SystemEvent, TaskBoundaryEvent, ThinkingEvent, TokenUsageAccounting, ToolCallEvent,
    ToolCallId, ToolResultEvent, TrajectoryLedger, TurnId, UserInputEvent, WorkspaceRootUri,
    parse_workspace_identity, resolve_canonical_title,
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
        anchor_t1 = EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T01:00:00Z", agy_step_index=StepIndex(0))
        events = [
            UserInputEvent(kind="user_input", anchor=anchor_t1, query_text="<USER_REQUEST>\nFirst user prompt line\nSecond line\n</USER_REQUEST>"),
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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t1, timestamp_iso="2026-11-10T01:00:00Z", agy_step_index=StepIndex(0)), query_text="Inspect README and run check"),
            ThinkingEvent(
                kind="thinking",
                anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t1, timestamp_iso="2026-11-10T01:00:01Z", agy_step_index=StepIndex(1)),
                summary_text="Planning to read file and run command.",
                token_usage=TokenUsageAccounting(input_tokens=100, output_tokens=25),
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t1, timestamp_iso="2026-11-10T01:00:01Z", agy_step_index=StepIndex(1)),
                text="Reading README and running check now.",
                token_usage=TokenUsageAccounting(input_tokens=100, output_tokens=25),
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t1, timestamp_iso="2026-11-10T01:00:01Z", agy_step_index=StepIndex(1)),
                call_id=call_1,
                tool_name="read_file",
                tool_summary="Read README",
                payload=ReadFilePayload(kind="read_file", path="README.md"),
            ),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t1, timestamp_iso="2026-11-10T01:00:01Z", agy_step_index=StepIndex(1)),
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
                anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t1, timestamp_iso="2026-11-10T01:00:02Z", agy_step_index=StepIndex(2)),
                call_id=call_1,
                tool_name="read_file",
                output_text="# README content",
                is_error=False,
                payload=ReadFilePayload(kind="read_file", path="README.md", file_num_lines=10),
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t1, timestamp_iso="2026-11-10T01:00:03Z", agy_step_index=StepIndex(3)),
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
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t1, timestamp_iso="2026-11-10T01:00:04Z", agy_step_index=StepIndex(4)), text="Checks succeeded."),
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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t2, timestamp_iso="2026-11-10T01:05:00Z", agy_step_index=StepIndex(5)), query_text="Start a second turn with an unresolved tool call"),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=t2, timestamp_iso="2026-11-10T01:05:01Z", agy_step_index=StepIndex(6)),
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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=t1, timestamp_iso="2026-11-10T02:00:00Z", codex_line_no=RolloutLineNo(1)), query_text="Verify companion projections"),
            ThinkingEvent(kind="thinking", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=t1, timestamp_iso="2026-11-10T02:00:01Z", codex_line_no=RolloutLineNo(2)), summary_text="Checking projection requirements."),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=t1, timestamp_iso="2026-11-10T02:00:02Z", codex_line_no=RolloutLineNo(3)), text="Projections verified."),
            TaskBoundaryEvent(
                kind="task_boundary",
                anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=t1, timestamp_iso="2026-11-10T02:00:03Z", codex_line_no=RolloutLineNo(4)),
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
        pbtxt_text = render_annotation_pbtxt(annot)
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
        self.assertTrue(len(encode_cascade_summary_proto(summary)) > 0)

        repo_root = Path(__file__).resolve().parent.parent
        schema = json.loads((repo_root / "schema" / "antigravity_2_30_schema.json").read_text())
        unknown_fields: list[str] = []
        decoded_summary = decode_protobuf(
            encode_cascade_summary_proto(summary),
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
            encode_summaries_state_proto(hub_state),
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

    def test_resolve_canonical_title_collapses_multiline_whitespace(self) -> None:
        anchor = EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:00:00Z")
        events = [
            UserInputEvent(kind="user_input", anchor=anchor, query_text="First\r\n   line\twith   gaps"),
            CheckpointEvent(
                kind="checkpoint",
                anchor=anchor,
                checkpoint_index=0,
                user_intent="checkpoint",
                session_summary="",
                code_change_summary="",
                conversation_title="Checkpoint\n  Multi\r\nTitle",
            ),
            AssistantTextEvent(kind="assistant_text", anchor=anchor, text="Done."),
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
        self.assertIn('"Checkpoint Multi Title"', render_annotation_pbtxt(proj))

    def test_from_events_step_indexing_and_recompute_flag(self) -> None:
        ws_path, ws_uri = parse_workspace_identity(self.workspace_dir)
        events = [
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:10:00Z", agy_step_index=StepIndex(10)), query_text="Think twice"),
            ThinkingEvent(kind="thinking", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:10:01Z"), summary_text="First thought"),
            ThinkingEvent(kind="thinking", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:10:02Z"), summary_text="Second thought"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:10:03Z", agy_step_index=StepIndex(25)), text="Answer"),
        ]
        recomputed = TrajectoryLedger.from_events(
            ws_path,
            ws_uri,
            events,
            recompute_agy_step_indices=True,
        )
        step_indices = [int(ev.anchor.agy_step_index) for ev in recomputed.events if ev.anchor.agy_step_index is not None]
        self.assertEqual(step_indices, [0, 1, 2, 2])
        encoded_steps = encode_agy_trajectory(recomputed)
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
                        anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:20:00Z"),
                        event_type="world_state",
                        message_text='{"ide":"agy"}',
                    ),
                    UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:20:01Z"), query_text="Run test suite"),
                    ToolCallEvent(
                        kind="tool_call",
                        anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:20:02Z"),
                        call_id=ToolCallId("call-cmd-1"),
                        tool_name="run_command",
                        tool_summary="Run pytest",
                        payload=run_pl,
                    ),
                    ToolResultEvent(
                        kind="tool_result",
                        anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:20:03Z"),
                        call_id=ToolCallId("call-cmd-1"),
                        tool_name="run_command",
                        output_text="1 passed",
                        is_error=False,
                        payload=run_pl,
                    ),
                    AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T03:20:04Z"), text="All green."),
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

        codex_lines = encode_codex_rollout(
            agy_ledger,
            session_id="01970000-0000-7000-8000-000000000099",
        )
        codex_ledger = decode_codex_rollout(ws_path, ws_uri, codex_lines)
        codex_wm = codex_ledger.latest_watermark()
        self.assertIsNotNone(codex_wm)
        assert codex_wm is not None
        self.assertEqual(agy_wm.boundary_digest, codex_wm.boundary_digest)


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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T01:00:00Z"), query_text="Initial session prompt"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T01:00:02Z"), text="Initial reply"),
        ]
        ledger = TrajectoryLedger.from_events(self.ws_path, self.ws_uri, events)
        cid = CascadeId("11111111-2222-3333-4444-555555555599")
        pid = AgyProjectId("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        summary_proj = ledger.project_agy_cascade_summary(cascade_id=cid, project_id=pid)
        orig_bytes = encode_cascade_summary_proto(summary_proj)

        mutated = dataclasses.replace(summary_proj, step_count=99, title="Updated")
        self.assertNotEqual(encode_cascade_summary_proto(mutated), orig_bytes)
        self.assertEqual(mutated.annotations.title, "Updated")

        unknowns: list[str] = []
        decoded_raw = decode_protobuf(
            encode_cascade_summary_proto(mutated),
            "exa.jetski_cortex_pb.CascadeTrajectorySummary",
            self.schema,
            unknowns,
        )
        self.assertEqual(unknowns, [])
        self.assertEqual(decoded_raw["step_count"], 99)
        self.assertEqual(decoded_raw["summary"], "Updated")
        self.assertEqual(decoded_raw["annotations"]["title"], "Updated")

        decoded_proj = decode_cascade_summary_proto(encode_cascade_summary_proto(mutated), fallback_cascade_id=cid)
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
