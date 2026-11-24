from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
INVARIANTS_SCRIPT = REPO_ROOT / "scripts" / "check_invariants.py"
SESSION_MAPPING_SCRIPT = REPO_ROOT / "scripts" / "verify_session_mapping.py"


def _load_invariants_module():
    spec = importlib.util.spec_from_file_location("check_invariants", INVARIANTS_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


INVARIANTS = _load_invariants_module()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_clean_mini_repo(root: Path, version: str = "1.2.3") -> None:
    _write_text(
        root / "pyproject.toml",
        f'[project]\nname = "codex-agy-sync"\nversion = "{version}"\n',
    )
    _write_text(
        root / "src" / "codex_agy_sync" / "workspace.py",
        f'PACKAGE_VERSION: str = "{version}"\n',
    )
    _write_text(
        root / "rust" / "Cargo.toml",
        f'[workspace.package]\nversion = "{version}"\n',
    )
    _write_text(
        root / "rust" / "crates" / "cas-core" / "src" / "id.rs",
        f'/// Package version.\npub const PACKAGE_VERSION: &str = "{version}";\n',
    )
    _write_text(
        root / "macos" / "Sources" / "CodexAgySyncCore" / "UpdateService.swift",
        (
            "import Foundation\n"
            "func version() -> String {\n"
            '    return (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String)\n'
            f'        ?? "{version}"\n'
            "}\n"
        ),
    )
    _write_text(
        root / ".comment-ratchet.json",
        json.dumps({"version": 1, "max_total": 0, "exclude": [], "files": {}}, indent=2) + "\n",
    )


class InvariantGatesTest(unittest.TestCase):
    def setUp(self) -> None:
        out_root = REPO_ROOT / "out"
        out_root.mkdir(parents=True, exist_ok=True)
        self._temp = tempfile.TemporaryDirectory(prefix="test-invariants-", dir=out_root)
        self.mini_repo = Path(self._temp.name)
        _seed_clean_mini_repo(self.mini_repo)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_clean_mini_repo_and_real_repo_pass_with_zero_errors(self) -> None:
        self.assertEqual(INVARIANTS.check_repo_invariants(self.mini_repo), [])
        self.assertEqual(INVARIANTS.check_comment_ratchet(self.mini_repo), [])
        self.assertEqual(INVARIANTS.check_repo_invariants(REPO_ROOT), [])
        self.assertEqual(INVARIANTS.check_comment_ratchet(REPO_ROOT), [])

    def test_raw_sqlite_connect_catches_disallowed_and_allows_allowlisted(self) -> None:
        _write_text(
            self.mini_repo / "src" / "codex_agy_sync" / "bridge" / "transport.py",
            'import sqlite3\ndef ok():\n    return sqlite3.connect(":memory:")\n',
        )
        self.assertEqual(INVARIANTS.check_repo_invariants(self.mini_repo), [])

        _write_text(
            self.mini_repo / "src" / "codex_agy_sync" / "workbench" / "bad.py",
            'import sqlite3\ndef bad():\n    return sqlite3.connect(":memory:")\n',
        )
        _write_text(
            self.mini_repo / "src" / "codex_agy_sync" / "bridge" / "handoff.py",
            'import sqlite3\ndef bad2():\n    return sqlite3.connect(":memory:")\n',
        )
        errors = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertEqual(len(errors), 2)
        joined = "\n".join(errors)
        self.assertIn("RAW_SQLITE_CONNECT", joined)
        self.assertIn("src/codex_agy_sync/workbench/bad.py:3", joined)
        self.assertIn("src/codex_agy_sync/bridge/handoff.py:3", joined)
        self.assertIn("open_readonly_sqlite", joined)

    def test_bare_urlopen_catches_module_and_imported_forms(self) -> None:
        _write_text(
            self.mini_repo / "skills" / "verify-codex-agy-sync" / "scripts" / "bad.py",
            (
                "import urllib.request\n"
                "from urllib.request import urlopen\n"
                "def f(req):\n"
                "    urllib.request.urlopen(req)\n"
                "    urlopen(req)\n"
            ),
        )
        errors = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertEqual(len(errors), 2)
        for err in errors:
            self.assertIn("BARE_URLOPEN", err)
            self.assertIn("ProxyHandler", err)

    def test_hardcoded_tmp_path_catches_all_forms_and_allows_real_guard_fn(self) -> None:
        _write_text(
            self.mini_repo / "skills" / "verify-codex-agy-sync" / "scripts" / "_workbench_sandbox.py",
            (
                "from pathlib import Path\n"
                "def is_tmp_path(candidate: Path) -> bool:\n"
                '    return any(candidate == r for r in (Path("/tmp"), Path("/private/tmp")))\n'
            ),
        )
        self.assertEqual(INVARIANTS.check_repo_invariants(self.mini_repo), [])

        _write_text(
            self.mini_repo / "scripts" / "bad.py",
            (
                "import os\n"
                "import shutil\n"
                "import tempfile\n"
                "from pathlib import Path\n"
                'FORBIDDEN_TMP_PREFIXES = (Path("/tmp"),)\n'
                "a = tempfile.gettempdir()\n"
                'b = Path("/tmp/x")\n'
                'c = os.path.join("/tmp", "y")\n'
                'd = tempfile.TemporaryDirectory(dir="/tmp")\n'
                'e = open(file="/tmp/z")\n'
                'f = Path("/") / "tmp"\n'
                'g = "/tmp/" + "concat"\n'
                'os.makedirs("/tmp/dir")\n'
                'shutil.rmtree("/tmp/dir")\n'
            ),
        )
        errors = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertEqual(len(errors), 10)
        for err in errors:
            self.assertIn("HARDCODED_TMP_PATH", err)
            self.assertIn("_artifacts", err)

    def test_version_sync_catches_rust_and_swift_mismatches_and_ignores_comments(self) -> None:
        _write_text(
            self.mini_repo / "rust" / "crates" / "cas-core" / "src" / "id.rs",
            'pub const PACKAGE_VERSION: &str = "9.9.9";\n',
        )
        errors_rust = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertEqual(len(errors_rust), 1)
        self.assertIn("VERSION_SYNC", errors_rust[0])
        self.assertIn("rust/crates/cas-core/src/id.rs", errors_rust[0])
        self.assertIn("1.2.3", errors_rust[0])

        _seed_clean_mini_repo(self.mini_repo)
        _write_text(
            self.mini_repo / "macos" / "Sources" / "CodexAgySyncCore" / "UpdateService.swift",
            (
                "func version() -> String {\n"
                '    // CFBundleShortVersionString ?? "1.2.3"\n'
                '    return (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String)\n'
                '        ?? "0.0.1"\n'
                "}\n"
            ),
        )
        errors_swift = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertEqual(len(errors_swift), 1)
        self.assertIn("VERSION_SYNC", errors_swift[0])
        self.assertIn("UpdateService.swift", errors_swift[0])
        self.assertIn("1.2.3", errors_swift[0])

    def test_swift_raw_kill_catches_qualified_calls_and_unguarded_cliclient(self) -> None:
        _write_text(
            self.mini_repo / "macos" / "Sources" / "CodexAgySyncCore" / "CLIClient.swift",
            "func terminateProcessWithEscalation(_ pid: Int32) {\n    if pid > 0 { kill(pid, SIGKILL) }\n}\n",
        )
        _write_text(
            self.mini_repo / "macos" / "Sources" / "CodexAgySyncCore" / "Safe.swift",
            'let note = "avoid kill(0, SIGKILL)"\n',
        )
        self.assertEqual(INVARIANTS.check_repo_invariants(self.mini_repo), [])

        _write_text(
            self.mini_repo / "macos" / "Sources" / "CodexAgySyncCore" / "CLIClient.swift",
            "func terminateProcessWithEscalation(_ pid: Int32) {\n    kill(pid, SIGKILL)\n}\n",
        )
        unguarded_errors = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertEqual(len(unguarded_errors), 1)
        self.assertIn("SWIFT_RAW_KILL", unguarded_errors[0])
        self.assertIn("pid > 0", unguarded_errors[0])

        _write_text(
            self.mini_repo / "macos" / "Sources" / "CodexAgySyncCore" / "CLIClient.swift",
            "func terminateProcessWithEscalation(_ pid: Int32) {\n    guard pid > 0 else { return }\n    kill(pid, SIGKILL)\n}\n",
        )
        _write_text(
            self.mini_repo / "macos" / "Sources" / "CodexAgySyncCore" / "Bad.swift",
            "func stop(_ pid: Int32) {\n    kill(pid, SIGKILL)\n    Darwin.kill(pid, SIGKILL)\n    Glibc.kill(pid, SIGKILL)\n}\n",
        )
        errors = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertEqual(len(errors), 3)
        for err in errors:
            self.assertIn("SWIFT_RAW_KILL", err)
            self.assertIn("terminateProcessWithEscalation", err)

    def test_swift_reverse_dns_defaults_catches_concat_and_raw_strings(self) -> None:
        _write_text(
            self.mini_repo / "macos" / "Sources" / "CodexAgySyncCore" / "PreferencesStore.swift",
            'let d = UserDefaults(suiteName: "dev.semanticcraft.codex-agy-sync")\n',
        )
        self.assertEqual(INVARIANTS.check_repo_invariants(self.mini_repo), [])

        _write_text(
            self.mini_repo / "macos" / "Sources" / "CodexAgySyncCore" / "Bad.swift",
            (
                'let a = UserDefaults(suiteName: "dev.semanticcraft.codex-agy-sync.leak")\n'
                'let b = UserDefaults(suiteName: "dev.semanticcraft." + suffix)\n'
                'let c = UserDefaults(suiteName: #"dev.semanticcraft.raw"#)\n'
            ),
        )
        errors = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertEqual(len(errors), 3)
        for err in errors:
            self.assertIn("SWIFT_REVERSE_DNS_DEFAULTS", err)

    def test_subprocess_close_fds_false_zero_and_pass_fds_caught(self) -> None:
        _write_text(
            self.mini_repo / "skills" / "verify-codex-agy-sync" / "scripts" / "ok.py",
            'import subprocess\nsubprocess.run(["true"], close_fds=True, pass_fds=())\n',
        )
        self.assertEqual(INVARIANTS.check_repo_invariants(self.mini_repo), [])

        _write_text(
            self.mini_repo / "skills" / "verify-codex-agy-sync" / "scripts" / "bad.py",
            (
                "import subprocess\n"
                'subprocess.run(["true"], close_fds=False)\n'
                'subprocess.Popen(["true"], close_fds=0)\n'
                'subprocess.run(["true"], pass_fds=(3,))\n'
            ),
        )
        errors = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertEqual(len(errors), 3)
        for err in errors:
            self.assertIn("SUBPROCESS_CLOSE_FDS_FALSE", err)
            self.assertIn("close_fds=True", err)

    def test_parse_errors_fail_loudly_in_invariants_and_comment_ratchet(self) -> None:
        _write_text(
            self.mini_repo / "scripts" / "broken.py",
            'def broken(:\n    x = "unterminated\n',
        )
        inv_errors = INVARIANTS.check_repo_invariants(self.mini_repo)
        self.assertTrue(any("PARSE_ERROR" in e and "scripts/broken.py" in e for e in inv_errors))
        cmt_errors = INVARIANTS.check_comment_ratchet(self.mini_repo)
        self.assertTrue(any("COMMENT_RATCHET" in e and "scripts/broken.py" in e for e in cmt_errors))

    def test_comment_ratchet_lexers_exemptions_and_bypasses(self) -> None:
        _write_text(
            self.mini_repo / "scripts" / "exempt.py",
            "#!/usr/bin/env python3\nimport os  # noqa: F401\n",
        )
        _write_text(
            self.mini_repo / "rust" / "crates" / "cas-core" / "src" / "exempt.rs",
            (
                "#![allow(dead_code)]\n"
                "//! Crate docs.\n"
                "/// Function docs.\n"
                'pub fn url() -> &\'static str { "https://example.com//not-a-comment" }\n'
                "// SAFETY: pointer validated\n"
            ),
        )
        _write_text(
            self.mini_repo / "macos" / "Package.swift",
            '// swift-tools-version: 5.10\nlet s = "https://example.com//not-a-comment"\n',
        )
        _write_text(
            self.mini_repo / "scripts" / "exempt.sh",
            '#!/usr/bin/env bash\n# shellcheck disable=SC2034\nOUT="$(echo "# inside")"\nURL="http://localhost/#frag"\n',
        )
        self.assertEqual(INVARIANTS.check_comment_ratchet(self.mini_repo), [])

        _write_text(
            self.mini_repo / "scripts" / "bad.py",
            "x = 1\n# restating SAFETY: sneaky bypass\n",
        )
        _write_text(
            self.mini_repo / "rust" / "crates" / "cas-core" / "src" / "bad.rs",
            "#![allow(dead_code)] // hidden line-1 rust comment\n",
        )
        _write_text(
            self.mini_repo / "macos" / "Sources" / "CodexAgySyncCore" / "Bad.swift",
            "//! not a swift doc comment\nlet x = 1 /// inline triple slash\n",
        )
        _write_text(
            self.mini_repo / "scripts" / "bad.sh",
            '#!/usr/bin/env bash\n(echo "$(echo hi)")#trailing shell comment\n# ordinary shell comment\n',
        )
        errors = INVARIANTS.check_comment_ratchet(self.mini_repo)
        self.assertEqual(len(errors), 6)
        joined = "\n".join(errors)
        self.assertIn("scripts/bad.py:2", joined)
        self.assertIn("rust/crates/cas-core/src/bad.rs:1", joined)
        self.assertIn("macos/Sources/CodexAgySyncCore/Bad.swift:1", joined)
        self.assertIn("macos/Sources/CodexAgySyncCore/Bad.swift:2", joined)
        self.assertIn("scripts/bad.sh:2", joined)
        self.assertIn("scripts/bad.sh:3", joined)
        self.assertIn("docs/DECISIONS.md", joined)

    def test_verify_session_mapping_ci_sandbox_ignores_env_and_cleans_up(self) -> None:
        report_path = self.mini_repo / "ci_session_report.json"
        before_dirs = {p.name for p in (REPO_ROOT / "out").glob("ci-session-sandbox-*")}
        env = dict(os.environ)
        env["AGY_CONVERSATION_ID"] = "nonexistent-host-agy-id"
        env["CODEX_THREAD_ID"] = "nonexistent-host-codex-id"
        proc = subprocess.run(
            [sys.executable, str(SESSION_MAPPING_SCRIPT), "--ci-sandbox", "--output", str(report_path)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nstdout={proc.stdout}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "VERIFIED")
        neg = report["negative_controls"]
        self.assertIs(neg["unknown_wire_field_9999_caught"], True)
        self.assertIs(neg["forbidden_project_bucket_caught"], True)
        self.assertIs(neg["codex_empty_preview_caught"], True)
        self.assertIs(neg["codex_empty_cwd_caught"], True)
        self.assertEqual(report["antigravity_session"]["counts"]["unknown_wire_fields_count"], 0)
        self.assertIs(report["antigravity_session"]["readonly_integrity"]["conv_db_unchanged"], True)
        self.assertIs(report["antigravity_session"]["readonly_integrity"]["summaries_db_unchanged"], True)
        self.assertIs(report["codex_session"]["readonly_integrity"]["state_5_sqlite_unchanged"], True)
        self.assertEqual(report["codex_session"]["counts"]["unpaired_tool_calls"], 0)
        after_dirs = {p.name for p in (REPO_ROOT / "out").glob("ci-session-sandbox-*")}
        self.assertEqual(after_dirs - before_dirs, set())


if __name__ == "__main__":
    unittest.main()
