from __future__ import annotations

import contextlib
import http.client
import importlib.resources
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

from codex_agy_sync import HostEnvironmentPaths, WorkbenchServer, WorkbenchService
from codex_agy_sync import cli as cli_mod
from codex_agy_sync.cli import _build_parser, main as cli_main
from codex_agy_sync.workbench.lease import default_workbench_ui_path


class WorkbenchPackagingTest(unittest.TestCase):
    def test_default_workbench_ui_path_resolves_to_packaged_html(self) -> None:
        ui_path = default_workbench_ui_path()
        self.assertTrue(ui_path.is_file(), f"Missing packaged UI HTML: {ui_path}")
        self.assertEqual(ui_path.name, "index.html")
        self.assertEqual(ui_path.parent.name, "ui")
        self.assertTrue(ui_path.as_posix().endswith("codex_agy_sync/workbench/ui/index.html"))

    def test_package_data_and_importlib_resources_hygiene(self) -> None:
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["version"], "0.4.0")
        pkg_data = pyproject["tool"]["setuptools"]["package-data"]["codex_agy_sync"]
        self.assertIn("workbench/ui/*.html", pkg_data)

        ui_resource = importlib.resources.files("codex_agy_sync").joinpath("workbench/ui/index.html")
        self.assertTrue(ui_resource.is_file())
        html_text = ui_resource.read_text(encoding="utf-8")

        for token in (
            "handoffPairIfOffline",
            "handoffIncludeInterrupted",
            "reconcilePendingHandoff",
            "createLiveController",
            "auto_pair",
            "include_interrupted",
            "/api/handoff/reconcile",
            "await loadWorkDetail(hit.work_id);",
            "preserveDraftState: true",
        ):
            self.assertIn(token, html_text)

        for retired in ("createOfflineController", "Atlas", "Orbit", "模拟发送给 AGY"):
            self.assertNotIn(retired, html_text)

        self.assertNotIn("/Users/", html_text)
        urls = re.findall(r"https?://[^\s\"'<>]+", html_text)
        self.assertTrue(all(url == "http://www.w3.org/2000/svg" for url in urls), f"Unexpected URLs: {urls}")

    def test_workbench_server_default_ui_serves_html_with_meta_token(self) -> None:
        artifacts = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync"
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="wb-pack-", dir=artifacts) as tmpdir:
            root = Path(tmpdir)
            paths = HostEnvironmentPaths(
                gemini_config_dir=root / "gemini-config",
                agy_data_dir=root / "agy-data",
                codex_home_dir=root / "codex-home",
                sync_state_dir=root / "sync-state",
            )
            server = WorkbenchServer(WorkbenchService(paths), port=0, ui_path=None)
            server.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5.0)
                try:
                    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{server.port}"})
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8", errors="replace")
                    content_type = resp.getheader("Content-Type", "")
                    x_frame = resp.getheader("X-Frame-Options")
                    csp = resp.getheader("Content-Security-Policy")
                finally:
                    conn.close()
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", content_type)
                self.assertEqual(x_frame, "DENY")
                self.assertEqual(csp, "frame-ancestors 'none'")
                self.assertIn(f'<meta name="workbench-token" content="{server.token}">', body)
                self.assertIn('<meta name="workbench-mode" content="stage1-live">', body)
                self.assertIn("handoffIncludeInterrupted", body)
            finally:
                server.stop()

    def test_cli_serve_help_and_ui_default_resolution(self) -> None:
        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), self.assertRaises(SystemExit) as ctx:
            cli_main(["serve", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = out_buf.getvalue()
        self.assertIn("--ui", help_text)
        self.assertIn("packaged default UI", help_text)

        parser = _build_parser()
        default_args = parser.parse_args(["serve"])
        self.assertIsNone(default_args.ui)
        self.assertEqual(default_args.ui or default_workbench_ui_path(), default_workbench_ui_path())

        custom_args = parser.parse_args(["serve", "--ui", "custom-workbench.html"])
        self.assertEqual(custom_args.ui, Path("custom-workbench.html"))

    def test_p2_missing_ui_early_cli_rejection_importlib_fallback_and_service_invalidation(self) -> None:
        import json
        from unittest import mock
        from codex_agy_sync import HandoffPendingError, HandoffReceipt

        artifacts = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync"
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="wb-p2-", dir=artifacts) as tmpdir:
            root = Path(tmpdir)
            paths = HostEnvironmentPaths(
                gemini_config_dir=root / "gemini-config",
                agy_data_dir=root / "agy-data",
                codex_home_dir=root / "codex-home",
                sync_state_dir=root / "sync-state",
            )
            missing_ui = root / "does-not-exist.html"
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf), mock.patch("webbrowser.open") as mock_open:
                rc = cli_main([
                    "--gemini-config-dir", str(paths.gemini_config_dir),
                    "--agy-data-dir", str(paths.agy_data_dir),
                    "--codex-home-dir", str(paths.codex_home_dir),
                    "--sync-state-dir", str(paths.sync_state_dir),
                    "serve", "--ui", str(missing_ui), "--open",
                ])
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(err_buf.getvalue())["error"], "file_not_found")
            mock_open.assert_not_called()
            self.assertFalse((paths.sync_state_dir / "workbench.json").exists())

            with mock.patch("codex_agy_sync.workbench.lease.importlib.resources.files", side_effect=ModuleNotFoundError("no pkg")):
                fallback_ui = default_workbench_ui_path()
            self.assertTrue(fallback_ui.is_file())
            self.assertEqual(fallback_ui.name, "index.html")

            ws = root / "ws-inval"
            ws.mkdir(parents=True, exist_ok=True)
            svc = WorkbenchService(paths, ttl_seconds=60.0)
            from codex_agy_sync import WorkbenchIndexer

            with mock.patch.object(WorkbenchIndexer, "invalidate") as mock_inv:
                with mock.patch("codex_agy_sync.workbench.service.execute_handoff", side_effect=RuntimeError("boom")):
                    with self.assertRaises(RuntimeError):
                        svc.trigger_handoff(workspace=str(ws), latest=True, auto_pair=True)
                self.assertEqual(mock_inv.call_count, 1)

                rec = HandoffReceipt(str(ws), "codex", "01970000-0000-7000-8000-000000000001", "d", "antigravity", None, "T", "pending", str(root / "s"), "e")
                with mock.patch("codex_agy_sync.workbench.service.execute_handoff", side_effect=HandoffPendingError(rec, root / "r.json")):
                    with self.assertRaises(HandoffPendingError):
                        svc.trigger_handoff(workspace=str(ws), latest=True, pair_if_offline=True)
                self.assertEqual(mock_inv.call_count, 2)

                with mock.patch("codex_agy_sync.workbench.service.execute_handoff", side_effect=RuntimeError("boom2")):
                    with self.assertRaises(RuntimeError):
                        svc.trigger_handoff(workspace=str(ws), latest=True, auto_pair=False, pair_if_offline=False)
                self.assertEqual(mock_inv.call_count, 2)

    def test_lazy_package_exports_cli_cold_start_and_all_resolution(self) -> None:
        import codex_agy_sync
        import codex_agy_sync.bridge as bridge_pkg
        import codex_agy_sync.workbench as workbench_pkg
        from codex_agy_sync.bridge.state_machine import ForkResolutionPolicy, SyncDirection

        repo_root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(repo_root / "src")}
        probe_script = """
import sys
import codex_agy_sync
assert "codex_agy_sync.codecs.steps" not in sys.modules
assert "codex_agy_sync.workbench.indexer" not in sys.modules
import codex_agy_sync.cli
assert "codex_agy_sync.codecs.steps" not in sys.modules
assert "codex_agy_sync.workbench.indexer" not in sys.modules
assert codex_agy_sync.bridge.__name__ == "codex_agy_sync.bridge"
assert codex_agy_sync.bridge.rpc.__name__ == "codex_agy_sync.bridge.rpc"
assert codex_agy_sync.workbench.menubar_feed.__name__ == "codex_agy_sync.workbench.menubar_feed"
print(codex_agy_sync.__version__)
"""
        probe_res = subprocess.run(
            [sys.executable, "-c", probe_script],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
        expected_ver = pyproject["project"]["version"]
        self.assertEqual(probe_res.stdout.strip(), expected_ver)

        ver_res = subprocess.run(
            [sys.executable, str(repo_root / "bin" / "codex-agy-sync"), "--version"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(ver_res.stdout.strip(), f"codex-agy-sync {expected_ver}")

        for pkg in (codex_agy_sync, bridge_pkg, workbench_pkg):
            for name in pkg.__all__:
                with self.subTest(pkg=pkg.__name__, symbol=name):
                    self.assertIsNotNone(getattr(pkg, name))
            self.assertTrue(set(pkg.__all__).issubset(set(dir(pkg))))

        self.assertEqual(cli_mod._SYNC_DIRECTION_CHOICES, tuple(d.value for d in SyncDirection))
        self.assertEqual(cli_mod._FORK_POLICY_CHOICES, tuple(p.value for p in ForkResolutionPolicy))

        from codex_agy_sync.bridge.state_machine import EmptySessionError
        err = EmptySessionError("empty")
        with mock.patch.dict(sys.modules):
            sys.modules.pop("codex_agy_sync.bridge.state_machine", None)
            payload = cli_mod._exception_to_error_payload(err, "sync-session")
        self.assertEqual(payload["error"], "empty_session")


if __name__ == "__main__":
    unittest.main()
