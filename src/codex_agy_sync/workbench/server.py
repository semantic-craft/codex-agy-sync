"""Loopback-only HTTP server and lifecycle coordinator for Stage-1 Workbench."""

from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import time
from typing import Any, Callable
import webbrowser

from ..bridge.paths_and_locks import HostEnvironmentPaths, now_iso
from .handler import (
    WorkbenchRequestHandler,
    inject_html_metas,
    validate_host_header,
    validate_origin_header,
)
from .lease import (
    WorkbenchLease,
    default_workbench_ui_path,
    load_workbench_lease,
    query_workbench_status,
    stop_workbench_server,
    workbench_lease_path,
    write_lease_file,
)
from .service import WorkbenchService

__all__ = [
    "WorkbenchLease",
    "WorkbenchServer",
    "default_workbench_ui_path",
    "inject_html_metas",
    "load_workbench_lease",
    "query_workbench_status",
    "run_serve_server",
    "stop_workbench_server",
    "validate_host_header",
    "validate_origin_header",
]


class _BoundThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], owner: WorkbenchServer) -> None:
        self.owner = owner
        super().__init__(server_address, WorkbenchRequestHandler)


class WorkbenchServer:
    """Managed loopback HTTP server with idle/lifetime self-reclamation and lease file."""

    def __init__(
        self,
        service: WorkbenchService,
        *,
        ui_path: Path | None = None,
        port: int = 8765,
        idle_timeout_sec: int = 1800,
        max_lifetime_sec: int = 14400,
    ) -> None:
        port_val = int(port)
        idle_val = int(idle_timeout_sec)
        life_val = int(max_lifetime_sec)
        if not (0 <= port_val <= 65535):
            raise ValueError(f"port must be between 0 and 65535, got {port!r}")
        if idle_val < 0:
            raise ValueError(f"idle_timeout_sec must be >= 0, got {idle_timeout_sec!r}")
        if life_val < 0:
            raise ValueError(f"max_lifetime_sec must be >= 0, got {max_lifetime_sec!r}")
        self.service = service
        self.ui_path = (ui_path or default_workbench_ui_path()).expanduser().resolve(strict=False)
        self.requested_port = port_val
        self.idle_timeout_sec = idle_val
        self.max_lifetime_sec = life_val
        self.token = secrets.token_urlsafe(32)
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._started_mono = self._last_activity_mono = 0.0
        self._started_iso = self._last_activity_iso = ""
        self._httpd: _BoundThreadingHTTPServer | None = None
        self._bound_port: int | None = None
        self._owns_lease = False
        self._serve_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._closed = False

    @property
    def port(self) -> int:
        with self._state_lock:
            return self.requested_port if self._httpd is None else int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def health_payload(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "status": "ok",
                "stage": "stage_1",
                "pid": os.getpid(),
                "port": self.port,
                "started_at_iso": self._started_iso,
                "last_activity_iso": self._last_activity_iso,
                "remote_message_send": False,
            }

    def lease_snapshot(self) -> WorkbenchLease:
        with self._state_lock:
            return WorkbenchLease(
                pid=os.getpid(), url=self.url, port=self.port,
                started_at_iso=self._started_iso, last_activity_iso=self._last_activity_iso,
                idle_timeout_sec=self.idle_timeout_sec, max_lifetime_sec=self.max_lifetime_sec, ui_path=str(self.ui_path),
            )

    def startup_payload(self) -> dict[str, Any]:
        return {
            "url": self.url, "pid": os.getpid(), "port": self.port,
            "idle_timeout_sec": self.idle_timeout_sec, "max_lifetime_sec": self.max_lifetime_sec,
            "stage": "stage_1", "ui_path": str(self.ui_path),
        }

    def touch_activity(self) -> None:
        with self._state_lock:
            prev_iso = self._last_activity_iso
            self._last_activity_mono = time.monotonic()
            new_iso = now_iso()
            self._last_activity_iso = new_iso
            if new_iso != prev_iso and self._httpd is not None and not self._closed and self._owns_lease:
                existing = load_workbench_lease(self.service.paths)
                if existing is None or (existing.pid == os.getpid() and existing.port == self._bound_port):
                    try:
                        write_lease_file(workbench_lease_path(self.service.paths), self.lease_snapshot())
                    except OSError:
                        pass

    def start(self) -> None:
        with self._state_lock:
            if self._httpd is not None:
                return
            self._closed = False
            self._owns_lease = False
            self._bound_port = None
            self._stop_event.clear()
            now_mono, cur_iso = time.monotonic(), now_iso()
            self._started_mono = self._last_activity_mono = now_mono
            self._started_iso = self._last_activity_iso = cur_iso
            httpd = _BoundThreadingHTTPServer(("127.0.0.1", self.requested_port), self)
            try:
                self._httpd = httpd
                self._bound_port = int(httpd.server_address[1])
                write_lease_file(workbench_lease_path(self.service.paths), self.lease_snapshot())
                self._owns_lease = True
            except Exception:
                self._httpd = None
                self._bound_port = None
                self._owns_lease = False
                httpd.server_close()
                raise
            self._serve_thread = threading.Thread(target=httpd.serve_forever, name="workbench-http", daemon=True)
            self._serve_thread.start()
            self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="workbench-watchdog", daemon=True)
            self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        positive = [t for t in (self.idle_timeout_sec, self.max_lifetime_sec) if t > 0]
        interval = min(2.0, max(0.05, min(positive) / 4.0)) if positive else 2.0
        while not self._stop_event.wait(interval):
            now = time.monotonic()
            with self._state_lock:
                idle_hit = self.idle_timeout_sec > 0 and (now - self._last_activity_mono) >= self.idle_timeout_sec
                life_hit = self.max_lifetime_sec > 0 and (now - self._started_mono) >= self.max_lifetime_sec
            if idle_hit or life_hit:
                self._stop_event.set()
                break
        with self._state_lock:
            httpd = None if self._closed else self._httpd
        if httpd is not None:
            httpd.shutdown()

    def request_stop(self) -> None:
        self._stop_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._stop_event.wait(timeout)

    def stop(self) -> None:
        self._stop_event.set()
        with self._state_lock:
            self._closed = True
            httpd = self._httpd
            bound_port = self._bound_port
            owns_lease = self._owns_lease
            threads = (self._serve_thread, self._watchdog_thread)
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        for t in threads:
            if t is not None and t is not threading.current_thread():
                t.join(timeout=2.0)
        with self._state_lock:
            if owns_lease:
                existing = load_workbench_lease(self.service.paths)
                if existing is not None and existing.pid == os.getpid() and existing.port == bound_port:
                    workbench_lease_path(self.service.paths).unlink(missing_ok=True)
                self._owns_lease = False
            self._httpd = None
            self._bound_port = None


def run_serve_server(
    paths: HostEnvironmentPaths,
    *,
    seed_workspaces: list[str] | tuple[str, ...] = (),
    ui_path: Path | None = None,
    port: int = 8765,
    idle_timeout_sec: int = 1800,
    max_lifetime_sec: int = 14400,
    open_browser: bool = False,
    on_started: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    server = WorkbenchServer(
        WorkbenchService(paths, seed_workspaces=seed_workspaces),
        ui_path=ui_path, port=port, idle_timeout_sec=idle_timeout_sec, max_lifetime_sec=max_lifetime_sec,
    )
    installed: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        def _handle_signal(_signum: int, _frame: Any) -> None:
            server.request_stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            installed[sig] = signal.getsignal(sig)
            signal.signal(sig, _handle_signal)
    try:
        try:
            server.start()
        except OSError as exc:
            status = query_workbench_status(paths)
            err_payload = {
                "error": "workbench_bind_failed",
                "port": port,
                "message": str(exc),
                "existing_server": status if status.get("running") else None,
            }
            print(json.dumps(err_payload, ensure_ascii=False), file=sys.stderr, flush=True)
            return 2
        info = server.startup_payload()
        if on_started is not None:
            on_started(info)
        else:
            print(json.dumps(info, ensure_ascii=False), flush=True)
        if open_browser:
            try:
                webbrowser.open(server.url)
            except Exception:
                pass
        server.wait()
    finally:
        server.stop()
        for sig, prev in installed.items():
            signal.signal(sig, prev)
    return 0
