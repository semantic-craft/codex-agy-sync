//! Native `libc` PID/zombie checks, non-blocking `flock`, SQLite SHM `F_GETLK` WAL probes, and daemon discovery.

use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::net::{SocketAddr, TcpStream};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::FileTypeExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use cas_core::{
    AgyDaemonEndpoint, CodexDaemonEndpoint, CsrfSecret, HostKind, HostLockSnapshot,
    resolve_strict_false, validate_session_id,
};
use serde_json::Value;

use crate::backup::{BackupManifest, create_offline_backup_manifest};
use crate::error::HostError;
use crate::paths::{HostEnvironmentPaths, validated_child_path};

/// Check process liveness via `kill(pid, 0)` and zombie status without spawning `ps`.
#[must_use]
pub fn is_pid_alive(pid: u32) -> bool {
    let Ok(raw_pid) = libc::pid_t::try_from(pid) else {
        return false;
    };
    if raw_pid <= 0 {
        return false;
    }
    // SAFETY: `kill(pid, 0)` only queries signal deliverability; no memory is mutated.
    let rc = unsafe { libc::kill(raw_pid, 0) };
    if rc != 0 {
        let err = std::io::Error::last_os_error();
        if err.raw_os_error() != Some(libc::EPERM) {
            return false;
        }
    }
    !is_pid_zombie(raw_pid)
}

#[cfg(target_os = "macos")]
fn is_pid_zombie(pid: libc::pid_t) -> bool {
    const SZOMB: u32 = 5;
    // SAFETY: `proc_bsdinfo` is zero-initialized POD; size matches `PROC_PIDTBSDINFO`.
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let expected_sz = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    // SAFETY: valid pointer to stack `proc_bsdinfo` of exact size.
    let n = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            (&raw mut info).cast::<libc::c_void>(),
            expected_sz,
        )
    };
    n == expected_sz && info.pbi_status == SZOMB
}

#[cfg(target_os = "linux")]
fn is_pid_zombie(pid: libc::pid_t) -> bool {
    let Ok(stat_txt) = fs::read_to_string(format!("/proc/{pid}/stat")) else {
        return false;
    };
    stat_txt
        .rsplit_once(')')
        .and_then(|(_, rest)| rest.split_whitespace().next())
        .is_some_and(|st| st == "Z")
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn is_pid_zombie(_pid: libc::pid_t) -> bool {
    false
}

/// RAII exclusive `flock` descriptor that unlocks and closes on drop.
pub struct FlockGuard {
    fd: OwnedFd,
    unlink_on_drop: Option<PathBuf>,
}

impl Drop for FlockGuard {
    fn drop(&mut self) {
        if let Some(ref p) = self.unlink_on_drop {
            let _ = fs::remove_file(p);
        }
        // SAFETY: `self.fd` is a valid open file descriptor owned by this guard.
        unsafe {
            libc::flock(self.fd.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

/// Acquire a non-blocking `LOCK_EX | LOCK_NB` `flock` on `lock_path`.
///
/// # Errors
/// Returns [`std::io::Error`] (`WouldBlock` or OS error) if lock acquisition fails.
pub fn open_nonblocking_ex_flock(
    lock_path: &Path,
    create: bool,
) -> Result<FlockGuard, std::io::Error> {
    let c_path = CString::new(lock_path.as_os_str().as_bytes())
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "NUL byte in path"))?;
    let raw_fd = if create {
        // SAFETY: `c_path` is NUL-terminated.
        unsafe { libc::open(c_path.as_ptr(), libc::O_CREAT | libc::O_RDWR, 0o600) }
    } else {
        // SAFETY: `c_path` is NUL-terminated.
        let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDWR, 0) };
        if fd < 0 {
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::EACCES) || err.raw_os_error() == Some(libc::EPERM) {
                // SAFETY: fallback read-only open if file is read-only.
                unsafe { libc::open(c_path.as_ptr(), libc::O_RDONLY, 0) }
            } else {
                return Err(err);
            }
        } else {
            fd
        }
    };
    if raw_fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: `raw_fd` was just opened by us and is valid.
    let owned = unsafe { OwnedFd::from_raw_fd(raw_fd) };
    // SAFETY: `owned` is valid.
    let rc = unsafe { libc::flock(owned.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc != 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::WouldBlock,
            format!("Lock busy: {}", lock_path.display()),
        ));
    }
    Ok(FlockGuard {
        fd: owned,
        unlink_on_drop: None,
    })
}

/// Acquire an exclusive non-blocking `handoff.lock` under `<sync_state_dir>/handoffs`.
///
/// # Errors
/// Returns [`HostError::SyncBusyLock`] if another sync holds the lock.
pub fn open_handoff_lock(
    paths: &HostEnvironmentPaths,
    workspace_path: &Path,
) -> Result<(PathBuf, FlockGuard), HostError> {
    let state = paths.sync_state_dir.join("handoffs");
    fs::create_dir_all(&state)?;
    let lock_path = state.join("handoff.lock");
    match open_nonblocking_ex_flock(&lock_path, true) {
        Ok(guard) => Ok((state, guard)),
        Err(_) => Err(HostError::SyncBusyLock {
            message: "Another sync operation is currently running; wait and retry.".to_owned(),
            host: Some("handoff".to_owned()),
            remediation: vec![format!(
                "Wait for the active sync to finish, then retry: codex-agy-sync sync -w \"{}\"",
                workspace_path.display()
            )],
        }),
    }
}

/// Return `true` iff `path` exists and resolves to a Unix domain socket.
#[must_use]
pub fn has_live_unix_socket(path: Option<&Path>) -> bool {
    let Some(p) = path else {
        return false;
    };
    fs::metadata(p).is_ok_and(|md| md.file_type().is_socket())
}

/// Select the shorter of `sock_path` and its symlink-resolved path to stay under macOS `sun_path` limits.
#[must_use]
pub fn unix_socket_connect_path(sock_path: &Path) -> PathBuf {
    let resolved = resolve_strict_false(sock_path);
    if resolved.as_os_str().as_bytes().len() < sock_path.as_os_str().as_bytes().len() {
        resolved
    } else {
        sock_path.to_path_buf()
    }
}

fn can_tcp_connect_loopback(port: u16) -> bool {
    if port == 0 {
        return false;
    }
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(100)).is_ok()
}

fn has_nonempty_wal(db_path: &Path) -> bool {
    let Some(fname) = db_path.file_name().and_then(|n| n.to_str()) else {
        return false;
    };
    let wal_path = db_path.with_file_name(format!("{fname}-wal"));
    fs::metadata(&wal_path).is_ok_and(|md| md.is_file() && md.len() > 0)
}

/// Probe SQLite `-shm` byte-range lock via `fcntl(F_GETLK)` matching SQLite's `WAL_READ_LOCK(0)` offset 120..128.
#[must_use]
pub fn is_sqlite_shm_wal_locked(db_path: &Path) -> bool {
    let Some(fname) = db_path.file_name().and_then(|n| n.to_str()) else {
        return false;
    };
    let shm_path = db_path.with_file_name(format!("{fname}-shm"));
    if !shm_path.is_file() {
        return false;
    }
    let Ok(file) = File::open(&shm_path) else {
        return false;
    };
    let mut fl = libc::flock {
        l_start: 120,
        l_len: 8,
        l_pid: 0,
        l_type: libc::F_WRLCK as libc::c_short,
        l_whence: libc::SEEK_SET as libc::c_short,
    };
    // SAFETY: `file.as_raw_fd()` is valid and `fl` is a properly initialized `libc::flock`.
    let rc = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETLK, &raw mut fl) };
    rc == 0 && i64::from(fl.l_type) != i64::from(libc::F_UNLCK)
}

/// RAII critical-section guard holding `sync.lock` and optional Antigravity `presence/<id>.lock`.
pub struct OfflineWriteLockGuard {
    _presence_guard: Option<FlockGuard>,
    _sync_guard: FlockGuard,
    pub snapshot: HostLockSnapshot,
}

/// Enforces read-only SQLite access, held `flock` critical sections, and [`BackupManifest`] creation.
#[derive(Debug, Clone)]
pub struct PresenceWalGuard {
    paths: HostEnvironmentPaths,
}

impl PresenceWalGuard {
    #[must_use]
    pub fn new(paths: HostEnvironmentPaths) -> Self {
        Self { paths }
    }

    #[must_use]
    pub fn paths(&self) -> &HostEnvironmentPaths {
        &self.paths
    }

    #[must_use]
    pub fn discover_agy_daemon(&self) -> Option<AgyDaemonEndpoint> {
        let daemon_dir = self.paths.agy_data_dir.join("daemon");
        if !daemon_dir.is_dir() {
            return None;
        }
        let Ok(entries) = fs::read_dir(&daemon_dir) else {
            return None;
        };
        let mut hub_files: Vec<(SystemTime, PathBuf)> = entries
            .flatten()
            .filter_map(|e| {
                let p = e.path();
                let name = p.file_name()?.to_str()?;
                if name.starts_with("hub_") && name.ends_with(".json") {
                    let mtime = fs::metadata(&p)
                        .ok()?
                        .modified()
                        .unwrap_or(SystemTime::UNIX_EPOCH);
                    Some((mtime, p))
                } else {
                    None
                }
            })
            .collect();
        hub_files.sort_by_key(|a| std::cmp::Reverse(a.0));

        let mut candidates = Vec::new();
        for (_, hf) in hub_files {
            let Ok(text) = fs::read_to_string(&hf) else {
                continue;
            };
            let Ok(raw) = serde_json::from_str::<Value>(&text) else {
                continue;
            };
            let pid = raw.get("pid").and_then(Value::as_u64).unwrap_or(0) as u32;
            let tok = raw
                .get("csrfToken")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if pid == 0 || tok.is_empty() || !is_pid_alive(pid) {
                continue;
            }
            let Ok(csrf_secret) = CsrfSecret::try_new(tok) else {
                continue;
            };
            let http_port = raw
                .get("httpPort")
                .or_else(|| raw.get("port"))
                .and_then(Value::as_u64)
                .unwrap_or(0) as u16;
            let https_port = raw.get("httpsPort").and_then(Value::as_u64).unwrap_or(0) as u16;
            let ls_version = raw
                .get("lsVersion")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("2.30.0")
                .to_owned();
            candidates.push(AgyDaemonEndpoint {
                pid,
                http_port,
                https_port,
                ls_version,
                csrf_secret,
            });
        }
        if candidates.len() > 1 {
            if let Some(pos) = candidates
                .iter()
                .position(|ep| can_tcp_connect_loopback(ep.http_port))
            {
                return Some(candidates.swap_remove(pos));
            }
        }
        candidates.into_iter().next()
    }

    #[must_use]
    pub fn discover_codex_daemon(&self) -> Option<CodexDaemonEndpoint> {
        let mut live_pid: Option<u32> = None;
        let daemon_json = self.paths.codex_home_dir.join("daemon.json");
        if daemon_json.is_file()
            && let Ok(text) = fs::read_to_string(&daemon_json)
            && let Ok(Value::Object(raw)) = serde_json::from_str::<Value>(&text)
            && let Some(pid) = raw.get("pid").and_then(Value::as_u64)
            && pid > 0
            && is_pid_alive(pid as u32)
        {
            live_pid = Some(pid as u32);
        }

        let candidates = [
            self.paths.codex_app_server_socket.clone(),
            Some(self.paths.codex_home_dir.join("app-server.sock")),
            Some(
                self.paths
                    .codex_home_dir
                    .join("app-server-control")
                    .join("app-server-control.sock"),
            ),
            Some(self.paths.codex_home_dir.join("ipc").join("ipc.sock")),
            Some(
                self.paths
                    .codex_home_dir
                    .join("run")
                    .join("app-server.sock"),
            ),
        ];
        for cand in candidates.into_iter().flatten() {
            if !has_live_unix_socket(Some(&cand)) {
                continue;
            }
            let conn_path = unix_socket_connect_path(&cand);
            if let Ok(sock) = UnixStream::connect(&conn_path) {
                let _ = sock.set_read_timeout(Some(Duration::from_millis(200)));
                drop(sock);
                return Some(CodexDaemonEndpoint {
                    socket_path: cand,
                    pid: live_pid,
                });
            }
        }
        live_pid.map(|pid| CodexDaemonEndpoint {
            socket_path: daemon_json,
            pid: Some(pid),
        })
    }

    #[must_use]
    pub fn probe_flock_locked(&self, lock_file: &Path) -> bool {
        if !lock_file.exists() {
            return false;
        }
        let Ok(file) = OpenOptions::new().read(true).open(lock_file) else {
            return matches!(
                fs::OpenOptions::new()
                    .read(true)
                    .open(lock_file)
                    .err()
                    .map(|e| e.kind()),
                Some(std::io::ErrorKind::PermissionDenied)
            );
        };
        let fd = file.as_raw_fd();
        // SAFETY: `fd` is open and owned by `file`.
        let rc = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
        if rc != 0 {
            return true;
        }
        // SAFETY: we just acquired `LOCK_EX`; release before closing.
        unsafe {
            libc::flock(fd, libc::LOCK_UN);
        }
        false
    }

    /// Probe daemons, presence flocks, and SQLite WAL locks for both hosts.
    ///
    /// # Errors
    /// Returns [`HostError`] on invalid session IDs or escaping paths.
    pub fn probe_locks(
        &self,
        cascade_id: Option<&cas_core::CascadeId>,
        codex_thread_id: Option<&cas_core::CodexThreadId>,
        agy_turn_inflight: bool,
        codex_turn_inflight: bool,
        ignore_held_agy_presence: bool,
    ) -> Result<HostLockSnapshot, HostError> {
        if let Some(cid) = cascade_id {
            validate_session_id(cid.as_str(), "cascade_id")?;
        }
        if let Some(tid) = codex_thread_id {
            validate_session_id(tid.as_str(), "codex_thread_id")?;
        }

        let agy_daemon = self.discover_agy_daemon();
        let codex_daemon = self.discover_codex_daemon();

        let mut agy_presence_locked = false;
        let mut agy_dbs = vec![self.paths.agy_data_dir.join("conversation_summaries.db")];
        let mut conv_db: Option<PathBuf> = None;

        if let Some(cid) = cascade_id {
            let lock_path = validated_child_path(
                &self.paths.agy_data_dir,
                &["presence", &format!("{}.lock", cid.as_str())],
                "cascade_id",
            )?;
            if !ignore_held_agy_presence {
                agy_presence_locked = self.probe_flock_locked(&lock_path);
            }
            let cdb = validated_child_path(
                &self.paths.agy_data_dir,
                &["conversations", &format!("{}.db", cid.as_str())],
                "cascade_id",
            )?;
            agy_dbs.push(cdb.clone());
            conv_db = Some(cdb);
        }

        let agy_wal_active = (agy_daemon.is_some()
            && agy_dbs.iter().any(|db| has_nonempty_wal(db)))
            || (conv_db
                .as_deref()
                .is_some_and(|cdb| agy_presence_locked && has_nonempty_wal(cdb)))
            || agy_dbs.iter().any(|db| is_sqlite_shm_wal_locked(db));

        let codex_state = self.paths.codex_home_dir.join("state_5.sqlite");
        let codex_wal_active = (codex_daemon.is_some() && has_nonempty_wal(&codex_state))
            || is_sqlite_shm_wal_locked(&codex_state);

        Ok(HostLockSnapshot {
            agy_daemon,
            agy_presence_locked,
            agy_wal_active,
            agy_turn_inflight,
            codex_daemon,
            codex_wal_active,
            codex_turn_inflight,
        })
    }

    /// Acquire `sync.lock` + optional Antigravity presence flock and re-verify offline safety.
    ///
    /// # Errors
    /// Returns [`std::io::Error`] (`WouldBlock`) or [`HostError`] if locks are busy.
    pub fn acquire_offline_write_locks(
        &self,
        target_host: HostKind,
        cascade_id: Option<&cas_core::CascadeId>,
        codex_thread_id: Option<&cas_core::CodexThreadId>,
        agy_turn_inflight: bool,
        codex_turn_inflight: bool,
    ) -> Result<OfflineWriteLockGuard, HostError> {
        if let Some(cid) = cascade_id {
            validate_session_id(cid.as_str(), "cascade_id")?;
        }
        if let Some(tid) = codex_thread_id {
            validate_session_id(tid.as_str(), "codex_thread_id")?;
        }

        let sync_lock_path = self.paths.sync_state_dir.join("locks").join("sync.lock");
        if let Some(parent) = sync_lock_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let sync_guard = open_nonblocking_ex_flock(&sync_lock_path, true)?;

        let mut presence_guard: Option<FlockGuard> = None;
        if target_host == HostKind::Antigravity
            && let Some(cid) = cascade_id
        {
            let presence_dir = self.paths.agy_data_dir.join("presence");
            let candidate_lock = validated_child_path(
                &self.paths.agy_data_dir,
                &["presence", &format!("{}.lock", cid.as_str())],
                "cascade_id",
            )?;
            if candidate_lock.exists() {
                match open_nonblocking_ex_flock(&candidate_lock, false) {
                    Ok(g) => presence_guard = Some(g),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                    Err(e) => return Err(HostError::Io(e)),
                }
            }
            if presence_guard.is_none() && presence_dir.is_dir() {
                let mut g = open_nonblocking_ex_flock(&candidate_lock, true)?;
                g.unlink_on_drop = Some(candidate_lock);
                presence_guard = Some(g);
            }
        }

        let locks = self.probe_locks(
            cascade_id,
            codex_thread_id,
            agy_turn_inflight,
            codex_turn_inflight,
            presence_guard.is_some(),
        )?;
        let can_write = if target_host == HostKind::Antigravity {
            locks.can_write_agy_offline()
        } else {
            locks.can_write_codex_offline()
        };
        if !can_write {
            return Err(HostError::Io(std::io::Error::new(
                std::io::ErrorKind::WouldBlock,
                format!(
                    "Offline write blocked after lock re-probe for {}.",
                    target_host.as_str()
                ),
            )));
        }

        Ok(OfflineWriteLockGuard {
            _presence_guard: presence_guard,
            _sync_guard: sync_guard,
            snapshot: locks,
        })
    }

    /// Create an atomic backup manifest under `<sync_state_dir>/backups/`.
    ///
    /// # Errors
    /// Propagates filesystem or validation errors.
    pub fn create_offline_backup(
        &self,
        target_host: HostKind,
        target_session_id: &str,
        target_files: &[PathBuf],
        expected_new_files: &[PathBuf],
        cleanup_globs: &[(PathBuf, String)],
    ) -> Result<BackupManifest, HostError> {
        create_offline_backup_manifest(
            &self.paths.sync_state_dir,
            target_host,
            target_session_id,
            target_files,
            expected_new_files,
            cleanup_globs,
        )
    }
}
