from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from codex_agy_sync.bridge.native_codex import NativeCodexImportPending, import_codex_rollout_via_rpc
from codex_agy_sync.bridge.paths_and_locks import CodexDaemonEndpoint


class TestNativeCodexImport(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_id = "01960000-0000-7000-8000-000000000011"
        self.target_id = "01960000-0000-7000-8000-000000000022"
        self.rollout = self.root / "rollout.jsonl"
        rows = [{"type": "session_meta", "payload": {"id": self.source_id}}, {"type": "response_item", "payload": {"type": "message"}}]
        self.rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.endpoint = CodexDaemonEndpoint(self.root / "app-server.sock")
        self.sock = mock.Mock(spec=socket.socket)
        self.ws = mock.Mock()
        self.addCleanup(self.temp.cleanup)
        for name, replacement in [("socket.socket", self.sock), ("UnixWebSocketClient", self.ws)]:
            patcher = mock.patch(f"codex_agy_sync.bridge.native_codex.{name}", return_value=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("codex_agy_sync.bridge.native_codex.has_live_unix_socket", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def responses(self, *, items: bool = True, unsubscribe: str = "unsubscribed") -> list[dict]:
        thread = {"id": self.target_id, "projectId": "project-1", "status": {"type": "idle"}, "turns": [{"items": [{"type": "userMessage"}] if items else []}]}
        return [{"id": 1, "result": {}}, {"id": 2, "result": {"thread": {"id": self.target_id}}}, {"id": 3, "result": {}}, {"id": 4, "result": {}}, {"id": 5, "result": {"thread": thread}}, {"id": 6, "result": {"status": unsubscribe}}]

    def invoke(self) -> str:
        return import_codex_rollout_via_rpc(self.endpoint, self.rollout, self.root, "project-1", "Imported", timeout_s=90)

    def test_returns_actual_id_only_after_matching_close_notification(self) -> None:
        responses = self.responses()
        responses.insert(5, {"method": "thread/closed", "params": {"threadId": "unrelated"}})
        responses.append({"method": "thread/closed", "params": {"threadId": self.target_id}})
        self.ws.recv_json.side_effect = responses
        self.assertEqual(self.invoke(), self.target_id)
        requests = [call.args[0] for call in self.ws.send_json.call_args_list]
        self.assertEqual([request["method"] for request in requests], ["initialize", "initialized", "thread/fork", "thread/metadata/update", "thread/name/set", "thread/read", "thread/unsubscribe"])
        self.assertEqual(requests[2]["params"]["threadId"], self.source_id)
        self.assertEqual(requests[2]["params"]["path"], str(self.rollout.resolve()))
        self.sock.close.assert_called_once()

    def test_close_before_unsubscribe_response_is_retained(self) -> None:
        responses = self.responses()
        responses.insert(5, {"method": "thread/closed", "params": {"threadId": self.target_id}})
        self.ws.recv_json.side_effect = responses
        self.assertEqual(self.invoke(), self.target_id)

    def test_not_loaded_acknowledgement_is_immediately_ready(self) -> None:
        self.ws.recv_json.side_effect = self.responses(unsubscribe="notLoaded")
        self.assertEqual(self.invoke(), self.target_id)

    def test_not_loaded_status_notification_confirms_unload(self) -> None:
        self.ws.recv_json.side_effect = [*self.responses(), {"method": "thread/status/changed", "params": {"threadId": self.target_id, "status": {"type": "notLoaded"}}}]
        self.assertEqual(self.invoke(), self.target_id)

    def test_unload_timeout_keeps_actual_id_without_another_fork(self) -> None:
        self.ws.recv_json.side_effect = [*self.responses(), TimeoutError("still loaded")]
        with self.assertRaises(NativeCodexImportPending) as raised:
            self.invoke()
        self.assertEqual(raised.exception.thread_id, self.target_id)
        self.assertEqual(sum(call.args[0]["method"] == "thread/fork" for call in self.ws.send_json.call_args_list), 1)
        self.sock.close.assert_called_once()

    def test_fork_response_timeout_is_uncertain_and_does_not_retry(self) -> None:
        self.ws.recv_json.side_effect = [{"id": 1, "result": {}}, TimeoutError("response lost")]
        with self.assertRaises(NativeCodexImportPending) as raised:
            self.invoke()
        self.assertIsNone(raised.exception.thread_id)
        self.assertEqual(sum(call.args[0]["method"] == "thread/fork" for call in self.ws.send_json.call_args_list), 1)

    def test_nonempty_source_with_empty_readback_still_unsubscribes(self) -> None:
        self.ws.recv_json.side_effect = self.responses(items=False)
        with self.assertRaises(NativeCodexImportPending) as raised:
            self.invoke()
        self.assertEqual(raised.exception.thread_id, self.target_id)
        self.assertIn("without Desktop items", str(raised.exception))
        self.assertEqual(self.ws.send_json.call_args_list[-1].args[0]["method"], "thread/unsubscribe")

    def test_rpc_error_data_is_not_exposed_in_pending_receipt(self) -> None:
        responses = self.responses()[:2]
        responses.extend([{"id": 3, "error": {"code": -32000, "message": "secret-prompt", "data": {"token": "secret-token"}}}, {"id": 4, "result": {"status": "notLoaded"}}])
        self.ws.recv_json.side_effect = responses
        with self.assertRaises(NativeCodexImportPending) as raised:
            self.invoke()
        self.assertEqual(raised.exception.thread_id, self.target_id)
        self.assertIn("thread/metadata/update", str(raised.exception))
        self.assertIn("-32000", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
