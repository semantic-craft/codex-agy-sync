from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_agy_sync import HostEnvironmentPaths, SyncBridge, TransportMode
from codex_agy_sync.bridge.handoff import HandoffReceipt
from codex_agy_sync.bridge.registry import ProjectRegistry
from codex_agy_sync.bridge.transport import DefaultHostTransport
from codex_agy_sync.cli import main as cli_main
from codex_agy_sync.ledger import (
    AssistantTextEvent,
    CascadeId,
    CodexThreadId,
    EventOriginAnchor,
    HostKind,
    TrajectoryLedger,
    TurnId,
    UserInputEvent,
    parse_workspace_identity,
)
from codex_agy_sync.workbench import (
    WorkbenchLease,
    WorkbenchServer,
    WorkbenchService,
    load_workbench_lease,
    query_workbench_status,
    run_serve_server,
    stop_workbench_server,
)
from codex_agy_sync.workbench._lease import _write_lease_file
from codex_agy_sync.workbench._discovery import (
    _is_sensitive_excluded_path,
    discover_workspaces,
    normalize_workspace_spec,
)
from codex_agy_sync.workbench._handler import (
    _inject_html_metas,
    _validate_host_header,
    _validate_origin_header,
)
from codex_agy_sync.workbench.indexer import _build_work_card, _classify_session_links
from codex_agy_sync.workbench.models import TimelineTurn


ARTIFACTS_DIR = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync"


def _make_sandbox(tmp_root: Path) -> tuple[Path, HostEnvironmentPaths]:
    sb = tmp_root / "sandbox"
    paths = HostEnvironmentPaths(
        gemini_config_dir=sb / "gemini-config",
        agy_data_dir=sb / "agy-data",
        codex_home_dir=sb / "codex-home",
        sync_state_dir=sb / "sync-state",
        agy_cli_data_dir=None,
        codex_app_server_socket=None,
    )
    for d in (
        paths.gemini_config_dir / "projects",
        paths.agy_data_dir / "conversations",
        paths.codex_home_dir / "sessions",
        paths.sync_state_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    return sb, paths


def _seed_turn_events(host: HostKind, turn_id: str, ts_iso: str, user_text: str, asst_text: str) -> list[object]:
    anchor = EventOriginAnchor(origin_host=host, turn_id=TurnId(turn_id), timestamp_iso=ts_iso)
    return [
        UserInputEvent(kind="user_input", anchor=anchor, query_text=user_text),
        AssistantTextEvent(kind="assistant_text", anchor=anchor, text=asst_text),
    ]


def _seed_codex_thread_row(
    paths: HostEnvironmentPaths,
    thread_id: str,
    cwd: str,
    title: str,
) -> None:
    db_path = paths.codex_home_dir / "state_5.sqlite"
    rollout = paths.codex_home_dir / "sessions" / f"rollout-{thread_id}.jsonl"
    rollout.write_text("", encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS project_roots (path TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL, "
            "cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL, "
            "approval_mode TEXT NOT NULL, has_user_event INTEGER NOT NULL, first_user_message TEXT NOT NULL, "
            "archived INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO threads "
            "(id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, "
            "sandbox_policy, approval_mode, has_user_event, first_user_message, archived) "
            "VALUES (?, ?, 1763082000, 1763082000, 'desktop', 'openai', ?, ?, 'workspace-write', 'on-request', 1, ?, 0)",
            (thread_id, str(rollout), cwd, title, title),
        )
        conn.commit()
    finally:
        conn.close()


def _raw_http_exchange(port: int, payload: bytes) -> tuple[int, dict[str, str], bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        sock.sendall(payload)
        chunks: list[bytes] = []
        while True:
            buf = sock.recv(65536)
            if not buf:
                break
            chunks.append(buf)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


class WorkbenchInterrogateFixesTest(unittest.TestCase):
    def _new_sandbox(self) -> tuple[Path, Path, HostEnvironmentPaths]:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        td = tempfile.TemporaryDirectory(prefix="wb-interrogate-", dir=ARTIFACTS_DIR)
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        sb, paths = _make_sandbox(root)
        return root, sb, paths

    def test_f1_service_default_uses_discover(self) -> None:
        _, sb, expected = self._new_sandbox()
        env = {
            "HOME": str(sb),
            "GEMINI_CONFIG_DIR": str(expected.gemini_config_dir),
            "ANTIGRAVITY_DATA_DIR": str(expected.agy_data_dir),
            "CODEX_HOME": str(expected.codex_home_dir),
            "CODEX_AGY_SYNC_STATE_DIR": str(expected.sync_state_dir),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ANTIGRAVITY_CLI_DATA_DIR", None)
            svc = WorkbenchService()
        self.assertEqual(svc.paths.codex_home_dir, expected.codex_home_dir.resolve())
        self.assertEqual(svc.paths.sync_state_dir, expected.sync_state_dir.resolve())
        snap = svc.get_snapshot()
        self.assertEqual(snap.workspaces, ())

    def test_f2_darwin_case_insensitive_segment_boundary_exclusion(self) -> None:
        _, sb, paths = self._new_sandbox()
        archive_upper = sb / "Archive" / "secret"
        archive_lower = sb / "archive" / "lower-secret"
        priv_dir = sb / ".private_keys" / "keys"
        archive2 = sb / "Archive2" / "legit"
        priv2 = sb / ".private_keys2" / "legit"
        symlink_escape = sb / "workspaces" / "link-into-archive"
        for d in (archive_upper, archive_lower, priv_dir, archive2, priv2, symlink_escape.parent):
            d.mkdir(parents=True, exist_ok=True)
        symlink_escape.symlink_to(archive_upper)

        with mock.patch.object(sys, "platform", "darwin"):
            self.assertTrue(_is_sensitive_excluded_path(archive_lower, sb))
            self.assertTrue(_is_sensitive_excluded_path(sb / "ARCHIVE" / "deep", sb))
            self.assertTrue(_is_sensitive_excluded_path(sb / ".PRIVATE_KEYS" / "pem", sb))
            self.assertTrue(_is_sensitive_excluded_path(symlink_escape, sb))
            self.assertFalse(_is_sensitive_excluded_path(archive2, sb))
            self.assertFalse(_is_sensitive_excluded_path(priv2, sb))

            _seed_codex_thread_row(paths, "01960000-0000-7000-8000-00000000d001", str(archive_upper), "upper")
            _seed_codex_thread_row(paths, "01960000-0000-7000-8000-00000000d002", str(sb / "archive" / "lower-secret"), "lower")
            _seed_codex_thread_row(paths, "01960000-0000-7000-8000-00000000d003", str(priv_dir), "priv")
            _seed_codex_thread_row(paths, "01960000-0000-7000-8000-00000000d004", str(symlink_escape), "sym")
            _seed_codex_thread_row(paths, "01960000-0000-7000-8000-00000000d005", str(archive2), "legit")

            discovered = [str(ws_path) for ws_path, _ in discover_workspaces(paths)]
        self.assertEqual(discovered, [str(archive2.resolve())])

    def test_f3_rejects_excluded_and_nonexistent_handoff_workspaces(self) -> None:
        _, sb, paths = self._new_sandbox()
        archive_ws = sb / "Archive" / "secret-proj"
        archive_lower_ws = sb / "archive" / "lower-proj"
        priv_ws = sb / ".private_keys" / "key-proj"
        symlink_ws = sb / "workspaces" / "sym-secret"
        missing_ws = sb / "workspaces" / "does-not-exist"
        ghost_archive = sb / "Archive" / "nonexistent-secret"
        for d in (archive_ws, archive_lower_ws, priv_ws, symlink_ws.parent):
            d.mkdir(parents=True, exist_ok=True)
        symlink_ws.symlink_to(archive_ws)

        svc = WorkbenchService(paths)
        server = WorkbenchServer(svc, port=0)
        server.start()
        self.addCleanup(server.stop)
        port = server.port
        assert port is not None

        with mock.patch.object(sys, "platform", "darwin"):
            for bad_ws in (archive_ws, archive_lower_ws, priv_ws, symlink_ws, missing_ws, ghost_archive):
                with self.assertRaises(ValueError):
                    svc.trigger_handoff(workspace=str(bad_ws), pair_if_offline=True)
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
                conn.request(
                    "POST",
                    "/api/handoff",
                    body=json.dumps({"workspace": str(bad_ws), "pair_if_offline": True}),
                    headers={
                        "Content-Type": "application/json",
                        "X-Workbench-Token": server.token,
                        "Host": f"127.0.0.1:{port}",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                )
                resp = conn.getresponse()
                payload = json.loads(resp.read().decode("utf-8"))
                conn.close()
                self.assertEqual(resp.status, 422, f"Expected 422 for {bad_ws}, got {resp.status}: {payload}")
        self.assertFalse(missing_ws.exists())

    def test_f4_and_f15_work_id_invariant_and_none_seed_untouched(self) -> None:
        _, sb, paths = self._new_sandbox()
        ws = sb / "workspaces" / "repo-f4"
        ws.mkdir(parents=True)
        bridge = SyncBridge(paths=paths)
        binding = bridge.pair_workspace(ws)
        ws_id = "ws-f4"
        a0_key = ("antigravity", "00000000-0000-4000-8000-00000000a001")
        t1_key = ("codex", "01960000-0000-7000-8000-00000000c001")
        a1_key = ("antigravity", "00000000-0000-4000-8000-00000000a002")
        t2_key = ("codex", "01960000-0000-7000-8000-00000000c002")

        r_c2a = HandoffReceipt(
            workspace=str(ws),
            source_host="codex",
            source_id=t1_key[1],
            source_digest="d1",
            target_host="antigravity",
            target_id=a0_key[1],
            title="Pair",
            status="ready",
            staging_path="",
            created_at_iso="2026-11-14T00:00:00Z",
            target_seed_turn_count=None,
        )
        sessions = {
            t1_key: {
                "title": "Codex root",
                "iso": "2026-11-14T00:01:00Z",
                "epoch": 100.0,
                "count": 2,
                "turns": [
                    TimelineTurn(
                        turn_id="t1",
                        seq=1,
                        host="codex",
                        session_id=t1_key[1],
                        session_title="Codex root",
                        timestamp_iso="2026-11-14T00:01:00Z",
                        user_text="u1",
                        assistant_text="a1",
                        tool_summary=(),
                        is_inflight=False,
                        has_thinking=False,
                    )
                ],
            },
            a0_key: {"title": "AGY target", "iso": "2026-11-14T00:02:00Z", "epoch": 200.0, "count": 0, "turns": []},
        }
        links_empty, untouched_empty = _classify_session_links([t1_key, a0_key], sessions, [("r1", r_c2a, 100.0)])
        self.assertIn(a0_key, untouched_empty)
        self.assertTrue(any(link.session_id == a0_key[1] and link.is_baton_target for link in links_empty))

        card_initial = _build_work_card(
            ws_id,
            binding,
            True,
            [t1_key, a0_key],
            sessions,
            [("r1", r_c2a, 100.0)],
            {t1_key: [a0_key], a0_key: [t1_key]},
        )
        self.assertEqual(card_initial.active_host, "codex")
        self.assertEqual(card_initial.latest_source_id, t1_key[1])

        sessions[a0_key]["turns"] = [
            TimelineTurn(
                turn_id="t2",
                seq=1,
                host="antigravity",
                session_id=a0_key[1],
                session_title="AGY target",
                timestamp_iso="2026-11-14T00:03:00Z",
                user_text="u2",
                assistant_text="a2",
                tool_summary=(),
                is_inflight=False,
                has_thinking=False,
            )
        ]
        _, untouched_after_turn = _classify_session_links([t1_key, a0_key], sessions, [("r1", r_c2a, 100.0)])
        self.assertNotIn(a0_key, untouched_after_turn)

        r_next = HandoffReceipt(
            workspace=str(ws),
            source_host="antigravity",
            source_id=a0_key[1],
            source_digest="d3",
            target_host="codex",
            target_id=t2_key[1],
            title="Pair",
            status="ready",
            staging_path="",
            created_at_iso="2026-11-14T00:06:00Z",
            target_seed_turn_count=None,
        )
        sessions[t2_key] = {"title": "Codex hop 2", "iso": "2026-11-14T00:06:00Z", "epoch": 300.0, "count": 1, "turns": []}
        card_roundtrip = _build_work_card(
            ws_id,
            binding,
            True,
            [t1_key, a0_key, t2_key],
            sessions,
            [("r1", r_c2a, 100.0), ("r2", r_next, 200.0)],
            {t1_key: [a0_key], a0_key: [t1_key]},
        )
        self.assertEqual(card_roundtrip.work_id, card_initial.work_id)

        card_cursor_base = _build_work_card(
            ws_id,
            binding,
            True,
            [t1_key, a0_key],
            sessions,
            [],
            {t1_key: [a0_key], a0_key: [t1_key]},
        )
        r_cursor_handoff = HandoffReceipt(
            workspace=str(ws),
            source_host="codex",
            source_id=t1_key[1],
            source_digest="d4",
            target_host="antigravity",
            target_id=a1_key[1],
            title="Pair",
            status="ready",
            staging_path="",
            created_at_iso="2026-11-14T00:07:00Z",
            target_seed_turn_count=1,
        )
        sessions[a1_key] = {"title": "AGY hop", "iso": "2026-11-14T00:07:00Z", "epoch": 400.0, "count": 1, "turns": []}
        card_cursor_after = _build_work_card(
            ws_id,
            binding,
            True,
            [t1_key, a0_key, a1_key],
            sessions,
            [("r3", r_cursor_handoff, 400.0)],
            {t1_key: [a0_key], a0_key: [t1_key]},
        )
        self.assertEqual(card_cursor_after.work_id, card_cursor_base.work_id)

    def test_f7_post_framing_and_transfer_encoding(self) -> None:
        _, _, paths = self._new_sandbox()
        svc = WorkbenchService(paths)
        server = WorkbenchServer(svc, port=0)
        server.start()
        self.addCleanup(server.stop)
        port = server.port
        assert port is not None
        base_hdr = (
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\n"
            f"X-Workbench-Token: {server.token}\r\n"
            "Connection: close\r\n"
        )
        cases = (
            (f"POST /api/draft/render HTTP/1.1\r\n{base_hdr}Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{{}}".encode(), 501),
            (f"POST /api/draft/render HTTP/1.1\r\n{base_hdr}\r\n".encode(), 411),
            (f"POST /api/draft/render HTTP/1.1\r\n{base_hdr}Content-Length: -5\r\n\r\n".encode(), 400),
            (f"POST /api/draft/render HTTP/1.1\r\n{base_hdr}Content-Length: nope\r\n\r\n".encode(), 400),
            (f"POST /api/draft/render HTTP/1.1\r\n{base_hdr}Content-Length: 2\r\nContent-Length: 2\r\n\r\n{{}}".encode(), 400),
            (f"POST /api/draft/render HTTP/1.1\r\n{base_hdr}Content-Length: 2000000\r\n\r\n".encode(), 413),
            (f"POST /api/draft/render HTTP/1.1\r\n{base_hdr}Content-Length: 2\r\n\r\n{{}}".encode(), 422),
        )
        for req_bytes, expected_status in cases:
            status, _, _ = _raw_http_exchange(port, req_bytes)
            self.assertEqual(status, expected_status)

    def test_f8_host_and_origin_validation_ipv6_and_delimiters(self) -> None:
        self.assertTrue(_validate_host_header("[::1]:8765", 8765))
        self.assertTrue(_validate_origin_header("http://[::1]:8765", 8765))
        for bad_host in ("127.0.0.1:8765?evil", "127.0.0.1:8765/foo", "user@127.0.0.1:8765", "127.0.0.1:8765#frag", "127.0.0.1:"):
            self.assertFalse(_validate_host_header(bad_host, 8765), bad_host)
        for bad_origin in ("http://127.0.0.1:8765?evil=1", "http://user@127.0.0.1:8765", "http://127.0.0.1:8765/path"):
            self.assertFalse(_validate_origin_header(bad_origin, 8765), bad_origin)

        _, _, paths = self._new_sandbox()
        server = WorkbenchServer(WorkbenchService(paths), port=0)
        server.start()
        self.addCleanup(server.stop)
        port = server.port
        assert port is not None

        ok_status, _, _ = _raw_http_exchange(
            port,
            f"GET /api/snapshot HTTP/1.1\r\nHost: [::1]:{port}\r\nOrigin: http://[::1]:{port}\r\nX-Workbench-Token: {server.token}\r\nConnection: close\r\n\r\n".encode(),
        )
        self.assertEqual(ok_status, 200)

        for bad_host in (f"127.0.0.1:{port}?evil=1", f"user@127.0.0.1:{port}"):
            status, _, _ = _raw_http_exchange(
                port,
                f"GET /api/health HTTP/1.1\r\nHost: {bad_host}\r\nConnection: close\r\n\r\n".encode(),
            )
            self.assertEqual(status, 403)

    def test_f9_stop_preserves_live_lease_and_handles_malformed_lease(self) -> None:
        _, _, paths = self._new_sandbox()
        lease_file = paths.sync_state_dir / "workbench" / "server.json"
        lease_file.parent.mkdir(parents=True, exist_ok=True)
        child_code = (
            "from http.server import BaseHTTPRequestHandler, HTTPServer; import json, os, signal\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        b = json.dumps({'status': 'ok', 'pid': os.getpid()}).encode()\n"
            "        self.send_response(200); self.send_header('Content-Type', 'application/json')\n"
            "        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)\n"
            "    def log_message(self, *args): pass\n"
            "s = HTTPServer(('127.0.0.1', 0), H)\n"
            "print(s.server_address[1], flush=True)\n"
            "s.serve_forever()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=5)) if proc.poll() is None else None)
        assert proc.stdout is not None and proc.stderr is not None
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stderr.close)
        unused_port = int(proc.stdout.readline().strip())
        _write_lease_file(
            lease_file,
            WorkbenchLease(
                pid=int(proc.pid),
                port=unused_port,
                url=f"http://127.0.0.1:{unused_port}",
                started_at_iso="2026-11-14T00:00:00Z",
                last_activity_iso="2026-11-14T00:00:00Z",
                idle_timeout_sec=1800,
                max_lifetime_sec=14400,
                ui_path=None,
            ),
        )
        res = stop_workbench_server(paths, timeout_sec=0.25)
        self.assertFalse(res["stopped"])
        self.assertTrue(res["running"])
        self.assertEqual(res["reason"], "stop_timeout")
        self.assertTrue(lease_file.is_file())

        proc.kill()
        proc.wait(timeout=5)
        dead_res = stop_workbench_server(paths, timeout_sec=0.5)
        self.assertFalse(dead_res["stopped"])
        self.assertTrue(dead_res["stale_cleaned"])
        self.assertFalse(lease_file.is_file())

        proc_clean = subprocess.Popen(
            [sys.executable, "-c", child_code.replace("signal.SIG_IGN", "signal.SIG_DFL")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: (proc_clean.kill(), proc_clean.wait(timeout=5)) if proc_clean.poll() is None else None)
        assert proc_clean.stdout is not None and proc_clean.stderr is not None
        self.addCleanup(proc_clean.stdout.close)
        self.addCleanup(proc_clean.stderr.close)
        clean_port = int(proc_clean.stdout.readline().strip())
        _write_lease_file(
            lease_file,
            WorkbenchLease(
                pid=int(proc_clean.pid),
                port=clean_port,
                url=f"http://127.0.0.1:{clean_port}",
                started_at_iso="2026-11-14T00:00:00Z",
                last_activity_iso="2026-11-14T00:00:00Z",
                idle_timeout_sec=1800,
                max_lifetime_sec=14400,
                ui_path=None,
            ),
        )
        clean_res = stop_workbench_server(paths, timeout_sec=1.5)
        self.assertTrue(clean_res["stopped"])
        self.assertFalse(lease_file.is_file())
        proc_clean.wait(timeout=5)

        lease_file.write_text("{broken-json", encoding="utf-8")
        self.assertEqual(query_workbench_status(paths)["reason"], "malformed_lease")
        lease_file.write_text(json.dumps({"pid": True, "port": 8765, "url": "http://127.0.0.1:8765"}), encoding="utf-8")
        self.assertEqual(query_workbench_status(paths)["reason"], "malformed_lease")
        malformed_stop = stop_workbench_server(paths)
        self.assertFalse(malformed_stop["stopped"])
        self.assertTrue(malformed_stop["stale_cleaned"])
        self.assertEqual(malformed_stop["reason"], "malformed_lease")
        self.assertFalse(lease_file.is_file())

    def test_f11_and_f19_unique_hit_ids_citation_targeting_dedup_and_newline_sanitization(self) -> None:
        _, sb, paths = self._new_sandbox()
        ws = sb / "workspaces" / "repo-f11"
        ws.mkdir(parents=True)
        bridge = SyncBridge(paths=paths)
        binding = bridge.pair_workspace(ws)
        transport = DefaultHostTransport(paths)
        locks = bridge.guard.probe_locks(None, None)

        a0 = CascadeId("00000000-0000-4000-8000-00000000b101")
        a0_events = _seed_turn_events(
            HostKind.ANTIGRAVITY,
            "shared-turn-1",
            "2026-11-14T01:00:00Z",
            "Inspect tokenflow in AGY",
            "AGY answered with unique_agy_excerpt for tokenflow",
        )
        a0_ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, a0_events, explicit_title_override="Shared Work")
        transport.apply_to_agy(binding, a0, a0_events, a0_ledger, locks, TransportMode.OFFLINE_STORAGE)

        t1 = CodexThreadId("01960000-0000-7000-8000-00000000b201")
        t1_events = _seed_turn_events(
            HostKind.CODEX,
            "shared-turn-1",
            "2026-11-14T02:00:00Z",
            "Inspect tokenflow in Codex",
            "Codex answered with unique_codex_excerpt for tokenflow",
        )
        t1_ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, t1_events, explicit_title_override="Shared Work")
        transport.apply_to_codex(binding, t1, t1_events, t1_ledger, locks, TransportMode.OFFLINE_STORAGE)

        handoffs_dir = paths.sync_state_dir / "handoffs"
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        receipt = HandoffReceipt(
            workspace=str(binding.workspace_path),
            source_host="antigravity",
            source_id=str(a0),
            source_digest="d1",
            target_host="codex",
            target_id=str(t1),
            title="Shared Work",
            status="ready",
            staging_path="",
            created_at_iso="2026-11-14T02:00:00Z",
        )
        (handoffs_dir / "0001-f11.json").write_text(json.dumps(receipt.__dict__), encoding="utf-8")

        svc = WorkbenchService(paths)
        hits_single = svc.search("tokenflow")
        hits_two = svc.search("tokenflow tokenflow")
        hits_repeated = svc.search("tokenflow " * 20)
        self.assertGreaterEqual(len(hits_single), 2)
        self.assertEqual(len({h.hit_id for h in hits_single}), len(hits_single))
        self.assertEqual(hits_two[0].score, hits_repeated[0].score)
        self.assertLessEqual(hits_repeated[0].score, hits_single[0].score)

        snap = svc.get_snapshot()
        work = snap.works[0]
        work_id = work.work_id
        agy_turn = next(t for t in work.turns if t.host == "antigravity")
        codex_turn = next(t for t in work.turns if t.host == "codex")
        self.assertEqual(agy_turn.turn_id, codex_turn.turn_id)

        draft_agy = svc.render_draft(
            work_id,
            "Check",
            citations=[
                {
                    "work_id": work_id,
                    "turn_id": agy_turn.turn_id,
                    "host": "agy",
                    "session_id": str(a0),
                    "label": "bad\n# Injected Heading\r\nlabel",
                }
            ],
        )
        self.assertIn("unique_agy_excerpt", draft_agy.markdown)
        self.assertNotIn("unique_codex_excerpt", draft_agy.markdown)
        self.assertNotIn("\n# Injected Heading", draft_agy.markdown)
        self.assertIn("bad # Injected Heading label", draft_agy.markdown)

        draft_codex = svc.render_draft(
            work_id,
            "Check",
            citations=[{"work_id": work_id, "turn_id": codex_turn.turn_id, "host": "codex", "session_id": str(t1)}],
        )
        self.assertIn("unique_codex_excerpt", draft_codex.markdown)
        self.assertNotIn("unique_agy_excerpt", draft_codex.markdown)

    def test_f12_project_resources_none_or_non_dict_tolerated(self) -> None:
        _, sb, paths = self._new_sandbox()
        valid_ws = sb / "workspaces" / "valid-ws"
        valid_ws.mkdir(parents=True)
        _seed_codex_thread_row(paths, "01960000-0000-7000-8000-00000000c112", str(valid_ws), "Valid")
        for bad_resources in (None, ["not-a-dict"], "str-val"):
            (paths.gemini_config_dir / "projects.json").write_text(
                json.dumps({"projects": {}, "projectResources": bad_resources}),
                encoding="utf-8",
            )
            discovered = discover_workspaces(paths)
            self.assertEqual([str(w) for w, _ in discovered], [str(valid_ws.resolve())])
            binding = ProjectRegistry(paths).resolve_readonly_binding(valid_ws)
            self.assertEqual(binding.workspace_path, parse_workspace_identity(valid_ws)[0])

    def test_f13_bad_json_types_corrupt_ui_and_broken_pipe(self) -> None:
        root, sb, paths = self._new_sandbox()
        bad_ui = root / "bad-ui.html"
        bad_ui.write_bytes(b"\xff\xfe\xfd")
        svc = WorkbenchService(paths)
        server = WorkbenchServer(svc, ui_path=bad_ui, port=0)
        server.start()
        self.addCleanup(server.stop)
        port = server.port
        assert port is not None

        status_ui, _, body_ui = _raw_http_exchange(
            port,
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode(),
        )
        self.assertEqual(status_ui, 500)
        self.assertEqual(json.loads(body_ui.decode("utf-8"))["error"], "ui_read_failed")

        for endpoint, payload in (
            ("/api/draft/render", []),
            ("/api/draft/render", {"work_id": 123, "note": "x"}),
            ("/api/draft/render", {"work_id": "w1", "note": ["bad"]}),
            ("/api/draft/render", {"work_id": "w1", "note": "x", "citations": [{"turn_id": 99}]}),
            ("/api/handoff", []),
            ("/api/handoff", {"workspace": 123}),
            ("/api/handoff", {"workspace": str(sb), "direction": 99}),
        ):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
            conn.request(
                "POST",
                endpoint,
                body=json.dumps(payload),
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-Token": server.token,
                    "Host": f"127.0.0.1:{port}",
                    "Origin": f"http://127.0.0.1:{port}",
                },
            )
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 400, f"{endpoint} {payload} -> {resp.status}")

        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
            sock.sendall(f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
        ok_status, _, _ = _raw_http_exchange(
            port,
            f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode(),
        )
        self.assertEqual(ok_status, 200)

    def test_f14_cli_bounds_activity_heartbeat_bind_collision_and_norm(self) -> None:
        for bad_args in (
            ["serve", "--port", "-1"],
            ["serve", "--port", "70000"],
            ["serve", "--idle-timeout", "-1"],
            ["serve", "--max-lifetime", "-5"],
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    cli_main(bad_args)
            self.assertNotEqual(ctx.exception.code, 0)

        _, sb, paths = self._new_sandbox()
        svc = WorkbenchService(paths)
        for kw in ({"port": -1}, {"port": 65536}, {"idle_timeout_sec": -1}, {"max_lifetime_sec": -1}):
            with self.assertRaises(ValueError):
                WorkbenchServer(svc, **kw)

        sym = sb / "workspaces" / "sym-link"
        target = sb / "Archive" / "t"
        target.mkdir(parents=True, exist_ok=True)
        sym.parent.mkdir(parents=True, exist_ok=True)
        sym.symlink_to(target)
        self.assertEqual(normalize_workspace_spec(sym), os.path.abspath(str(sym)))

        t0 = datetime(2026, 11, 14, 1, 0, 0, tzinfo=timezone.utc)
        now_box = [t0]

        def _fake_now_iso() -> str:
            return now_box[0].strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock.patch("codex_agy_sync.workbench.server._now_iso", side_effect=_fake_now_iso):
            server_a = WorkbenchServer(svc, port=0)
            server_a.start()
            self.addCleanup(server_a.stop)
            port_a = server_a.port
            assert port_a is not None
            initial_activity = server_a.health_payload()["last_activity_iso"]

            now_box[0] = t0 + timedelta(seconds=10)
            conn = http.client.HTTPConnection("127.0.0.1", port_a, timeout=5.0)
            conn.request(
                "GET",
                "/api/snapshot",
                headers={
                    "X-Workbench-Token": server_a.token,
                    "Host": f"127.0.0.1:{port_a}",
                    "Origin": f"http://127.0.0.1:{port_a}",
                },
            )
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()

            updated_activity = server_a.health_payload()["last_activity_iso"]
            self.assertGreater(updated_activity, initial_activity)
            lease_a = load_workbench_lease(paths)
            assert lease_a is not None
            self.assertEqual(lease_a.last_activity_iso, updated_activity)

        server_b = WorkbenchServer(svc, port=port_a)
        with self.assertRaises(OSError):
            server_b.start()
        lease_after_fail = load_workbench_lease(paths)
        assert lease_after_fail is not None
        self.assertEqual(lease_after_fail.port, port_a)

        with contextlib.redirect_stderr(io.StringIO()):
            exit_code = run_serve_server(paths, port=port_a)
        self.assertEqual(exit_code, 2)
        lease_after_cli_fail = load_workbench_lease(paths)
        assert lease_after_cli_fail is not None
        self.assertEqual(lease_after_cli_fail.port, port_a)

    def test_f18_inject_html_metas_ignores_comment_script_style(self) -> None:
        html = (
            "<!doctype html><html><head>"
            "<!-- </head> -->"
            "<script>const tag = '</head>';</script>"
            "<style>body::after { content: '</head>'; }</style>"
            "<title>T</title></HEAD><body></body></html>"
        )
        out = _inject_html_metas(html, "tok-123")
        self.assertEqual(out.count('name="workbench-token"'), 1)
        idx_meta = out.index('name="workbench-token"')
        idx_real_head = out.index("</HEAD>")
        self.assertGreater(idx_meta, out.index("</style>"))
        self.assertLess(idx_meta, idx_real_head)

        no_head = "<!-- </head> --><script>'</head>'</script><body>hi</body>"
        out_no_head = _inject_html_metas(no_head, "tok-456")
        self.assertTrue(out_no_head.startswith("<meta "))
        self.assertEqual(out_no_head.count('name="workbench-token"'), 1)


if __name__ == "__main__":
    unittest.main()
