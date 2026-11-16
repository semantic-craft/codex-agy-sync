"""Shared RPC stubs, Cua Driver AX helpers, and subprocess utilities for menubar_verify."""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable
import uuid

from _workbench_sandbox import REPO_ROOT  # noqa: E402

SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
from codex_agy_sync.bridge import ensure_codex_state_db_schema, generate_uuid7  # noqa: E402

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def run_cmd(argv: list[str], *, check: bool = True, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run a local subprocess with captured UTF-8 output."""
    return subprocess.run(argv, text=True, capture_output=True, check=check, timeout=timeout)


def cua_call(
    tool: str,
    payload: dict[str, Any] | None = None,
    *,
    screenshot_out: Path | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Invoke a cua-driver MCP tool via CLI and parse JSON output."""
    argv = ["cua-driver", "call"]
    if screenshot_out is not None:
        argv.extend(["--screenshot-out-file", str(screenshot_out)])
    argv.append(tool)
    if payload is not None:
        argv.append(json.dumps(payload, ensure_ascii=False))
    proc = run_cmd(argv, check=False, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"cua-driver {tool} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def find_element(elements: list[dict[str, Any]], needle: str, prefer_role: str = "") -> dict[str, Any] | None:
    """Locate an AX element whose label, title, or value contains needle."""
    hits = [el for el in elements if any(needle in str(el.get(k) or "") for k in ("label", "title", "value"))]
    if prefer_role:
        for el in hits:
            if prefer_role in str(el.get("role") or "").lower():
                return el
    return hits[0] if hits else None


def wait_for_window(pid: int, title: str, session_name: str, timeout_s: float = 12.0) -> int:
    """Wait until a window with matching title appears for pid."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        wins = cua_call("list_windows", {"pid": pid, "session": session_name}).get("windows", [])
        if match := next(
            (w for w in wins if w.get("title") == title and (w.get("bounds") or {}).get("height", 0) >= 100),
            None,
        ):
            return int(match["window_id"])
        time.sleep(0.25)
    raise RuntimeError(f"Window {title!r} never appeared for pid={pid}.")


def wait_for_window_title(
    pid: int,
    wid: int,
    expected_title: str,
    session_name: str,
    timeout_s: float = 6.0,
) -> None:
    """Wait until window wid updates its title to expected_title."""
    deadline = time.monotonic() + timeout_s
    last_title: Any = None
    while time.monotonic() < deadline:
        wins = cua_call("list_windows", {"pid": pid, "session": session_name}).get("windows", [])
        for w in wins:
            if int(w.get("window_id") or -1) == wid:
                last_title = w.get("title")
                if last_title == expected_title:
                    return
        time.sleep(0.2)
    raise RuntimeError(f"Window {wid} title remained {last_title!r}; expected {expected_title!r}.")


def has_window_element(pid: int, wid: int, needle: str, session_name: str) -> bool:
    """Return whether needle appears in AX elements or tree markdown of window wid."""
    st = cua_call(
        "get_window_state",
        {"pid": pid, "window_id": wid, "tree_format": "both", "include_screenshot": False, "session": session_name},
    )
    return find_element(st.get("elements", []), needle) is not None or needle in str(st.get("tree_markdown") or "")


def click_until(
    pid: int,
    wid: int,
    label: str,
    session_name: str,
    pred: Callable[[], bool],
    wait_s: float = 2.2,
) -> None:
    """Escalate click on label: AX element_token -> background pixel -> foreground pixel until pred()."""
    state = cua_call(
        "get_window_state",
        {"pid": pid, "window_id": wid, "tree_format": "elements", "include_screenshot": False, "session": session_name},
    )
    el = find_element(state.get("elements", []), label, "button") or find_element(state.get("elements", []), label)
    if el and el.get("element_token"):
        with contextlib.suppress(RuntimeError):
            cua_call("click", {"pid": pid, "element_token": str(el["element_token"]), "session": session_name})
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.25)
    for mode in ("background", "foreground"):
        shot_state = cua_call(
            "get_window_state",
            {"pid": pid, "window_id": wid, "tree_format": "elements", "session": session_name},
        )
        if pred():
            return
        fresh = find_element(shot_state.get("elements", []), label, "button") or find_element(
            shot_state.get("elements", []), label
        )
        sf = (fresh or {}).get("screenshot_frame") or {}
        if sf.get("x") is None or sf.get("y") is None or not sf.get("w") or not sf.get("h"):
            raise RuntimeError(f"Element {label!r} missing screenshot_frame bounds: {fresh}")
        payload: dict[str, Any] = {
            "pid": pid,
            "window_id": wid,
            "x": float(sf["x"]) + float(sf["w"]) / 2.0,
            "y": float(sf["y"]) + float(sf["h"]) / 2.0,
            "delivery_mode": mode,
            "session": session_name,
        }
        if shot_state.get("capture_id"):
            payload["capture_id"] = shot_state["capture_id"]
        with contextlib.suppress(RuntimeError):
            cua_call("click", payload)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.25)
    raise RuntimeError(f"Action on {label!r} did not satisfy post-condition.")


class MockReleaseHttpStub:
    """Loopback HTTP server serving a deterministic GitHub Release JSON payload."""

    def __init__(self, tag_name: str) -> None:
        self.tag_name = tag_name
        self.requests: list[str] = []
        self._lock = threading.Lock()
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.api_url = ""

    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def start(self) -> str:
        """Bind ephemeral loopback port and serve /releases/latest."""
        stub = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                with stub._lock:
                    stub.requests.append(self.path)
                base = f"http://127.0.0.1:{self.server.server_port}"
                wheel_ver = stub.tag_name.lstrip("vV") or "0.0.0"
                payload = {
                    "tag_name": stub.tag_name,
                    "html_url": f"https://github.com/semantic-craft/codex-agy-sync/releases/tag/{stub.tag_name}",
                    "body": "QA mock release: unified Settings window and dual sync update",
                    "assets": [
                        {"name": "SHA256SUMS", "browser_download_url": f"{base}/SHA256SUMS", "size": 128},
                        {
                            "name": f"codex_agy_sync-{wheel_ver}-py3-none-any.whl",
                            "browser_download_url": f"{base}/pkg.whl",
                            "size": 64,
                        },
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
    hdr = struct.pack("!BB", head, n) if n < 126 else (
        struct.pack("!BBH", head, 126, n) if n < 65536 else struct.pack("!BBQ", head, 127, n)
    )
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
            ws_key = next(
                (ln.split(":", 1)[1].strip() for ln in hdr.decode("latin1").splitlines() if ln.lower().startswith("sec-websocket-key:")),
                "",
            )
            accept = base64.b64encode(hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
            conn.sendall(
                f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii")
            )
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
                    conn.sendall(
                        _pack_ws_frame(
                            0x1,
                            json.dumps({"id": msg["id"], "result": self._dispatch(method, dict(msg.get("params") or {}))}).encode("utf-8"),
                        )
                    )

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
        pid, name, roots, now_ms = (
            str(uuid.uuid4()),
            str(params.get("name") or "ws-rescue"),
            [dict(r) for r in params.get("roots", [])],
            int(time.time() * 1000),
        )
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn, conn:
            ensure_codex_state_db_schema(conn)
            conn.execute(
                "INSERT INTO projects (id, name, metadata, position, created_at_ms, updated_at_ms) VALUES (?, ?, '{}', ?, ?, ?)",
                (pid, name, len(self._projects), now_ms, now_ms),
            )
            for idx, root in enumerate(roots):
                conn.execute("INSERT INTO project_roots (project_id, position, path) VALUES (?, ?, ?)", (pid, idx, str(root.get("path") or "")))
        proj = {"id": pid, "name": name, "roots": roots}
        self._projects.append(proj)
        return proj

    def _create_thread(self, params: dict[str, Any], *, copy_rollout: bool, tid_override: str | None = None) -> dict[str, Any]:
        tid, now_dt = tid_override or generate_uuid7(), datetime.now(timezone.utc)
        dest = (
            self.codex_home
            / "sessions"
            / now_dt.strftime("%Y/%m/%d")
            / f"rollout-{now_dt.strftime('%Y-%m-%dT%H-%M-%S')}-{tid}.jsonl"
        )
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
