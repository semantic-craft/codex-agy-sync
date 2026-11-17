//! Fast bounded read-only feed snapshot for the Antigravity <-> Codex menubar app.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use cas_codecs::{
    decode_codex_rollout, decode_trajectory_metadata_proto, read_codex_rollout_lines,
};
use cas_core::{
    AgyDaemonEndpoint, AgyProjectId, CanonicalEvent, CascadeId, CodexDaemonEndpoint,
    CodexLegacyProjectId, CodexProjectId, CodexThreadId, FORBIDDEN_AGY_PROJECT_IDS, HostKind,
    HostLockSnapshot, SyncReceipt, TrajectoryLedger, TransportMode, WorkspacePath,
    WorkspaceRootUri, ledger_seed_fingerprint, normalize_single_line_query,
    raw_spec_to_candidate_path, rescue_interrupted_ledger, validate_session_id,
};
use serde::Serialize;
use serde_json::Value;

use crate::catalog::{
    PreloadedHostTables, discover_agy_subagent_sidecar_ids, load_preloaded_host_tables,
    parse_codex_thread_source_lineage,
};
use crate::error::HostError;
use crate::locks::PresenceWalGuard;
use crate::paths::{
    HostEnvironmentPaths, candidate_home_dirs, deterministic_uuid, format_epoch_precise_iso,
    is_excluded_for_homes, parse_ts_epoch_iso, parse_ws_ident, validated_child_path,
};
use crate::registry::{ProjectRegistry, WorkspaceProjectBinding};
use crate::rpc_agy::{AgyHttpConn, fetch_agy_run_statuses_via_rpc, fetch_agy_trajectory_via_rpc};
use crate::sync_engine::{handoff_ledger, is_source_actively_running, load_all_receipts};
use crate::transport::DefaultHostTransport;

const HERO_WINDOW_SEC: f64 = 900.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MenubarCandidateState {
    InterruptedStopped,
    CompletedReady,
    PendingReconcile,
    AlreadyHanded,
    ActiveRunning,
}

impl MenubarCandidateState {
    #[must_use]
    pub const fn is_actionable(self) -> bool {
        matches!(
            self,
            Self::InterruptedStopped | Self::CompletedReady | Self::PendingReconcile
        )
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct MenubarSessionItem {
    pub host: String,
    pub target_host: String,
    pub direction: String,
    pub session_id: String,
    pub title: String,
    pub workspace_path: String,
    pub workspace_name: String,
    pub paired: bool,
    pub state: MenubarCandidateState,
    pub updated_at_iso: String,
    pub sort_epoch: f64,
    pub turn_count: usize,
    pub has_inflight_turn: bool,
    pub handed_target_id: Option<String>,
    pub pending_error: Option<String>,
    pub chain_depth: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct MenubarFeedSnapshot {
    pub generated_at_iso: String,
    pub agy_daemon_active: bool,
    pub codex_daemon_active: bool,
    pub hero: Option<MenubarSessionItem>,
    pub recent: Vec<MenubarSessionItem>,
}

#[derive(Debug, Clone)]
struct WorkspaceRecord {
    binding: WorkspaceProjectBinding,
    paired: bool,
}

#[derive(Debug, Clone)]
struct RawCand {
    host: &'static str,
    session_id: String,
    workspace: WorkspaceRecord,
    title: String,
    sort_epoch: f64,
    updated_at_iso: String,
}

#[derive(Default)]
struct ReceiptIdx {
    pending: HashMap<(String, String, String), SyncReceipt>,
    ready_sources: HashMap<(String, String, String), Vec<SyncReceipt>>,
    ready_targets: HashMap<(String, String, String), Vec<SyncReceipt>>,
}

fn norm_ws_cached<'a>(
    raw: &str,
    cache: &'a mut HashMap<String, Option<(WorkspacePath, WorkspaceRootUri)>>,
) -> Option<&'a (WorkspacePath, WorkspaceRootUri)> {
    if !cache.contains_key(raw) {
        let parsed = parse_ws_ident(raw).ok();
        cache.insert(raw.to_owned(), parsed);
    }
    cache.get(raw).and_then(Option::as_ref)
}

fn load_receipt_index(
    paths: &HostEnvironmentPaths,
    homes: &[PathBuf],
    norm_cache: &mut HashMap<String, Option<(WorkspacePath, WorkspaceRootUri)>>,
) -> ReceiptIdx {
    let mut idx = ReceiptIdx::default();
    let loaded = load_all_receipts(paths);
    let mut ready_by_source_map: HashMap<(String, String, String), Vec<SyncReceipt>> =
        HashMap::new();
    for (_, rec, _) in loaded {
        let Some((ws_path, _)) = norm_ws_cached(&rec.workspace, norm_cache).cloned() else {
            continue;
        };
        if is_excluded_for_homes(ws_path.as_path(), homes) {
            continue;
        }
        let ws_str = ws_path.to_string();
        if rec.status_str() == "pending" {
            idx.pending
                .entry((ws_str, rec.source_host.clone(), rec.source_id.clone()))
                .or_insert(rec);
        } else if rec.status_str() == "ready" {
            ready_by_source_map
                .entry((
                    ws_str.clone(),
                    rec.source_host.clone(),
                    rec.source_id.clone(),
                ))
                .or_default()
                .push(rec.clone());
            if let Some(tid) = rec.target_id() {
                idx.ready_targets
                    .entry((ws_str, rec.target_host.clone(), tid.to_owned()))
                    .or_default()
                    .push(rec);
            }
        }
    }
    for (k, mut v) in ready_by_source_map {
        v.sort_by(|a, b| b.created_at_iso.cmp(&a.created_at_iso));
        idx.ready_sources.insert(k, v);
    }
    idx
}

struct WorkspaceLookup {
    by_path: HashMap<String, WorkspaceRecord>,
    by_uri: HashMap<String, WorkspaceRecord>,
    restricted_paths: Option<HashSet<String>>,
}

impl WorkspaceLookup {
    fn resolve_raw(
        &self,
        raw: &str,
        homes: &[PathBuf],
        cache: &mut HashMap<String, Option<(WorkspacePath, WorkspaceRootUri)>>,
    ) -> Option<WorkspaceRecord> {
        let (ws_path, ws_uri) = norm_ws_cached(raw, cache)?.clone();
        if is_excluded_for_homes(ws_path.as_path(), homes) {
            return None;
        }
        let path_str = ws_path.to_string();
        if let Some(ref allowed) = self.restricted_paths
            && !allowed.contains(&path_str)
        {
            return None;
        }
        if let Some(rec) = self.by_path.get(&path_str) {
            return Some(rec.clone());
        }
        if let Some(rec) = self.by_uri.get(ws_uri.as_str()) {
            return Some(rec.clone());
        }
        // Fallback deterministic unpaired workspace record for sessions discovered dynamically.
        let agy_pid =
            AgyProjectId::parse(&deterministic_uuid("agy-project", ws_uri.as_str())).ok()?;
        let codex_pid =
            CodexProjectId::parse(&deterministic_uuid("codex-project", ws_uri.as_str())).ok()?;
        let codex_legacy = CodexLegacyProjectId::parse(&deterministic_uuid(
            "codex-legacy-project",
            ws_uri.as_str(),
        ))
        .ok()?;
        let ws_name = ws_path
            .as_path()
            .file_name()
            .and_then(|n| n.to_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("workspace")
            .to_owned();
        Some(WorkspaceRecord {
            binding: WorkspaceProjectBinding {
                workspace_path: path_str,
                workspace_uri: ws_uri.as_str().to_owned(),
                workspace_name: ws_name,
                agy_project_id: agy_pid,
                codex_project_id: codex_pid,
                codex_legacy_project_id: codex_legacy,
            },
            paired: false,
        })
    }
}

fn build_workspace_lookup(
    paths: &HostEnvironmentPaths,
    preloaded: &PreloadedHostTables,
    receipts: &ReceiptIdx,
    seed_workspaces: &[String],
    homes: &[PathBuf],
    norm_cache: &mut HashMap<String, Option<(WorkspacePath, WorkspaceRootUri)>>,
) -> Result<WorkspaceLookup, HostError> {
    let mut agy_by_uri: HashMap<String, AgyProjectId> = HashMap::new();
    for (pid, _, uris) in &preloaded.agy_projects {
        if FORBIDDEN_AGY_PROJECT_IDS.contains(&pid.as_str()) {
            continue;
        }
        for raw_u in uris {
            if let Some((_, wu)) = norm_ws_cached(raw_u, norm_cache) {
                agy_by_uri
                    .entry(wu.as_str().to_owned())
                    .or_insert_with(|| pid.clone());
            }
        }
    }
    for row in preloaded.agy_summaries.values() {
        let pid_str = row.project_id.trim();
        if FORBIDDEN_AGY_PROJECT_IDS.contains(&pid_str) {
            continue;
        }
        if let Ok(pid) = AgyProjectId::parse(pid_str) {
            for raw_u in &row.parsed_uris {
                if let Some((_, wu)) = norm_ws_cached(raw_u, norm_cache) {
                    agy_by_uri
                        .entry(wu.as_str().to_owned())
                        .or_insert_with(|| pid.clone());
                }
            }
        }
    }

    let mut codex_by_path: HashMap<String, CodexProjectId> = HashMap::new();
    for proj in &preloaded.codex_projects {
        if let Ok(cpid) = CodexProjectId::parse(&proj.id) {
            for r in &proj.roots {
                if let Some((wp, _)) = norm_ws_cached(r, norm_cache) {
                    codex_by_path
                        .entry(wp.to_string())
                        .or_insert_with(|| cpid.clone());
                }
            }
        }
    }

    let registry = ProjectRegistry::new(paths.clone());
    let mut candidate_raw_specs: Vec<String> = Vec::new();
    let mut restricted_paths: Option<HashSet<String>> = None;

    if !seed_workspaces.is_empty() {
        let mut allowed = HashSet::new();
        for sw in seed_workspaces {
            let (wp, _) = parse_ws_ident(sw)?;
            allowed.insert(wp.to_string());
            candidate_raw_specs.push(sw.clone());
        }
        restricted_paths = Some(allowed);
    } else {
        for (_, _, uris) in &preloaded.agy_projects {
            candidate_raw_specs.extend(uris.iter().cloned());
        }
        for proj in &preloaded.codex_projects {
            candidate_raw_specs.extend(proj.roots.iter().cloned());
        }
        for (ws_str, _, _) in receipts.pending.keys() {
            candidate_raw_specs.push(ws_str.clone());
        }
        for (ws_str, _, _) in receipts.ready_sources.keys() {
            candidate_raw_specs.push(ws_str.clone());
        }
        for row in preloaded.agy_summaries.values() {
            candidate_raw_specs.extend(row.parsed_uris.iter().cloned());
        }
        for row in preloaded.codex_threads.values() {
            if !row.cwd.trim().is_empty() {
                candidate_raw_specs.push(row.cwd.clone());
            }
        }
    }

    let mut by_path = HashMap::new();
    let mut by_uri = HashMap::new();

    for spec in candidate_raw_specs {
        let Some((wp, wu)) = norm_ws_cached(&spec, norm_cache).cloned() else {
            continue;
        };
        if is_excluded_for_homes(wp.as_path(), homes) {
            continue;
        }
        let path_str = wp.to_string();
        if let Some(ref allowed) = restricted_paths
            && !allowed.contains(&path_str)
        {
            continue;
        }
        if by_path.contains_key(&path_str) {
            continue;
        }
        let agy_opt = agy_by_uri.get(wu.as_str()).cloned();
        let codex_opt = codex_by_path.get(&path_str).cloned();
        let paired = agy_opt.is_some() && codex_opt.is_some();
        let binding = if let (Some(a), Some(c)) = (agy_opt, codex_opt) {
            let mut ro = registry.resolve_readonly_binding(&path_str)?;
            ro.agy_project_id = a;
            ro.codex_project_id = c;
            ro
        } else {
            registry.resolve_readonly_binding(&path_str)?
        };
        let rec = WorkspaceRecord { binding, paired };
        by_path.insert(path_str, rec.clone());
        by_uri.insert(wu.as_str().to_owned(), rec);
    }

    Ok(WorkspaceLookup {
        by_path,
        by_uri,
        restricted_paths,
    })
}

fn collect_agy_candidates(
    paths: &HostEnvironmentPaths,
    preloaded: &PreloadedHostTables,
    lookup: &WorkspaceLookup,
    pending_ids: &HashSet<String>,
    pool_size: usize,
    homes: &[PathBuf],
    norm_cache: &mut HashMap<String, Option<(WorkspacePath, WorkspaceRootUri)>>,
) -> Vec<RawCand> {
    let sidecars = discover_agy_subagent_sidecar_ids(&paths.agy_data_dir, None, Some(preloaded));
    let mut by_id: HashMap<String, RawCand> = HashMap::new();

    let mut keys: Vec<&String> = preloaded.agy_summaries.keys().collect();
    keys.sort();
    for cid_str in keys {
        let row = &preloaded.agy_summaries[cid_str];
        let Ok(cid) = CascadeId::parse(cid_str) else {
            continue;
        };
        if row.nesting_depth > 0
            || !row.parent_conversation_id.trim().is_empty()
            || sidecars.contains(&cid)
        {
            continue;
        }
        let conv_db = validated_child_path(
            &paths.agy_data_dir,
            &["conversations", &format!("{cid_str}.db")],
            "cascade_id",
        )
        .ok();
        if !conv_db.as_ref().is_some_and(|p| p.is_file()) {
            continue;
        }
        let ws_rec = row
            .parsed_uris
            .iter()
            .find_map(|u| lookup.resolve_raw(u, homes, norm_cache));
        let Some(ws_rec) = ws_rec else {
            continue;
        };

        let mtime_epoch = conv_db
            .as_deref()
            .and_then(|p| fs::metadata(p).ok())
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map_or(0.0, |d| d.as_secs_f64());
        let (sort_epoch, updated_at_iso) = parse_ts_epoch_iso(
            (!row.last_modified_time.is_empty()).then_some(row.last_modified_time.as_str()),
            (mtime_epoch > 0.0).then_some(mtime_epoch),
            mtime_epoch,
        );
        let raw_title = if !row.title.trim().is_empty() {
            row.title.trim()
        } else {
            row.preview.trim()
        };
        let title = if raw_title.is_empty() {
            String::new()
        } else {
            normalize_single_line_query(raw_title, 120)
        };
        by_id.insert(
            cid_str.clone(),
            RawCand {
                host: "antigravity",
                session_id: cid_str.clone(),
                workspace: ws_rec,
                title,
                sort_epoch,
                updated_at_iso,
            },
        );
    }

    // Also include `conversations/*.db` not present in `conversation_summaries.db`.
    let conv_dir = paths.agy_data_dir.join("conversations");
    if let Ok(rd) = fs::read_dir(&conv_dir) {
        let mut dbs: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file() && p.extension().and_then(|e| e.to_str()) == Some("db"))
            .collect();
        dbs.sort();
        for dbp in dbs {
            let Some(stem) = dbp.file_stem().and_then(|s| s.to_str()) else {
                continue;
            };
            if by_id.contains_key(stem) || validate_session_id(stem, "cascade_id").is_err() {
                continue;
            }
            let Ok(cid) = CascadeId::parse(stem) else {
                continue;
            };
            if sidecars.contains(&cid) {
                continue;
            }
            let Ok(conn) = crate::sqlite::open_readonly_sqlite(&dbp) else {
                continue;
            };
            let Ok(blob) = conn.query_row(
                "SELECT data FROM trajectory_metadata_blob WHERE id = 'main' LIMIT 1",
                [],
                |r| r.get::<_, Vec<u8>>(0),
            ) else {
                continue;
            };
            let Ok(meta) = decode_trajectory_metadata_proto(&blob) else {
                continue;
            };
            if meta.nesting_depth > 0 || !meta.parent_conversation_id.trim().is_empty() {
                continue;
            }
            let Some(ws_rec) = meta
                .workspace_uris
                .iter()
                .find_map(|u| lookup.resolve_raw(u, homes, norm_cache))
            else {
                continue;
            };
            let mtime_epoch = fs::metadata(&dbp)
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map_or(0.0, |d| d.as_secs_f64());
            let (sort_epoch, updated_at_iso) = parse_ts_epoch_iso(None, None, mtime_epoch);
            by_id.insert(
                stem.to_owned(),
                RawCand {
                    host: "antigravity",
                    session_id: stem.to_owned(),
                    workspace: ws_rec,
                    title: String::new(),
                    sort_epoch,
                    updated_at_iso,
                },
            );
        }
    }

    let mut out: Vec<RawCand> = by_id.into_values().collect();
    out.sort_by(|a, b| {
        b.sort_epoch
            .partial_cmp(&a.sort_epoch)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.session_id.cmp(&b.session_id))
    });
    if out.len() > pool_size {
        let mut kept: Vec<RawCand> = Vec::with_capacity(pool_size + pending_ids.len());
        for (i, c) in out.into_iter().enumerate() {
            if i < pool_size || pending_ids.contains(&c.session_id) {
                kept.push(c);
            }
        }
        kept
    } else {
        out
    }
}

fn collect_codex_candidates(
    paths: &HostEnvironmentPaths,
    preloaded: &PreloadedHostTables,
    lookup: &WorkspaceLookup,
    pending_ids: &HashSet<String>,
    pool_size: usize,
    homes: &[PathBuf],
    norm_cache: &mut HashMap<String, Option<(WorkspacePath, WorkspaceRootUri)>>,
) -> Vec<RawCand> {
    let mut by_id: HashMap<String, RawCand> = HashMap::new();
    let proj_roots_by_id: HashMap<&str, &Vec<String>> = preloaded
        .codex_projects
        .iter()
        .map(|p| (p.id.as_str(), &p.roots))
        .collect();

    for row in preloaded.codex_threads.values() {
        if row.archived != 0 {
            continue;
        }
        let lin =
            parse_codex_thread_source_lineage(&row.source, &row.agent_nickname, &row.agent_role);
        if lin.is_subagent || lin.parent_thread_id.is_some() || lin.depth > 0 {
            continue;
        }
        let ws_rec = (!row.cwd.trim().is_empty())
            .then(|| lookup.resolve_raw(&row.cwd, homes, norm_cache))
            .flatten()
            .or_else(|| {
                proj_roots_by_id
                    .get(row.project_id.trim())
                    .and_then(|roots| {
                        roots
                            .iter()
                            .find_map(|r| lookup.resolve_raw(r, homes, norm_cache))
                    })
            });
        let Some(ws_rec) = ws_rec else {
            continue;
        };

        let mut epoch = row
            .updated_at_ms
            .map(|ms| ms as f64 / 1000.0)
            .filter(|e| *e > 0.0)
            .unwrap_or(row.updated_at.max(0) as f64);
        if !row.rollout_path.trim().is_empty() {
            let rp = PathBuf::from(row.rollout_path.trim());
            let cand = if rp.is_file() {
                Some(rp)
            } else {
                let z = PathBuf::from(format!("{}.zst", row.rollout_path.trim()));
                z.is_file().then_some(z)
            };
            if let Some(p) = cand
                && let Ok(m) = fs::metadata(p)
                && let Ok(mod_t) = m.modified()
                && let Ok(dur) = mod_t.duration_since(UNIX_EPOCH)
            {
                epoch = epoch.max(dur.as_secs_f64());
            }
        }
        let (sort_epoch, updated_at_iso) =
            parse_ts_epoch_iso(None, (epoch > 0.0).then_some(epoch), epoch);
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
            String::new()
        } else {
            normalize_single_line_query(raw_title, 120)
        };
        by_id.insert(
            row.id.clone(),
            RawCand {
                host: "codex",
                session_id: row.id.clone(),
                workspace: ws_rec,
                title,
                sort_epoch,
                updated_at_iso,
            },
        );
    }

    // Scan orphan rollout files not listed in `state_5.sqlite`.
    let sessions_root = paths.codex_home_dir.join("sessions");
    if sessions_root.is_dir() {
        let mut stack = vec![sessions_root];
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
                    if !fname.starts_with("rollout-")
                        || (!fname.ends_with(".jsonl") && !fname.ends_with(".jsonl.zst"))
                    {
                        continue;
                    }
                    let Ok(lines) = read_codex_rollout_lines(&p) else {
                        continue;
                    };
                    let mut tid = String::new();
                    let mut cwd = String::new();
                    let mut title = String::new();
                    let mut first_ts = String::new();
                    for l in &lines {
                        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&l.raw_json)
                        else {
                            continue;
                        };
                        if first_ts.is_empty()
                            && let Some(ts) = obj.get("timestamp").and_then(Value::as_str)
                        {
                            first_ts = ts.to_owned();
                        }
                        match obj.get("type").and_then(Value::as_str).unwrap_or("") {
                            "session_meta" => {
                                if let Some(pl) = obj.get("payload").and_then(Value::as_object) {
                                    if let Some(id) = pl.get("id").and_then(Value::as_str) {
                                        tid = id.trim().to_owned();
                                    }
                                    if let Some(c) = pl.get("cwd").and_then(Value::as_str) {
                                        cwd = c.trim().to_owned();
                                    }
                                }
                            }
                            "event_msg" if title.is_empty() => {
                                if let Some(pl) = obj.get("payload").and_then(Value::as_object)
                                    && pl.get("type").and_then(Value::as_str)
                                        == Some("user_message")
                                    && let Some(msg) = pl.get("message").and_then(Value::as_str)
                                {
                                    title = normalize_single_line_query(msg, 120);
                                }
                            }
                            _ => {}
                        }
                        if !tid.is_empty() && !cwd.is_empty() && !title.is_empty() {
                            break;
                        }
                    }
                    if tid.is_empty()
                        || by_id.contains_key(&tid)
                        || preloaded.codex_threads.contains_key(&tid)
                        || validate_session_id(&tid, "codex_thread_id").is_err()
                    {
                        continue;
                    }
                    let Some(ws_rec) = lookup.resolve_raw(&cwd, homes, norm_cache) else {
                        continue;
                    };
                    let mtime = fs::metadata(&p)
                        .ok()
                        .and_then(|m| m.modified().ok())
                        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                        .map_or(0.0, |d| d.as_secs_f64());
                    let (sort_epoch, updated_at_iso) = parse_ts_epoch_iso(
                        (!first_ts.is_empty()).then_some(first_ts.as_str()),
                        None,
                        mtime,
                    );
                    by_id.insert(
                        tid.clone(),
                        RawCand {
                            host: "codex",
                            session_id: tid,
                            workspace: ws_rec,
                            title,
                            sort_epoch,
                            updated_at_iso,
                        },
                    );
                }
            }
        }
    }

    let mut out: Vec<RawCand> = by_id.into_values().collect();
    out.sort_by(|a, b| {
        b.sort_epoch
            .partial_cmp(&a.sort_epoch)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.session_id.cmp(&b.session_id))
    });
    if out.len() > pool_size {
        let mut kept: Vec<RawCand> = Vec::with_capacity(pool_size + pending_ids.len());
        for (i, c) in out.into_iter().enumerate() {
            if i < pool_size || pending_ids.contains(&c.session_id) {
                kept.push(c);
            }
        }
        kept
    } else {
        out
    }
}

fn classify_ledger_item(
    cand: &RawCand,
    ledger: &TrajectoryLedger,
    is_active: bool,
    receipts: &ReceiptIdx,
) -> Option<MenubarSessionItem> {
    let binding = &cand.workspace.binding;
    let ws_str = &binding.workspace_path;
    let key = (
        ws_str.clone(),
        cand.host.to_owned(),
        cand.session_id.clone(),
    );
    let title = if !cand.title.is_empty() {
        cand.title.clone()
    } else if !ledger.title.trim().is_empty() {
        ledger.title.trim().to_owned()
    } else {
        "Untitled Session".to_owned()
    };

    let mut handed_id: Option<String> = None;
    let mut pending_err: Option<String> = None;
    let mut chain_depth = 0i64;
    let inflight = ledger.has_inflight_turn;
    let mut turn_count = ledger.completed_turn_watermarks.len();
    let state: MenubarCandidateState;

    if let Some(pending_rec) = receipts.pending.get(&key) {
        state = MenubarCandidateState::PendingReconcile;
        pending_err = pending_rec.error().map(str::to_owned);
    } else if is_active {
        state = MenubarCandidateState::ActiveRunning;
    } else {
        let eff = if inflight {
            cas_core::rescue_interrupted_ledger(ledger).0
        } else {
            ledger.clone()
        };
        let wm = eff.latest_watermark()?;
        if !inflight && let Some(tgt_recs) = receipts.ready_targets.get(&key) {
            let hl = handoff_ledger(ledger, None);
            let seed_fp = ledger_seed_fingerprint(&hl);
            if tgt_recs.iter().any(|r| {
                r.target_seed_turn_count == Some(seed_fp.0)
                    && r.target_seed_text_digest.as_deref() == Some(seed_fp.1.as_str())
            }) {
                return None;
            }
        }
        turn_count = eff.completed_turn_watermarks.len();
        if let Some(matched) = receipts.ready_sources.get(&key).and_then(|recs| {
            recs.iter()
                .find(|r| r.source_digest == wm.boundary_digest.as_str())
        }) {
            state = MenubarCandidateState::AlreadyHanded;
            handed_id = matched.target_id().map(str::to_owned);
            chain_depth = matched.chain_depth;
        } else {
            state = if inflight {
                MenubarCandidateState::InterruptedStopped
            } else {
                MenubarCandidateState::CompletedReady
            };
        }
    }

    Some(MenubarSessionItem {
        host: cand.host.to_owned(),
        target_host: if cand.host == "antigravity" {
            "codex".to_owned()
        } else {
            "antigravity".to_owned()
        },
        direction: if cand.host == "antigravity" {
            "agy_to_codex".to_owned()
        } else {
            "codex_to_agy".to_owned()
        },
        session_id: cand.session_id.clone(),
        title,
        workspace_path: ws_str.clone(),
        workspace_name: binding.workspace_name.clone(),
        paired: cand.workspace.paired,
        state,
        updated_at_iso: cand.updated_at_iso.clone(),
        sort_epoch: cand.sort_epoch,
        turn_count,
        has_inflight_turn: inflight,
        handed_target_id: handed_id,
        pending_error: pending_err,
        chain_depth,
    })
}

fn agy_needs_rpc(
    cand: &RawCand,
    locks: &HostLockSnapshot,
    ledger: &TrajectoryLedger,
    agy_daemon: Option<&AgyDaemonEndpoint>,
    eff_now: f64,
) -> bool {
    agy_daemon.is_some_and(|e| e.http_port > 0)
        && (ledger.has_inflight_turn
            || locks.agy_presence_locked
            || cand.sort_epoch >= eff_now - HERO_WINDOW_SEC)
}

#[allow(clippy::too_many_arguments)]
fn finalize_agy_item(
    cand: &RawCand,
    locks: &HostLockSnapshot,
    ledger: &TrajectoryLedger,
    agy_daemon: Option<&AgyDaemonEndpoint>,
    receipts: &ReceiptIdx,
    eff_now: f64,
    agy_http_conn: Option<&mut AgyHttpConn>,
    agy_run_statuses: Option<&HashMap<String, i32>>,
) -> Option<MenubarSessionItem> {
    let cid = CascadeId::parse(&cand.session_id).ok()?;
    let mut rpc_active = false;
    let mut rpc_verified_inactive = false;
    if agy_needs_rpc(cand, locks, ledger, agy_daemon, eff_now) {
        if let Some(st_map) = agy_run_statuses
            && let Some(&st) = st_map.get(&cand.session_id)
        {
            rpc_active = matches!(st, 2..=4);
            rpc_verified_inactive = st == 1;
        } else if let Ok(rpc_res) =
            fetch_agy_trajectory_via_rpc(agy_daemon, &cid, 2.0, false, agy_http_conn)
        {
            rpc_active = rpc_res.is_active();
            rpc_verified_inactive = rpc_res.cascade_run_status == 1;
        }
    }
    let is_active = rpc_active
        || (agy_daemon.is_none() && locks.agy_presence_locked)
        || (ledger.has_inflight_turn
            && !rpc_verified_inactive
            && is_source_actively_running(HostKind::Antigravity, ledger, locks, false));
    classify_ledger_item(cand, ledger, is_active, receipts)
}

fn probe_codex_item(
    cand: &RawCand,
    guard: &PresenceWalGuard,
    transport: &DefaultHostTransport,
    codex_daemon: Option<&CodexDaemonEndpoint>,
    receipts: &ReceiptIdx,
) -> Option<MenubarSessionItem> {
    let tid = CodexThreadId::parse(&cand.session_id).ok()?;
    let locks = guard
        .probe_locks(None, Some(&tid), false, false, false)
        .ok()?;
    let mode = if codex_daemon.is_some() {
        TransportMode::OnlineRpc
    } else {
        TransportMode::OfflineStorage
    };
    let (ledger, read_mode, rpc_active) = transport
        .read_codex_ledger_with_meta(&cand.workspace.binding, &tid, &locks, mode)
        .or_else(|_| {
            transport.read_codex_ledger_with_meta(
                &cand.workspace.binding,
                &tid,
                &locks,
                TransportMode::OfflineStorage,
            )
        })
        .ok()?;
    let has_in_progress_boundary = ledger.events.iter().any(|ev| {
        matches!(
            ev,
            CanonicalEvent::TaskBoundary { task_status, .. }
            if task_status.eq_ignore_ascii_case("in_progress")
        )
    });
    let rpc_verified_inactive =
        read_mode == TransportMode::OnlineRpc && !rpc_active && !has_in_progress_boundary;
    let is_active = rpc_active
        || (has_in_progress_boundary && codex_daemon.is_some())
        || (ledger.has_inflight_turn
            && !rpc_verified_inactive
            && is_source_actively_running(HostKind::Codex, &ledger, &locks, false));
    classify_ledger_item(cand, &ledger, is_active, receipts)
}

#[allow(clippy::too_many_arguments)]
fn probe_candidates(
    merged: &[RawCand],
    target_n: usize,
    guard: &PresenceWalGuard,
    transport: &DefaultHostTransport,
    agy_daemon: Option<&AgyDaemonEndpoint>,
    codex_daemon: Option<&CodexDaemonEndpoint>,
    receipts: &ReceiptIdx,
    eff_now: f64,
) -> Vec<MenubarSessionItem> {
    if merged.is_empty() {
        return Vec::new();
    }
    let primary_indices: Vec<usize> = merged
        .iter()
        .enumerate()
        .filter(|(i, c)| {
            *i < target_n
                || receipts.pending.contains_key(&(
                    c.workspace.binding.workspace_path.clone(),
                    c.host.to_owned(),
                    c.session_id.clone(),
                ))
        })
        .map(|(i, _)| i)
        .collect();

    let needs_batch = agy_daemon.is_some_and(|e| e.http_port > 0)
        && merged.iter().any(|c| c.host == "antigravity");

    let mut decoded_agy: HashMap<usize, (HostLockSnapshot, TrajectoryLedger)> = HashMap::new();
    let mut results: HashMap<usize, Option<MenubarSessionItem>> = HashMap::new();
    let mut agy_run_statuses: Option<HashMap<String, i32>> = None;

    std::thread::scope(|s| {
        let batch_handle = if needs_batch {
            Some(s.spawn(|| fetch_agy_run_statuses_via_rpc(agy_daemon, 4.0, None).ok()))
        } else {
            None
        };

        let codex_handle = s.spawn(|| {
            let mut cr = Vec::new();
            for &idx in &primary_indices {
                if merged[idx].host == "codex" {
                    cr.push((
                        idx,
                        probe_codex_item(&merged[idx], guard, transport, codex_daemon, receipts),
                    ));
                }
            }
            cr
        });

        for &idx in &primary_indices {
            if merged[idx].host == "antigravity" {
                let c = &merged[idx];
                if let Ok(cid) = CascadeId::parse(&c.session_id)
                    && let Ok(locks) = guard.probe_locks(Some(&cid), None, false, false, false)
                    && let Ok((ledger, _, _)) = transport.read_agy_ledger_with_meta(
                        &c.workspace.binding,
                        &cid,
                        &locks,
                        TransportMode::OfflineStorage,
                    )
                {
                    decoded_agy.insert(idx, (locks, ledger));
                } else {
                    results.insert(idx, None);
                }
            }
        }

        if let Ok(cr) = codex_handle.join() {
            for (idx, item) in cr {
                results.insert(idx, item);
            }
        }
        if let Some(bh) = batch_handle
            && let Ok(st) = bh.join()
        {
            agy_run_statuses = st;
        }
    });

    let mut conn = agy_daemon
        .filter(|e| e.http_port > 0)
        .map(|e| AgyHttpConn::new(e.http_port));
    for (idx, (locks, ledger)) in &decoded_agy {
        let item = finalize_agy_item(
            &merged[*idx],
            locks,
            ledger,
            agy_daemon,
            receipts,
            eff_now,
            conn.as_mut(),
            agy_run_statuses.as_ref(),
        );
        results.insert(*idx, item);
    }

    let mut valid_count = results.values().filter(|v| v.is_some()).count();
    for (idx, cand) in merged.iter().enumerate() {
        if valid_count >= target_n {
            break;
        }
        if results.contains_key(&idx) {
            continue;
        }
        let item = if cand.host == "antigravity" {
            if let Ok(cid) = CascadeId::parse(&cand.session_id)
                && let Ok(locks) = guard.probe_locks(Some(&cid), None, false, false, false)
                && let Ok((ledger, _, _)) = transport.read_agy_ledger_with_meta(
                    &cand.workspace.binding,
                    &cid,
                    &locks,
                    TransportMode::OfflineStorage,
                )
            {
                finalize_agy_item(
                    cand,
                    &locks,
                    &ledger,
                    agy_daemon,
                    receipts,
                    eff_now,
                    conn.as_mut(),
                    agy_run_statuses.as_ref(),
                )
            } else {
                None
            }
        } else {
            probe_codex_item(cand, guard, transport, codex_daemon, receipts)
        };
        if item.is_some() {
            valid_count += 1;
        }
        results.insert(idx, item);
    }

    let mut indices: Vec<usize> = results.keys().copied().collect();
    indices.sort_unstable();
    let _ = (
        decode_codex_rollout,
        rescue_interrupted_ledger,
        raw_spec_to_candidate_path,
    );
    indices
        .into_iter()
        .filter_map(|i| results.remove(&i).flatten())
        .collect()
}

/// Build a bounded snapshot of recent Antigravity and Codex sessions for the menubar feed.
///
/// # Errors
/// Returns [`HostError::InvalidArgument`] if `limit` is outside `1..=50` or a workspace is invalid.
pub fn build_menubar_feed(
    paths: &HostEnvironmentPaths,
    limit: usize,
    seed_workspaces: &[String],
    now_epoch: Option<f64>,
) -> Result<MenubarFeedSnapshot, HostError> {
    if !(1..=50).contains(&limit) {
        return Err(HostError::InvalidArgument(format!(
            "limit must be between 1 and 50, got {limit}."
        )));
    }
    let eff_now = now_epoch.unwrap_or_else(|| {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
    });
    let generated_at_iso = format_epoch_precise_iso(eff_now);

    let guard = PresenceWalGuard::new(paths.clone());
    let preloaded = load_preloaded_host_tables(paths);
    let transport = DefaultHostTransport::new(paths.clone());
    let homes = candidate_home_dirs(paths);
    let mut norm_cache = HashMap::new();

    let agy_daemon = guard.discover_agy_daemon();
    let codex_daemon = guard.discover_codex_daemon();
    let receipts = load_receipt_index(paths, &homes, &mut norm_cache);
    let lookup = build_workspace_lookup(
        paths,
        &preloaded,
        &receipts,
        seed_workspaces,
        &homes,
        &mut norm_cache,
    )?;

    let pool_size = (limit * 4).max(24);
    let pending_agy: HashSet<String> = receipts
        .pending
        .keys()
        .filter(|(_, h, _)| h == "antigravity")
        .map(|(_, _, id)| id.clone())
        .collect();
    let pending_codex: HashSet<String> = receipts
        .pending
        .keys()
        .filter(|(_, h, _)| h == "codex")
        .map(|(_, _, id)| id.clone())
        .collect();

    let mut merged = collect_agy_candidates(
        paths,
        &preloaded,
        &lookup,
        &pending_agy,
        pool_size,
        &homes,
        &mut norm_cache,
    );
    merged.extend(collect_codex_candidates(
        paths,
        &preloaded,
        &lookup,
        &pending_codex,
        pool_size,
        &homes,
        &mut norm_cache,
    ));
    merged.sort_by(|a, b| {
        b.sort_epoch
            .partial_cmp(&a.sort_epoch)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.host.cmp(b.host))
            .then_with(|| a.session_id.cmp(&b.session_id))
    });

    let items = probe_candidates(
        &merged,
        limit + 2,
        &guard,
        &transport,
        agy_daemon.as_ref(),
        codex_daemon.as_ref(),
        &receipts,
        eff_now,
    );

    let actionable_indices: Vec<usize> = items
        .iter()
        .enumerate()
        .filter(|(_, it)| it.state.is_actionable())
        .map(|(i, _)| i)
        .collect();

    let hero_idx = if let Some(&newest_idx) = actionable_indices.first() {
        let interrupted_idx = actionable_indices
            .iter()
            .copied()
            .find(|&i| items[i].state == MenubarCandidateState::InterruptedStopped);
        match interrupted_idx {
            Some(ii) if ii == newest_idx || items[ii].sort_epoch >= eff_now - HERO_WINDOW_SEC => {
                Some(ii)
            }
            _ => Some(newest_idx),
        }
    } else {
        None
    };

    let hero = hero_idx.map(|i| items[i].clone());
    let recent: Vec<MenubarSessionItem> = items
        .into_iter()
        .enumerate()
        .filter(|(i, _)| Some(*i) != hero_idx)
        .map(|(_, it)| it)
        .take(limit)
        .collect();

    Ok(MenubarFeedSnapshot {
        generated_at_iso,
        agy_daemon_active: agy_daemon.is_some(),
        codex_daemon_active: codex_daemon.is_some(),
        hero,
        recent,
    })
}
