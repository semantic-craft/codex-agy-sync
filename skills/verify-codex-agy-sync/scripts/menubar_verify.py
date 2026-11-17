#!/usr/bin/env python3
"""Isolated end-to-end QA verifier for the CodexAgySync macOS menu bar app."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import plistlib
import shutil
import signal
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _menubar_qa_helpers import (  # noqa: E402
    CodexWsRpcStub,
    MockReleaseHttpStub,
    click_until,
    cua_call,
    find_element,
    has_window_element,
    run_cmd,
    wait_for_window,
    wait_for_window_title,
)
from _workbench_sandbox import AgyLoopbackRpcStub, CLI_BIN, REPO_ROOT, capture_host_fingerprints, now_iso, validate_run_dir, write_json  # noqa: E402

SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
from codex_agy_sync import DefaultHostTransport, HostEnvironmentPaths, HostKind, INTERRUPTED_TURN_TEXT, SyncBridge, TrajectoryLedger, TransportMode  # noqa: E402
from codex_agy_sync.bridge import diagnose_workspace_binding, ensure_codex_state_db_schema  # noqa: E402
from codex_agy_sync.events import AssistantTextEvent, CascadeId, EventOriginAnchor, ThinkingEvent, ToolCallEvent, ToolCallId, TurnId, UserInputEvent  # noqa: E402
from codex_agy_sync.payloads import RunCommandPayload  # noqa: E402

QA_BUNDLE_ID = "dev.semanticcraft.codex-agy-sync.qa"
CTA_LABEL = "用 Codex 继续"
DONE_BANNER = "已在 Codex 打开对话"
SETTINGS_TITLE = "CodexAgySync 设置"
SETTINGS_TITLE_EN = "CodexAgySync Settings"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_menubar_app.sh"
PACKAGE_VERSION = str(tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
_VER_PARTS = PACKAGE_VERSION.split(".")
_NEXT_QA_VERSION = f"{_VER_PARTS[0]}.{int(_VER_PARTS[1]) + 1}.0"
LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
_SEED_CID = "00000000-0000-4000-8000-000000000091"


def collect_host_fingerprints() -> dict[str, dict[str, Any]]:
    """Capture host main SQLite fingerprints matching _workbench_sandbox."""
    return capture_host_fingerprints()


def _check_cua_permissions() -> dict[str, Any]:
    """Inspect Cua Driver daemon availability and TCC permissions."""
    proc = run_cmd(["cua-driver", "permissions", "status", "--json"], check=False, timeout=15.0)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid JSON: {exc}"}
    ok = bool(data.get("daemon_running") and data.get("accessibility") and data.get("screen_recording"))
    return {
        "ok": ok,
        "daemon_running": bool(data.get("daemon_running")),
        "accessibility": bool(data.get("accessibility")),
        "screen_recording": bool(data.get("screen_recording")),
    }


def cmd_doctor() -> int:
    """Verify Swift toolchain, Cua permissions, CLI feed command, icon generator, and build script."""
    swift_p = run_cmd(["swift", "--version"], check=False, timeout=15.0)
    feed_p = run_cmd([str(CLI_BIN), "menubar-feed", "--help"], check=False, timeout=15.0)
    icon_gen = REPO_ROOT / "scripts" / "generate_app_icon.swift"
    checks = {
        "swift": {"ok": swift_p.returncode == 0, "version": swift_p.stdout.splitlines()[0] if swift_p.stdout else ""},
        "cua_permissions": _check_cua_permissions(),
        "cli_menubar_feed": {"ok": feed_p.returncode == 0, "bin": str(CLI_BIN)},
        "build_script": {"ok": BUILD_SCRIPT.is_file() and os.access(BUILD_SCRIPT, os.X_OK), "path": str(BUILD_SCRIPT)},
        "icon_generator": {"ok": icon_gen.is_file(), "path": str(icon_gen)},
    }
    all_ok = all(bool(item["ok"]) for item in checks.values())
    print(json.dumps({"status": "ok" if all_ok else "failed", "checked_at": now_iso(), "checks": checks}, indent=2, ensure_ascii=False))
    return 0 if all_ok else 1


def _make_sandbox_env(run_dir: Path, release_api_url: str) -> tuple[dict[str, Path], dict[str, str], str]:
    """Build isolated sandbox paths, prefs suite, and app environment variables."""
    sb = run_dir / "_sandbox"
    dirs = {
        "sandbox": sb,
        "ws_rescue": sb / "ws-rescue",
        "gemini_home": sb / "gemini-home",
        "gemini_config": sb / "gemini-config",
        "agy_data": sb / "gemini-home" / "antigravity",
        "codex_home": sb / "codex-home",
        "sync_state": sb / "sync-state",
        "prefs_dir": sb / "prefs",
        "sock_path": run_dir / "c.sock",
        "app_dir": run_dir / "app",
        "shots_dir": run_dir / "screenshots",
    }
    for k, p in dirs.items():
        if k not in {"sandbox", "sock_path"}:
            p.mkdir(parents=True, exist_ok=True)
    prefs_suite = str(dirs["prefs_dir"] / "menubar-qa")
    env = {
        "GEMINI_HOME": str(dirs["gemini_home"]),
        "GEMINI_CONFIG_DIR": str(dirs["gemini_config"]),
        "ANTIGRAVITY_DATA_DIR": str(dirs["agy_data"]),
        "CODEX_HOME": str(dirs["codex_home"]),
        "CODEX_AGY_SYNC_STATE_DIR": str(dirs["sync_state"]),
        "CODEX_APP_SERVER_SOCKET": str(dirs["sock_path"]),
        "PATH": os.environ.get("PATH", ""),
        "MENUBAR_AUTO_OPEN_PANEL": "1",
        "MENUBAR_PIN_PANEL": "1",
        "MENUBAR_SKIP_APP_ACTIVATION": "1",
        "MENUBAR_DISABLE_GLOBAL_HOTKEY": "1",
        "MENUBAR_PREFS_SUITE": prefs_suite,
        "MENUBAR_DISABLE_LOGIN_ITEM": "1",
        "MENUBAR_RELEASE_API_URL": release_api_url,
    }
    return dirs, env, prefs_suite


def _verify_app_bundle_assets(app_bundle: Path) -> dict[str, Any]:
    """Assert AppIcon.icns, bundled Rust helper CLI, and Info.plist match bundle ID, localizations, and version."""
    icon_path = app_bundle / "Contents" / "Resources" / "AppIcon.icns"
    helper_cli = app_bundle / "Contents" / "Helpers" / "codex-agy-sync"
    plist_path = app_bundle / "Contents" / "Info.plist"
    if not icon_path.is_file() or icon_path.stat().st_size <= 10_000:
        raise AssertionError(f"Expected non-trivial AppIcon.icns at {icon_path}")
    if not helper_cli.is_file() or not os.access(helper_cli, os.X_OK):
        raise AssertionError(f"Expected executable bundled Rust CLI at {helper_cli}")
    ver_proc = run_cmd([str(helper_cli), "--version"], check=True, timeout=10.0)
    if PACKAGE_VERSION not in ver_proc.stdout:
        raise AssertionError(f"Bundled helper version {ver_proc.stdout!r} does not contain {PACKAGE_VERSION!r}")
    info = plistlib.loads(plist_path.read_bytes())
    if info.get("CFBundleIdentifier") != QA_BUNDLE_ID:
        raise AssertionError(f"Expected CFBundleIdentifier={QA_BUNDLE_ID!r}, got {info.get('CFBundleIdentifier')!r}")
    if info.get("CFBundleExecutable") != "CodexAgySync":
        raise AssertionError(f"Expected CFBundleExecutable='CodexAgySync', got {info.get('CFBundleExecutable')!r}")
    if info.get("CFBundleIconFile") != "AppIcon":
        raise AssertionError(f"Expected CFBundleIconFile='AppIcon', got {info.get('CFBundleIconFile')!r}")
    if info.get("CFBundleShortVersionString") != PACKAGE_VERSION:
        raise AssertionError(f"Bundle version {info.get('CFBundleShortVersionString')!r} != pyproject {PACKAGE_VERSION!r}")
    if info.get("CFBundleLocalizations") != ["zh-Hans", "en"]:
        raise AssertionError(f"Expected CFBundleLocalizations=['zh-Hans', 'en'], got {info.get('CFBundleLocalizations')!r}")
    return {
        "bundle_id": str(info["CFBundleIdentifier"]),
        "app_icon_bytes": icon_path.stat().st_size,
        "bundled_cli_version": ver_proc.stdout.strip(),
        "bundle_version": str(info["CFBundleShortVersionString"]),
        "localizations": list(info["CFBundleLocalizations"]),
    }


def _seed_interrupted_agy(dirs: dict[str, Path], paths: HostEnvironmentPaths) -> tuple[sqlite3.Connection, int]:
    """Seed an unpaired stopped interrupted AGY session plus active WAL and flock."""
    (dirs["ws_rescue"] / "README.md").write_text("# QA Rescue Workspace\n", encoding="utf-8")
    with contextlib.closing(sqlite3.connect(dirs["codex_home"] / "state_5.sqlite")) as conn, conn:
        ensure_codex_state_db_schema(conn)
    bridge, now_dt = SyncBridge(paths), datetime.now(timezone.utc)
    binding, cid = bridge.registry.resolve_readonly_binding(dirs["ws_rescue"]), CascadeId(_SEED_CID)
    t1 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), (now_dt - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    t2 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-2"), (now_dt - timedelta(seconds=45)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    events = [
        UserInputEvent(kind="user_input", anchor=t1, query_text="Check workspace layout"),
        AssistantTextEvent(kind="assistant_text", anchor=t1, text="Workspace layout verified."),
        UserInputEvent(kind="user_input", anchor=t2, query_text="Run test suite before unexpected stop"),
        ThinkingEvent(kind="thinking", anchor=t2, summary_text="About to run pytest in ws-rescue."),
        ToolCallEvent(
            kind="tool_call",
            anchor=t2,
            call_id=ToolCallId("call-menubar-interrupted-1"),
            tool_name="run_command",
            tool_summary="pytest -q",
            payload=RunCommandPayload(kind="run_command", command_line="pytest -q", cwd=str(dirs["ws_rescue"])),
        ),
    ]
    ledger = TrajectoryLedger.from_events(
        binding.workspace_path,
        binding.workspace_uri,
        events,
        explicit_title_override="QA Rescue Interrupted Session",
        recompute_agy_step_indices=True,
    )
    DefaultHostTransport(paths).apply_to_agy(
        binding=binding,
        cascade_id=cid,
        events=ledger.events,
        full_ledger=ledger,
        lock_snapshot=bridge.guard.probe_locks(cid, None),
        mode=TransportMode.OFFLINE_STORAGE,
    )
    conv_db = dirs["agy_data"] / "conversations" / f"{cid}.db"
    with contextlib.closing(sqlite3.connect(conv_db)) as conn, conn:
        conn.execute("UPDATE steps SET status = 2 WHERE idx = (SELECT MAX(idx) FROM steps)")
    os.utime(conv_db, (time.time() - 30.0, time.time() - 30.0))
    sum_db = dirs["agy_data"] / "conversation_summaries.db"
    wal_conn = sqlite3.connect(sum_db)
    wal_conn.executescript(
        "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE IF NOT EXISTS _wal_probe (id INTEGER PRIMARY KEY); INSERT OR REPLACE INTO _wal_probe (id) VALUES (1);"
    )
    wal_conn.commit()
    wal_path = sum_db.parent / "conversation_summaries.db-wal"
    if not wal_path.is_file() or wal_path.stat().st_size <= 0:
        raise RuntimeError("Failed to create non-empty sandbox conversation_summaries.db-wal.")
    lock_file = dirs["agy_data"] / "presence" / f"{cid}.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text("qa-held-lock\n", encoding="utf-8")
    lock_fd = os.open(str(lock_file), os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return wal_conn, lock_fd


def _find_pids_for_exe(exe_path: str) -> list[int]:
    """Return PIDs whose exact comm matches the QA app executable path."""
    proc = run_cmd(["ps", "-ax", "-o", "pid=,comm="], check=False, timeout=10.0)
    return [int(p[0]) for ln in proc.stdout.splitlines() if len(p := ln.strip().split(None, 1)) == 2 and p[1].strip() == exe_path and p[0].isdigit()]


def _launch_qa_app(app_bundle: Path, env: dict[str, str]) -> int:
    """Launch isolated QA .app bundle via macOS open -n -F with sandbox env vars."""
    argv = ["open", "-n", "-F", *[x for k, v in env.items() for x in ("--env", f"{k}={v}")], str(app_bundle)]
    run_cmd(argv, check=True, timeout=20.0)
    exe_path, deadline = str(app_bundle / "Contents" / "MacOS" / "CodexAgySync"), time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if pids := _find_pids_for_exe(exe_path):
            return pids[-1]
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for QA app PID: {exe_path}")


def _verify_search_filter(pid: int, wid: int, state: dict[str, Any], session_name: str) -> None:
    """Verify unmatched search query hides rows and clearing restores the CTA element."""
    search_el = find_element(state.get("elements", []), "筛选项目或对话标题", "textfield") or find_element(state.get("elements", []), "筛选项目或对话标题")
    if not search_el or not search_el.get("element_token"):
        raise RuntimeError(f"Search textfield token missing: {search_el}")
    cua_call("set_value", {"pid": pid, "element_token": str(search_el["element_token"]), "value": "zzz-unmatched-query", "session": session_name})
    filtered, last_els = False, []
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        cand = cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "both", "include_screenshot": False, "session": session_name})
        last_els = cand.get("elements", [])
        has_empty_msg = find_element(last_els, "未找到匹配的对话") is not None or "未找到匹配的对话" in str(cand.get("tree_markdown") or "")
        if has_empty_msg and find_element(last_els, CTA_LABEL) is None:
            filtered = True
            break
        time.sleep(0.2)
    if not filtered:
        labels = [el.get("label") or el.get("value") for el in last_els]
        raise RuntimeError(f"Search filter did not transition to empty state '未找到匹配的对话': {labels}")
    click_until(
        pid,
        wid,
        "清空筛选",
        session_name,
        lambda: find_element(
            cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "include_screenshot": False, "session": session_name}).get("elements", []),
            CTA_LABEL,
            "button",
        )
        is not None,
    )


def _assert_popover_language(pid: int, panel_wid: int, session_name: str, expected_needles: tuple[str, ...]) -> None:
    """Wait for the pinned popover panel to reflect expected localized AX labels."""
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if all(has_window_element(pid, panel_wid, needle, session_name) for needle in expected_needles):
            return
        time.sleep(0.2)
    raise RuntimeError(f"Popover window {panel_wid} did not switch to expected labels: {expected_needles}")


def _drive_settings_gui(
    pid: int,
    panel_wid: int,
    shots_dir: Path,
    session_name: str,
    release_stub: MockReleaseHttpStub,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Open Settings window, verify auto-update policy UI, and verify live zh-Hans <-> en switching."""
    click_until(
        pid,
        panel_wid,
        "打开设置",
        session_name,
        lambda: any(w.get("title") == SETTINGS_TITLE for w in cua_call("list_windows", {"pid": pid, "session": session_name}).get("windows", [])),
    )
    swid = wait_for_window(pid, SETTINGS_TITLE, session_name, 8.0)
    cua_call("bring_to_front", {"pid": pid, "window_id": swid, "session": session_name})

    def _has_el(needle: str) -> bool:
        return has_window_element(pid, swid, needle, session_name)

    click_until(pid, swid, "设置分栏：常规与同步", session_name, lambda: _has_el("存在未完成回复或待确认对话时显示状态栏圆点"))
    if not _has_el("展开并自动同步首个对话") or not _has_el("简体中文") or not _has_el("English"):
        raise RuntimeError("Settings General tab missing expected hotkey or language controls.")
    cua_call("bring_to_front", {"pid": pid, "window_id": swid, "session": session_name})
    general_shot = shots_dir / "03-settings-general.png"
    cua_call("get_window_state", {"pid": pid, "window_id": swid, "tree_format": "elements", "session": session_name}, screenshot_out=general_shot)
    if not general_shot.is_file() or general_shot.stat().st_size <= 0:
        raise RuntimeError("03-settings-general.png was not written.")

    click_until(pid, swid, "设置分栏：版本与更新", session_name, lambda: _has_el("检查更新"))
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _has_el("双端已同频") and _has_el(f"v{PACKAGE_VERSION}") and _has_el("内置引擎"):
            break
        time.sleep(0.3)
    else:
        raise RuntimeError(f"Settings Updates tab did not display aligned bundled engine v{PACKAGE_VERSION}.")

    for policy_label in ("手动检查", "仅自动检查", "自动下载准备", "上次检查："):
        if not _has_el(policy_label):
            raise RuntimeError(f"Settings Updates tab missing auto-update UI element: {policy_label!r}")

    prior_hits = release_stub.request_count()
    assert prior_hits >= 1, f"Expected opportunistic check before clicking 检查更新, got {prior_hits}"
    click_until(
        pid,
        swid,
        "检查更新",
        session_name,
        lambda: release_stub.request_count() > prior_hits and _has_el(release_stub.tag_name),
        wait_s=4.0,
    )
    assert release_stub.request_count() > prior_hits, (
        f"Manual update check did not issue a new request (prior_hits={prior_hits}, current={release_stub.request_count()})"
    )
    cua_call("bring_to_front", {"pid": pid, "window_id": swid, "session": session_name})
    update_shot = shots_dir / "04-settings-update.png"
    cua_call("get_window_state", {"pid": pid, "window_id": swid, "tree_format": "elements", "session": session_name}, screenshot_out=update_shot)
    if not update_shot.is_file() or update_shot.stat().st_size <= 0:
        raise RuntimeError("04-settings-update.png was not written.")

    click_until(pid, swid, "设置分栏：常规与同步", session_name, lambda: _has_el("界面语言：English"))
    click_until(
        pid,
        swid,
        "界面语言：English",
        session_name,
        lambda: _has_el("Settings tab: General & Sync") and _has_el("Open & Sync Top Session"),
        wait_s=4.0,
    )
    wait_for_window_title(pid, swid, SETTINGS_TITLE_EN, session_name)
    _assert_popover_language(pid, panel_wid, session_name, ("Filter projects or session titles", "Open Settings"))

    click_until(pid, swid, "Settings tab: Version & Updates", session_name, lambda: _has_el("Check Only") and _has_el("Aligned"))
    cua_call("bring_to_front", {"pid": pid, "window_id": swid, "session": session_name})
    english_shot = shots_dir / "05-settings-english.png"
    cua_call("get_window_state", {"pid": pid, "window_id": swid, "tree_format": "elements", "session": session_name}, screenshot_out=english_shot)
    if not english_shot.is_file() or english_shot.stat().st_size <= 0:
        raise RuntimeError("05-settings-english.png was not written.")

    click_until(pid, swid, "Settings tab: General & Sync", session_name, lambda: _has_el("Language: 简体中文"))
    click_until(
        pid,
        swid,
        "Language: 简体中文",
        session_name,
        lambda: _has_el("设置分栏：常规与同步") and _has_el("展开并自动同步首个对话"),
        wait_s=4.0,
    )
    wait_for_window_title(pid, swid, SETTINGS_TITLE, session_name)
    _assert_popover_language(pid, panel_wid, session_name, ("筛选项目或对话标题", "打开设置"))

    checks = {
        "release_api_hits": release_stub.request_count(),
        "auto_update_policy_verified": True,
        "bilingual_live_switch_verified": True,
    }
    return {
        "settings_general": str(general_shot),
        "settings_update": str(update_shot),
        "settings_english": str(english_shot),
    }, checks


def _drive_qa_gui(
    pid: int,
    shots_dir: Path,
    session_name: str,
    codex_stub: CodexWsRpcStub,
    release_stub: MockReleaseHttpStub,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Drive popover handoff and Settings tabs end-to-end, capturing visual receipts."""
    wid = wait_for_window(pid, "CodexAgySync", session_name, 15.0)
    vstate = cua_call(
        "verify_state",
        {
            "pid": pid,
            "window_id": wid,
            "session": session_name,
            "stable_samples": 1,
            "timeout_ms": 10000,
            "expect": [{"element": {"selector": {"label_contains": CTA_LABEL}, "exists": True, "enabled": True}}],
        },
    )
    if "unsatisfied" in json.dumps(vstate).lower():
        raise RuntimeError(f"CTA button never became ready: {vstate}")
    ready_shot, done_shot = shots_dir / "01-hero-ready.png", shots_dir / "02-handoff-done.png"
    state = cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "session": session_name}, screenshot_out=ready_shot)
    cta = find_element(state.get("elements", []), CTA_LABEL, "button")
    if not cta or not cta.get("element_token") or not ready_shot.is_file() or ready_shot.stat().st_size <= 0:
        raise RuntimeError("CTA button token missing or 01-hero-ready.png not written.")
    _verify_search_filter(pid, wid, state, session_name)
    click_until(
        pid,
        wid,
        CTA_LABEL,
        session_name,
        lambda: bool(codex_stub.methods_called)
        or find_element(
            cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "include_screenshot": False, "session": session_name}).get("elements", []),
            DONE_BANNER,
        )
        is not None,
        wait_s=3.0,
    )
    deadline, done, last_texts = time.monotonic() + 25.0, False, []
    while time.monotonic() < deadline:
        state = cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "include_screenshot": False, "session": session_name})
        last_texts = [str(el.get("label") or el.get("value") or el.get("title") or "") for el in state.get("elements", []) if any(el.get(k) for k in ("label", "value", "title"))]
        if find_element(state.get("elements", []), DONE_BANNER) is not None:
            done = True
            break
        err_banner = find_element(state.get("elements", []), "关闭提示：")
        if err_banner is not None and DONE_BANNER not in str(err_banner.get("label") or ""):
            raise RuntimeError(f"Unexpected error banner: {err_banner.get('label')!r}")
        time.sleep(0.4)
    if not done:
        raise RuntimeError(f"Timed out waiting for banner: {DONE_BANNER}; visible={last_texts}")
    cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "session": session_name}, screenshot_out=done_shot)
    if not done_shot.is_file() or done_shot.stat().st_size <= 0:
        raise RuntimeError("02-handoff-done.png was not written.")
    settings_shots, settings_checks = _drive_settings_gui(pid, wid, shots_dir, session_name, release_stub)
    return {"hero_ready": str(ready_shot), "handoff_done": str(done_shot), **settings_shots}, settings_checks


def _verify_sandbox_outputs(paths: HostEnvironmentPaths, ws_rescue: Path, codex_stub: CodexWsRpcStub) -> tuple[dict[str, Any], list[str]]:
    """Verify ready receipt, staging cleanup, RPC online pairing, and forked Codex rollout."""
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((paths.sync_state_dir / "handoffs").glob("*.json")) if p.is_file()]
    ready = [r for r in receipts if r.get("status") == "ready" and r.get("target_id") and not Path(str(r.get("staging_path") or "")).exists()]
    if not ready:
        raise AssertionError(f"Expected ready receipt with cleaned staging dir, found: {receipts}")
    if "project/create" not in codex_stub.methods_called or "thread/fork" not in codex_stub.methods_called:
        raise AssertionError(f"Expected project/create and thread/fork RPC calls, got: {codex_stub.methods_called}")
    if not diagnose_workspace_binding(paths, ws_rescue).is_paired():
        raise AssertionError(f"Expected paired workspace after --auto-pair: {ws_rescue}")
    matched = [str(p.relative_to(paths.codex_home_dir)) for p in sorted(paths.codex_home_dir.rglob("*.jsonl")) if INTERRUPTED_TURN_TEXT in p.read_text(encoding="utf-8")]
    if not matched:
        raise AssertionError(f"Expected Codex rollout containing {INTERRUPTED_TURN_TEXT!r} under {paths.codex_home_dir}")
    return ready[-1], matched


def _teardown_suite(ctx: dict[str, Any]) -> dict[str, Any]:
    """Guarantee cleanup of locks, stubs, QA process, LaunchServices, prefs, and sandbox."""
    receipt: dict[str, Any] = {"killed_pids": [], "unregistered": False, "plist_removed": False, "sandbox_removed": False}
    if ctx.get("cua_session"):
        with contextlib.suppress(Exception):
            cua_call("end_session", {"session": ctx["cua_session"]})
    for obj, meth in (
        (ctx.get("wal_conn"), "close"),
        (ctx.get("codex_stub"), "stop"),
        (ctx.get("agy_stub"), "stop"),
        (ctx.get("release_stub"), "stop"),
    ):
        if obj is not None:
            with contextlib.suppress(Exception):
                getattr(obj, meth)()
    if ctx.get("lock_fd") is not None:
        with contextlib.suppress(OSError):
            os.close(ctx["lock_fd"])
    if isinstance(app_bundle := ctx.get("app_bundle"), Path):
        pids = set(_find_pids_for_exe(str(app_bundle / "Contents" / "MacOS" / "CodexAgySync"))) | ({int(ctx["qa_pid"])} if ctx.get("qa_pid") else set())
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in sorted(pids):
                if run_cmd(["ps", "-p", str(pid), "-o", "comm="], check=False).stdout.strip().startswith(str(app_bundle)):
                    with contextlib.suppress(OSError):
                        os.kill(pid, sig)
                    if sig == signal.SIGTERM and pid not in receipt["killed_pids"]:
                        receipt["killed_pids"].append(pid)
            if sig == signal.SIGTERM and receipt["killed_pids"]:
                time.sleep(0.5)
        if Path(LSREGISTER).is_file():
            receipt["unregistered"] = run_cmd([LSREGISTER, "-u", str(app_bundle)], check=False).returncode == 0
    prefs_dir = Path.home() / "Library" / "Preferences"
    removed_any = False
    for cand in [prefs_dir / f"{QA_BUNDLE_ID}.plist", *prefs_dir.glob(f"ByHost/{QA_BUNDLE_ID}.*.plist")]:
        if cand.exists() or cand.is_symlink():
            cand.unlink(missing_ok=True)
            removed_any = True
    receipt["plist_removed"] = removed_any
    for key in ("sock_path", "sandbox", "app_dir"):
        if isinstance(target := ctx.get(key), Path) and target.exists():
            shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink(missing_ok=True)
    receipt["sandbox_removed"] = not ctx["sandbox"].exists() and not ctx["app_dir"].exists()
    return receipt


def cmd_suite(raw_run_dir: str | None) -> int:
    """Execute full isolated GUI handoff and Settings verification suite with guaranteed teardown."""
    default_dir = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync" / f"verify-menubar-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    _, run_dir = validate_run_dir(Path(raw_run_dir) if raw_run_dir else default_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    release_stub = MockReleaseHttpStub(tag_name=f"v{_NEXT_QA_VERSION}")
    release_url = release_stub.start()
    dirs, env, prefs_suite = _make_sandbox_env(run_dir, release_url)
    before_fp = collect_host_fingerprints()
    ctx: dict[str, Any] = {
        "sandbox": dirs["sandbox"],
        "app_dir": dirs["app_dir"],
        "sock_path": dirs["sock_path"],
        "app_bundle": dirs["app_dir"] / "CodexAgySync.app",
        "prefs_suite": prefs_suite,
        "release_stub": release_stub,
    }
    error_text, shots, bundle_checks, settings_checks, receipt_info, rollouts, rpc_methods = None, {}, {}, {}, None, [], []
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    try:
        run_cmd([str(BUILD_SCRIPT), "--output-dir", str(dirs["app_dir"]), "--bundle-id", QA_BUNDLE_ID], check=True, timeout=180.0)
        bundle_checks = _verify_app_bundle_assets(ctx["app_bundle"])
        before_fp = collect_host_fingerprints()
        paths = HostEnvironmentPaths(dirs["gemini_config"], dirs["agy_data"], dirs["codex_home"], dirs["sync_state"], agy_cli_data_dir=None, codex_app_server_socket=dirs["sock_path"])
        ctx["wal_conn"], ctx["lock_fd"] = _seed_interrupted_agy(dirs, paths)
        ctx["agy_stub"] = AgyLoopbackRpcStub(dirs["agy_data"], dirs["gemini_config"])
        ctx["agy_stub"].start()
        ctx["codex_stub"] = CodexWsRpcStub(dirs["sock_path"], dirs["codex_home"])
        ctx["codex_stub"].start()
        ctx["qa_pid"] = _launch_qa_app(ctx["app_bundle"], env)
        ctx["cua_session"] = f"menubar-qa-{ctx['qa_pid']}"
        shots, settings_checks = _drive_qa_gui(int(ctx["qa_pid"]), dirs["shots_dir"], ctx["cua_session"], ctx["codex_stub"], release_stub)
        receipt_info, rollouts = _verify_sandbox_outputs(paths, dirs["ws_rescue"], ctx["codex_stub"])
    except BaseException as exc:
        error_text = f"{type(exc).__name__}: {exc}"
    finally:
        if ctx.get("codex_stub") is not None:
            rpc_methods = list(ctx["codex_stub"].methods_called)
        teardown = _teardown_suite(ctx)
        unchanged = before_fp == collect_host_fingerprints()
        summary = {
            "status": "passed" if (error_text is None and unchanged) else "failed",
            "completed_at": now_iso(),
            "run_dir": str(run_dir),
            "bundle_checks": bundle_checks,
            "settings_checks": settings_checks,
            "screenshots": shots,
            "receipt": receipt_info,
            "codex_rollouts": rollouts,
            "codex_rpc_methods": rpc_methods,
            "host_fingerprints_unchanged": unchanged,
            "teardown": teardown,
            "error": error_text,
        }
        write_json(run_dir / "summary.json", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if error_text is None and unchanged else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for CodexAgySync verification."""
    parser = argparse.ArgumentParser(description="Verify CodexAgySync menu bar app in an isolated sandbox.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check Swift, Cua Driver permissions, CLI feed command, and build script.")
    suite_p = sub.add_parser("suite", help="Run full isolated GUI handoff verification.")
    suite_p.add_argument("--run-dir", default=None, help="Artifact directory (outside temporary folders).")
    args = parser.parse_args(argv)
    return cmd_doctor() if args.command == "doctor" else cmd_suite(args.run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
