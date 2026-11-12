"""Host environment path resolution, CSRF redaction, held `flock` critical section, and atomic backup/restore."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import sqlite3
import stat
import struct
import sys
from typing import Iterator, Mapping, Sequence, Tuple

from ..ledger import CascadeId, CodexThreadId, HostKind, validate_session_id


class CsrfSecret:
    """In-memory-only wrapper for `x-codeium-csrf-token` from `hub_<port>.json`."""

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        cleaned = str(token or "").strip()
        if not cleaned:
            raise ValueError("CSRF token cannot be empty.")
        object.__setattr__(self, "_token", cleaned)

    def as_loopback_header(self) -> Mapping[str, str]:
        return {"x-codeium-csrf-token": self._token}

    def __repr__(self) -> str:
        return "CsrfSecret('[REDACTED_CSRF]')"

    def __str__(self) -> str:
        return "[REDACTED_CSRF]"

    def __format__(self, format_spec: str) -> str:
        return "[REDACTED_CSRF]"


@dataclass(frozen=True)
class HostEnvironmentPaths:
    """Runtime-resolved host storage roots with zero hardcoded machine usernames."""

    gemini_config_dir: Path
    agy_data_dir: Path
    codex_home_dir: Path
    sync_state_dir: Path
    agy_cli_data_dir: Path | None = None
    codex_app_server_socket: Path | None = None

    @classmethod
    def discover(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> HostEnvironmentPaths:
        source_env = env if env is not None else os.environ
        home = Path(source_env.get("HOME") or Path.home()).expanduser().resolve(strict=False)
        gemini_root = Path(source_env.get("GEMINI_HOME") or (home / ".gemini")).expanduser().resolve(strict=False)

        gemini_config_dir = Path(
            source_env.get("GEMINI_CONFIG_DIR") or (gemini_root / "config")
        ).expanduser().resolve(strict=False)
        agy_data_dir = Path(
            source_env.get("ANTIGRAVITY_DATA_DIR") or (gemini_root / "antigravity")
        ).expanduser().resolve(strict=False)
        codex_home_dir = Path(
            source_env.get("CODEX_HOME") or (home / ".codex")
        ).expanduser().resolve(strict=False)
        sync_state_dir = Path(
            source_env.get("CODEX_AGY_SYNC_STATE_DIR") or (gemini_root / "codex-agy-sync")
        ).expanduser().resolve(strict=False)

        raw_cli_dir = source_env.get("ANTIGRAVITY_CLI_DATA_DIR")
        if raw_cli_dir:
            agy_cli_data_dir: Path | None = Path(raw_cli_dir).expanduser().resolve(strict=False)
        elif (gemini_root / "antigravity-cli").is_dir():
            agy_cli_data_dir = (gemini_root / "antigravity-cli").resolve(strict=False)
        else:
            agy_cli_data_dir = None

        raw_codex_sock = (source_env.get("CODEX_APP_SERVER_SOCKET") or "").strip()
        codex_app_server_socket = Path(raw_codex_sock).expanduser() if raw_codex_sock else None

        return cls(
            gemini_config_dir=gemini_config_dir,
            agy_data_dir=agy_data_dir,
            codex_home_dir=codex_home_dir,
            sync_state_dir=sync_state_dir,
            agy_cli_data_dir=agy_cli_data_dir,
            codex_app_server_socket=codex_app_server_socket,
        )


def _atomic_write_text(target_path: Path, content: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, target_path)


def _atomic_write_bytes(target_path: Path, content: bytes) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    tmp_path.write_bytes(content)
    os.replace(tmp_path, target_path)


def _atomic_copy_file(src_path: Path, dst_path: Path) -> None:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dst_path.with_name(f".{dst_path.name}.{os.getpid()}.restore.tmp")
    shutil.copy2(src_path, tmp_path)
    os.replace(tmp_path, dst_path)


def _deterministic_uuid(namespace_label: str, key: str) -> str:
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-agy-sync:{namespace_label}:{key}"))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validated_child_path(base_dir: Path, *parts: str, label: str = "path") -> Path:
    resolved_base = base_dir.resolve(strict=False)
    raw_candidate = base_dir.joinpath(*parts)
    resolved_candidate = raw_candidate.resolve(strict=False)
    if resolved_candidate == resolved_base or not resolved_candidate.is_relative_to(resolved_base):
        raise ValueError(f"Invalid {label}: {parts!r} escapes {base_dir}")
    return raw_candidate


def _open_readonly_sqlite(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve(strict=False).as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only = ON;")
    return conn


class TransportMode(str, Enum):
    """Selected execution mode for reading or writing a host session."""

    AUTO = "auto"
    ONLINE_RPC = "online_rpc"
    OFFLINE_STORAGE = "offline_storage"


@dataclass(frozen=True)
class AgyDaemonEndpoint:
    """Live Antigravity `LanguageServerService` endpoint discovered from `hub_<port>.json`."""

    pid: int
    http_port: int
    https_port: int
    ls_version: str
    csrf_secret: CsrfSecret


@dataclass(frozen=True)
class CodexDaemonEndpoint:
    """Live `codex app-server` JSON-RPC v2 Unix socket endpoint."""

    socket_path: Path
    pid: int | None = None


@dataclass(frozen=True)
class HostLockSnapshot:
    """Point-in-time concurrency and liveness probe for a session on both hosts."""

    agy_daemon: AgyDaemonEndpoint | None
    agy_presence_locked: bool
    agy_wal_active: bool
    agy_turn_inflight: bool
    codex_daemon: CodexDaemonEndpoint | None
    codex_wal_active: bool
    codex_turn_inflight: bool

    def can_write_agy_offline(self) -> bool:
        return (
            (self.agy_daemon is None)
            and (not self.agy_presence_locked)
            and (not self.agy_wal_active)
            and (not self.agy_turn_inflight)
        )

    def can_write_codex_offline(self) -> bool:
        return (
            (self.codex_daemon is None)
            and (not self.codex_wal_active)
            and (not self.codex_turn_inflight)
        )


def _unlink_with_sqlite_sidecars(path: Path, keep_resolved: set[Path]) -> None:
    if path.resolve(strict=False) not in keep_resolved:
        path.unlink(missing_ok=True)
    if path.suffix in (".db", ".sqlite"):
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(f"{path.name}{suffix}")
            if sidecar.resolve(strict=False) not in keep_resolved:
                sidecar.unlink(missing_ok=True)


@dataclass(frozen=True)
class BackupManifest:
    """Self-describing atomic backup manifest supporting transactional rollback."""

    manifest_id: str
    created_at_iso: str
    target_host: HostKind
    target_session_id: str
    backup_dir: Path
    backed_up_files: Tuple[Path, ...]
    file_sha256_map: Mapping[str, str]
    restoration_command: str
    created_targets: Tuple[Path, ...] = ()
    restore_map: Tuple[Tuple[Path, Path], ...] = ()
    cleanup_globs: Tuple[Tuple[Path, str], ...] = ()

    def restore(self) -> None:
        backed_up_originals = {orig.resolve(strict=False) for _, orig in self.restore_map}
        for backup_copy, original_target in self.restore_map:
            if backup_copy.is_file():
                _atomic_copy_file(backup_copy, original_target)
            if original_target.suffix in (".db", ".sqlite"):
                for suffix in ("-wal", "-shm"):
                    sidecar = original_target.with_name(f"{original_target.name}{suffix}")
                    if sidecar.resolve(strict=False) not in backed_up_originals:
                        sidecar.unlink(missing_ok=True)

        for created in self.created_targets:
            _unlink_with_sqlite_sidecars(created, backed_up_originals)
            if ".system_generated" in created.parts:
                parent = created.parent
                for _ in range(3):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent

        for root_dir, pattern in self.cleanup_globs:
            if not root_dir.is_dir():
                continue
            resolved_root = root_dir.resolve(strict=False)
            for match in list(root_dir.rglob(pattern)):
                if match.is_file() and match.resolve(strict=False).is_relative_to(resolved_root):
                    _unlink_with_sqlite_sidecars(match, backed_up_originals)


def _open_nonblocking_ex_flock(lock_path: Path, *, create: bool) -> int:
    flags = (os.O_CREAT | os.O_RDWR) if create else os.O_RDWR
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except PermissionError:
        if create:
            raise BlockingIOError(f"Cannot create lock file: {lock_path}") from None
        try:
            fd = os.open(str(lock_path), os.O_RDONLY)
        except OSError as exc:
            raise BlockingIOError(f"Cannot open lock file: {lock_path}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        os.close(fd)
        raise BlockingIOError(f"Lock busy: {lock_path}") from exc
    return fd


def has_live_unix_socket(path: Path | None) -> bool:
    """Return `True` iff `path` exists and resolves (following symlinks) to a Unix domain socket."""
    if path is None:
        return False
    try:
        return path.exists() and stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def unix_socket_connect_path(sock_path: Path) -> str:
    """Select the shorter of `sock_path` and its symlink-resolved path to stay under macOS `sun_path` limits."""
    raw_str = str(sock_path)
    resolved_str = str(sock_path.resolve(strict=False))
    return resolved_str if len(os.fsencode(resolved_str)) < len(os.fsencode(raw_str)) else raw_str


def _can_tcp_connect_loopback(port: int) -> bool:
    if port <= 0:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


def _has_nonempty_wal(db_path: Path) -> bool:
    wal_path = Path(f"{db_path}-wal")
    try:
        return wal_path.is_file() and wal_path.stat().st_size > 0
    except OSError:
        return False


def _is_sqlite_shm_wal_locked(db_path: Path) -> bool:
    shm_path = Path(f"{db_path}-shm")
    if not shm_path.is_file():
        return False
    try:
        fd = os.open(str(shm_path), os.O_RDONLY)
    except OSError:
        return False
    try:
        if sys.platform == "darwin":
            req = struct.pack("qqihh", 120, 8, 0, fcntl.F_WRLCK, os.SEEK_SET)
            l_type = struct.unpack("qqihh", fcntl.fcntl(fd, fcntl.F_GETLK, req))[3]
        elif sys.platform.startswith("linux"):
            req = struct.pack("hhqqi0q", fcntl.F_WRLCK, os.SEEK_SET, 120, 8, 0)
            l_type = struct.unpack("hhqqi0q", fcntl.fcntl(fd, fcntl.F_GETLK, req))[0]
        else:
            return False
        return int(l_type) != fcntl.F_UNLCK
    except (OSError, struct.error):
        return False
    finally:
        os.close(fd)


class PresenceWalGuard:
    """Enforces read-only SQLite access, held `flock` critical sections, and `BackupManifest` creation."""

    def __init__(self, paths: HostEnvironmentPaths) -> None:
        self._paths = paths

    def _is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _discover_agy_daemon(self) -> AgyDaemonEndpoint | None:
        daemon_dir = self._paths.agy_data_dir / "daemon"
        if not daemon_dir.is_dir():
            return None
        candidates: list[AgyDaemonEndpoint] = []
        for hf in sorted(daemon_dir.glob("hub_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                raw = json.loads(hf.read_text(encoding="utf-8"))
                pid = int(raw.get("pid") or 0)
                tok = str(raw.get("csrfToken") or "").strip()
                if pid > 0 and tok and self._is_pid_alive(pid):
                    candidates.append(
                        AgyDaemonEndpoint(
                            pid=pid,
                            http_port=int(raw.get("httpPort") or raw.get("port") or 0),
                            https_port=int(raw.get("httpsPort") or 0),
                            ls_version=str(raw.get("lsVersion") or "2.30.0"),
                            csrf_secret=CsrfSecret(tok),
                        )
                    )
            except Exception:
                continue
        if len(candidates) > 1:
            for ep in candidates:
                if _can_tcp_connect_loopback(ep.http_port):
                    return ep
        return candidates[0] if candidates else None

    def _discover_codex_daemon(self) -> CodexDaemonEndpoint | None:
        live_pid: int | None = None
        daemon_json = self._paths.codex_home_dir / "daemon.json"
        if daemon_json.is_file():
            try:
                raw = json.loads(daemon_json.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    pid = int(raw.get("pid") or 0)
                    if pid > 0 and self._is_pid_alive(pid):
                        live_pid = pid
            except Exception:
                pass

        for candidate in (
            self._paths.codex_app_server_socket,
            self._paths.codex_home_dir / "app-server.sock",
            self._paths.codex_home_dir / "app-server-control" / "app-server-control.sock",
            self._paths.codex_home_dir / "ipc" / "ipc.sock",
            self._paths.codex_home_dir / "run" / "app-server.sock",
        ):
            if candidate is None or not has_live_unix_socket(candidate):
                continue
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(0.2)
            try:
                sock.connect(unix_socket_connect_path(candidate))
                return CodexDaemonEndpoint(socket_path=candidate, pid=live_pid)
            except OSError:
                continue
            finally:
                sock.close()

        return CodexDaemonEndpoint(socket_path=daemon_json, pid=live_pid) if live_pid is not None else None

    def _probe_flock_locked(self, lock_file: Path) -> bool:
        if not lock_file.exists():
            return False
        try:
            fd = os.open(str(lock_file), os.O_RDONLY)
        except PermissionError:
            return True
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def probe_locks(
        self,
        cascade_id: CascadeId | None,
        codex_thread_id: CodexThreadId | None,
        *,
        agy_turn_inflight: bool = False,
        codex_turn_inflight: bool = False,
        ignore_held_agy_presence: bool = False,
    ) -> HostLockSnapshot:
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")

        agy_daemon = self._discover_agy_daemon()
        codex_daemon = self._discover_codex_daemon()

        agy_presence_locked = False
        agy_dbs: list[Path] = [self._paths.agy_data_dir / "conversation_summaries.db"]
        conv_db: Path | None = None
        if cascade_id is not None:
            lock_path = _validated_child_path(
                self._paths.agy_data_dir, "presence", f"{cascade_id}.lock", label="cascade_id"
            )
            if not ignore_held_agy_presence:
                agy_presence_locked = self._probe_flock_locked(lock_path)
            conv_db = _validated_child_path(
                self._paths.agy_data_dir, "conversations", f"{cascade_id}.db", label="cascade_id"
            )
            agy_dbs.append(conv_db)
        agy_wal_active = (
            (agy_daemon is not None and any(_has_nonempty_wal(db) for db in agy_dbs))
            or (conv_db is not None and agy_presence_locked and _has_nonempty_wal(conv_db))
            or any(_is_sqlite_shm_wal_locked(db) for db in agy_dbs)
        )

        codex_state = self._paths.codex_home_dir / "state_5.sqlite"
        codex_wal_active = bool(
            (codex_daemon is not None and _has_nonempty_wal(codex_state))
            or _is_sqlite_shm_wal_locked(codex_state)
        )

        return HostLockSnapshot(
            agy_daemon=agy_daemon,
            agy_presence_locked=agy_presence_locked,
            agy_wal_active=agy_wal_active,
            agy_turn_inflight=agy_turn_inflight,
            codex_daemon=codex_daemon,
            codex_wal_active=codex_wal_active,
            codex_turn_inflight=codex_turn_inflight,
        )

    @contextlib.contextmanager
    def acquire_offline_write_locks(
        self,
        target_host: HostKind,
        cascade_id: CascadeId | None,
        codex_thread_id: CodexThreadId | None,
        *,
        agy_turn_inflight: bool = False,
        codex_turn_inflight: bool = False,
    ) -> Iterator[HostLockSnapshot]:
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if codex_thread_id is not None:
            validate_session_id(str(codex_thread_id), label="codex_thread_id")

        sync_lock_path = self._paths.sync_state_dir / "locks" / "sync.lock"
        sync_lock_path.parent.mkdir(parents=True, exist_ok=True)
        sync_fd = _open_nonblocking_ex_flock(sync_lock_path, create=True)
        presence_fd: int | None = None
        presence_lock_path: Path | None = None
        created_temp_presence = False
        try:
            if target_host == HostKind.ANTIGRAVITY and cascade_id is not None:
                presence_dir = self._paths.agy_data_dir / "presence"
                candidate_lock = _validated_child_path(
                    self._paths.agy_data_dir, "presence", f"{cascade_id}.lock", label="cascade_id"
                )
                if candidate_lock.exists():
                    try:
                        presence_fd = _open_nonblocking_ex_flock(candidate_lock, create=False)
                        presence_lock_path = candidate_lock
                    except FileNotFoundError:
                        pass
                if presence_fd is None and presence_dir.is_dir():
                    presence_fd = _open_nonblocking_ex_flock(candidate_lock, create=True)
                    presence_lock_path = candidate_lock
                    created_temp_presence = True

            locks = self.probe_locks(
                cascade_id,
                codex_thread_id,
                agy_turn_inflight=agy_turn_inflight,
                codex_turn_inflight=codex_turn_inflight,
                ignore_held_agy_presence=(presence_fd is not None),
            )
            can_write = (
                locks.can_write_agy_offline()
                if target_host == HostKind.ANTIGRAVITY
                else locks.can_write_codex_offline()
            )
            if not can_write:
                raise BlockingIOError(f"Offline write blocked after lock re-probe for {target_host.value}.")
            yield locks
        finally:
            if presence_fd is not None:
                try:
                    if created_temp_presence and presence_lock_path is not None:
                        presence_lock_path.unlink(missing_ok=True)
                    fcntl.flock(presence_fd, fcntl.LOCK_UN)
                finally:
                    os.close(presence_fd)
            try:
                fcntl.flock(sync_fd, fcntl.LOCK_UN)
            finally:
                os.close(sync_fd)

    def sqlite_readonly_uri(self, db_path: Path) -> str:
        return f"{db_path.resolve(strict=False).as_uri()}?mode=ro"

    def _resolve_unique_backup_dir(self, target_host: HostKind, target_session_id: str) -> tuple[str, Path]:
        ts_compact = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base_id = f"backup-{target_host.value}-{target_session_id}-{ts_compact}"
        manifest_id = base_id
        backup_dir = _validated_child_path(self._paths.sync_state_dir, "backups", manifest_id, label="target_session_id")
        seq = 1
        while (backup_dir / "manifest.json").exists():
            seq += 1
            manifest_id = f"{base_id}-{seq}"
            backup_dir = _validated_child_path(self._paths.sync_state_dir, "backups", manifest_id, label="target_session_id")
        backup_dir.mkdir(parents=True, exist_ok=True)
        return manifest_id, backup_dir

    def create_offline_backup(
        self,
        target_host: HostKind,
        target_session_id: str,
        target_files: Sequence[Path],
        *,
        expected_new_files: Sequence[Path] = (),
        cleanup_globs: Sequence[Tuple[Path, str]] = (),
    ) -> BackupManifest:
        validate_session_id(str(target_session_id), label="target_session_id")
        manifest_id, backup_dir = self._resolve_unique_backup_dir(target_host, target_session_id)
        backed_up: list[Path] = []
        restore_pairs: list[Tuple[Path, Path]] = []
        created_targets: list[Path] = []
        seen_created: set[Path] = set()
        sha_map: dict[str, str] = {}
        restore_cmds: list[str] = []
        target_set = {p.resolve(strict=False) for p in target_files if p.is_file()}

        for idx, src_path in enumerate(target_files):
            if not src_path.is_file():
                resolved_missing = src_path.resolve(strict=False)
                if resolved_missing not in seen_created:
                    seen_created.add(resolved_missing)
                    created_targets.append(src_path)
                    restore_cmds.append(f"rm -f {shlex.quote(str(src_path))}")
                continue
            dst_path = backup_dir / f"{idx:02d}_{src_path.name}"
            shutil.copy2(src_path, dst_path)
            sha_map[str(src_path)] = hashlib.sha256(dst_path.read_bytes()).hexdigest()
            backed_up.append(dst_path)
            restore_pairs.append((dst_path, src_path))
            restore_cmds.append(f"cp -p {shlex.quote(str(dst_path))} {shlex.quote(str(src_path))}")
            if src_path.suffix in (".db", ".sqlite"):
                for suffix in ("-wal", "-shm"):
                    sidecar = src_path.with_name(f"{src_path.name}{suffix}")
                    if sidecar.resolve(strict=False) not in target_set:
                        restore_cmds.append(f"rm -f {shlex.quote(str(sidecar))}")

        for new_path in expected_new_files:
            resolved_new = new_path.resolve(strict=False)
            if not new_path.is_file() and resolved_new not in seen_created and resolved_new not in target_set:
                seen_created.add(resolved_new)
                created_targets.append(new_path)
                restore_cmds.append(f"rm -f {shlex.quote(str(new_path))}")

        restoration_command = " && ".join(restore_cmds) if restore_cmds else ":"
        created_iso = _now_iso()
        manifest_payload = {
            "manifest_id": manifest_id, "created_at_iso": created_iso, "target_host": target_host.value,
            "target_session_id": target_session_id, "backup_dir": str(backup_dir),
            "backed_up_files": [str(p) for p in backed_up], "created_targets": [str(p) for p in created_targets],
            "file_sha256_map": sha_map, "restoration_command": restoration_command,
        }
        _atomic_write_text(backup_dir / "manifest.json", json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n")
        return BackupManifest(
            manifest_id=manifest_id, created_at_iso=created_iso, target_host=target_host,
            target_session_id=target_session_id, backup_dir=backup_dir, backed_up_files=tuple(backed_up),
            file_sha256_map=sha_map, restoration_command=restoration_command,
            created_targets=tuple(created_targets), restore_map=tuple(restore_pairs), cleanup_globs=tuple(cleanup_globs),
        )
