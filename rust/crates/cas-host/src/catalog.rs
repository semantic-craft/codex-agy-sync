//! Unified read-only catalog scanner for Antigravity and Codex host stores.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use cas_codecs::{
    decode_cascade_summary_proto, decode_trajectory_metadata_proto, parse_annotation_pbtxt_title,
    read_codex_rollout_lines,
};
use cas_core::{
    AgyProjectId, CascadeId, CodexThreadId, FORBIDDEN_AGY_PROJECT_IDS, HostKind, SubagentLineage,
    WorkspacePath, WorkspaceRootUri, normalize_single_line_query, raw_spec_to_candidate_path,
    validate_session_id,
};
use rusqlite::Connection;
use serde_json::Value;

use crate::error::HostError;
use crate::paths::{
    HostEnvironmentPaths, parse_ts_epoch_iso, parse_ws_ident, validated_child_path,
};
use crate::sqlite::{open_readonly_sqlite, optional_select_columns_sql, table_column_names};

/// Row read from Antigravity `conversation_summaries.db` (or decoded `raw_summary`).
#[derive(Debug, Clone, Default)]
pub struct AgySummaryRow {
    pub cascade_id: String,
    pub title: String,
    pub preview: String,
    pub step_count: u32,
    pub last_modified_time: String,
    pub workspace_uris: String,
    pub parsed_uris: Vec<String>,
    pub status: String,
    pub source: String,
    pub project_id: String,
    pub agent_name: String,
    pub parent_conversation_id: String,
    pub nesting_depth: u32,
    pub agent_type_name: String,
    pub agent_role: String,
    pub last_user_input_time: String,
    pub last_user_input_step_index: i64,
    pub raw_summary: Option<Vec<u8>>,
    pub created_at_iso: String,
}

/// Row read from Codex `state_5.sqlite` `threads` table.
#[derive(Debug, Clone, Default)]
pub struct CodexThreadRow {
    pub id: String,
    pub rollout_path: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub created_at_ms: Option<i64>,
    pub updated_at_ms: Option<i64>,
    pub source: String,
    pub cwd: String,
    pub title: String,
    pub first_user_message: String,
    pub name: String,
    pub project_id: String,
    pub agent_nickname: String,
    pub agent_role: String,
    pub archived: i64,
}

/// Row read from Codex `state_5.sqlite` `projects` + `project_roots`.
#[derive(Debug, Clone, Default)]
pub struct CodexProjectRow {
    pub id: String,
    pub name: String,
    pub roots: Vec<String>,
}

/// Preloaded read-only snapshot of both hosts' catalog tables for one scan/feed pass.
#[derive(Debug, Clone, Default)]
pub struct PreloadedHostTables {
    pub agy_summaries: HashMap<String, AgySummaryRow>,
    pub agy_projects: Vec<(AgyProjectId, String, Vec<String>)>,
    pub codex_threads: HashMap<String, CodexThreadRow>,
    pub codex_projects: Vec<CodexProjectRow>,
    pub codex_global_state_roots: Vec<String>,
    pub codex_legacy_projects: Vec<(String, Vec<String>)>,
}

/// Discovered Antigravity session entry for a workspace (or global feed).
#[derive(Debug, Clone)]
pub struct AgyCatalogEntry {
    pub cascade_id: CascadeId,
    pub title: String,
    pub preview: String,
    pub step_count: u32,
    pub updated_at_iso: String,
    pub sort_epoch: f64,
    pub workspace_uris: Vec<String>,
    pub project_id: String,
    pub source_name: String,
    pub parent_cascade_id: Option<CascadeId>,
    pub nesting_depth: u32,
    pub agent_type_name: String,
    pub agent_role: String,
    pub is_subagent: bool,
    pub conv_db_exists: bool,
    pub trajectory_metadata_blob: Vec<u8>,
}

/// Discovered Codex session entry for a workspace (or global feed).
#[derive(Debug, Clone)]
pub struct CodexCatalogEntry {
    pub thread_id: CodexThreadId,
    pub rollout_path: Option<PathBuf>,
    pub title: String,
    pub first_user_message: String,
    pub cwd: String,
    pub project_id: String,
    pub updated_at_iso: String,
    pub sort_epoch: f64,
    pub turn_count: usize,
    pub parent_thread_id: Option<CodexThreadId>,
    pub nesting_depth: u32,
    pub agent_type_name: String,
    pub agent_role: String,
    pub is_subagent: bool,
}

/// Combined session descriptor emitted by `status` and `sync --pick`.
#[derive(Debug, Clone, serde::Serialize)]
pub struct CombinedSessionDescriptor {
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

/// Parsed Codex `threads.source` subagent lineage.
#[derive(Debug, Clone, Default)]
pub struct ParsedCodexSourceLineage {
    pub parent_thread_id: Option<CodexThreadId>,
    pub depth: u32,
    pub agent_type_name: String,
    pub agent_role: String,
    pub is_subagent: bool,
}

fn push_unique_str(out: &mut Vec<String>, seen: &mut HashSet<String>, raw: &str) {
    let trimmed = raw.trim();
    if !trimmed.is_empty() && seen.insert(trimmed.to_owned()) {
        out.push(trimmed.to_owned());
    }
}

/// Extract candidate workspace paths/URIs from an Antigravity project JSON metadata object.
#[must_use]
pub fn extract_agy_project_folder_uris(raw: &Value) -> Vec<String> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    let Some(obj) = raw.as_object() else {
        return out;
    };

    for key in [
        "workspaceUri",
        "workspace_uri",
        "rootPath",
        "root_path",
        "workspacePath",
        "workspace_path",
        "projectRoot",
        "project_root",
        "folderUri",
        "folder_uri",
        "path",
        "uri",
    ] {
        if let Some(s) = obj.get(key).and_then(Value::as_str) {
            push_unique_str(&mut out, &mut seen, s);
        }
    }

    for key in [
        "workspaceUris",
        "workspace_uris",
        "folders",
        "folderUris",
        "folder_uris",
        "workspaces",
        "roots",
        "resources",
    ] {
        if let Some(arr) = obj.get(key).and_then(Value::as_array) {
            extract_uris_from_array(arr, &mut out, &mut seen);
        }
    }
    if let Some(res_arr) = obj
        .get("projectResources")
        .and_then(Value::as_object)
        .and_then(|pr| pr.get("resources"))
        .and_then(Value::as_array)
    {
        extract_uris_from_array(res_arr, &mut out, &mut seen);
    }
    out
}

fn extract_uris_from_array(arr: &[Value], out: &mut Vec<String>, seen: &mut HashSet<String>) {
    for item in arr {
        match item {
            Value::String(s) => push_unique_str(out, seen, s),
            Value::Object(m) => {
                for k in [
                    "uri",
                    "path",
                    "folderUri",
                    "workspaceFolderAbsoluteUri",
                    "gitRootAbsoluteUri",
                    "rootUri",
                ] {
                    if let Some(s) = m.get(k).and_then(Value::as_str) {
                        push_unique_str(out, seen, s);
                    }
                }
                if let Some(repo) = m.get("gitRepo").and_then(Value::as_object)
                    && let Some(ru) = repo.get("rootUri").and_then(Value::as_str)
                {
                    push_unique_str(out, seen, ru);
                }
            }
            _ => {}
        }
    }
}

/// Parse `conversation_summaries.workspace_uris` column (JSON array, comma-separated, or bare URI/path).
#[must_use]
pub fn parse_summary_workspace_uris(raw_text: &str) -> Vec<String> {
    let trimmed = raw_text.trim();
    if trimmed.is_empty() {
        return Vec::new();
    }
    if trimmed.starts_with('[')
        && let Ok(Value::Array(arr)) = serde_json::from_str::<Value>(trimmed)
    {
        return arr
            .into_iter()
            .filter_map(|v| v.as_str().map(str::trim).map(str::to_owned))
            .filter(|s| !s.is_empty())
            .collect();
    }
    if trimmed.contains(',') && !trimmed.starts_with("file://") {
        return trimmed
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
    }
    vec![trimmed.to_owned()]
}

/// Check if any candidate workspace specifier matches `(ws_path, ws_uri)`.
#[must_use]
pub fn uris_match_workspace(
    uris: &[String],
    ws_path: &WorkspacePath,
    ws_uri: &WorkspaceRootUri,
) -> bool {
    for raw in uris {
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            continue;
        }
        if let Ok((cand_path, cand_uri)) = parse_ws_ident(trimmed)
            && (&cand_path == ws_path || &cand_uri == ws_uri)
        {
            return true;
        }
        if let Some(cand) = raw_spec_to_candidate_path(trimmed)
            && cand == ws_path.as_path()
        {
            return true;
        }
    }
    false
}

/// Parse Codex `threads.source` JSON subagent lineage (`{"subagent": {"thread_spawn": ...}}`).
#[must_use]
pub fn parse_codex_thread_source_lineage(
    raw_source: &str,
    agent_nickname: &str,
    agent_role: &str,
) -> ParsedCodexSourceLineage {
    let mut out = ParsedCodexSourceLineage {
        parent_thread_id: None,
        depth: 0,
        agent_type_name: agent_nickname.trim().to_owned(),
        agent_role: agent_role.trim().to_owned(),
        is_subagent: false,
    };
    let trimmed = raw_source.trim();
    if !trimmed.starts_with('{') {
        if trimmed.eq_ignore_ascii_case("subagent") {
            out.is_subagent = true;
            out.depth = 1;
        }
        return out;
    }
    let Ok(Value::Object(root)) = serde_json::from_str::<Value>(trimmed) else {
        return out;
    };
    let Some(sub_obj) = root.get("subagent").and_then(Value::as_object) else {
        return out;
    };
    out.is_subagent = true;
    let spawn = sub_obj
        .get("thread_spawn")
        .and_then(Value::as_object)
        .unwrap_or(sub_obj);
    if let Some(pid) = spawn
        .get("parent_thread_id")
        .or_else(|| spawn.get("parentThreadId"))
        .and_then(Value::as_str)
        && let Ok(parsed) = CodexThreadId::parse(pid.trim())
    {
        out.parent_thread_id = Some(parsed);
    }
    if let Some(d) = spawn.get("depth").and_then(Value::as_u64) {
        out.depth = (d as u32).max(1);
    } else {
        out.depth = 1;
    }
    if out.agent_type_name.is_empty()
        && let Some(nick) = spawn
            .get("agent_nickname")
            .or_else(|| spawn.get("nickname"))
            .and_then(Value::as_str)
    {
        out.agent_type_name = nick.trim().to_owned();
    }
    if out.agent_role.is_empty()
        && let Some(role) = spawn
            .get("agent_role")
            .or_else(|| spawn.get("role"))
            .and_then(Value::as_str)
    {
        out.agent_role = role.trim().to_owned();
    }
    out
}

/// Compute Codex nesting depth recursively with cycle detection.
#[must_use]
pub fn compute_codex_nesting_depth(
    thread_id: &str,
    threads: &HashMap<String, CodexThreadRow>,
) -> u32 {
    let mut visited = HashSet::new();
    let mut current = thread_id.to_owned();
    let mut depth = 0u32;
    while visited.insert(current.clone()) {
        let Some(row) = threads.get(&current) else {
            break;
        };
        let parsed =
            parse_codex_thread_source_lineage(&row.source, &row.agent_nickname, &row.agent_role);
        if !parsed.is_subagent && parsed.parent_thread_id.is_none() {
            break;
        }
        if let Some(parent) = parsed.parent_thread_id {
            depth += 1;
            current = parent.as_str().to_owned();
        } else {
            depth = depth.max(parsed.depth);
            break;
        }
    }
    depth
}

fn col_text(row: &rusqlite::Row<'_>, idx_map: &HashMap<&str, usize>, col: &str) -> String {
    let Some(&idx) = idx_map.get(col) else {
        return String::new();
    };
    row.get::<_, Option<String>>(idx)
        .ok()
        .flatten()
        .unwrap_or_default()
}

fn col_i64(row: &rusqlite::Row<'_>, idx_map: &HashMap<&str, usize>, col: &str) -> Option<i64> {
    let &idx = idx_map.get(col)?;
    row.get::<_, Option<i64>>(idx).ok().flatten()
}

fn col_blob(row: &rusqlite::Row<'_>, idx_map: &HashMap<&str, usize>, col: &str) -> Option<Vec<u8>> {
    let &idx = idx_map.get(col)?;
    row.get::<_, Option<Vec<u8>>>(idx).ok().flatten()
}

fn read_agy_projects_from_dirs(
    paths: &HostEnvironmentPaths,
) -> Vec<(AgyProjectId, String, Vec<String>)> {
    let mut out = Vec::new();
    let dir = paths.gemini_config_dir.join("projects");
    let Ok(rd) = fs::read_dir(&dir) else {
        return out;
    };
    let mut entries: Vec<PathBuf> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_file() && p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    entries.sort();
    for p in entries {
        let Ok(text) = fs::read_to_string(&p) else {
            continue;
        };
        let Ok(val) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let raw_id = val
            .get("id")
            .or_else(|| val.get("projectId"))
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .or_else(|| {
                p.file_stem()
                    .and_then(|s| s.to_str())
                    .map(str::trim)
                    .map(str::to_owned)
            })
            .unwrap_or_default();
        if raw_id.is_empty() || FORBIDDEN_AGY_PROJECT_IDS.contains(&raw_id.as_str()) {
            continue;
        }
        let Ok(pid) = AgyProjectId::parse(&raw_id) else {
            continue;
        };
        let name = val
            .get("name")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("")
            .to_owned();
        let uris = extract_agy_project_folder_uris(&val);
        out.push((pid, name, uris));
    }
    out
}

fn read_agy_summaries_table(conn: &Connection) -> HashMap<String, AgySummaryRow> {
    let mut out = HashMap::new();
    let Ok(cols) = table_column_names(conn, "conversation_summaries") else {
        return out;
    };
    if !cols.contains("conversation_id") {
        return out;
    }
    let desired = [
        "conversation_id",
        "title",
        "preview",
        "step_count",
        "last_modified_time",
        "workspace_uris",
        "status",
        "source",
        "project_id",
        "agent_name",
        "parent_conversation_id",
        "nesting_depth",
        "last_user_input_time",
        "last_user_input_step_index",
        "raw_summary",
    ];
    let select_sql = optional_select_columns_sql(&cols, &desired);
    let idx_map: HashMap<&str, usize> = desired
        .iter()
        .enumerate()
        .map(|(i, name)| (*name, i))
        .collect();
    let sql = format!("SELECT {select_sql} FROM conversation_summaries");
    let Ok(mut stmt) = conn.prepare(&sql) else {
        return out;
    };
    let Ok(mut rows) = stmt.query([]) else {
        return out;
    };
    while let Ok(Some(row)) = rows.next() {
        let cid = col_text(row, &idx_map, "conversation_id");
        let cid_trim = cid.trim();
        if cid_trim.is_empty() || validate_session_id(cid_trim, "cascade_id").is_err() {
            continue;
        }
        let ws_raw = col_text(row, &idx_map, "workspace_uris");
        let mut srow = AgySummaryRow {
            cascade_id: cid_trim.to_owned(),
            title: col_text(row, &idx_map, "title"),
            preview: col_text(row, &idx_map, "preview"),
            step_count: col_i64(row, &idx_map, "step_count").unwrap_or(0).max(0) as u32,
            last_modified_time: col_text(row, &idx_map, "last_modified_time"),
            workspace_uris: ws_raw.clone(),
            parsed_uris: parse_summary_workspace_uris(&ws_raw),
            status: col_text(row, &idx_map, "status"),
            source: col_text(row, &idx_map, "source"),
            project_id: col_text(row, &idx_map, "project_id"),
            agent_name: col_text(row, &idx_map, "agent_name"),
            parent_conversation_id: col_text(row, &idx_map, "parent_conversation_id"),
            nesting_depth: col_i64(row, &idx_map, "nesting_depth").unwrap_or(0).max(0) as u32,
            agent_type_name: String::new(),
            agent_role: String::new(),
            last_user_input_time: col_text(row, &idx_map, "last_user_input_time"),
            last_user_input_step_index: col_i64(row, &idx_map, "last_user_input_step_index")
                .unwrap_or(-1),
            raw_summary: col_blob(row, &idx_map, "raw_summary"),
            created_at_iso: String::new(),
        };
        if let Some(blob) = srow.raw_summary.as_deref()
            && !blob.is_empty()
            && let Ok(parsed_cid) = CascadeId::parse(&srow.cascade_id)
            && let Ok(proj) = decode_cascade_summary_proto(blob, Some(&parsed_cid))
        {
            if srow.title.is_empty() {
                srow.title = proj.title;
            }
            if srow.preview.is_empty() {
                srow.preview = proj.preview;
            }
            if srow.step_count == 0 {
                srow.step_count = proj.step_count;
            }
            if srow.parsed_uris.is_empty() && !proj.workspace_uris.is_empty() {
                srow.parsed_uris = proj
                    .workspace_uris
                    .iter()
                    .map(|u| u.as_str().to_owned())
                    .collect();
            }
            if srow.project_id.is_empty() && !proj.project_id.as_str().is_empty() {
                srow.project_id = proj.project_id.as_str().to_owned();
            }
            if srow.parent_conversation_id.is_empty()
                && let Some(pc) = proj.parent_cascade_id
            {
                srow.parent_conversation_id = pc.as_str().to_owned();
            }
            if srow.nesting_depth == 0 {
                srow.nesting_depth = proj.nesting_depth;
            }
            srow.agent_type_name = proj.agent_type_name;
            srow.agent_role = proj.agent_role;
            srow.created_at_iso = proj.created_at_iso;
        }
        out.insert(srow.cascade_id.clone(), srow);
    }
    out
}

fn read_codex_state_tables(
    conn: &Connection,
) -> (HashMap<String, CodexThreadRow>, Vec<CodexProjectRow>) {
    let mut threads = HashMap::new();
    let mut projects = Vec::new();

    if let Ok(tcols) = table_column_names(conn, "threads")
        && tcols.contains("id")
    {
        let desired = [
            "id",
            "rollout_path",
            "created_at",
            "updated_at",
            "created_at_ms",
            "updated_at_ms",
            "source",
            "cwd",
            "title",
            "first_user_message",
            "name",
            "project_id",
            "agent_nickname",
            "agent_role",
            "archived",
        ];
        let select_sql = optional_select_columns_sql(&tcols, &desired);
        {
            let idx_map: HashMap<&str, usize> =
                desired.iter().enumerate().map(|(i, n)| (*n, i)).collect();
            let sql = format!("SELECT {select_sql} FROM threads");
            if let Ok(mut stmt) = conn.prepare(&sql)
                && let Ok(mut rows) = stmt.query([])
            {
                while let Ok(Some(row)) = rows.next() {
                    let tid = col_text(row, &idx_map, "id");
                    let tid_trim = tid.trim();
                    if tid_trim.is_empty()
                        || validate_session_id(tid_trim, "codex_thread_id").is_err()
                    {
                        continue;
                    }
                    threads.insert(
                        tid_trim.to_owned(),
                        CodexThreadRow {
                            id: tid_trim.to_owned(),
                            rollout_path: col_text(row, &idx_map, "rollout_path"),
                            created_at: col_i64(row, &idx_map, "created_at").unwrap_or(0),
                            updated_at: col_i64(row, &idx_map, "updated_at").unwrap_or(0),
                            created_at_ms: col_i64(row, &idx_map, "created_at_ms"),
                            updated_at_ms: col_i64(row, &idx_map, "updated_at_ms"),
                            source: col_text(row, &idx_map, "source"),
                            cwd: col_text(row, &idx_map, "cwd"),
                            title: col_text(row, &idx_map, "title"),
                            first_user_message: col_text(row, &idx_map, "first_user_message"),
                            name: col_text(row, &idx_map, "name"),
                            project_id: col_text(row, &idx_map, "project_id"),
                            agent_nickname: col_text(row, &idx_map, "agent_nickname"),
                            agent_role: col_text(row, &idx_map, "agent_role"),
                            archived: col_i64(row, &idx_map, "archived").unwrap_or(0),
                        },
                    );
                }
            }
        }
    }

    if let Ok(pcols) = table_column_names(conn, "projects")
        && pcols.contains("id")
    {
        let has_roots = table_column_names(conn, "project_roots")
            .is_ok_and(|rcols| rcols.contains("project_id") && rcols.contains("path"));
        let mut roots_by_pid: HashMap<String, Vec<String>> = HashMap::new();
        if has_roots
            && let Ok(mut stmt) = conn
                .prepare("SELECT project_id, path FROM project_roots ORDER BY project_id, position")
            && let Ok(mut rows) = stmt.query([])
        {
            while let Ok(Some(row)) = rows.next() {
                let pid: String = row.get(0).unwrap_or_default();
                let path: String = row.get(1).unwrap_or_default();
                if !pid.trim().is_empty() && !path.trim().is_empty() {
                    roots_by_pid
                        .entry(pid.trim().to_owned())
                        .or_default()
                        .push(path.trim().to_owned());
                }
            }
        }
        let name_col = if pcols.contains("name") { "name" } else { "''" };
        let sql = format!("SELECT id, {name_col} FROM projects ORDER BY position, id");
        if let Ok(mut stmt) = conn.prepare(&sql)
            && let Ok(mut rows) = stmt.query([])
        {
            while let Ok(Some(row)) = rows.next() {
                let pid: String = row.get(0).unwrap_or_default();
                let name: String = row.get(1).unwrap_or_default();
                let pid_trim = pid.trim();
                if !pid_trim.is_empty() {
                    let roots = roots_by_pid.remove(pid_trim).unwrap_or_default();
                    projects.push(CodexProjectRow {
                        id: pid_trim.to_owned(),
                        name: name.trim().to_owned(),
                        roots,
                    });
                }
            }
        }
    }

    (threads, projects)
}

fn read_codex_global_state_roots(codex_home_dir: &Path) -> Vec<String> {
    let gs_path = codex_home_dir.join(".codex-global-state.json");
    let Ok(text) = fs::read_to_string(&gs_path) else {
        return Vec::new();
    };
    let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for key in ["electron-saved-workspace-roots", "active-workspace-roots"] {
        if let Some(arr) = obj.get(key).and_then(Value::as_array) {
            for v in arr {
                if let Some(s) = v.as_str() {
                    push_unique_str(&mut out, &mut seen, s);
                }
            }
        }
    }
    out
}

fn read_codex_legacy_projects(codex_home_dir: &Path) -> Vec<(String, Vec<String>)> {
    let legacy_path = codex_home_dir.join("projects.json");
    let Ok(text) = fs::read_to_string(&legacy_path) else {
        return Vec::new();
    };
    let Ok(val) = serde_json::from_str::<Value>(&text) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    match val {
        Value::Object(map) => {
            if let Some(arr) = map.get("projects").and_then(Value::as_array) {
                for item in arr {
                    if let Some(obj) = item.as_object()
                        && let Some(id) = obj.get("id").and_then(Value::as_str)
                    {
                        let roots = extract_agy_project_folder_uris(item);
                        out.push((id.trim().to_owned(), roots));
                    }
                }
            } else {
                for (id, item) in map {
                    let roots = extract_agy_project_folder_uris(&item);
                    out.push((id, roots));
                }
            }
        }
        Value::Array(arr) => {
            for item in arr {
                if let Some(obj) = item.as_object()
                    && let Some(id) = obj.get("id").and_then(Value::as_str)
                {
                    let roots = extract_agy_project_folder_uris(&item);
                    out.push((id.trim().to_owned(), roots));
                }
            }
        }
        _ => {}
    }
    out
}

/// Load a unified read-only snapshot of Antigravity and Codex catalog tables.
#[must_use]
pub fn load_preloaded_host_tables(paths: &HostEnvironmentPaths) -> PreloadedHostTables {
    let mut out = PreloadedHostTables {
        agy_projects: read_agy_projects_from_dirs(paths),
        codex_global_state_roots: read_codex_global_state_roots(&paths.codex_home_dir),
        codex_legacy_projects: read_codex_legacy_projects(&paths.codex_home_dir),
        ..PreloadedHostTables::default()
    };

    let summaries_db = paths.agy_data_dir.join("conversation_summaries.db");
    if summaries_db.is_file()
        && let Ok(conn) = open_readonly_sqlite(&summaries_db)
    {
        out.agy_summaries = read_agy_summaries_table(&conn);
    }

    let state_db = paths.codex_home_dir.join("state_5.sqlite");
    if state_db.is_file()
        && let Ok(conn) = open_readonly_sqlite(&state_db)
    {
        let (threads, projects) = read_codex_state_tables(&conn);
        out.codex_threads = threads;
        out.codex_projects = projects;
    }

    out
}

/// Find a registered non-forbidden Antigravity `project_id` matching `workspace` strictly read-only.
#[must_use]
pub fn find_readonly_agy_project_id(
    paths: &HostEnvironmentPaths,
    ws_path: &WorkspacePath,
    ws_uri: &WorkspaceRootUri,
    preloaded: Option<&PreloadedHostTables>,
) -> Option<AgyProjectId> {
    let owned_projects;
    let projects = match preloaded {
        Some(p) => &p.agy_projects,
        None => {
            owned_projects = read_agy_projects_from_dirs(paths);
            &owned_projects
        }
    };
    for (pid, _, uris) in projects {
        if !FORBIDDEN_AGY_PROJECT_IDS.contains(&pid.as_str())
            && uris_match_workspace(uris, ws_path, ws_uri)
        {
            return Some(pid.clone());
        }
    }
    None
}

/// Discover Antigravity subagent sidecar `(cascade_id, conv_db_path)` entries under `agy_data_dir`.
#[must_use]
pub fn discover_agy_subagent_sidecars(
    agy_data_dir: &Path,
    cascade_id: Option<&CascadeId>,
    preloaded: Option<&PreloadedHostTables>,
) -> Vec<(CascadeId, PathBuf)> {
    let mut found: BTreeMap<String, PathBuf> = BTreeMap::new();

    if let Some(p) = preloaded {
        for row in p.agy_summaries.values() {
            let is_sub = row.nesting_depth > 0 || !row.parent_conversation_id.trim().is_empty();
            if !is_sub {
                continue;
            }
            if let Some(target_cid) = cascade_id
                && row.parent_conversation_id.trim() != target_cid.as_str()
            {
                continue;
            }
            if let Ok(sub_cid) = CascadeId::parse(&row.cascade_id)
                && let Ok(dbp) = validated_child_path(
                    agy_data_dir,
                    &["conversations", &format!("{}.db", sub_cid.as_str())],
                    "cascade_id",
                )
            {
                found.insert(sub_cid.as_str().to_owned(), dbp);
            }
        }
    }

    let conv_dir = agy_data_dir.join("conversations");
    if let Ok(rd) = fs::read_dir(&conv_dir) {
        let mut db_paths: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file() && p.extension().and_then(|e| e.to_str()) == Some("db"))
            .collect();
        db_paths.sort();
        for dbp in db_paths {
            let Some(stem) = dbp.file_stem().and_then(|s| s.to_str()) else {
                continue;
            };
            let Ok(sub_cid) = CascadeId::parse(stem) else {
                continue;
            };
            if found.contains_key(sub_cid.as_str()) {
                continue;
            }
            if let Ok(conn) = open_readonly_sqlite(&dbp)
                && let Ok(blob) = conn.query_row(
                    "SELECT data FROM trajectory_metadata_blob WHERE id = 'main' LIMIT 1",
                    [],
                    |r| r.get::<_, Vec<u8>>(0),
                )
                && let Ok(meta) = decode_trajectory_metadata_proto(&blob)
            {
                let is_sub =
                    meta.nesting_depth > 0 || !meta.parent_conversation_id.trim().is_empty();
                let matches_parent = match cascade_id {
                    Some(target) => {
                        meta.parent_conversation_id.trim() == target.as_str()
                            || meta.root_conversation_id.trim() == target.as_str()
                    }
                    None => true,
                };
                if is_sub && matches_parent {
                    found.insert(sub_cid.as_str().to_owned(), dbp);
                }
            }
        }
    }

    found
        .into_iter()
        .filter_map(|(cid_str, p)| CascadeId::parse(&cid_str).ok().map(|c| (c, p)))
        .collect()
}

/// Discover Antigravity subagent sidecar `CascadeId`s.
#[must_use]
pub fn discover_agy_subagent_sidecar_ids(
    agy_data_dir: &Path,
    cascade_id: Option<&CascadeId>,
    preloaded: Option<&PreloadedHostTables>,
) -> HashSet<CascadeId> {
    discover_agy_subagent_sidecars(agy_data_dir, cascade_id, preloaded)
        .into_iter()
        .map(|(cid, _)| cid)
        .collect()
}

/// Scan `conversation_summaries.db` returning `(matching_rows, foreign_cascade_ids)`.
#[must_use]
pub fn scan_agy_summaries_with_foreign_ids(
    paths: &HostEnvironmentPaths,
    ws_path: &WorkspacePath,
    ws_uri: &WorkspaceRootUri,
    preloaded: Option<&PreloadedHostTables>,
) -> (Vec<AgySummaryRow>, HashSet<String>) {
    let owned;
    let summaries = match preloaded {
        Some(p) => &p.agy_summaries,
        None => {
            let db = paths.agy_data_dir.join("conversation_summaries.db");
            owned = if db.is_file() {
                open_readonly_sqlite(&db)
                    .map(|c| read_agy_summaries_table(&c))
                    .unwrap_or_default()
            } else {
                HashMap::new()
            };
            &owned
        }
    };

    let mut matched = Vec::new();
    let mut foreign = HashSet::new();
    let mut keys: Vec<&String> = summaries.keys().collect();
    keys.sort();
    for k in keys {
        let row = &summaries[k];
        if row.parsed_uris.is_empty() {
            continue;
        }
        if uris_match_workspace(&row.parsed_uris, ws_path, ws_uri) {
            matched.push(row.clone());
        } else {
            foreign.insert(row.cascade_id.clone());
        }
    }
    (matched, foreign)
}

fn read_conv_db_metadata(
    conv_db: &Path,
) -> (
    u32,
    Vec<u8>,
    Option<cas_codecs::DecodedTrajectoryMetadata>,
    String,
) {
    let Ok(conn) = open_readonly_sqlite(conv_db) else {
        return (0, Vec::new(), None, String::new());
    };
    let step_count = conn
        .query_row("SELECT COUNT(*) FROM steps", [], |r| r.get::<_, i64>(0))
        .unwrap_or(0)
        .max(0) as u32;
    let blob: Vec<u8> = conn
        .query_row(
            "SELECT data FROM trajectory_metadata_blob WHERE id = 'main' LIMIT 1",
            [],
            |r| r.get::<_, Vec<u8>>(0),
        )
        .unwrap_or_default();
    let decoded_meta = if blob.is_empty() {
        None
    } else {
        decode_trajectory_metadata_proto(&blob).ok()
    };
    let source_val: i64 = conn
        .query_row("SELECT source FROM trajectory_meta LIMIT 1", [], |r| {
            r.get(0)
        })
        .unwrap_or(0);
    let source_name = cas_core::AgyGuiSafeSource::from_proto_enum(source_val.max(0) as u32)
        .map_or_else(String::new, |s| s.summary_column_name().to_owned());
    (step_count, blob, decoded_meta, source_name)
}

fn read_annotation_title(paths: &HostEnvironmentPaths, cid: &CascadeId) -> Option<String> {
    let ann_path = validated_child_path(
        &paths.agy_data_dir,
        &["annotations", &format!("{}.pbtxt", cid.as_str())],
        "cascade_id",
    )
    .ok()?;
    let text = fs::read_to_string(ann_path).ok()?;
    parse_annotation_pbtxt_title(&text).filter(|s| !s.trim().is_empty())
}

/// Enrich a summary row with live read-only step count and trajectory metadata from `conversations/<cid>.db`.
#[must_use]
pub fn enrich_agy_session_from_conv_db(
    paths: &HostEnvironmentPaths,
    row: &AgySummaryRow,
) -> AgyCatalogEntry {
    let cid = CascadeId::parse(&row.cascade_id).unwrap_or_else(|_| {
        CascadeId::parse(cas_core::NULL_CASCADE_ID_STR).unwrap_or_else(|_| unreachable!())
    });
    let conv_db = validated_child_path(
        &paths.agy_data_dir,
        &["conversations", &format!("{}.db", cid.as_str())],
        "cascade_id",
    )
    .ok();
    let conv_exists = conv_db.as_ref().is_some_and(|p| p.is_file());
    let (db_steps, blob, meta_opt, db_source) = conv_db
        .as_deref()
        .filter(|p| p.is_file())
        .map_or((0, Vec::new(), None, String::new()), read_conv_db_metadata);

    let step_count = if conv_exists && db_steps > 0 {
        db_steps.max(row.step_count)
    } else {
        row.step_count
    };
    let mtime_epoch = conv_db
        .as_deref()
        .and_then(|p| fs::metadata(p).ok())
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map_or(0.0, |d| d.as_secs_f64());

    let (sort_epoch, updated_at_iso) = parse_ts_epoch_iso(
        (!row.last_modified_time.is_empty()).then_some(row.last_modified_time.as_str()),
        (mtime_epoch > 0.0).then_some(mtime_epoch),
        mtime_epoch,
    );

    let ann_title = read_annotation_title(paths, &cid);
    let mut title = ann_title.unwrap_or_else(|| row.title.clone());
    if title.trim().is_empty() {
        title.clone_from(&row.preview);
    }
    if title.trim().is_empty() {
        title = format!("Session {}", &cid.as_str()[..cid.as_str().len().min(8)]);
    }

    let mut parent_cid = CascadeId::parse(row.parent_conversation_id.trim()).ok();
    let mut nesting_depth = row.nesting_depth;
    let mut agent_type_name = row.agent_type_name.clone();
    let mut agent_role = row.agent_role.clone();
    let mut uris = row.parsed_uris.clone();
    let mut project_id = row.project_id.clone();

    if let Some(meta) = meta_opt {
        if parent_cid.is_none() && !meta.parent_conversation_id.trim().is_empty() {
            parent_cid = CascadeId::parse(meta.parent_conversation_id.trim()).ok();
        }
        if nesting_depth == 0 {
            nesting_depth = meta.nesting_depth;
        }
        if agent_type_name.is_empty() {
            agent_type_name = meta.agent_type_name;
        }
        if agent_role.is_empty() {
            agent_role = meta.agent_role;
        }
        if uris.is_empty() && !meta.workspace_uris.is_empty() {
            uris = meta.workspace_uris;
        }
        if project_id.is_empty() && !meta.project_id.trim().is_empty() {
            project_id = meta.project_id;
        }
    }
    let source_name = if row.source.is_empty() {
        db_source
    } else {
        row.source.clone()
    };
    let is_subagent = nesting_depth > 0 || parent_cid.is_some();

    AgyCatalogEntry {
        cascade_id: cid,
        title: normalize_single_line_query(&title, 120),
        preview: row.preview.clone(),
        step_count,
        updated_at_iso,
        sort_epoch,
        workspace_uris: uris,
        project_id,
        source_name,
        parent_cascade_id: parent_cid,
        nesting_depth,
        agent_type_name,
        agent_role,
        is_subagent,
        conv_db_exists: conv_exists,
        trajectory_metadata_blob: blob,
    }
}

/// Discover all Antigravity sessions belonging to `(ws_path, ws_uri)` (or all workspaces if `ws_filter` is `None`).
#[must_use]
pub fn discover_agy_entries(
    paths: &HostEnvironmentPaths,
    ws_filter: Option<(&WorkspacePath, &WorkspaceRootUri)>,
    preloaded: Option<&PreloadedHostTables>,
) -> Vec<AgyCatalogEntry> {
    let owned_preloaded;
    let tbls = match preloaded {
        Some(p) => p,
        None => {
            owned_preloaded = load_preloaded_host_tables(paths);
            &owned_preloaded
        }
    };

    let mut entries_by_id: BTreeMap<String, AgyCatalogEntry> = BTreeMap::new();
    let mut foreign_ids: HashSet<String> = HashSet::new();

    if let Some((ws_path, ws_uri)) = ws_filter {
        let (matched, foreign) =
            scan_agy_summaries_with_foreign_ids(paths, ws_path, ws_uri, Some(tbls));
        foreign_ids = foreign;
        for row in matched {
            let entry = enrich_agy_session_from_conv_db(paths, &row);
            entries_by_id.insert(entry.cascade_id.as_str().to_owned(), entry);
        }
    } else {
        for row in tbls.agy_summaries.values() {
            let entry = enrich_agy_session_from_conv_db(paths, row);
            entries_by_id.insert(entry.cascade_id.as_str().to_owned(), entry);
        }
    }

    // Also inspect `conversations/*.db` for sessions not indexed in `conversation_summaries.db`.
    let conv_dir = paths.agy_data_dir.join("conversations");
    if let Ok(rd) = fs::read_dir(&conv_dir) {
        let mut db_paths: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file() && p.extension().and_then(|e| e.to_str()) == Some("db"))
            .collect();
        db_paths.sort();
        for dbp in db_paths {
            let Some(stem) = dbp.file_stem().and_then(|s| s.to_str()) else {
                continue;
            };
            if entries_by_id.contains_key(stem) || foreign_ids.contains(stem) {
                continue;
            }
            if validate_session_id(stem, "cascade_id").is_err() {
                continue;
            }
            let (step_count, blob, meta_opt, source_name) = read_conv_db_metadata(&dbp);
            let Some(meta) = meta_opt else {
                continue;
            };
            if let Some((ws_path, ws_uri)) = ws_filter
                && !uris_match_workspace(&meta.workspace_uris, ws_path, ws_uri)
            {
                continue;
            }
            let row = AgySummaryRow {
                cascade_id: stem.to_owned(),
                step_count,
                workspace_uris: meta.workspace_uris.join(","),
                parsed_uris: meta.workspace_uris,
                source: source_name,
                project_id: meta.project_id,
                parent_conversation_id: meta.parent_conversation_id,
                nesting_depth: meta.nesting_depth,
                agent_type_name: meta.agent_type_name,
                agent_role: meta.agent_role,
                raw_summary: None,
                created_at_iso: meta.created_at_iso,
                ..AgySummaryRow::default()
            };
            let mut entry = enrich_agy_session_from_conv_db(paths, &row);
            if entry.trajectory_metadata_blob.is_empty() {
                entry.trajectory_metadata_blob = blob;
            }
            entries_by_id.insert(entry.cascade_id.as_str().to_owned(), entry);
        }
    }

    let mut entries: Vec<AgyCatalogEntry> = entries_by_id.into_values().collect();
    entries.sort_by(|a, b| {
        b.sort_epoch
            .partial_cmp(&a.sort_epoch)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.cascade_id.cmp(&b.cascade_id))
    });
    entries
}

fn read_rollout_session_meta(rollout_path: &Path) -> Option<(String, String, String, String)> {
    let lines = read_codex_rollout_lines(rollout_path).ok()?;
    let mut tid = String::new();
    let mut cwd = String::new();
    let mut title = String::new();
    let mut first_ts = String::new();

    for line in &lines {
        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&line.raw_json) else {
            continue;
        };
        if first_ts.is_empty()
            && let Some(ts) = obj.get("timestamp").and_then(Value::as_str)
        {
            first_ts = ts.to_owned();
        }
        let rtype = obj.get("type").and_then(Value::as_str).unwrap_or("");
        if rtype == "session_meta"
            && let Some(pl) = obj.get("payload").and_then(Value::as_object)
        {
            if let Some(id) = pl.get("id").and_then(Value::as_str) {
                tid = id.trim().to_owned();
            }
            if let Some(c) = pl.get("cwd").and_then(Value::as_str) {
                cwd = c.trim().to_owned();
            }
        } else if rtype == "event_msg"
            && title.is_empty()
            && let Some(pl) = obj.get("payload").and_then(Value::as_object)
            && pl.get("type").and_then(Value::as_str) == Some("user_message")
            && let Some(msg) = pl.get("message").and_then(Value::as_str)
        {
            title = normalize_single_line_query(msg, 120);
        }
        if !tid.is_empty() && !cwd.is_empty() && !title.is_empty() {
            break;
        }
    }
    if tid.is_empty() {
        None
    } else {
        Some((tid, cwd, title, first_ts))
    }
}

/// Count user turns in a Codex `.jsonl` / `.jsonl.zst` rollout matching Python `count_codex_rollout_user_turns`.
#[must_use]
pub fn count_codex_rollout_user_turns(
    rollout_path: &Path,
    _ws_path: Option<&WorkspacePath>,
    _ws_uri: Option<&WorkspaceRootUri>,
) -> usize {
    let Ok(lines) = read_codex_rollout_lines(rollout_path) else {
        return 1;
    };
    let count = lines
        .iter()
        .filter(|row| {
            row.raw_json.contains("\"role\":\"user\"")
                || row.raw_json.contains("\"role\": \"user\"")
        })
        .count();
    count.max(1)
}

fn walk_codex_session_rollouts(sessions_root: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let mut stack = vec![sessions_root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(rd) = fs::read_dir(&dir) else {
            continue;
        };
        let mut children: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
        children.sort();
        for p in children {
            if p.is_dir() {
                stack.push(p);
            } else if p.is_file() {
                let fname = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if fname.starts_with("rollout-")
                    && (fname.ends_with(".jsonl") || fname.ends_with(".jsonl.zst"))
                {
                    out.push(p);
                }
            }
        }
    }
    out.sort();
    out
}

fn find_codex_rollout_path_for_id(
    paths: &HostEnvironmentPaths,
    thread_id: &str,
    hint_path: &str,
) -> Option<PathBuf> {
    if !hint_path.trim().is_empty() {
        let cand = PathBuf::from(hint_path.trim());
        if cand.is_file() {
            return Some(cand);
        }
    }
    let needle = format!("-{thread_id}.jsonl");
    let needle_zst = format!("-{thread_id}.jsonl.zst");
    for p in walk_codex_session_rollouts(&paths.codex_home_dir.join("sessions")) {
        let fname = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if fname.ends_with(&needle) || fname.ends_with(&needle_zst) {
            return Some(p);
        }
    }
    None
}

/// Discover Codex sessions matching `ws_filter` (or all active workspaces if `None`).
#[must_use]
pub fn discover_codex_entries(
    paths: &HostEnvironmentPaths,
    ws_filter: Option<(&WorkspacePath, &WorkspaceRootUri)>,
    preloaded: Option<&PreloadedHostTables>,
) -> Vec<CodexCatalogEntry> {
    let owned_preloaded;
    let tbls = match preloaded {
        Some(p) => p,
        None => {
            owned_preloaded = load_preloaded_host_tables(paths);
            &owned_preloaded
        }
    };

    let matching_proj_ids: HashSet<String> = if let Some((ws_path, ws_uri)) = ws_filter {
        tbls.codex_projects
            .iter()
            .filter(|proj| uris_match_workspace(&proj.roots, ws_path, ws_uri))
            .map(|proj| proj.id.clone())
            .collect()
    } else {
        HashSet::new()
    };

    let mut by_id: BTreeMap<String, CodexCatalogEntry> = BTreeMap::new();
    let mut keys: Vec<&String> = tbls.codex_threads.keys().collect();
    keys.sort();

    for k in keys {
        let row = &tbls.codex_threads[k];
        if row.archived != 0 {
            continue;
        }
        if let Some((ws_path, ws_uri)) = ws_filter {
            let cwd_matches = !row.cwd.trim().is_empty()
                && uris_match_workspace(std::slice::from_ref(&row.cwd), ws_path, ws_uri);
            let proj_matches = !row.project_id.trim().is_empty()
                && matching_proj_ids.contains(row.project_id.trim());
            if !cwd_matches && !proj_matches {
                continue;
            }
        }
        let Ok(tid) = CodexThreadId::parse(&row.id) else {
            continue;
        };
        let rollout_path = find_codex_rollout_path_for_id(paths, tid.as_str(), &row.rollout_path);
        let fallback_epoch = row
            .updated_at_ms
            .map(|ms| ms as f64 / 1000.0)
            .filter(|e| *e > 0.0)
            .unwrap_or(row.updated_at.max(0) as f64);
        let file_epoch = rollout_path
            .as_deref()
            .and_then(|p| fs::metadata(p).ok())
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(fallback_epoch, |d| d.as_secs_f64().max(fallback_epoch));
        let (sort_epoch, updated_at_iso) = parse_ts_epoch_iso(
            None,
            (fallback_epoch > 0.0).then_some(fallback_epoch),
            file_epoch,
        );

        let raw_title = [
            row.name.as_str(),
            row.title.as_str(),
            row.first_user_message.as_str(),
        ]
        .into_iter()
        .map(str::trim)
        .find(|s| !s.is_empty())
        .unwrap_or("");
        let title = if raw_title.is_empty() {
            format!("Codex {}", &tid.as_str()[..tid.as_str().len().min(8)])
        } else {
            normalize_single_line_query(raw_title, 120)
        };

        let parsed_src =
            parse_codex_thread_source_lineage(&row.source, &row.agent_nickname, &row.agent_role);
        let depth = compute_codex_nesting_depth(tid.as_str(), &tbls.codex_threads);
        let eff_depth = depth.max(parsed_src.depth);
        let is_subagent =
            parsed_src.is_subagent || eff_depth > 0 || parsed_src.parent_thread_id.is_some();

        let turn_count = rollout_path.as_deref().map_or(0, |rp| {
            count_codex_rollout_user_turns(rp, ws_filter.map(|(p, _)| p), ws_filter.map(|(_, u)| u))
        });

        by_id.insert(
            tid.as_str().to_owned(),
            CodexCatalogEntry {
                thread_id: tid,
                rollout_path,
                title,
                first_user_message: row.first_user_message.clone(),
                cwd: row.cwd.clone(),
                project_id: row.project_id.clone(),
                updated_at_iso,
                sort_epoch,
                turn_count,
                parent_thread_id: parsed_src.parent_thread_id,
                nesting_depth: eff_depth,
                agent_type_name: parsed_src.agent_type_name,
                agent_role: parsed_src.agent_role,
                is_subagent,
            },
        );
    }

    // Scan filesystem rollouts not yet listed in `state_5.sqlite` (never archived_sessions).
    for rp in walk_codex_session_rollouts(&paths.codex_home_dir.join("sessions")) {
        let Some((tid_str, cwd, first_title, first_ts)) = read_rollout_session_meta(&rp) else {
            continue;
        };
        if by_id.contains_key(&tid_str) {
            continue;
        }
        // Skip explicitly archived threads in state_5.sqlite.
        if tbls
            .codex_threads
            .get(&tid_str)
            .is_some_and(|r| r.archived != 0)
        {
            continue;
        }
        if let Some((ws_path, ws_uri)) = ws_filter
            && !uris_match_workspace(std::slice::from_ref(&cwd), ws_path, ws_uri)
        {
            continue;
        }
        let Ok(tid) = CodexThreadId::parse(&tid_str) else {
            continue;
        };
        let file_epoch = fs::metadata(&rp)
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0.0, |d| d.as_secs_f64());
        let (sort_epoch, updated_at_iso) = parse_ts_epoch_iso(
            (!first_ts.is_empty()).then_some(first_ts.as_str()),
            None,
            file_epoch,
        );
        let turn_count = count_codex_rollout_user_turns(
            &rp,
            ws_filter.map(|(p, _)| p),
            ws_filter.map(|(_, u)| u),
        );
        let title = if first_title.is_empty() {
            format!("Codex {}", &tid.as_str()[..tid.as_str().len().min(8)])
        } else {
            first_title
        };
        by_id.insert(
            tid.as_str().to_owned(),
            CodexCatalogEntry {
                thread_id: tid,
                rollout_path: Some(rp),
                title,
                first_user_message: String::new(),
                cwd,
                project_id: String::new(),
                updated_at_iso,
                sort_epoch,
                turn_count,
                parent_thread_id: None,
                nesting_depth: 0,
                agent_type_name: String::new(),
                agent_role: String::new(),
                is_subagent: false,
            },
        );
    }

    let mut out: Vec<CodexCatalogEntry> = by_id.into_values().collect();
    out.sort_by(|a, b| {
        b.sort_epoch
            .partial_cmp(&a.sort_epoch)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.thread_id.cmp(&b.thread_id))
    });
    out
}

/// Probe whether `cascade_id` exists in Antigravity storage and which workspace owns it.
///
/// Returns `(exists_anywhere, belongs_to_target, foreign_uris)`.
#[must_use]
pub fn probe_agy_ownership(
    paths: &HostEnvironmentPaths,
    cascade_id: &CascadeId,
    ws_path: &WorkspacePath,
    ws_uri: &WorkspaceRootUri,
    preloaded: Option<&PreloadedHostTables>,
) -> (bool, bool, Vec<String>) {
    let owned;
    let tbls = match preloaded {
        Some(p) => p,
        None => {
            owned = load_preloaded_host_tables(paths);
            &owned
        }
    };
    let mut exists = false;
    let mut uris = Vec::new();
    if let Some(row) = tbls.agy_summaries.get(cascade_id.as_str()) {
        exists = true;
        uris.clone_from(&row.parsed_uris);
    }
    if let Ok(conv_db) = validated_child_path(
        &paths.agy_data_dir,
        &["conversations", &format!("{}.db", cascade_id.as_str())],
        "cascade_id",
    ) && conv_db.is_file()
    {
        exists = true;
        if uris.is_empty() {
            let (_, _, meta_opt, _) = read_conv_db_metadata(&conv_db);
            if let Some(meta) = meta_opt {
                uris = meta.workspace_uris;
            }
        }
    }
    if !exists
        && let Ok(brain_dir) = validated_child_path(
            &paths.agy_data_dir,
            &["brain", cascade_id.as_str()],
            "cascade_id",
        )
        && brain_dir.is_dir()
    {
        exists = true;
    }
    if !exists {
        return (false, false, Vec::new());
    }
    if uris.is_empty() {
        return (true, true, Vec::new());
    }
    let matches = uris_match_workspace(&uris, ws_path, ws_uri);
    (true, matches, uris)
}

/// Probe whether `thread_id` exists in Codex storage and which workspace owns it.
///
/// Returns `(exists_anywhere, belongs_to_target, foreign_cwds)`.
#[must_use]
pub fn probe_codex_ownership(
    paths: &HostEnvironmentPaths,
    thread_id: &CodexThreadId,
    ws_path: &WorkspacePath,
    ws_uri: &WorkspaceRootUri,
    preloaded: Option<&PreloadedHostTables>,
) -> (bool, bool, Vec<String>) {
    let owned;
    let tbls = match preloaded {
        Some(p) => p,
        None => {
            owned = load_preloaded_host_tables(paths);
            &owned
        }
    };
    let mut exists = false;
    let mut cwds = Vec::new();
    let mut proj_id = String::new();
    let mut hint_path = String::new();

    if let Some(row) = tbls.codex_threads.get(thread_id.as_str()) {
        exists = true;
        if !row.cwd.trim().is_empty() {
            cwds.push(row.cwd.trim().to_owned());
        }
        proj_id = row.project_id.trim().to_owned();
        hint_path = row.rollout_path.trim().to_owned();
    }
    if let Some(rp) = find_codex_rollout_path_for_id(paths, thread_id.as_str(), &hint_path) {
        exists = true;
        if cwds.is_empty()
            && let Some((_, meta_cwd, _, _)) = read_rollout_session_meta(&rp)
            && !meta_cwd.trim().is_empty()
        {
            cwds.push(meta_cwd);
        }
    }
    if !exists {
        return (false, false, Vec::new());
    }
    if !cwds.is_empty() && uris_match_workspace(&cwds, ws_path, ws_uri) {
        return (true, true, cwds);
    }
    if !proj_id.is_empty()
        && tbls
            .codex_projects
            .iter()
            .any(|p| p.id == proj_id && uris_match_workspace(&p.roots, ws_path, ws_uri))
    {
        return (true, true, cwds);
    }
    if cwds.is_empty() && proj_id.is_empty() {
        return (true, true, Vec::new());
    }
    (true, false, cwds)
}

/// Assert that `source_id` on `host` exists and belongs to `(ws_path, ws_uri)`.
///
/// # Errors
/// Returns [`HostError::FileNotFound`] if missing or [`HostError::InvalidArgument`] if owned by another workspace.
pub fn assert_source_belongs_to_binding(
    paths: &HostEnvironmentPaths,
    host: HostKind,
    source_id: &str,
    ws_path: &WorkspacePath,
    ws_uri: &WorkspaceRootUri,
    preloaded: Option<&PreloadedHostTables>,
) -> Result<(), HostError> {
    validate_session_id(source_id, "source_id")?;
    let (exists, matches, foreign) = match host {
        HostKind::Antigravity => {
            let cid = CascadeId::parse(source_id)?;
            probe_agy_ownership(paths, &cid, ws_path, ws_uri, preloaded)
        }
        HostKind::Codex => {
            let tid = CodexThreadId::parse(source_id)?;
            probe_codex_ownership(paths, &tid, ws_path, ws_uri, preloaded)
        }
    };
    if !exists {
        return Err(HostError::FileNotFound(format!(
            "Source session {source_id:?} was not found on {}.",
            host.as_str()
        )));
    }
    if !matches {
        return Err(HostError::InvalidArgument(format!(
            "Source session {source_id:?} on {} belongs to a different workspace ({}) rather than {}.",
            host.as_str(),
            foreign.join(", "),
            ws_path
        )));
    }
    Ok(())
}

/// Infer which host owns `source_id` (`Antigravity` vs `Codex`) without guessing.
///
/// # Errors
/// Returns [`HostError::FileNotFound`] if present on neither host, or [`HostError::InvalidArgument`] if ambiguous across both hosts.
pub fn infer_direction_from_source_id(
    paths: &HostEnvironmentPaths,
    source_id: &str,
    preloaded: Option<&PreloadedHostTables>,
) -> Result<HostKind, HostError> {
    validate_session_id(source_id, "source_id")?;
    let owned;
    let tbls = match preloaded {
        Some(p) => p,
        None => {
            owned = load_preloaded_host_tables(paths);
            &owned
        }
    };
    let on_agy = tbls.agy_summaries.contains_key(source_id)
        || validated_child_path(
            &paths.agy_data_dir,
            &["conversations", &format!("{source_id}.db")],
            "source_id",
        )
        .is_ok_and(|p| p.is_file())
        || validated_child_path(&paths.agy_data_dir, &["brain", source_id], "source_id")
            .is_ok_and(|p| p.is_dir());

    let on_codex = tbls.codex_threads.contains_key(source_id)
        || find_codex_rollout_path_for_id(paths, source_id, "").is_some();

    match (on_agy, on_codex) {
        (true, false) => Ok(HostKind::Antigravity),
        (false, true) => Ok(HostKind::Codex),
        (true, true) => Err(HostError::InvalidArgument(format!(
            "Session ID {source_id:?} exists on both Antigravity and Codex; pass --direction explicitly."
        ))),
        (false, false) => Err(HostError::FileNotFound(format!(
            "Session ID {source_id:?} was not found on either Antigravity or Codex."
        ))),
    }
}

/// Combine discovered Antigravity and Codex entries with persisted sync cursors for `status`.
#[must_use]
pub fn build_combined_lineage(
    agy_entries: &[AgyCatalogEntry],
    codex_entries: &[CodexCatalogEntry],
    cursors: &[(String, String)],
    include_subagents: bool,
) -> Vec<CombinedSessionDescriptor> {
    let mut agy_by_id: HashMap<&str, &AgyCatalogEntry> = HashMap::new();
    for a in agy_entries {
        agy_by_id.insert(a.cascade_id.as_str(), a);
    }
    let mut codex_by_id: HashMap<&str, &CodexCatalogEntry> = HashMap::new();
    for c in codex_entries {
        codex_by_id.insert(c.thread_id.as_str(), c);
    }

    let mut paired_agy = HashSet::new();
    let mut paired_codex = HashSet::new();
    let mut out = Vec::new();

    for (cid_str, tid_str) in cursors {
        let agy_opt = agy_by_id.get(cid_str.as_str()).copied();
        let codex_opt = codex_by_id.get(tid_str.as_str()).copied();
        if agy_opt.is_none() && codex_opt.is_none() {
            continue;
        }
        paired_agy.insert(cid_str.as_str());
        paired_codex.insert(tid_str.as_str());

        let is_sub =
            agy_opt.is_some_and(|a| a.is_subagent) || codex_opt.is_some_and(|c| c.is_subagent);
        if !include_subagents && is_sub {
            continue;
        }
        let title = agy_opt
            .map(|a| a.title.clone())
            .filter(|t| !t.is_empty())
            .or_else(|| codex_opt.map(|c| c.title.clone()))
            .unwrap_or_default();
        out.push(CombinedSessionDescriptor {
            cascade_id: Some(cid_str.clone()),
            codex_thread_id: Some(tid_str.clone()),
            title,
            agy_step_count: agy_opt.map_or(0, |a| a.step_count),
            codex_turn_count: codex_opt.map_or(0, |c| c.turn_count),
            parent_cascade_id: agy_opt
                .and_then(|a| a.parent_cascade_id.as_ref())
                .map(|c| c.as_str().to_owned()),
            parent_codex_thread_id: codex_opt
                .and_then(|c| c.parent_thread_id.as_ref())
                .map(|t| t.as_str().to_owned()),
            is_subagent: is_sub,
            nesting_depth: agy_opt
                .map_or(0, |a| a.nesting_depth)
                .max(codex_opt.map_or(0, |c| c.nesting_depth)),
            agent_type_name: agy_opt
                .map(|a| a.agent_type_name.clone())
                .filter(|s| !s.is_empty())
                .or_else(|| codex_opt.map(|c| c.agent_type_name.clone()))
                .unwrap_or_default(),
            agent_role: agy_opt
                .map(|a| a.agent_role.clone())
                .filter(|s| !s.is_empty())
                .or_else(|| codex_opt.map(|c| c.agent_role.clone()))
                .unwrap_or_default(),
        });
    }

    for a in agy_entries {
        if paired_agy.contains(a.cascade_id.as_str()) {
            continue;
        }
        if !include_subagents && a.is_subagent {
            continue;
        }
        out.push(CombinedSessionDescriptor {
            cascade_id: Some(a.cascade_id.as_str().to_owned()),
            codex_thread_id: None,
            title: a.title.clone(),
            agy_step_count: a.step_count,
            codex_turn_count: 0,
            parent_cascade_id: a.parent_cascade_id.as_ref().map(|c| c.as_str().to_owned()),
            parent_codex_thread_id: None,
            is_subagent: a.is_subagent,
            nesting_depth: a.nesting_depth,
            agent_type_name: a.agent_type_name.clone(),
            agent_role: a.agent_role.clone(),
        });
    }

    for c in codex_entries {
        if paired_codex.contains(c.thread_id.as_str()) {
            continue;
        }
        if !include_subagents && c.is_subagent {
            continue;
        }
        out.push(CombinedSessionDescriptor {
            cascade_id: None,
            codex_thread_id: Some(c.thread_id.as_str().to_owned()),
            title: c.title.clone(),
            agy_step_count: 0,
            codex_turn_count: c.turn_count,
            parent_cascade_id: None,
            parent_codex_thread_id: c.parent_thread_id.as_ref().map(|t| t.as_str().to_owned()),
            is_subagent: c.is_subagent,
            nesting_depth: c.nesting_depth,
            agent_type_name: c.agent_type_name.clone(),
            agent_role: c.agent_role.clone(),
        });
    }

    let _ = SubagentLineage::default();
    out
}
