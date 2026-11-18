//! Runtime host path resolution, atomic file writes, deterministic UUIDv5, and safe child paths.

use std::collections::{BTreeMap, HashSet};
use std::env;
use std::fs;
use std::path::{Component, Path, PathBuf};

use cas_core::{lexical_abs_path, parse_workspace_identity, resolve_strict_false};
use chrono::{DateTime, NaiveDateTime, SecondsFormat, Utc};
use uuid::Uuid;

use crate::error::HostError;

/// Runtime-resolved host storage roots with zero hardcoded machine usernames.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostEnvironmentPaths {
    pub gemini_config_dir: PathBuf,
    pub agy_data_dir: PathBuf,
    pub codex_home_dir: PathBuf,
    pub sync_state_dir: PathBuf,
    pub agy_cli_data_dir: Option<PathBuf>,
    pub codex_app_server_socket: Option<PathBuf>,
}

fn expand_user_path(raw: &str, home: &Path) -> PathBuf {
    let trimmed = raw.trim();
    if trimmed == "~" {
        return home.to_path_buf();
    }
    if let Some(rest) = trimmed.strip_prefix("~/") {
        return home.join(rest);
    }
    PathBuf::from(trimmed)
}

impl HostEnvironmentPaths {
    /// Discover host paths from `env` (or `std::env::vars()` when `None`).
    #[must_use]
    pub fn discover(env_override: Option<&BTreeMap<String, String>>) -> Self {
        let lookup = |key: &str| -> Option<String> {
            match env_override {
                Some(m) => m.get(key).cloned(),
                None => env::var(key).ok(),
            }
        };
        let home = lookup("HOME")
            .filter(|s| !s.trim().is_empty())
            .map(|s| resolve_strict_false(&PathBuf::from(s)))
            .unwrap_or_else(|| resolve_strict_false(&PathBuf::from(".")));

        let gemini_root = lookup("GEMINI_HOME")
            .filter(|s| !s.trim().is_empty())
            .map(|s| resolve_strict_false(&expand_user_path(&s, &home)))
            .unwrap_or_else(|| resolve_strict_false(&home.join(".gemini")));

        let gemini_config_dir = lookup("GEMINI_CONFIG_DIR")
            .filter(|s| !s.trim().is_empty())
            .map(|s| resolve_strict_false(&expand_user_path(&s, &home)))
            .unwrap_or_else(|| resolve_strict_false(&gemini_root.join("config")));

        let agy_data_dir = lookup("ANTIGRAVITY_DATA_DIR")
            .filter(|s| !s.trim().is_empty())
            .map(|s| resolve_strict_false(&expand_user_path(&s, &home)))
            .unwrap_or_else(|| resolve_strict_false(&gemini_root.join("antigravity")));

        let codex_home_dir = lookup("CODEX_HOME")
            .filter(|s| !s.trim().is_empty())
            .map(|s| resolve_strict_false(&expand_user_path(&s, &home)))
            .unwrap_or_else(|| resolve_strict_false(&home.join(".codex")));

        let sync_state_dir = lookup("CODEX_AGY_SYNC_STATE_DIR")
            .filter(|s| !s.trim().is_empty())
            .map(|s| resolve_strict_false(&expand_user_path(&s, &home)))
            .unwrap_or_else(|| resolve_strict_false(&gemini_root.join("codex-agy-sync")));

        let agy_cli_data_dir = match lookup("ANTIGRAVITY_CLI_DATA_DIR") {
            Some(raw) if !raw.trim().is_empty() => {
                Some(resolve_strict_false(&expand_user_path(&raw, &home)))
            }
            _ => {
                let fallback = gemini_root.join("antigravity-cli");
                if fallback.is_dir() {
                    Some(resolve_strict_false(&fallback))
                } else {
                    None
                }
            }
        };

        let codex_app_server_socket = lookup("CODEX_APP_SERVER_SOCKET")
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty())
            .map(|s| expand_user_path(&s, &home));

        Self {
            gemini_config_dir,
            agy_data_dir,
            codex_home_dir,
            sync_state_dir,
            agy_cli_data_dir,
            codex_app_server_socket,
        }
    }
}

/// Write UTF-8 text atomically via same-directory temporary file + `fs::rename`.
///
/// # Errors
/// Returns [`HostError::Io`] on filesystem failure.
pub fn atomic_write_text(target_path: &Path, content: &str) -> Result<(), HostError> {
    atomic_write_bytes(target_path, content.as_bytes())
}

/// Write bytes atomically via same-directory temporary file + `fs::rename`.
///
/// # Errors
/// Returns [`HostError::Io`] on filesystem failure.
pub fn atomic_write_bytes(target_path: &Path, content: &[u8]) -> Result<(), HostError> {
    if let Some(parent) = target_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let name = target_path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("file");
    let tmp_path = target_path.with_file_name(format!(".{name}.{}.tmp", std::process::id()));
    fs::write(&tmp_path, content)?;
    fs::rename(&tmp_path, target_path)?;
    Ok(())
}

/// Copy `src_path` onto `dst_path` atomically, preserving file permissions.
///
/// # Errors
/// Returns [`HostError::Io`] on filesystem failure.
pub fn atomic_copy_file(src_path: &Path, dst_path: &Path) -> Result<(), HostError> {
    if let Some(parent) = dst_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let name = dst_path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("file");
    let tmp_path = dst_path.with_file_name(format!(".{name}.{}.restore.tmp", std::process::id()));
    fs::copy(src_path, &tmp_path)?;
    fs::rename(&tmp_path, dst_path)?;
    Ok(())
}

/// Compute deterministic UUIDv5 matching Python `uuid.uuid5(uuid.NAMESPACE_URL, f"codex-agy-sync:{namespace_label}:{key}")`.
#[must_use]
pub fn deterministic_uuid(namespace_label: &str, key: &str) -> String {
    let payload = format!("codex-agy-sync:{namespace_label}:{key}");
    Uuid::new_v5(&Uuid::NAMESPACE_URL, payload.as_bytes()).to_string()
}

/// Current UTC timestamp formatted as `YYYY-MM-DDTHH:MM:SSZ`.
#[must_use]
pub fn now_iso() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

/// Format an epoch second timestamp as `YYYY-MM-DDTHH:MM:SSZ`.
#[must_use]
pub fn format_epoch_utc_iso(epoch_sec: f64) -> String {
    if epoch_sec <= 0.0 || !epoch_sec.is_finite() {
        return String::new();
    }
    let whole = epoch_sec.floor() as i64;
    DateTime::<Utc>::from_timestamp(whole, 0).map_or_else(String::new, |dt| {
        dt.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    })
}

/// Format an epoch second timestamp as ISO-8601 UTC preserving sub-second precision when present.
#[must_use]
pub fn format_epoch_precise_iso(epoch_sec: f64) -> String {
    let whole = epoch_sec.trunc() as i64;
    let nanos = ((epoch_sec - (whole as f64)).clamp(0.0, 0.999_999_999) * 1_000_000_000.0) as u32;
    DateTime::<Utc>::from_timestamp(whole, nanos).map_or_else(now_iso, |dt| {
        if nanos == 0 {
            dt.to_rfc3339_opts(SecondsFormat::Secs, true)
        } else {
            dt.to_rfc3339_opts(SecondsFormat::Micros, true)
        }
    })
}

/// Parse numeric ms/sec epoch or ISO-8601 string into `(epoch_sec, utc_iso)`.
#[must_use]
pub fn parse_ts_epoch_iso(
    raw: Option<&str>,
    raw_num: Option<f64>,
    fallback_mtime: f64,
) -> (f64, String) {
    let mut epoch = 0.0f64;
    if let Some(n) = raw_num
        && n > 0.0
        && n.is_finite()
    {
        epoch = if n > 1e11 { n / 1000.0 } else { n };
    } else if let Some(s) = raw.map(str::trim).filter(|s| !s.is_empty()) {
        if let Ok(n) = s.parse::<f64>() {
            if n > 0.0 && n.is_finite() {
                epoch = if n > 1e11 { n / 1000.0 } else { n };
            }
        } else {
            let norm = if let Some(stripped) = s.strip_suffix('Z') {
                format!("{stripped}+00:00")
            } else {
                s.to_owned()
            };
            if let Ok(dt) = DateTime::parse_from_rfc3339(&norm) {
                epoch = dt.timestamp() as f64 + (f64::from(dt.timestamp_subsec_nanos()) / 1e9);
            } else if let Ok(ndt) = NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S") {
                epoch = ndt.and_utc().timestamp() as f64;
            } else if let Ok(ndt) = NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f") {
                epoch = ndt.and_utc().timestamp() as f64
                    + (f64::from(ndt.and_utc().timestamp_subsec_nanos()) / 1e9);
            }
        }
    }
    if epoch <= 0.0 && fallback_mtime > 0.0 {
        epoch = fallback_mtime;
    }
    let iso = format_epoch_utc_iso(epoch);
    (epoch, iso)
}

/// Validate and join child `parts` inside `base_dir`, blocking `..` or symlink escape.
///
/// # Errors
/// Returns [`HostError::InvalidArgument`] if the resulting path escapes `base_dir`.
pub fn validated_child_path(
    base_dir: &Path,
    parts: &[&str],
    label: &str,
) -> Result<PathBuf, HostError> {
    for part in parts {
        if part.is_empty()
            || *part == "."
            || *part == ".."
            || part.chars().any(|c| (c as u32) < 32 || c as u32 == 127)
        {
            return Err(HostError::InvalidArgument(format!(
                "Invalid {label}: {parts:?} escapes {}",
                base_dir.display()
            )));
        }
    }
    let resolved_base = resolve_strict_false(base_dir);
    let mut raw_candidate = base_dir.to_path_buf();
    for part in parts {
        raw_candidate.push(part);
    }
    let resolved_candidate = resolve_strict_false(&raw_candidate);
    if resolved_candidate == resolved_base || !resolved_candidate.starts_with(&resolved_base) {
        return Err(HostError::InvalidArgument(format!(
            "Invalid {label}: {parts:?} escapes {}",
            base_dir.display()
        )));
    }
    Ok(raw_candidate)
}

/// Expand existing files plus any SQLite `-wal` and `-shm` sidecar files.
#[must_use]
pub fn expand_existing_with_sidecars(candidates: &[PathBuf]) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for p in candidates {
        if p.is_file() {
            out.push(p.clone());
            if matches!(
                p.extension().and_then(|e| e.to_str()),
                Some("db" | "sqlite")
            ) && let Some(fname) = p.file_name().and_then(|n| n.to_str())
            {
                for sfx in ["-wal", "-shm"] {
                    let sc = p.with_file_name(format!("{fname}{sfx}"));
                    if sc.is_file() {
                        out.push(sc);
                    }
                }
            }
        }
    }
    out
}

/// Compute candidate home root directories derived from `paths` and `HOME` for sensitive-path exclusion.
#[must_use]
pub fn candidate_home_dirs(paths: &HostEnvironmentPaths) -> Vec<PathBuf> {
    let mut homes: Vec<PathBuf> = Vec::new();
    let mut seen: HashSet<PathBuf> = HashSet::new();
    let mut push_home = |p: PathBuf| {
        let r = resolve_strict_false(&p);
        if seen.insert(r.clone()) {
            homes.push(r);
        }
    };
    if let Some(h) = env::var_os("HOME").filter(|v| !v.is_empty()) {
        push_home(PathBuf::from(h));
    }
    for root in [
        &paths.codex_home_dir,
        &paths.agy_data_dir,
        &paths.gemini_config_dir,
        &paths.sync_state_dir,
    ] {
        let abs = lexical_abs_path(root);
        let name = abs.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if let Some(parent) = abs.parent() {
            if matches!(name, "antigravity" | "config" | "codex-agy-sync")
                && parent.file_name().and_then(|n| n.to_str()) == Some(".gemini")
            {
                if let Some(grand) = parent.parent() {
                    push_home(grand.to_path_buf());
                }
            } else if name.starts_with('.') {
                push_home(parent.to_path_buf());
            }
        }
    }
    homes
}

const EXCLUDED_HOME_SUBDIRS: &[&str] = &[
    "Archive",
    ".private_keys",
    ".Trash",
    "Library/Mail",
    "Library/Messages",
    "Library/Safari",
    "Library/Cookies",
    "Library/HomeKit",
    "Library/IdentityServices",
    "Library/Suggestions",
    "Library/Calendars",
    "Library/Reminders",
    "Library/PersonalizationPortrait",
    "Library/Metadata/CoreSpotlight",
    "Library/Containers",
    "Library/Group Containers",
    "Library/Application Support/AddressBook",
    "Library/Application Support/CallHistoryDB",
    "Library/Application Support/MobileSync",
    "Library/Application Support/com.apple.TCC",
];

fn path_starts_with_ci(cand: &Path, root: &Path) -> bool {
    if cand.starts_with(root) {
        return true;
    }
    if cfg!(target_os = "macos") {
        let cand_parts: Vec<String> = cand
            .components()
            .map(|c| c.as_os_str().to_string_lossy().to_lowercase())
            .collect();
        let root_parts: Vec<String> = root
            .components()
            .map(|c| c.as_os_str().to_string_lossy().to_lowercase())
            .collect();
        return cand_parts.len() >= root_parts.len()
            && cand_parts[..root_parts.len()] == root_parts[..];
    }
    false
}

fn is_path_variant_excluded(cand: &Path, homes: &[PathBuf]) -> bool {
    for comp in cand.components() {
        if let Component::Normal(os_s) = comp
            && let Some(s) = os_s.to_str()
        {
            let matched = if cfg!(target_os = "macos") {
                matches!(
                    s.to_lowercase().as_str(),
                    ".private_keys" | ".trash" | ".trashes"
                )
            } else {
                matches!(s, ".private_keys" | ".Trash" | ".Trashes")
            };
            if matched {
                return true;
            }
        }
    }
    if path_starts_with_ci(
        cand,
        Path::new("/Library/Application Support/com.apple.TCC"),
    ) {
        return true;
    }
    for h in homes {
        let h_lex = lexical_abs_path(h);
        for base in [h.as_path(), h_lex.as_path()] {
            for forbidden_rel in EXCLUDED_HOME_SUBDIRS {
                if path_starts_with_ci(cand, &base.join(forbidden_rel)) {
                    return true;
                }
            }
        }
    }
    false
}

/// Return `true` if `path` resides under `~/Archive`, `~/.private_keys`, `.Trash`, or macOS TCC-protected directories.
#[must_use]
pub fn is_excluded_for_homes(path: &Path, homes: &[PathBuf]) -> bool {
    if homes.is_empty() {
        return false;
    }
    let lex = lexical_abs_path(path);
    if is_path_variant_excluded(&lex, homes) {
        return true;
    }
    let res = resolve_strict_false(path);
    res != lex && is_path_variant_excluded(&res, homes)
}

/// Helper matching `parse_workspace_identity` cleanly inside `cas-host`.
///
/// # Errors
/// Propagates `CoreError::InvalidWorkspace`.
pub fn parse_ws_ident(
    raw: &str,
) -> Result<(cas_core::WorkspacePath, cas_core::WorkspaceRootUri), HostError> {
    Ok(parse_workspace_identity(raw)?)
}
