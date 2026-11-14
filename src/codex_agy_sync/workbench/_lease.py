"""Lease file persistence and loopback process status/stop management."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any
import urllib.request

from ..bridge.paths_and_locks import HostEnvironmentPaths, _atomic_write_text


def default_workbench_ui_path() -> Path:
    base = Path(__file__).resolve().parent
    candidates = (base / "ui" / "index.html", base / "workbench.html")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def workbench_lease_path(paths: HostEnvironmentPaths) -> Path:
    return paths.sync_state_dir / "workbench" / "server.json"


@dataclass(frozen=True)
class WorkbenchLease:
    pid: int
    url: str
    port: int
    started_at_iso: str
    last_activity_iso: str
    idle_timeout_sec: int
    max_lifetime_sec: int
    ui_path: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WorkbenchLease:
        raw_pid = payload["pid"]
        raw_port = payload["port"]
        raw_url = payload["url"]
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
            raise ValueError(f"Invalid lease pid: {raw_pid!r}")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int) or not (0 <= raw_port <= 65535):
            raise ValueError(f"Invalid lease port: {raw_port!r}")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise ValueError(f"Invalid lease url: {raw_url!r}")
        idle_raw = payload.get("idle_timeout_sec")
        life_raw = payload.get("max_lifetime_sec")
        idle_sec = 0 if idle_raw is None else int(idle_raw) if not isinstance(idle_raw, bool) else 0
        life_sec = 0 if life_raw is None else int(life_raw) if not isinstance(life_raw, bool) else 0
        return cls(
            pid=raw_pid,
            url=raw_url,
            port=raw_port,
            started_at_iso=str(payload.get("started_at_iso") or ""),
            last_activity_iso=str(payload.get("last_activity_iso") or ""),
            idle_timeout_sec=idle_sec,
            max_lifetime_sec=life_sec,
            ui_path=str(payload.get("ui_path") or ""),
        )


def load_workbench_lease(paths: HostEnvironmentPaths) -> WorkbenchLease | None:
    path = workbench_lease_path(paths)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WorkbenchLease.from_dict(data) if isinstance(data, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_lease_file(path: Path, lease: WorkbenchLease) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    _atomic_write_text(path, json.dumps(lease.to_dict(), ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _loopback_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _probe_health(port: int, timeout_sec: float = 1.5) -> dict[str, Any] | None:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health", method="GET")
    try:
        with _loopback_opener().open(req, timeout=timeout_sec) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        proc = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if proc.stdout.strip().startswith("Z"):
            return False
    except (subprocess.SubprocessError, OSError):
        pass
    return True


def query_workbench_status(paths: HostEnvironmentPaths) -> dict[str, Any]:
    lease_path = workbench_lease_path(paths)
    lease = load_workbench_lease(paths)
    if lease is None:
        if lease_path.is_file():
            return {"running": False, "reason": "malformed_lease"}
        return {"running": False, "reason": "no_lease"}
    alive = _pid_is_alive(lease.pid)
    health = _probe_health(lease.port)
    if not (alive and health is not None and health.get("pid") == lease.pid):
        return {"running": False, "stale_lease": True, **lease.to_dict()}
    return {"running": True, **lease.to_dict(), "health": health}


def stop_workbench_server(paths: HostEnvironmentPaths, *, timeout_sec: float = 3.0) -> dict[str, Any]:
    lease_path = workbench_lease_path(paths)
    lease = load_workbench_lease(paths)
    if lease is None:
        if lease_path.is_file():
            lease_path.unlink(missing_ok=True)
            return {"stopped": False, "running": False, "stale_cleaned": True, "reason": "malformed_lease"}
        return {"stopped": False, "running": False, "reason": "no_lease"}
    alive = _pid_is_alive(lease.pid)
    health = _probe_health(lease.port)
    if not alive or health is None or health.get("pid") != lease.pid:
        lease_path.unlink(missing_ok=True)
        return {"stopped": False, "running": False, "stale_cleaned": True, "pid": lease.pid}
    try:
        os.kill(lease.pid, signal.SIGTERM)
    except ProcessLookupError:
        lease_path.unlink(missing_ok=True)
        return {"stopped": True, "pid": lease.pid, "port": lease.port}
    except PermissionError:
        return {"stopped": False, "running": True, "reason": "permission_denied", "pid": lease.pid, "port": lease.port}
    deadline = time.monotonic() + max(0.1, timeout_sec)
    while time.monotonic() < deadline:
        if not _pid_is_alive(lease.pid):
            lease_path.unlink(missing_ok=True)
            return {"stopped": True, "pid": lease.pid, "port": lease.port}
        time.sleep(0.05)
    if not _pid_is_alive(lease.pid):
        lease_path.unlink(missing_ok=True)
        return {"stopped": True, "pid": lease.pid, "port": lease.port}
    return {"stopped": False, "running": True, "reason": "stop_timeout", "pid": lease.pid, "port": lease.port}
