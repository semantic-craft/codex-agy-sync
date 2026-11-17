//! Self-describing atomic backup manifests supporting transactional rollback.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use cas_core::{HostKind, resolve_strict_false};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::HostError;
use crate::paths::{atomic_copy_file, atomic_write_text, now_iso, validated_child_path};

/// Backup manifest tracking original files, expected new files, and rollback globs.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackupManifest {
    pub manifest_id: String,
    pub created_at_iso: String,
    pub target_host: HostKind,
    pub target_session_id: String,
    pub backup_dir: PathBuf,
    pub backed_up_files: Vec<PathBuf>,
    pub file_sha256_map: BTreeMap<String, String>,
    pub restoration_command: String,
    #[serde(default)]
    pub created_targets: Vec<PathBuf>,
    #[serde(skip)]
    pub restore_map: Vec<(PathBuf, PathBuf)>,
    #[serde(skip)]
    pub cleanup_globs: Vec<(PathBuf, String)>,
}

fn unlink_with_sqlite_sidecars(path: &Path, keep_resolved: &[PathBuf]) {
    let resolved = resolve_strict_false(path);
    if !keep_resolved.contains(&resolved) {
        let _ = fs::remove_file(path);
    }
    if matches!(
        path.extension().and_then(|e| e.to_str()),
        Some("db" | "sqlite")
    ) && let Some(fname) = path.file_name().and_then(|n| n.to_str())
    {
        for sfx in ["-wal", "-shm"] {
            let sidecar = path.with_file_name(format!("{fname}{sfx}"));
            if !keep_resolved.contains(&resolve_strict_false(&sidecar)) {
                let _ = fs::remove_file(&sidecar);
            }
        }
    }
}

fn shell_quote(s: &str) -> String {
    if !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.' | '/' | ':' | '@'))
    {
        s.to_owned()
    } else {
        format!("'{}'", s.replace('\'', "'\"'\"'"))
    }
}

impl BackupManifest {
    /// Restore all backed-up originals and remove newly created targets/sidecars.
    pub fn restore(&self) {
        let backed_up_originals: Vec<PathBuf> = self
            .restore_map
            .iter()
            .map(|(_, orig)| resolve_strict_false(orig))
            .collect();

        for (backup_copy, original_target) in &self.restore_map {
            if backup_copy.is_file() {
                let _ = atomic_copy_file(backup_copy, original_target);
            }
            if matches!(
                original_target.extension().and_then(|e| e.to_str()),
                Some("db" | "sqlite")
            ) && let Some(fname) = original_target.file_name().and_then(|n| n.to_str())
            {
                for sfx in ["-wal", "-shm"] {
                    let sidecar = original_target.with_file_name(format!("{fname}{sfx}"));
                    if !backed_up_originals.contains(&resolve_strict_false(&sidecar)) {
                        let _ = fs::remove_file(&sidecar);
                    }
                }
            }
        }

        for created in &self.created_targets {
            unlink_with_sqlite_sidecars(created, &backed_up_originals);
            if created
                .components()
                .any(|c| c.as_os_str() == ".system_generated")
                && let Some(mut parent) = created.parent()
            {
                for _ in 0..3 {
                    if fs::remove_dir(parent).is_err() {
                        break;
                    }
                    let Some(next) = parent.parent() else {
                        break;
                    };
                    parent = next;
                }
            }
        }

        for (root_dir, pattern) in &self.cleanup_globs {
            if !root_dir.is_dir() {
                continue;
            }
            let resolved_root = resolve_strict_false(root_dir);
            let mut stack = vec![root_dir.clone()];
            while let Some(dir) = stack.pop() {
                let Ok(rd) = fs::read_dir(&dir) else {
                    continue;
                };
                for entry in rd.flatten() {
                    let p = entry.path();
                    if p.is_dir() {
                        stack.push(p);
                    } else if p.is_file()
                        && resolve_strict_false(&p).starts_with(&resolved_root)
                        && glob_matches_simple(
                            p.file_name().and_then(|n| n.to_str()).unwrap_or(""),
                            pattern,
                        )
                    {
                        unlink_with_sqlite_sidecars(&p, &backed_up_originals);
                    }
                }
            }
        }
    }
}

fn glob_matches_simple(name: &str, pattern: &str) -> bool {
    if let Some(rest) = pattern.strip_prefix('*') {
        name.ends_with(rest)
    } else if let Some(prefix) = pattern.strip_suffix('*') {
        name.starts_with(prefix)
    } else {
        name == pattern
    }
}

fn resolve_unique_backup_dir(
    sync_state_dir: &Path,
    target_host: HostKind,
    target_session_id: &str,
) -> Result<(String, PathBuf), HostError> {
    let ts_compact = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let base_id = format!(
        "backup-{}-{target_session_id}-{ts_compact}",
        target_host.as_str()
    );
    let mut manifest_id = base_id.clone();
    let backups_root = sync_state_dir.join("backups");
    let mut backup_dir = validated_child_path(&backups_root, &[&manifest_id], "target_session_id")?;
    let mut seq = 1u32;
    while backup_dir.join("manifest.json").exists() {
        seq += 1;
        manifest_id = format!("{base_id}-{seq}");
        backup_dir = validated_child_path(&backups_root, &[&manifest_id], "target_session_id")?;
    }
    fs::create_dir_all(&backup_dir)?;
    Ok((manifest_id, backup_dir))
}

/// Snapshot existing target files and write `manifest.json` before offline mutation.
///
/// # Errors
/// Returns [`HostError`] if session ID validation, hashing, or file I/O fails.
pub fn create_offline_backup_manifest(
    sync_state_dir: &Path,
    target_host: HostKind,
    target_session_id: &str,
    target_files: &[PathBuf],
    expected_new_files: &[PathBuf],
    cleanup_globs: &[(PathBuf, String)],
) -> Result<BackupManifest, HostError> {
    cas_core::validate_session_id(target_session_id, "target_session_id")?;
    let (manifest_id, backup_dir) =
        resolve_unique_backup_dir(sync_state_dir, target_host, target_session_id)?;

    let mut backed_up = Vec::new();
    let mut restore_pairs = Vec::new();
    let mut created_targets = Vec::new();
    let mut seen_created: Vec<PathBuf> = Vec::new();
    let mut sha_map = BTreeMap::new();
    let mut restore_cmds = Vec::new();

    let target_set: Vec<PathBuf> = target_files
        .iter()
        .filter(|p| p.is_file())
        .map(|p| resolve_strict_false(p))
        .collect();

    for (idx, src_path) in target_files.iter().enumerate() {
        if !src_path.is_file() {
            let resolved_missing = resolve_strict_false(src_path);
            if !seen_created.contains(&resolved_missing) {
                seen_created.push(resolved_missing);
                created_targets.push(src_path.clone());
                restore_cmds.push(format!(
                    "rm -f {}",
                    shell_quote(&src_path.display().to_string())
                ));
            }
            continue;
        }
        let fname = src_path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("file");
        let dst_path = backup_dir.join(format!("{idx:02}_{fname}"));
        fs::copy(src_path, &dst_path)?;
        let bytes = fs::read(&dst_path)?;
        let digest = hex::encode(Sha256::digest(&bytes));
        sha_map.insert(src_path.display().to_string(), digest);
        backed_up.push(dst_path.clone());
        restore_pairs.push((dst_path.clone(), src_path.clone()));
        restore_cmds.push(format!(
            "cp -p {} {}",
            shell_quote(&dst_path.display().to_string()),
            shell_quote(&src_path.display().to_string())
        ));
        if matches!(
            src_path.extension().and_then(|e| e.to_str()),
            Some("db" | "sqlite")
        ) && let Some(sfname) = src_path.file_name().and_then(|n| n.to_str())
        {
            for sfx in ["-wal", "-shm"] {
                let sidecar = src_path.with_file_name(format!("{sfname}{sfx}"));
                if !target_set.contains(&resolve_strict_false(&sidecar)) {
                    restore_cmds.push(format!(
                        "rm -f {}",
                        shell_quote(&sidecar.display().to_string())
                    ));
                }
            }
        }
    }

    for new_path in expected_new_files {
        let resolved_new = resolve_strict_false(new_path);
        if !new_path.is_file()
            && !seen_created.contains(&resolved_new)
            && !target_set.contains(&resolved_new)
        {
            seen_created.push(resolved_new);
            created_targets.push(new_path.clone());
            restore_cmds.push(format!(
                "rm -f {}",
                shell_quote(&new_path.display().to_string())
            ));
        }
    }

    let restoration_command = if restore_cmds.is_empty() {
        ":".to_owned()
    } else {
        restore_cmds.join(" && ")
    };
    let created_iso = now_iso();
    let manifest = BackupManifest {
        manifest_id,
        created_at_iso: created_iso,
        target_host,
        target_session_id: target_session_id.to_owned(),
        backup_dir: backup_dir.clone(),
        backed_up_files: backed_up,
        file_sha256_map: sha_map,
        restoration_command,
        created_targets,
        restore_map: restore_pairs,
        cleanup_globs: cleanup_globs.to_vec(),
    };
    let json_text = serde_json::to_string_pretty(&manifest)? + "\n";
    atomic_write_text(&backup_dir.join("manifest.json"), &json_text)?;
    Ok(manifest)
}
