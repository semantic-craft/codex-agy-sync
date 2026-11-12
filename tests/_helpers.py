from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unittest

from codex_agy_sync.bridge import FORBIDDEN_AGY_PROJECT_IDS, HostEnvironmentPaths, SyncBridge
from codex_agy_sync.cli import main as cli_main
from codex_agy_sync.codecs import decode_summaries_state_proto
from codex_agy_sync.ledger import (
    AssistantTextEvent, CascadeId, CodexThreadId, EventOriginAnchor, HostKind, TurnId,
    UserInputEvent, parse_workspace_identity,
)
from scripts.verify_session_mapping import decode_protobuf


class BridgeSandboxBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace_dir = self.root / "agent-fleet"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.ws_path, self.ws_uri = parse_workspace_identity(self.workspace_dir)
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.root / "gemini-config",
            agy_data_dir=self.root / "antigravity",
            codex_home_dir=self.root / "codex",
            sync_state_dir=self.root / "sync-state",
        )
        self.bridge = SyncBridge(paths=self.paths)
        repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = repo_root
        self.schema = json.loads((repo_root / "schema" / "antigravity_2_30_schema.json").read_text())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _copy_real_agy_session_into_temp(self, cascade_id: str) -> CascadeId:
        real_agy = Path.home() / ".gemini" / "antigravity"
        real_conv = real_agy / "conversations" / f"{cascade_id}.db"
        if not real_conv.is_file():
            self.skipTest(f"Real Antigravity session {cascade_id} not found")

        (self.paths.agy_data_dir / "conversations").mkdir(parents=True, exist_ok=True)
        shutil.copy2(real_conv, self.paths.agy_data_dir / "conversations" / f"{cascade_id}.db")

        real_annot = real_agy / "annotations" / f"{cascade_id}.pbtxt"
        if real_annot.is_file():
            (self.paths.agy_data_dir / "annotations").mkdir(parents=True, exist_ok=True)
            shutil.copy2(real_annot, self.paths.agy_data_dir / "annotations" / f"{cascade_id}.pbtxt")

        real_sum = real_agy / "conversation_summaries.db"
        if real_sum.is_file():
            src_conn = sqlite3.connect(f"file:{real_sum}?mode=ro", uri=True)
            try:
                row = src_conn.execute(
                    "SELECT title, preview, step_count, last_modified_time, workspace_uris, status, source, project_id, last_user_input_time, last_user_input_step_index, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                    (cascade_id,),
                ).fetchall()
            finally:
                src_conn.close()
            if row:
                r = row[0]
                dst_sum = self.paths.agy_data_dir / "conversation_summaries.db"
                dconn = sqlite3.connect(str(dst_sum))
                try:
                    from codex_agy_sync.bridge import _ensure_agy_summaries_db_schema

                    _ensure_agy_summaries_db_schema(dconn)
                    dconn.execute(
                        """
                        INSERT OR REPLACE INTO conversation_summaries (
                            conversation_id, title, preview, step_count, last_modified_time,
                            workspace_uris, status, source, project_id, last_user_input_time,
                            last_user_input_step_index, raw_summary
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (cascade_id, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10]),
                    )
                    dconn.commit()
                finally:
                    dconn.close()
        return CascadeId(cascade_id)

    def _copy_real_codex_session_into_temp(self, thread_id: str) -> CodexThreadId:
        real_codex = Path.home() / ".codex"
        matches = list((real_codex / "sessions").rglob(f"*{thread_id}.jsonl"))
        if not matches:
            self.skipTest(f"Real Codex rollout for {thread_id} not found")

        dst_dir = self.paths.codex_home_dir / "sessions" / "2026" / "11" / "10"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_rollout = dst_dir / matches[0].name
        shutil.copy2(matches[0], dst_rollout)

        real_state = real_codex / "state_5.sqlite"
        title = "Copied Codex Session"
        preview = "Copied preview"
        if real_state.is_file():
            sconn = sqlite3.connect(f"file:{real_state}?mode=ro", uri=True)
            try:
                trow = sconn.execute(
                    "SELECT title, preview FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
                if trow:
                    title = str(trow[0] or title)
                    preview = str(trow[1] or preview)
            finally:
                sconn.close()

        binding = self.bridge.pair_workspace(self.workspace_dir)
        dst_state = self.paths.codex_home_dir / "state_5.sqlite"
        dconn = sqlite3.connect(str(dst_state))
        try:
            from codex_agy_sync.bridge import _ensure_codex_state_db_schema

            _ensure_codex_state_db_schema(dconn)
            dconn.execute(
                """
                INSERT OR REPLACE INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider,
                    cwd, title, sandbox_policy, approval_mode, tokens_used, has_user_event,
                    archived, cli_version, first_user_message, created_at_ms, updated_at_ms,
                    preview, recency_at, recency_at_ms, name, project_id, originator
                ) VALUES (?, ?, 1794200000, 1794200100, 'desktop', 'openai', ?, ?, 'workspace-write', 'never', 0, 1, 0, '0.166.0', ?, 1794200000000, 1794200100000, ?, 1794200100, 1794200100000, ?, ?, 'codex_vscode')
                """,
                (
                    thread_id,
                    str(dst_rollout),
                    str(binding.workspace_path),
                    title,
                    preview,
                    preview,
                    title,
                    str(binding.codex_project_id),
                ),
            )
            dconn.commit()
        finally:
            dconn.close()

        return CodexThreadId(thread_id)

    def _assert_title_and_project_bindings_across_stores(
        self,
        cid: CascadeId,
        tid: CodexThreadId,
        expected_title: str,
    ) -> None:
        binding = self.bridge.registry.resolve_readonly_binding(self.workspace_dir)
        self.assertNotIn(str(binding.agy_project_id), FORBIDDEN_AGY_PROJECT_IDS)

        proj_json_path = self.paths.gemini_config_dir / "projects" / f"{binding.agy_project_id}.json"
        self.assertTrue(proj_json_path.is_file())
        proj_doc = json.loads(proj_json_path.read_text(encoding="utf-8"))
        self.assertEqual(proj_doc["id"], str(binding.agy_project_id))
        self.assertIn(
            str(binding.workspace_uri),
            self.bridge.registry._extract_agy_project_folder_uris(proj_doc),
        )

        annot_pbtxt = self.paths.agy_data_dir / "annotations" / f"{cid}.pbtxt"
        self.assertTrue(annot_pbtxt.is_file())
        pbtxt_match = re.search(
            r'title:\s*"((?:[^"\\]|\\.)*)"',
            annot_pbtxt.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(pbtxt_match)
        assert pbtxt_match is not None
        pbtxt_title = (
            pbtxt_match.group(1)
            .replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\\\", "\\")
            .strip()
        )
        self.assertEqual(pbtxt_title, expected_title)

        conv_conn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversations' / f'{cid}.db'}?mode=ro",
            uri=True,
        )
        try:
            tmeta_blob = conv_conn.execute(
                "SELECT data FROM trajectory_metadata_blob WHERE id = 'main'"
            ).fetchone()[0]
        finally:
            conv_conn.close()
        tmeta_unk: list[dict[str, object]] = []
        decoded_tmeta = decode_protobuf(
            bytes(tmeta_blob),
            "exa.cortex_pb.CortexTrajectoryMetadata",
            self.schema,
            tmeta_unk,
        )
        self.assertEqual(tmeta_unk, [])
        self.assertEqual(decoded_tmeta["project_id"], str(binding.agy_project_id))

        sum_conn = sqlite3.connect(
            f"file:{self.paths.agy_data_dir / 'conversation_summaries.db'}?mode=ro",
            uri=True,
        )
        try:
            sum_row = sum_conn.execute(
                "SELECT title, project_id, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                (str(cid),),
            ).fetchone()
        finally:
            sum_conn.close()
        self.assertIsNotNone(sum_row)
        assert sum_row is not None
        self.assertEqual(sum_row[0], expected_title)
        self.assertEqual(sum_row[1], str(binding.agy_project_id))
        sum_unk: list[dict[str, object]] = []
        decoded_sum = decode_protobuf(
            bytes(sum_row[2]),
            "exa.jetski_cortex_pb.CascadeTrajectorySummary",
            self.schema,
            sum_unk,
        )
        self.assertEqual(sum_unk, [])
        self.assertEqual(decoded_sum["annotations"]["title"], expected_title)
        self.assertEqual(decoded_sum["trajectory_metadata"]["project_id"], str(binding.agy_project_id))

        hub_pb = self.paths.agy_data_dir / "agyhub_summaries_proto.pb"
        self.assertTrue(hub_pb.is_file())
        hub_state = decode_summaries_state_proto(hub_pb.read_bytes())
        self.assertIn(cid, hub_state.summaries)
        hub_unk: list[dict[str, object]] = []
        decoded_hub_sum = decode_protobuf(
            hub_state.summaries[cid].summary_proto_bytes,
            "exa.jetski_cortex_pb.CascadeTrajectorySummary",
            self.schema,
            hub_unk,
        )
        self.assertEqual(hub_unk, [])
        self.assertEqual(decoded_hub_sum["annotations"]["title"], expected_title)
        self.assertEqual(decoded_hub_sum["trajectory_metadata"]["project_id"], str(binding.agy_project_id))

        state_conn = sqlite3.connect(
            f"file:{self.paths.codex_home_dir / 'state_5.sqlite'}?mode=ro",
            uri=True,
        )
        try:
            proj_row = state_conn.execute(
                "SELECT id FROM projects WHERE id = ?",
                (str(binding.codex_project_id),),
            ).fetchone()
            root_row = state_conn.execute(
                "SELECT path FROM project_roots WHERE project_id = ?",
                (str(binding.codex_project_id),),
            ).fetchone()
            thread_row = state_conn.execute(
                "SELECT title, name, project_id, cwd, archived FROM threads WHERE id = ?",
                (str(tid),),
            ).fetchone()
        finally:
            state_conn.close()
        self.assertIsNotNone(proj_row)
        self.assertIsNotNone(root_row)
        assert root_row is not None
        self.assertEqual(root_row[0], str(binding.workspace_path))
        self.assertIsNotNone(thread_row)
        assert thread_row is not None
        self.assertEqual(thread_row[0], expected_title)
        self.assertEqual(thread_row[1], expected_title)
        self.assertEqual(thread_row[2], str(binding.codex_project_id))
        self.assertEqual(thread_row[3], str(binding.workspace_path))
        self.assertEqual(int(thread_row[4]), 0)

        session_idx = self.paths.codex_home_dir / "session_index.jsonl"
        self.assertTrue(session_idx.is_file())
        idx_entries = [
            json.loads(line)
            for line in session_idx.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matched_idx = [entry for entry in idx_entries if entry.get("id") == str(tid)]
        self.assertEqual(len(matched_idx), 1)
        self.assertEqual(matched_idx[0]["thread_name"], expected_title)

        gs_path = self.paths.codex_home_dir / ".codex-global-state.json"
        self.assertTrue(gs_path.is_file())
        gs_doc = json.loads(gs_path.read_text(encoding="utf-8"))
        self.assertIn(str(binding.codex_legacy_project_id), gs_doc["local-projects"])
        self.assertEqual(
            gs_doc["thread-project-assignments"][str(tid)]["projectId"],
            str(binding.codex_legacy_project_id),
        )

    def _run_cli_json(self, *args: str) -> dict[str, object]:
        buf = io.StringIO()
        base = [
            "--gemini-config-dir",
            str(self.paths.gemini_config_dir),
            "--agy-data-dir",
            str(self.paths.agy_data_dir),
            "--codex-home-dir",
            str(self.paths.codex_home_dir),
            "--sync-state-dir",
            str(self.paths.sync_state_dir),
            *args,
        ]
        with contextlib.redirect_stdout(buf):
            rc = cli_main(base)
        self.assertEqual(rc, 0, msg=buf.getvalue())
        return json.loads(buf.getvalue())

    def _turn_events(
        self, host: HostKind, turn_id: str, ts_prefix: str, prompt: str, reply: str
    ) -> list[UserInputEvent | AssistantTextEvent]:
        anchor = EventOriginAnchor(origin_host=host, turn_id=TurnId(turn_id), timestamp_iso=f"{ts_prefix}00Z")
        reply_anchor = EventOriginAnchor(origin_host=host, turn_id=TurnId(turn_id), timestamp_iso=f"{ts_prefix}01Z")
        return [
            UserInputEvent(kind="user_input", anchor=anchor, query_text=prompt),
            AssistantTextEvent(kind="assistant_text", anchor=reply_anchor, text=reply),
        ]

    @staticmethod
    def _sha256_map(paths: list[Path]) -> dict[Path, str]:
        return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
