from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import sqlite3
import uuid

from codex_agy_sync.bridge import (
    DefaultHostTransport, ForkResolutionPolicy, HostEnvironmentPaths, SyncBridge,
    SyncDirection, TransportMode, generate_uuid7,
)
from codex_agy_sync.cli import main as cli_main
from codex_agy_sync.codecs import decode_summaries_state_proto
from codex_agy_sync._wire import encode_string_field
from codex_agy_sync.ledger import (
    AssistantTextEvent, CascadeId, CodexThreadId, EventOriginAnchor, HostKind,
    TrajectoryLedger, TurnId, UserInputEvent,
)
from tests._helpers import BridgeSandboxBase


class TestUnit4LocksRollbackSelfHealAndFork(BridgeSandboxBase):
    def setUp(self) -> None:
        super().setUp()
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.root / "gemini-config",
            agy_data_dir=self.root / "antigravity",
            codex_home_dir=self.root / "codex",
            sync_state_dir=self.root / "sync-state",
            agy_cli_data_dir=self.root / "antigravity-cli",
        )
        self.bridge = SyncBridge(paths=self.paths)
        self.binding = self.bridge.pair_workspace(self.workspace_dir)
        self.locks = self.bridge.guard.probe_locks(None, None)

    def test_4a_generate_uuid7_format_monotonicity_and_codex_thread_allocation(self) -> None:
        samples = [generate_uuid7() for _ in range(256)]
        self.assertEqual(len(set(samples)), 256)
        self.assertEqual(samples, sorted(samples))
        for raw_u in samples:
            self.assertEqual(raw_u, raw_u.lower())
            parsed = uuid.UUID(raw_u)
            self.assertEqual(parsed.version, 7)
            self.assertEqual(parsed.variant, uuid.RFC_4122)

        cid_a = CascadeId("44440001-1111-4111-8111-111111111111")
        cid_blocker = CascadeId("44440002-1111-4111-8111-111111111111")
        evs_a = self._turn_events(HostKind.ANTIGRAVITY, "turn-1", "2026-11-12T06:00:", "Alloc check A", "Done A")
        evs_b = self._turn_events(HostKind.ANTIGRAVITY, "turn-1", "2026-11-12T06:01:", "Blocker", "Done B")
        self.bridge._transport.apply_to_agy(
            self.binding, cid_a, evs_a,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, evs_a, recompute_agy_step_indices=True),
            self.locks, TransportMode.OFFLINE_STORAGE,
        )
        self.bridge._transport.apply_to_agy(
            self.binding, cid_blocker, evs_b,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, evs_b, recompute_agy_step_indices=True),
            self.locks, TransportMode.OFFLINE_STORAGE,
        )
        det_tid_for_a = CodexThreadId(
            str(uuid.uuid5(uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8"), f"codex-agy-sync:paired-codex-thread:{cid_a}"))
        )
        r_block = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid_blocker, codex_thread_id=det_tid_for_a, direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(r_block.transition_kind, "initial_import")

        r_alloc = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid_a, direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(r_alloc.transition_kind, "initial_import")
        self.assertNotEqual(r_alloc.codex_thread_id, det_tid_for_a)
        self.assertEqual(uuid.UUID(str(r_alloc.codex_thread_id)).version, 7)

    def test_4b_mid_write_failure_restores_backups_and_deletes_new_files(self) -> None:
        cid_base = CascadeId("44440010-2222-4222-8222-222222222222")
        t1_agy = self._turn_events(HostKind.ANTIGRAVITY, "turn-1", "2026-11-12T07:00:", "Base turn 1", "Base reply 1")
        self.bridge._transport.apply_to_agy(
            self.binding, cid_base, t1_agy,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t1_agy, recompute_agy_step_indices=True),
            self.locks, TransportMode.OFFLINE_STORAGE,
        )
        r_base = self.bridge.sync_session(self.workspace_dir, cascade_id=cid_base, direction=SyncDirection.AGY_TO_CODEX)
        tid_base = r_base.codex_thread_id
        cursor_file = self.bridge._cursor_path(cid_base, tid_base)
        base_rollouts = list((self.paths.codex_home_dir / "sessions").rglob(f"*{tid_base}.jsonl"))
        self.assertEqual(len(base_rollouts), 1)

        assert self.paths.agy_cli_data_dir is not None
        agy_tracked = [
            self.paths.agy_data_dir / "conversations" / f"{cid_base}.db",
            self.paths.agy_data_dir / "annotations" / f"{cid_base}.pbtxt",
            self.paths.agy_data_dir / "brain" / str(cid_base) / ".system_generated" / "logs" / "transcript.jsonl",
            self.paths.agy_cli_data_dir / "brain" / str(cid_base) / ".system_generated" / "logs" / "transcript.jsonl",
            self.paths.agy_data_dir / "conversation_summaries.db",
            self.paths.agy_data_dir / "agyhub_summaries_proto.pb",
            cursor_file,
        ]
        agy_before = self._sha256_map(agy_tracked)

        t2_codex = self._turn_events(HostKind.CODEX, "turn-2", "2026-11-12T07:05:", "Turn 2 from Codex", "Reply 2")
        self.bridge._transport.apply_to_codex(
            self.binding, tid_base, t2_codex,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, [*t1_agy, *t2_codex]),
            self.locks, TransportMode.OFFLINE_STORAGE, incremental=True,
        )

        class _CrashAgyTransport(DefaultHostTransport):
            def _write_agy_companion_stores(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                super()._write_agy_companion_stores(*args, **kwargs)
                conv_db_path: Path = args[4]
                conv_db_path.with_name(f"{conv_db_path.name}-wal").write_bytes(b"dangling-wal")
                conv_db_path.with_name(f"{conv_db_path.name}-shm").write_bytes(b"dangling-shm")
                raise RuntimeError("simulated mid-write agy failure")

        crash_agy_bridge = SyncBridge(paths=self.paths, transport=_CrashAgyTransport(self.paths))
        with self.assertRaisesRegex(RuntimeError, "simulated mid-write agy failure"):
            crash_agy_bridge.sync_session(
                self.workspace_dir, cascade_id=cid_base, codex_thread_id=tid_base, direction=SyncDirection.CODEX_TO_AGY,
            )
        self.assertEqual(self._sha256_map(agy_tracked), agy_before)
        self.assertFalse((self.paths.agy_data_dir / "conversations" / f"{cid_base}.db-wal").exists())
        self.assertFalse((self.paths.agy_data_dir / "conversations" / f"{cid_base}.db-shm").exists())

        tid_unpaired = CodexThreadId("44440011-2222-4222-8222-222222222222")
        t1_new_codex = self._turn_events(HostKind.CODEX, "turn-1", "2026-11-12T07:10:", "New codex session", "Reply")
        self.bridge._transport.apply_to_codex(
            self.binding, tid_unpaired, t1_new_codex,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t1_new_codex),
            self.locks, TransportMode.OFFLINE_STORAGE,
        )
        expected_new_cid = CascadeId(
            str(uuid.uuid5(uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8"), f"codex-agy-sync:paired-cascade:{tid_unpaired}"))
        )
        agy_before_new = self._sha256_map(agy_tracked)
        with self.assertRaisesRegex(RuntimeError, "simulated mid-write agy failure"):
            crash_agy_bridge.sync_session(
                self.workspace_dir, codex_thread_id=tid_unpaired, direction=SyncDirection.CODEX_TO_AGY,
            )
        self.assertEqual(self._sha256_map(agy_tracked), agy_before_new)
        self.assertFalse((self.paths.agy_data_dir / "conversations" / f"{expected_new_cid}.db").exists())
        self.assertFalse((self.paths.agy_data_dir / "annotations" / f"{expected_new_cid}.pbtxt").exists())
        self.assertFalse(
            (self.paths.agy_data_dir / "brain" / str(expected_new_cid) / ".system_generated" / "logs" / "transcript.jsonl").exists()
        )
        self.assertFalse(
            (self.paths.agy_cli_data_dir / "brain" / str(expected_new_cid) / ".system_generated" / "logs" / "transcript.jsonl").exists()
        )
        self.assertIsNone(self.bridge._load_cursor(self.ws_uri, None, tid_unpaired))

        r_catchup = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid_base, codex_thread_id=tid_base, direction=SyncDirection.CODEX_TO_AGY,
        )
        self.assertEqual(r_catchup.transition_kind, "incremental_append")

        t3_agy = self._turn_events(HostKind.ANTIGRAVITY, "turn-3", "2026-11-12T07:15:", "Turn 3 from AGY", "Reply 3")
        self.bridge._transport.apply_to_agy(
            self.binding, cid_base, t3_agy,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, [*t1_agy, *t2_codex, *t3_agy], recompute_agy_step_indices=True),
            self.locks, TransportMode.OFFLINE_STORAGE, incremental=True,
        )
        codex_tracked = [
            self.paths.codex_home_dir / "state_5.sqlite",
            self.paths.codex_home_dir / ".codex-global-state.json",
            self.paths.codex_home_dir / "session_index.jsonl",
            base_rollouts[0],
            cursor_file,
        ]
        codex_before = self._sha256_map(codex_tracked)

        class _CrashCodexTransport(DefaultHostTransport):
            def _write_codex_index_and_global_state(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                super()._write_codex_index_and_global_state(*args, **kwargs)
                state_db = self._paths.codex_home_dir / "state_5.sqlite"
                state_db.with_name("state_5.sqlite-wal").write_bytes(b"codex-dangling-wal")
                state_db.with_name("state_5.sqlite-shm").write_bytes(b"codex-dangling-shm")
                raise RuntimeError("simulated mid-write codex failure")

        crash_codex_bridge = SyncBridge(paths=self.paths, transport=_CrashCodexTransport(self.paths))
        with self.assertRaisesRegex(RuntimeError, "simulated mid-write codex failure"):
            crash_codex_bridge.sync_session(
                self.workspace_dir, cascade_id=cid_base, codex_thread_id=tid_base, direction=SyncDirection.AGY_TO_CODEX,
            )
        self.assertEqual(self._sha256_map(codex_tracked), codex_before)
        self.assertFalse((self.paths.codex_home_dir / "state_5.sqlite-wal").exists())
        self.assertFalse((self.paths.codex_home_dir / "state_5.sqlite-shm").exists())

        cid_unpaired = CascadeId("44440012-2222-4222-8222-222222222222")
        t1_new_agy = self._turn_events(HostKind.ANTIGRAVITY, "turn-1", "2026-11-12T07:20:", "New AGY session", "Reply")
        self.bridge._transport.apply_to_agy(
            self.binding, cid_unpaired, t1_new_agy,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t1_new_agy, recompute_agy_step_indices=True),
            self.locks, TransportMode.OFFLINE_STORAGE,
        )
        expected_new_tid = CodexThreadId(
            str(uuid.uuid5(uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8"), f"codex-agy-sync:paired-codex-thread:{cid_unpaired}"))
        )
        with self.assertRaisesRegex(RuntimeError, "simulated mid-write codex failure"):
            crash_codex_bridge.sync_session(
                self.workspace_dir, cascade_id=cid_unpaired, direction=SyncDirection.AGY_TO_CODEX,
            )
        self.assertEqual(self._sha256_map(codex_tracked), codex_before)
        self.assertEqual(list((self.paths.codex_home_dir / "sessions").rglob(f"*{expected_new_tid}.jsonl")), [])
        self.assertIsNone(self.bridge._load_cursor(self.ws_uri, cid_unpaired, None))

    def test_4c_held_flock_on_sync_lock_defers_concurrent_writer(self) -> None:
        cid = CascadeId("44440020-3333-4333-8333-333333333333")
        evs = self._turn_events(HostKind.ANTIGRAVITY, "turn-1", "2026-11-12T08:00:", "Flock test", "Reply")
        self.bridge._transport.apply_to_agy(
            self.binding, cid, evs,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, evs, recompute_agy_step_indices=True),
            self.locks, TransportMode.OFFLINE_STORAGE,
        )
        sync_lock_file = self.paths.sync_state_dir / "locks" / "sync.lock"
        sync_lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(sync_lock_file), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deferred = self.bridge.sync_session(
                self.workspace_dir, cascade_id=cid, direction=SyncDirection.AGY_TO_CODEX,
            )
            self.assertEqual(deferred.transition_kind, "deferred_busy")
            self.assertEqual(deferred.events_applied, 0)
            self.assertIsNone(deferred.backup_manifest)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        imported = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid, direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(imported.transition_kind, "initial_import")
        self.assertEqual(imported.events_applied, 2)

    def test_4d_noop_companion_self_heal_and_agy_cli_brain_mirror(self) -> None:
        assert self.paths.agy_cli_data_dir is not None
        cid = CascadeId("44440030-4444-4444-8444-444444444444")
        evs = self._turn_events(HostKind.ANTIGRAVITY, "turn-1", "2026-11-12T08:30:", "Self-heal check", "All good")
        self.bridge._transport.apply_to_agy(
            self.binding, cid, evs,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, evs, recompute_agy_step_indices=True),
            self.locks, TransportMode.OFFLINE_STORAGE,
        )
        r_init = self.bridge.sync_session(self.workspace_dir, cascade_id=cid, direction=SyncDirection.AGY_TO_CODEX)
        tid = r_init.codex_thread_id

        agy_tx = self.paths.agy_data_dir / "brain" / str(cid) / ".system_generated" / "logs" / "transcript.jsonl"
        cli_tx = self.paths.agy_cli_data_dir / "brain" / str(cid) / ".system_generated" / "logs" / "transcript.jsonl"
        pbtxt = self.paths.agy_data_dir / "annotations" / f"{cid}.pbtxt"
        hub_pb = self.paths.agy_data_dir / "agyhub_summaries_proto.pb"
        sess_idx = self.paths.codex_home_dir / "session_index.jsonl"
        global_state = self.paths.codex_home_dir / ".codex-global-state.json"

        self.assertTrue(cli_tx.is_file())
        self.assertEqual(cli_tx.read_text(encoding="utf-8"), agy_tx.read_text(encoding="utf-8"))

        for target in (pbtxt, hub_pb, cli_tx, sess_idx, global_state):
            target.unlink()

        noop_rec = self.bridge.sync_session(
            self.workspace_dir, cascade_id=cid, codex_thread_id=tid, direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(noop_rec.transition_kind, "no_op")
        self.assertEqual(noop_rec.events_applied, 0)

        for target in (pbtxt, hub_pb, cli_tx, sess_idx, global_state):
            self.assertTrue(target.is_file(), msg=f"Missing self-healed companion: {target}")
        self.assertEqual(cli_tx.read_text(encoding="utf-8"), agy_tx.read_text(encoding="utf-8"))
        self.assertIn(cid, decode_summaries_state_proto(hub_pb.read_bytes()).summaries)
        self.assertIn(str(tid), sess_idx.read_text(encoding="utf-8"))
        gs_loaded = json.loads(global_state.read_text(encoding="utf-8"))
        self.assertIn(str(tid), gs_loaded["thread-project-assignments"])

    def test_4e_single_sync_session_symmetric_fork_new_session(self) -> None:
        cid = CascadeId("44440040-5555-4555-8555-555555555555")
        t1 = self._turn_events(HostKind.ANTIGRAVITY, "turn-1", "2026-11-12T09:00:", "Shared turn 1", "Shared reply 1")
        self.bridge._transport.apply_to_agy(
            self.binding, cid, t1,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, t1, recompute_agy_step_indices=True),
            self.locks, TransportMode.OFFLINE_STORAGE,
        )
        r_init = self.bridge.sync_session(self.workspace_dir, cascade_id=cid, direction=SyncDirection.AGY_TO_CODEX)
        tid = r_init.codex_thread_id

        t2_agy = self._turn_events(HostKind.ANTIGRAVITY, "turn-2-agy", "2026-11-12T09:05:", "AGY branch", "AGY reply")
        t2_codex = self._turn_events(HostKind.CODEX, "turn-2-codex", "2026-11-12T09:06:", "Codex branch", "Codex reply")
        self.bridge._transport.apply_to_agy(
            self.binding, cid, t2_agy,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, [*t1, *t2_agy], recompute_agy_step_indices=True),
            self.locks, TransportMode.OFFLINE_STORAGE, incremental=True,
        )
        self.bridge._transport.apply_to_codex(
            self.binding, tid, t2_codex,
            TrajectoryLedger.from_events(self.ws_path, self.ws_uri, [*t1, *t2_codex]),
            self.locks, TransportMode.OFFLINE_STORAGE, incremental=True,
        )

        fork_rec = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
            fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION,
        )
        self.assertEqual(fork_rec.transition_kind, "diverged_forked")
        self.assertEqual(fork_rec.cascade_id, cid)
        self.assertNotEqual(fork_rec.codex_thread_id, tid)
        self.assertEqual(uuid.UUID(str(fork_rec.codex_thread_id)).version, 7)

        active_cursors = list((self.paths.sync_state_dir / "cursors").glob("*.json"))
        superseded_cursors = list((self.paths.sync_state_dir / "cursors" / "superseded").glob("*.json"))
        self.assertEqual(len(active_cursors), 2)
        self.assertEqual(len(superseded_cursors), 1)

        roots = self.bridge.registry.list_workspace_sessions(self.binding, include_subagents=False)
        self.assertEqual(len(roots), 2)
        for desc in roots:
            self.assertIsNotNone(desc.cascade_id)
            self.assertIsNotNone(desc.codex_thread_id)
            self.assertFalse(desc.lineage.is_subagent)
            self.assertEqual(desc.lineage.nesting_depth, 0)

        peer_desc = next(d for d in roots if d.codex_thread_id == tid)
        assert peer_desc.cascade_id is not None
        self.assertNotEqual(peer_desc.cascade_id, cid)
        ro_conn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversations' / f'{peer_desc.cascade_id}.db'}?mode=ro",
            uri=True,
        )
        try:
            parent_refs = ro_conn.execute("SELECT idx, data FROM parent_references ORDER BY idx ASC").fetchall()
        finally:
            ro_conn.close()
        self.assertEqual(parent_refs, [(0, encode_string_field(1, str(cid)))])

        ws_receipts = self.bridge.sync_workspace(
            self.workspace_dir, fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION,
        )
        self.assertEqual(len(ws_receipts), 2)
        self.assertEqual([r.transition_kind for r in ws_receipts], ["no_op", "no_op"])


class TestReadonlyStatusAndOnlineDefer(BridgeSandboxBase):
    def test_online_rpc_defers_and_status_is_strictly_readonly(self) -> None:
        self.paths.codex_home_dir.mkdir(parents=True, exist_ok=True)
        daemon_json = self.paths.codex_home_dir / "daemon.json"
        daemon_json.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        codex_wal = self.paths.codex_home_dir / "state_5.sqlite-wal"
        codex_wal.write_bytes(b"active-wal-frame")

        orig_cwd = Path.cwd()
        stdout_buf = io.StringIO()
        try:
            os.chdir(self.workspace_dir)
            with contextlib.redirect_stdout(stdout_buf):
                rc = cli_main(
                    [
                        "--gemini-config-dir",
                        str(self.paths.gemini_config_dir),
                        "--agy-data-dir",
                        str(self.paths.agy_data_dir),
                        "--codex-home-dir",
                        str(self.paths.codex_home_dir),
                        "--sync-state-dir",
                        str(self.paths.sync_state_dir),
                        "status",
                        "-w",
                        ".",
                    ]
                )
        finally:
            os.chdir(orig_cwd)

        self.assertEqual(rc, 0)
        status_payload = json.loads(stdout_buf.getvalue())
        self.assertEqual(
            status_payload["workspace"]["workspace_path"],
            str(self.workspace_dir.resolve()),
        )
        self.assertFalse((self.paths.codex_home_dir / "state_5.sqlite").exists())
        self.assertFalse((self.paths.codex_home_dir / ".codex-global-state.json").exists())
        self.assertEqual(list((self.paths.gemini_config_dir / "projects").glob("*.json")), [])

        with self.assertRaises(RuntimeError):
            self.bridge.pair_workspace(self.workspace_dir)

        daemon_json.unlink()
        codex_wal.unlink()

        src_tid = CodexThreadId("01a1ae7d-2222-7000-8000-000000000010")
        t1_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T05:00:00Z",
                ),
                query_text="Online guard test",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T05:00:02Z",
                ),
                text="Ready.",
            ),
        ]
        codex_binding = self.bridge.registry.ensure_codex_binding(self.workspace_dir)
        clean_locks = self.bridge.guard.probe_locks(None, src_tid)
        self.bridge._transport.apply_to_codex(
            codex_binding,
            src_tid,
            t1_events,
            TrajectoryLedger.from_events(codex_binding.workspace_path, codex_binding.workspace_uri, t1_events),
            clean_locks,
            TransportMode.OFFLINE_STORAGE,
        )

        target_cid = CascadeId("bbbb1111-2222-3333-4444-555555555555")
        (self.paths.agy_data_dir / "daemon").mkdir(parents=True, exist_ok=True)
        (self.paths.agy_data_dir / "daemon" / "hub_55000.json").write_text(
            json.dumps({"pid": os.getpid(), "csrfToken": "ephemeral-secret"}),
            encoding="utf-8",
        )
        (self.paths.agy_data_dir / "presence").mkdir(parents=True, exist_ok=True)
        lock_file = self.paths.agy_data_dir / "presence" / f"{target_cid}.lock"
        lock_file.write_text("", encoding="utf-8")
        lock_fd = os.open(str(lock_file), os.O_RDONLY)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deferred = self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=target_cid,
                codex_thread_id=src_tid,
                direction=SyncDirection.CODEX_TO_AGY,
                mode=TransportMode.ONLINE_RPC,
            )
            self.assertEqual(deferred.transition_kind, "deferred_busy")
            self.assertIsNone(deferred.backup_manifest)
            self.assertFalse((self.paths.agy_data_dir / "conversations" / f"{target_cid}.db").exists())
            self.assertEqual(list((self.paths.gemini_config_dir / "projects").glob("*.json")), [])

            held_locks = self.bridge.guard.probe_locks(target_cid, src_tid)
            sample_ledger = TrajectoryLedger.from_events(
                codex_binding.workspace_path, codex_binding.workspace_uri, t1_events
            )
            with self.assertRaises(RuntimeError):
                self.bridge._transport.apply_to_agy(
                    codex_binding,
                    target_cid,
                    t1_events,
                    sample_ledger,
                    held_locks,
                    TransportMode.ONLINE_RPC,
                )
            with self.assertRaises(RuntimeError):
                self.bridge._transport.apply_to_agy(
                    codex_binding,
                    target_cid,
                    t1_events,
                    sample_ledger,
                    held_locks,
                    TransportMode.OFFLINE_STORAGE,
                )
            with self.assertRaises(RuntimeError):
                self.bridge._transport.apply_to_codex(
                    codex_binding,
                    src_tid,
                    t1_events,
                    sample_ledger,
                    clean_locks,
                    TransportMode.ONLINE_RPC,
                )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
