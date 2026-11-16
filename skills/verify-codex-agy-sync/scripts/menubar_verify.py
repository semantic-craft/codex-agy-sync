#!/usr/bin/env python3
"""Isolated end-to-end QA verifier for the CodexAgyHandoff macOS menu bar app."""
from __future__ import annotations

import argparse, base64, contextlib, fcntl, hashlib, http.server, json, os, plistlib, shutil, signal, socket, sqlite3, struct, subprocess, sys, threading, time, tomllib, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _workbench_sandbox import AgyLoopbackRpcStub, CLI_BIN, REPO_ROOT, capture_host_fingerprints, now_iso, validate_run_dir, write_json  # noqa: E402

SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
from codex_agy_sync import DefaultHostTransport, HostEnvironmentPaths, HostKind, INTERRUPTED_TURN_TEXT, SyncBridge, TrajectoryLedger, TransportMode  # noqa: E402
from codex_agy_sync.bridge import diagnose_workspace_binding, ensure_codex_state_db_schema, generate_uuid7  # noqa: E402
from codex_agy_sync.events import AssistantTextEvent, CascadeId, EventOriginAnchor, ThinkingEvent, ToolCallEvent, ToolCallId, TurnId, UserInputEvent  # noqa: E402
from codex_agy_sync.payloads import RunCommandPayload  # noqa: E402

QA_BUNDLE_ID = "dev.semanticcraft.codex-agy-handoff.qa"
CTA_LABEL = "用 Codex 继续"
DONE_BANNER = "已在 Codex 打开对话"
SETTINGS_TITLE = "CodexAgyHandoff 设置"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_menubar_app.sh"
PACKAGE_VERSION = str(tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
_VER_PARTS = PACKAGE_VERSION.split(".")
_NEXT_QA_VERSION = f"{_VER_PARTS[0]}.{int(_VER_PARTS[1]) + 1}.0"
LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_SEED_CID = "00000000-0000-4000-8000-000000000091"


def _run_cmd(argv: list[str], *, check: bool = True, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run a local subprocess with captured UTF-8 output."""
    return subprocess.run(argv, text=True, capture_output=True, check=check, timeout=timeout)


def _cua_call(tool: str, payload: dict[str, Any] | None = None, *, screenshot_out: Path | None = None, timeout: float = 20.0) -> dict[str, Any]:
    """Invoke a cua-driver MCP tool via CLI and parse JSON output."""
    argv = ["cua-driver", "call"]
    if screenshot_out is not None:
        argv.extend(["--screenshot-out-file", str(screenshot_out)])
    argv.append(tool)
    if payload is not None:
        argv.append(json.dumps(payload, ensure_ascii=False))
    proc = _run_cmd(argv, check=False, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"cua-driver {tool} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def collect_host_fingerprints() -> dict[str, dict[str, Any]]:
    """Capture host main SQLite fingerprints matching _workbench_sandbox."""
    return capture_host_fingerprints()


def _check_cua_permissions() -> dict[str, Any]:
    """Inspect Cua Driver daemon availability and TCC permissions."""
    proc = _run_cmd(["cua-driver", "permissions", "status", "--json"], check=False, timeout=15.0)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid JSON: {exc}"}
    ok = bool(data.get("daemon_running") and data.get("accessibility") and data.get("screen_recording"))
    return {"ok": ok, "daemon_running": bool(data.get("daemon_running")), "accessibility": bool(data.get("accessibility")), "screen_recording": bool(data.get("screen_recording"))}


def cmd_doctor() -> int:
    """Verify Swift toolchain, Cua permissions, CLI feed command, icon generator, and build script."""
    swift_p = _run_cmd(["swift", "--version"], check=False, timeout=15.0)
    feed_p = _run_cmd([str(CLI_BIN), "menubar-feed", "--help"], check=False, timeout=15.0)
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


class MockReleaseHttpStub:
    """Loopback HTTP server serving a deterministic GitHub Release JSON payload."""

    def __init__(self, tag_name: str = f"v{_NEXT_QA_VERSION}") -> None:
        self.tag_name = tag_name
        self.requests: list[str] = []
        self._lock = threading.Lock()
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.api_url = ""

    def start(self) -> str:
        """Bind ephemeral loopback port and serve /releases/latest."""
        stub = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                with stub._lock:
                    stub.requests.append(self.path)
                base = f"http://127.0.0.1:{self.server.server_port}"
                wheel_ver = stub.tag_name.lstrip("vV") or _NEXT_QA_VERSION
                payload = {
                    "tag_name": stub.tag_name,
                    "html_url": f"https://github.com/semantic-craft/codex-agy-sync/releases/tag/{stub.tag_name}",
                    "body": "QA mock release: unified Settings window and dual sync update",
                    "assets": [
                        {"name": "SHA256SUMS", "browser_download_url": f"{base}/SHA256SUMS", "size": 128},
                        {"name": f"codex_agy_sync-{wheel_ver}-py3-none-any.whl", "browser_download_url": f"{base}/pkg.whl", "size": 64},
                        {"name": "CodexAgyHandoff-macos.zip", "browser_download_url": f"{base}/app.zip", "size": 64},
                    ],
                }
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.api_url = f"http://127.0.0.1:{self._httpd.server_port}/releases/latest"
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self.api_url

    def stop(self) -> None:
        """Shut down loopback HTTP server cleanly."""
        if self._httpd is not None:
            with contextlib.suppress(Exception):
                self._httpd.shutdown()
                self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def _pack_ws_frame(opcode: int, payload: bytes) -> bytes:
    """Encode an unmasked WebSocket server frame."""
    n, head = len(payload), 0x80 | (opcode & 0x0F)
    hdr = struct.pack("!BB", head, n) if n < 126 else (struct.pack("!BBH", head, 126, n) if n < 65536 else struct.pack("!BBQ", head, 127, n))
    return hdr + payload


def _read_ws_frame(conn: socket.socket, buf: bytearray) -> tuple[int, bytes]:
    """Read and unmask one WebSocket frame from a client socket."""
    def _exact(n: int) -> bytes:
        while len(buf) < n:
            if not (chunk := conn.recv(max(4096, n - len(buf)))):
                raise EOFError("WebSocket client closed connection.")
            buf.extend(chunk)
        out = bytes(buf[:n])
        del buf[:n]
        return out

    b0, b1 = _exact(2)
    op, masked, n = b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F
    n = struct.unpack("!H", _exact(2))[0] if n == 126 else (struct.unpack("!Q", _exact(8))[0] if n == 127 else n)
    mask, payload = _exact(4) if masked else b"", _exact(n) if n else b""
    if masked and n:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return op, payload


class CodexWsRpcStub:
    """Lightweight AF_UNIX WebSocket JSON-RPC stub simulating Codex app-server."""

    def __init__(self, sock_path: Path, codex_home: Path) -> None:
        self.sock_path, self.codex_home, self.state_db = sock_path, codex_home, codex_home / "state_5.sqlite"
        self.methods_called: list[str] = []
        self._projects: list[dict[str, Any]] = []
        self._threads: dict[str, dict[str, Any]] = {}
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._srv: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind AF_UNIX socket within sun_path bounds and begin accepting clients."""
        if len(os.fsencode(str(self.sock_path))) > 96:
            raise ValueError(f"Unix socket path exceeds 96 bytes for macOS sun_path safety: {self.sock_path}")
        self.sock_path.unlink(missing_ok=True)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self.sock_path))
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Shut down listener socket cleanly."""
        self._stop.set()
        if self._srv is not None:
            with contextlib.suppress(OSError):
                self._srv.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.sock_path.unlink(missing_ok=True)

    def _accept_loop(self) -> None:
        assert self._srv is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
                threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
            except TimeoutError:
                continue
            except OSError:
                break

    def _handle_conn(self, conn: socket.socket) -> None:
        buf = bytearray()
        with contextlib.closing(conn), contextlib.suppress(EOFError, OSError):
            conn.settimeout(5.0)
            while b"\r\n\r\n" not in buf and (chunk := conn.recv(4096)):
                buf.extend(chunk)
            if b"\r\n\r\n" not in buf:
                return
            hdr, rest = bytes(buf).split(b"\r\n\r\n", 1)
            buf = bytearray(rest)
            ws_key = next((ln.split(":", 1)[1].strip() for ln in hdr.decode("latin1").splitlines() if ln.lower().startswith("sec-websocket-key:")), "")
            accept = base64.b64encode(hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
            conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii"))
            while not self._stop.is_set():
                op, raw = _read_ws_frame(conn, buf)
                if op == 0x8:
                    return
                if op != 0x1:
                    continue
                msg = json.loads(raw.decode("utf-8"))
                method = str(msg.get("method") or "")
                with self._lock:
                    self.methods_called.append(method)
                if "id" in msg:
                    conn.sendall(_pack_ws_frame(0x1, json.dumps({"id": msg["id"], "result": self._dispatch(method, dict(msg.get("params") or {}))}).encode("utf-8")))

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "project/list":
            return {"data": list(self._projects), "nextCursor": None}
        if method == "project/create":
            return {"project": self._create_project(params)}
        if method == "thread/start":
            return {"thread": self._create_thread(params, copy_rollout=False)}
        if method == "thread/resume":
            tid = str(params.get("threadId") or "")
            return {"thread": self._threads.get(tid) or self._create_thread(params, copy_rollout=False, tid_override=tid or None)}
        if method == "thread/fork":
            return {"thread": self._create_thread(params, copy_rollout=True)}
        if method == "thread/metadata/update":
            self._update_thread(str(params["threadId"]), project_id=str(params.get("projectId") or ""))
        elif method == "thread/name/set":
            self._update_thread(str(params["threadId"]), title=str(params.get("name") or ""))
        elif method == "thread/read":
            tid = str(params.get("threadId") or "")
            info = self._threads.get(tid, {"id": tid, "projectId": None, "name": "Handoff"})
            turns = [{"id": "turn-1", "status": "completed", "items": [{"type": "userMessage", "content": [{"type": "text", "text": "QA"}]}]}]
            return {"thread": {**info, "status": {"type": "idle"}, "turns": turns}}
        elif method == "thread/unsubscribe":
            return {"status": "notLoaded"}
        return {}

    def _create_project(self, params: dict[str, Any]) -> dict[str, Any]:
        pid, name, roots, now_ms = str(uuid.uuid4()), str(params.get("name") or "ws-rescue"), [dict(r) for r in params.get("roots", [])], int(time.time() * 1000)
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn, conn:
            ensure_codex_state_db_schema(conn)
            conn.execute("INSERT INTO projects (id, name, metadata, position, created_at_ms, updated_at_ms) VALUES (?, ?, '{}', ?, ?, ?)", (pid, name, len(self._projects), now_ms, now_ms))
            for idx, root in enumerate(roots):
                conn.execute("INSERT INTO project_roots (project_id, position, path) VALUES (?, ?, ?)", (pid, idx, str(root.get("path") or "")))
        proj = {"id": pid, "name": name, "roots": roots}
        self._projects.append(proj)
        return proj

    def _create_thread(self, params: dict[str, Any], *, copy_rollout: bool, tid_override: str | None = None) -> dict[str, Any]:
        tid, now_dt = tid_override or generate_uuid7(), datetime.now(timezone.utc)
        dest = self.codex_home / "sessions" / now_dt.strftime("%Y/%m/%d") / f"rollout-{now_dt.strftime('%Y-%m-%dT%H-%M-%S')}-{tid}.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        src_raw, cwd = str(params.get("path") or ""), str(params.get("cwd") or "")
        if copy_rollout and src_raw and Path(src_raw).is_file():
            lines = Path(src_raw).read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip() and (first := json.loads(lines[0])).get("type") == "session_meta" and isinstance(first.get("payload"), dict):
                first["payload"]["id"] = tid
                lines[0] = json.dumps(first, ensure_ascii=False)
            dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            dest.write_text(json.dumps({"type": "session_meta", "payload": {"id": tid, "cwd": cwd}}) + "\n", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn, conn:
            ensure_codex_state_db_schema(conn)
            conn.execute(
                "INSERT OR REPLACE INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, has_user_event, archived, preview, name) VALUES (?, ?, ?, ?, 'exec', 'openai', ?, 'Handoff', '{}', 'never', 1, 0, 'Handoff', 'Handoff')",
                (tid, str(dest), int(now_dt.timestamp()), int(now_dt.timestamp()), cwd),
            )
        self._threads[tid] = {"id": tid, "projectId": None, "name": "Handoff", "rollout_path": str(dest)}
        return self._threads[tid]

    def _update_thread(self, tid: str, *, project_id: str | None = None, title: str | None = None) -> None:
        info = self._threads.setdefault(tid, {"id": tid, "projectId": None, "name": "Handoff"})
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn, conn:
            if project_id is not None:
                info["projectId"] = project_id
                conn.execute("UPDATE threads SET project_id = ? WHERE id = ?", (project_id, tid))
            if title is not None:
                info["name"] = title
                conn.execute("UPDATE threads SET title = ?, name = ?, preview = ? WHERE id = ?", (title, title, title, tid))


def _make_sandbox_env(run_dir: Path, release_api_url: str) -> tuple[dict[str, Path], dict[str, str], str]:
    """Build isolated sandbox paths, prefs suite, and app environment variables."""
    sb = run_dir / "_sandbox"
    dirs = {
        "sandbox": sb, "ws_rescue": sb / "ws-rescue", "gemini_home": sb / "gemini-home", "gemini_config": sb / "gemini-config",
        "agy_data": sb / "gemini-home" / "antigravity", "codex_home": sb / "codex-home", "sync_state": sb / "sync-state",
        "sock_path": run_dir / "c.sock", "app_dir": run_dir / "app", "shots_dir": run_dir / "screenshots",
    }
    for k, p in dirs.items():
        if k not in {"sandbox", "sock_path"}:
            p.mkdir(parents=True, exist_ok=True)
    prefs_suite = f"{QA_BUNDLE_ID}.prefs.{os.getpid()}.{int(time.time())}"
    env = {
        "GEMINI_HOME": str(dirs["gemini_home"]), "GEMINI_CONFIG_DIR": str(dirs["gemini_config"]), "ANTIGRAVITY_DATA_DIR": str(dirs["agy_data"]),
        "CODEX_HOME": str(dirs["codex_home"]), "CODEX_AGY_SYNC_STATE_DIR": str(dirs["sync_state"]), "CODEX_APP_SERVER_SOCKET": str(dirs["sock_path"]),
        "CODEX_AGY_SYNC_BIN": str(CLI_BIN), "PATH": os.environ.get("PATH", ""), "MENUBAR_AUTO_OPEN_PANEL": "1",
        "MENUBAR_PIN_PANEL": "1", "MENUBAR_SKIP_APP_ACTIVATION": "1", "MENUBAR_DISABLE_GLOBAL_HOTKEY": "1",
        "MENUBAR_PREFS_SUITE": prefs_suite, "MENUBAR_DISABLE_LOGIN_ITEM": "1", "MENUBAR_RELEASE_API_URL": release_api_url,
    }
    return dirs, env, prefs_suite


def _verify_app_bundle_assets(app_bundle: Path) -> dict[str, Any]:
    """Assert AppIcon.icns exists and Info.plist matches bundle ID and pyproject.toml version."""
    icon_path = app_bundle / "Contents" / "Resources" / "AppIcon.icns"
    plist_path = app_bundle / "Contents" / "Info.plist"
    if not icon_path.is_file() or icon_path.stat().st_size <= 10_000:
        raise AssertionError(f"Expected non-trivial AppIcon.icns at {icon_path}")
    info = plistlib.loads(plist_path.read_bytes())
    if info.get("CFBundleIdentifier") != QA_BUNDLE_ID:
        raise AssertionError(f"Expected CFBundleIdentifier={QA_BUNDLE_ID!r}, got {info.get('CFBundleIdentifier')!r}")
    if info.get("CFBundleIconFile") != "AppIcon":
        raise AssertionError(f"Expected CFBundleIconFile='AppIcon', got {info.get('CFBundleIconFile')!r}")
    if info.get("CFBundleShortVersionString") != PACKAGE_VERSION:
        raise AssertionError(f"Bundle version {info.get('CFBundleShortVersionString')!r} != pyproject {PACKAGE_VERSION!r}")
    return {
        "bundle_id": str(info["CFBundleIdentifier"]),
        "app_icon_bytes": icon_path.stat().st_size,
        "bundle_version": str(info["CFBundleShortVersionString"]),
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
        ToolCallEvent(kind="tool_call", anchor=t2, call_id=ToolCallId("call-menubar-interrupted-1"), tool_name="run_command", tool_summary="pytest -q", payload=RunCommandPayload(kind="run_command", command_line="pytest -q", cwd=str(dirs["ws_rescue"]))),
    ]
    ledger = TrajectoryLedger.from_events(binding.workspace_path, binding.workspace_uri, events, explicit_title_override="QA Rescue Interrupted Session", recompute_agy_step_indices=True)
    DefaultHostTransport(paths).apply_to_agy(binding=binding, cascade_id=cid, events=ledger.events, full_ledger=ledger, lock_snapshot=bridge.guard.probe_locks(cid, None), mode=TransportMode.OFFLINE_STORAGE)
    conv_db = dirs["agy_data"] / "conversations" / f"{cid}.db"
    with contextlib.closing(sqlite3.connect(conv_db)) as conn, conn:
        conn.execute("UPDATE steps SET status = 2 WHERE idx = (SELECT MAX(idx) FROM steps)")
    os.utime(conv_db, (time.time() - 30.0, time.time() - 30.0))
    sum_db = dirs["agy_data"] / "conversation_summaries.db"
    wal_conn = sqlite3.connect(sum_db)
    wal_conn.executescript("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE IF NOT EXISTS _wal_probe (id INTEGER PRIMARY KEY); INSERT OR REPLACE INTO _wal_probe (id) VALUES (1);")
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
    proc = _run_cmd(["ps", "-ax", "-o", "pid=,comm="], check=False, timeout=10.0)
    return [int(p[0]) for ln in proc.stdout.splitlines() if len(p := ln.strip().split(None, 1)) == 2 and p[1].strip() == exe_path and p[0].isdigit()]


def _launch_qa_app(app_bundle: Path, env: dict[str, str]) -> int:
    """Launch isolated QA .app bundle via macOS open -n -F with sandbox env vars."""
    argv = ["open", "-n", "-F", *[x for k, v in env.items() for x in ("--env", f"{k}={v}")], str(app_bundle)]
    _run_cmd(argv, check=True, timeout=20.0)
    exe_path, deadline = str(app_bundle / "Contents" / "MacOS" / "CodexAgyHandoff"), time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if pids := _find_pids_for_exe(exe_path):
            return pids[-1]
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for QA app PID: {exe_path}")


def _find_element(elements: list[dict[str, Any]], needle: str, prefer_role: str = "") -> dict[str, Any] | None:
    """Locate an AX element whose label, title, or value contains needle."""
    hits = [el for el in elements if any(needle in str(el.get(k) or "") for k in ("label", "title", "value"))]
    if prefer_role:
        for el in hits:
            if prefer_role in str(el.get("role") or "").lower():
                return el
    return hits[0] if hits else None


def _wait_for_window(pid: int, title: str, session_name: str, timeout_s: float = 12.0) -> int:
    """Wait until a window with matching title appears for pid."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        wins = _cua_call("list_windows", {"pid": pid, "session": session_name}).get("windows", [])
        if match := next((w for w in wins if w.get("title") == title and (w.get("bounds") or {}).get("height", 0) >= 100), None):
            return int(match["window_id"])
        time.sleep(0.25)
    raise RuntimeError(f"Window {title!r} never appeared for pid={pid}.")


def _click_until(pid: int, wid: int, label: str, session_name: str, pred: Callable[[], bool], wait_s: float = 2.2) -> None:
    """Escalate click on label: AX element_token -> background pixel -> foreground pixel until pred()."""
    state = _cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "include_screenshot": False, "session": session_name})
    el = _find_element(state.get("elements", []), label, "button") or _find_element(state.get("elements", []), label)
    if el and el.get("element_token"):
        with contextlib.suppress(RuntimeError):
            _cua_call("click", {"pid": pid, "element_token": str(el["element_token"]), "session": session_name})
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.25)
    for mode in ("background", "foreground"):
        shot_state = _cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "session": session_name})
        if pred():
            return
        fresh = _find_element(shot_state.get("elements", []), label, "button") or _find_element(shot_state.get("elements", []), label)
        sf = (fresh or {}).get("screenshot_frame") or {}
        if sf.get("x") is None or sf.get("y") is None or not sf.get("w") or not sf.get("h"):
            raise RuntimeError(f"Element {label!r} missing screenshot_frame bounds: {fresh}")
        payload: dict[str, Any] = {
            "pid": pid, "window_id": wid, "x": float(sf["x"]) + float(sf["w"]) / 2.0, "y": float(sf["y"]) + float(sf["h"]) / 2.0,
            "delivery_mode": mode, "session": session_name,
        }
        if shot_state.get("capture_id"):
            payload["capture_id"] = shot_state["capture_id"]
        with contextlib.suppress(RuntimeError):
            _cua_call("click", payload)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.25)
    raise RuntimeError(f"Action on {label!r} did not satisfy post-condition.")


def _verify_search_filter(pid: int, wid: int, state: dict[str, Any], session_name: str) -> None:
    """Verify unmatched search query hides rows and clearing restores the CTA element."""
    search_el = _find_element(state.get("elements", []), "筛选项目或对话标题", "textfield") or _find_element(state.get("elements", []), "筛选项目或对话标题")
    if not search_el or not search_el.get("element_token"):
        raise RuntimeError(f"Search textfield token missing: {search_el}")
    _cua_call("set_value", {"pid": pid, "element_token": str(search_el["element_token"]), "value": "zzz-unmatched-query", "session": session_name})
    filtered, last_els = False, []
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        cand = _cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "both", "include_screenshot": False, "session": session_name})
        last_els = cand.get("elements", [])
        has_empty_msg = _find_element(last_els, "未找到匹配的对话") is not None or "未找到匹配的对话" in str(cand.get("tree_markdown") or "")
        if has_empty_msg and _find_element(last_els, CTA_LABEL) is None:
            filtered = True
            break
        time.sleep(0.2)
    if not filtered:
        labels = [el.get("label") or el.get("value") for el in last_els]
        raise RuntimeError(f"Search filter did not transition to empty state '未找到匹配的对话': {labels}")
    _click_until(
        pid, wid, "清空筛选", session_name,
        lambda: _find_element(
            _cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "include_screenshot": False, "session": session_name}).get("elements", []),
            CTA_LABEL, "button",
        ) is not None,
    )


def _drive_settings_gui(
    pid: int, panel_wid: int, shots_dir: Path, session_name: str, prefs_suite: str, release_stub: MockReleaseHttpStub
) -> tuple[dict[str, str], dict[str, Any]]:
    """Open Settings window, verify general settings tab, and verify version alignment + update check."""
    _click_until(
        pid, panel_wid, "打开设置", session_name,
        lambda: any(w.get("title") == SETTINGS_TITLE for w in _cua_call("list_windows", {"pid": pid, "session": session_name}).get("windows", [])),
    )
    swid = _wait_for_window(pid, SETTINGS_TITLE, session_name, 8.0)
    _cua_call("bring_to_front", {"pid": pid, "window_id": swid, "session": session_name})

    def _has_settings_el(needle: str) -> bool:
        st = _cua_call("get_window_state", {"pid": pid, "window_id": swid, "tree_format": "both", "include_screenshot": False, "session": session_name})
        return _find_element(st.get("elements", []), needle) is not None or needle in str(st.get("tree_markdown") or "")

    _click_until(pid, swid, "设置分栏：常规与接力", session_name, lambda: _has_settings_el("存在未完成回复或待确认对话时显示状态栏圆点"))
    _cua_call("bring_to_front", {"pid": pid, "window_id": swid, "session": session_name})
    general_shot = shots_dir / "03-settings-general.png"
    _cua_call("get_window_state", {"pid": pid, "window_id": swid, "tree_format": "elements", "session": session_name}, screenshot_out=general_shot)
    if not general_shot.is_file() or general_shot.stat().st_size <= 0:
        raise RuntimeError("03-settings-general.png was not written.")

    _click_until(pid, swid, "设置分栏：版本与同步更新", session_name, lambda: _has_settings_el("检查更新"))
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _has_settings_el("双端已同频") and _has_settings_el(f"v{PACKAGE_VERSION}"):
            break
        time.sleep(0.3)
    else:
        raise RuntimeError(f"Settings Updates tab did not display aligned version v{PACKAGE_VERSION}.")

    _click_until(
        pid, swid, "检查更新", session_name,
        lambda: bool(release_stub.requests) and _has_settings_el(release_stub.tag_name),
        wait_s=4.0,
    )
    _cua_call("bring_to_front", {"pid": pid, "window_id": swid, "session": session_name})
    update_shot = shots_dir / "04-settings-update.png"
    _cua_call("get_window_state", {"pid": pid, "window_id": swid, "tree_format": "elements", "session": session_name}, screenshot_out=update_shot)
    if not update_shot.is_file() or update_shot.stat().st_size <= 0:
        raise RuntimeError("04-settings-update.png was not written.")
    checks = {"release_api_hits": len(release_stub.requests)}
    return {"settings_general": str(general_shot), "settings_update": str(update_shot)}, checks


def _drive_qa_gui(
    pid: int, shots_dir: Path, session_name: str, codex_stub: CodexWsRpcStub, prefs_suite: str, release_stub: MockReleaseHttpStub
) -> tuple[dict[str, str], dict[str, Any]]:
    """Drive popover handoff and Settings tabs end-to-end, capturing visual receipts."""
    wid = _wait_for_window(pid, "CodexAgyHandoff", session_name, 15.0)
    vstate = _cua_call("verify_state", {"pid": pid, "window_id": wid, "session": session_name, "stable_samples": 1, "timeout_ms": 10000, "expect": [{"element": {"selector": {"label_contains": CTA_LABEL}, "exists": True, "enabled": True}}]})
    if "unsatisfied" in json.dumps(vstate).lower():
        raise RuntimeError(f"CTA button never became ready: {vstate}")
    ready_shot, done_shot = shots_dir / "01-hero-ready.png", shots_dir / "02-handoff-done.png"
    state = _cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "session": session_name}, screenshot_out=ready_shot)
    cta = _find_element(state.get("elements", []), CTA_LABEL, "button")
    if not cta or not cta.get("element_token") or not ready_shot.is_file() or ready_shot.stat().st_size <= 0:
        raise RuntimeError("CTA button token missing or 01-hero-ready.png not written.")
    _verify_search_filter(pid, wid, state, session_name)
    _click_until(
        pid, wid, CTA_LABEL, session_name,
        lambda: bool(codex_stub.methods_called) or _find_element(
            _cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "include_screenshot": False, "session": session_name}).get("elements", []),
            DONE_BANNER,
        ) is not None,
        wait_s=3.0,
    )
    deadline, done, last_texts = time.monotonic() + 25.0, False, []
    while time.monotonic() < deadline:
        state = _cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "include_screenshot": False, "session": session_name})
        last_texts = [str(el.get("label") or el.get("value") or el.get("title") or "") for el in state.get("elements", []) if any(el.get(k) for k in ("label", "value", "title"))]
        if _find_element(state.get("elements", []), DONE_BANNER) is not None:
            done = True
            break
        err_banner = _find_element(state.get("elements", []), "关闭提示：")
        if err_banner is not None and DONE_BANNER not in str(err_banner.get("label") or ""):
            raise RuntimeError(f"Unexpected error banner: {err_banner.get('label')!r}")
        time.sleep(0.4)
    if not done:
        raise RuntimeError(f"Timed out waiting for banner: {DONE_BANNER}; visible={last_texts}")
    _cua_call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "session": session_name}, screenshot_out=done_shot)
    if not done_shot.is_file() or done_shot.stat().st_size <= 0:
        raise RuntimeError("02-handoff-done.png was not written.")
    settings_shots, settings_checks = _drive_settings_gui(pid, wid, shots_dir, session_name, prefs_suite, release_stub)
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
            _cua_call("end_session", {"session": ctx["cua_session"]})
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
        pids = set(_find_pids_for_exe(str(app_bundle / "Contents" / "MacOS" / "CodexAgyHandoff"))) | ({int(ctx["qa_pid"])} if ctx.get("qa_pid") else set())
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in sorted(pids):
                if _run_cmd(["ps", "-p", str(pid), "-o", "comm="], check=False).stdout.strip().startswith(str(app_bundle)):
                    with contextlib.suppress(OSError):
                        os.kill(pid, sig)
                    if sig == signal.SIGTERM and pid not in receipt["killed_pids"]:
                        receipt["killed_pids"].append(pid)
            if sig == signal.SIGTERM and receipt["killed_pids"]:
                time.sleep(0.5)
        if Path(LSREGISTER).is_file():
            receipt["unregistered"] = _run_cmd([LSREGISTER, "-u", str(app_bundle)], check=False).returncode == 0
    prefs_dir = Path.home() / "Library" / "Preferences"
    removed_any = False
    for domain in filter(None, [QA_BUNDLE_ID, ctx.get("prefs_suite")]):
        _run_cmd(["defaults", "delete", str(domain)], check=False)
        plist = prefs_dir / f"{domain}.plist"
        if plist.is_file():
            plist.unlink(missing_ok=True)
            removed_any = True
    if removed_any:
        _run_cmd(["killall", "cfprefsd"], check=False)
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
    release_stub = MockReleaseHttpStub()
    release_url = release_stub.start()
    dirs, env, prefs_suite = _make_sandbox_env(run_dir, release_url)
    before_fp = collect_host_fingerprints()
    ctx: dict[str, Any] = {
        "sandbox": dirs["sandbox"], "app_dir": dirs["app_dir"], "sock_path": dirs["sock_path"],
        "app_bundle": dirs["app_dir"] / "CodexAgyHandoff.app", "prefs_suite": prefs_suite, "release_stub": release_stub,
    }
    error_text, shots, bundle_checks, settings_checks, receipt_info, rollouts, rpc_methods = None, {}, {}, {}, None, [], []
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    try:
        _run_cmd([str(BUILD_SCRIPT), "--output-dir", str(dirs["app_dir"]), "--bundle-id", QA_BUNDLE_ID], check=True, timeout=180.0)
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
        shots, settings_checks = _drive_qa_gui(int(ctx["qa_pid"]), dirs["shots_dir"], ctx["cua_session"], ctx["codex_stub"], prefs_suite, release_stub)
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
            "completed_at": now_iso(), "run_dir": str(run_dir), "bundle_checks": bundle_checks,
            "settings_checks": settings_checks, "screenshots": shots, "receipt": receipt_info,
            "codex_rollouts": rollouts, "codex_rpc_methods": rpc_methods,
            "host_fingerprints_unchanged": unchanged, "teardown": teardown, "error": error_text,
        }
        write_json(run_dir / "summary.json", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if error_text is None and unchanged else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for CodexAgyHandoff verification."""
    parser = argparse.ArgumentParser(description="Verify CodexAgyHandoff menu bar app in an isolated sandbox.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check Swift, Cua Driver permissions, CLI feed command, and build script.")
    suite_p = sub.add_parser("suite", help="Run full isolated GUI handoff verification.")
    suite_p.add_argument("--run-dir", default=None, help="Artifact directory (outside temporary folders).")
    args = parser.parse_args(argv)
    return cmd_doctor() if args.command == "doctor" else cmd_suite(args.run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
