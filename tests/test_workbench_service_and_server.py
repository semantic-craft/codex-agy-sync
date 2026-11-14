from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from codex_agy_sync import (
    CascadeId,
    CodexDaemonEndpoint,
    CodexRpcReadResult,
    CodexThreadId,
    HandoffReceipt,
    HostEnvironmentPaths,
    HostKind,
    SyncBridge,
    SyncBusyLockError,
    TransportMode,
    WorkbenchServer,
    WorkbenchService,
    load_workbench_lease,
    query_workbench_status,
    stop_workbench_server,
)
from codex_agy_sync.bridge.handoff import HandoffPendingError
from codex_agy_sync.bridge.registry import ensure_agy_summaries_db_schema
from codex_agy_sync.bridge.rpc import AgyRpcReadResult
from codex_agy_sync.bridge.state_machine import generate_uuid7
from codex_agy_sync.bridge.transport import DefaultHostTransport
from codex_agy_sync.cli import main as cli_main
from codex_agy_sync.codecs import decode_codex_rollout, iter_codex_rollout_lines
from codex_agy_sync.ledger import (
    AssistantTextEvent,
    EventOriginAnchor,
    HostExtensionEnvelope,
    RunCommandPayload,
    ToolCallEvent,
    ToolCallId,
    TrajectoryLedger,
    TurnId,
    UserInputEvent,
)
from codex_agy_sync.workbench import discover_workspaces
from codex_agy_sync.workspace import is_sensitive_excluded_path


def _host_fingerprints() -> dict[str, tuple[bool, int | None, int | None, int | None]]:
    home = Path.home()
    targets = {
        "agy_summaries": home / ".gemini" / "antigravity" / "conversation_summaries.db",
        "codex_state": home / ".codex" / "state_5.sqlite",
    }
    out: dict[str, tuple[bool, int | None, int | None, int | None]] = {}
    for key, target in targets.items():
        try:
            st = os.stat(target)
            out[key] = (True, int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))
        except FileNotFoundError:
            out[key] = (False, None, None, None)
    return out


def _turn_pair(host: HostKind, turn_id: str, ts_iso: str, user_text: str, asst_text: str, cmd: str | None = None) -> list:
    anchor = EventOriginAnchor(origin_host=host, turn_id=TurnId(turn_id), timestamp_iso=ts_iso)
    events: list = [UserInputEvent(kind="user_input", anchor=anchor, query_text=user_text)]
    if cmd:
        events.append(ToolCallEvent(
            kind="tool_call", anchor=anchor, call_id=ToolCallId(f"call-{turn_id}"),
            tool_name="run_command", payload=RunCommandPayload(kind="run_command", command_line=cmd, cwd="workspace"), tool_summary=cmd,
        ))
    events.append(AssistantTextEvent(kind="assistant_text", anchor=anchor, text=asst_text))
    return events


def _http_raw(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw
    finally:
        conn.close()


class WorkbenchServiceAndServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._before_host = _host_fingerprints()
        artifacts = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="workbench-test-", dir=artifacts)
        self.root = Path(self.temp.name)
        self.fake_home = self.root / "fakehome"
        self.paths = HostEnvironmentPaths(
            gemini_config_dir=self.fake_home / ".gemini" / "config",
            agy_data_dir=self.fake_home / ".gemini" / "antigravity",
            codex_home_dir=self.fake_home / ".codex",
            sync_state_dir=self.root / "sync-state",
        )
        self.ws_alpha = self.root / "ws-alpha"
        self.ws_beta = self.root / "ws-beta"
        self.ws_alpha.mkdir(parents=True, exist_ok=True)
        self.ws_beta.mkdir(parents=True, exist_ok=True)
        self.bridge = SyncBridge(self.paths)
        self.transport = DefaultHostTransport(self.paths)
        self.cli_bin = Path(__file__).resolve().parents[1] / "bin" / "codex-agy-sync"
        self.ui_file = self.root / "workbench.html"
        self.ui_file.write_text(
            "<!doctype html><html><head><title>Stage 1</title></head><body><div>OK</div></body></html>\n",
            encoding="utf-8",
        )
        self.ui_digest_before = hashlib.sha256(self.ui_file.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.assertEqual(_host_fingerprints(), self._before_host)

    def _base_cli_args(self) -> list[str]:
        return [
            "--gemini-config-dir", str(self.paths.gemini_config_dir),
            "--agy-data-dir", str(self.paths.agy_data_dir),
            "--codex-home-dir", str(self.paths.codex_home_dir),
            "--sync-state-dir", str(self.paths.sync_state_dir),
        ]

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = cli_main(self._base_cli_args() + argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def _seed_codex(self, binding, tid: str, events: list, *, title: str = "Codex Work") -> CodexThreadId:
        tid_obj = CodexThreadId(tid)
        ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, events, explicit_title_override=title)
        locks = self.bridge.guard.probe_locks(None, tid_obj)
        self.transport.apply_to_codex(binding, tid_obj, ledger.events, ledger, locks, TransportMode.OFFLINE_STORAGE)
        return tid_obj

    def _seed_agy(self, binding, cid: str, events: list, *, title: str = "AGY Work") -> CascadeId:
        cid_obj = CascadeId(cid)
        ledger = TrajectoryLedger.from_events(
            binding.workspace_path, binding.workspace_uri, events,
            explicit_title_override=title, recompute_agy_step_indices=True,
        )
        locks = self.bridge.guard.probe_locks(cid_obj, None)
        self.transport.apply_to_agy(binding, cid_obj, ledger.events, ledger, locks, TransportMode.OFFLINE_STORAGE)
        return cid_obj

    def _activate_agy_rpc(self, endpoint: object, cid: CascadeId, **kwargs: object) -> AgyRpcReadResult:
        _ = endpoint, kwargs
        db = self.paths.agy_data_dir / "conversations" / f"{cid}.db"
        return AgyRpcReadResult(tuple(self.transport.read_persisted_agy_steps(db)), 1)

    def _fetch_codex_rpc(self, endpoint: object, tid: CodexThreadId, **kwargs: object) -> CodexRpcReadResult:
        _ = endpoint, kwargs
        rp, title = self.transport.find_codex_rollout_and_title(tid)
        return CodexRpcReadResult(thread={"id": str(tid), "name": title or "", "path": str(rp), "turns": []}, is_active=False)

    def _materialize_codex_rpc(
        self, endpoint: object, rollout_path: Path, ws_path: Path, codex_project_id: str, title: str, timeout_s: float = 90.0,
    ) -> str:
        _ = endpoint, ws_path, codex_project_id, timeout_s
        binding = self.bridge.registry.resolve_readonly_binding(self.ws_alpha)
        lines = tuple(iter_codex_rollout_lines(rollout_path))
        ledger = decode_codex_rollout(binding.workspace_path, binding.workspace_uri, lines, explicit_title_override=title)
        new_tid = CodexThreadId(generate_uuid7())
        clean_locks = dataclasses.replace(self.bridge.guard.probe_locks(None, None), codex_daemon=None)
        self.transport.apply_to_codex(binding, new_tid, ledger.events, ledger, clean_locks, TransportMode.OFFLINE_STORAGE)
        return str(new_tid)

    @contextlib.contextmanager
    def _mock_live_daemons(self):
        fake_ep = CodexDaemonEndpoint(socket_path=self.root / "codex.sock", pid=os.getpid())
        with (
            mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard.discover_agy_daemon", return_value=object()),
            mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard.discover_codex_daemon", return_value=fake_ep),
            mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=self._activate_agy_rpc),
            mock.patch("codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc", side_effect=self._fetch_codex_rpc),
            mock.patch("codex_agy_sync.bridge.handoff.import_codex_rollout_via_rpc", side_effect=self._materialize_codex_rpc),
        ):
            yield

    def _write_receipt(
        self,
        name: str,
        ws_path: Path,
        source_host: str,
        source_id: str,
        target_host: str,
        target_id: str | None,
        *,
        status: str = "ready",
        chain_id: str = "chain-abc",
        chain_depth: int = 0,
        seed_turns: int | None = None,
        created_at: str = "2026-11-14T02:00:00Z",
    ) -> None:
        hdir = self.paths.sync_state_dir / "handoffs"
        hdir.mkdir(parents=True, exist_ok=True)
        receipt = HandoffReceipt(
            workspace=str(ws_path),
            source_host=source_host,
            source_id=source_id,
            source_digest="digest-1",
            target_host=target_host,
            target_id=target_id,
            title="Chained Work",
            status=status,  # type: ignore[arg-type]
            staging_path=str(self.root / "staging.jsonl"),
            created_at_iso=created_at,
            chain_id=chain_id,
            chain_depth=chain_depth,
            target_seed_turn_count=seed_turns,
        )
        (hdir / f"{name}.json").write_text(json.dumps(dataclasses.asdict(receipt)), encoding="utf-8")

    def test_1_discovery_exclusions_and_corrupt_tolerance(self) -> None:
        archive_direct = self.fake_home / "Archive"
        archive_nested = archive_direct / "secret-repo"
        priv_direct = self.fake_home / ".private_keys"
        priv_nested = priv_direct / "vault-repo"
        symlink_trap = self.root / "symlink-into-archive"
        for d in (archive_nested, priv_nested):
            d.mkdir(parents=True, exist_ok=True)
        symlink_trap.symlink_to(archive_nested)

        self.assertTrue(is_sensitive_excluded_path(archive_direct, self.fake_home))
        self.assertTrue(is_sensitive_excluded_path(archive_nested, self.fake_home))
        self.assertTrue(is_sensitive_excluded_path(priv_nested, self.fake_home))
        self.assertTrue(is_sensitive_excluded_path(symlink_trap, self.fake_home))
        self.assertFalse(is_sensitive_excluded_path(self.ws_alpha, self.fake_home))

        self.bridge.pair_workspace(self.ws_alpha)
        c_only = self.bridge.registry.ensure_codex_binding(self.ws_beta)
        self._seed_codex(c_only, "01960000-0000-7000-8000-000000000101", _turn_pair(HostKind.CODEX, "t1", "2026-11-14T01:00:00Z", "Alpha beta", "Reply beta"))

        ws_gamma = self.root / "ws-gamma"
        ws_gamma.mkdir()
        sum_db = self.paths.agy_data_dir / "conversation_summaries.db"
        sum_db.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(str(sum_db))) as conn:
            ensure_agy_summaries_db_schema(conn)
            conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, title, last_modified_time, last_user_input_time, workspace_uris, project_id) VALUES (?, ?, ?, ?, ?, ?)",
                ("aaaaaaaa-0000-7000-8000-000000000001", "Gamma Sum", 1794200000000, 1794200000000, json.dumps([ws_gamma.as_uri()]), "gamma-proj"),
            )
            conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, title, last_modified_time, last_user_input_time, workspace_uris, project_id) VALUES (?, ?, ?, ?, ?, ?)",
                ("bbbbbbbb-0000-7000-8000-000000000001", "Excluded Sum", 1794200000000, 1794200000000, json.dumps([symlink_trap.as_uri()]), "no-folder"),
            )
            conn.commit()

        proj_dir = self.paths.gemini_config_dir / "projects"
        (proj_dir / "corrupt.json").write_text("{bad json", encoding="utf-8")
        (proj_dir / "no-project.json").write_text(json.dumps({"id": "no-project", "folders": [{"uri": archive_nested.as_uri()}]}), encoding="utf-8")
        conv_dir = self.paths.agy_data_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        (conv_dir / "00000000-0000-7000-8000-000000000009.db").write_text("not sqlite", encoding="utf-8")

        discovered_paths = {p for p, _ in discover_workspaces(self.paths, seed_workspaces=[archive_direct, priv_nested, symlink_trap, self.ws_alpha])}
        self.assertEqual(discovered_paths, {self.ws_alpha.resolve(), self.ws_beta.resolve(), ws_gamma.resolve()})

        svc = WorkbenchService(self.paths)
        snap = svc.get_snapshot()
        self.assertEqual(snap.stage, "stage_1")
        self.assertFalse(snap.capabilities["remote_message_send"])
        self.assertTrue(snap.capabilities["readonly_history"])
        self.assertEqual({Path(w.workspace_path) for w in snap.workspaces}, discovered_paths)

    def test_2_union_find_clustering_and_prefix_dedup(self) -> None:
        binding = self.bridge.pair_workspace(self.ws_alpha)
        t_cur = self._seed_codex(
            binding, "01960000-0000-7000-8000-000000000201",
            _turn_pair(HostKind.CODEX, "tc1", "2026-11-14T01:00:00Z", "Cursor question", "Cursor answer"),
            title="Paired Cursor Work",
        )
        c_cur = self._seed_agy(
            binding, "11111111-1111-7111-8111-000000000201",
            _turn_pair(HostKind.ANTIGRAVITY, "tc1", "2026-11-14T01:00:00Z", "Cursor question", "Cursor answer"),
            title="Paired Cursor Work",
        )
        cur_dir = self.paths.sync_state_dir / "cursors"
        cur_dir.mkdir(parents=True, exist_ok=True)
        (cur_dir / "pair.json").write_text(json.dumps({
            "workspace_uri": str(binding.workspace_uri),
            "cascade_id": str(c_cur),
            "codex_thread_id": str(t_cur),
        }), encoding="utf-8")
        (cur_dir / "broken.json").write_text("{", encoding="utf-8")

        prov_bytes = json.dumps({"content_item_kinds": ["agents_md.instructions"], "content_item_metadata": [{"provenance": {"type": "agents_md"}}]}).encode("utf-8")
        injected_anchor = EventOriginAnchor(
            origin_host=HostKind.CODEX,
            turn_id=TurnId("h-wrap"),
            timestamp_iso="2026-11-14T01:59:00Z",
            host_extensions=(HostExtensionEnvelope(HostKind.CODEX, "codex_message_provenance", prov_bytes),),
        )
        c1_events = (
            [UserInputEvent(kind="user_input", anchor=injected_anchor, query_text="AGENTS harness instructions wrapper")]
            + _turn_pair(HostKind.CODEX, "h-t1", "2026-11-14T02:00:00Z", "Inspect oauth flow", "Checked oauth client", cmd="pytest tests/auth.py")
            + _turn_pair(HostKind.CODEX, "h-t2", "2026-11-14T02:01:00Z", "Fix refresh bug", "Patched token refresh")
        )
        t1 = self._seed_codex(binding, "01960000-0000-7000-8000-000000000301", c1_events, title="OAuth Refresh Work")

        a1_events = (
            _turn_pair(HostKind.ANTIGRAVITY, "h-t1", "2026-11-14T02:00:00Z", "Inspect oauth flow", "[Imported History] Checked oauth client")
            + _turn_pair(HostKind.ANTIGRAVITY, "h-t2", "2026-11-14T02:01:00Z", "Fix refresh bug", "[Imported History] Patched token refresh")
            + _turn_pair(HostKind.ANTIGRAVITY, "h-t3", "2026-11-14T02:02:00Z", "Verify pkce verifier", "Verified pkce challenge")
        )
        a1 = self._seed_agy(binding, "11111111-1111-7111-8111-000000000301", a1_events, title="OAuth Refresh Work")

        c2_events = (
            _turn_pair(HostKind.CODEX, "h-t1", "2026-11-14T02:00:00Z", "Inspect oauth flow", "Imported")
            + _turn_pair(HostKind.CODEX, "h-t2", "2026-11-14T02:01:00Z", "Fix refresh bug", "Imported")
            + _turn_pair(HostKind.CODEX, "h-t3", "2026-11-14T02:02:00Z", "Verify pkce verifier", "Imported")
        )
        t2 = self._seed_codex(binding, "01960000-0000-7000-8000-000000000302", c2_events, title="OAuth Refresh Work")

        self._write_receipt("r1", self.ws_alpha, "codex", str(t1), "antigravity", str(a1), chain_id="chain-oauth", chain_depth=0, seed_turns=2, created_at="2026-11-14T02:01:30Z")
        self._write_receipt("r2", self.ws_alpha, "antigravity", str(a1), "codex", str(t2), chain_id="chain-oauth", chain_depth=1, seed_turns=3, created_at="2026-11-14T02:03:00Z")

        svc = WorkbenchService(self.paths, ttl_seconds=60.0)
        snap = svc.get_snapshot()
        self.assertEqual(len(snap.works), 2)

        oauth_work = next(w for w in snap.works if w.title == "OAuth Refresh Work")
        self.assertEqual(oauth_work.session_count, 3)
        self.assertEqual(oauth_work.turn_count, 3)
        self.assertEqual([(t.host, t.session_id, t.user_text) for t in oauth_work.turns], [
            ("codex", str(t1), "Inspect oauth flow"),
            ("codex", str(t1), "Fix refresh bug"),
            ("antigravity", str(a1), "Verify pkce verifier"),
        ])
        self.assertIn("pytest tests/auth.py", oauth_work.turns[0].tool_summary)
        self.assertEqual(oauth_work.active_host, "antigravity")
        self.assertEqual(oauth_work.latest_source_id, str(a1))
        self.assertEqual(oauth_work.latest_direction, "agy_to_codex")
        self.assertEqual(oauth_work.chain_id, "chain-oauth")
        self.assertEqual(oauth_work.chain_depth, 1)
        baton = next(s for s in oauth_work.sessions if s.session_id == str(t2))
        self.assertTrue(baton.is_baton_target)

        cursor_work = next(w for w in snap.works if w.title == "Paired Cursor Work")
        self.assertEqual(cursor_work.session_count, 2)
        self.assertEqual(cursor_work.turn_count, 1)

    def test_3_search_draft_render_and_security_gates(self) -> None:
        binding = self.bridge.pair_workspace(self.ws_alpha)
        long_asst = " ".join(["prefix context"] * 8) + " exact oauth token verifier details " + " ".join(["trailing verification note"] * 8)
        t1 = self._seed_codex(
            binding, "01960000-0000-7000-8000-000000000401",
            _turn_pair(HostKind.CODEX, "st1", "2026-11-14T03:00:00Z", "Implement exact oauth token verifier", long_asst),
            title="Search Target Work",
        )
        a1 = self._seed_agy(
            binding, "11111111-1111-7111-8111-000000000401",
            _turn_pair(HostKind.ANTIGRAVITY, "st1", "2026-11-14T03:00:00Z", "Implement exact oauth token verifier", long_asst)
            + _turn_pair(HostKind.ANTIGRAVITY, "st2", "2026-11-14T03:05:00Z", "verifier token check in antigravity", "Confirmed agy pkce check passes cleanly"),
            title="Search Target Work",
        )
        self._write_receipt("s1", self.ws_alpha, "codex", str(t1), "antigravity", str(a1), seed_turns=1)

        svc = WorkbenchService(self.paths)
        server = WorkbenchServer(svc, ui_path=self.ui_file, port=0, idle_timeout_sec=120, max_lifetime_sec=300)
        server.start()
        try:
            lease = load_workbench_lease(self.paths)
            self.assertIsNotNone(lease)
            assert lease is not None
            lease_path = self.paths.sync_state_dir / "workbench" / "server.json"
            self.assertNotIn(server.token, lease_path.read_text(encoding="utf-8"))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(lease_path).st_mode), 0o600)

            st, hdrs, raw = _http_raw(server.port, "GET", "/", headers={"Host": f"127.0.0.1:{server.port}"})
            self.assertEqual(st, 200)
            self.assertEqual(hdrs.get("cache-control"), "no-store")
            html_body = raw.decode("utf-8")
            self.assertIn(f'<meta name="workbench-token" content="{server.token}">', html_body)
            self.assertIn('<meta name="workbench-mode" content="stage1-live">', html_body)
            self.assertEqual(hashlib.sha256(self.ui_file.read_bytes()).hexdigest(), self.ui_digest_before)

            st_health, _, raw_health = _http_raw(server.port, "GET", "/api/health", headers={"Host": f"localhost:{server.port}"})
            self.assertEqual(st_health, 200)
            self.assertFalse(json.loads(raw_health)["remote_message_send"])

            tok_hdr = {"Host": f"127.0.0.1:{server.port}", "X-Workbench-Token": server.token}
            self.assertEqual(_http_raw(server.port, "GET", "/api/snapshot", headers={"Host": f"127.0.0.1:{server.port}"})[0], 401)
            self.assertEqual(_http_raw(server.port, "GET", "/api/snapshot", headers={"Host": f"127.0.0.1:{server.port}", "X-Workbench-Token": "wrong"})[0], 401)
            self.assertEqual(_http_raw(server.port, "GET", "/api/snapshot", headers={"Host": f"evil.example:{server.port}", "X-Workbench-Token": server.token})[0], 403)
            self.assertEqual(_http_raw(server.port, "GET", "/api/snapshot", headers={**tok_hdr, "Origin": "http://evil.example:3000"})[0], 403)
            self.assertEqual(_http_raw(server.port, "GET", "/api/snapshot", headers={**tok_hdr, "Origin": "null"})[0], 403)
            self.assertEqual(_http_raw(server.port, "GET", "/api/snapshot", headers={**tok_hdr, "Sec-Fetch-Site": "cross-site"})[0], 403)
            self.assertEqual(_http_raw(server.port, "GET", "/api/snapshot", headers={**tok_hdr, "Origin": f"http://localhost:{server.port}", "Sec-Fetch-Site": "same-origin"})[0], 200)

            st_snap, _, snap_raw = _http_raw(server.port, "GET", "/api/snapshot", headers=tok_hdr)
            self.assertEqual(st_snap, 200)
            work = json.loads(snap_raw)["works"][0]
            work_id = work["work_id"]
            self.assertEqual(_http_raw(server.port, "GET", f"/api/works/{work_id}", headers=tok_hdr)[0], 200)

            _, _, s_all = _http_raw(server.port, "GET", "/api/search?q=oauth+token+verifier&scope=all", headers=tok_hdr)
            all_hits = json.loads(s_all)["hits"]
            self.assertEqual({h["host"] for h in all_hits}, {"codex", "antigravity"})
            self.assertGreater(all_hits[0]["score"], 100)
            _, _, s_rev = _http_raw(server.port, "GET", "/api/search?q=verifier+token&scope=all", headers=tok_hdr)
            rev_hits = json.loads(s_rev)["hits"]
            self.assertGreater(all_hits[0]["score"], rev_hits[-1]["score"])
            self.assertTrue(any("..." in h["snippet"] for h in all_hits))
            _, _, s_codex = _http_raw(server.port, "GET", "/api/search?q=oauth+token+verifier&scope=codex", headers=tok_hdr)
            self.assertTrue(all(h["host"] == "codex" for h in json.loads(s_codex)["hits"]))
            for agy_scope in ("agy", "antigravity"):
                _, _, s_agy = _http_raw(server.port, "GET", f"/api/search?q=pkce+check&scope={agy_scope}", headers=tok_hdr)
                agy_hits = json.loads(s_agy)["hits"]
                self.assertTrue(agy_hits and all(h["host"] == "antigravity" for h in agy_hits))

            draft_payload = {
                "work_id": work_id,
                "target_hint": "codex",
                "note": "Continue from PKCE check",
                "citations": [{"turn_id": "st2", "label": "AGY PKCE"}],
            }
            st_draft, _, draft_raw = _http_raw(
                server.port, "POST", "/api/draft/render",
                headers={**tok_hdr, "Content-Type": "application/json"},
                body=json.dumps(draft_payload).encode("utf-8"),
            )
            self.assertEqual(st_draft, 200)
            draft_json = json.loads(draft_raw)
            self.assertFalse(draft_json["delivery_executed"])
            self.assertEqual(draft_json["stage"], "stage_1_copy_only")
            self.assertIn("[Antigravity]", draft_json["markdown"])
            self.assertIn("Continue from PKCE check", draft_json["markdown"])

            self.assertEqual(
                _http_raw(
                    server.port, "POST", "/api/draft/render", headers=tok_hdr,
                    body=json.dumps({"work_id": work_id, "note": "   ", "citations": []}).encode("utf-8"),
                )[0],
                422,
            )
            self.assertEqual(_http_raw(server.port, "POST", "/api/draft/render", headers=tok_hdr, body=b"{not-json")[0], 400)
            self.assertEqual(_http_raw(server.port, "POST", "/api/draft/render", headers=tok_hdr, body=b"x" * 1_048_600)[0], 413)
            self.assertEqual(_http_raw(server.port, "POST", "/api/send", headers=tok_hdr, body=b"{}")[0], 404)
            self.assertEqual(_http_raw(server.port, "GET", "/api/works/missing-work", headers=tok_hdr)[0], 404)
            self.assertEqual(_http_raw(server.port, "GET", "/api/search?q=oauth&scope=bogus", headers=tok_hdr)[0], 400)
            self.assertEqual(_http_raw(server.port, "GET", "/api/health", headers={"Host": f"[::1]:{server.port}"})[0], 200)
            self.assertEqual(_http_raw(server.port, "GET", "/api/health", headers={"Host": "[::1]:9999"})[0], 403)
            ws_id = work["workspace_id"]
            st_f, _, raw_f = _http_raw(server.port, "GET", f"/api/snapshot?workspace_id={ws_id}&refresh=1", headers=tok_hdr)
            self.assertEqual(st_f, 200)
            self.assertEqual(json.loads(raw_f)["selected_workspace_id"], ws_id)
        finally:
            server.stop()

    def test_4_http_handoff_cache_invalidation_and_conflicts(self) -> None:
        binding = self.bridge.pair_workspace(self.ws_alpha)
        t1 = self._seed_codex(
            binding, "01960000-0000-7000-8000-000000000501",
            _turn_pair(HostKind.CODEX, "ht1", "2026-11-14T04:00:00Z", "Start baton", "Completed baton work"),
            title="HTTP Handoff Work",
        )
        c_only = self.bridge.registry.ensure_codex_binding(self.ws_beta)
        self._seed_codex(c_only, "01960000-0000-7000-8000-000000000502", _turn_pair(HostKind.CODEX, "ut1", "2026-11-14T04:00:00Z", "Unpaired", "Answer"))

        svc = WorkbenchService(self.paths, ttl_seconds=60.0)
        server = WorkbenchServer(svc, ui_path=self.root / "missing-ui.html", port=0, idle_timeout_sec=120, max_lifetime_sec=300)
        server.start()
        try:
            tok_hdr = {"Host": f"127.0.0.1:{server.port}", "X-Workbench-Token": server.token}
            self.assertEqual(_http_raw(server.port, "GET", "/", headers={"Host": f"127.0.0.1:{server.port}"})[0], 404)
            snap0 = json.loads(_http_raw(server.port, "GET", "/api/snapshot", headers=tok_hdr)[2])
            ws_alpha_card = next(w for w in snap0["workspaces"] if Path(w["workspace_path"]) == self.ws_alpha.resolve())
            work0 = next(w for w in snap0["works"] if w["workspace_id"] == ws_alpha_card["workspace_id"])
            self.assertEqual(len(work0["handoffs"]), 0)

            with self._mock_live_daemons():
                st_ok, _, raw_ok = _http_raw(
                    server.port, "POST", "/api/handoff", headers=tok_hdr,
                    body=json.dumps({"workspace_id": ws_alpha_card["workspace_id"], "latest": True}).encode("utf-8"),
                )
                self.assertEqual(st_ok, 200)
                rec = json.loads(raw_ok)
                self.assertEqual(rec["status"], "ready")
                self.assertEqual(rec["source_id"], str(t1))

                _, _, raw_work1 = _http_raw(server.port, "GET", f"/api/works/{work0['work_id']}", headers=tok_hdr)
                work1 = json.loads(raw_work1)
                self.assertEqual(len(work1["handoffs"]), 1)
                self.assertEqual(work1["session_count"], 2)

                st_chain, _, raw_chain = _http_raw(
                    server.port, "POST", "/api/handoff", headers=tok_hdr,
                    body=json.dumps({"workspace": str(self.ws_alpha), "chain": True}).encode("utf-8"),
                )
                self.assertEqual(st_chain, 200)
                self.assertEqual(json.loads(raw_chain)["status"], "no_op")

            self.assertEqual(
                _http_raw(server.port, "POST", "/api/handoff", headers=tok_hdr, body=json.dumps({"workspace": str(self.ws_beta), "latest": True}).encode("utf-8"))[0],
                422,
            )
            self.assertEqual(
                _http_raw(server.port, "POST", "/api/handoff", headers=tok_hdr, body=json.dumps({"workspace": str(self.ws_alpha)}).encode("utf-8"))[0],
                422,
            )
            busy_err = SyncBusyLockError("busy", host="codex", remediation=["close codex"])
            pending_rec = HandoffReceipt(
                workspace=str(self.ws_alpha), source_host="codex", source_id=str(t1),
                source_digest="abc", target_host="antigravity", target_id=None,
                title="Pending", status="pending", staging_path=str(self.root / "pending.json"),
            )
            pending_err = HandoffPendingError(pending_rec, self.root / "pending.json")
            with mock.patch("codex_agy_sync.bridge.handoff_resolve.handoff_session", side_effect=busy_err):
                with self._mock_live_daemons():
                    st_busy, _, raw_busy = _http_raw(
                        server.port, "POST", "/api/handoff", headers=tok_hdr,
                        body=json.dumps({"workspace": str(self.ws_alpha), "source_id": str(t1)}).encode("utf-8"),
                    )
                    self.assertEqual(st_busy, 409)
                    self.assertEqual(json.loads(raw_busy)["error"], "sync_busy_lock")
            with mock.patch("codex_agy_sync.bridge.handoff_resolve.resolve_handoff_request", side_effect=pending_err):
                st_pend, _, raw_pend = _http_raw(
                    server.port, "POST", "/api/handoff", headers=tok_hdr,
                    body=json.dumps({"workspace": str(self.ws_alpha), "latest": True}).encode("utf-8"),
                )
                self.assertEqual(st_pend, 409)
                self.assertEqual(json.loads(raw_pend)["error"], "handoff_pending")
        finally:
            server.stop()

    def test_5_cli_serve_status_stop_and_idle_reclamation(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self._run_cli(["serve", "--status", "--stop"])

        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        cmd = [
            sys.executable, str(self.cli_bin),
            *self._base_cli_args(),
            "serve", "-w", str(self.ws_alpha), "--ui", str(self.ui_file), "--port", "0", "--idle-timeout", "30",
        ]
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env) as proc:
            try:
                assert proc.stdout is not None
                startup_line = proc.stdout.readline().strip()
                info = json.loads(startup_line)
                self.assertEqual(info["pid"], proc.pid)
                self.assertEqual(info["stage"], "stage_1")

                rc_st, out_st, _ = self._run_cli(["serve", "--status"])
                self.assertEqual(rc_st, 0)
                status_json = json.loads(out_st)
                self.assertTrue(status_json["running"])
                self.assertEqual(status_json["pid"], proc.pid)

                rc_stop, out_stop, _ = self._run_cli(["serve", "--stop"])
                self.assertEqual(rc_stop, 0)
                self.assertTrue(json.loads(out_stop)["stopped"])
                self.assertEqual(proc.wait(timeout=5.0), 0)
                self.assertIsNone(load_workbench_lease(self.paths))
                self.assertFalse(query_workbench_status(self.paths)["running"])
                self.assertFalse(stop_workbench_server(self.paths)["running"])
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3.0)

        idle_cmd = [
            sys.executable, str(self.cli_bin),
            *self._base_cli_args(),
            "serve", "--ui", str(self.ui_file), "--port", "0", "--idle-timeout", "1", "--max-lifetime", "20",
        ]
        with subprocess.Popen(idle_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env) as idle_proc:
            try:
                assert idle_proc.stdout is not None
                idle_info = json.loads(idle_proc.stdout.readline().strip())
                self.assertEqual(_http_raw(idle_info["port"], "GET", "/api/health", headers={"Host": f"127.0.0.1:{idle_info['port']}"})[0], 200)
                rc_idle = idle_proc.wait(timeout=5.0)
                self.assertEqual(rc_idle, 0)
                self.assertIsNone(load_workbench_lease(self.paths))
            finally:
                if idle_proc.poll() is None:
                    idle_proc.kill()
                    idle_proc.wait(timeout=3.0)

        life_srv = WorkbenchServer(WorkbenchService(self.paths), ui_path=self.ui_file, port=0, idle_timeout_sec=0, max_lifetime_sec=1)
        life_srv.start()
        try:
            self.assertTrue(life_srv.wait(timeout=4.0))
        finally:
            life_srv.stop()
        self.assertIsNone(load_workbench_lease(self.paths))
