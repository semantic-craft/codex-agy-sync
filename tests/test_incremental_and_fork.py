from __future__ import annotations

import fcntl
import json
import os
import sqlite3

from codex_agy_sync.bridge import ForkResolutionPolicy, SyncDirection, TransportMode
from codex_agy_sync._wire import encode_string_field
from codex_agy_sync.ledger import (
    AssistantTextEvent, CascadeId, CodexThreadId, EventOriginAnchor, HostKind,
    ReadFilePayload, ThinkingEvent, ToolCallEvent, ToolCallId, ToolResultEvent,
    TrajectoryLedger, TurnId, UserInputEvent,
)
from tests._helpers import BridgeSandboxBase


class TestIncrementalAndFork(BridgeSandboxBase):
    def test_incremental_append_divergence_fork_overwrite_and_presence_lock(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("aaaa1111-2222-3333-4444-555555555555")
        tid = CodexThreadId("01a1ae7d-aaaa-7000-8000-111111111111")

        t1_events = [
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T04:00:00Z"), query_text="Initial turn 1 prompt"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T04:00:01Z"), text="Initial turn 1 response"),
        ]
        t1_ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            t1_events,
            explicit_title_override="Incremental & Fork Test Session",
        )
        locks = self.bridge.guard.probe_locks(cid, tid)
        self.bridge.transport.apply_to_agy(
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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-2"), timestamp_iso="2026-11-10T04:05:00Z"), query_text="Incremental turn 2 prompt"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-2"), timestamp_iso="2026-11-10T04:05:01Z"), text="Incremental turn 2 response"),
        ]
        t2_ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            t2_events,
            explicit_title_override="Incremental & Fork Test Session",
        )
        self.bridge.transport.apply_to_agy(
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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-3-agy"), timestamp_iso="2026-11-10T04:10:00Z"), query_text="AGY independent turn 3"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-3-agy"), timestamp_iso="2026-11-10T04:10:01Z"), text="AGY independent response 3"),
        ]
        codex_t3_events = list(t2_events) + [
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-3-codex"), timestamp_iso="2026-11-10T04:11:00Z"), query_text="Codex independent turn 3"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-3-codex"), timestamp_iso="2026-11-10T04:11:01Z"), text="Codex independent response 3"),
        ]
        self.bridge.transport.apply_to_agy(
            binding=binding,
            cascade_id=cid,
            events=agy_t3_events,
            full_ledger=TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, agy_t3_events),
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )
        self.bridge.transport.apply_to_codex(
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
                UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-4-codex"), timestamp_iso="2026-11-10T04:15:00Z"), query_text="Turn 4 while AGY is flock locked"),
                AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-4-codex"), timestamp_iso="2026-11-10T04:15:01Z"), text="Response 4"),
            ]
            self.bridge.transport.apply_to_codex(
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

    def test_top_level_fork_preserves_sidebar_visibility_and_converges_once(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("1111aaaa-2222-3333-4444-555555555555")
        tid = CodexThreadId("01a1ae7d-3333-7000-8000-000000000001")

        t1_events = [
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T06:00:00Z"), query_text="Shared ancestor turn"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T06:00:01Z"), text="Shared ancestor reply"),
        ]
        locks = self.bridge.guard.probe_locks(cid, tid)
        self.bridge.transport.apply_to_agy(
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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-2-agy"), timestamp_iso="2026-11-10T06:01:00Z"), query_text="Antigravity branch turn"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-2-agy"), timestamp_iso="2026-11-10T06:01:01Z"), text="Antigravity branch reply"),
        ]
        codex_t2 = [
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-2-codex"), timestamp_iso="2026-11-10T06:01:05Z"), query_text="Codex branch turn"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-2-codex"), timestamp_iso="2026-11-10T06:01:06Z"), text="Codex branch reply"),
        ]
        self.bridge.transport.apply_to_agy(
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
        self.bridge.transport.apply_to_codex(
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

        ws2 = self.root / "agents-everywhere-c2a-fork"
        ws2.mkdir(parents=True, exist_ok=True)
        b2 = self.bridge.pair_workspace(ws2)
        cid2 = CascadeId("2222bbbb-2222-3333-4444-555555555555")
        tid2 = CodexThreadId("01a1ae7d-4444-7000-8000-000000000002")
        locks2 = self.bridge.guard.probe_locks(cid2, tid2)
        self.bridge.transport.apply_to_agy(
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
        self.bridge.transport.apply_to_agy(
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
        self.bridge.transport.apply_to_codex(
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

    def test_incremental_codex_to_agy_step_indices_match_across_stores(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("3333cccc-2222-3333-4444-555555555555")
        tid = CodexThreadId("01a1ae7d-6666-7000-8000-000000000003")
        locks = self.bridge.guard.probe_locks(cid, tid)

        t1_events = [
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T07:00:00Z"), query_text="Initial question"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T07:00:01Z"), text="Initial answer"),
        ]
        self.bridge.transport.apply_to_agy(
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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-2"), timestamp_iso="2026-11-10T07:01:00Z"), query_text="Followup in Codex"),
            ThinkingEvent(kind="thinking", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-2"), timestamp_iso="2026-11-10T07:01:01Z"), summary_text="Checking README"),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-2"), timestamp_iso="2026-11-10T07:01:02Z"),
                call_id=ToolCallId("call-step-align-1"),
                tool_name="read_file",
                tool_summary="Read README",
                payload=read_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-2"), timestamp_iso="2026-11-10T07:01:03Z"),
                call_id=ToolCallId("call-step-align-1"),
                tool_name="read_file",
                output_text="# Title",
                is_error=False,
                payload=read_payload,
            ),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-2"), timestamp_iso="2026-11-10T07:01:04Z"), text="Checked README."),
        ]
        self.bridge.transport.apply_to_codex(
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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T08:00:00Z"), query_text="Crash recovery check"),
            ToolCallEvent(
                kind="tool_call",
                anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T08:00:01Z"),
                call_id=ToolCallId("call-crash-1"),
                tool_name="read_file",
                tool_summary="Read a.py",
                payload=read_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T08:00:02Z"),
                call_id=ToolCallId("call-crash-1"),
                tool_name="read_file",
                output_text="x = 1",
                is_error=False,
                payload=read_payload,
            ),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-10T08:00:03Z"), text="Read complete."),
        ]
        self.bridge.transport.apply_to_agy(
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
        cpath = self.bridge.cursor_path(cid, tid)
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
            UserInputEvent(kind="user_input", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-2"), timestamp_iso="2026-11-10T08:01:00Z"), query_text="Second turn before cursor crash"),
            AssistantTextEvent(kind="assistant_text", anchor=EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=TurnId("turn-2"), timestamp_iso="2026-11-10T08:01:01Z"), text="Second turn done."),
        ]
        self.bridge.transport.apply_to_agy(
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
