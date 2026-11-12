from __future__ import annotations

import json
import sqlite3

from codex_agy_sync.bridge import ForkResolutionPolicy, SyncDirection, TransportMode
from codex_agy_sync.ledger import (
    AssistantTextEvent, CascadeId, CodexThreadId, EventOriginAnchor, HostKind, StepIndex,
    SubagentLineage, TrajectoryLedger, TurnId, UserInputEvent,
)
from tests._helpers import BridgeSandboxBase


class TestSubagentLineage(BridgeSandboxBase):
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


class TestOrthogonalSubagentLineage(BridgeSandboxBase):
    def setUp(self) -> None:
        super().setUp()
        self.binding = self.bridge.pair_workspace(self.workspace_dir)
        self.locks = self.bridge.guard.probe_locks(None, None)

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
