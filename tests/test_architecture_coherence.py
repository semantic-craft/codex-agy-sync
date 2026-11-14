from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_agy_sync import (
    CascadeId, CodexThreadId, HostEnvironmentPaths, HostKind, SyncBridge,
    TransportMode, execute_handoff,
)
from codex_agy_sync.bridge import handoff_resolve
from codex_agy_sync.bridge.rpc import AgyRpcReadResult, CodexRpcReadResult
from codex_agy_sync.bridge.transport import DefaultHostTransport
from codex_agy_sync.ledger import AssistantTextEvent, EventOriginAnchor, TrajectoryLedger, TurnId, UserInputEvent

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ArchitectureCoherenceTest(unittest.TestCase):
    def setUp(self) -> None:
        artifacts = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="arch-coh-", dir=artifacts)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_pkg(self, idx: int, files: dict[str, str]) -> Path:
        pkg = self.root / f"syn_pkg_{idx}" / "codex_agy_sync"
        base_files = {"__init__.py": "", "_wire.py": "", "workspace.py": "", "payloads.py": "", "events.py": "", "ledger.py": "", "codecs/__init__.py": "", "bridge/__init__.py": "", "workbench/__init__.py": "", "cli.py": "", "__main__.py": "", **files}
        for rel, content in base_files.items():
            dst = pkg / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")
        return pkg

    def test_ast_coherence_gate_live_and_negative_controls(self) -> None:
        arch = _load_script("check_architecture.py")
        live = arch.check_package_architecture(REPO_ROOT / "src" / "codex_agy_sync")
        self.assertGreaterEqual(live["modules"], 30)
        self.assertGreater(live["edges"], 50)
        negatives = [
            ({"workspace.py": "_secret = 1\n", "events.py": "from .workspace import _secret\n"}, r"private cross-module import"),
            ({"codecs/_bad.py": "X = 1\n", "codecs/good.py": "from ._bad import X\n"}, r"disallowed private module segment"),
            ({"workspace.py": "__all__ = ['_hidden']\n_hidden = 1\n"}, r"private symbol '_hidden' in __all__"),
            ({"events.py": "def f():\n    from .workspace import X\n    return X\n", "workspace.py": "X = 1\n"}, r"function-scoped internal import"),
            ({"workspace.py": "def f(obj):\n    return obj._secret\n"}, r"non-self/cls private attribute access"),
            ({"workspace.py": "from .events import Y\n", "events.py": "Y = 1\n"}, r"upward tier dependency"),
            ({"bridge/a.py": "from .b import B\nA = B\n", "bridge/b.py": "from .a import A\nB = A\n"}, r"Internal import cycle detected"),
        ]
        for idx, (files, pattern) in enumerate(negatives):
            pkg = self._make_pkg(idx, files)
            with self.subTest(case=idx), self.assertRaisesRegex(AssertionError, pattern):
                arch.check_package_architecture(pkg)

    def test_execute_handoff_sandbox_parity_ready_noop_vs_new_handoff(self) -> None:
        paths = HostEnvironmentPaths(self.root / "cfg", self.root / "agy", self.root / "codex", self.root / "state")
        ws = self.root / "ws"
        ws.mkdir()
        bridge, transport = SyncBridge(paths), DefaultHostTransport(paths)
        binding = bridge.pair_workspace(ws)
        anchor = EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId("turn-1"), timestamp_iso="2026-11-14T00:00:00Z")
        evs = [UserInputEvent(kind="user_input", anchor=anchor, query_text="hi"), AssistantTextEvent(kind="assistant_text", anchor=anchor, text="ok")]
        ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, evs, explicit_title_override="Parity")
        tid = CodexThreadId("01960000-0000-7000-8000-000000000099")
        transport.apply_to_codex(binding, tid, ledger.events, ledger, bridge.guard.probe_locks(None, None), TransportMode.OFFLINE_STORAGE)

        def fake_agy_rpc(_ep: object, cid: CascadeId, **_kw: object) -> AgyRpcReadResult:
            return AgyRpcReadResult(tuple(transport.read_persisted_agy_steps(paths.agy_data_dir / "conversations" / f"{cid}.db")), 1)

        def fake_codex_rpc(_ep: object, thread_id: CodexThreadId, **_kw: object) -> CodexRpcReadResult:
            rp, title = transport.find_codex_rollout_and_title(thread_id)
            return CodexRpcReadResult(thread={"id": str(thread_id), "name": title or "", "path": str(rp), "turns": []}, is_active=False)

        with (
            mock.patch("codex_agy_sync.bridge.handoff.PresenceWalGuard.discover_agy_daemon", return_value=object()),
            mock.patch("codex_agy_sync.bridge.rpc.fetch_codex_thread_via_rpc", side_effect=fake_codex_rpc),
            mock.patch("codex_agy_sync.bridge.handoff.rpc.fetch_agy_trajectory_via_rpc", side_effect=fake_agy_rpc),
            mock.patch("codex_agy_sync.bridge.handoff_resolve.handoff_session", wraps=handoff_resolve.handoff_session) as spy,
        ):
            r1, mutated1 = execute_handoff(paths, ws, chain=True)
            r2, mutated2 = execute_handoff(paths, ws, chain=True)
        self.assertTrue(mutated1)
        self.assertEqual(r1.status, "ready")
        self.assertFalse(mutated2)
        self.assertEqual(r2.status, "no_op")
        self.assertEqual(r2.target_id, r1.target_id)
        self.assertEqual(spy.call_count, 1)

    def test_evaluate_codex_sidebar_visibility_cwd_non_empty(self) -> None:
        verify = _load_script("verify_session_mapping.py")
        rules = json.loads((REPO_ROOT / "schema" / "sidebar_visibility_rules.json").read_text(encoding="utf-8"))
        ws = self.root / "ws"
        valid = {"project_id": None, "archived": 0, "preview_non_empty": True, "cwd": str(ws)}
        ok = verify.evaluate_codex_sidebar_visibility(valid, rules)
        self.assertTrue(ok["sidebar_visible_predicate_met"])
        self.assertTrue(ok["cwd_non_empty_ok"])
        for bad in ({**valid, "cwd": ""}, {**valid, "cwd": "  "}, {**valid, "preview_non_empty": False}, {**valid, "archived": 1}):
            res = verify.evaluate_codex_sidebar_visibility(bad, rules)
            self.assertFalse(res["sidebar_visible_predicate_met"], bad)
