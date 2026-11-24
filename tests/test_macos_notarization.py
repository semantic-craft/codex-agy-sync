from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_menubar_app.sh"
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release_macos.sh"


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class MacosNotarizationTest(unittest.TestCase):
    def setUp(self) -> None:
        out_root = REPO_ROOT / "out"
        out_root.mkdir(parents=True, exist_ok=True)
        self._temp = tempfile.TemporaryDirectory(prefix="test-notary-", dir=out_root)
        self.root = Path(self._temp.name)
        self.bin_dir = self.root / "bin"
        self.home_dir = self.root / "home"
        self.fake_swift_bin = self.root / "fake-swift-bin"
        self.rust_target_dir = self.root / "rust-target"
        self.log_file = self.root / "invocations.jsonl"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.fake_swift_bin.mkdir(parents=True, exist_ok=True)
        self.rust_target_dir.mkdir(parents=True, exist_ok=True)
        self._install_common_stubs()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _install_common_stubs(self) -> None:
        _write_executable(
            self.bin_dir / "swift",
            f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "-e" ]]; then
    exec /usr/bin/swift "$@"
fi
if [[ "${{1:-}}" == "build" ]]; then
    for arg in "$@"; do
        if [[ "$arg" == "--show-bin-path" ]]; then
            printf '%s\\n' "{self.fake_swift_bin}"
            exit 0
        fi
    done
    printf '#!/usr/bin/env sh\\nexit 0\\n' > "{self.fake_swift_bin}/CodexAgySync"
    chmod 755 "{self.fake_swift_bin}/CodexAgySync"
    exit 0
fi
if [[ "${{1:-}}" == *generate_app_icon.swift ]]; then
    mkdir -p "$(dirname "$2")"
    printf 'ICNS' > "$2"
    exit 0
fi
echo "Unexpected swift args: $*" >&2
exit 1
""",
        )

        _write_executable(
            self.bin_dir / "cargo",
            f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "build" ]]; then
    target_dir="${{CARGO_TARGET_DIR:-{self.rust_target_dir}}}"
    mkdir -p "$target_dir/release"
    printf '#!/usr/bin/env sh\\nexit 0\\n' > "$target_dir/release/codex-agy-sync"
    chmod 755 "$target_dir/release/codex-agy-sync"
    exit 0
fi
echo "Unexpected cargo args: $*" >&2
exit 1
""",
        )

        for tool in ("codesign", "macos-notary", "xcrun", "spctl"):
            _write_executable(
                self.bin_dir / tool,
                f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

tool = {tool!r}
log_path = Path({str(self.log_file)!r})
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": tool, "args": sys.argv[1:]}}) + "\\n")

if tool == "codesign":
    if os.environ.get("STUB_CODESIGN_FAIL") == "1":
        print("codesign stub failure", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

if tool == "macos-notary":
    if os.environ.get("STUB_NOTARY_FAIL") == "1":
        print("macos-notary: Notarization failed: Rejected", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({{"status": "Accepted", "id": "sub-stub-1"}}))
    sys.exit(0)

if tool == "xcrun":
    if len(sys.argv) >= 3 and sys.argv[1] == "notarytool" and sys.argv[2] == "submit":
        status = os.environ.get("STUB_NOTARYTOOL_STATUS", "Accepted")
        print(json.dumps({{"status": status, "id": "sub-xcrun-1"}}))
        sys.exit(0 if status == "Accepted" else 1)
    if len(sys.argv) >= 2 and sys.argv[1] == "stapler":
        if os.environ.get("STUB_STAPLER_FAIL") == "1":
            print("stapler stub failure", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    sys.exit(0)

if tool == "spctl":
    if os.environ.get("STUB_SPCTL_FAIL") == "1":
        print("rejected\\nsource=Unnotarized Developer ID", file=sys.stderr)
        sys.exit(3)
    msg = os.environ.get("STUB_SPCTL_OUTPUT", "accepted\\nsource=Notarized Developer ID")
    print(msg, file=sys.stderr)
    sys.exit(0)
""",
            )

        self._write_security_stub(has_developer_id=True)
        self._write_profile_stub(self.bin_dir / "macos-notary-profile", "ci-stub-profile")

    def _write_security_stub(self, *, has_developer_id: bool) -> None:
        if has_developer_id:
            output = (
                '  1) AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555 '
                '"Developer ID Application: Test Developer (TEAM12345)"\n'
                '  2) 1111222233334444555566667777888899990000 '
                '"Apple Development: Test Developer (TEAM12345)"\n'
                "     2 valid identities found\n"
            )
        else:
            output = (
                '  1) 1111222233334444555566667777888899990000 '
                '"Apple Development: Test Developer (TEAM12345)"\n'
                "     1 valid identities found\n"
            )
        _write_executable(
            self.bin_dir / "security",
            f"#!/usr/bin/env bash\nprintf '%s' {output!r}\n",
        )

    def _write_profile_stub(self, path: Path, profile_name: str) -> None:
        _write_executable(
            path,
            f"#!/usr/bin/env bash\nprintf '%s\\n' {profile_name!r}\n",
        )

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "HOME": str(self.home_dir),
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "CARGO_TARGET_DIR": str(self.rust_target_dir),
            "CODEX_AGY_SKIP_LSREGISTER": "1",
        }
        if extra:
            env.update(extra)
        return env

    def _invocations(self) -> list[dict[str, object]]:
        if not self.log_file.is_file():
            return []
        return [
            json.loads(line)
            for line in self.log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @unittest.skipUnless(sys.platform == "darwin", "macOS packaging scripts require Darwin")
    def test_release_signs_developer_id_notarizes_and_writes_sha256sums(self) -> None:
        release_dir = self.root / "release-out"
        proc = subprocess.run(
            [str(RELEASE_SCRIPT), "--out-dir", str(release_dir)],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nstdout={proc.stdout}")

        app_bundle = release_dir / "CodexAgySync.app"
        zip_path = release_dir / "CodexAgySync-macos.zip"
        sums_path = release_dir / "SHA256SUMS.txt"
        zip_sha_path = release_dir / "CodexAgySync-macos.zip.sha256"
        self.assertTrue(app_bundle.is_dir())
        self.assertTrue(zip_path.is_file())
        self.assertTrue(sums_path.is_file())
        self.assertTrue(zip_sha_path.is_file())

        plist = plistlib.loads((app_bundle / "Contents" / "Info.plist").read_bytes())
        self.assertEqual(plist["CFBundleIdentifier"], "dev.semanticcraft.codex-agy-sync")

        expected_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        sums_text = sums_path.read_text(encoding="utf-8").strip()
        self.assertEqual(sums_text, f"{expected_sha}  CodexAgySync-macos.zip")
        self.assertEqual(zip_sha_path.read_text(encoding="utf-8").strip(), sums_text)

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            self.assertTrue(any(n.startswith("CodexAgySync.app/Contents/MacOS/CodexAgySync") for n in names))
            self.assertTrue(any(n.startswith("CodexAgySync.app/Contents/Helpers/codex-agy-sync") for n in names))

        self.assertTrue((app_bundle / "Contents" / "Helpers" / "codex-agy-sync").is_file())

        invocations = self._invocations()
        sign_calls = [c for c in invocations if c["tool"] == "codesign" and "--sign" in c["args"]]
        self.assertEqual(len(sign_calls), 2)
        helper_sign_args = sign_calls[0]["args"]
        app_sign_args = sign_calls[1]["args"]
        self.assertTrue(str(helper_sign_args[-1]).endswith("Contents/Helpers/codex-agy-sync"))
        self.assertNotIn("--deep", helper_sign_args)
        self.assertNotIn("--deep", app_sign_args)
        for sign_args in (helper_sign_args, app_sign_args):
            self.assertIn("--options", sign_args)
            self.assertIn("runtime", sign_args)
            self.assertIn("--timestamp", sign_args)
            self.assertIn("Developer ID Application: Test Developer (TEAM12345)", sign_args)
            self.assertNotIn("-", sign_args)

        notary_calls = [c for c in invocations if c["tool"] == "macos-notary"]
        self.assertEqual(len(notary_calls), 1)
        self.assertEqual(notary_calls[0]["args"][0], "submit")
        self.assertIn("--profile", notary_calls[0]["args"])
        self.assertIn("ci-stub-profile", notary_calls[0]["args"])

        spctl_calls = [c for c in invocations if c["tool"] == "spctl"]
        self.assertEqual(len(spctl_calls), 1)
        self.assertEqual(spctl_calls[0]["args"][:4], ["--assess", "--type", "execute", "--verbose=4"])

    @unittest.skipUnless(sys.platform == "darwin", "macOS packaging scripts require Darwin")
    def test_release_resolves_home_local_bin_profile_xcrun_fallback_and_installs_atomically(self) -> None:
        (self.bin_dir / "macos-notary-profile").unlink()
        (self.bin_dir / "macos-notary").unlink()
        self._write_profile_stub(self.home_dir / ".local" / "bin" / "macos-notary-profile", "local-bin-profile")

        existing_app = self.home_dir / "Applications" / "CodexAgySync.app" / "Contents" / "MacOS"
        existing_app.mkdir(parents=True, exist_ok=True)
        (existing_app / "old-marker").write_text("stale", encoding="utf-8")

        release_dir = self.root / "release-install"
        proc = subprocess.run(
            [str(RELEASE_SCRIPT), "--out-dir", str(release_dir), "--install"],
            env=self._env({"CODESIGN_IDENTITY": "Developer ID Application: Explicit Env (TEAM999)"}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nstdout={proc.stdout}")

        installed_app = self.home_dir / "Applications" / "CodexAgySync.app"
        self.assertTrue((installed_app / "Contents" / "MacOS" / "CodexAgySync").is_file())
        self.assertTrue((installed_app / "Contents" / "Helpers" / "codex-agy-sync").is_file())
        self.assertFalse((installed_app / "Contents" / "MacOS" / "old-marker").exists())
        self.assertTrue((release_dir / "CodexAgySync-macos.zip").is_file())
        self.assertTrue((release_dir / "SHA256SUMS.txt").is_file())

        invocations = self._invocations()
        sign_calls = [c for c in invocations if c["tool"] == "codesign" and "--sign" in c["args"]]
        self.assertIn("Developer ID Application: Explicit Env (TEAM999)", sign_calls[0]["args"])

        xcrun_calls = [c["args"] for c in invocations if c["tool"] == "xcrun"]
        self.assertTrue(any(a[:2] == ["notarytool", "submit"] and "--keychain-profile" in a and "local-bin-profile" in a for a in xcrun_calls))
        self.assertTrue(any(a[:2] == ["stapler", "staple"] for a in xcrun_calls))
        self.assertTrue(any(a[:2] == ["stapler", "validate"] for a in xcrun_calls))

    @unittest.skipUnless(sys.platform == "darwin", "macOS packaging scripts require Darwin")
    def test_release_fails_loudly_without_developer_id_or_with_adhoc(self) -> None:
        self._write_security_stub(has_developer_id=False)
        release_dir = self.root / "release-noid"

        no_id_proc = subprocess.run(
            [str(RELEASE_SCRIPT), "--out-dir", str(release_dir)],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(no_id_proc.returncode, 0)
        self.assertIn("Developer ID Application", no_id_proc.stderr)
        self.assertEqual(self._invocations(), [])

        self._write_security_stub(has_developer_id=True)
        adhoc_release = subprocess.run(
            [str(BUILD_SCRIPT), "--release", "--adhoc", "--output-dir", str(release_dir)],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(adhoc_release.returncode, 0)
        self.assertIn("--adhoc", adhoc_release.stderr)

        dash_env_release = subprocess.run(
            [str(BUILD_SCRIPT), "--notarize", "--output-dir", str(release_dir)],
            env=self._env({"CODESIGN_IDENTITY": "-"}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(dash_env_release.returncode, 0)
        self.assertIn("ad-hoc", dash_env_release.stderr.lower())

    @unittest.skipUnless(sys.platform == "darwin", "macOS packaging scripts require Darwin")
    def test_release_fails_loudly_when_notary_profile_missing(self) -> None:
        (self.bin_dir / "macos-notary-profile").unlink()
        proc = subprocess.run(
            [str(RELEASE_SCRIPT), "--out-dir", str(self.root / "release-noprofile")],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("macos-notary-profile", proc.stderr)
        self.assertEqual(self._invocations(), [])

    @unittest.skipUnless(sys.platform == "darwin", "macOS packaging scripts require Darwin")
    def test_release_fails_when_notarization_or_spctl_fails(self) -> None:
        fail_notary_dir = self.root / "release-fail-notary"
        p1 = subprocess.run(
            [str(RELEASE_SCRIPT), "--out-dir", str(fail_notary_dir)],
            env=self._env({"STUB_NOTARY_FAIL": "1"}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(p1.returncode, 0)
        self.assertFalse((fail_notary_dir / "CodexAgySync-macos.zip").exists())
        self.assertFalse((fail_notary_dir / "SHA256SUMS.txt").exists())

        fail_spctl_dir = self.root / "release-fail-spctl"
        p2 = subprocess.run(
            [str(RELEASE_SCRIPT), "--out-dir", str(fail_spctl_dir)],
            env=self._env({"STUB_SPCTL_OUTPUT": "accepted\nsource=Developer ID"}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(p2.returncode, 0)
        self.assertIn("Notarized Developer ID", p2.stderr)
        self.assertFalse((fail_spctl_dir / "CodexAgySync-macos.zip").exists())

    @unittest.skipUnless(sys.platform == "darwin", "macOS packaging scripts require Darwin")
    def test_dev_mode_adhoc_fallback_and_tmp_guard_preserved(self) -> None:
        adhoc_dir = self.root / "dev-adhoc"
        p1 = subprocess.run(
            [str(BUILD_SCRIPT), "--adhoc", "--output-dir", str(adhoc_dir)],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(p1.returncode, 0, msg=p1.stderr)
        inv1 = self._invocations()
        sign1 = [c for c in inv1 if c["tool"] == "codesign" and "--sign" in c["args"]][0]["args"]
        self.assertIn("-", sign1)
        self.assertNotIn("--timestamp", sign1)
        self.assertFalse(any(c["tool"] in ("macos-notary", "spctl", "xcrun") for c in inv1))

        self.log_file.unlink()
        self._write_security_stub(has_developer_id=False)
        dev_fallback_dir = self.root / "dev-fallback"
        p2 = subprocess.run(
            [str(BUILD_SCRIPT), "--output-dir", str(dev_fallback_dir)],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(p2.returncode, 0, msg=p2.stderr)
        inv2 = self._invocations()
        sign2 = [c for c in inv2 if c["tool"] == "codesign" and "--sign" in c["args"]][0]["args"]
        self.assertIn("-", sign2)

        install_noid = subprocess.run(
            [str(BUILD_SCRIPT), "--install"],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(install_noid.returncode, 0)
        self.assertIn("Developer ID Application", install_noid.stderr)

        qa_dir = self.root / "dev-qa-display"
        p3 = subprocess.run(
            [
                str(BUILD_SCRIPT),
                "--adhoc",
                "--output-dir",
                str(qa_dir),
                "--bundle-id",
                "dev.semanticcraft.codex-agy-sync.qa",
                "--display-name",
                "CodexAgySync QA",
            ],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(p3.returncode, 0, msg=p3.stderr)
        qa_plist = plistlib.loads((qa_dir / "CodexAgySync.app" / "Contents" / "Info.plist").read_bytes())
        self.assertEqual(qa_plist["CFBundleIdentifier"], "dev.semanticcraft.codex-agy-sync.qa")
        self.assertEqual(qa_plist["CFBundleDisplayName"], "CodexAgySync QA")
        self.assertEqual(qa_plist["CFBundleExecutable"], "CodexAgySync")

        tmp_bad = subprocess.run(
            [str(BUILD_SCRIPT), "--output-dir", "/tmp/forbidden-codex-agy-sync-out"],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(tmp_bad.returncode, 0)
        self.assertIn("/tmp", tmp_bad.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "macOS packaging scripts require Darwin")
    def test_install_retires_legacy_bundle_in_strict_tcc_lsregister_rm_order(self) -> None:
        legacy_name = "CodexAgy" + "Handoff"
        legacy_bundle_id = "dev.semanticcraft.codex-agy-" + "handoff"
        legacy_bundle = self.home_dir / "Applications" / f"{legacy_name}.app"
        (legacy_bundle / "Contents").mkdir(parents=True, exist_ok=True)
        (legacy_bundle / "Contents" / "Info.plist").write_text("<plist/>", encoding="utf-8")

        order_log = self.root / "retire-order.jsonl"
        stub_template = (
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "tool, log_path, legacy_path = {tool!r}, pathlib.Path({log!r}), pathlib.Path({legacy!r})\n"
            "rec = {{'tool': tool, 'args': sys.argv[1:], 'legacy_exists': legacy_path.exists()}}\n"
            "with log_path.open('a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(rec) + '\\n')\n"
        )
        tccutil_stub = self.bin_dir / "tccutil-stub"
        lsregister_stub = self.bin_dir / "lsregister-stub"
        _write_executable(
            tccutil_stub,
            stub_template.format(tool="tccutil", log=str(order_log), legacy=str(legacy_bundle)),
        )
        _write_executable(
            lsregister_stub,
            stub_template.format(tool="lsregister", log=str(order_log), legacy=str(legacy_bundle)),
        )

        env = self._env({
            "CODEX_AGY_SKIP_LSREGISTER": "0",
            "CODEX_AGY_TCCUTIL_BIN": str(tccutil_stub),
            "CODEX_AGY_LSREGISTER_BIN": str(lsregister_stub),
        })
        proc = subprocess.run(
            [str(BUILD_SCRIPT), "--install"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nstdout={proc.stdout}")

        events = [
            json.loads(line)
            for line in order_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        retire_events = [
            ev for ev in events
            if ev["tool"] == "tccutil"
            or (ev["tool"] == "lsregister" and ev["args"] == ["-u", str(legacy_bundle)])
        ]
        self.assertEqual(len(retire_events), 2, msg=f"Unexpected retirement sequence: {events}")
        self.assertEqual(retire_events[0]["tool"], "tccutil")
        self.assertEqual(retire_events[0]["args"], ["reset", "All", legacy_bundle_id])
        self.assertIs(retire_events[0]["legacy_exists"], True)

        self.assertEqual(retire_events[1]["tool"], "lsregister")
        self.assertEqual(retire_events[1]["args"], ["-u", str(legacy_bundle)])
        self.assertIs(retire_events[1]["legacy_exists"], True)

        self.assertFalse(legacy_bundle.exists())
        installed_app = self.home_dir / "Applications" / "CodexAgySync.app"
        self.assertTrue(installed_app.is_dir())
        self.assertTrue(
            any(ev["tool"] == "lsregister" and ev["args"] == ["-f", str(installed_app)] for ev in events),
            msg=f"Missing lsregister -f for installed app in {events}",
        )

    def test_notary_policy_compliance_and_no_hardcoded_private_literals(self) -> None:
        private_profile = "xw" + "-notary"
        custom_profile_re = re.compile(
            r"(?<![a-z0-9-])(?!macos-notary(?![a-z0-9-]))[a-z0-9][a-z0-9-]*-notary(?![a-z0-9-])",
            re.I,
        )
        for rel_path in ("scripts/build_menubar_app.sh", "scripts/release_macos.sh", "AGENTS.md"):
            text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            self.assertNotIn(private_profile, text.lower(), msg=f"Forbidden literal in {rel_path}")
            self.assertIsNone(
                custom_profile_re.search(text),
                msg=f"Unexpected custom notary literal in {rel_path}",
            )

        policy_script = Path.home() / "Projects" / "agent-home" / "tools" / "notary-check" / "check_notary_policy.py"
        if policy_script.is_file():
            proc = subprocess.run(
                [sys.executable, str(policy_script), "--visibility", "public", str(REPO_ROOT)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nstdout={proc.stdout}")
            self.assertIn("0 finding(s)", proc.stdout)
