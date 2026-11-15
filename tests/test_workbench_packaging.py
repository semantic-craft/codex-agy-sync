from __future__ import annotations

import contextlib
import http.client
import importlib.resources
import io
from pathlib import Path
import re
import tempfile
import tomllib
import unittest

from codex_agy_sync import HostEnvironmentPaths, WorkbenchServer, WorkbenchService
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
        self.assertEqual(pyproject["project"]["version"], "0.3.0")
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
            "createOfflineController",
            "auto_pair",
            "include_interrupted",
            "/api/handoff/reconcile",
        ):
            self.assertIn(token, html_text)

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
                finally:
                    conn.close()
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", content_type)
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


if __name__ == "__main__":
    unittest.main()
