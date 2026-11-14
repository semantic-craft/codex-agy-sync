from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import io
import itertools
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_agy_sync import (
    CascadeId,
    CodexDaemonEndpoint,
    CodexRpcReadResult,
    CodexThreadId,
    HandoffCancelledError,
    HostEnvironmentPaths,
    HostKind,
    SyncBridge,
    SyncDirection,
    TransportMode,
    UnpairedWorkspaceError,
    diagnose_workspace_binding,
    handoff_session,
    ledger_seed_fingerprint,
    list_handoff_candidates,
    load_handoff_receipt,
    resolve_handoff_request,
)
from codex_agy_sync.bridge.handoff import HandoffPendingError
from codex_agy_sync.bridge.registry import _ensure_agy_summaries_db_schema
from codex_agy_sync.bridge.rescue_and_fork import SyncBusyLockError, guarded_pair_workspace
from codex_agy_sync.bridge.rpc import AgyRpcReadResult
from codex_agy_sync.bridge.state_machine import generate_uuid7
from codex_agy_sync.bridge.transport import DefaultHostTransport
from codex_agy_sync.cli import main as cli_main
from codex_agy_sync.codecs import TrajectoryWireCodec, iter_codex_rollout_lines
from codex_agy_sync.ledger import (
    AssistantTextEvent,
    EventOriginAnchor,
    RunCommandPayload,
    SubagentLineage,
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


class HandoffCliUxTest(unittest.TestCase):
    def setUp(self) -> None:
        artifacts = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="handoff-ux-", dir=artifacts)
        self.root = Path(self.temp.name)
        self.paths = HostEnvironmentPaths(
            self.root / "config",
            self.root / "agy",
            self.root / "codex",
            self.root / "state",
        )
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.bridge = SyncBridge(self.paths)
        self.transport = DefaultHostTransport(self.paths)
        self.codec = TrajectoryWireCodec()
        self.cli_bin = Path(__file__).resolve().parents[1] / "bin" / "codex-agy-sync"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _base_cli_args(self) -> list[str]:
        return [
            "--gemini-config-dir", str(self.paths.gemini_config_dir),
            "--agy-data-dir", str(self.paths.agy_data_dir),
            "--codex-home-dir", str(self.paths.codex_home_dir),
            "--sync-state-dir", str(self.paths.sync_state_dir),
        ]

    def _run_cli_inproc(
        self,
        argv: list[str],
        *,
        stdin_text: str = "",
        stdin_tty: bool = False,
        stderr_tty: bool = False,
    ) -> tuple[int, str, str]:
        out_buf = io.StringIO()
        err_buf = io.StringIO()

        class _FakeStream(io.StringIO):
            def __init__(self, content: str, tty: bool) -> None:
                super().__init__(content)
                self._tty = tty

            def isatty(self) -> bool:
                return self._tty

        fake_in = _FakeStream(stdin_text, stdin_tty)
        fake_err = _FakeStream("", stderr_tty)
        with contextlib.redirect_stdout(out_buf), mock.patch("sys.stdin", fake_in), mock.patch("sys.stderr", fake_err):
            rc = cli_main(self._base_cli_args() + argv)
        return rc, out_buf.getvalue(), fake_err.getvalue() or err_buf.getvalue()

    def _activate_agy_rpc(self, endpoint: object, cid: CascadeId, **kwargs: object) -> AgyRpcReadResult:
        _ = endpoint, kwargs
        db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        return AgyRpcReadResult(tuple(self.transport._read_persisted_agy_steps(db)), 1)

    def _fetch_codex_rpc(self, endpoint: object, tid: CodexThreadId, **kwargs: object) -> CodexRpcReadResult:
        _ = endpoint, kwargs
        rp, title = self.transport._find_codex_rollout_and_title(tid)
        return CodexRpcReadResult(
            thread={"id": str(tid), "name": title or "", "path": str(rp), "turns": []},
            is_active=False,
        )

    def _materialize_codex_rpc(
        self,
        endpoint: object,
        rollout_path: Path,
        ws_path: Path,
        codex_project_id: str,
        title: str,
        timeout_s: float = 90.0,
    ) -> str:
        _ = endpoint, ws_path, codex_project_id, timeout_s
        binding = self.bridge.registry.resolve_readonly_binding(self.workspace)
        lines = tuple(iter_codex_rollout_lines(rollout_path))
        ledger = self.codec.decode_codex_rollout_to_ledger(
            binding.workspace_path, binding.workspace_uri, lines, explicit_title_override=title,
        )
        new_tid = CodexThreadId(generate_uuid7())
        clean_locks = dataclasses.replace(self.bridge.guard.probe_locks(None, None), codex_daemon=None)
        self.transport.apply_to_codex(binding, new_tid, ledger.events, ledger, clean_locks, TransportMode.OFFLINE_STORAGE)
        return str(new_tid)

    @contextlib.contextmanager
    def _mock_live_daemons(self):
        fake_codex_ep = CodexDaemonEndpoint(socket_path=self.root / "codex.sock", pid=os.getpid())
        with (
            mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_agy_daemon", return_value=object()),
            mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_codex_daemon", return_value=fake_codex_ep),
            mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=self._activate_agy_rpc),
            mock.patch("codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc", side_effect=self._fetch_codex_rpc),
            mock.patch("codex_agy_sync.bridge.handoff.rpc.import_codex_rollout_via_rpc", create=True, side_effect=self._materialize_codex_rpc),
        ):
            yield

    def _seed_codex_thread(
        self,
        binding,
        tid: str,
        events: list,
        *,
        title: str = "Codex Thread",
        lineage: SubagentLineage | None = None,
        parent_tid: str | None = None,
    ) -> CodexThreadId:
        tid_obj = CodexThreadId(tid)
        ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, events, explicit_title_override=title)
        locks = self.bridge.guard.probe_locks(None, tid_obj)
        self.transport.apply_to_codex(
            binding, tid_obj, ledger.events, ledger, locks, TransportMode.OFFLINE_STORAGE,
            parent_thread_id=CodexThreadId(parent_tid) if parent_tid else None, lineage=lineage,
        )
        return tid_obj

    def _seed_agy_cascade(
        self,
        binding,
        cid: str,
        events: list,
        *,
        title: str = "AGY Cascade",
        lineage: SubagentLineage | None = None,
        parent_cid: str | None = None,
    ) -> CascadeId:
        cid_obj = CascadeId(cid)
        ledger = TrajectoryLedger.from_events(
            binding.workspace_path, binding.workspace_uri, events,
            explicit_title_override=title, recompute_agy_step_indices=True,
        )
        locks = self.bridge.guard.probe_locks(cid_obj, None)
        self.transport.apply_to_agy(
            binding, cid_obj, ledger.events, ledger, locks, TransportMode.OFFLINE_STORAGE,
            parent_cascade_id=CascadeId(parent_cid) if parent_cid else None, lineage=lineage,
        )
        return cid_obj

    def test_1_unpaired_diagnosis_and_cli_json_error_leave_no_handoffs_dir(self) -> None:
        codex_only_binding = self.bridge.registry.ensure_codex_binding(self.workspace)
        tid = self._seed_codex_thread(
            codex_only_binding, "01960000-0000-7000-8000-000000000001",
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T01:00:00Z", "Hi", "Hello"),
        )
        with mock.patch("codex_agy_sync.bridge.paths_and_locks.PresenceWalGuard._discover_agy_daemon", return_value=object()):
            diag = diagnose_workspace_binding(self.paths, self.workspace)
            self.assertFalse(diag.is_paired())
            self.assertTrue(diag.codex_registered and not diag.agy_registered)
            self.assertTrue(diag.agy_daemon_active and not diag.codex_daemon_active)
            joined_steps = "\n".join(diag.remediation_steps)
            self.assertIn("Antigravity Desktop", joined_steps)
            self.assertIn("--pair-if-offline", joined_steps)
            self.assertIn("codex-agy-sync pair -w", joined_steps)

            with self.assertRaises(UnpairedWorkspaceError) as exc_ctx:
                handoff_session(self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(tid))
            self.assertIn("pair", str(exc_ctx.exception))
            self.assertEqual(exc_ctx.exception.to_error_payload()["error"], "unpaired_workspace")

            rc, out, err = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--source-id", str(tid)])
            self.assertEqual(rc, 2)
            self.assertEqual(out, "")
            err_json = json.loads(err)
            self.assertEqual(err_json["error"], "unpaired_workspace")
            self.assertFalse(err_json["diagnosis"]["agy_registered"])
            self.assertTrue(err_json["diagnosis"]["codex_registered"])
        self.assertFalse((self.paths.sync_state_dir / "handoffs").exists())

    def test_2_pair_if_offline_idempotency_and_busy_lock_errors(self) -> None:
        codex_only_binding = self.bridge.registry.ensure_codex_binding(self.workspace)
        tid = self._seed_codex_thread(
            codex_only_binding, "01960000-0000-7000-8000-000000000002",
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T01:10:00Z", "Pair offline", "Done"),
        )
        state_db = self.paths.codex_home_dir / "state_5.sqlite"
        (self.paths.codex_home_dir / "daemon.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        (self.paths.codex_home_dir / "state_5.sqlite-wal").write_bytes(b"codex-active-wal")
        self.assertTrue(state_db.is_file())

        with (
            mock.patch(
                "codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_agy_daemon",
                side_effect=itertools.chain([None, None], itertools.repeat(object())),
            ),
            mock.patch("codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc", side_effect=self._fetch_codex_rpc),
            mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=self._activate_agy_rpc),
        ):
            rc, out, err = self._run_cli_inproc([
                "handoff", "-w", str(self.workspace), "--source-id", str(tid), "--pair-if-offline",
            ])
        self.assertEqual(rc, 0, err)
        receipt = json.loads(out)
        self.assertEqual(receipt["status"], "ready")
        self.assertTrue(diagnose_workspace_binding(self.paths, self.workspace).is_paired())

        backups_before = list((self.paths.sync_state_dir / "backups").glob("*")) if (self.paths.sync_state_dir / "backups").exists() else []
        with (
            mock.patch.object(self.bridge.guard, "acquire_offline_write_locks") as lock_spy,
            mock.patch.object(self.bridge.guard, "create_offline_backup") as backup_spy,
        ):
            ro_res = guarded_pair_workspace(self.paths, self.bridge.guard, self.bridge.registry, self.workspace)
        self.assertEqual(ro_res.workspace_path, self.workspace.resolve())
        lock_spy.assert_not_called()
        backup_spy.assert_not_called()
        backups_after = list((self.paths.sync_state_dir / "backups").glob("*")) if (self.paths.sync_state_dir / "backups").exists() else []
        self.assertEqual(backups_before, backups_after)

        busy_ws = self.root / "busy-ws"
        busy_ws.mkdir()
        rc_pair, out_pair, err_pair = self._run_cli_inproc(["pair", "-w", str(busy_ws)])
        self.assertEqual(rc_pair, 2)
        self.assertEqual(out_pair, "")
        self.assertEqual(json.loads(err_pair)["error"], "sync_busy_lock")

        rc_busy, out_busy, err_busy = self._run_cli_inproc([
            "handoff", "-w", str(busy_ws), "--source-id", str(tid), "--pair-if-offline",
        ])
        self.assertEqual(rc_busy, 2)
        self.assertEqual(out_busy, "")
        busy_json = json.loads(err_busy)
        self.assertEqual(busy_json["error"], "sync_busy_lock")
        self.assertTrue(busy_json["remediation"])

    def test_3_source_id_direction_inference_ambiguity_missing_and_mutual_exclusion(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        codex_tid = self._seed_codex_thread(
            binding, "01960000-0000-7000-8000-000000000003",
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T02:00:00Z", "From Codex", "Ack Codex"),
            title="Codex Solo",
        )
        agy_cid = self._seed_agy_cascade(
            binding, "11111111-1111-4111-8111-111111111003",
            _turn_pair(HostKind.ANTIGRAVITY, "turn-1", "2026-11-14T02:05:00Z", "From AGY", "Ack AGY"),
            title="AGY Solo",
        )
        rollout_only_id = "01960000-0000-7000-8000-000000000333"
        rollout_ledger = TrajectoryLedger.from_events(
            binding.workspace_path, binding.workspace_uri,
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T02:02:00Z", "Rollout only", "Ack rollout"),
        )
        rollout_rows = self.codec.encode_ledger_to_codex_rollout(
            rollout_ledger, session_id=rollout_only_id, cwd=str(binding.workspace_path),
        )
        rollout_file = self.paths.codex_home_dir / "sessions" / "2026" / "11" / "14" / f"rollout-2026-11-14T02-02-00-{rollout_only_id}.jsonl"
        rollout_file.parent.mkdir(parents=True, exist_ok=True)
        rollout_file.write_text("\n".join(r.raw_json for r in rollout_rows) + "\n", encoding="utf-8")

        res_c = resolve_handoff_request(self.paths, self.workspace, source_id=str(codex_tid))
        res_a = resolve_handoff_request(self.paths, self.workspace, source_id=str(agy_cid))
        res_r = resolve_handoff_request(self.paths, self.workspace, source_id=rollout_only_id)
        self.assertEqual(res_c.direction, SyncDirection.CODEX_TO_AGY)
        self.assertEqual(res_a.direction, SyncDirection.AGY_TO_CODEX)
        self.assertEqual(res_r.direction, SyncDirection.CODEX_TO_AGY)

        shared_id = "22222222-2222-4222-8222-222222222003"
        self._seed_codex_thread(binding, shared_id, _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T02:06:00Z", "A", "B"))
        self._seed_agy_cascade(binding, shared_id, _turn_pair(HostKind.ANTIGRAVITY, "turn-1", "2026-11-14T02:06:00Z", "A", "B"))
        with self.assertRaisesRegex(ValueError, "--direction"):
            resolve_handoff_request(self.paths, self.workspace, source_id=shared_id)
        with self.assertRaisesRegex(ValueError, "not found"):
            resolve_handoff_request(self.paths, self.workspace, source_id="99999999-9999-4999-8999-999999999999")

        with self._mock_live_daemons():
            rc, out, _ = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--source-id", str(codex_tid)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["target_host"], "antigravity")

        for combo in (
            ["handoff", "-w", str(self.workspace), "--chain", "--latest"],
            ["handoff", "-w", str(self.workspace), "--chain", "--pick"],
            ["handoff", "-w", str(self.workspace), "--source-id", str(codex_tid), "--latest"],
            ["handoff", "-w", str(self.workspace), "--source-id", str(codex_tid), "--pick"],
            ["handoff", "-w", str(self.workspace), "--latest", "--pick"],
        ):
            sub_res = subprocess.run([sys.executable, str(self.cli_bin)] + self._base_cli_args() + combo, capture_output=True, text=True)
            self.assertEqual(sub_res.returncode, 2, f"Expected exit 2 for {combo}: {sub_res.stderr}")

    def test_4_latest_skips_untouched_baton_target_and_returns_noop_then_advances_when_touched(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        src_tid = self._seed_codex_thread(
            binding, "01960000-0000-7000-8000-000000000004",
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T01:00:00Z", "Primary prompt", "Primary answer"),
            title="Primary Thread",
        )
        self._seed_codex_thread(
            binding, "01960000-0000-7000-8000-000000000994",
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T05:00:00Z", "Subtask", "Subanswer"),
            title="Spawned Subagent",
            lineage=SubagentLineage(parent_codex_thread_id=src_tid, nesting_depth=1, agent_role="worker"),
            parent_tid=str(src_tid),
        )

        with self._mock_live_daemons():
            r1 = handoff_session(self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(src_tid))
        target_cid = r1.target_id
        assert target_cid is not None

        cands = list_handoff_candidates(self.paths, binding)
        self.assertNotIn("01960000-0000-7000-8000-000000000994", [c.session_id for c in cands])
        target_cand = next(c for c in cands if c.session_id == target_cid)
        src_cand = next(c for c in cands if c.session_id == str(src_tid))
        self.assertTrue(target_cand.is_untouched_baton_target)
        self.assertEqual(target_cand.title, "Primary Thread")
        self.assertTrue(src_cand.already_handed_ready and src_cand.handed_target_id == target_cid)

        with self._mock_live_daemons():
            rc_noop, out_noop, _ = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--latest"])
        self.assertEqual(rc_noop, 0)
        noop_json = json.loads(out_noop)
        self.assertEqual(noop_json["status"], "no_op")
        self.assertEqual(noop_json["target_id"], target_cid)

        t_ledger = self.transport.read_agy_ledger(binding, CascadeId(target_cid), self.bridge.guard.probe_locks(CascadeId(target_cid), None), TransportMode.OFFLINE_STORAGE)
        new_events = list(t_ledger.events) + _turn_pair(HostKind.ANTIGRAVITY, "turn-2", "2026-11-14T06:00:00Z", "Follow-up", "Reply 2")
        upd_ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, new_events, explicit_title_override="Primary Thread", recompute_agy_step_indices=True)
        self.transport.apply_to_agy(binding, CascadeId(target_cid), upd_ledger.events, upd_ledger, self.bridge.guard.probe_locks(CascadeId(target_cid), None), TransportMode.OFFLINE_STORAGE)

        cands_after = list_handoff_candidates(self.paths, binding)
        self.assertFalse(next(c for c in cands_after if c.session_id == target_cid).is_untouched_baton_target)
        with self._mock_live_daemons():
            rc_hop, out_hop, _ = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--latest"])
        self.assertEqual(rc_hop, 0)
        hop_json = json.loads(out_hop)
        self.assertEqual(hop_json["status"], "ready")
        self.assertEqual(hop_json["source_id"], target_cid)
        self.assertEqual(hop_json["target_host"], "codex")

    def test_5_chain_lifecycle_legacy_pending_and_cycle_safety(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        src_tid = self._seed_codex_thread(
            binding, "01960000-0000-7000-8000-000000000005",
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T01:00:00Z", "Start chain", "Hop 0 ready"),
            title="Chain Session",
        )

        with self._mock_live_daemons():
            rc0, out0, _ = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--chain"])
        self.assertEqual(rc0, 0)
        r0 = json.loads(out0)
        self.assertEqual(r0["status"], "ready")
        self.assertIsNotNone(r0["chain_id"])
        self.assertEqual(r0["chain_depth"], 0)
        self.assertEqual(r0["target_seed_turn_count"], 1)

        with self._mock_live_daemons():
            rc_idem, out_idem, _ = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--chain"])
        self.assertEqual(rc_idem, 0)
        r_idem = json.loads(out_idem)
        self.assertEqual(r_idem["status"], "no_op")
        self.assertEqual(r_idem["target_id"], r0["target_id"])

        t0_cid = CascadeId(r0["target_id"])
        t0_ledger = self.transport.read_agy_ledger(binding, t0_cid, self.bridge.guard.probe_locks(t0_cid, None), TransportMode.OFFLINE_STORAGE)
        t0_upd = TrajectoryLedger.from_events(
            binding.workspace_path, binding.workspace_uri,
            list(t0_ledger.events) + _turn_pair(HostKind.ANTIGRAVITY, "turn-2", "2026-11-14T02:00:00Z", "Hop 1 user", "Hop 1 assistant"),
            explicit_title_override="Chain Session", recompute_agy_step_indices=True,
        )
        self.transport.apply_to_agy(binding, t0_cid, t0_upd.events, t0_upd, self.bridge.guard.probe_locks(t0_cid, None), TransportMode.OFFLINE_STORAGE)

        with self.assertRaisesRegex(ValueError, "contradicts --chain"):
            resolve_handoff_request(self.paths, self.workspace, chain=True, direction=SyncDirection.CODEX_TO_AGY)

        with self._mock_live_daemons():
            rc1, out1, err1 = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--chain"])
        self.assertEqual(rc1, 0, err1)
        r1 = json.loads(out1)
        self.assertEqual((r1["status"], r1["chain_id"], r1["chain_depth"], r1["source_id"], r1["target_host"]), ("ready", r0["chain_id"], 1, str(t0_cid), "codex"))
        self.assertEqual(r1["chained_from_source_id"], str(src_tid))

        t0_upd2 = TrajectoryLedger.from_events(
            binding.workspace_path, binding.workspace_uri,
            list(t0_upd.events) + _turn_pair(HostKind.ANTIGRAVITY, "turn-3", "2026-11-14T03:00:00Z", "Branch user", "Branch assistant"),
            explicit_title_override="Chain Session", recompute_agy_step_indices=True,
        )
        self.transport.apply_to_agy(binding, t0_cid, t0_upd2.events, t0_upd2, self.bridge.guard.probe_locks(t0_cid, None), TransportMode.OFFLINE_STORAGE)
        with self._mock_live_daemons():
            rc2, out2, _ = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--chain"])
        self.assertEqual(rc2, 0)
        r2 = json.loads(out2)
        self.assertEqual((r2["status"], r2["chain_id"], r2["chain_depth"], r2["source_id"]), ("ready", r0["chain_id"], 2, str(t0_cid)))
        self.assertEqual(r2["chained_from_source_id"], str(src_tid))

        state_dir = self.paths.sync_state_dir / "handoffs"
        pending_file = state_dir / "pending-block.json"
        pending_file.write_text(json.dumps({
            "workspace": str(binding.workspace_path), "source_host": "codex",
            "source_id": str(r2["target_id"]), "source_digest": "deadbeef",
            "target_host": "antigravity", "target_id": "pending-cid-1",
            "title": "Blocked", "status": "pending", "staging_path": str(state_dir / "staging" / "pending"),
            "error": "Timeout",
        }), encoding="utf-8")
        with self.assertRaises(HandoffPendingError):
            resolve_handoff_request(self.paths, self.workspace, chain=True)
        rc_p, out_p, err_p = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--chain"])
        self.assertEqual(rc_p, 2)
        self.assertEqual(out_p, "")
        self.assertEqual(json.loads(err_p)["status"], "pending")
        pending_file.unlink()

        for f in state_dir.glob("*.json"):
            f.unlink()
        legacy_cid = CascadeId("33333333-3333-4333-8333-333333333005")
        self._seed_agy_cascade(
            binding, str(legacy_cid),
            _turn_pair(HostKind.ANTIGRAVITY, "turn-1", "2026-11-14T01:00:00Z", "Start chain", "Hop 0 ready"),
            title="Legacy Target",
        )
        legacy_path = state_dir / "legacy-ready.json"
        legacy_path.write_text(json.dumps({
            "workspace": str(binding.workspace_path), "source_host": "codex", "source_id": str(src_tid),
            "source_digest": r0["source_digest"], "target_host": "antigravity", "target_id": str(legacy_cid),
            "title": "Legacy", "status": "ready", "staging_path": str(state_dir / "staging" / "legacy"),
        }), encoding="utf-8")
        loaded_legacy = load_handoff_receipt(legacy_path)
        self.assertEqual((loaded_legacy.chain_depth, loaded_legacy.created_at_iso), (0, ""))
        legacy_noop = resolve_handoff_request(self.paths, self.workspace, chain=True)
        self.assertIsNotNone(legacy_noop.already_ready_receipt)
        self.assertEqual(legacy_noop.already_ready_receipt.status, "no_op")
        leg_l = self.transport.read_agy_ledger(binding, legacy_cid, self.bridge.guard.probe_locks(legacy_cid, None), TransportMode.OFFLINE_STORAGE)
        leg_upd = TrajectoryLedger.from_events(
            binding.workspace_path, binding.workspace_uri,
            list(leg_l.events) + _turn_pair(HostKind.ANTIGRAVITY, "turn-2", "2026-11-14T01:30:00Z", "Legacy turn 2", "Legacy reply 2"),
            explicit_title_override="Legacy Target", recompute_agy_step_indices=True,
        )
        self.transport.apply_to_agy(binding, legacy_cid, leg_upd.events, leg_upd, self.bridge.guard.probe_locks(legacy_cid, None), TransportMode.OFFLINE_STORAGE)
        legacy_next = resolve_handoff_request(self.paths, self.workspace, chain=True)
        self.assertIsNone(legacy_next.already_ready_receipt)
        self.assertEqual((legacy_next.source_id, legacy_next.direction, legacy_next.chain_depth), (str(legacy_cid), SyncDirection.AGY_TO_CODEX, 1))

        with self._mock_live_daemons():
            manual_hop = handoff_session(self.paths, self.workspace, direction=SyncDirection.AGY_TO_CODEX, source_id=str(legacy_cid))
        self.assertIsNotNone(manual_hop.chain_id)
        self.assertEqual((manual_hop.chained_from_source_id, manual_hop.chain_depth), (str(src_tid), 1))

        for f in state_dir.glob("*.json"):
            f.unlink()
        (state_dir / "cycle-ab.json").write_text(json.dumps({
            "workspace": str(binding.workspace_path), "source_host": "codex", "source_id": str(src_tid),
            "source_digest": r0["source_digest"], "target_host": "antigravity", "target_id": str(t0_cid),
            "title": "Cycle A->B", "status": "ready", "staging_path": str(state_dir / "staging" / "ab"),
        }), encoding="utf-8")
        t0_wm = self.transport.read_agy_ledger(binding, t0_cid, self.bridge.guard.probe_locks(t0_cid, None), TransportMode.OFFLINE_STORAGE).latest_watermark()
        assert t0_wm is not None
        (state_dir / "cycle-ba.json").write_text(json.dumps({
            "workspace": str(binding.workspace_path), "source_host": "antigravity", "source_id": str(t0_cid),
            "source_digest": str(t0_wm.boundary_digest), "target_host": "codex", "target_id": str(src_tid),
            "title": "Cycle B->A", "status": "ready", "staging_path": str(state_dir / "staging" / "ba"),
        }), encoding="utf-8")
        cycle_res = resolve_handoff_request(self.paths, self.workspace, chain=True, source_id=str(src_tid))
        self.assertIsNotNone(cycle_res.already_ready_receipt)
        self.assertEqual(cycle_res.already_ready_receipt.status, "no_op")

    def test_6_pick_tty_stderr_only_and_non_tty_errors(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        tid = self._seed_codex_thread(
            binding, "01960000-0000-7000-8000-000000000006",
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T04:00:00Z", "Pick me", "Picked"),
            title="Pick Candidate",
        )
        with self.assertRaises(ValueError) as err_ctx:
            resolve_handoff_request(self.paths, self.workspace, stdin_isatty=False, stderr_isatty=False)
        msg = str(err_ctx.exception)
        for flag in ("--latest", "--chain", "--source-id", "--pick"):
            self.assertIn(flag, msg)

        rc_nontty, out_nontty, err_nontty = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--pick"])
        self.assertEqual(rc_nontty, 2)
        self.assertEqual(out_nontty, "")
        self.assertEqual(json.loads(err_nontty)["error"], "invalid_handoff_request")

        res_inj = resolve_handoff_request(self.paths, self.workspace, pick=True, interactive_chooser=lambda cs: cs[0])
        self.assertEqual((res_inj.source_id, res_inj.selection_mode), (str(tid), "pick"))

        with self._mock_live_daemons():
            rc, out, err = self._run_cli_inproc(
                ["handoff", "-w", str(self.workspace), "--pick"],
                stdin_text="1\n", stdin_tty=True, stderr_tty=True,
            )
        self.assertEqual(rc, 0)
        self.assertIn("[1] codex_to_agy | Pick Candidate", err)
        parsed_out = json.loads(out)
        self.assertEqual((parsed_out["status"], parsed_out["source_id"]), ("ready", str(tid)))

        with mock.patch("sys.stdin", io.StringIO("q\n")):
            with self.assertRaises(HandoffCancelledError):
                resolve_handoff_request(self.paths, self.workspace, stdin_isatty=True, stderr_isatty=True)

        rc_q, out_q, err_q = self._run_cli_inproc(["handoff", "-w", str(self.workspace)], stdin_text="q\n", stdin_tty=True, stderr_tty=True)
        self.assertEqual(rc_q, 2)
        self.assertEqual(out_q, "")
        self.assertEqual(json.loads(err_q[err_q.index("{"):])["error"], "handoff_cancelled")

    def test_7_cross_workspace_ownership_and_direction_disambiguation(self) -> None:
        self.bridge.pair_workspace(self.workspace)
        ws_b = self.root / "workspace-b"
        ws_b.mkdir()
        bind_b = self.bridge.pair_workspace(ws_b)
        foreign_codex = self._seed_codex_thread(
            bind_b, "01960000-0000-7000-8000-000000000701",
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T01:00:00Z", "B codex", "B reply"),
        )
        foreign_agy = self._seed_agy_cascade(
            bind_b, "44444444-4444-4444-8444-444444444702",
            _turn_pair(HostKind.ANTIGRAVITY, "turn-1", "2026-11-14T01:05:00Z", "B agy", "B reply"),
        )
        rollout_only_b = "01960000-0000-7000-8000-000000000703"
        r_ledger = TrajectoryLedger.from_events(
            bind_b.workspace_path, bind_b.workspace_uri,
            _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T01:06:00Z", "B rollout", "Ack"),
        )
        r_rows = self.codec.encode_ledger_to_codex_rollout(r_ledger, session_id=rollout_only_b, cwd=str(bind_b.workspace_path))
        r_file = self.paths.codex_home_dir / "sessions" / "2026" / "11" / "14" / f"rollout-2026-11-14T01-06-00-{rollout_only_b}.jsonl"
        r_file.parent.mkdir(parents=True, exist_ok=True)
        r_file.write_text("\n".join(r.raw_json for r in r_rows) + "\n", encoding="utf-8")

        for sid, explicit_dir in (
            (str(foreign_codex), SyncDirection.CODEX_TO_AGY),
            (rollout_only_b, SyncDirection.CODEX_TO_AGY),
            (str(foreign_agy), SyncDirection.AGY_TO_CODEX),
        ):
            with self.assertRaisesRegex(ValueError, "does not belong to workspace"):
                resolve_handoff_request(self.paths, self.workspace, source_id=sid)
            with self.assertRaisesRegex(ValueError, "does not belong to workspace"):
                handoff_session(self.paths, self.workspace, direction=explicit_dir, source_id=sid)
            rc, out, err = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--source-id", sid])
            self.assertEqual((rc, out, json.loads(err)["error"]), (2, "", "invalid_handoff_request"))

        shared_cross_ws = "55555555-5555-4555-8555-555555555704"
        bind_a = self.bridge.registry.resolve_readonly_binding(self.workspace)
        self._seed_codex_thread(bind_b, shared_cross_ws, _turn_pair(HostKind.CODEX, "turn-1", "2026-11-14T01:10:00Z", "B", "R"))
        self._seed_agy_cascade(bind_a, shared_cross_ws, _turn_pair(HostKind.ANTIGRAVITY, "turn-1", "2026-11-14T01:10:00Z", "A", "R"))
        resolved = resolve_handoff_request(self.paths, self.workspace, source_id=shared_cross_ws)
        self.assertEqual(resolved.direction, SyncDirection.AGY_TO_CODEX)

    def test_8_chain_dag_recency_error_propagation_and_legacy_slice(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        state_dir = self.paths.sync_state_dir / "handoffs"
        state_dir.mkdir(parents=True, exist_ok=True)
        s0 = self._seed_codex_thread(binding, "01960000-0000-7000-8000-000000000801", _turn_pair(HostKind.CODEX, "t1", "2026-11-14T01:00:00Z", "Q0", "A0"))
        t0 = self._seed_agy_cascade(binding, "66666666-6666-4666-8666-666666666801", _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-14T01:01:00Z", "Q0", "A0"))
        t1 = self._seed_codex_thread(binding, "01960000-0000-7000-8000-000000000802", _turn_pair(HostKind.CODEX, "t1", "2026-11-14T01:02:00Z", "Q0", "A0"))
        t2 = self._seed_codex_thread(binding, "01960000-0000-7000-8000-000000000803", _turn_pair(HostKind.CODEX, "t1", "2026-11-14T01:03:00Z", "Q0", "A0"))
        t3 = self._seed_agy_cascade(binding, "66666666-6666-4666-8666-666666666803", _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-14T01:04:00Z", "Q0", "A0"))

        def _write_rec(name: str, sh: str, sid: str, th: str, tid: str, depth: int, iso: str, status: str = "ready", **extra: object) -> Path:
            if sh == "codex":
                seed_l = self.transport.read_codex_ledger(binding, CodexThreadId(sid), self.bridge.guard.probe_locks(None, CodexThreadId(sid)), TransportMode.OFFLINE_STORAGE)
            else:
                seed_l = self.transport.read_agy_ledger(binding, CascadeId(sid), self.bridge.guard.probe_locks(CascadeId(sid), None), TransportMode.OFFLINE_STORAGE)
            turns, dig = ledger_seed_fingerprint(seed_l)
            payload = {
                "workspace": str(binding.workspace_path), "source_host": sh, "source_id": sid,
                "source_digest": str(seed_l.latest_watermark().boundary_digest),
                "target_host": th, "target_id": tid, "title": "DAG", "status": status,
                "staging_path": str(state_dir / "staging" / name), "created_at_iso": iso,
                "chain_id": "chain-dag", "chain_depth": depth,
                "target_seed_turn_count": turns, "target_seed_text_digest": dig, **extra,
            }
            p = state_dir / f"{name}.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            return p

        _write_rec("r0", "codex", str(s0), "antigravity", str(t0), 0, "2026-11-14T01:00:00Z")
        _write_rec("r1", "antigravity", str(t0), "codex", str(t1), 1, "2026-11-14T01:02:00Z")
        _write_rec("r2", "antigravity", str(t0), "codex", str(t2), 1, "2026-11-14T01:03:00Z")
        _write_rec("r3", "codex", str(t2), "antigravity", str(t3), 2, "2026-11-14T01:04:00Z")
        dag_tip = resolve_handoff_request(self.paths, self.workspace, chain=True, source_id=str(s0))
        self.assertEqual((dag_tip.already_ready_receipt.target_id, dag_tip.chain_depth), (str(t3), 2))
        with self.assertRaisesRegex(ValueError, "contradicts --chain"):
            resolve_handoff_request(self.paths, self.workspace, chain=True, direction=SyncDirection.AGY_TO_CODEX, source_id=str(s0))

        _write_rec("r1_pending", "antigravity", str(t3), "codex", "pending-tgt-803", 3, "2026-11-14T01:05:00Z", status="pending")
        with self.assertRaises(HandoffPendingError):
            resolve_handoff_request(self.paths, self.workspace, chain=True, source_id=str(s0))
        s_new = self._seed_codex_thread(binding, "01960000-0000-7000-8000-000000000804", _turn_pair(HostKind.CODEX, "t1", "2026-11-14T02:00:00Z", "Q0", "A0"))
        t_new = self._seed_agy_cascade(binding, "66666666-6666-4666-8666-666666666804", _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-14T02:01:00Z", "Q0", "A0"))
        _write_rec("r_new", "codex", str(s_new), "antigravity", str(t_new), 0, "2026-11-14T02:01:00Z", chain_id="chain-new")
        chosen_new = resolve_handoff_request(self.paths, self.workspace, chain=True)
        self.assertEqual((chosen_new.already_ready_receipt.status, chosen_new.already_ready_receipt.target_id), ("no_op", str(t_new)))

        with mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard._discover_agy_daemon", return_value=object()), \
             mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", return_value=AgyRpcReadResult((), 2)):
            with self.assertRaisesRegex(ValueError, "active"):
                resolve_handoff_request(self.paths, self.workspace, chain=True)
        (self.paths.agy_data_dir / "conversations" / f"{t_new}.db").unlink()
        missing_fallback = resolve_handoff_request(self.paths, self.workspace, chain=True)
        self.assertEqual(missing_fallback.already_ready_receipt.status, "no_op")

        for f in state_dir.glob("*.json"):
            f.unlink()
        _write_rec("p_only", "codex", str(s_new), "antigravity", "pending-only-tgt", 0, "2026-11-14T02:05:00Z", status="pending")
        with self.assertRaises(HandoffPendingError):
            resolve_handoff_request(self.paths, self.workspace, chain=True, source_id="pending-only-tgt")
        for f in state_dir.glob("*.json"):
            f.unlink()

        s_leg = self._seed_codex_thread(binding, "01960000-0000-7000-8000-000000000805", _turn_pair(HostKind.CODEX, "t1", "2026-11-14T03:00:00Z", "Q1", "A1"))
        t_leg = self._seed_agy_cascade(binding, "66666666-6666-4666-8666-666666666805", _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-14T03:00:00Z", "Q1", "A1"))
        wm_1 = self.transport.read_codex_ledger(binding, s_leg, self.bridge.guard.probe_locks(None, s_leg), TransportMode.OFFLINE_STORAGE).latest_watermark()
        self._seed_codex_thread(
            binding, str(s_leg),
            _turn_pair(HostKind.CODEX, "t1", "2026-11-14T03:00:00Z", "Q1", "A1") + _turn_pair(HostKind.CODEX, "t2", "2026-11-14T03:05:00Z", "Q2", "A2"),
        )
        (state_dir / "legacy-src-adv.json").write_text(json.dumps({
            "workspace": str(binding.workspace_path), "source_host": "codex", "source_id": str(s_leg),
            "source_digest": str(wm_1.boundary_digest), "target_host": "antigravity", "target_id": str(t_leg),
            "title": "LegacySrcAdv", "status": "ready", "staging_path": str(state_dir / "staging" / "leg"),
        }), encoding="utf-8")
        leg_res = resolve_handoff_request(self.paths, self.workspace, chain=True)
        self.assertEqual((leg_res.source_id, leg_res.direction, leg_res.chain_depth), (str(s_leg), SyncDirection.CODEX_TO_AGY, 1))
        (state_dir / "legacy-src-adv.json").write_text(json.dumps({
            "workspace": str(binding.workspace_path), "source_host": "codex", "source_id": str(s_leg),
            "source_digest": "missing-watermark-digest", "target_host": "antigravity", "target_id": str(t_leg),
            "title": "LegacyMissing", "status": "ready", "staging_path": str(state_dir / "staging" / "leg2"),
        }), encoding="utf-8")
        self.assertIsNone(resolve_handoff_request(self.paths, self.workspace, chain=True).already_ready_receipt)

    def test_9_latest_ghost_incomplete_pending_corrupt_and_norm_uri(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        s_old = self._seed_codex_thread(binding, "01960000-0000-7000-8000-000000000901", _turn_pair(HostKind.CODEX, "t1", "2026-11-14T01:00:00Z", "Old", "Done"))
        with self._mock_live_daemons():
            r_old = handoff_session(self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(s_old))
        state_dir = self.paths.sync_state_dir / "handoffs"
        rec_file = next(state_dir.glob("*.json"))
        rec_obj = json.loads(rec_file.read_text(encoding="utf-8"))
        rec_obj["workspace"] = str(binding.workspace_uri)
        rec_file.write_text(json.dumps(rec_obj), encoding="utf-8")
        (state_dir / "corrupt.json").write_text("{truncated", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            load_handoff_receipt(state_dir / "corrupt.json")

        ghost_cid = "77777777-7777-4777-8777-777777777901"
        sum_db = self.paths.agy_data_dir / "conversation_summaries.db"
        with contextlib.closing(sqlite3.connect(sum_db)) as conn:
            _ensure_agy_summaries_db_schema(conn)
            conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, title, step_count, last_modified_time, workspace_uris, project_id, last_user_input_time) "
                "VALUES (?, ?, 4, '2026-11-14T05:00:00Z', ?, ?, '2026-11-14T05:00:00Z')",
                (ghost_cid, "Ghost", json.dumps([str(binding.workspace_uri)]), str(binding.agy_project_id)),
            )
            conn.commit()
        cands = list_handoff_candidates(self.paths, binding)
        self.assertNotIn(ghost_cid, [c.session_id for c in cands])

        s_inc = "01960000-0000-7000-8000-000000000902"
        anchor = EventOriginAnchor(HostKind.CODEX, TurnId("t-inc"), "2026-11-14T04:00:00Z")
        inc_events = [
            UserInputEvent(kind="user_input", anchor=anchor, query_text="Run command"),
            ToolCallEvent(kind="tool_call", anchor=anchor, call_id=ToolCallId("c-inc"), tool_name="exec_command", tool_summary="Exec", payload=RunCommandPayload("run_command", "echo hi", str(self.workspace))),
        ]
        inc_ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, inc_events, explicit_title_override="Incomplete")
        self.transport.apply_to_codex(binding, CodexThreadId(s_inc), inc_ledger.events, inc_ledger, self.bridge.guard.probe_locks(None, CodexThreadId(s_inc)), TransportMode.OFFLINE_STORAGE)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            resolve_handoff_request(self.paths, self.workspace, latest=True)
        rc_inc, out_inc, err_inc = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--latest"])
        self.assertEqual((rc_inc, out_inc, json.loads(err_inc)["error"]), (2, "", "invalid_handoff_request"))

        (state_dir / "pending-inc.json").write_text(json.dumps({
            "workspace": str(binding.workspace_uri), "source_host": "antigravity", "source_id": str(r_old.target_id),
            "source_digest": "d", "target_host": "codex", "target_id": s_inc,
            "title": "Pending", "status": "pending", "staging_path": str(state_dir / "staging" / "p"),
        }), encoding="utf-8")
        with self.assertRaises(HandoffPendingError):
            resolve_handoff_request(self.paths, self.workspace, latest=True)
        (state_dir / "pending-inc.json").unlink()
        for p in (self.paths.codex_home_dir / "sessions").rglob(f"*{s_inc}*"):
            p.unlink()
        with contextlib.closing(sqlite3.connect(self.paths.codex_home_dir / "state_5.sqlite")) as conn:
            conn.execute("DELETE FROM threads WHERE id = ?", (s_inc,))
            conn.commit()
        res_ok = resolve_handoff_request(self.paths, self.workspace, chain=True)
        self.assertEqual(res_ok.already_ready_receipt.status, "no_op")

    def test_10_handoff_lock_and_sync_lock_attribution(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace)
        tid = self._seed_codex_thread(binding, "01960000-0000-7000-8000-000000000a01", _turn_pair(HostKind.CODEX, "t1", "2026-11-14T01:00:00Z", "Lock", "Test"))
        hlock = self.paths.sync_state_dir / "handoffs" / "handoff.lock"
        hlock.parent.mkdir(parents=True, exist_ok=True)
        with hlock.open("a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(SyncBusyLockError) as h_ctx:
                    handoff_session(self.paths, self.workspace, direction=SyncDirection.CODEX_TO_AGY, source_id=str(tid))
                self.assertEqual(h_ctx.exception.host, "handoff")
                rc, out, err = self._run_cli_inproc(["handoff", "-w", str(self.workspace), "--source-id", str(tid)])
                self.assertEqual((rc, out), (2, ""))
                err_j = json.loads(err)
                self.assertEqual((err_j["error"], err_j["host"]), ("sync_busy_lock", "handoff"))
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

        ws_sync = self.root / "sync-locked-ws"
        ws_sync.mkdir()
        slock = self.paths.sync_state_dir / "locks" / "sync.lock"
        slock.parent.mkdir(parents=True, exist_ok=True)
        with slock.open("a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(SyncBusyLockError) as s_ctx:
                    guarded_pair_workspace(self.paths, self.bridge.guard, self.bridge.registry, ws_sync)
                self.assertEqual(s_ctx.exception.host, "sync")
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
