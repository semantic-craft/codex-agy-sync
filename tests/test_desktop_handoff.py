from __future__ import annotations

import contextlib
import dataclasses
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from codex_agy_sync._wire import iter_wire_fields
from codex_agy_sync.bridge import HostEnvironmentPaths, SyncBridge, SyncDirection, TransportMode
from codex_agy_sync.bridge.handoff import HandoffPendingError, _handoff_ledger, _publish_new_file, handoff_session
from codex_agy_sync.bridge.rpc import AgyRpcReadResult
from codex_agy_sync.bridge.transport import DefaultHostTransport
from codex_agy_sync.codecs import decode_trajectory_metadata_proto
from codex_agy_sync.ledger import AssistantTextEvent, CascadeId, CheckpointEvent, CodexThreadId, EventOriginAnchor, HostKind, HostExtensionEnvelope, ThinkingEvent, SystemEvent, ToolCallEvent, ToolResultEvent, ToolCallId, RunCommandPayload, TrajectoryLedger, TurnId, UserInputEvent


class DesktopHandoffTest(unittest.TestCase):
    def setUp(self):
        artifacts = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="handoff-test-", dir=artifacts)
        self.root = Path(self.temp.name)
        self.paths = HostEnvironmentPaths(self.root / "config", self.root / "agy", self.root / "codex", self.root / "state")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.bridge = SyncBridge(self.paths)
        self.binding = self.bridge.pair_workspace(self.workspace)
        self.transport = DefaultHostTransport(self.paths)
        self.tid = CodexThreadId("01960000-0000-7000-8000-000000000011")
        self.anchor = EventOriginAnchor(HostKind.CODEX, TurnId("turn-1"), "2026-11-14T00:00:00Z")
        self.events = [UserInputEvent(kind="user_input", anchor=self.anchor, query_text="Remember CEDAR 523"), AssistantTextEvent(kind="assistant_text", anchor=self.anchor, text="Remembered CEDAR 523")]
        self.ledger = TrajectoryLedger.from_events(self.binding.workspace_path, self.binding.workspace_uri, self.events, explicit_title_override="Handoff test")
        self.transport.apply_to_codex(self.binding, self.tid, self.events, self.ledger, self.bridge.guard.probe_locks(None, self.tid), TransportMode.OFFLINE_STORAGE)

    def tearDown(self):
        self.temp.cleanup()

    def activate(self, endpoint, cid, **kwargs):
        db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        return AgyRpcReadResult(tuple(self.transport._read_persisted_agy_steps(db)), 1)

    def handoff(self):
        return handoff_session(self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(self.tid))

    def test_new_agy_session_registers_native_files_once_without_global_writes(self):
        summary = self.paths.agy_data_dir / "conversation_summaries.db"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_bytes(b"host-owned summary")
        before_codex = (self.paths.codex_home_dir / "state_5.sqlite").read_bytes()
        with mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_agy_daemon", return_value=object()), mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=self.activate) as activate:
            receipt = self.handoff()
            again = self.handoff()
        self.assertEqual(receipt.status, "ready")
        self.assertEqual(again.status, "no_op")
        self.assertEqual(receipt.target_id, again.target_id)
        self.assertEqual(activate.call_count, 1)
        self.assertEqual(summary.read_bytes(), b"host-owned summary")
        self.assertEqual((self.paths.codex_home_dir / "state_5.sqlite").read_bytes(), before_codex)
        cid = receipt.target_id
        db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            trajectory, actual_cid, kind, source = conn.execute("SELECT * FROM trajectory_meta").fetchone()
            self.assertEqual((actual_cid, kind, source), (cid, 4, 1))
            metadata = conn.execute("SELECT data FROM trajectory_metadata_blob").fetchone()[0]
            self.assertEqual(decode_trajectory_metadata_proto(metadata)["root_conversation_id"], cid)
            self.assertEqual({f[0] for f in iter_wire_fields(metadata)}, {1, 2, 6, 7, 18})
            for idx, format_code, meta, payload in conn.execute("SELECT idx,step_format,metadata,step_payload FROM steps"):
                self.assertEqual(format_code, 0)
                self.assertEqual(next(raw for n,w,v,raw in iter_wire_fields(payload) if n == 5), meta)
                pointer = next(raw for n,w,v,raw in iter_wire_fields(meta) if n == 20)
                fields = {n: (v if w == 0 else raw.decode()) for n,w,v,raw in iter_wire_fields(pointer)}
                self.assertEqual(fields, {1: trajectory, 2: idx, 4: cid})
        self.assertNotEqual(db.stat().st_ino, (Path(receipt.staging_path) / "conversations" / db.name).stat().st_ino)
        self.assertIn("CEDAR", (self.paths.agy_data_dir / "brain" / cid / ".system_generated/logs/transcript.jsonl").read_text())

    def test_pending_activation_does_not_allocate_a_second_target(self):
        with mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_agy_daemon", return_value=object()), mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=RuntimeError("lost response")) as activate:
            with self.assertRaises(HandoffPendingError) as first:
                self.handoff()
            with self.assertRaises(HandoffPendingError) as second:
                self.handoff()
        self.assertEqual(first.exception.receipt.target_id, second.exception.receipt.target_id)
        self.assertEqual(len(list((self.paths.agy_data_dir / "conversations").glob("*.db"))), 1)
        self.assertEqual(activate.call_count, 1)

    def test_native_load_with_same_step_count_but_wrong_context_is_pending(self):
        def wrong_context(endpoint, cid, **kwargs):
            result = self.activate(endpoint, cid, **kwargs)
            rows = tuple(dataclasses.replace(row, step_payload_blob=row.step_payload_blob.replace(b"Remembered CEDAR 523", b"Different! CEDAR 523")) for row in result.raw_steps)
            return dataclasses.replace(result, raw_steps=rows)
        with mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_agy_daemon", return_value=object()), mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=wrong_context):
            with self.assertRaises(HandoffPendingError) as failure:
                self.handoff()
        self.assertIn("does not match", failure.exception.receipt.error)

    def test_active_source_is_rejected_before_publication(self):
        with mock.patch.object(DefaultHostTransport, "read_codex_ledger_with_meta", return_value=(self.ledger, TransportMode.ONLINE_RPC, True)):
            with self.assertRaisesRegex(ValueError, "active"):
                self.handoff()
        self.assertFalse((self.paths.agy_data_dir / "conversations").exists())

    def test_missing_project_is_not_created_by_handoff(self):
        other = self.root / "unregistered"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "pair"):
            handoff_session(self.paths, other, direction=SyncDirection.CODEX_TO_AGY, source_id=str(self.tid))
        self.assertFalse((self.paths.sync_state_dir / "handoffs").exists())

    def test_publication_never_overwrites_and_does_not_share_stage_inode(self):
        src, dst = self.root / "source", self.root / "target"
        src.write_bytes(b"new")
        dst.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            _publish_new_file(src, dst)
        self.assertEqual(dst.read_bytes(), b"existing")
        self.assertEqual(list(self.root.glob(".handoff-*")), [])

    def test_source_advance_creates_a_new_target(self):
        with mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_agy_daemon", return_value=object()), mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=self.activate):
            first = self.handoff()
        anchor = dataclasses.replace(self.anchor, turn_id=TurnId("turn-2"))
        events = self.events + [UserInputEvent(kind="user_input", anchor=anchor, query_text="New history"), AssistantTextEvent(kind="assistant_text", anchor=anchor, text="New answer")]
        ledger = TrajectoryLedger.from_events(self.binding.workspace_path, self.binding.workspace_uri, events)
        self.transport.apply_to_codex(self.binding, self.tid, events, ledger, self.bridge.guard.probe_locks(None, self.tid), TransportMode.OFFLINE_STORAGE)
        with mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_agy_daemon", return_value=object()), mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=self.activate):
            second = self.handoff()
        self.assertNotEqual(first.target_id, second.target_id)
        self.assertNotEqual(first.source_digest, second.source_digest)

    def test_codex_rpc_receives_staged_history_and_records_real_target(self):
        cid = CascadeId("00000000-0000-4000-8000-000000000011")
        actual_tid = CodexThreadId("01960000-0000-7000-8000-000000000099")
        with mock.patch.object(DefaultHostTransport, "read_agy_ledger_with_meta", return_value=(self.ledger, TransportMode.ONLINE_RPC, False)), mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_codex_daemon", return_value=object()), mock.patch("codex_agy_sync.bridge.handoff.rpc.import_codex_rollout_via_rpc", create=True, return_value=actual_tid) as importer:
            result = handoff_session(self.paths, self.workspace, direction=SyncDirection.AGY_TO_CODEX, source_id=str(cid), title="Chosen title")
            again = handoff_session(self.paths, self.workspace, direction=SyncDirection.AGY_TO_CODEX, source_id=str(cid))
        self.assertEqual(result.target_id, actual_tid)
        self.assertEqual(again.status, "no_op")
        self.assertEqual(importer.call_count, 1)
        staged = importer.call_args.args[1]
        self.assertTrue(staged.is_relative_to(self.paths.sync_state_dir))
        self.assertIn("CEDAR", staged.read_text())
        self.assertEqual(importer.call_args.args[4], "Chosen title")

    def test_reasoning_summary_is_visible_text_without_native_runtime_state(self):
        from codex_agy_sync.codecs import TrajectoryWireCodec
        source_id = "00000000-0000-4000-8000-000000000077"
        anchor = dataclasses.replace(self.anchor, host_extensions=(HostExtensionEnvelope(HostKind.CODEX, "response_item", b'{"encrypted_content":"private-runtime"}'),))
        events = [SystemEvent(kind="system_message", anchor=anchor, event_type="session_meta", message_text=source_id), self.events[0], ThinkingEvent(kind="thinking", anchor=anchor, summary_text="Visible analysis", raw_thinking="private-runtime", thinking_signature=b"private-runtime"), self.events[1]]
        ledger = TrajectoryLedger.from_events(self.binding.workspace_path, self.binding.workspace_uri, events)
        handed = _handoff_ledger(ledger, None)
        self.assertEqual([e.kind for e in handed.events], ["user_input", "assistant_text", "assistant_text"])
        self.assertEqual(handed.events[1].text, "[Imported reasoning summary]\nVisible analysis")
        self.assertTrue(all(not e.anchor.host_extensions for e in handed.events))
        rows = TrajectoryWireCodec.encode_ledger_to_codex_rollout(handed, session_id=str(self.tid), cwd=str(self.workspace))
        encoded = "\n".join(row.raw_json for row in rows)
        self.assertNotIn("private-runtime", encoded)
        self.assertNotIn(source_id, encoded)
        self.assertNotIn('"type":"reasoning"', encoded)
        self.assertIn("Visible analysis", encoded)

    def test_handoff_filters_observed_codex_runtime_events_but_keeps_other_history(self):
        runtime_types = ["session_meta", "world_state", "codex_developer_message", "codex_event_msg:thread_settings_applied", "codex_response_item:additional_tools"]
        events = [self.events[0]] + [SystemEvent(kind="system_message", anchor=self.anchor, event_type=kind, message_text=kind) for kind in runtime_types]
        events += [SystemEvent(kind="system_message", anchor=self.anchor, event_type="codex_event_msg:unknown_history", message_text="Retain unknown history"), SystemEvent(kind="system_message", anchor=dataclasses.replace(self.anchor, origin_host=HostKind.ANTIGRAVITY), event_type="hook_json_SessionStart", message_text="AGY hook history"), self.events[1]]
        ledger = TrajectoryLedger.from_events(self.binding.workspace_path, self.binding.workspace_uri, events)
        handed = _handoff_ledger(ledger, None)
        retained = [event.event_type for event in handed.events if isinstance(event, SystemEvent)]
        self.assertEqual(retained, ["codex_event_msg:unknown_history", "hook_json_SessionStart"])
        self.assertEqual(handed.events[0].query_text, self.events[0].query_text)
        self.assertEqual(handed.events[-1].text, self.events[1].text)

    def test_handoff_does_not_transfer_native_checkpoint_history_boundaries(self):
        from codex_agy_sync.codecs import TrajectoryWireCodec
        runtime = CheckpointEvent(kind="checkpoint", anchor=dataclasses.replace(self.anchor, host_extensions=(HostExtensionEnvelope(HostKind.CODEX, "turn_context", b'{"summary":"none","model":"gpt-6-astra"}'),)), checkpoint_index=2, user_intent="", session_summary="none", code_change_summary="")
        useful = CheckpointEvent(kind="checkpoint", anchor=dataclasses.replace(self.anchor, origin_host=HostKind.ANTIGRAVITY), checkpoint_index=8, user_intent="Remember ORCHID", session_summary="The earlier number was 317.", code_change_summary="")
        ledger = TrajectoryLedger.from_events(self.binding.workspace_path, self.binding.workspace_uri, [self.events[0], runtime, useful, self.events[1]])
        handed = _handoff_ledger(ledger, None)
        self.assertFalse(any(isinstance(event, CheckpointEvent) for event in handed.events))
        rows = TrajectoryWireCodec.encode_ledger_to_agy_steps(handed)
        self.assertNotIn(23, [row.step_type for row in rows])
        self.assertIn("[Imported checkpoint summary]\nRemember ORCHID\nThe earlier number was 317.", [event.text for event in handed.events if isinstance(event, AssistantTextEvent)])
        self.assertFalse(any(getattr(event, "text", "") == "[Imported checkpoint summary]\nnone" for event in handed.events))

    def test_user_provenance_filters_only_complete_host_injection_metadata(self):
        def message(label, kinds, metadata):
            data = json.dumps({"content_item_kinds": kinds, "content_item_metadata": metadata}).encode()
            anchor = dataclasses.replace(self.anchor, host_extensions=(HostExtensionEnvelope(HostKind.CODEX, "codex_message_provenance", data),))
            return UserInputEvent(kind="user_input", anchor=anchor, query_text=label)
        host = [{"provenance": {"type": "agents_md"}}, {"provenance": {"type": "harness"}}]
        user = [{"provenance": {"type": "user"}}]
        events = [message("Injected project instructions", ["agents_md.instructions", "environments.environment_context"], host), message("Real user", ["user.text"], user), message("Mixed user and host", [], host + user), message("Incomplete metadata", [], host + [{}]), self.events[0], self.events[1]]
        ledger = TrajectoryLedger.from_events(self.binding.workspace_path, self.binding.workspace_uri, events)
        handed = _handoff_ledger(ledger, None)
        self.assertEqual([event.query_text for event in handed.events if isinstance(event, UserInputEvent)], ["Real user", "Mixed user and host", "Incomplete metadata", self.events[0].query_text])

    def test_only_unpaired_codex_apps_runtime_outputs_are_filtered(self):
        payload = RunCommandPayload(kind="run_command", command_line="python3 -c 'print(5663)'", cwd=str(self.workspace), exit_code=0)
        call = ToolCallEvent(kind="tool_call", anchor=self.anchor, call_id=ToolCallId("real-exec"), tool_name="exec_command", tool_summary="Python product", payload=payload)
        result = ToolResultEvent(kind="tool_result", anchor=self.anchor, call_id=call.call_id, tool_name=call.tool_name, output_text="5663", is_error=False, payload=payload)
        raw = json.dumps({"type": "function_call_output", "namespace": "agent", "name": "codex_apps_open_page", "output": "Injected page"}).encode()
        injected = dataclasses.replace(result, call_id=ToolCallId("page-injection"), tool_name="unknown_tool", output_text="Injected page", anchor=dataclasses.replace(self.anchor, host_extensions=(HostExtensionEnvelope(HostKind.CODEX, "codex_unpaired_tool_output", raw),)))
        unknown = dataclasses.replace(result, call_id=ToolCallId("unpaired-history"), tool_name="unknown_tool", output_text="Keep unknown output")
        ledger = TrajectoryLedger.from_events(self.binding.workspace_path, self.binding.workspace_uri, [self.events[0], call, result, injected, unknown, self.events[1]])
        handed = _handoff_ledger(ledger, None)
        text = "\n".join(event.text for event in handed.events if isinstance(event, AssistantTextEvent))
        self.assertIn("[Imported tool call] exec_command", text)
        self.assertIn("Output:\n5663", text)
        self.assertIn("Output:\nKeep unknown output", text)
        self.assertNotIn("Injected page", text)
        self.assertFalse(any(isinstance(event, (ToolCallEvent, ToolResultEvent)) for event in handed.events))

    def test_real_custom_exec_history_preserves_input_and_output_across_both_hosts(self):
        from codex_agy_sync.codecs import CodexRawRolloutLine, TrajectoryWireCodec
        from codex_agy_sync.ledger import RolloutLineNo, OpaqueToolPayload, INTERRUPTED_TOOL_OUTPUT
        source_input = "const r = await tools.exec_command({cmd:\"python3 -c 'print(809 * 7)'\",login:false,max_output_tokens:100});text(r.output);\n"
        source_output = "Script completed\nWall time 0.0 seconds\nOutput:\n\n5663\n"
        records = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "native-turn"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Use Python to calculate 809 * 7."}]}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "call-real-exec", "name": "exec", "input": source_input}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call-real-exec", "output": [{"type": "input_text", "text": "Script completed\nWall time 0.0 seconds\nOutput:\n"}, {"type": "input_text", "text": "5663\n"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "The product is 5663."}]}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "native-turn"}},
        ]
        lines = [CodexRawRolloutLine(RolloutLineNo(i + 1), json.dumps(record)) for i, record in enumerate(records)]
        codec = TrajectoryWireCodec()
        source = codec.decode_codex_rollout_to_ledger(self.binding.workspace_path, self.binding.workspace_uri, lines)
        call = next(event for event in source.events if isinstance(event, ToolCallEvent))
        self.assertIsInstance(call.payload, OpaqueToolPayload)
        self.assertEqual(call.payload.raw_arguments_json, source_input)
        first = _handoff_ledger(source, None)
        agy_rows = codec.encode_ledger_to_agy_steps(first)
        self.assertNotIn(132, [row.step_type for row in agy_rows])
        agy = codec.decode_agy_steps_to_ledger(self.binding.workspace_path, self.binding.workspace_uri, agy_rows)
        second = _handoff_ledger(agy, None)
        codex_rows = codec.encode_ledger_to_codex_rollout(second, session_id=str(self.tid), cwd=str(self.workspace))
        returned = codec.decode_codex_rollout_to_ledger(self.binding.workspace_path, self.binding.workspace_uri, codex_rows)
        texts = [event.text for event in returned.events if isinstance(event, AssistantTextEvent)]
        self.assertIn(f"[Imported tool call] exec\nCall ID: call-real-exec\nArguments:\n{source_input}", texts)
        self.assertIn(f"[Imported tool result] exec\nCall ID: call-real-exec\nis_error: false\nOutput:\n{source_output}", texts)
        self.assertFalse(any(INTERRUPTED_TOOL_OUTPUT in text for text in texts))
        self.assertFalse(any(isinstance(event, (ToolCallEvent, ToolResultEvent)) for event in returned.events))

    def test_imported_failed_tool_result_stays_explicitly_failed(self):
        payload = RunCommandPayload(kind="run_command", command_line="false", cwd=str(self.workspace), exit_code=1)
        result = ToolResultEvent(kind="tool_result", anchor=self.anchor, call_id=ToolCallId("failed-call"), tool_name="exec_command", output_text="Process exited with code 1", is_error=True, payload=payload)
        source = TrajectoryLedger.from_events(self.binding.workspace_path, self.binding.workspace_uri, [self.events[0], result, self.events[1]])
        handed = _handoff_ledger(source, None)
        text = next(event.text for event in handed.events if isinstance(event, AssistantTextEvent) and event.text.startswith("[Imported tool result]"))
        self.assertIn("is_error: true\nOutput:\nProcess exited with code 1", text)

    def test_codex_uncertain_response_preserves_known_target(self):
        error = RuntimeError("response timeout")
        error.thread_id = "01960000-0000-7000-8000-000000000099"
        with mock.patch.object(DefaultHostTransport, "read_agy_ledger_with_meta", return_value=(self.ledger, TransportMode.ONLINE_RPC, False)), mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_codex_daemon", return_value=object()), mock.patch("codex_agy_sync.bridge.handoff.rpc.import_codex_rollout_via_rpc", create=True, side_effect=error):
            with self.assertRaises(HandoffPendingError) as failure:
                handoff_session(self.paths, self.workspace, direction=SyncDirection.AGY_TO_CODEX, source_id="00000000-0000-4000-8000-000000000011")
        self.assertEqual(failure.exception.receipt.target_id, error.thread_id)
        saved = list((self.paths.sync_state_dir / "handoffs").glob("*.json"))
        self.assertEqual(json.loads(saved[0].read_text())["target_id"], error.thread_id)


if __name__ == "__main__":
    unittest.main()
