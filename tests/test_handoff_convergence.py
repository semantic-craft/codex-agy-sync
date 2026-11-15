from __future__ import annotations

import contextlib
import fcntl
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_agy_sync import (
    AgyDaemonEndpoint,
    CascadeId,
    CodexDaemonEndpoint,
    CodexRpcReadResult,
    CodexThreadId,
    CsrfSecret,
    HandoffPendingError,
    HandoffReceipt,
    HostEnvironmentPaths,
    HostKind,
    OnlineRpcUnavailableError,
    SyncBridge,
    SyncBusyLockError,
    SyncDirection,
    TransportMode,
    WorkbenchServer,
    WorkbenchService,
    execute_handoff,
    handoff_session,
    reconcile_workspace_handoffs,
    summarize_workspace_handoffs,
)
from codex_agy_sync.bridge.rpc import AgyRpcReadResult
from codex_agy_sync.bridge.transport import DefaultHostTransport
from codex_agy_sync.cli import main as cli_main
from codex_agy_sync.codecs import decode_codex_rollout, iter_codex_rollout_lines
from codex_agy_sync.events import INTERRUPTED_TOOL_OUTPUT, INTERRUPTED_TURN_TEXT
from codex_agy_sync.ledger import (
    AssistantTextEvent,
    EventOriginAnchor,
    RunCommandPayload,
    ToolCallEvent,
    ToolCallId,
    TrajectoryLedger,
    TurnId,
    UserInputEvent,
)


def _turn_pair(host: HostKind, turn_id: str, ts_iso: str, user_text: str, asst_text: str) -> list:
    anchor = EventOriginAnchor(origin_host=host, turn_id=TurnId(turn_id), timestamp_iso=ts_iso)
    return [
        UserInputEvent(kind="user_input", anchor=anchor, query_text=user_text),
        AssistantTextEvent(kind="assistant_text", anchor=anchor, text=asst_text),
    ]


def _http_post(port: int, path: str, headers: dict[str, str], payload: dict) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        req_headers = {"Content-Type": "application/json", **headers}
        conn.request("POST", path, body=json.dumps(payload).encode("utf-8"), headers=req_headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw)
    finally:
        conn.close()


class HandoffConvergenceTest(unittest.TestCase):
    def setUp(self) -> None:
        artifacts = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="handoff-conv-", dir=artifacts)
        self.root = Path(self.temp.name)
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.root / "config",
            agy_data_dir=self.root / "agy",
            codex_home_dir=self.root / "codex",
            sync_state_dir=self.root / "state",
            agy_cli_data_dir=self.root / "agy-cli",
        )
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.bridge = SyncBridge(self.paths)
        self.transport = DefaultHostTransport(self.paths)
        self.repo_root = Path(__file__).resolve().parents[1]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _base_cli_args(self) -> list[str]:
        return [
            "--gemini-config-dir", str(self.paths.gemini_config_dir),
            "--agy-data-dir", str(self.paths.agy_data_dir),
            "--agy-cli-data-dir", str(self.paths.agy_cli_data_dir),
            "--codex-home-dir", str(self.paths.codex_home_dir),
            "--sync-state-dir", str(self.paths.sync_state_dir),
        ]

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = cli_main(self._base_cli_args() + argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def _seed_codex(self, binding, raw_tid: str, events: list, title: str = "Codex Work") -> CodexThreadId:
        tid = CodexThreadId(raw_tid)
        ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, events, explicit_title_override=title)
        self.transport.apply_to_codex(
            binding, tid, ledger.events, ledger, self.bridge.guard.probe_locks(None, tid), TransportMode.OFFLINE_STORAGE,
        )
        return tid

    def _seed_agy(self, binding, raw_cid: str, events: list, title: str = "AGY Work") -> CascadeId:
        cid = CascadeId(raw_cid)
        ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, events, explicit_title_override=title)
        self.transport.apply_to_agy(
            binding, cid, ledger.events, ledger, self.bridge.guard.probe_locks(cid, None), TransportMode.OFFLINE_STORAGE,
        )
        return cid

    def _mock_daemons(self, *, agy_active: bool = False, codex_active: bool = False):
        fake_sock = self.paths.codex_home_dir / "fake-app.sock"
        fake_sock.parent.mkdir(parents=True, exist_ok=True)
        fake_sock.touch(exist_ok=True)

        def _fake_agy_rpc(endpoint: object, cid: CascadeId, **kwargs: object) -> AgyRpcReadResult:
            _ = endpoint, kwargs
            db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
            return AgyRpcReadResult(tuple(self.transport.read_persisted_agy_steps(db)), 2 if agy_active else 1)

        def _fake_codex_rpc(endpoint: object, tid: CodexThreadId, **kwargs: object) -> CodexRpcReadResult:
            _ = endpoint, kwargs
            rp, title = self.transport.find_codex_rollout_and_title(tid)
            return CodexRpcReadResult(
                thread={"id": str(tid), "name": title or "", "path": str(rp), "turns": []},
                is_active=codex_active,
            )

        def _fake_codex_import(endpoint: object, rollout_path: Path, ws_path: Path, pid: str, title: str, timeout_s: float = 90.0) -> str:
            _ = endpoint, ws_path, pid, timeout_s
            binding = self.bridge.registry.resolve_readonly_binding(self.workspace)
            lines = tuple(iter_codex_rollout_lines(rollout_path))
            ledger = decode_codex_rollout(binding.workspace_path, binding.workspace_uri, lines, explicit_title_override=title)
            new_tid = CodexThreadId(f"01970000-0000-7000-8000-{len(list((self.paths.codex_home_dir / 'sessions').rglob('rollout-*.jsonl'))) + 1:012x}")
            self.transport.apply_to_codex(
                binding, new_tid, ledger.events, ledger, self.bridge.guard.probe_locks(None, new_tid), TransportMode.OFFLINE_STORAGE,
            )
            return str(new_tid)

        fake_agy_ep = AgyDaemonEndpoint(os.getpid(), 0, 0, "2.30.0", CsrfSecret("test-only-token"))
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_agy_daemon", return_value=fake_agy_ep))
        stack.enter_context(mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_codex_daemon", return_value=CodexDaemonEndpoint(fake_sock, os.getpid())))
        stack.enter_context(mock.patch("codex_agy_sync.bridge.rpc.fetch_agy_trajectory_via_rpc", side_effect=_fake_agy_rpc))
        stack.enter_context(mock.patch("codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc", side_effect=_fake_codex_rpc))
        stack.enter_context(mock.patch("codex_agy_sync.bridge.handoff.import_codex_rollout_via_rpc", side_effect=_fake_codex_import))
        return stack

    def test_1_agy_cli_data_dir_transcript_mirroring(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        tid = self._seed_codex(binding, "01970000-0000-7000-8000-000000000101", _turn_pair(HostKind.CODEX, "t1", "2026-11-15T01:00:00Z", "Mirror", "Check"))
        with self._mock_daemons():
            receipt = handoff_session(self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(tid))
        self.assertEqual(receipt.status, "ready")
        assert receipt.target_id is not None
        desktop_tx = self.paths.agy_data_dir / "brain" / receipt.target_id / ".system_generated" / "logs" / "transcript.jsonl"
        cli_tx = self.paths.agy_cli_data_dir / "brain" / receipt.target_id / ".system_generated" / "logs" / "transcript.jsonl"
        self.assertTrue(desktop_tx.is_file())
        self.assertTrue(cli_tx.is_file())
        self.assertEqual(desktop_tx.read_bytes(), cli_tx.read_bytes())

        # Same agy_cli_data_dir == agy_data_dir path must not crash with FileExistsError.
        same_paths = HostEnvironmentPaths(
            gemini_config_dir=self.paths.gemini_config_dir,
            agy_data_dir=self.paths.agy_data_dir,
            codex_home_dir=self.paths.codex_home_dir,
            sync_state_dir=self.paths.sync_state_dir,
            agy_cli_data_dir=self.paths.agy_data_dir,
        )
        tid2 = self._seed_codex(binding, "01970000-0000-7000-8000-000000000102", _turn_pair(HostKind.CODEX, "t2", "2026-11-15T01:05:00Z", "SameDir", "Ok"))
        with self._mock_daemons():
            rec2 = handoff_session(same_paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(tid2))
        self.assertEqual(rec2.status, "ready")

    def test_2_include_interrupted_rescues_stopped_and_rejects_active(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        t1 = EventOriginAnchor(HostKind.CODEX, TurnId("t1"), "2026-11-15T01:00:00Z")
        t2 = EventOriginAnchor(HostKind.CODEX, TurnId("t2"), "2026-11-15T01:02:00Z")
        codex_inc = [
            *_turn_pair(HostKind.CODEX, "t1", "2026-11-15T01:00:00Z", "Done", "Ready"),
            UserInputEvent(kind="user_input", anchor=t2, query_text="Interrupted turn"),
            ToolCallEvent(
                kind="tool_call", anchor=t2, call_id=ToolCallId("c-2"), tool_name="run_command",
                tool_summary="Run test", payload=RunCommandPayload("run_command", "echo test", str(self.workspace)),
            ),
        ]
        tid = self._seed_codex(binding, "01970000-0000-7000-8000-000000000201", codex_inc, title="Interrupted Codex")

        with self.assertRaisesRegex(ValueError, "incomplete"):
            handoff_session(self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(tid))

        with mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_codex_daemon", return_value=CodexDaemonEndpoint(self.root / "fake.sock", os.getpid())), \
                mock.patch("codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc", return_value=CodexRpcReadResult({"id": str(tid), "turns": [{"id": "t2", "items": [{"type": "userMessage", "content": [{"type": "text", "text": "Live"}]}]}]}, True)):
            with self.assertRaisesRegex(ValueError, "active"):
                handoff_session(self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(tid), include_interrupted=True)

        fake_agy_ep = AgyDaemonEndpoint(os.getpid(), 0, 0, "2.30.0", CsrfSecret("test-only-token"))
        with mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_agy_daemon", return_value=fake_agy_ep), \
                mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_codex_daemon", return_value=None), \
                mock.patch("codex_agy_sync.bridge.rpc.fetch_agy_trajectory_via_rpc", side_effect=lambda ep, cid, **kw: AgyRpcReadResult(tuple(self.transport.read_persisted_agy_steps(self.paths.agy_data_dir / "conversations" / f"{cid}.db")), 1)):
            rc, out, err = self._run_cli(["handoff", "-w", str(self.workspace), "--latest", "--include-interrupted"])
            self.assertEqual((rc, err), (0, ""))
            rec = json.loads(out)
            self.assertEqual(rec["status"], "ready")
            agy_imported = self.bridge.inspect_session_ledger(self.workspace, cascade_id=CascadeId(rec["target_id"]))
            asst_texts = "\n".join(e.text for e in agy_imported.events if isinstance(e, AssistantTextEvent))
            self.assertIn(INTERRUPTED_TURN_TEXT, asst_texts)
            self.assertIn(INTERRUPTED_TOOL_OUTPUT, asst_texts)

        a_anchor = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("at1"), "2026-11-15T02:00:00Z")
        agy_cid = self._seed_agy(
            binding,
            "22222222-2222-4222-8222-222222220202",
            [
                UserInputEvent(kind="user_input", anchor=a_anchor, query_text="AGY Interrupted"),
                ToolCallEvent(kind="tool_call", anchor=a_anchor, call_id=ToolCallId("ac-1"), tool_name="run_command", tool_summary="Run", payload=RunCommandPayload("run_command", "ls", str(self.workspace))),
            ],
            title="AGY Incomplete",
        )
        # Seed a live Codex session newer than AGY stopped-incomplete session; --latest --include-interrupted must exclude live Codex and select AGY.
        live_tid = self._seed_codex(binding, "01970000-0000-7000-8000-000000000203", _turn_pair(HostKind.CODEX, "lt", "2026-11-15T05:00:00Z", "Live", "Working"), title="Live Codex")

        def _selective_codex_rpc(ep: object, qtid: CodexThreadId, **kw: object) -> CodexRpcReadResult:
            _ = ep, kw
            rp, t_name = self.transport.find_codex_rollout_and_title(qtid)
            return CodexRpcReadResult({"id": str(qtid), "name": t_name or "", "path": str(rp), "turns": []}, is_active=(qtid == live_tid))

        fake_sock = self.paths.codex_home_dir / "fake-selective.sock"
        fake_sock.touch(exist_ok=True)
        with mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_agy_daemon", return_value=None), \
                mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_codex_daemon", return_value=CodexDaemonEndpoint(fake_sock, os.getpid())), \
                mock.patch("codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc", side_effect=_selective_codex_rpc), \
                mock.patch("codex_agy_sync.bridge.handoff.import_codex_rollout_via_rpc", return_value="01970000-0000-7000-8000-000000000299"):
            sel_rec, _ = execute_handoff(self.paths, self.workspace, latest=True, include_interrupted=True)
        self.assertEqual((sel_rec.source_id, sel_rec.target_id, sel_rec.status), (str(agy_cid), "01970000-0000-7000-8000-000000000299", "ready"))

        # Chain advancement when baton target received a stopped interrupted turn.
        baton_cid = CascadeId(rec["target_id"])
        baton_ledger = self.bridge.inspect_session_ledger(self.workspace, cascade_id=baton_cid)
        b_anc = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("bt-inc"), "2026-11-15T06:00:00Z")
        b_events = [
            *baton_ledger.events,
            UserInputEvent(kind="user_input", anchor=b_anc, query_text="Baton next interrupted"),
            ToolCallEvent(kind="tool_call", anchor=b_anc, call_id=ToolCallId("bc-1"), tool_name="run_command", tool_summary="Exec", payload=RunCommandPayload("run_command", "echo chain", str(self.workspace))),
        ]
        self.transport.apply_to_agy(
            binding, baton_cid, b_events, TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, b_events),
            self.bridge.guard.probe_locks(baton_cid, None), TransportMode.OFFLINE_STORAGE,
        )
        with mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_agy_daemon", return_value=None), \
                mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_codex_daemon", return_value=CodexDaemonEndpoint(fake_sock, os.getpid())), \
                mock.patch("codex_agy_sync.bridge.handoff.import_codex_rollout_via_rpc", return_value="01970000-0000-7000-8000-000000000298"):
            chain_rec, _ = execute_handoff(self.paths, self.workspace, chain=True, source_id=str(baton_cid), include_interrupted=True)
        self.assertEqual((chain_rec.source_id, chain_rec.target_id, chain_rec.chain_depth, chain_rec.status), (str(baton_cid), "01970000-0000-7000-8000-000000000298", 1, "ready"))

    def test_3_reconcile_promotes_clears_preserves_partial_and_guards_paths(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        tid = self._seed_codex(binding, "01970000-0000-7000-8000-000000000301", _turn_pair(HostKind.CODEX, "t1", "2026-11-15T01:00:00Z", "P1", "R1"))
        # First fail AGY import after files hit agy_data_dir so receipt stays pending with real staged files.
        with mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_agy_daemon", return_value=object()), \
                mock.patch("codex_agy_sync.bridge.rpc.fetch_agy_trajectory_via_rpc", side_effect=RuntimeError("rpc timeout")):
            with self.assertRaises(HandoffPendingError) as pend_ctx:
                handoff_session(self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(tid))
        self.assertIn("--reconcile", str(pend_ctx.exception))

        state_dir = self.paths.sync_state_dir / "handoffs"
        outside_keep = self.root / "outside-keep"
        outside_keep.mkdir()
        (outside_keep / "important.txt").write_text("keep", encoding="utf-8")

        absent_stage = state_dir / "staging" / "absent-key"
        absent_stage.mkdir(parents=True, exist_ok=True)
        (absent_stage / "orphan.txt").write_text("tmp", encoding="utf-8")
        absent_receipt = state_dir / "absent.json"
        absent_receipt.write_text(json.dumps({
            "workspace": str(self.workspace), "source_host": "antigravity", "source_id": "33333333-3333-4333-8333-333333330302",
            "source_digest": "d2", "target_host": "codex", "target_id": None,
            "title": "Absent Codex", "status": "pending", "staging_path": str(absent_stage), "error": "TimeoutError",
        }), encoding="utf-8")

        escaped_receipt = state_dir / "escaped.json"
        escaped_receipt.write_text(json.dumps({
            "workspace": str(self.workspace), "source_host": "codex", "source_id": "01970000-0000-7000-8000-000000000303",
            "source_digest": "d3", "target_host": "antigravity", "target_id": "33333333-3333-4333-8333-333333330303",
            "title": "Escaped Stage", "status": "pending", "staging_path": str(outside_keep), "error": "boom",
        }), encoding="utf-8")

        partial_cid = "33333333-3333-4333-8333-333333330304"
        (self.paths.agy_data_dir / "annotations").mkdir(parents=True, exist_ok=True)
        partial_pbtxt = self.paths.agy_data_dir / "annotations" / f"{partial_cid}.pbtxt"
        partial_pbtxt.write_text("partial", encoding="utf-8")
        partial_receipt = state_dir / "partial.json"
        partial_receipt.write_text(json.dumps({
            "workspace": str(self.workspace), "source_host": "codex", "source_id": "01970000-0000-7000-8000-000000000304",
            "source_digest": "d4", "target_host": "antigravity", "target_id": partial_cid,
            "title": "Partial AGY", "status": "pending", "staging_path": str(state_dir / "staging" / "partial"), "error": "partial",
        }), encoding="utf-8")

        # Pending Codex receipt with live RPC verifying materialization.
        codex_target_tid = self._seed_codex(binding, "01970000-0000-7000-8000-000000000399", _turn_pair(HostKind.CODEX, "ct1", "2026-11-15T02:00:00Z", "Promoted", "Codex Target"))
        codex_pend_receipt = state_dir / "codex-promoted.json"
        codex_pend_receipt.write_text(json.dumps({
            "workspace": str(self.workspace), "source_host": "antigravity", "source_id": "33333333-3333-4333-8333-333333330305",
            "source_digest": "d5", "target_host": "codex", "target_id": str(codex_target_tid),
            "title": "Codex Target", "status": "pending", "staging_path": str(state_dir / "staging" / "codex-p"),
            "error": "NativeCodexImportPending", "target_seed_turn_count": 1,
        }), encoding="utf-8")

        sock_path = self.paths.codex_home_dir / "reconcile-live.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(1)
        try:
            def _codex_rpc_ok(ep: object, qtid: CodexThreadId, **kw: object) -> CodexRpcReadResult:
                _ = ep, kw
                return CodexRpcReadResult({
                    "id": str(qtid),
                    "turns": [{"id": "ct1", "status": "completed", "items": [{"type": "userMessage", "content": [{"type": "text", "text": "Promoted"}]}, {"type": "agentMessage", "text": "Codex Target"}]}],
                }, False)

            with mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_agy_daemon", return_value=None), \
                    mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard.discover_codex_daemon", return_value=CodexDaemonEndpoint(sock_path, os.getpid())), \
                    mock.patch("codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc", side_effect=_codex_rpc_ok):
                dry = reconcile_workspace_handoffs(self.paths, self.workspace, dry_run=True)
                self.assertEqual(dry["dry_run"], True)
                self.assertEqual(len(dry["promoted"]), 2)
                self.assertEqual(len(dry["cleared"]), 2)
                self.assertEqual(len(dry["unresolved"]), 1)
                self.assertEqual(dry["total_pending_before"], 5)
                self.assertEqual(dry["total_pending_after"], 5)
                self.assertTrue(absent_receipt.exists())

                real = reconcile_workspace_handoffs(self.paths, self.workspace)
                self.assertEqual(len(real["promoted"]), 2)
                self.assertEqual(len(real["cleared"]), 2)
                self.assertEqual(len(real["unresolved"]), 1)
                self.assertEqual(real["total_pending_after"], 1)
        finally:
            srv.close()

        self.assertFalse(absent_receipt.exists())
        self.assertFalse(absent_stage.exists())
        self.assertFalse(escaped_receipt.exists())
        self.assertTrue((outside_keep / "important.txt").is_file())
        self.assertTrue(partial_receipt.exists())
        self.assertTrue(partial_pbtxt.is_file())
        promoted_codex = json.loads(codex_pend_receipt.read_text(encoding="utf-8"))
        self.assertEqual((promoted_codex["status"], promoted_codex["error"]), ("ready", None))

        # Verify handoff.lock conflict during reconcile raises SyncBusyLockError.
        with (state_dir / "handoff.lock").open("a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(SyncBusyLockError):
                    reconcile_workspace_handoffs(self.paths, self.workspace)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def test_4_enriched_status_auto_pair_help_and_cli_json_errors(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        tid = self._seed_codex(binding, "01970000-0000-7000-8000-000000000401", _turn_pair(HostKind.CODEX, "t1", "2026-11-15T01:00:00Z", "S1", "A1"))
        with self._mock_daemons():
            self._run_cli(["handoff", "-w", str(self.workspace), "--source-id", str(tid), "--chain"])

        tid_pend = self._seed_codex(binding, "01970000-0000-7000-8000-000000000402", _turn_pair(HostKind.CODEX, "tp1", "2026-11-15T01:02:00Z", "Pend", "Turn"))
        state_dir = self.paths.sync_state_dir / "handoffs"
        (state_dir / "pend.json").write_text(json.dumps({
            "workspace": str(self.workspace), "source_host": "codex", "source_id": str(tid_pend),
            "source_digest": "d", "target_host": "antigravity", "target_id": "44444444-4444-4444-8444-444444440402",
            "title": "Pending Item", "status": "pending", "staging_path": str(state_dir / "staging" / "p"), "error": "TimeoutError",
        }), encoding="utf-8")

        rc_st, out_st, err_st = self._run_cli(["status", "-w", str(self.workspace)])
        self.assertEqual((rc_st, err_st), (0, ""))
        status_json = json.loads(out_st)
        self.assertIn("workspace", status_json)
        self.assertIn("sessions", status_json)
        self.assertTrue(status_json["pairing"]["paired"])
        self.assertEqual(len(status_json["handoff_chains"]), 2)
        self.assertEqual(len(status_json["pending_handoffs"]), 1)
        self.assertEqual(status_json["pending_handoffs"][0]["title"], "Pending Item")
        self.assertNotIn("csrfToken", out_st)

        # CLI help labeling check
        out_h = io.StringIO()
        with contextlib.redirect_stdout(out_h), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as h_exc:
            cli_main([*self._base_cli_args(), "--help"])
        self.assertEqual(h_exc.exception.code, 0)
        self.assertIn("[Advanced/Offline]", out_h.getvalue())

        # CLI flag conflict validations
        for bad_flags in (
            ["handoff", "-w", str(self.workspace), "--dry-run"],
            ["handoff", "-w", str(self.workspace), "--reconcile", "--latest"],
            ["handoff", "-w", str(self.workspace), "--auto-pair", "--pair-if-offline", "--latest"],
        ):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as sys_exc:
                self._run_cli(bad_flags)
            self.assertEqual(sys_exc.exception.code, 2)

        # Structured JSON errors (zero raw tracebacks)
        rc_fnf, out_fnf, err_fnf = self._run_cli(["inspect", "-w", str(self.workspace), "--codex-thread-id", "01970000-0000-7000-8000-000000009999"])
        self.assertEqual((rc_fnf, out_fnf), (2, ""))
        self.assertNotIn("Traceback", err_fnf)
        self.assertEqual(json.loads(err_fnf)["error"], "file_not_found")

        rc_inv, out_inv, err_inv = self._run_cli(["inspect", "-w", str(self.workspace), "--codex-thread-id", "../bad"])
        self.assertEqual((rc_inv, out_inv), (2, ""))
        self.assertEqual(json.loads(err_inv)["error"], "invalid_argument")

        rc_pnd, out_pnd, err_pnd = self._run_cli(["handoff", "-w", str(self.workspace), "--source-id", str(tid_pend)])
        self.assertEqual((rc_pnd, out_pnd), (2, ""))
        pnd_json = json.loads(err_pnd)
        self.assertEqual((pnd_json["error"], pnd_json["status"]), ("handoff_pending", "pending"))
        self.assertIn("--reconcile", pnd_json["message"])

        empty_cid = self._seed_agy(binding, "44444444-4444-4444-8444-444444440403", [], title="Empty")
        rc_emp, out_emp, err_emp = self._run_cli(["sync-session", "-w", str(self.workspace), "--cascade-id", str(empty_cid), "--direction", "agy_to_codex"])
        self.assertEqual((rc_emp, out_emp), (2, ""))
        self.assertEqual(json.loads(err_emp)["error"], "empty_session")

        # Subprocess CLI offline test verifying clean stderr single-object JSON and auto-pair fallback
        unpaired_ws = self.root / "unpaired-ws"
        unpaired_ws.mkdir()
        codex_only = self.bridge.registry.ensure_codex_binding(unpaired_ws)
        self._seed_codex(codex_only, "01970000-0000-7000-8000-000000000450", _turn_pair(HostKind.CODEX, "ut1", "2026-11-15T01:00:00Z", "Unpaired", "Turn"))
        proc = subprocess.run(
            [sys.executable, "-m", "codex_agy_sync", *self._base_cli_args(), "handoff", "-w", str(unpaired_ws), "--latest"],
            cwd=str(self.repo_root),
            env={**os.environ, "PYTHONPATH": str(self.repo_root / "src")},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"], "unpaired_workspace")

        with self._mock_daemons(), mock.patch(
            "codex_agy_sync.bridge.registry.register_agy_project_via_rpc",
            side_effect=lambda *a, **kw: self.bridge.registry.ensure_agy_binding(unpaired_ws).agy_project_id,
        ):
            rc_ap, out_ap, err_ap = self._run_cli(["handoff", "-w", str(unpaired_ws), "--latest", "--auto-pair"])
        self.assertEqual((rc_ap, err_ap), (0, ""))
        ap_json = json.loads(out_ap)
        self.assertEqual((ap_json["status"], ap_json["pairing_mode"]), ("ready", "online_rpc"))

    def test_5_workbench_http_api_handoff_convergence(self) -> None:
        ws_unpaired = self.root / "ws-http"
        ws_unpaired.mkdir()
        codex_only = self.bridge.registry.ensure_codex_binding(ws_unpaired)
        t_anc = EventOriginAnchor(HostKind.CODEX, TurnId("ht1"), "2026-11-15T03:00:00Z")
        tid = self._seed_codex(
            codex_only, "01970000-0000-7000-8000-000000000501",
            [
                UserInputEvent(kind="user_input", anchor=t_anc, query_text="HTTP interrupted"),
                ToolCallEvent(kind="tool_call", anchor=t_anc, call_id=ToolCallId("hc1"), tool_name="run_command", tool_summary="Cmd", payload=RunCommandPayload("run_command", "pwd", str(ws_unpaired))),
            ],
            title="HTTP Interrupted Work",
        )

        svc = WorkbenchService(self.paths, ttl_seconds=60.0)
        server = WorkbenchServer(svc, ui_path=self.root / "missing-ui.html", port=0, idle_timeout_sec=120, max_lifetime_sec=300)
        server.start()
        try:
            hdr = {"Host": f"127.0.0.1:{server.port}", "X-Workbench-Token": server.token}
            st_404, d_404 = _http_post(server.port, "/api/send", hdr, {})
            self.assertEqual((st_404, d_404), (404, {"error": "not_found"}))

            st_bad_bool, _ = _http_post(server.port, "/api/handoff", hdr, {"workspace": str(ws_unpaired), "latest": True, "auto_pair": "yes"})
            self.assertEqual(st_bad_bool, 400)

            with self._mock_daemons(), mock.patch(
                "codex_agy_sync.bridge.registry.register_agy_project_via_rpc",
                side_effect=lambda *a, **kw: self.bridge.registry.ensure_agy_binding(ws_unpaired).agy_project_id,
            ), mock.patch(
                "codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc",
                return_value=CodexRpcReadResult(
                    thread={
                        "id": str(tid),
                        "name": "HTTP Interrupted Work",
                        "turns": [{
                            "id": "ht1",
                            "status": "interrupted",
                            "items": [
                                {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "HTTP interrupted"}]},
                                {"type": "commandExecution", "id": "hc1", "command": "pwd", "cwd": str(ws_unpaired), "status": "inProgress"},
                            ],
                        }],
                    },
                    is_active=False,
                ),
            ):
                st_ok, d_ok = _http_post(
                    server.port, "/api/handoff", hdr,
                    {"workspace": str(ws_unpaired), "source_id": str(tid), "auto_pair": True, "include_interrupted": True},
                )
            self.assertEqual((st_ok, d_ok.get("status")), (200, "ready"), msg=str(d_ok))

            with mock.patch("codex_agy_sync.bridge.handoff_resolve.resolve_handoff_request", side_effect=OnlineRpcUnavailableError("rpc down")):
                st_503, d_503 = _http_post(server.port, "/api/handoff", hdr, {"workspace": str(ws_unpaired), "latest": True})
            self.assertEqual((st_503, d_503["error"]), (503, "online_rpc_unavailable"))

            state_dir = self.paths.sync_state_dir / "handoffs"
            (state_dir / "http-absent.json").write_text(json.dumps({
                "workspace": str(ws_unpaired), "source_host": "codex", "source_id": str(tid),
                "source_digest": "dx", "target_host": "antigravity", "target_id": "55555555-5555-4555-8555-555555550502",
                "title": "HTTP Pending", "status": "pending", "staging_path": str(state_dir / "staging" / "hp"), "error": "err",
            }), encoding="utf-8")

            st_rec_bad, _ = _http_post(server.port, "/api/handoff/reconcile", hdr, {"workspace": str(ws_unpaired), "source_id": "../bad"})
            self.assertEqual(st_rec_bad, 400)

            st_rec_dry, d_rec_dry = _http_post(server.port, "/api/handoff/reconcile", hdr, {"workspace": str(ws_unpaired), "dry_run": True})
            self.assertEqual((st_rec_dry, len(d_rec_dry["cleared"]), d_rec_dry["total_pending_after"]), (200, 1, 1))

            st_rec_ok, d_rec_ok = _http_post(server.port, "/api/handoff/reconcile", hdr, {"workspace": str(ws_unpaired)})
            self.assertEqual((st_rec_ok, len(d_rec_ok["cleared"]), d_rec_ok["total_pending_after"]), (200, 1, 0))
            self.assertEqual(len(summarize_workspace_handoffs(self.paths, ws_unpaired)["pending_handoffs"]), 0)
        finally:
            server.stop()

    def test_p0_reconcile_missing_columns_unreadable_state_and_unsafe_promoted_path(self) -> None:
        import sqlite3
        from codex_agy_sync.bridge import handoff_reconcile

        self.bridge.registry.ensure_workspace_binding(self.workspace)
        state_dir = self.paths.sync_state_dir / "handoffs"
        state_dir.mkdir(parents=True, exist_ok=True)
        src_cid = "55555555-5555-4555-8555-555555550901"
        orphan_tid = "01970000-0000-7000-8000-000000000901"

        self.paths.codex_home_dir.mkdir(parents=True, exist_ok=True)
        state_db = self.paths.codex_home_dir / "state_5.sqlite"
        state_db.unlink(missing_ok=True)
        with contextlib.closing(sqlite3.connect(str(state_db))) as conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, title TEXT)")
            conn.execute(
                "INSERT INTO threads (id, cwd, title) VALUES (?, ?, ?)",
                (orphan_tid, str(self.workspace), "Minimal Columns Title"),
            )
            conn.commit()

        match_file = state_dir / "codex-orphan-match.json"
        absent_file = state_dir / "codex-orphan-absent.json"
        match_file.write_text(json.dumps({
            "workspace": str(self.workspace), "source_host": "antigravity", "source_id": src_cid,
            "source_digest": "d1", "target_host": "codex", "target_id": None,
            "title": "Minimal Columns Title", "status": "pending",
            "staging_path": str(state_dir / "staging" / "m1"), "error": "err",
        }), encoding="utf-8")
        absent_file.write_text(json.dumps({
            "workspace": str(self.workspace), "source_host": "antigravity", "source_id": src_cid,
            "source_digest": "d2", "target_host": "codex", "target_id": None,
            "title": "Absent Title", "status": "pending",
            "staging_path": str(state_dir / "staging" / "m2"), "error": "err",
        }), encoding="utf-8")

        report = reconcile_workspace_handoffs(self.paths, self.workspace)
        self.assertEqual([x["reason"] for x in report["unresolved"]], ["codex_candidate_thread_found_without_id"])
        self.assertEqual([x["reason"] for x in report["cleared"]], ["codex_target_never_allocated"])
        self.assertTrue(match_file.is_file())
        self.assertFalse(absent_file.exists())

        state_db.write_bytes(b"corrupt-sqlite-header")
        match_file.unlink(missing_ok=True)
        r_unknown = state_dir / "corrupt-unknown-id.json"
        r_known = state_dir / "corrupt-known-id.json"
        r_unknown.write_text(json.dumps({
            "workspace": str(self.workspace), "source_host": "antigravity", "source_id": src_cid,
            "source_digest": "d3", "target_host": "codex", "target_id": None,
            "title": "Corrupt Unknown", "status": "pending",
            "staging_path": str(state_dir / "staging" / "c1"), "error": "err",
        }), encoding="utf-8")
        r_known.write_text(json.dumps({
            "workspace": str(self.workspace), "source_host": "antigravity", "source_id": src_cid,
            "source_digest": "d4", "target_host": "codex", "target_id": "01970000-0000-7000-8000-000000000902",
            "title": "Corrupt Known", "status": "pending",
            "staging_path": str(state_dir / "staging" / "c2"), "error": "err",
        }), encoding="utf-8")

        corrupt_report = reconcile_workspace_handoffs(self.paths, self.workspace)
        self.assertEqual(corrupt_report["cleared"], [])
        self.assertEqual(
            [x["reason"] for x in corrupt_report["unresolved"]],
            ["codex_state_db_unreadable", "codex_state_db_unreadable"],
        )
        self.assertTrue(r_unknown.is_file() and r_known.is_file())

        outside_receipt = self.root / "escaped-promoted.json"
        rec_obj = HandoffReceipt(
            str(self.workspace), "antigravity", src_cid, "d5", "codex",
            "01970000-0000-7000-8000-000000000903", "Escaped", "pending",
            str(state_dir / "staging" / "esc"), error="err",
        )
        bucket, entry = handoff_reconcile._apply_reconcile_decision(
            state_dir, state_dir / "staging", outside_receipt, rec_obj,
            "promoted", "codex_rpc_verified", dry_run=False,
        )
        self.assertEqual((bucket, entry["reason"]), ("unresolved", "invalid_receipt_path"))
        self.assertFalse(outside_receipt.exists())

    def test_p1_p2_unmocked_missing_target_daemon_corrupt_blob_and_binding_passthrough(self) -> None:
        from codex_agy_sync.bridge import handoff_store

        binding = self.bridge.registry.ensure_workspace_binding(self.workspace)
        tid = self._seed_codex(
            binding, "01970000-0000-7000-8000-000000000911",
            _turn_pair(HostKind.CODEX, "t1", "2026-11-14T00:00:00Z", "Unmocked missing daemon", "Done"),
            title="Missing Target Daemon",
        )

        rc_cli, _, err_cli = self._run_cli(["handoff", "-w", str(self.workspace), "--source-id", str(tid), "--direction", "codex_to_agy"])
        self.assertEqual(rc_cli, 2)
        self.assertEqual(json.loads(err_cli)["error"], "online_rpc_unavailable")

        svc = WorkbenchService(self.paths)
        server = WorkbenchServer(svc, ui_path=self.root / "missing-ui.html", port=0, idle_timeout_sec=120, max_lifetime_sec=300)
        server.start()
        try:
            hdr = {"Host": f"127.0.0.1:{server.port}", "X-Workbench-Token": server.token}
            st_503, d_503 = _http_post(
                server.port, "/api/handoff", hdr,
                {"workspace": str(self.workspace), "source_id": str(tid), "direction": "codex_to_agy"},
            )
            self.assertEqual((st_503, d_503["error"]), (503, "online_rpc_unavailable"))
        finally:
            server.stop()

        cid = self._seed_agy(
            binding, "55555555-5555-4555-8555-555555550912",
            _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-14T00:01:00Z", "Corrupt meta", "Reply"),
            title="Corrupt Meta Ownership",
        )
        conv_db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        import sqlite3
        with contextlib.closing(sqlite3.connect(str(conv_db))) as conn:
            conn.execute("UPDATE trajectory_metadata_blob SET data = ? WHERE id = 'main'", (b"\xff\xff\x80",))
            conn.commit()
        exists, ownership = handoff_store._probe_agy_ownership(self.paths, str(cid), binding)
        self.assertTrue(exists)
        self.assertEqual(ownership, "match")
        self.assertEqual(handoff_store.infer_direction_from_source_id(self.paths, str(cid), binding), SyncDirection.AGY_TO_CODEX)

        with mock.patch("codex_agy_sync.bridge.handoff._require_binding", side_effect=AssertionError("unexpected disk binding lookup")):
            with self._mock_daemons(), mock.patch(
                "codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc",
                return_value=CodexRpcReadResult(thread={"id": str(tid), "name": "Missing Target Daemon", "turns": []}, is_active=False),
            ):
                receipt, mutated = execute_handoff(
                    self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(tid),
                )
        self.assertTrue(mutated)
        self.assertEqual(receipt.status, "ready")


if __name__ == "__main__":
    unittest.main()
