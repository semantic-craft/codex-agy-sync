"""Behavioral tests for the fast menubar feed snapshot and CLI command."""

from __future__ import annotations

from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_agy_sync import (
    AgyDaemonEndpoint,
    AgyGuiSafeSource,
    AgyRpcReadResult,
    AssistantTextEvent,
    CascadeId,
    CodexDaemonEndpoint,
    CodexThreadId,
    CsrfSecret,
    DefaultHostTransport,
    EventOriginAnchor,
    HandoffReceipt,
    HostEnvironmentPaths,
    HostKind,
    MenubarCandidateState,
    PresenceWalGuard,
    SubagentLineage,
    SyncBridge,
    SyncDirection,
    TaskBoundaryEvent,
    TrajectoryLedger,
    TransportMode,
    UserInputEvent,
    build_menubar_feed,
    handoff_session,
)
from codex_agy_sync.bridge import ensure_codex_state_db_schema
from codex_agy_sync.bridge.handoff_store import save_handoff_receipt
from codex_agy_sync.cli import main as cli_main
from codex_agy_sync.workspace import AgyProjectId, TurnId

_NOW_EPOCH = 1_794_730_000.0  # 2026-11-15T08:06:40Z


def _turn_pair(host: HostKind, turn_id: str, ts_iso: str, user_text: str, asst_text: str) -> list:
    anchor = EventOriginAnchor(origin_host=host, turn_id=TurnId(turn_id), timestamp_iso=ts_iso)
    return [
        UserInputEvent(kind="user_input", anchor=anchor, query_text=user_text),
        AssistantTextEvent(kind="assistant_text", anchor=anchor, text=asst_text),
    ]


class MenubarFeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="menubar-feed-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.home / ".gemini",
            agy_data_dir=self.home / ".gemini" / "antigravity",
            codex_home_dir=self.home / ".codex",
            sync_state_dir=self.home / ".codex-agy-sync",
        )
        for d in (
            self.paths.gemini_config_dir / "projects",
            self.paths.agy_data_dir / "conversations",
            self.paths.codex_home_dir / "sessions",
            self.paths.sync_state_dir / "handoffs",
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.ws_a = (self.root / "ws-alpha").resolve()
        self.ws_b = (self.root / "ws-beta").resolve()
        self.ws_archive = (self.home / "Archive" / "secret-ws").resolve()
        for ws in (self.ws_a, self.ws_b, self.ws_archive):
            ws.mkdir(parents=True, exist_ok=True)
        self.bridge = SyncBridge(paths=self.paths)
        self.transport = DefaultHostTransport(self.paths)
        self.guard = PresenceWalGuard(self.paths)
        self.binding_a = self.bridge.pair_workspace(self.ws_a, prefer_online_rpc=False)
        self.binding_b = self.bridge.pair_workspace(self.ws_b, prefer_online_rpc=False)
        self.binding_arch = self.bridge.pair_workspace(self.ws_archive, prefer_online_rpc=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_codex_session(
        self,
        binding,
        tid: str,
        title: str,
        events: list,
        *,
        mtime: float,
        archived: bool = False,
        lineage: SubagentLineage | None = None,
    ) -> Path:
        ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, events, explicit_title_override=title)
        locks = self.guard.probe_locks(None, CodexThreadId(tid))
        self.transport.apply_to_codex(
            binding, CodexThreadId(tid), events, ledger, locks, TransportMode.OFFLINE_STORAGE, lineage=lineage,
        )
        rollout_dir = self.paths.codex_home_dir / "sessions" / "2026" / "11" / "15"
        cand = next(rollout_dir.glob(f"*{tid}.jsonl*"))
        os.utime(cand, (mtime, mtime))
        state_db = self.paths.codex_home_dir / "state_5.sqlite"
        with closing(sqlite3.connect(state_db)) as conn, conn:
            ensure_codex_state_db_schema(conn)
            conn.execute(
                "UPDATE threads SET updated_at = ?, archived = ? WHERE id = ?",
                (int(mtime), 1 if archived else 0, tid),
            )
            conn.commit()
        return cand

    def _write_agy_session(
        self,
        binding,
        cid: str,
        title: str,
        events: list,
        *,
        mtime: float,
        project_id_override: str | None = None,
        lineage: SubagentLineage | None = None,
    ) -> Path:
        eff_binding = binding
        if project_id_override is not None:
            eff_binding = type(binding)(
                binding.workspace_path,
                binding.workspace_uri,
                binding.workspace_name,
                AgyProjectId(project_id_override),
                binding.codex_project_id,
                binding.codex_legacy_project_id,
            )
        ledger = TrajectoryLedger.from_events(eff_binding.workspace_path, eff_binding.workspace_uri, events, explicit_title_override=title)
        locks = self.guard.probe_locks(CascadeId(cid), None)
        if project_id_override is None:
            self.transport.apply_to_agy(
                eff_binding, CascadeId(cid), events, ledger, locks, TransportMode.OFFLINE_STORAGE, lineage=lineage,
            )
        else:
            self.transport.write_agy_conversation_db(
                eff_binding, CascadeId(cid), events, ledger, AgyGuiSafeSource.CASCADE_CLIENT, False, lineage or SubagentLineage(), None,
            )
        db_file = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        os.utime(db_file, (mtime, mtime))
        return db_file

    def test_interrupted_stopped_promoted_to_hero_ahead_of_older_completed(self) -> None:
        cid_completed = "11111111-1111-4111-8111-111111111111"
        tid_interrupted = "22222222-2222-4222-8222-222222222222"
        self._write_agy_session(
            self.binding_a,
            cid_completed,
            "Completed AGY",
            _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-15T07:50:00Z", "Done prompt", "Done answer"),
            mtime=_NOW_EPOCH - 120.0,
        )
        interrupted_anchor = EventOriginAnchor(HostKind.CODEX, TurnId("t2"), "2026-11-15T08:00:00Z")
        interrupted_events = [
            *_turn_pair(HostKind.CODEX, "t1", "2026-11-15T07:55:00Z", "First turn", "First answer"),
            UserInputEvent(kind="user_input", anchor=interrupted_anchor, query_text="Crashed turn"),
        ]
        self._write_codex_session(self.binding_a, tid_interrupted, "Crashed Codex", interrupted_events, mtime=_NOW_EPOCH - 300.0)

        snap = build_menubar_feed(self.paths, now_epoch=_NOW_EPOCH)
        self.assertIsNotNone(snap.hero)
        assert snap.hero is not None
        self.assertEqual(snap.hero.session_id, tid_interrupted)
        self.assertEqual(snap.hero.state, MenubarCandidateState.INTERRUPTED_STOPPED)
        self.assertTrue(snap.hero.has_inflight_turn)
        self.assertEqual(snap.hero.turn_count, 2)
        self.assertEqual([it.session_id for it in snap.recent], [cid_completed])
        self.assertEqual(snap.recent[0].state, MenubarCandidateState.COMPLETED_READY)

        # Beyond the 15-minute window, the newer COMPLETED_READY item wins hero.
        stale_snap = build_menubar_feed(self.paths, now_epoch=_NOW_EPOCH + 1000.0)
        assert stale_snap.hero is not None
        self.assertEqual(stale_snap.hero.session_id, cid_completed)
        self.assertEqual(stale_snap.hero.state, MenubarCandidateState.COMPLETED_READY)

    def test_active_running_classified_and_excluded_from_hero(self) -> None:
        cid_ready = "33333333-3333-4333-8333-333333333333"
        cid_active_agy = "44444444-4444-4444-8444-444444444444"
        tid_active_codex = "55555555-5555-4555-8555-555555555555"

        self._write_agy_session(
            self.binding_a,
            cid_ready,
            "Ready Fallback",
            _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-15T07:58:00Z", "Ready q", "Ready a"),
            mtime=_NOW_EPOCH - 200.0,
        )
        agy_active_events = [
            *_turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-15T08:01:00Z", "First q", "First a"),
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("t2"), "2026-11-15T08:05:00Z"), query_text="Streaming"),
        ]
        db_active = self._write_agy_session(self.binding_a, cid_active_agy, "Streaming AGY", agy_active_events, mtime=_NOW_EPOCH - 20.0)
        with closing(sqlite3.connect(db_active)) as conn, conn:
            conn.execute("UPDATE steps SET status = 2 WHERE idx = (SELECT MAX(idx) FROM steps)")
        os.utime(db_active, (_NOW_EPOCH - 20.0, _NOW_EPOCH - 20.0))

        self._write_codex_session(
            self.binding_a,
            tid_active_codex,
            "Streaming Codex",
            _turn_pair(HostKind.CODEX, "t1", "2026-11-15T08:04:00Z", "Run q", "Run a"),
            mtime=_NOW_EPOCH - 10.0,
        )
        agy_ep = AgyDaemonEndpoint(os.getpid(), 41233, 41234, "1.0", CsrfSecret("tok"))
        codex_ep = CodexDaemonEndpoint(self.root / "codex.sock", os.getpid())
        active_codex_ledger = TrajectoryLedger.from_events(
            self.binding_a.workspace_path,
            self.binding_a.workspace_uri,
            [
                *_turn_pair(HostKind.CODEX, "t1", "2026-11-15T08:04:00Z", "Run q", "Run a"),
                TaskBoundaryEvent(
                    kind="task_boundary",
                    anchor=EventOriginAnchor(HostKind.CODEX, TurnId("t2"), "2026-11-15T08:05:30Z"),
                    boundary_kind="task_boundary",
                    task_name="Thinking",
                    task_status="in_progress",
                    task_summary="Working",
                ),
            ],
            explicit_title_override="Streaming Codex",
        )
        orig_read_codex = DefaultHostTransport.read_codex_ledger_with_meta

        def _fake_read_codex(self_t, binding, tid, locks, mode):
            if str(tid) == tid_active_codex:
                return active_codex_ledger, TransportMode.ONLINE_RPC, False
            return orig_read_codex(self_t, binding, tid, locks, mode)

        with (
            patch.object(PresenceWalGuard, "discover_agy_daemon", return_value=agy_ep),
            patch.object(PresenceWalGuard, "discover_codex_daemon", return_value=codex_ep),
            patch(
                "codex_agy_sync.workbench.menubar_feed.fetch_agy_trajectory_via_rpc",
                return_value=AgyRpcReadResult(raw_steps=(), cascade_run_status=0),
            ),
            patch.object(DefaultHostTransport, "read_codex_ledger_with_meta", _fake_read_codex),
        ):
            snap = build_menubar_feed(self.paths, now_epoch=_NOW_EPOCH)

        self.assertTrue(snap.agy_daemon_active)
        self.assertTrue(snap.codex_daemon_active)
        assert snap.hero is not None
        self.assertEqual(snap.hero.session_id, cid_ready)
        self.assertEqual(snap.hero.state, MenubarCandidateState.COMPLETED_READY)
        by_id = {it.session_id: it for it in snap.recent}
        self.assertEqual(by_id[cid_active_agy].state, MenubarCandidateState.ACTIVE_RUNNING)
        self.assertEqual(by_id[tid_active_codex].state, MenubarCandidateState.ACTIVE_RUNNING)

    def test_handoff_baton_hides_untouched_target_and_promotes_after_new_turn(self) -> None:
        tid_src = "66666666-6666-4666-8666-666666666666"
        self._write_codex_session(
            self.binding_a,
            tid_src,
            "Source Codex",
            _turn_pair(HostKind.CODEX, "t1", "2026-11-15T08:00:00Z", "Analyze bug", "Root cause found"),
            mtime=_NOW_EPOCH - 60.0,
        )
        agy_ep = AgyDaemonEndpoint(os.getpid(), 41233, 41234, "1.0", CsrfSecret("tok"))

        def _fake_fetch_agy(_endpoint, cid, **_kwargs):
            db_path = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
            steps = tuple(self.transport.read_persisted_agy_steps(db_path))
            return AgyRpcReadResult(raw_steps=steps, cascade_run_status=0)

        with (
            patch("codex_agy_sync.bridge.handoff.PresenceWalGuard.discover_agy_daemon", return_value=agy_ep),
            patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=_fake_fetch_agy),
        ):
            receipt = handoff_session(
                self.paths,
                self.ws_a,
                direction=SyncDirection.CODEX_TO_AGY,
                source_id=tid_src,
                binding=self.binding_a,
            )
        self.assertEqual(receipt.status, "ready")
        assert receipt.target_id is not None

        snap_before = build_menubar_feed(self.paths, now_epoch=_NOW_EPOCH)
        self.assertIsNone(snap_before.hero)
        self.assertEqual(len(snap_before.recent), 1)
        src_item = snap_before.recent[0]
        self.assertEqual(src_item.session_id, tid_src)
        self.assertEqual(src_item.state, MenubarCandidateState.ALREADY_HANDED)
        self.assertEqual(src_item.handed_target_id, receipt.target_id)
        self.assertNotIn(receipt.target_id, [it.session_id for it in snap_before.recent])

        target_cid = CascadeId(receipt.target_id)
        existing, _, _ = self.transport.read_agy_ledger_with_meta(
            self.binding_a, target_cid, self.guard.probe_locks(target_cid, None), TransportMode.OFFLINE_STORAGE,
        )
        new_events = list(existing.events) + _turn_pair(
            HostKind.ANTIGRAVITY, "t-follow", "2026-11-15T08:06:00Z", "Continue in AGY", "Applied fix",
        )
        updated_ledger = TrajectoryLedger.from_events(
            self.binding_a.workspace_path, self.binding_a.workspace_uri, new_events, explicit_title_override=existing.title,
        )
        self.transport.apply_to_agy(
            self.binding_a, target_cid, new_events, updated_ledger, self.guard.probe_locks(target_cid, None), TransportMode.OFFLINE_STORAGE,
        )
        os.utime(self.paths.agy_data_dir / "conversations" / f"{receipt.target_id}.db", (_NOW_EPOCH - 5.0, _NOW_EPOCH - 5.0))

        snap_after = build_menubar_feed(self.paths, now_epoch=_NOW_EPOCH)
        assert snap_after.hero is not None
        self.assertEqual(snap_after.hero.session_id, receipt.target_id)
        self.assertEqual(snap_after.hero.state, MenubarCandidateState.COMPLETED_READY)

    def test_pending_reconcile_classification(self) -> None:
        tid_pend = "77777777-7777-4777-8777-777777777777"
        self._write_codex_session(
            self.binding_a,
            tid_pend,
            "Pending Source",
            _turn_pair(HostKind.CODEX, "t1", "2026-11-15T08:01:00Z", "Pending q", "Pending a"),
            mtime=_NOW_EPOCH - 30.0,
        )
        receipt = HandoffReceipt(
            workspace=str(self.binding_a.workspace_path),
            source_host="codex",
            source_id=tid_pend,
            source_digest="sha256:abc",
            target_host="antigravity",
            target_id=None,
            title="Pending Source",
            status="pending",
            staging_path=str(self.paths.sync_state_dir / "handoffs" / "staging" / "rec-pend-1"),
            created_at_iso="2026-11-15T08:02:00Z",
            error="rpc timeout waiting for confirmation",
        )
        save_handoff_receipt(self.paths.sync_state_dir / "handoffs" / "rec-pend-1.json", receipt)

        snap = build_menubar_feed(self.paths, now_epoch=_NOW_EPOCH)
        assert snap.hero is not None
        self.assertEqual(snap.hero.session_id, tid_pend)
        self.assertEqual(snap.hero.state, MenubarCandidateState.PENDING_RECONCILE)
        self.assertEqual(snap.hero.pending_error, "rpc timeout waiting for confirmation")

    def test_exclusions_for_subagents_archive_forbidden_pids_and_seed_filter(self) -> None:
        cid_valid = "88888888-8888-4888-8888-888888888888"
        cid_sub = "99999999-9999-4999-8999-999999999999"
        tid_sub = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        cid_arch = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        cid_forb = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        cid_ws_b = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

        self._write_agy_session(self.binding_a, cid_valid, "Visible Alpha", _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-15T08:00:00Z", "q", "a"), mtime=_NOW_EPOCH - 50.0)
        self._write_agy_session(self.binding_b, cid_ws_b, "Visible Beta", _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-15T08:02:00Z", "q", "a"), mtime=_NOW_EPOCH - 10.0)
        self._write_agy_session(
            self.binding_a,
            cid_sub,
            "AGY Subagent",
            _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-15T08:03:00Z", "sub q", "sub a"),
            mtime=_NOW_EPOCH - 5.0,
            lineage=SubagentLineage(parent_cascade_id=CascadeId(cid_valid), nesting_depth=1, agent_role="research"),
        )
        self._write_codex_session(
            self.binding_a,
            tid_sub,
            "Codex Subagent",
            _turn_pair(HostKind.CODEX, "t1", "2026-11-15T08:03:00Z", "sub q", "sub a"),
            mtime=_NOW_EPOCH - 4.0,
            lineage=SubagentLineage(parent_codex_thread_id=CodexThreadId("12121212-1212-4121-8121-121212121212"), nesting_depth=1, agent_role="worker"),
        )
        self._write_agy_session(self.binding_arch, cid_arch, "Archive Secret", _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-15T08:03:00Z", "arch", "secret"), mtime=_NOW_EPOCH - 3.0)
        self._write_agy_session(
            self.binding_a,
            cid_forb,
            "Forbidden CLI Project",
            _turn_pair(HostKind.ANTIGRAVITY, "t1", "2026-11-15T08:03:00Z", "cli", "noise"),
            mtime=_NOW_EPOCH - 2.0,
            project_id_override="default-cli-project",
        )

        snap_all = build_menubar_feed(self.paths, now_epoch=_NOW_EPOCH)
        all_ids = {it.session_id for it in (snap_all.hero, *snap_all.recent) if it is not None}
        self.assertEqual(all_ids, {cid_valid, cid_ws_b})

        snap_seeded = build_menubar_feed(self.paths, seed_workspaces=[self.ws_a], now_epoch=_NOW_EPOCH)
        seeded_ids = {it.session_id for it in (snap_seeded.hero, *snap_seeded.recent) if it is not None}
        self.assertEqual(seeded_ids, {cid_valid})

    def test_cli_menubar_feed_json_and_limit_validation(self) -> None:
        for idx in range(6):
            sid = f"e0000000-0000-4000-8000-00000000000{idx}"
            self._write_codex_session(
                self.binding_a,
                sid,
                f"Codex Session {idx}",
                _turn_pair(HostKind.CODEX, "t1", "2026-11-15T08:00:00Z", f"q {idx}", f"a {idx}"),
                mtime=_NOW_EPOCH - (60.0 - idx * 5.0),
            )

        out_buf = io.StringIO()
        env = {
            "HOME": str(self.home),
            "GEMINI_CONFIG_DIR": str(self.paths.gemini_config_dir),
            "ANTIGRAVITY_DATA_DIR": str(self.paths.agy_data_dir),
            "CODEX_HOME": str(self.paths.codex_home_dir),
            "CODEX_AGY_SYNC_STATE_DIR": str(self.paths.sync_state_dir),
        }
        with redirect_stdout(out_buf):
            rc = cli_main(["menubar-feed", "--limit", "4"], env=env)
        self.assertEqual(rc, 0)
        payload = json.loads(out_buf.getvalue())
        self.assertIn("generated_at_iso", payload)
        self.assertIsNotNone(payload["hero"])
        self.assertEqual(payload["hero"]["state"], "completed_ready")
        self.assertEqual(len(payload["recent"]), 4)

        with self.assertRaises(SystemExit) as ctx_low:
            cli_main(["menubar-feed", "--limit", "0"], env=env)
        self.assertEqual(ctx_low.exception.code, 2)
        with self.assertRaises(SystemExit) as ctx_high:
            cli_main(["menubar-feed", "--limit", "51"], env=env)
        self.assertEqual(ctx_high.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
