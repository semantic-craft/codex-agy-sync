from __future__ import annotations

import json
from pathlib import Path
import unittest

from codex_agy_sync import __version__
from codex_agy_sync.codecs import CodexRawRolloutLine, decode_codex_rollout, encode_codex_rollout
from codex_agy_sync.ledger import (
    AssistantTextEvent, EventOriginAnchor, HostExtensionEnvelope, HostKind,
    RolloutLineNo, SystemEvent, ToolResultEvent, TurnId, UserInputEvent, parse_workspace_identity,
)


class TestDesktopRollout(unittest.TestCase):
    def setUp(self) -> None:
        self.ws_path, self.ws_uri = parse_workspace_identity(Path.cwd())
        self.events = []
        for turn in (1, 2):
            anchor = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId(f"turn-{turn}"), f"2026-11-14T00:0{turn}:00Z")
            self.events.extend((
                UserInputEvent(kind="user_input", anchor=anchor, query_text="Remember the same code.\n口令"),
                AssistantTextEvent(kind="assistant_text", anchor=anchor, text="The code is remembered."),
            ))

    def test_generated_header_and_desktop_messages_survive_repeated_roundtrips(self) -> None:
        expected = [(event.kind, event.anchor.turn_id, getattr(event, "query_text", getattr(event, "text", ""))) for event in self.events]
        events = self.events
        for _ in range(3):
            lines = encode_codex_rollout(events, session_id="01960000-0000-7000-8000-000000000011", cwd=str(self.ws_path))
            records = [json.loads(line.raw_json) for line in lines]
            self.assertEqual(records[0]["payload"]["cli_version"], __version__)
            model_messages = [row["payload"] for row in records if row["type"] == "response_item" and row["payload"].get("type") == "message"]
            desktop_messages = [row["payload"] for row in records if row["type"] == "event_msg" and row["payload"].get("type") in ("user_message", "agent_message")]
            self.assertEqual(len(model_messages), 4)
            self.assertEqual([row["type"] for row in desktop_messages], ["user_message", "agent_message"] * 2)
            self.assertEqual([row["message"] for row in desktop_messages], [text for _, _, text in expected])
            events = list(decode_codex_rollout(self.ws_path, self.ws_uri, lines).events)
            actual = [(event.kind, event.anchor.turn_id, getattr(event, "query_text", getattr(event, "text", ""))) for event in events if isinstance(event, (UserInputEvent, AssistantTextEvent))]
            self.assertEqual(actual, expected)

    def test_preserved_native_session_version_is_not_replaced(self) -> None:
        metadata = {"id": "01960000-0000-7000-8000-000000000011", "cli_version": "native-version", "cwd": str(self.ws_path)}
        anchor = EventOriginAnchor(HostKind.CODEX, TurnId("turn-1"), "2026-11-14T00:00:00Z", host_extensions=(HostExtensionEnvelope(HostKind.CODEX, "session_meta", json.dumps(metadata).encode()),))
        session = SystemEvent(kind="system_message", anchor=anchor, event_type="session_meta", message_text="session")
        lines = encode_codex_rollout([session, *self.events], cwd=str(self.ws_path))
        self.assertEqual(json.loads(lines[0].raw_json)["payload"]["cli_version"], "native-version")

    def test_native_messages_without_event_turn_ids_merge_before_turn_inference(self) -> None:
        records = []
        for turn in (1, 2):
            raw_turn = f"native-turn-{turn}"
            records.extend([
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": raw_turn}},
                {"type": "response_item", "payload": {"type": "function_call_output", "name": "codex_apps_open_page", "namespace": "agent", "output": "Native context", "internal_chat_message_metadata_passthrough": {"turn_id": raw_turn}}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Remember the repeated code."}], "internal_chat_message_metadata_passthrough": {"turn_id": raw_turn}}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Remember the repeated code."}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "The code is remembered."}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "The code is remembered."}], "internal_chat_message_metadata_passthrough": {"turn_id": raw_turn}}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": raw_turn}},
            ])
        lines = [CodexRawRolloutLine(RolloutLineNo(index + 1), json.dumps(row)) for index, row in enumerate(records)]
        ledger = decode_codex_rollout(self.ws_path, self.ws_uri, lines)
        messages = [(event.kind, event.anchor.turn_id) for event in ledger.events if isinstance(event, (UserInputEvent, AssistantTextEvent))]
        self.assertEqual(messages, [("user_input", "turn-1"), ("assistant_text", "turn-1"), ("user_input", "turn-2"), ("assistant_text", "turn-2")])

    def test_injected_and_real_user_provenance_are_preserved_without_filtering(self) -> None:
        provenance = [
            {"content_item_kinds": ["agents_md.instructions", "environments.environment_context"], "content_item_metadata": [{"provenance": {"type": "agents_md"}}, {"provenance": {"type": "harness"}}]},
            {"content_item_kinds": ["user.text"], "content_item_metadata": [{"provenance": {"type": "user"}}]},
        ]
        records = [{"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}], "internal_chat_message_metadata_passthrough": {"turn_id": "native-turn", **metadata}}} for text, metadata in zip(("Injected instructions", "Real user request"), provenance)]
        lines = [CodexRawRolloutLine(RolloutLineNo(index + 1), json.dumps(row)) for index, row in enumerate(records)]
        for _ in range(2):
            ledger = decode_codex_rollout(self.ws_path, self.ws_uri, lines)
            users = [event for event in ledger.events if isinstance(event, UserInputEvent)]
            self.assertEqual([event.query_text for event in users], ["Injected instructions", "Real user request"])
            self.assertEqual([json.loads(event.anchor.host_extensions[0].raw_payload) for event in users], provenance)
            lines = list(encode_codex_rollout(ledger.events, cwd=str(self.ws_path)))

    def test_hosted_output_without_call_id_keeps_native_context_metadata(self) -> None:
        hosted = {"type": "function_call_output", "id": "hosted-output", "namespace": "agent", "name": "codex_apps_open_page", "output": "Selected page context"}
        records = [
            {"type": "response_item", "payload": hosted},
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "call-python", "arguments": json.dumps({"cmd": "python3 -c 'print(809 * 7)'"})}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-python", "output": "5663"}},
        ]
        lines = [CodexRawRolloutLine(RolloutLineNo(index + 1), json.dumps(row)) for index, row in enumerate(records)]
        ledger = decode_codex_rollout(self.ws_path, self.ws_uri, lines)
        outputs = [event for event in ledger.events if isinstance(event, ToolResultEvent)]
        self.assertEqual(json.loads(outputs[0].anchor.host_extensions[0].raw_payload), hosted)
        self.assertEqual(outputs[0].anchor.host_extensions[0].extension_type, "codex_unpaired_tool_output")
        self.assertEqual(outputs[1].call_id, "call-python")
        self.assertEqual(outputs[1].tool_name, "exec_command")
        self.assertEqual(outputs[1].output_text, "5663")
        self.assertFalse(outputs[1].anchor.host_extensions)
        encoded = encode_codex_rollout(ledger.events, cwd=str(self.ws_path))
        native_outputs = [json.loads(row.raw_json)["payload"] for row in encoded if json.loads(row.raw_json)["payload"].get("id") == "hosted-output"]
        self.assertEqual(native_outputs, [hosted])
        replayed = decode_codex_rollout(self.ws_path, self.ws_uri, encoded)
        replayed_outputs = [event for event in replayed.events if isinstance(event, ToolResultEvent)]
        self.assertEqual(json.loads(replayed_outputs[0].anchor.host_extensions[0].raw_payload), hosted)
        self.assertEqual(replayed_outputs[1].call_id, "call-python")
