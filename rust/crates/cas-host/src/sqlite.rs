//! Read-only SQLite openers (`?mode=ro` + `PRAGMA query_only = ON`) and host DDL schemas.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::Duration;

use cas_core::resolve_strict_false;
use rusqlite::{Connection, OpenFlags};

use crate::error::HostError;
use crate::locks::OfflineWriteLockGuard;

fn path_to_readonly_sqlite_uri(db_path: &Path) -> Result<String, HostError> {
    let resolved = resolve_strict_false(db_path);
    let url = url::Url::from_file_path(&resolved).map_err(|()| {
        HostError::InvalidArgument(format!(
            "Cannot convert SQLite path to file URI: {}",
            resolved.display()
        ))
    })?;
    Ok(format!("{url}?mode=ro"))
}

/// Open `db_path` strictly read-only with `?mode=ro` and `PRAGMA query_only = ON`.
///
/// # Errors
/// Returns [`HostError::Sqlite`] or [`HostError::InvalidArgument`] if the file cannot be opened read-only.
pub fn open_readonly_sqlite(db_path: &Path) -> Result<Connection, HostError> {
    let uri = path_to_readonly_sqlite_uri(db_path)?;
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI;
    let conn = Connection::open_with_flags(&uri, flags)?;
    conn.busy_timeout(Duration::from_secs(5))?;
    conn.execute_batch("PRAGMA query_only = ON;")?;
    Ok(conn)
}

/// Validated capability proving a handoff staging directory sits strictly inside its staging root.
#[derive(Debug, Clone)]
pub struct HandoffStagingPermit {
    staging_dir: PathBuf,
}

impl HandoffStagingPermit {
    /// Construct a staging write permit after verifying `staging_dir` is strictly inside `staging_root`.
    ///
    /// # Errors
    /// Returns [`HostError::InvalidArgument`] if `staging_dir` equals or escapes `staging_root`.
    pub fn new(staging_root: &Path, staging_dir: &Path) -> Result<Self, HostError> {
        let root_resolved = resolve_strict_false(staging_root);
        let dir_resolved = resolve_strict_false(staging_dir);
        if dir_resolved == root_resolved || !dir_resolved.starts_with(&root_resolved) {
            return Err(HostError::InvalidArgument(format!(
                "Staging directory {} must be strictly inside staging root {}.",
                staging_dir.display(),
                staging_root.display()
            )));
        }
        Ok(Self {
            staging_dir: dir_resolved,
        })
    }

    /// Return the canonicalized staging directory path.
    #[must_use]
    pub fn dir(&self) -> &Path {
        &self.staging_dir
    }
}

/// Capability proof required to open a writable SQLite connection via [`open_locked_rw_sqlite`].
#[derive(Clone, Copy)]
pub enum SqliteWritePermit<'a> {
    Locked(&'a OfflineWriteLockGuard),
    Staging(&'a HandoffStagingPermit),
}

/// Open a writable SQLite connection ONLY with an active [`OfflineWriteLockGuard`] or validated [`HandoffStagingPermit`].
///
/// ```compile_fail
/// use std::path::Path;
/// use cas_host::open_locked_rw_sqlite;
/// let _ = open_locked_rw_sqlite(Path::new("x.db"));
/// ```
///
/// # Errors
/// Returns [`HostError::InvalidArgument`] if a staging permit is used outside its staging directory,
/// or [`HostError::Sqlite`] / [`HostError::Io`] if connection or busy-timeout configuration fails.
pub fn open_locked_rw_sqlite(
    db_path: &Path,
    permit: SqliteWritePermit<'_>,
) -> Result<Connection, HostError> {
    if let SqliteWritePermit::Staging(staging) = permit {
        let resolved_db = resolve_strict_false(db_path);
        if resolved_db == staging.dir() || !resolved_db.starts_with(staging.dir()) {
            return Err(HostError::InvalidArgument(format!(
                "Staging SQLite write path {} escapes staging directory {}.",
                db_path.display(),
                staging.dir().display()
            )));
        }
    }
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let conn = Connection::open(db_path)?;
    conn.busy_timeout(Duration::from_secs(10))?;
    Ok(conn)
}

/// Initialize Antigravity per-conversation `conversations/<cid>.db` schema.
///
/// # Errors
/// Returns [`HostError::Sqlite`] if DDL fails.
pub fn ensure_agy_conversation_db_schema(conn: &Connection) -> Result<(), HostError> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS `trajectory_meta` (
            `trajectory_id` text, `cascade_id` text, `trajectory_type` integer, `source` integer,
            PRIMARY KEY (`trajectory_id`)
        );
        CREATE TABLE IF NOT EXISTS `steps` (
            `idx` integer, `step_type` integer NOT NULL DEFAULT 0, `status` integer NOT NULL DEFAULT 0,
            `has_subtrajectory` numeric NOT NULL DEFAULT false, `metadata` blob, `error_details` blob,
            `permissions` blob, `task_details` blob, `render_info` blob, `step_payload` blob,
            `step_format` integer NOT NULL DEFAULT 0, PRIMARY KEY (`idx`)
        );
        CREATE INDEX IF NOT EXISTS `idx_steps_status` ON `steps`(`status`);
        CREATE INDEX IF NOT EXISTS `idx_steps_step_type` ON `steps`(`step_type`);
        CREATE TABLE IF NOT EXISTS `gen_metadata` (`idx` integer, `data` blob, `size` integer NOT NULL DEFAULT 0, PRIMARY KEY (`idx`));
        CREATE TABLE IF NOT EXISTS `executor_metadata` (`idx` integer, `data` blob, PRIMARY KEY (`idx`));
        CREATE TABLE IF NOT EXISTS `parent_references` (`idx` integer, `data` blob, PRIMARY KEY (`idx`));
        CREATE TABLE IF NOT EXISTS `trajectory_metadata_blob` (`id` text DEFAULT "main", `data` blob, PRIMARY KEY (`id`));
        CREATE TABLE IF NOT EXISTS `battle_mode_infos` (`idx` integer, `data` blob, PRIMARY KEY (`idx`));
        "#,
    )?;
    let ver: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap_or(0);
    if ver == 0 {
        conn.execute_batch("PRAGMA user_version = 1;")?;
    }
    Ok(())
}

/// Initialize Antigravity `conversation_summaries.db` schema.
///
/// # Errors
/// Returns [`HostError::Sqlite`] if DDL fails.
pub fn ensure_agy_summaries_db_schema(conn: &Connection) -> Result<(), HostError> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS `conversation_summaries` (
            `conversation_id` text, `title` text NOT NULL DEFAULT "", `preview` text NOT NULL DEFAULT "",
            `step_count` integer NOT NULL DEFAULT 0, `last_modified_time` datetime NOT NULL,
            `workspace_uris` text NOT NULL, `status` text NOT NULL DEFAULT "", `source` text NOT NULL DEFAULT "",
            `project_id` text NOT NULL DEFAULT "", `agent_name` text NOT NULL DEFAULT "",
            `parent_conversation_id` text NOT NULL DEFAULT "", `nesting_depth` integer NOT NULL DEFAULT 0,
            `battle_id` text NOT NULL DEFAULT "", `winning_conversation_id` text NOT NULL DEFAULT "",
            `not_fully_idle` numeric NOT NULL DEFAULT false, `killed` numeric NOT NULL DEFAULT false,
            `last_user_input_time` datetime NOT NULL, `last_user_input_step_index` integer NOT NULL DEFAULT -1,
            `app_data_dir` text NOT NULL DEFAULT "", `raw_summary` blob, `group_id` text NOT NULL DEFAULT "",
            PRIMARY KEY (`conversation_id`)
        );
        CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_user_input_time` ON `conversation_summaries`(`last_user_input_time`);
        CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_modified_time` ON `conversation_summaries`(`last_modified_time`);
        "#,
    )?;
    Ok(())
}

/// Initialize Codex `state_5.sqlite` schema.
///
/// # Errors
/// Returns [`HostError::Sqlite`] if DDL fails.
pub fn ensure_codex_state_db_schema(conn: &Connection) -> Result<(), HostError> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_roots (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, path TEXT NOT NULL, PRIMARY KEY (project_id, position)
        );
        CREATE TABLE IF NOT EXISTS project_idempotency_keys (
            key TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,
            cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
            approval_mode TEXT NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
            has_user_event INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
            archived_at INTEGER, git_sha TEXT, git_branch TEXT, git_origin_url TEXT,
            cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '',
            agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT 'enabled',
            model TEXT, reasoning_effort TEXT, agent_path TEXT, created_at_ms INTEGER,
            updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT '',
            recency_at INTEGER NOT NULL DEFAULT 0, recency_at_ms INTEGER NOT NULL DEFAULT 0,
            history_mode TEXT NOT NULL DEFAULT 'legacy', name TEXT, is_pinned INTEGER NOT NULL DEFAULT 0,
            thread_section_id TEXT, section_position INTEGER, section_entered_at_ms INTEGER,
            project_id TEXT, originator TEXT, daybreak_enabled BOOLEAN,
            creator_user_id TEXT, creator_account_id TEXT, attached_thread_parent_id TEXT
        );
        "#,
    )?;
    Ok(())
}

/// Inspect existing column names of `table_name` via `PRAGMA table_info`.
///
/// # Errors
/// Returns [`HostError::Sqlite`] on query failure.
pub fn table_column_names(
    conn: &Connection,
    table_name: &str,
) -> Result<HashSet<String>, HostError> {
    let mut stmt = conn.prepare(&format!("PRAGMA table_info({table_name})"))?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
    let mut out = HashSet::new();
    for r in rows {
        out.insert(r?);
    }
    Ok(out)
}

/// Build a SQL SELECT column projection emitting `NULL AS <col>` for absent optional columns.
#[must_use]
pub fn optional_select_columns_sql(cols: &HashSet<String>, desired: &[&str]) -> String {
    desired
        .iter()
        .map(|&col| {
            if cols.contains(col) {
                col.to_owned()
            } else {
                format!("NULL AS {col}")
            }
        })
        .collect::<Vec<_>>()
        .join(", ")
}
