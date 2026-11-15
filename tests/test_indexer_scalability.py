"""Scalability, single-pass catalog scanning, and on-demand hydration tests for WorkbenchIndexer."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from codex_agy_sync.bridge import (
    DefaultHostTransport,
    HostEnvironmentPaths,
    ProjectRegistry,
)
from codex_agy_sync.bridge.handoff_store import discover_agy_entries, discover_codex_entries
from codex_agy_sync.bridge.paths_and_locks import deterministic_uuid, open_readonly_sqlite
from codex_agy_sync.bridge.store_catalog import (
    ensure_agy_conversation_db_schema,
    ensure_agy_summaries_db_schema,
    ensure_codex_state_db_schema,
    extract_rollout_filename_uuid,
)
from codex_agy_sync.codecs import compress_zstd_rollout_bytes, encode_agy_trajectory, iter_codex_rollout_lines
from codex_agy_sync.events import AssistantTextEvent, EventOriginAnchor, HostKind, TurnId, UserInputEvent
from codex_agy_sync.workbench import WorkbenchIndexer, WorkbenchService


def _write_agy_session(
    paths: HostEnvironmentPaths,
    sconn: sqlite3.Connection,
    ws_path: Path,
    cid: str,
    title: str,
    user_text: str,
    assistant_text: str,
    ts_iso: str,
    epoch_sec: float,
    *,
    pbtxt_title: str | None = None,
) -> None:
    conv_db = paths.agy_data_dir / "conversations" / f"{cid}.db"
    conv_db.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(conv_db))) as cconn:
        ensure_agy_conversation_db_schema(cconn)
        rows = encode_agy_trajectory([
            UserInputEvent(
                kind="user_input",
                anchor=EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), ts_iso),
                query_text=user_text,
            ),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), ts_iso),
                text=assistant_text,
            ),
        ])
        for row in rows:
            cconn.execute(
                "INSERT INTO steps (idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(row.idx), row.step_type, row.status, 1 if row.has_subtrajectory else 0, row.metadata_blob, row.step_payload_blob, row.step_format),
            )
        cconn.commit()
    sconn.execute(
        "INSERT INTO conversation_summaries (conversation_id, title, step_count, last_modified_time, workspace_uris, project_id, nesting_depth, last_user_input_time) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (
            cid, title, 2, int(epoch_sec * 1000), json.dumps([ws_path.as_uri()]),
            deterministic_uuid("agy-project", ws_path.as_uri()), int(epoch_sec * 1000),
        ),
    )
    if pbtxt_title:
        annot = paths.agy_data_dir / "annotations" / f"{cid}.pbtxt"
        annot.parent.mkdir(parents=True, exist_ok=True)
        annot.write_text(f'title: "{pbtxt_title}"\n', encoding="utf-8")


def _rollout_lines(tid: str, ws_path: Path, user_text: str, assistant_text: str, ts_iso: str) -> str:
    return "\n".join([
        json.dumps({"type": "session_meta", "payload": {"id": tid, "cwd": str(ws_path), "source": "cli", "timestamp": ts_iso}}),
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": user_text}]}}),
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": assistant_text}]}}),
    ]) + "\n"


def _write_codex_session(
    paths: HostEnvironmentPaths,
    cconn: sqlite3.Connection | None,
    ws_path: Path,
    tid: str,
    title: str,
    user_text: str,
    assistant_text: str,
    ts_iso: str,
    epoch_sec: float,
    *,
    use_zst: bool = False,
) -> Path:
    suffix = ".jsonl.zst" if use_zst else ".jsonl"
    rpath = paths.codex_home_dir / "sessions" / "2026" / "11" / "01" / f"rollout-2026-11-01T00-00-00-{tid}{suffix}"
    rpath.parent.mkdir(parents=True, exist_ok=True)
    text = _rollout_lines(tid, ws_path, user_text, assistant_text, ts_iso)
    if use_zst:
        rpath.write_bytes(compress_zstd_rollout_bytes(text))
    else:
        rpath.write_text(text, encoding="utf-8")
    if cconn is not None:
        cconn.execute(
            "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, archived, has_user_event) "
            "VALUES (?, ?, ?, ?, 'cli', 'openai', ?, ?, 'workspace-write', 'never', 0, 1)",
            (tid, str(rpath), int(epoch_sec), int(epoch_sec), str(ws_path), title),
        )
    return rpath


class IndexerScalabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        artifacts = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="indexer-scale-", dir=artifacts)
        self.root = Path(self.temp.name)
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.root / "gemini-cfg",
            agy_data_dir=self.root / "agy-data",
            codex_home_dir=self.root / "codex-home",
            sync_state_dir=self.root / "sync-state",
        )
        for d in (self.paths.gemini_config_dir, self.paths.agy_data_dir, self.paths.codex_home_dir, self.paths.sync_state_dir):
            d.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_35_workspaces_40_sessions(self) -> dict[str, Any]:
        ws_dirs: list[Path] = []
        for idx in range(35):
            ws = self.root / "workspaces" / f"ws-{idx:02d}"
            ws.mkdir(parents=True, exist_ok=True)
            ws_dirs.append(ws)

        sum_db = self.paths.agy_data_dir / "conversation_summaries.db"
        state_db = self.paths.codex_home_dir / "state_5.sqlite"
        base_dt = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        agy_cids: list[str] = []
        codex_tids: list[str] = []
        rollout_by_tid: dict[str, Path] = {}
        needle_tid = ""

        with contextlib.closing(sqlite3.connect(str(sum_db))) as sconn, contextlib.closing(sqlite3.connect(str(state_db))) as cconn:
            ensure_agy_summaries_db_schema(sconn)
            ensure_codex_state_db_schema(cconn)
            for s_idx in range(40):
                # Put sess-37 at an old timestamp so it is NOT in the top-5 newest works.
                rank = 1 if s_idx == 37 else (s_idx + 2)
                dt = base_dt + timedelta(hours=rank)
                ts_iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                epoch = dt.timestamp()
                if s_idx < 20:
                    ws = ws_dirs[s_idx]
                    cid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agy-{s_idx}"))
                    agy_cids.append(cid)
                    _write_agy_session(
                        self.paths, sconn, ws, cid, f"AGY Title {s_idx}", f"agy user {s_idx}", f"agy reply {s_idx}",
                        ts_iso, epoch, pbtxt_title=("PBTXT Title 0" if s_idx == 0 else None),
                    )
                else:
                    ws = ws_dirs[20 + ((s_idx - 20) % 15)]
                    tid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"codex-{s_idx}"))
                    codex_tids.append(tid)
                    is_fallback = s_idx == 38
                    use_zst = s_idx in (25, 37)
                    body = f"codex reply {s_idx}" + (" contains unique_needle_37 inside zst" if s_idx == 37 else "")
                    if s_idx == 37:
                        needle_tid = tid
                    rp = _write_codex_session(
                        self.paths, None if is_fallback else cconn, ws, tid, f"Codex Title {s_idx}",
                        f"codex user {s_idx}", body, ts_iso, epoch, use_zst=use_zst,
                    )
                    rollout_by_tid[tid] = rp

            sub_cid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "agy-subagent-1"))
            sub_ms = int((base_dt + timedelta(hours=1)).timestamp() * 1000)
            sconn.execute(
                "INSERT INTO conversation_summaries (conversation_id, title, step_count, last_modified_time, workspace_uris, project_id, parent_conversation_id, nesting_depth, last_user_input_time) "
                "VALUES (?, 'Subagent', 2, ?, ?, '', ?, 1, ?)",
                (sub_cid, sub_ms, json.dumps([ws_dirs[1].as_uri()]), agy_cids[1], sub_ms),
            )
            sconn.commit()
            cconn.commit()

        corrupt_db = self.paths.agy_data_dir / "conversations" / f"{uuid.uuid4()}.db"
        corrupt_db.write_bytes(b"not a valid sqlite database")
        return {"ws_dirs": ws_dirs, "agy_cids": agy_cids, "codex_tids": codex_tids, "rollout_by_tid": rollout_by_tid, "needle_tid": needle_tid}

    def test_extract_rollout_filename_uuid(self) -> None:
        sample = str(uuid.uuid4())
        self.assertEqual(extract_rollout_filename_uuid(f"rollout-2026-11-15T10-00-00-{sample}.jsonl.zst"), sample)
        self.assertEqual(extract_rollout_filename_uuid(f"rollout-2026-11-15T10-00-00-{sample}.jsonl"), sample)
        self.assertEqual(extract_rollout_filename_uuid("rollout-short.jsonl"), "")

    def test_single_pass_catalog_lazy_hydration_and_search(self) -> None:
        seeded = self._seed_35_workspaces_40_sessions()
        ws_dirs: list[Path] = seeded["ws_dirs"]
        needle_tid: str = seeded["needle_tid"]
        opened_paths: list[str] = []

        def _counting_open(path: Path) -> sqlite3.Connection:
            opened_paths.append(Path(path).name)
            return open_readonly_sqlite(path)

        with (
            patch("codex_agy_sync.workbench.discovery.open_readonly_sqlite", side_effect=_counting_open),
            patch("codex_agy_sync.workbench.catalog.open_readonly_sqlite", side_effect=_counting_open),
            patch("codex_agy_sync.bridge.store_catalog.open_readonly_sqlite", side_effect=_counting_open),
            patch("codex_agy_sync.bridge.handoff_store.open_readonly_sqlite", side_effect=_counting_open),
            patch("codex_agy_sync.bridge.registry.open_readonly_sqlite", side_effect=_counting_open),
            patch("codex_agy_sync.bridge.transport.open_readonly_sqlite", side_effect=_counting_open),
        ):
            indexer = WorkbenchIndexer(self.paths, eager_turn_budget=5)
            snap = indexer.get_snapshot()

        self.assertEqual(opened_paths.count("conversation_summaries.db"), 1)
        self.assertEqual(opened_paths.count("state_5.sqlite"), 1)
        self.assertEqual(len(snap.workspaces), 35)
        self.assertEqual(len(snap.works), 40)

        hydrated_works = [w for w in snap.works if w.turns]
        lazy_works = [w for w in snap.works if not w.turns and w.turn_count > 0]
        self.assertEqual(len(hydrated_works), 5)
        self.assertEqual(len(lazy_works), 35)
        self.assertIn("PBTXT Title 0", {w.title for w in snap.works})

        agy_lazy = next(w for w in lazy_works if w.active_host == HostKind.ANTIGRAVITY.value)
        hydrated_agy = indexer.get_work(agy_lazy.work_id)
        self.assertGreaterEqual(len(hydrated_agy.turns), 1)

        ws22_snap = indexer.get_snapshot(workspace_id=str(ws_dirs[22]))
        self.assertTrue(ws22_snap.works)
        self.assertTrue(all(len(w.turns) >= 1 for w in ws22_snap.works))

        svc = WorkbenchService(self.paths, eager_turn_budget=5)
        cold_svc_snap = svc.get_snapshot()
        needle_work = next(w for w in cold_svc_snap.works if w.latest_source_id == needle_tid)
        self.assertEqual(needle_work.turns, ())
        hits = svc.search("unique_needle_37")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].work_id, needle_work.work_id)
        self.assertGreaterEqual(len(svc.get_work(needle_work.work_id).turns), 1)

        with self.assertRaises(ValueError):
            svc.get_work("   ")
        with self.assertRaises(KeyError):
            svc.get_work("work-does-not-exist")

    def test_bridge_skips_foreign_dbs_and_known_rollouts(self) -> None:
        seeded = self._seed_35_workspaces_40_sessions()
        ws_dirs: list[Path] = seeded["ws_dirs"]
        agy_cids: list[str] = seeded["agy_cids"]
        codex_tids: list[str] = seeded["codex_tids"]
        registry = ProjectRegistry(self.paths)
        transport = DefaultHostTransport(self.paths)

        opened_agy: list[str] = []

        def _count_agy(path: Path) -> sqlite3.Connection:
            opened_agy.append(Path(path).name)
            return open_readonly_sqlite(path)

        with (
            patch("codex_agy_sync.bridge.store_catalog.open_readonly_sqlite", side_effect=_count_agy),
            patch("codex_agy_sync.bridge.handoff_store.open_readonly_sqlite", side_effect=_count_agy),
        ):
            entries = discover_agy_entries(self.paths, transport, registry.resolve_readonly_binding(ws_dirs[0]))

        self.assertEqual([e["session_id"] for e in entries], [agy_cids[0]])
        self.assertEqual(opened_agy.count("conversation_summaries.db"), 1)
        for foreign_cid in agy_cids[1:]:
            self.assertNotIn(f"{foreign_cid}.db", opened_agy)

        iterated_files: list[str] = []

        def _count_iter(rpath: Path):
            iterated_files.append(Path(rpath).name)
            return iter_codex_rollout_lines(rpath)

        with (
            patch("codex_agy_sync.bridge.handoff_store.iter_codex_rollout_lines", side_effect=_count_iter),
            patch("codex_agy_sync.bridge.store_catalog.iter_codex_rollout_lines", side_effect=_count_iter),
        ):
            codex_entries = discover_codex_entries(self.paths, registry.resolve_readonly_binding(ws_dirs[20]), transport)

        self.assertTrue(codex_entries)
        for other_tid in codex_tids:
            if other_tid not in {e["session_id"] for e in codex_entries} and other_tid != codex_tids[18]:
                self.assertFalse(any(other_tid in name for name in iterated_files))

    def test_mutation_invalidate_and_minimal_threads_schema(self) -> None:
        seeded = self._seed_35_workspaces_40_sessions()
        codex_tids: list[str] = seeded["codex_tids"]
        rollout_by_tid: dict[str, Path] = seeded["rollout_by_tid"]
        target_tid = codex_tids[0]
        target_rp = rollout_by_tid[target_tid]

        svc = WorkbenchService(self.paths, eager_turn_budget=5)
        snap1 = svc.get_snapshot()
        work_id = next(w.work_id for w in snap1.works if w.latest_source_id == target_tid)
        self.assertEqual(len(svc.get_work(work_id).turns), 1)

        extra = "\n".join([
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second prompt"}]}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "fresh_turn_after_invalidate"}]}}),
        ]) + "\n"
        target_rp.write_text(target_rp.read_text(encoding="utf-8") + extra, encoding="utf-8")
        svc.invalidate()
        refreshed_work = svc.get_work(work_id)
        self.assertEqual(len(refreshed_work.turns), 2)
        self.assertTrue(svc.search("fresh_turn_after_invalidate"))

        min_root = self.root / "minimal-host"
        ws_min = min_root / "ws-min"
        ws_min.mkdir(parents=True, exist_ok=True)
        min_paths = HostEnvironmentPaths(
            gemini_config_dir=min_root / "g", agy_data_dir=min_root / "a",
            codex_home_dir=min_root / "c", sync_state_dir=min_root / "s",
        )
        for d in (min_paths.gemini_config_dir, min_paths.agy_data_dir, min_paths.codex_home_dir, min_paths.sync_state_dir):
            d.mkdir(parents=True, exist_ok=True)
        min_tid = str(uuid.uuid4())
        min_rp = _write_codex_session(min_paths, None, ws_min, min_tid, "Minimal", "hello", "world", "2026-11-10T12:00:00Z", 1762776000.0)
        with contextlib.closing(sqlite3.connect(str(min_paths.codex_home_dir / "state_5.sqlite"))) as mconn:
            mconn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT, title TEXT)")
            mconn.execute("INSERT INTO threads (id, cwd, rollout_path, title) VALUES (?, ?, ?, ?)", (min_tid, str(ws_min), str(min_rp), "Minimal Title"))
            mconn.commit()
        min_entries = discover_codex_entries(min_paths, ProjectRegistry(min_paths).resolve_readonly_binding(ws_min), DefaultHostTransport(min_paths))
        self.assertEqual([e["session_id"] for e in min_entries], [min_tid])
        min_snap = WorkbenchIndexer(min_paths, eager_turn_budget=0).get_snapshot()
        self.assertEqual(len(min_snap.works), 1)
        self.assertEqual(min_snap.works[0].title, "Minimal Title")
        self.assertEqual(min_snap.works[0].turns, ())
        self.assertEqual(min_snap.works[0].turn_count, 1)


if __name__ == "__main__":
    unittest.main()
