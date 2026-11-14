from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import uuid

from codex_agy_sync.bridge import (
    CsrfSecret, FORBIDDEN_AGY_PROJECT_IDS, HostEnvironmentPaths, SyncBridge, SyncDirection,
    TransportMode,
)
from codex_agy_sync.codecs import (
    decode_agy_trajectory, decode_codex_rollout, encode_agy_trajectory, encode_codex_rollout,
)
from codex_agy_sync.ledger import (
    AssistantTextEvent, CascadeId, CodexThreadId,
    EventOriginAnchor, HostKind, ThinkingEvent, ToolCallEvent, ToolResultEvent,
    TrajectoryLedger, TurnId, UserInputEvent, canonical_event_digest_payload,
)
from tests._helpers import BridgeSandboxBase


class TestRoundtripAndCli(BridgeSandboxBase):
    def test_csrf_secret_redaction_and_idempotent_project_pairing(self) -> None:
        secret = CsrfSecret("secret-token-value-12345")
        self.assertEqual(str(secret), "[REDACTED_CSRF]")
        self.assertEqual(repr(secret), "CsrfSecret('[REDACTED_CSRF]')")
        self.assertEqual(f"{secret}", "[REDACTED_CSRF]")
        self.assertEqual(
            dict(secret.as_loopback_header()),
            {"x-codeium-csrf-token": "secret-token-value-12345"},
        )
        with self.assertRaises(ValueError):
            CsrfSecret("   ")

        b1 = self.bridge.pair_workspace(self.workspace_dir)
        b2 = self.bridge.pair_workspace(self.workspace_dir.as_uri() + "/")
        self.assertEqual(b1, b2)
        self.assertNotIn(str(b1.agy_project_id), FORBIDDEN_AGY_PROJECT_IDS)

        agy_proj_file = self.paths.gemini_config_dir / "projects" / f"{b1.agy_project_id}.json"
        self.assertTrue(agy_proj_file.is_file())

        gs_file = self.paths.codex_home_dir / ".codex-global-state.json"
        self.assertTrue(gs_file.is_file())
        gs_data = json.loads(gs_file.read_text(encoding="utf-8"))
        self.assertIn(str(b1.codex_legacy_project_id), gs_data["local-projects"])

    def test_real_session_isolated_roundtrip_agy_to_codex_and_back(self) -> None:
        src_cid = self._copy_real_agy_session_into_temp("eb95dc40-cb5e-4017-b4ff-f41223e1f701")
        orig_agy_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            cascade_id=src_cid,
        )

        receipt_1 = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=src_cid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(receipt_1.transition_kind, "initial_import")
        self.assertGreater(receipt_1.events_applied, 200)
        synced_tid = receipt_1.codex_thread_id

        noop_receipt = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=src_cid,
            codex_thread_id=synced_tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(noop_receipt.transition_kind, "no_op")
        self.assertEqual(noop_receipt.events_applied, 0)

        codex_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            codex_thread_id=synced_tid,
        )
        self.assertEqual(
            len([e for e in codex_ledger.events if isinstance(e, ToolCallEvent)]),
            97,
        )
        self.assertEqual(
            len([e for e in codex_ledger.events if isinstance(e, ToolResultEvent)]),
            97,
        )

        roundtrip_cid = CascadeId("99999999-8888-7777-6666-555555555555")
        receipt_2 = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=roundtrip_cid,
            codex_thread_id=synced_tid,
            direction=SyncDirection.CODEX_TO_AGY,
        )
        self.assertEqual(receipt_2.transition_kind, "initial_import")
        self.assertEqual(receipt_2.cascade_id, roundtrip_cid)

        rt_agy_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            cascade_id=roundtrip_cid,
        )
        orig_calls = [e for e in orig_agy_ledger.events if isinstance(e, ToolCallEvent)]
        rt_calls = [e for e in rt_agy_ledger.events if isinstance(e, ToolCallEvent)]
        orig_results = [e for e in orig_agy_ledger.events if isinstance(e, ToolResultEvent)]
        codex_results = [e for e in codex_ledger.events if isinstance(e, ToolResultEvent)]
        rt_results = [e for e in rt_agy_ledger.events if isinstance(e, ToolResultEvent)]

        self.assertEqual(len(rt_calls), 97)
        self.assertEqual(len(rt_results), 97)
        self.assertEqual(
            [
                (str(c.call_id), c.payload.kind if c.payload.kind != "opaque" else c.tool_name, c.payload.kind)
                for c in rt_calls
            ],
            [
                (str(c.call_id), c.payload.kind if c.payload.kind != "opaque" else c.tool_name, c.payload.kind)
                for c in orig_calls
            ],
        )
        for orig_call, rt_call in zip(orig_calls, rt_calls):
            self.assertEqual(
                canonical_event_digest_payload(rt_call),
                canonical_event_digest_payload(orig_call),
            )
            self.assertEqual(rt_call.payload, orig_call.payload)
            paired_call, paired_res = rt_agy_ledger.call_and_result(rt_call.call_id)
            self.assertEqual(paired_call.call_id, rt_call.call_id)
            self.assertIsNotNone(paired_res)

        cur_ledger = rt_agy_ledger
        for _ in range(2):
            hop_lines = encode_codex_rollout(
                cur_ledger.events,
                session_id=str(synced_tid),
                cwd=str(self.workspace_dir),
            )
            hop_codex = decode_codex_rollout(cur_ledger.workspace_path, cur_ledger.workspace_uri, hop_lines)
            hop_steps = encode_agy_trajectory(hop_codex.events)
            cur_ledger = decode_agy_trajectory(cur_ledger.workspace_path, cur_ledger.workspace_uri, hop_steps)
            hop_calls = [e for e in cur_ledger.events if isinstance(e, ToolCallEvent)]
            self.assertEqual([c.payload for c in hop_calls], [c.payload for c in orig_calls])
            self.assertEqual(
                [canonical_event_digest_payload(c) for c in hop_calls],
                [canonical_event_digest_payload(c) for c in orig_calls],
            )

        self.assertEqual(
            [canonical_event_digest_payload(r) for r in codex_results],
            [canonical_event_digest_payload(r) for r in orig_results],
        )
        self.assertEqual(
            [canonical_event_digest_payload(r) for r in rt_results],
            [canonical_event_digest_payload(r) for r in orig_results],
        )
        self.assertEqual(codex_ledger.title, orig_agy_ledger.title)
        self.assertEqual(rt_agy_ledger.title, orig_agy_ledger.title)

        transcript_path = (
            self.paths.agy_data_dir
            / "brain"
            / str(roundtrip_cid)
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        self.assertTrue(transcript_path.is_file())
        self._assert_title_and_project_bindings_across_stores(
            roundtrip_cid,
            synced_tid,
            orig_agy_ledger.title,
        )

    def test_real_session_isolated_roundtrip_codex_to_agy_and_back(self) -> None:
        src_tid = self._copy_real_codex_session_into_temp("01a1ae7d-d56f-7650-8336-aa848b9f2a5a")
        orig_codex_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            codex_thread_id=src_tid,
        )

        receipt_1 = self.bridge.sync_session(
            self.workspace_dir,
            codex_thread_id=src_tid,
            direction=SyncDirection.CODEX_TO_AGY,
        )
        self.assertEqual(receipt_1.transition_kind, "initial_import")
        synced_cid = receipt_1.cascade_id

        receipt_1_noop = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=synced_cid,
            codex_thread_id=src_tid,
            direction=SyncDirection.AUTO_INCREMENTAL,
        )
        self.assertEqual(receipt_1_noop.transition_kind, "no_op")
        self.assertEqual(receipt_1_noop.events_applied, 0)

        agy_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            cascade_id=synced_cid,
        )
        self.assertEqual(
            len([e for e in agy_ledger.events if isinstance(e, ThinkingEvent)]),
            18,
        )
        self.assertEqual(
            len([e for e in agy_ledger.events if isinstance(e, ToolCallEvent)]),
            19,
        )
        self.assertEqual(
            len([e for e in agy_ledger.events if isinstance(e, ToolResultEvent)]),
            19,
        )

        roundtrip_tid = CodexThreadId("01a1ae7d-ffff-7000-8000-000000000099")
        receipt_2 = self.bridge.sync_session(
            self.workspace_dir,
            cascade_id=synced_cid,
            codex_thread_id=roundtrip_tid,
            direction=SyncDirection.AGY_TO_CODEX,
        )
        self.assertEqual(receipt_2.transition_kind, "initial_import")

        rt_codex_ledger = self.bridge.inspect_session_ledger(
            self.workspace_dir,
            codex_thread_id=roundtrip_tid,
        )
        orig_calls = [e for e in orig_codex_ledger.events if isinstance(e, ToolCallEvent)]
        agy_calls = [e for e in agy_ledger.events if isinstance(e, ToolCallEvent)]
        rt_calls = [e for e in rt_codex_ledger.events if isinstance(e, ToolCallEvent)]
        orig_results = [e for e in orig_codex_ledger.events if isinstance(e, ToolResultEvent)]
        agy_results = [e for e in agy_ledger.events if isinstance(e, ToolResultEvent)]
        rt_results = [e for e in rt_codex_ledger.events if isinstance(e, ToolResultEvent)]

        self.assertEqual(
            len([e for e in rt_codex_ledger.events if isinstance(e, ThinkingEvent)]),
            18,
        )
        self.assertEqual(len(rt_calls), 19)
        self.assertEqual(len(rt_results), 19)
        self.assertEqual(
            [(c.tool_name, c.payload) for c in rt_calls],
            [(c.tool_name, c.payload) for c in orig_calls],
        )
        self.assertEqual(
            [canonical_event_digest_payload(c) for c in agy_calls],
            [canonical_event_digest_payload(c) for c in orig_calls],
        )
        self.assertEqual(
            [canonical_event_digest_payload(r) for r in agy_results],
            [canonical_event_digest_payload(r) for r in orig_results],
        )
        self.assertEqual(
            [(str(r.call_id), r.output_text, r.is_error) for r in rt_results],
            [(str(r.call_id), r.output_text, r.is_error) for r in orig_results],
        )
        self.assertEqual(agy_ledger.title, orig_codex_ledger.title)
        self.assertEqual(rt_codex_ledger.title, orig_codex_ledger.title)
        self._assert_title_and_project_bindings_across_stores(
            synced_cid,
            roundtrip_tid,
            orig_codex_ledger.title,
        )

    def test_cli_entrypoints_end_to_end(self) -> None:
        import codex_agy_sync

        self.assertEqual(codex_agy_sync.__version__, "0.2.0")

        src_tid = self._copy_real_codex_session_into_temp("01a1ae7d-d56f-7650-8336-aa848b9f2a5a")
        cli_bin = self.repo_root / "bin" / "codex-agy-sync"

        ver_res = subprocess.run(
            [sys.executable, str(cli_bin), "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(ver_res.stdout.strip(), "codex-agy-sync 0.2.0")

        short_v_res = subprocess.run(
            [sys.executable, str(cli_bin), "-V"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(short_v_res.stdout.strip(), "codex-agy-sync 0.2.0")

        base_args = [
            sys.executable,
            str(cli_bin),
            "--agy-data-dir",
            str(self.paths.agy_data_dir),
            "--gemini-config-dir",
            str(self.paths.gemini_config_dir),
            "--codex-home-dir",
            str(self.paths.codex_home_dir),
            "--sync-state-dir",
            str(self.paths.sync_state_dir),
        ]

        pair_res = subprocess.run(
            base_args + ["pair", "--workspace", str(self.workspace_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        pair_json = json.loads(pair_res.stdout)
        self.assertEqual(pair_json["workspace_name"], "agents-everywhere")

        sync_res = subprocess.run(
            base_args
            + [
                "sync-workspace",
                "--workspace",
                str(self.workspace_dir),
                "--direction",
                "codex_to_agy",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        sync_json = json.loads(sync_res.stdout)
        self.assertEqual(sync_json["synced_count"], 1)
        synced_cid = sync_json["receipts"][0]["cascade_id"]

        inspect_res = subprocess.run(
            base_args
            + [
                "inspect",
                "--workspace",
                str(self.workspace_dir),
                "--cascade-id",
                synced_cid,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        inspect_json = json.loads(inspect_res.stdout)
        self.assertEqual(inspect_json["counts"]["tool_calls"], 19)
        self.assertEqual(inspect_json["counts"]["thinking_events"], 18)

        status_res = subprocess.run(
            base_args + ["status", "--workspace", str(self.workspace_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        status_json = json.loads(status_res.stdout)
        self.assertEqual(len(status_json["sessions"]), 1)
        self.assertEqual(status_json["sessions"][0]["codex_thread_id"], str(src_tid))

    def test_session_id_traversal_rejected_and_backup_command_shell_quoted(self) -> None:
        evil_id = "../../../escaped_target"
        with self.assertRaises(ValueError):
            self.bridge.sync_session(
                self.workspace_dir,
                cascade_id=CascadeId(evil_id),
                direction=SyncDirection.CODEX_TO_AGY,
            )
        with self.assertRaises(ValueError):
            self.bridge.inspect_session_ledger(
                self.workspace_dir,
                cascade_id=CascadeId(evil_id),
            )
        with self.assertRaises(ValueError):
            self.bridge.guard.probe_locks(CascadeId(evil_id), None)
        with self.assertRaises(ValueError):
            self.bridge.guard.create_offline_backup(HostKind.ANTIGRAVITY, evil_id, ())
        self.assertFalse((self.root / "escaped_target.db").exists())

        conv_dir = self.paths.agy_data_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        (conv_dir / "bad..session.db").write_bytes(b"not-a-valid-id")
        bad_sc_dir = self.paths.agy_data_dir / "brain" / "bad..parent" / ".system_generated" / "subagents"
        bad_sc_dir.mkdir(parents=True, exist_ok=True)
        (bad_sc_dir / "child.json").write_text(
            json.dumps({"conversationId": "../evil", "workspaceUris": [self.workspace_dir.as_uri()]}),
            encoding="utf-8",
        )
        ro_binding = self.bridge.registry.resolve_readonly_binding(self.workspace_dir)
        self.assertEqual(
            self.bridge.registry.list_workspace_sessions(ro_binding, include_subagents=True),
            (),
        )

        tricky_dir = self.root / "dir with space; echo pwned"
        tricky_dir.mkdir(parents=True, exist_ok=True)
        tricky_db = tricky_dir / "state.sqlite"
        tricky_wal = tricky_dir / "state.sqlite-wal"
        tricky_db.write_text("db-bytes", encoding="utf-8")
        tricky_wal.write_text("wal-bytes", encoding="utf-8")

        manifest = self.bridge.guard.create_offline_backup(
            HostKind.CODEX,
            "01a1ae7d-5555-7000-8000-000000000077",
            (tricky_db,),
        )
        self.assertIn(shlex.quote(str(tricky_db)), manifest.restoration_command)
        self.assertIn(shlex.quote(str(tricky_wal)), manifest.restoration_command)
        cmd_parts = manifest.restoration_command.split(" && ")
        self.assertGreaterEqual(len(cmd_parts), 2)
        expected_targets = {
            str(tricky_db),
            str(tricky_wal),
            str(tricky_dir / "state.sqlite-shm"),
        }
        for part in cmd_parts:
            tokens = shlex.split(part)
            self.assertIn(tokens[0], ("cp", "rm"))
            self.assertIn(tokens[-1], expected_targets)

    def test_pragma_user_version_preserved_and_stale_codex_socket_ignored(self) -> None:
        binding = self.bridge.pair_workspace(self.workspace_dir)
        cid = CascadeId("5555eeee-2222-3333-4444-555555555555")
        locks = self.bridge.guard.probe_locks(cid, None)

        t1_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T09:00:00Z",
                ),
                query_text="Schema user_version check",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-1"),
                    timestamp_iso="2026-11-10T09:00:01Z",
                ),
                text="Created.",
            ),
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
        conv_db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        conn = sqlite3.connect(str(conv_db))
        try:
            initial_uv = int(conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(initial_uv, 1)
            conn.execute("PRAGMA user_version = 7")
            conn.commit()
        finally:
            conn.close()

        t2_events = [
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T09:01:00Z",
                ),
                query_text="Preserve user_version",
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=TurnId("turn-2"),
                    timestamp_iso="2026-11-10T09:01:01Z",
                ),
                text="Preserved.",
            ),
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
        ro_conn = sqlite3.connect(f"file:{conv_db}?mode=ro", uri=True)
        try:
            preserved_uv = int(ro_conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            ro_conn.close()
        self.assertEqual(preserved_uv, 7)

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="cas-") as short_tmp:
            sroot = Path(short_tmp)
            spaths = HostEnvironmentPaths(
                gemini_config_dir=sroot / "g",
                agy_data_dir=sroot / "a",
                codex_home_dir=sroot / "c",
                sync_state_dir=sroot / "s",
            )
            spaths.codex_home_dir.mkdir(parents=True, exist_ok=True)
            sguard = SyncBridge(paths=spaths).guard
            sock_path = spaths.codex_home_dir / "app-server.sock"

            sock_path.write_text("not-a-socket", encoding="utf-8")
            self.assertIsNone(sguard.discover_codex_daemon())
            sock_path.unlink()

            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                srv.bind(str(sock_path))
                srv.listen(1)
                live_ep = sguard.discover_codex_daemon()
                self.assertIsNotNone(live_ep)
            finally:
                srv.close()

            self.assertTrue(sock_path.exists())
            self.assertIsNone(sguard.discover_codex_daemon())

    def test_verify_skill_tmp_guard_rejects_tmp_run_dir(self) -> None:
        verify_script = (
            self.repo_root
            / "skills"
            / "verify-codex-agy-sync"
            / "scripts"
            / "control_sync_verify.py"
        )
        forbidden_dir = Path("/tmp") / f"forbidden-cas-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.assertFalse(forbidden_dir.exists())
        proc = subprocess.run(
            [sys.executable, str(verify_script), "init", "--run-dir", str(forbidden_dir)],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        self.assertIn("/tmp", proc.stderr)
        self.assertFalse(forbidden_dir.exists())
