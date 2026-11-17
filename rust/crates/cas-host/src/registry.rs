//! Bidirectional workspace-to-project registration, pairing diagnosis, and cursor listing.

use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use cas_core::{
    AgyProjectId, CascadeId, CodexLegacyProjectId, CodexProjectId, CodexThreadId, HostKind,
    WorkspacePath, WorkspaceRootUri, validate_session_id,
};
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::catalog::{
    CombinedSessionDescriptor, PreloadedHostTables, build_combined_lineage, discover_agy_entries,
    discover_codex_entries, find_readonly_agy_project_id, load_preloaded_host_tables,
    uris_match_workspace,
};
use crate::error::HostError;
use crate::locks::PresenceWalGuard;
use crate::paths::{
    HostEnvironmentPaths, atomic_write_text, deterministic_uuid, expand_existing_with_sidecars,
    parse_ws_ident,
};
use crate::rpc_agy::register_agy_project_via_rpc;
use crate::rpc_codex::register_codex_project_via_rpc;
use crate::sqlite::{ensure_codex_state_db_schema, open_locked_rw_sqlite, open_readonly_sqlite};

/// Verified bidirectional project identity for a single workspace folder.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WorkspaceProjectBinding {
    pub workspace_path: String,
    pub workspace_uri: String,
    pub workspace_name: String,
    pub agy_project_id: AgyProjectId,
    pub codex_project_id: CodexProjectId,
    pub codex_legacy_project_id: CodexLegacyProjectId,
}

impl WorkspaceProjectBinding {
    #[must_use]
    pub fn parsed_path(&self) -> WorkspacePath {
        parse_ws_ident(&self.workspace_path)
            .map(|(p, _)| p)
            .unwrap_or_else(|_| unreachable!())
    }

    #[must_use]
    pub fn parsed_uri(&self) -> WorkspaceRootUri {
        WorkspaceRootUri::parse(&self.workspace_uri).unwrap_or_else(|_| unreachable!())
    }
}

/// Summary of a discovered or paired conversation across Antigravity and Codex.
#[derive(Debug, Clone, Serialize)]
pub struct PairedSessionDescriptor {
    pub cascade_id: Option<String>,
    pub codex_thread_id: Option<String>,
    pub title: String,
    pub agy_step_count: u32,
    pub codex_turn_count: usize,
    pub parent_cascade_id: Option<String>,
    pub parent_codex_thread_id: Option<String>,
    pub is_subagent: bool,
    pub nesting_depth: u32,
    pub agent_type_name: String,
    pub agent_role: String,
}

impl From<CombinedSessionDescriptor> for PairedSessionDescriptor {
    fn from(d: CombinedSessionDescriptor) -> Self {
        Self {
            cascade_id: d.cascade_id,
            codex_thread_id: d.codex_thread_id,
            title: d.title,
            agy_step_count: d.agy_step_count,
            codex_turn_count: d.codex_turn_count,
            parent_cascade_id: d.parent_cascade_id,
            parent_codex_thread_id: d.parent_codex_thread_id,
            is_subagent: d.is_subagent,
            nesting_depth: d.nesting_depth,
            agent_type_name: d.agent_type_name,
            agent_role: d.agent_role,
        }
    }
}

/// Read-only report of Antigravity/Codex project pairing and live daemon status.
#[derive(Debug, Clone, Serialize)]
pub struct WorkspaceBindingDiagnosis {
    pub workspace_path: String,
    pub workspace_uri: String,
    pub workspace_name: String,
    pub agy_registered: bool,
    pub codex_registered: bool,
    pub agy_project_id: Option<String>,
    pub codex_project_id: Option<String>,
    pub agy_daemon_active: bool,
    pub codex_daemon_active: bool,
    pub remediation_steps: Vec<String>,
}

impl WorkspaceBindingDiagnosis {
    #[must_use]
    pub const fn is_paired(&self) -> bool {
        self.agy_registered && self.codex_registered
    }
}

fn workspace_display_name(ws_path: &WorkspacePath) -> String {
    ws_path
        .as_path()
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("workspace")
        .to_owned()
}

/// Owns bidirectional workspace-to-project resolution and session discovery.
#[derive(Debug, Clone)]
pub struct ProjectRegistry {
    paths: HostEnvironmentPaths,
}

impl ProjectRegistry {
    #[must_use]
    pub const fn new(paths: HostEnvironmentPaths) -> Self {
        Self { paths }
    }

    #[must_use]
    pub const fn paths(&self) -> &HostEnvironmentPaths {
        &self.paths
    }

    #[must_use]
    pub fn find_readonly_agy_project_id(
        &self,
        ws_path: &WorkspacePath,
        ws_uri: &WorkspaceRootUri,
        preloaded: Option<&PreloadedHostTables>,
    ) -> Option<AgyProjectId> {
        find_readonly_agy_project_id(&self.paths, ws_path, ws_uri, preloaded)
    }

    #[must_use]
    pub fn find_readonly_codex_project_id(
        &self,
        ws_path: &WorkspacePath,
        ws_uri: &WorkspaceRootUri,
    ) -> Option<String> {
        let state_db = self.paths.codex_home_dir.join("state_5.sqlite");
        if !state_db.is_file() {
            return None;
        }
        let Ok(conn) = open_readonly_sqlite(&state_db) else {
            return None;
        };
        let Ok(mut stmt) = conn.prepare("SELECT project_id, path FROM project_roots") else {
            return None;
        };
        let Ok(mut rows) = stmt.query([]) else {
            return None;
        };
        while let Ok(Some(row)) = rows.next() {
            let pid_val: String = row
                .get::<_, Option<String>>(0)
                .ok()
                .flatten()
                .unwrap_or_default();
            let raw_root: String = row
                .get::<_, Option<String>>(1)
                .ok()
                .flatten()
                .unwrap_or_default();
            let pid_trim = pid_val.trim();
            if pid_trim.is_empty() {
                continue;
            }
            if uris_match_workspace(std::slice::from_ref(&raw_root), ws_path, ws_uri) {
                return Some(pid_trim.to_owned());
            }
        }
        None
    }

    fn load_codex_global_state(&self) -> (PathBuf, Map<String, Value>) {
        let gs_path = self.paths.codex_home_dir.join(".codex-global-state.json");
        if gs_path.is_file()
            && let Ok(text) = fs::read_to_string(&gs_path)
            && let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text)
        {
            return (gs_path, obj);
        }
        (gs_path, Map::new())
    }

    fn find_legacy_codex_project_id(
        &self,
        gs_data: &Map<String, Value>,
        ws_path: &WorkspacePath,
        ws_uri: &WorkspaceRootUri,
    ) -> Option<String> {
        let local_projects = gs_data.get("local-projects")?.as_object()?;
        let mut keys: Vec<&String> = local_projects.keys().collect();
        keys.sort();
        for lpid in keys {
            let trim_id = lpid.trim();
            if trim_id.is_empty() {
                continue;
            }
            let Some(lentry) = local_projects[lpid].as_object() else {
                continue;
            };
            let Some(roots) = lentry.get("rootPaths").and_then(Value::as_array) else {
                continue;
            };
            for rp in roots {
                if let Some(s) = rp.as_str()
                    && uris_match_workspace(std::slice::from_ref(&s.to_owned()), ws_path, ws_uri)
                {
                    return Some(trim_id.to_owned());
                }
            }
        }
        None
    }

    /// Resolve a workspace binding strictly read-only (synthesizing deterministic fallback IDs if unregistered).
    ///
    /// # Errors
    /// Returns [`HostError`] if `workspace` is invalid.
    pub fn resolve_readonly_binding(
        &self,
        workspace: &str,
    ) -> Result<WorkspaceProjectBinding, HostError> {
        let (ws_path, ws_uri) = parse_ws_ident(workspace)?;
        let ws_name = workspace_display_name(&ws_path);
        let agy_pid = self
            .find_readonly_agy_project_id(&ws_path, &ws_uri, None)
            .unwrap_or_else(|| {
                AgyProjectId::parse(&deterministic_uuid("agy-project", ws_uri.as_str()))
                    .unwrap_or_else(|_| unreachable!())
            });
        let codex_pid_str = self
            .find_readonly_codex_project_id(&ws_path, &ws_uri)
            .unwrap_or_else(|| deterministic_uuid("codex-project", ws_uri.as_str()));
        let (_, gs_data) = self.load_codex_global_state();
        let legacy_pid_str = self
            .find_legacy_codex_project_id(&gs_data, &ws_path, &ws_uri)
            .unwrap_or_else(|| deterministic_uuid("codex-legacy-project", ws_uri.as_str()));

        Ok(WorkspaceProjectBinding {
            workspace_path: ws_path.to_string(),
            workspace_uri: ws_uri.as_str().to_owned(),
            workspace_name: ws_name,
            agy_project_id: agy_pid,
            codex_project_id: CodexProjectId::parse(&codex_pid_str)?,
            codex_legacy_project_id: CodexLegacyProjectId::parse(&legacy_pid_str)?,
        })
    }

    pub fn ensure_agy_project(
        &self,
        ws_path: &WorkspacePath,
        ws_uri: &WorkspaceRootUri,
        ws_name: &str,
    ) -> Result<AgyProjectId, HostError> {
        if let Some(existing) = self.find_readonly_agy_project_id(ws_path, ws_uri, None) {
            return Ok(existing);
        }
        let projects_dir = self.paths.gemini_config_dir.join("projects");
        fs::create_dir_all(&projects_dir)?;
        let new_pid = deterministic_uuid("agy-project", ws_uri.as_str());
        let doc = json!({
            "id": new_pid,
            "name": ws_name,
            "projectResources": {"resources": [{"folderUri": ws_uri.as_str()}]},
            "permissionGrants": {"v2Migrated": true},
            "settings": {},
            "isWorkspaceOnly": false,
        });
        let rendered = format!("{}\n", serde_json::to_string_pretty(&doc)?);
        atomic_write_text(&projects_dir.join(format!("{new_pid}.json")), &rendered)?;
        Ok(AgyProjectId::parse(&new_pid)?)
    }

    pub fn ensure_codex_project(
        &self,
        ws_path: &WorkspacePath,
        ws_uri: &WorkspaceRootUri,
        ws_name: &str,
    ) -> Result<(CodexProjectId, CodexLegacyProjectId), HostError> {
        fs::create_dir_all(&self.paths.codex_home_dir)?;
        let state_db = self.paths.codex_home_dir.join("state_5.sqlite");
        let now_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0i64, |d| d.as_millis() as i64);
        let ws_str = ws_path.to_string();

        let existing_pid = self.find_readonly_codex_project_id(ws_path, ws_uri);
        let (gs_path, mut gs_data) = self.load_codex_global_state();
        let legacy_pid = self
            .find_legacy_codex_project_id(&gs_data, ws_path, ws_uri)
            .unwrap_or_else(|| deterministic_uuid("codex-legacy-project", ws_uri.as_str()));

        let codex_pid_str = if let Some(pid) = existing_pid {
            pid
        } else {
            let pid = deterministic_uuid("codex-project", ws_uri.as_str());
            let conn = open_locked_rw_sqlite(&state_db)?;
            ensure_codex_state_db_schema(&conn)?;
            let next_pos: i64 = conn
                .query_row(
                    "SELECT COALESCE(MAX(position), -1) FROM projects",
                    [],
                    |r| r.get::<_, i64>(0),
                )
                .unwrap_or(-1)
                + 1;
            conn.execute(
                "INSERT OR IGNORE INTO projects (id, name, metadata, position, created_at_ms, updated_at_ms) VALUES (?, ?, '{}', ?, ?, ?)",
                rusqlite::params![pid, ws_name, next_pos, now_ms, now_ms],
            )?;
            conn.execute(
                "INSERT OR REPLACE INTO project_roots (project_id, position, path) VALUES (?, 0, ?)",
                rusqlite::params![pid, ws_str],
            )?;
            conn.execute(
                "INSERT OR REPLACE INTO project_idempotency_keys (key, project_id, created_at_ms) VALUES (?, ?, ?)",
                rusqlite::params![format!("codex-agy-sync:project:{}", ws_uri.as_str()), pid, now_ms],
            )?;
            pid
        };

        let local_projects = gs_data
            .entry("local-projects")
            .or_insert_with(|| Value::Object(Map::new()));
        if !local_projects.is_object() {
            *local_projects = Value::Object(Map::new());
        }
        if let Some(lp_obj) = local_projects.as_object_mut() {
            let created_at = lp_obj
                .get(&legacy_pid)
                .and_then(Value::as_object)
                .and_then(|o| o.get("createdAt"))
                .and_then(Value::as_i64)
                .unwrap_or(now_ms);
            lp_obj.insert(
                legacy_pid.clone(),
                json!({
                    "id": legacy_pid,
                    "name": ws_name,
                    "rootPaths": [ws_str],
                    "createdAt": created_at,
                    "updatedAt": now_ms,
                }),
            );
        }

        let host_map = gs_data
            .entry("app-server-project-id-by-legacy-project-id-by-host")
            .or_insert_with(|| Value::Object(Map::new()));
        if !host_map.is_object() {
            *host_map = Value::Object(Map::new());
        }
        let host_key = format!("local:{}", self.paths.codex_home_dir.display());
        if let Some(hm_obj) = host_map.as_object_mut() {
            let inner = hm_obj
                .entry(&host_key)
                .or_insert_with(|| Value::Object(Map::new()));
            if !inner.is_object() {
                *inner = Value::Object(Map::new());
            }
            if let Some(in_obj) = inner.as_object_mut() {
                in_obj.insert(legacy_pid.clone(), Value::String(codex_pid_str.clone()));
            }
        }

        let proj_order = gs_data
            .entry("project-order")
            .or_insert_with(|| Value::Array(Vec::new()));
        if !proj_order.is_array() {
            *proj_order = Value::Array(Vec::new());
        }
        if let Some(arr) = proj_order.as_array_mut()
            && !arr.iter().any(|v| v.as_str() == Some(legacy_pid.as_str()))
        {
            arr.push(Value::String(legacy_pid.clone()));
        }

        let rendered = format!(
            "{}\n",
            serde_json::to_string_pretty(&Value::Object(gs_data))?
        );
        atomic_write_text(&gs_path, &rendered)?;

        Ok((
            CodexProjectId::parse(&codex_pid_str)?,
            CodexLegacyProjectId::parse(&legacy_pid)?,
        ))
    }

    pub fn ensure_workspace_binding(
        &self,
        workspace: &str,
    ) -> Result<WorkspaceProjectBinding, HostError> {
        let (ws_path, ws_uri) = parse_ws_ident(workspace)?;
        let ws_name = workspace_display_name(&ws_path);
        let agy_pid = self.ensure_agy_project(&ws_path, &ws_uri, &ws_name)?;
        let (codex_pid, codex_legacy_pid) =
            self.ensure_codex_project(&ws_path, &ws_uri, &ws_name)?;
        Ok(WorkspaceProjectBinding {
            workspace_path: ws_path.to_string(),
            workspace_uri: ws_uri.as_str().to_owned(),
            workspace_name: ws_name,
            agy_project_id: agy_pid,
            codex_project_id: codex_pid,
            codex_legacy_project_id: codex_legacy_pid,
        })
    }

    pub fn ensure_agy_binding(
        &self,
        workspace: &str,
    ) -> Result<WorkspaceProjectBinding, HostError> {
        let mut ro = self.resolve_readonly_binding(workspace)?;
        let ws_path = ro.parsed_path();
        let ws_uri = ro.parsed_uri();
        ro.agy_project_id = self.ensure_agy_project(&ws_path, &ws_uri, &ro.workspace_name)?;
        Ok(ro)
    }

    pub fn ensure_codex_binding(
        &self,
        workspace: &str,
    ) -> Result<WorkspaceProjectBinding, HostError> {
        let mut ro = self.resolve_readonly_binding(workspace)?;
        let ws_path = ro.parsed_path();
        let ws_uri = ro.parsed_uri();
        let (cp, lp) = self.ensure_codex_project(&ws_path, &ws_uri, &ro.workspace_name)?;
        ro.codex_project_id = cp;
        ro.codex_legacy_project_id = lp;
        Ok(ro)
    }

    /// Load sorted `(cascade_id, codex_thread_id)` pairs from `<sync_state_dir>/cursors/*.json`.
    #[must_use]
    pub fn load_sorted_workspace_cursor_pairs(
        &self,
        workspace_uri: &WorkspaceRootUri,
    ) -> Vec<(String, String)> {
        let cursors_dir = self.paths.sync_state_dir.join("cursors");
        let Ok(rd) = fs::read_dir(&cursors_dir) else {
            return Vec::new();
        };
        let mut records: Vec<((String, String), String, String)> = Vec::new();
        for entry in rd.flatten() {
            let p = entry.path();
            if !p.is_file() || p.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let Ok(text) = fs::read_to_string(&p) else {
                continue;
            };
            let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text) else {
                continue;
            };
            if obj.get("workspace_uri").and_then(Value::as_str) != Some(workspace_uri.as_str()) {
                continue;
            }
            let cid = obj
                .get("cascade_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| validate_session_id(s, "cascade_id").is_ok());
            let tid = obj
                .get("codex_thread_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| validate_session_id(s, "codex_thread_id").is_ok());
            if let (Some(c), Some(t)) = (cid, tid) {
                let upd = obj
                    .get("updated_at_iso")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                let fname = p
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("")
                    .to_owned();
                records.push(((upd, fname), c.to_owned(), t.to_owned()));
            }
        }
        records.sort_by(|a, b| b.0.cmp(&a.0));
        records.into_iter().map(|(_, c, t)| (c, t)).collect()
    }

    /// List discovered and paired workspace sessions for `status`.
    #[must_use]
    pub fn list_workspace_sessions(
        &self,
        binding: &WorkspaceProjectBinding,
        include_subagents: bool,
    ) -> Vec<PairedSessionDescriptor> {
        let ws_path = binding.parsed_path();
        let ws_uri = binding.parsed_uri();
        let preloaded = load_preloaded_host_tables(&self.paths);
        let agy_entries =
            discover_agy_entries(&self.paths, Some((&ws_path, &ws_uri)), Some(&preloaded));
        let codex_entries =
            discover_codex_entries(&self.paths, Some((&ws_path, &ws_uri)), Some(&preloaded));
        let cursors = self.load_sorted_workspace_cursor_pairs(&ws_uri);
        build_combined_lineage(&agy_entries, &codex_entries, &cursors, include_subagents)
            .into_iter()
            .map(PairedSessionDescriptor::from)
            .collect()
    }

    /// Build a cursor map `{session_id -> paired_target_id}` matching Python status/discovery lookups.
    #[must_use]
    pub fn workspace_cursor_lookup(
        &self,
        workspace_uri: &WorkspaceRootUri,
    ) -> (HashMap<String, String>, HashMap<String, String>) {
        let mut agy_to_codex = HashMap::new();
        let mut codex_to_agy = HashMap::new();
        for (cid, tid) in self.load_sorted_workspace_cursor_pairs(workspace_uri) {
            agy_to_codex
                .entry(cid.clone())
                .or_insert_with(|| tid.clone());
            codex_to_agy.entry(tid).or_insert(cid);
        }
        (agy_to_codex, codex_to_agy)
    }
}

/// Build a read-only workspace pairing diagnosis from pre-resolved IDs and daemon states.
#[must_use]
pub fn build_workspace_binding_diagnosis(
    ws_path: &WorkspacePath,
    ws_uri: &WorkspaceRootUri,
    agy_pid: Option<&str>,
    codex_pid: Option<&str>,
    agy_active: bool,
    codex_active: bool,
) -> WorkspaceBindingDiagnosis {
    let mut steps = Vec::new();
    if agy_pid.is_none() && agy_active {
        steps.push(format!(
            "Open \"{ws_path}\" in Antigravity Desktop so it registers natively, or quit Antigravity Desktop before offline pairing."
        ));
    }
    if codex_pid.is_none() && codex_active {
        steps.push(format!(
            "Open \"{ws_path}\" in Codex Desktop so it registers natively, or quit Codex Desktop before offline pairing."
        ));
    }
    if agy_pid.is_none() || codex_pid.is_none() {
        steps.push(format!(
            "Retry sync with automatic online/offline pairing: codex-agy-sync sync -w \"{ws_path}\" --auto-pair"
        ));
        steps.push(format!(
            "Or retry with offline-only pairing: codex-agy-sync sync -w \"{ws_path}\" --pair-if-offline"
        ));
        steps.push(format!(
            "Or pair explicitly: codex-agy-sync pair -w \"{ws_path}\""
        ));
    }
    WorkspaceBindingDiagnosis {
        workspace_path: ws_path.to_string(),
        workspace_uri: ws_uri.as_str().to_owned(),
        workspace_name: workspace_display_name(ws_path),
        agy_registered: agy_pid.is_some(),
        codex_registered: codex_pid.is_some(),
        agy_project_id: agy_pid.map(str::to_owned),
        codex_project_id: codex_pid.map(str::to_owned),
        agy_daemon_active: agy_active,
        codex_daemon_active: codex_active,
        remediation_steps: steps,
    }
}

/// Inspect read-only workspace registrations and daemon presence without mutating host files.
///
/// # Errors
/// Returns [`HostError`] if `workspace` is invalid.
pub fn diagnose_workspace_binding(
    paths: &HostEnvironmentPaths,
    workspace: &str,
) -> Result<WorkspaceBindingDiagnosis, HostError> {
    let (ws_path, ws_uri) = parse_ws_ident(workspace)?;
    let registry = ProjectRegistry::new(paths.clone());
    let guard = PresenceWalGuard::new(paths.clone());
    let agy_pid = registry.find_readonly_agy_project_id(&ws_path, &ws_uri, None);
    let codex_pid = registry.find_readonly_codex_project_id(&ws_path, &ws_uri);
    let agy_active = guard.discover_agy_daemon().is_some();
    let codex_active = guard.discover_codex_daemon().is_some();
    Ok(build_workspace_binding_diagnosis(
        &ws_path,
        &ws_uri,
        agy_pid.as_ref().map(AgyProjectId::as_str),
        codex_pid.as_deref(),
        agy_active,
        codex_active,
    ))
}

fn busy_pair_error(host: &str, ws_str: &str) -> HostError {
    if host == "sync" {
        return HostError::SyncBusyLock {
            message: "Another sync or pairing operation currently holds sync.lock; wait and retry."
                .to_owned(),
            host: Some("sync".to_owned()),
            remediation: vec![format!(
                "Wait for running sync or pair processes to finish, then retry: codex-agy-sync pair -w \"{ws_str}\""
            )],
        };
    }
    let app_label = if host == "antigravity" {
        "Antigravity"
    } else {
        "Codex"
    };
    HostError::SyncBusyLock {
        message: format!(
            "Cannot register {app_label} project while {app_label} Desktop or WAL lock is active. Open '{ws_str}' in {app_label} Desktop so it registers natively, or quit {app_label} Desktop and retry pairing."
        ),
        host: Some(host.to_owned()),
        remediation: vec![
            format!("Open \"{ws_str}\" in {app_label} Desktop so it registers natively."),
            format!("Or quit running Desktop apps and run: codex-agy-sync pair -w \"{ws_str}\""),
        ],
    }
}

fn classify_busy_pair_error(
    guard: &PresenceWalGuard,
    ws_str: &str,
    need_agy: bool,
    need_codex: bool,
) -> HostError {
    if let Ok(locks) = guard.probe_locks(None, None, false, false, false) {
        if need_codex && !locks.can_write_codex_offline() {
            return busy_pair_error("codex", ws_str);
        }
        if need_agy && !locks.can_write_agy_offline() {
            return busy_pair_error("antigravity", ws_str);
        }
    }
    busy_pair_error("sync", ws_str)
}

fn apply_rpc_project_ids(
    mut binding: WorkspaceProjectBinding,
    agy_rpc_id: Option<&str>,
    codex_rpc_id: Option<&str>,
) -> Result<WorkspaceProjectBinding, HostError> {
    if let Some(a) = agy_rpc_id {
        binding.agy_project_id = AgyProjectId::parse(a)?;
    }
    if let Some(c) = codex_rpc_id {
        binding.codex_project_id = CodexProjectId::parse(c)?;
    }
    Ok(binding)
}

/// Pair missing host registrations via online RPC (when preferred) or locked offline backup.
///
/// Returns `(binding, mode)` where `mode` is `"already_paired"`, `"online_rpc"`, `"hybrid"`, or `"offline_locked"`.
///
/// # Errors
/// Returns [`HostError::SyncBusyLock`] if an active lock blocks offline registration, or [`HostError`] on failure.
pub fn pair_workspace_with_mode(
    paths: &HostEnvironmentPaths,
    guard: &PresenceWalGuard,
    registry: &ProjectRegistry,
    workspace: &str,
    prefer_online_rpc: bool,
) -> Result<(WorkspaceProjectBinding, String), HostError> {
    let ro_binding = registry.resolve_readonly_binding(workspace)?;
    let ws_path = ro_binding.parsed_path();
    let ws_uri = ro_binding.parsed_uri();

    let mut need_agy = registry
        .find_readonly_agy_project_id(&ws_path, &ws_uri, None)
        .is_none();
    let mut need_codex = registry
        .find_readonly_codex_project_id(&ws_path, &ws_uri)
        .is_none();
    if !need_agy && !need_codex {
        return Ok((ro_binding, "already_paired".to_owned()));
    }

    let mut agy_rpc_id: Option<String> = None;
    let mut codex_rpc_id: Option<String> = None;
    let mut online_count = 0usize;

    if prefer_online_rpc {
        if need_agy
            && let Ok(pid) = register_agy_project_via_rpc(
                paths,
                workspace,
                |uri| {
                    let (wp, wu) = parse_ws_ident(uri.as_str()).ok()?;
                    registry.find_readonly_agy_project_id(&wp, &wu, None)
                },
                5.0,
            )
        {
            agy_rpc_id = Some(pid);
            need_agy = false;
            online_count += 1;
        }
        if need_codex && let Ok(pid) = register_codex_project_via_rpc(paths, workspace, 5.0) {
            codex_rpc_id = Some(pid);
            need_codex = false;
            online_count += 1;
        }
    }

    if !need_agy && !need_codex {
        let fresh = registry.resolve_readonly_binding(workspace)?;
        return Ok((
            apply_rpc_project_ids(fresh, agy_rpc_id.as_deref(), codex_rpc_id.as_deref())?,
            "online_rpc".to_owned(),
        ));
    }

    let ws_str = ws_path.to_string();
    let offline_binding = if need_agy && !need_codex {
        let lock_res =
            guard.acquire_offline_write_locks(HostKind::Antigravity, None, None, false, false);
        let _lock_guard = match lock_res {
            Ok(g) => g,
            Err(HostError::Io(ref e)) if e.kind() == std::io::ErrorKind::WouldBlock => {
                return Err(classify_busy_pair_error(guard, &ws_str, true, false));
            }
            Err(e) => return Err(e),
        };
        let candidates = vec![
            paths
                .gemini_config_dir
                .join("projects")
                .join(format!("{}.json", ro_binding.agy_project_id.as_str())),
        ];
        let backup = guard.create_offline_backup(
            HostKind::Antigravity,
            ro_binding.agy_project_id.as_str(),
            &expand_existing_with_sidecars(&candidates),
            &candidates,
            &[],
        )?;
        match registry.ensure_agy_binding(workspace) {
            Ok(b) => b,
            Err(e) => {
                backup.restore();
                return Err(e);
            }
        }
    } else {
        let lock_res = guard.acquire_offline_write_locks(HostKind::Codex, None, None, false, false);
        let locks = match lock_res {
            Ok(g) => g,
            Err(HostError::Io(ref e)) if e.kind() == std::io::ErrorKind::WouldBlock => {
                return Err(classify_busy_pair_error(guard, &ws_str, need_agy, true));
            }
            Err(e) => return Err(e),
        };
        if need_agy && !locks.snapshot.can_write_agy_offline() {
            return Err(busy_pair_error("antigravity", &ws_str));
        }
        let mut candidates = vec![
            paths.codex_home_dir.join("state_5.sqlite"),
            paths.codex_home_dir.join(".codex-global-state.json"),
        ];
        if need_agy {
            candidates.insert(
                0,
                paths
                    .gemini_config_dir
                    .join("projects")
                    .join(format!("{}.json", ro_binding.agy_project_id.as_str())),
            );
        }
        let backup = guard.create_offline_backup(
            HostKind::Codex,
            ro_binding.codex_project_id.as_str(),
            &expand_existing_with_sidecars(&candidates),
            &candidates,
            &[],
        )?;
        let res = if need_agy {
            registry.ensure_workspace_binding(workspace)
        } else {
            registry.ensure_codex_binding(workspace)
        };
        match res {
            Ok(b) => b,
            Err(e) => {
                backup.restore();
                return Err(e);
            }
        }
    };

    let mode = if online_count > 0 {
        "hybrid".to_owned()
    } else {
        "offline_locked".to_owned()
    };
    let final_binding = apply_rpc_project_ids(
        offline_binding,
        agy_rpc_id.as_deref(),
        codex_rpc_id.as_deref(),
    )?;
    let _ = (
        BTreeMap::<String, String>::new(),
        HashMap::<String, String>::new(),
        CascadeId::parse,
        CodexThreadId::parse,
    );
    Ok((final_binding, mode))
}

/// Pair workspace registrations using online RPC when available or offline locked backup.
///
/// # Errors
/// Propagates [`pair_workspace_with_mode`] errors.
pub fn guarded_pair_workspace(
    paths: &HostEnvironmentPaths,
    guard: &PresenceWalGuard,
    registry: &ProjectRegistry,
    workspace: &str,
    prefer_online_rpc: bool,
) -> Result<WorkspaceProjectBinding, HostError> {
    let (binding, _) =
        pair_workspace_with_mode(paths, guard, registry, workspace, prefer_online_rpc)?;
    Ok(binding)
}
