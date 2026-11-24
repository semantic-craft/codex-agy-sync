//! Inspect, handoff orchestration (`--latest`, `--chain`, `--source-id`, `--auto-pair`, `--pair-if-offline`, `--include-interrupted`), and pending reconciliation.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use cas_codecs::{
    decode_agy_trajectory, decode_codex_rollout, decode_codex_rpc_thread_to_ledger,
    encode_codex_rollout, read_codex_rollout_lines,
};
use cas_core::{
    AgyGuiSafeSource, CanonicalEvent, CascadeId, CodexThreadId, HostKind, HostLockSnapshot,
    PairingPolicy, ReceiptState, RolloutLineNo, SubagentLineage, SyncDirection, SyncReceipt,
    TrajectoryLedger, TransportMode, WorkspacePath, WorkspaceRootUri, generate_uuid7,
    is_host_injected_tool_result, is_host_injected_user_event, ledger_seed_fingerprint,
    rescue_interrupted_ledger, resolve_strict_false, serialize_tool_payload_arguments_json,
    validate_session_id,
};
use serde::Serialize;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::catalog::{
    assert_source_belongs_to_binding, discover_agy_entries, discover_codex_entries,
    infer_direction_from_source_id, load_preloaded_host_tables,
};
use crate::error::HostError;
use crate::locks::{PresenceWalGuard, has_live_unix_socket, open_handoff_lock};
use crate::paths::{
    HostEnvironmentPaths, atomic_write_text, deterministic_uuid, now_iso, parse_ts_epoch_iso,
    parse_ws_ident, validated_child_path,
};
use crate::registry::{
    ProjectRegistry, WorkspaceProjectBinding, diagnose_workspace_binding, pair_workspace_with_mode,
};
use crate::rpc_agy::fetch_agy_trajectory_via_rpc;
use crate::rpc_codex::{fetch_codex_thread_via_rpc, import_codex_rollout_via_rpc};
use crate::sqlite::{
    HandoffStagingPermit, SqliteWritePermit, open_readonly_sqlite, optional_select_columns_sql,
    table_column_names,
};
use crate::transport::DefaultHostTransport;
use sha2::{Digest, Sha256};

const CODEX_RUNTIME_EVENTS: &[&str] = &[
    "session_meta",
    "world_state",
    "codex_developer_message",
    "codex_event_msg:thread_settings_applied",
    "codex_response_item:additional_tools",
];

#[derive(Debug, Clone, Serialize)]
pub struct InspectCounts {
    pub user_inputs: usize,
    pub assistant_texts: usize,
    pub thinking_events: usize,
    pub tool_calls: usize,
    pub tool_results: usize,
    pub checkpoints: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct InspectWatermark {
    pub turn_id: String,
    pub boundary_digest: String,
    pub last_agy_step_index: u32,
    pub last_codex_line_no: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct InspectReport {
    pub workspace_path: String,
    pub workspace_uri: String,
    pub canonical_title: String,
    pub total_events: usize,
    pub completed_turns: usize,
    pub has_inflight_turn: bool,
    pub counts: InspectCounts,
    pub latest_watermark: Option<InspectWatermark>,
}

/// Inspect a single Antigravity cascade or Codex thread strictly read-only.
///
/// # Errors
/// Returns [`HostError`] if selectors are invalid, session missing, or RPC unavailable in `online_rpc` mode.
pub fn inspect_session_ledger(
    paths: &HostEnvironmentPaths,
    workspace: &str,
    cascade_id: Option<&str>,
    codex_thread_id: Option<&str>,
    mode: TransportMode,
) -> Result<InspectReport, HostError> {
    if cascade_id.is_some() == codex_thread_id.is_some() {
        return Err(HostError::InvalidArgument(
            "Pass exactly one of --cascade-id or --codex-thread-id.".to_owned(),
        ));
    }
    let binding = ProjectRegistry::new(paths.clone()).resolve_readonly_binding(workspace)?;
    let guard = PresenceWalGuard::new(paths.clone());
    let transport = DefaultHostTransport::new(paths.clone());

    let ledger = if let Some(cid_raw) = cascade_id {
        let cid = CascadeId::parse(cid_raw)?;
        let locks = guard.probe_locks(Some(&cid), None, false, false, false)?;
        let (l, _, _) = transport.read_agy_ledger_with_meta(&binding, &cid, &locks, mode)?;
        l
    } else {
        let tid = CodexThreadId::parse(codex_thread_id.unwrap_or_default())?;
        let locks = guard.probe_locks(None, Some(&tid), false, false, false)?;
        let (l, _, _) = transport.read_codex_ledger_with_meta(&binding, &tid, &locks, mode)?;
        l
    };

    let mut counts = InspectCounts {
        user_inputs: 0,
        assistant_texts: 0,
        thinking_events: 0,
        tool_calls: 0,
        tool_results: 0,
        checkpoints: 0,
    };
    for ev in &ledger.events {
        match ev {
            CanonicalEvent::UserInput { .. } => counts.user_inputs += 1,
            CanonicalEvent::AssistantText { .. } => counts.assistant_texts += 1,
            CanonicalEvent::Thinking { .. } => counts.thinking_events += 1,
            CanonicalEvent::ToolCall { .. } => counts.tool_calls += 1,
            CanonicalEvent::ToolResult { .. } => counts.tool_results += 1,
            CanonicalEvent::Checkpoint { .. } => counts.checkpoints += 1,
            CanonicalEvent::TaskBoundary { .. } | CanonicalEvent::SystemMessage { .. } => {}
        }
    }
    let latest_watermark = ledger.latest_watermark().map(|wm| InspectWatermark {
        turn_id: wm.turn_id.as_str().to_owned(),
        boundary_digest: wm.boundary_digest.as_str().to_owned(),
        last_agy_step_index: wm.last_agy_step_index.0,
        last_codex_line_no: wm.last_codex_line_no.0,
    });

    Ok(InspectReport {
        workspace_path: binding.workspace_path,
        workspace_uri: binding.workspace_uri,
        canonical_title: ledger.title,
        total_events: ledger.events.len(),
        completed_turns: ledger.completed_turn_watermarks.len(),
        has_inflight_turn: ledger.has_inflight_turn,
        counts,
        latest_watermark,
    })
}

/// Determine whether a source session is actively running right now.
#[must_use]
pub fn is_source_actively_running(
    host: HostKind,
    ledger: &TrajectoryLedger,
    locks: &HostLockSnapshot,
    rpc_active: bool,
) -> bool {
    if rpc_active {
        return true;
    }
    match host {
        HostKind::Antigravity => {
            locks.agy_presence_locked
                || (ledger.has_inflight_turn
                    && (locks.agy_daemon.is_some() || locks.agy_wal_active))
        }
        HostKind::Codex => {
            ledger.has_inflight_turn && (locks.codex_daemon.is_some() || locks.codex_wal_active)
        }
    }
}

fn load_receipt_file(path: &Path) -> Result<SyncReceipt, HostError> {
    let text = fs::read_to_string(path)?;
    Ok(serde_json::from_str::<SyncReceipt>(&text)?)
}

fn save_receipt_file(path: &Path, receipt: &SyncReceipt) -> Result<(), HostError> {
    let rendered = format!("{}\n", serde_json::to_string_pretty(receipt)?);
    atomic_write_text(path, &rendered)
}

fn receipt_matches_workspace(receipt_ws: &str, ws_path: &WorkspacePath) -> bool {
    if receipt_ws == ws_path.as_path().to_string_lossy().as_ref() {
        return true;
    }
    parse_ws_ident(receipt_ws).is_ok_and(|(p, _)| &p == ws_path)
}

/// Load all persisted receipts under `<sync_state_dir>/handoffs/*.json`.
#[must_use]
pub fn load_all_receipts(paths: &HostEnvironmentPaths) -> Vec<(PathBuf, SyncReceipt, f64)> {
    let state = paths.sync_state_dir.join("handoffs");
    let Ok(rd) = fs::read_dir(&state) else {
        return Vec::new();
    };
    let mut files: Vec<PathBuf> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_file() && p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    files.sort();
    let mut out = Vec::new();
    for p in files {
        let Ok(rec) = load_receipt_file(&p) else {
            continue;
        };
        let mtime = fs::metadata(&p)
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map_or(0.0, |d| d.as_secs_f64());
        out.push((p, rec, mtime));
    }
    out
}

/// Load persisted receipts matching `ws_path`.
#[must_use]
pub fn load_workspace_receipts(
    paths: &HostEnvironmentPaths,
    ws_path: &WorkspacePath,
) -> Vec<(PathBuf, SyncReceipt, f64)> {
    load_all_receipts(paths)
        .into_iter()
        .filter(|(_, r, _)| receipt_matches_workspace(&r.workspace, ws_path))
        .collect()
}

fn require_binding(
    paths: &HostEnvironmentPaths,
    workspace: &str,
) -> Result<WorkspaceProjectBinding, HostError> {
    let diag = diagnose_workspace_binding(paths, workspace)?;
    if !diag.is_paired() {
        let diag_json = serde_json::to_value(&diag)?;
        let steps = diag.remediation_steps.clone();
        return Err(HostError::UnpairedWorkspace {
            message: format!(
                "Workspace {:?} is not paired on both Antigravity and Codex.",
                diag.workspace_path
            ),
            diagnosis: diag_json,
            remediation: steps,
        });
    }
    ProjectRegistry::new(paths.clone()).resolve_readonly_binding(workspace)
}

/// Read and validate source trajectory for sync, rescuing interrupted turns when requested.
///
/// # Errors
/// Returns [`HostError`] if source is missing, active, incomplete without `--include-interrupted`, or empty.
pub fn read_handoff_source(
    paths: &HostEnvironmentPaths,
    binding: &WorkspaceProjectBinding,
    direction: SyncDirection,
    source_id: &str,
    include_interrupted: bool,
) -> Result<TrajectoryLedger, HostError> {
    let ws_path = binding.parsed_path();
    let ws_uri = binding.parsed_uri();
    let source_host = match direction {
        SyncDirection::CodexToAgy => HostKind::Codex,
        SyncDirection::AgyToCodex => HostKind::Antigravity,
        SyncDirection::AutoIncremental => {
            return Err(HostError::InvalidArgument(
                "Handoff direction must be codex_to_agy or agy_to_codex.".to_owned(),
            ));
        }
    };
    assert_source_belongs_to_binding(paths, source_host, source_id, &ws_path, &ws_uri, None)?;

    let guard = PresenceWalGuard::new(paths.clone());
    let transport = DefaultHostTransport::new(paths.clone());
    let to_agy = direction == SyncDirection::CodexToAgy;

    let locks = if to_agy {
        let tid = CodexThreadId::parse(source_id)?;
        guard.probe_locks(None, Some(&tid), false, false, false)?
    } else {
        let cid = CascadeId::parse(source_id)?;
        guard.probe_locks(Some(&cid), None, false, false, false)?
    };

    let (mut ledger, active, rpc_verified_inactive) = if to_agy {
        let tid = CodexThreadId::parse(source_id)?;
        let mode = if locks.codex_daemon.is_some() {
            TransportMode::OnlineRpc
        } else {
            TransportMode::OfflineStorage
        };
        let (l, read_mode, active) =
            match transport.read_codex_ledger_with_meta(binding, &tid, &locks, mode) {
                Ok(res) => res,
                Err(HostError::OnlineRpcUnavailable(_)) => {
                    if locks.codex_wal_active {
                        return Err(HostError::InvalidArgument(
                            "Source Codex session is locked and its live status cannot be read."
                                .to_owned(),
                        ));
                    }
                    transport.read_codex_ledger_with_meta(
                        binding,
                        &tid,
                        &locks,
                        TransportMode::OfflineStorage,
                    )?
                }
                Err(e) => return Err(e),
            };
        let has_in_progress = l.events.iter().any(|ev| {
            matches!(
                ev,
                CanonicalEvent::TaskBoundary { task_status, .. }
                if task_status.eq_ignore_ascii_case("in_progress")
            )
        });
        let verified_inactive =
            read_mode == TransportMode::OnlineRpc && !active && !has_in_progress;
        (l, active, verified_inactive)
    } else {
        let cid = CascadeId::parse(source_id)?;
        if locks.agy_daemon.is_none() && locks.agy_presence_locked {
            return Err(HostError::InvalidArgument(
                "Source Antigravity session is locked and its live status cannot be read."
                    .to_owned(),
            ));
        }
        let mut active = false;
        let mut verified_inactive = false;
        if let Some(ref ep) = locks.agy_daemon {
            let rpc_res = fetch_agy_trajectory_via_rpc(Some(ep), &cid, 5.0, true, None)?;
            active = rpc_res.is_active();
            verified_inactive = rpc_res.cascade_run_status == 1;
        }
        if active {
            return Err(HostError::InvalidArgument(
                "Source session is active. Finish the turn before sync.".to_owned(),
            ));
        }
        let (l, _, _) = transport.read_agy_ledger_with_meta(
            binding,
            &cid,
            &locks,
            TransportMode::OfflineStorage,
        )?;
        (l, active, verified_inactive)
    };

    if active || ledger.has_inflight_turn {
        if !ledger.has_inflight_turn || !include_interrupted {
            return Err(HostError::InvalidArgument(
                "Source session is active or has an incomplete turn. Finish the turn before sync."
                    .to_owned(),
            ));
        }
        if active
            || (!rpc_verified_inactive
                && is_source_actively_running(source_host, &ledger, &locks, active))
        {
            return Err(HostError::InvalidArgument(
                "Source session is active. Finish the turn before sync.".to_owned(),
            ));
        }
        ledger = rescue_interrupted_ledger(&ledger).0;
    }

    if ledger.latest_watermark().is_none() {
        return Err(HostError::EmptySession(
            "Source session has no completed history to sync.".to_owned(),
        ));
    }
    Ok(ledger)
}

fn checkpoint_text(event: &CanonicalEvent) -> String {
    let CanonicalEvent::Checkpoint {
        anchor,
        user_intent,
        session_summary,
        code_change_summary,
        ..
    } = event
    else {
        return String::new();
    };
    let mut summary = session_summary.clone();
    for ext in &anchor.host_extensions {
        if ext.extension_type == "turn_context"
            && !ext.raw_payload.is_empty()
            && let Ok(Value::Object(obj)) = serde_json::from_slice::<Value>(&ext.raw_payload)
        {
            summary = obj
                .get("summary")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
        }
    }
    let s = if summary == "none" || summary == "turn_context" {
        ""
    } else {
        summary.as_str()
    };
    [user_intent.as_str(), s, code_change_summary.as_str()]
        .into_iter()
        .filter(|p| !p.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

/// Flatten a completed source [`TrajectoryLedger`] into a portable handoff baton ledger.
#[must_use]
pub fn handoff_ledger(ledger: &TrajectoryLedger, title_override: Option<&str>) -> TrajectoryLedger {
    let calls: HashSet<_> = ledger
        .events
        .iter()
        .filter_map(|e| match e {
            CanonicalEvent::ToolCall { call_id, .. } => Some(call_id),
            _ => None,
        })
        .collect();
    let mut events = Vec::new();
    let completed = ledger.slice_after_watermark(None).unwrap_or(&[]);

    for ev in completed {
        if let CanonicalEvent::SystemMessage {
            anchor, event_type, ..
        } = ev
            && anchor.origin_host == HostKind::Codex
            && CODEX_RUNTIME_EVENTS.contains(&event_type.as_str())
        {
            continue;
        }
        if matches!(ev, CanonicalEvent::UserInput { .. }) && is_host_injected_user_event(ev) {
            continue;
        }
        if let CanonicalEvent::ToolResult { call_id, .. } = ev
            && !calls.contains(call_id)
            && is_host_injected_tool_result(ev)
        {
            continue;
        }

        let mut clean_anchor = ev.anchor().clone();
        clean_anchor.host_extensions.clear();
        clean_anchor.unknown_wire_fields.clear();

        match ev {
            CanonicalEvent::Thinking { summary_text, .. } => {
                if !summary_text.is_empty() {
                    events.push(CanonicalEvent::AssistantText {
                        anchor: clean_anchor,
                        text: format!("[Imported reasoning summary]\n{summary_text}"),
                        message_id: None,
                        stop_reason: None,
                        generator_model: None,
                        token_usage: Default::default(),
                    });
                }
            }
            CanonicalEvent::Checkpoint { .. } => {
                let txt = checkpoint_text(ev);
                if !txt.is_empty() {
                    events.push(CanonicalEvent::AssistantText {
                        anchor: clean_anchor,
                        text: format!("[Imported checkpoint summary]\n{txt}"),
                        message_id: None,
                        stop_reason: None,
                        generator_model: None,
                        token_usage: Default::default(),
                    });
                }
            }
            CanonicalEvent::ToolCall {
                call_id,
                tool_name,
                payload,
                ..
            } => {
                let args = serialize_tool_payload_arguments_json(payload);
                events.push(CanonicalEvent::AssistantText {
                    anchor: clean_anchor,
                    text: format!(
                        "[Imported tool call] {tool_name}\nCall ID: {}\nArguments:\n{args}",
                        call_id.as_str()
                    ),
                    message_id: None,
                    stop_reason: None,
                    generator_model: None,
                    token_usage: Default::default(),
                });
            }
            CanonicalEvent::ToolResult {
                call_id,
                tool_name,
                output_text,
                is_error,
                full_output_uri,
                ..
            } => {
                let mut text = format!(
                    "[Imported tool result] {tool_name}\nCall ID: {}\nis_error: {}\nOutput:\n{output_text}",
                    call_id.as_str(),
                    if *is_error { "true" } else { "false" }
                );
                if let Some(uri) = full_output_uri.as_deref().filter(|s| !s.is_empty()) {
                    text.push_str(&format!("\nFull output URI: {uri}"));
                }
                events.push(CanonicalEvent::AssistantText {
                    anchor: clean_anchor,
                    text,
                    message_id: None,
                    stop_reason: None,
                    generator_model: None,
                    token_usage: Default::default(),
                });
            }
            other => {
                let mut cloned = other.clone();
                *cloned.anchor_mut() = clean_anchor;
                events.push(cloned);
            }
        }
    }

    let eff_title = title_override
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(ledger.title.as_str());
    TrajectoryLedger::from_events(
        ledger.workspace_path.clone(),
        ledger.workspace_uri.clone(),
        &events,
        Some(eff_title),
        false,
    )
}

fn conversation_text(ledger: &TrajectoryLedger) -> Vec<(&'static str, &str)> {
    ledger
        .events
        .iter()
        .filter_map(|e| match e {
            CanonicalEvent::UserInput { query_text, .. } => {
                Some(("user_input", query_text.as_str()))
            }
            CanonicalEvent::AssistantText { text, .. } => Some(("assistant_text", text.as_str())),
            _ => None,
        })
        .collect()
}

fn verify_context(expected: &TrajectoryLedger, actual: &TrajectoryLedger) -> Result<(), HostError> {
    if actual.latest_watermark().is_none()
        || conversation_text(expected) != conversation_text(actual)
    {
        return Err(HostError::Runtime(
            "Imported user and assistant history does not match the completed source conversation."
                .to_owned(),
        ));
    }
    Ok(())
}

fn publish_new_file(source: &Path, target: &Path) -> Result<(), HostError> {
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp_name = format!(
        ".handoff-{}-{}.tmp",
        std::process::id(),
        Uuid::new_v4().simple()
    );
    let tmp_path = target.with_file_name(tmp_name);
    let data = fs::read(source)?;
    let write_res = (|| -> Result<(), HostError> {
        let mut f = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&tmp_path)?;
        f.write_all(&data)?;
        f.sync_all()?;
        drop(f);
        fs::hard_link(&tmp_path, target)?;
        Ok(())
    })();
    let _ = fs::remove_file(&tmp_path);
    write_res
}

fn cleanup_handoff_staging_dir(staging_root: &Path, staging_path: &str) -> bool {
    if staging_path.trim().is_empty() {
        return false;
    }
    let st_resolved = resolve_strict_false(Path::new(staging_path));
    let root_resolved = resolve_strict_false(staging_root);
    if st_resolved == root_resolved || !st_resolved.starts_with(&root_resolved) {
        return false;
    }
    if st_resolved.is_dir() {
        fs::remove_dir_all(&st_resolved).is_ok()
    } else {
        false
    }
}

fn stage_agy(
    paths: &HostEnvironmentPaths,
    binding: &WorkspaceProjectBinding,
    ledger: &TrajectoryLedger,
    staging_permit: &HandoffStagingPermit,
    target_id: &str,
) -> Result<(Vec<PathBuf>, PathBuf), HostError> {
    let stage = staging_permit.dir();
    let mut staged_paths = paths.clone();
    staged_paths.agy_data_dir = stage.to_path_buf();
    staged_paths.agy_cli_data_dir = None;
    let transport = DefaultHostTransport::new(staged_paths);
    let cid = CascadeId::parse(target_id)?;
    let lineage = SubagentLineage {
        root_cascade_id: Some(cid.clone()),
        ..SubagentLineage::default()
    };
    let (db_path, rows) = transport.write_agy_conversation_db(
        binding,
        &cid,
        &ledger.events,
        ledger,
        AgyGuiSafeSource::CascadeClient,
        false,
        &lineage,
        None,
        SqliteWritePermit::Staging(staging_permit),
    )?;
    let persisted = decode_agy_trajectory(
        binding.parsed_path(),
        binding.parsed_uri(),
        &rows,
        Some(&ledger.title),
    )?;
    verify_context(ledger, &persisted)?;
    let viewed_iso = now_iso();
    let proj = persisted.project_agy_cascade_summary(
        &cid,
        &binding.agy_project_id,
        Some(&viewed_iso),
        AgyGuiSafeSource::Unspecified,
        Some(&lineage),
    )?;
    transport.write_agy_brain_and_pbtxt(binding, &cid, &persisted, &proj, &viewed_iso, &lineage)?;
    let files = vec![
        stage
            .join("annotations")
            .join(format!("{}.pbtxt", cid.as_str())),
        stage
            .join("brain")
            .join(cid.as_str())
            .join(".system_generated")
            .join("logs")
            .join("transcript.jsonl"),
        db_path.clone(),
    ];
    Ok((files, db_path))
}

fn import_agy(
    paths: &HostEnvironmentPaths,
    binding: &WorkspaceProjectBinding,
    ledger: &TrajectoryLedger,
    receipt: &SyncReceipt,
    staging_permit: &HandoffStagingPermit,
) -> Result<String, HostError> {
    let stage = staging_permit.dir().to_path_buf();
    let target_id = receipt.target_id().ok_or_else(|| {
        HostError::Runtime("Missing target_id on Antigravity handoff receipt.".to_owned())
    })?;
    let (files, staged_db) = stage_agy(paths, binding, ledger, staging_permit, target_id)?;
    for f in &files {
        let rel = f
            .strip_prefix(&stage)
            .map_err(|e| HostError::Runtime(format!("Staging path mismatch: {e}")))?;
        publish_new_file(f, &paths.agy_data_dir.join(rel))?;
    }
    if let Some(ref cli_dir) = paths.agy_cli_data_dir
        && resolve_strict_false(cli_dir) != resolve_strict_false(&paths.agy_data_dir)
    {
        let staged_tx = stage
            .join("brain")
            .join(target_id)
            .join(".system_generated")
            .join("logs")
            .join("transcript.jsonl");
        let cli_tx = validated_child_path(
            cli_dir,
            &[
                "brain",
                target_id,
                ".system_generated",
                "logs",
                "transcript.jsonl",
            ],
            "target_id",
        )?;
        if staged_tx.is_file() && !cli_tx.exists() {
            publish_new_file(&staged_tx, &cli_tx)?;
        }
    }
    let endpoint = PresenceWalGuard::new(paths.clone()).discover_agy_daemon();
    let target_cid = CascadeId::parse(target_id)?;
    let result = fetch_agy_trajectory_via_rpc(endpoint.as_ref(), &target_cid, 30.0, true, None)?;
    let mut staged_paths = paths.clone();
    staged_paths.agy_data_dir = stage;
    let staged_steps =
        DefaultHostTransport::new(staged_paths).read_persisted_agy_steps(&staged_db)?;
    if result.raw_steps.len() != staged_steps.len() {
        return Err(HostError::Runtime(
            "Antigravity did not load all staged steps; sync remains pending.".to_owned(),
        ));
    }
    let loaded = decode_agy_trajectory(
        binding.parsed_path(),
        binding.parsed_uri(),
        &result.raw_steps,
        None,
    )?;
    verify_context(ledger, &loaded)?;
    Ok(target_id.to_owned())
}

fn import_codex(
    paths: &HostEnvironmentPaths,
    binding: &WorkspaceProjectBinding,
    ledger: &TrajectoryLedger,
    receipt: &SyncReceipt,
) -> Result<CodexThreadId, HostError> {
    let stage = PathBuf::from(receipt.staging_path());
    fs::create_dir_all(&stage)?;
    let rollout = stage.join("rollout.jsonl");
    let sid = generate_uuid7();
    let rows = encode_codex_rollout(
        &ledger.events,
        RolloutLineNo(1),
        Some(&sid),
        Some(&binding.workspace_path),
    );
    let mut body = rows
        .into_iter()
        .map(|r| r.raw_json)
        .collect::<Vec<_>>()
        .join("\n");
    body.push('\n');
    let mut f = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&rollout)?;
    f.write_all(body.as_bytes())?;
    drop(f);

    let endpoint = PresenceWalGuard::new(paths.clone())
        .discover_codex_daemon()
        .ok_or_else(|| {
            HostError::OnlineRpcUnavailable(
                "No live Codex app-server Unix socket available.".to_owned(),
            )
        })?;
    import_codex_rollout_via_rpc(
        &endpoint,
        &rollout,
        Path::new(&binding.workspace_path),
        binding.codex_project_id.as_str(),
        &ledger.title,
        90.0,
    )
}

fn find_prior_chain_receipt(
    handoffs_dir: &Path,
    ws_path: &WorkspacePath,
    source_host: HostKind,
    source_id: &str,
) -> Option<(String, String, i64)> {
    let rd = fs::read_dir(handoffs_dir).ok()?;
    let mut matches = Vec::new();
    for entry in rd.flatten() {
        let p = entry.path();
        if p.is_file()
            && p.extension().and_then(|e| e.to_str()) == Some("json")
            && let Ok(prev) = load_receipt_file(&p)
            && prev.status_str() == "ready"
            && receipt_matches_workspace(&prev.workspace, ws_path)
            && prev.target_host == source_host.as_str()
            && prev.target_id() == Some(source_id)
        {
            matches.push(prev);
        }
    }
    let best = matches.into_iter().max_by(|a, b| {
        a.chain_depth
            .cmp(&b.chain_depth)
            .then_with(|| a.created_at_iso.cmp(&b.created_at_iso))
    })?;
    let cid = best.chain_id.unwrap_or_else(|| {
        deterministic_uuid(
            "handoff-chain",
            &format!("{}:{}:{}", best.workspace, best.source_host, best.source_id),
        )
    });
    Some((cid, best.source_id, best.chain_depth + 1))
}

fn ensure_no_pending_sibling(
    handoffs_root: &Path,
    receipt_path: &Path,
    ws_path: &WorkspacePath,
    source_host: HostKind,
    source_id: &str,
) -> Result<(), HostError> {
    let Ok(rd) = fs::read_dir(handoffs_root) else {
        return Ok(());
    };
    let mut files: Vec<PathBuf> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_file() && p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    files.sort();
    for existing in files {
        if existing == receipt_path {
            continue;
        }
        let Ok(prev) = load_receipt_file(&existing) else {
            continue;
        };
        if prev.status_str() == "pending"
            && receipt_matches_workspace(&prev.workspace, ws_path)
            && prev.source_host == source_host.as_str()
            && prev.source_id == source_id
        {
            return Err(HostError::HandoffPending {
                receipt: Box::new(prev),
                receipt_path: existing,
            });
        }
    }
    Ok(())
}

/// Execute a one-shot session sync/handoff and return its persisted [`SyncReceipt`].
///
/// # Errors
/// Returns [`HostError`] on validation, unpaired workspace, pending receipt, unavailable target daemon, or transport failure.
#[allow(clippy::too_many_arguments)]
pub fn handoff_session(
    paths: &HostEnvironmentPaths,
    workspace: &str,
    direction: SyncDirection,
    source_id: &str,
    title: Option<&str>,
    include_interrupted: bool,
    chain_id: Option<&str>,
    chained_from_source_id: Option<&str>,
    chain_depth: i64,
    pairing_mode: Option<&str>,
) -> Result<SyncReceipt, HostError> {
    validate_session_id(source_id, "source_id")?;
    let binding = require_binding(paths, workspace)?;
    let ws_path = binding.parsed_path();
    let (handoffs_root, _lock_guard) = open_handoff_lock(paths, ws_path.as_path())?;
    let ledger = read_handoff_source(paths, &binding, direction, source_id, include_interrupted)?;
    let wm = ledger.latest_watermark().ok_or_else(|| {
        HostError::EmptySession("Source session has no completed history to sync.".to_owned())
    })?;
    let to_agy = direction == SyncDirection::CodexToAgy;
    let source_host = if to_agy {
        HostKind::Codex
    } else {
        HostKind::Antigravity
    };
    let target_host = if to_agy {
        HostKind::Antigravity
    } else {
        HostKind::Codex
    };
    let source_digest = wm.boundary_digest.as_str().to_owned();

    let staging_root = handoffs_root.join("staging");
    fs::create_dir_all(&staging_root)?;

    let identity_json = serde_json::to_string(&json!([
        binding.workspace_uri,
        source_host.as_str(),
        source_id,
        source_digest,
    ]))?;
    let mut hasher = Sha256::new();
    hasher.update(identity_json.as_bytes());
    let receipt_stem = hex::encode(hasher.finalize());
    let receipt_path = handoffs_root.join(format!("{receipt_stem}.json"));
    let staging_dir = staging_root.join(&receipt_stem);

    if receipt_path.is_file()
        && let Ok(existing) = load_receipt_file(&receipt_path)
    {
        if existing.status_str() == "pending" {
            return Err(HostError::HandoffPending {
                receipt: Box::new(existing),
                receipt_path,
            });
        }
        if existing.status_str() == "ready"
            && let Some(tid) = existing.target_id()
        {
            return Ok(SyncReceipt {
                state: ReceiptState::NoOp {
                    target_id: tid.to_owned(),
                    staging_path: existing.staging_path().to_owned(),
                },
                ..existing
            });
        }
    }

    ensure_no_pending_sibling(
        &handoffs_root,
        &receipt_path,
        &ws_path,
        source_host,
        source_id,
    )?;

    let guard = PresenceWalGuard::new(paths.clone());
    let target_daemon_present = if to_agy {
        guard.discover_agy_daemon().is_some()
    } else {
        guard
            .discover_codex_daemon()
            .is_some_and(|ep| has_live_unix_socket(Some(&ep.socket_path)))
    };
    if !target_daemon_present {
        return Err(HostError::OnlineRpcUnavailable(
            "Open the target Desktop app before sync.".to_owned(),
        ));
    }

    let handoff = handoff_ledger(&ledger, title);
    let (seed_turns, seed_digest) = ledger_seed_fingerprint(&handoff);

    let (eff_chain_id, eff_chained_from, eff_chain_depth) =
        if chain_id.is_some() || chained_from_source_id.is_some() || chain_depth > 0 {
            (
                chain_id.map(str::to_owned),
                chained_from_source_id.map(str::to_owned),
                chain_depth,
            )
        } else if let Some((cid, cfrom, cdepth)) =
            find_prior_chain_receipt(&handoffs_root, &ws_path, source_host, source_id)
        {
            (Some(cid), Some(cfrom), cdepth)
        } else {
            (None, None, 0)
        };

    let created_iso = now_iso();
    let initial_target_id = if to_agy { Some(generate_uuid7()) } else { None };

    let mut receipt = SyncReceipt {
        workspace: binding.workspace_path.clone(),
        source_host: source_host.as_str().to_owned(),
        source_id: source_id.to_owned(),
        source_digest,
        target_host: target_host.as_str().to_owned(),
        title: handoff.title.clone(),
        state: ReceiptState::Pending {
            target_id: initial_target_id.clone(),
            staging_path: staging_dir.display().to_string(),
            error: None,
        },
        runtime_state_not_transferred: true,
        created_at_iso: created_iso,
        chain_id: eff_chain_id,
        chained_from_source_id: eff_chained_from,
        chain_depth: eff_chain_depth,
        target_seed_turn_count: Some(seed_turns),
        target_seed_text_digest: Some(seed_digest),
        pairing_mode: pairing_mode.map(str::to_owned),
    };
    save_receipt_file(&receipt_path, &receipt)?;

    if to_agy {
        let staging_permit = HandoffStagingPermit::new(&staging_root, &staging_dir)?;
        match import_agy(paths, &binding, &handoff, &receipt, &staging_permit) {
            Ok(verified_id) => {
                receipt.state = ReceiptState::Ready {
                    target_id: verified_id,
                    staging_path: staging_dir.display().to_string(),
                };
                save_receipt_file(&receipt_path, &receipt)?;
                cleanup_handoff_staging_dir(&staging_root, receipt.staging_path());
                Ok(receipt)
            }
            Err(err) => {
                receipt.state = ReceiptState::Pending {
                    target_id: initial_target_id,
                    staging_path: staging_dir.display().to_string(),
                    error: Some(err.to_string()),
                };
                let _ = save_receipt_file(&receipt_path, &receipt);
                Err(HostError::HandoffPending {
                    receipt: Box::new(receipt),
                    receipt_path,
                })
            }
        }
    } else {
        match import_codex(paths, &binding, &handoff, &receipt) {
            Ok(tid) => {
                receipt.state = ReceiptState::Ready {
                    target_id: tid.as_str().to_owned(),
                    staging_path: staging_dir.display().to_string(),
                };
                save_receipt_file(&receipt_path, &receipt)?;
                cleanup_handoff_staging_dir(&staging_root, receipt.staging_path());
                Ok(receipt)
            }
            Err(HostError::NativeCodexImportPending { message, thread_id }) => {
                receipt.state = ReceiptState::Pending {
                    target_id: thread_id,
                    staging_path: staging_dir.display().to_string(),
                    error: Some(message),
                };
                let _ = save_receipt_file(&receipt_path, &receipt);
                Err(HostError::HandoffPending {
                    receipt: Box::new(receipt),
                    receipt_path,
                })
            }
            Err(err) => {
                receipt.state = ReceiptState::Pending {
                    target_id: None,
                    staging_path: staging_dir.display().to_string(),
                    error: Some(err.to_string()),
                };
                let _ = save_receipt_file(&receipt_path, &receipt);
                Err(HostError::HandoffPending {
                    receipt: Box::new(receipt),
                    receipt_path,
                })
            }
        }
    }
}

#[derive(Debug, Clone)]
pub struct HandoffCandidate {
    pub host: HostKind,
    pub direction: SyncDirection,
    pub session_id: String,
    pub title: String,
    pub updated_at_iso: String,
    pub turn_or_step_count: usize,
    pub already_handed_ready: bool,
    pub handed_target_host: Option<String>,
    pub handed_target_id: Option<String>,
    pub is_untouched_baton_target: bool,
    pub chain_id: Option<String>,
    pub chain_depth: i64,
    pub sort_epoch: f64,
    pub ready_receipt: Option<SyncReceipt>,
}

#[derive(Debug, Clone)]
pub struct ResolvedHandoffRequest {
    pub binding: WorkspaceProjectBinding,
    pub direction: SyncDirection,
    pub source_id: String,
    pub title: Option<String>,
    pub selection_mode: String,
    pub chain_id: Option<String>,
    pub chained_from_source_id: Option<String>,
    pub chain_depth: i64,
    pub already_ready_receipt: Option<SyncReceipt>,
    pub paired_now: bool,
    pub pairing_mode: String,
}

/// Validate mutual exclusivity of `sync` CLI selector flags.
///
/// # Errors
/// Returns [`HostError::InvalidArgument`] on invalid flag combinations.
pub fn validate_handoff_selector_flags(
    source_id: Option<&str>,
    latest: bool,
    chain: bool,
    pick: bool,
    require_selector: bool,
) -> Result<(), HostError> {
    if chain && (latest || pick) {
        return Err(HostError::InvalidArgument(
            "--chain cannot be combined with --latest or --pick.".to_owned(),
        ));
    }
    if source_id.is_some() && (latest || pick) {
        return Err(HostError::InvalidArgument(
            "--source-id cannot be combined with --latest or --pick.".to_owned(),
        ));
    }
    if latest && pick {
        return Err(HostError::InvalidArgument(
            "--latest cannot be combined with --pick.".to_owned(),
        ));
    }
    if require_selector && source_id.is_none() && !latest && !chain && !pick {
        return Err(HostError::InvalidArgument(
            "Specify source_id, latest, chain, or pick for sync.".to_owned(),
        ));
    }
    Ok(())
}

fn read_ledger_for_chain(
    paths: &HostEnvironmentPaths,
    binding: &WorkspaceProjectBinding,
    direction: SyncDirection,
    sid: &str,
    propagate_active: bool,
    include_interrupted: bool,
) -> Result<Option<TrajectoryLedger>, HostError> {
    match read_handoff_source(paths, binding, direction, sid, include_interrupted) {
        Ok(l) => Ok(Some(l)),
        Err(HostError::FileNotFound(_)) => Ok(None),
        Err(HostError::InvalidArgument(msg)) => {
            let lower = msg.to_ascii_lowercase();
            if propagate_active
                && (lower.contains("active")
                    || lower.contains("incomplete")
                    || lower.contains("locked"))
            {
                Err(HostError::InvalidArgument(msg))
            } else {
                Ok(None)
            }
        }
        Err(_) => Ok(None),
    }
}

fn did_target_advance_beyond_seed(
    paths: &HostEnvironmentPaths,
    binding: &WorkspaceProjectBinding,
    receipt: &SyncReceipt,
    propagate_active: bool,
    include_interrupted: bool,
) -> Result<Option<bool>, HostError> {
    let Some(target_id) = receipt.target_id() else {
        return Ok(None);
    };
    let t_dir = if receipt.target_host == HostKind::Antigravity.as_str() {
        SyncDirection::AgyToCodex
    } else {
        SyncDirection::CodexToAgy
    };
    let Some(t_ledger) = read_ledger_for_chain(
        paths,
        binding,
        t_dir,
        target_id,
        propagate_active,
        include_interrupted,
    )?
    else {
        return Ok(None);
    };
    let (t_turns, t_digest) = ledger_seed_fingerprint(&t_ledger);
    if let (Some(sc), Some(sd)) = (
        receipt.target_seed_turn_count,
        receipt.target_seed_text_digest.as_deref(),
    ) {
        if t_turns == sc && t_digest == sd {
            return Ok(Some(false));
        }
        return Ok(Some(t_turns > sc || (t_turns == sc && t_digest != sd)));
    }
    let s_dir = if receipt.source_host == HostKind::Codex.as_str() {
        SyncDirection::CodexToAgy
    } else {
        SyncDirection::AgyToCodex
    };
    let Some(s_ledger) = read_ledger_for_chain(
        paths,
        binding,
        s_dir,
        &receipt.source_id,
        false,
        include_interrupted,
    )?
    else {
        return Ok(None);
    };
    let Some(matched_wm) = s_ledger
        .completed_turn_watermarks
        .iter()
        .find(|wm| wm.boundary_digest.as_str() == receipt.source_digest)
    else {
        return Ok(None);
    };
    let slice_end = (matched_wm.last_ledger_seq.0 + 1).min(s_ledger.events.len());
    let seeded_src = TrajectoryLedger::from_events(
        s_ledger.workspace_path.clone(),
        s_ledger.workspace_uri.clone(),
        &s_ledger.events[..slice_end],
        Some(&s_ledger.title),
        false,
    );
    let hl = handoff_ledger(&seeded_src, None);
    let (seed_turns, seed_dig) = ledger_seed_fingerprint(&hl);
    if t_turns == seed_turns && t_digest == seed_dig {
        Ok(Some(false))
    } else {
        Ok(Some(
            t_turns > seed_turns || (t_turns == seed_turns && t_digest != seed_dig),
        ))
    }
}

/// List non-subagent workspace candidates newest-first without mutating host or receipt state.
#[must_use]
pub fn list_handoff_candidates(
    paths: &HostEnvironmentPaths,
    binding: &WorkspaceProjectBinding,
    direction: Option<SyncDirection>,
    include_interrupted: bool,
) -> Vec<HandoffCandidate> {
    let ws_path = binding.parsed_path();
    let ws_uri = binding.parsed_uri();
    let receipts = load_workspace_receipts(paths, &ws_path);
    let mut ready_by_source: HashMap<(String, String), SyncReceipt> = HashMap::new();
    let mut ready_by_target: HashMap<(String, String), SyncReceipt> = HashMap::new();

    for (_, rec, _) in &receipts {
        if rec.status_str() == "ready" {
            ready_by_source
                .entry((rec.source_host.clone(), rec.source_id.clone()))
                .and_modify(|cur| {
                    if rec.created_at_iso > cur.created_at_iso {
                        *cur = rec.clone();
                    }
                })
                .or_insert_with(|| rec.clone());
            if let Some(tid) = rec.target_id() {
                ready_by_target
                    .entry((rec.target_host.clone(), tid.to_owned()))
                    .and_modify(|cur| {
                        if rec.created_at_iso > cur.created_at_iso {
                            *cur = rec.clone();
                        }
                    })
                    .or_insert_with(|| rec.clone());
            }
        }
    }

    let preloaded = load_preloaded_host_tables(paths);
    let mut cands = Vec::new();

    if direction.is_none() || direction == Some(SyncDirection::AgyToCodex) {
        for a in discover_agy_entries(paths, Some((&ws_path, &ws_uri)), Some(&preloaded)) {
            if a.is_subagent {
                continue;
            }
            let cid_str = a.cascade_id.as_str().to_owned();
            let src_rec = ready_by_source.get(&("antigravity".to_owned(), cid_str.clone()));
            let tgt_rec = ready_by_target.get(&("antigravity".to_owned(), cid_str.clone()));
            let untouched = tgt_rec.is_some_and(|r| {
                did_target_advance_beyond_seed(paths, binding, r, false, include_interrupted)
                    .ok()
                    .flatten()
                    == Some(false)
            });
            let (handed_ready, handed_th, handed_tid) = if let Some(r) = src_rec {
                let is_same = read_ledger_for_chain(
                    paths,
                    binding,
                    SyncDirection::AgyToCodex,
                    &cid_str,
                    false,
                    include_interrupted,
                )
                .ok()
                .flatten()
                .and_then(|l| {
                    l.latest_watermark()
                        .map(|w| w.boundary_digest.as_str().to_owned())
                })
                .is_some_and(|dig| dig == r.source_digest);
                if is_same {
                    (
                        true,
                        Some(r.target_host.clone()),
                        r.target_id().map(str::to_owned),
                    )
                } else {
                    (false, None, None)
                }
            } else {
                (false, None, None)
            };
            cands.push(HandoffCandidate {
                host: HostKind::Antigravity,
                direction: SyncDirection::AgyToCodex,
                session_id: cid_str,
                title: a.title,
                updated_at_iso: a.updated_at_iso,
                turn_or_step_count: a.step_count as usize,
                already_handed_ready: handed_ready,
                handed_target_host: handed_th,
                handed_target_id: handed_tid,
                is_untouched_baton_target: untouched,
                chain_id: src_rec
                    .and_then(|r| r.chain_id.clone())
                    .or_else(|| tgt_rec.and_then(|r| r.chain_id.clone())),
                chain_depth: src_rec.map_or_else(
                    || tgt_rec.map_or(0, |r| r.chain_depth + 1),
                    |r| r.chain_depth,
                ),
                sort_epoch: a.sort_epoch,
                ready_receipt: handed_ready.then(|| src_rec.cloned()).flatten(),
            });
        }
    }

    if direction.is_none() || direction == Some(SyncDirection::CodexToAgy) {
        for c in discover_codex_entries(paths, Some((&ws_path, &ws_uri)), Some(&preloaded)) {
            if c.is_subagent {
                continue;
            }
            let tid_str = c.thread_id.as_str().to_owned();
            let src_rec = ready_by_source.get(&("codex".to_owned(), tid_str.clone()));
            let tgt_rec = ready_by_target.get(&("codex".to_owned(), tid_str.clone()));
            let untouched = tgt_rec.is_some_and(|r| {
                did_target_advance_beyond_seed(paths, binding, r, false, include_interrupted)
                    .ok()
                    .flatten()
                    == Some(false)
            });
            let (handed_ready, handed_th, handed_tid) = if let Some(r) = src_rec {
                let is_same = read_ledger_for_chain(
                    paths,
                    binding,
                    SyncDirection::CodexToAgy,
                    &tid_str,
                    false,
                    include_interrupted,
                )
                .ok()
                .flatten()
                .and_then(|l| {
                    l.latest_watermark()
                        .map(|w| w.boundary_digest.as_str().to_owned())
                })
                .is_some_and(|dig| dig == r.source_digest);
                if is_same {
                    (
                        true,
                        Some(r.target_host.clone()),
                        r.target_id().map(str::to_owned),
                    )
                } else {
                    (false, None, None)
                }
            } else {
                (false, None, None)
            };
            cands.push(HandoffCandidate {
                host: HostKind::Codex,
                direction: SyncDirection::CodexToAgy,
                session_id: tid_str,
                title: c.title,
                updated_at_iso: c.updated_at_iso,
                turn_or_step_count: c.turn_count,
                already_handed_ready: handed_ready,
                handed_target_host: handed_th,
                handed_target_id: handed_tid,
                is_untouched_baton_target: untouched,
                chain_id: src_rec
                    .and_then(|r| r.chain_id.clone())
                    .or_else(|| tgt_rec.and_then(|r| r.chain_id.clone())),
                chain_depth: src_rec.map_or_else(
                    || tgt_rec.map_or(0, |r| r.chain_depth + 1),
                    |r| r.chain_depth,
                ),
                sort_epoch: c.sort_epoch,
                ready_receipt: handed_ready.then(|| src_rec.cloned()).flatten(),
            });
        }
    }

    cands.sort_by(|a, b| {
        b.sort_epoch
            .partial_cmp(&a.sort_epoch)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.host.as_str().cmp(b.host.as_str()))
            .then_with(|| a.session_id.cmp(&b.session_id))
    });
    cands
}

fn ensure_binding_for_handoff(
    paths: &HostEnvironmentPaths,
    workspace: &str,
    policy: PairingPolicy,
) -> Result<(WorkspaceProjectBinding, bool, String), HostError> {
    let diag = diagnose_workspace_binding(paths, workspace)?;
    if diag.is_paired() {
        let b = ProjectRegistry::new(paths.clone()).resolve_readonly_binding(workspace)?;
        return Ok((b, false, "already_paired".to_owned()));
    }
    if policy == PairingPolicy::RequirePaired {
        return Err(HostError::UnpairedWorkspace {
            message: format!(
                "Workspace {:?} is not paired on both Antigravity and Codex.",
                diag.workspace_path
            ),
            diagnosis: serde_json::to_value(&diag)?,
            remediation: diag.remediation_steps,
        });
    }
    let prefer_online = policy == PairingPolicy::AutoPair;
    let guard = PresenceWalGuard::new(paths.clone());
    let registry = ProjectRegistry::new(paths.clone());
    let (binding, mode) =
        pair_workspace_with_mode(paths, &guard, &registry, workspace, prefer_online)?;
    Ok((binding, true, mode))
}

struct ChainTargetResolution {
    direction: SyncDirection,
    source_id: String,
    chain_id: Option<String>,
    chained_from_source_id: Option<String>,
    chain_depth: i64,
    ready_receipt: Option<SyncReceipt>,
}

fn resolve_chain_target(
    paths: &HostEnvironmentPaths,
    binding: &WorkspaceProjectBinding,
    anchor_source_id: Option<&str>,
    explicit_direction: Option<SyncDirection>,
    include_interrupted: bool,
) -> Result<ChainTargetResolution, HostError> {
    let ws_path = binding.parsed_path();
    let receipts = load_workspace_receipts(paths, &ws_path);
    let mut ready: Vec<(SyncReceipt, f64)> = receipts
        .into_iter()
        .filter(|(_, r, _)| r.status_str() == "ready" && r.target_id().is_some())
        .map(|(_, r, m)| (r, m))
        .collect();
    if ready.is_empty() {
        return Err(HostError::InvalidArgument(
            "No prior ready sync receipt exists in this workspace to --chain from.".to_owned(),
        ));
    }

    let seed_receipt = if let Some(sid) = anchor_source_id {
        let sid_clean = validate_session_id(sid, "source_id")?;
        ready
            .iter()
            .filter(|(r, _)| r.source_id == sid_clean || r.target_id() == Some(sid_clean))
            .max_by(|(a, ma), (b, mb)| {
                a.chain_depth
                    .cmp(&b.chain_depth)
                    .then_with(|| a.created_at_iso.cmp(&b.created_at_iso))
                    .then_with(|| ma.partial_cmp(mb).unwrap_or(std::cmp::Ordering::Equal))
            })
            .map(|(r, _)| r.clone())
            .ok_or_else(|| {
                HostError::InvalidArgument(format!(
                    "No ready sync receipt found matching --source-id {sid_clean:?} for --chain."
                ))
            })?
    } else {
        ready
            .iter()
            .max_by(|(a, ma), (b, mb)| {
                a.created_at_iso
                    .cmp(&b.created_at_iso)
                    .then_with(|| a.chain_depth.cmp(&b.chain_depth))
                    .then_with(|| ma.partial_cmp(mb).unwrap_or(std::cmp::Ordering::Equal))
            })
            .map(|(r, _)| r.clone())
            .unwrap_or_else(|| unreachable!())
    };

    let mut by_source: HashMap<(String, String), SyncReceipt> = HashMap::new();
    for (r, _) in ready.drain(..) {
        by_source
            .entry((r.source_host.clone(), r.source_id.clone()))
            .and_modify(|cur| {
                if r.created_at_iso > cur.created_at_iso {
                    *cur = r.clone();
                }
            })
            .or_insert(r);
    }

    let mut cur = seed_receipt;
    let mut visited = HashSet::new();
    while let Some(tid) = cur.target_id() {
        if !visited.insert((cur.source_host.clone(), cur.source_id.clone())) {
            break;
        }
        if let Some(next_r) = by_source.get(&(cur.target_host.clone(), tid.to_owned())) {
            cur = next_r.clone();
        } else {
            break;
        }
    }

    let tip_tid = cur
        .target_id()
        .ok_or_else(|| HostError::Runtime("Chain tip receipt missing target_id.".to_owned()))?
        .to_owned();
    let tip_dir = if cur.target_host == HostKind::Antigravity.as_str() {
        SyncDirection::AgyToCodex
    } else {
        SyncDirection::CodexToAgy
    };
    if let Some(exp) = explicit_direction
        && exp != tip_dir
    {
        return Err(HostError::InvalidArgument(format!(
            "Chain tip {} is on {} so next hop requires {}, contradicting --direction {}.",
            tip_tid,
            cur.target_host,
            tip_dir.as_str(),
            exp.as_str()
        )));
    }

    let chain_id = cur.chain_id.clone().or_else(|| {
        Some(deterministic_uuid(
            "handoff-chain",
            &format!("{}:{}:{}", cur.workspace, cur.source_host, cur.source_id),
        ))
    });

    match did_target_advance_beyond_seed(paths, binding, &cur, true, include_interrupted)? {
        Some(true) => Ok(ChainTargetResolution {
            direction: tip_dir,
            source_id: tip_tid,
            chain_id,
            chained_from_source_id: Some(cur.source_id.clone()),
            chain_depth: cur.chain_depth + 1,
            ready_receipt: None,
        }),
        Some(false) => Ok(ChainTargetResolution {
            direction: tip_dir,
            source_id: tip_tid,
            chain_id,
            chained_from_source_id: Some(cur.source_id.clone()),
            chain_depth: cur.chain_depth + 1,
            ready_receipt: Some(cur),
        }),
        None => Err(HostError::FileNotFound(format!(
            "Chain tip target session {tip_tid:?} on {} could not be read.",
            cur.target_host
        ))),
    }
}

/// Resolve CLI/API sync UX options into a concrete [`ResolvedHandoffRequest`].
///
/// # Errors
/// Returns [`HostError`] on invalid flags, missing sessions, or unpaired workspace.
#[allow(clippy::too_many_arguments)]
pub fn resolve_handoff_request(
    paths: &HostEnvironmentPaths,
    workspace: &str,
    direction: Option<SyncDirection>,
    source_id: Option<&str>,
    title: Option<&str>,
    latest: bool,
    chain: bool,
    pick: bool,
    pairing_policy: PairingPolicy,
    include_interrupted: bool,
) -> Result<ResolvedHandoffRequest, HostError> {
    validate_handoff_selector_flags(source_id, latest, chain, pick, false)?;
    let (binding, paired_now, pairing_mode) =
        ensure_binding_for_handoff(paths, workspace, pairing_policy)?;

    if chain {
        let res = resolve_chain_target(paths, &binding, source_id, direction, include_interrupted)?;
        return Ok(ResolvedHandoffRequest {
            binding,
            direction: res.direction,
            source_id: res.source_id,
            title: title.map(str::to_owned),
            selection_mode: "chain".to_owned(),
            chain_id: res.chain_id,
            chained_from_source_id: res.chained_from_source_id,
            chain_depth: res.chain_depth,
            already_ready_receipt: res.ready_receipt,
            paired_now,
            pairing_mode,
        });
    }

    if let Some(raw_sid) = source_id {
        let sid = validate_session_id(raw_sid, "source_id")?.to_owned();
        let eff_dir = match direction {
            Some(d) => d,
            None => match infer_direction_from_source_id(paths, &sid, None)? {
                HostKind::Antigravity => SyncDirection::AgyToCodex,
                HostKind::Codex => SyncDirection::CodexToAgy,
            },
        };
        assert_source_belongs_to_binding(
            paths,
            if eff_dir == SyncDirection::AgyToCodex {
                HostKind::Antigravity
            } else {
                HostKind::Codex
            },
            &sid,
            &binding.parsed_path(),
            &binding.parsed_uri(),
            None,
        )?;
        return Ok(ResolvedHandoffRequest {
            binding,
            direction: eff_dir,
            source_id: sid,
            title: title.map(str::to_owned),
            selection_mode: "source_id".to_owned(),
            chain_id: None,
            chained_from_source_id: None,
            chain_depth: 0,
            already_ready_receipt: None,
            paired_now,
            pairing_mode,
        });
    }

    if !latest {
        return Err(HostError::InvalidArgument(
            "Interactive session picker (--pick) requires a TTY. Pass --latest, --chain, or --source-id explicitly (or run in a TTY with --pick)."
                .to_owned(),
        ));
    }

    let cands = list_handoff_candidates(paths, &binding, direction, include_interrupted);
    let picked = cands
        .iter()
        .find(|c| !c.is_untouched_baton_target)
        .or_else(|| cands.first())
        .ok_or_else(|| {
            HostError::FileNotFound(format!(
                "No non-subagent candidate sessions found in workspace {:?}.",
                binding.workspace_path
            ))
        })?;

    Ok(ResolvedHandoffRequest {
        binding,
        direction: picked.direction,
        source_id: picked.session_id.clone(),
        title: title.map(str::to_owned),
        selection_mode: "latest".to_owned(),
        chain_id: None,
        chained_from_source_id: None,
        chain_depth: 0,
        already_ready_receipt: picked.ready_receipt.clone(),
        paired_now,
        pairing_mode,
    })
}

/// Execute a resolved handoff request, returning a `no_op` receipt immediately when baton is unchanged.
///
/// # Errors
/// Propagates [`handoff_session`] errors.
pub fn execute_handoff(
    paths: &HostEnvironmentPaths,
    resolved: &ResolvedHandoffRequest,
    include_interrupted: bool,
) -> Result<SyncReceipt, HostError> {
    if let Some(ref rec) = resolved.already_ready_receipt {
        let mut out = rec.clone();
        out.pairing_mode = Some(resolved.pairing_mode.clone());
        return Ok(out);
    }
    handoff_session(
        paths,
        &resolved.binding.workspace_path,
        resolved.direction,
        &resolved.source_id,
        resolved.title.as_deref(),
        include_interrupted,
        resolved.chain_id.as_deref(),
        resolved.chained_from_source_id.as_deref(),
        resolved.chain_depth,
        Some(&resolved.pairing_mode),
    )
}

#[derive(Debug, Clone, Serialize)]
pub struct ReconcileEntry {
    pub receipt_file: String,
    pub source_host: String,
    pub source_id: String,
    pub target_host: String,
    pub target_id: Option<String>,
    pub title: String,
    pub reason: String,
    pub staging_cleaned: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct ReconcileReport {
    pub workspace: String,
    pub dry_run: bool,
    pub promoted: Vec<ReconcileEntry>,
    pub cleared: Vec<ReconcileEntry>,
    pub unresolved: Vec<ReconcileEntry>,
    pub total_pending_before: usize,
    pub total_pending_after: usize,
}

fn staged_agy_step_count(
    paths: &HostEnvironmentPaths,
    staging_root: &Path,
    receipt: &SyncReceipt,
    target_id: &str,
) -> Option<usize> {
    let st_resolved = resolve_strict_false(Path::new(receipt.staging_path()));
    let root_resolved = resolve_strict_false(staging_root);
    if !st_resolved.starts_with(&root_resolved) {
        return None;
    }
    let staged_db = validated_child_path(
        &st_resolved,
        &["conversations", &format!("{target_id}.db")],
        "target_id",
    )
    .ok()?;
    if !staged_db.is_file() {
        return None;
    }
    DefaultHostTransport::new(paths.clone())
        .read_persisted_agy_steps(&staged_db)
        .ok()
        .map(|s| s.len())
}

fn verify_agy_pending_receipt(
    paths: &HostEnvironmentPaths,
    ws_path: &WorkspacePath,
    ws_uri: &WorkspaceRootUri,
    receipt: &SyncReceipt,
    staging_root: &Path,
) -> (&'static str, String) {
    let Some(raw_tid) = receipt.target_id() else {
        return ("cleared", "missing_target_id".to_owned());
    };
    let Ok(target_id) = validate_session_id(raw_tid, "target_id") else {
        return ("cleared", "invalid_target_id".to_owned());
    };
    let Ok(target_cid) = CascadeId::parse(target_id) else {
        return ("cleared", "invalid_target_id".to_owned());
    };

    let expected_steps = staged_agy_step_count(paths, staging_root, receipt, target_id);
    let guard = PresenceWalGuard::new(paths.clone());
    let agy_ep = guard.discover_agy_daemon();
    let mut rpc_failed = false;

    if agy_ep.is_some() {
        match fetch_agy_trajectory_via_rpc(agy_ep.as_ref(), &target_cid, 10.0, true, None) {
            Ok(rpc_res) => {
                if !rpc_res.raw_steps.is_empty() {
                    if let Some(exp) = expected_steps
                        && rpc_res.raw_steps.len() >= exp
                    {
                        return ("promoted", "agy_rpc_step_count_matched".to_owned());
                    }
                    if expected_steps.is_none()
                        && let Ok(decoded) = decode_agy_trajectory(
                            ws_path.clone(),
                            ws_uri.clone(),
                            &rpc_res.raw_steps,
                            None,
                        )
                    {
                        let (turns, _) = ledger_seed_fingerprint(&decoded);
                        if receipt
                            .target_seed_turn_count
                            .is_none_or(|req| turns >= req)
                        {
                            return ("promoted", "agy_rpc_verified".to_owned());
                        }
                    }
                }
            }
            Err(_) => {
                rpc_failed = true;
            }
        }
    }

    let Ok(target_db) = validated_child_path(
        &paths.agy_data_dir,
        &["conversations", &format!("{target_id}.db")],
        "target_id",
    ) else {
        return ("cleared", "invalid_target_id".to_owned());
    };
    let Ok(target_annot) = validated_child_path(
        &paths.agy_data_dir,
        &["annotations", &format!("{target_id}.pbtxt")],
        "target_id",
    ) else {
        return ("cleared", "invalid_target_id".to_owned());
    };

    if target_db.is_file() && target_annot.is_file() {
        let Ok(disk_steps) =
            DefaultHostTransport::new(paths.clone()).read_persisted_agy_steps(&target_db)
        else {
            return ("unresolved", "agy_target_db_unreadable".to_owned());
        };
        if let Some(exp) = expected_steps {
            if !disk_steps.is_empty() && disk_steps.len() >= exp {
                return ("promoted", "agy_disk_step_count_matched".to_owned());
            }
            return ("unresolved", "agy_disk_step_count_mismatch".to_owned());
        }
        let Ok(decoded) = decode_agy_trajectory(ws_path.clone(), ws_uri.clone(), &disk_steps, None)
        else {
            return ("unresolved", "agy_target_db_unreadable".to_owned());
        };
        let (turns, _) = ledger_seed_fingerprint(&decoded);
        if !disk_steps.is_empty()
            && receipt
                .target_seed_turn_count
                .is_none_or(|req| turns >= req)
        {
            return ("promoted", "agy_disk_verified".to_owned());
        }
        return ("unresolved", "agy_disk_seed_mismatch".to_owned());
    }
    if target_db.exists() || target_annot.exists() {
        return ("unresolved", "agy_partial_target_files".to_owned());
    }
    if rpc_failed
        && guard
            .probe_locks(Some(&target_cid), None, false, false, false)
            .is_ok_and(|l| l.agy_presence_locked || l.agy_wal_active)
    {
        return ("unresolved", "agy_daemon_active_rpc_unavailable".to_owned());
    }
    ("cleared", "agy_target_absent".to_owned())
}

fn codex_state_row_exists(state_db: &Path, target_id: &str) -> Option<bool> {
    if !state_db.is_file() {
        return Some(false);
    }
    let conn = open_readonly_sqlite(state_db).ok()?;
    let exists = conn
        .query_row("SELECT 1 FROM threads WHERE id = ?", [target_id], |_| {
            Ok(true)
        })
        .unwrap_or(false);
    Some(exists)
}

fn codex_orphan_matches_receipt(
    codex_home_dir: &Path,
    ws_path: &WorkspacePath,
    receipt: &SyncReceipt,
) -> Option<bool> {
    let state_db = codex_home_dir.join("state_5.sqlite");
    if !state_db.is_file() {
        return Some(false);
    }
    let (receipt_epoch, _) = parse_ts_epoch_iso(
        (!receipt.created_at_iso.is_empty()).then_some(receipt.created_at_iso.as_str()),
        None,
        0.0,
    );
    let conn = open_readonly_sqlite(&state_db).ok()?;
    let cols = table_column_names(&conn, "threads").ok()?;
    if !cols.contains("id") || !cols.contains("title") {
        return None;
    }
    let desired = ["id", "cwd", "title", "created_at", "created_at_ms"];
    let select_sql = optional_select_columns_sql(&cols, &desired);
    let idx_map: HashMap<&str, usize> = desired.iter().enumerate().map(|(i, c)| (*c, i)).collect();
    let sql = format!("SELECT {select_sql} FROM threads WHERE id != ? AND title = ?");
    let mut stmt = conn.prepare(&sql).ok()?;
    let mut rows = stmt
        .query(rusqlite::params![receipt.source_id, receipt.title])
        .ok()?;
    while let Ok(Some(row)) = rows.next() {
        let cwd = idx_map
            .get("cwd")
            .and_then(|&i| row.get::<_, Option<String>>(i).ok().flatten())
            .unwrap_or_default();
        if !cwd.trim().is_empty() && parse_ws_ident(cwd.trim()).is_ok_and(|(p, _)| &p != ws_path) {
            continue;
        }
        let c_ms = idx_map
            .get("created_at_ms")
            .and_then(|&i| row.get::<_, Option<i64>>(i).ok().flatten());
        let c_sec = idx_map
            .get("created_at")
            .and_then(|&i| row.get::<_, Option<i64>>(i).ok().flatten());
        if c_ms.is_none() && c_sec.is_none() {
            return Some(true);
        }
        let raw_num = c_ms.or(c_sec).unwrap_or(0);
        let (row_epoch, _) = parse_ts_epoch_iso(None, Some(raw_num as f64), 0.0);
        if receipt_epoch <= 0.0 || row_epoch >= receipt_epoch - 5.0 {
            return Some(true);
        }
    }
    Some(false)
}

fn verify_codex_pending_receipt(
    paths: &HostEnvironmentPaths,
    ws_path: &WorkspacePath,
    ws_uri: &WorkspaceRootUri,
    receipt: &SyncReceipt,
) -> (&'static str, String) {
    let Some(raw_tid) = receipt.target_id() else {
        return match codex_orphan_matches_receipt(&paths.codex_home_dir, ws_path, receipt) {
            None => ("unresolved", "codex_state_db_unreadable".to_owned()),
            Some(true) => (
                "unresolved",
                "codex_candidate_thread_found_without_id".to_owned(),
            ),
            Some(false) => ("cleared", "codex_target_never_allocated".to_owned()),
        };
    };
    let Ok(target_id) = validate_session_id(raw_tid, "target_id") else {
        return ("cleared", "invalid_target_id".to_owned());
    };
    let Ok(target_tid) = CodexThreadId::parse(target_id) else {
        return ("cleared", "invalid_target_id".to_owned());
    };

    let guard = PresenceWalGuard::new(paths.clone());
    let Ok(locks) = guard.probe_locks(None, Some(&target_tid), false, false, false) else {
        return ("cleared", "invalid_target_id".to_owned());
    };

    let mut rpc_failed = false;
    if let Some(ref ep) = locks.codex_daemon
        && has_live_unix_socket(Some(&ep.socket_path))
    {
        match fetch_codex_thread_via_rpc(Some(ep), &target_tid, 10.0) {
            Ok(rpc_res) => {
                if let Ok(decoded) = decode_codex_rpc_thread_to_ledger(
                    ws_path.clone(),
                    ws_uri.clone(),
                    &rpc_res.thread,
                    None,
                ) {
                    let (turns, _) = ledger_seed_fingerprint(&decoded);
                    if !decoded.events.is_empty()
                        && receipt
                            .target_seed_turn_count
                            .is_none_or(|req| turns >= req)
                    {
                        return ("promoted", "codex_rpc_verified".to_owned());
                    }
                    return ("unresolved", "codex_rpc_empty_or_incomplete".to_owned());
                }
            }
            Err(_) => {
                rpc_failed = true;
            }
        }
    }

    let transport = DefaultHostTransport::new(paths.clone());
    let (mut rollout_path, mut explicit_title) = transport
        .probe_codex_rollout_and_title(&target_tid)
        .unwrap_or((None, None));
    if rollout_path.is_none()
        && let Ok((rp, t)) = transport.find_codex_rollout_and_title(&target_tid)
    {
        rollout_path = Some(rp);
        explicit_title = t;
    }

    let Some(in_catalog) =
        codex_state_row_exists(&paths.codex_home_dir.join("state_5.sqlite"), target_id)
    else {
        return ("unresolved", "codex_state_db_unreadable".to_owned());
    };

    if let Some(ref rp) = rollout_path
        && rp.is_file()
        && in_catalog
    {
        let Ok(lines) = read_codex_rollout_lines(rp) else {
            return ("unresolved", "codex_rollout_unreadable".to_owned());
        };
        let Ok(decoded) = decode_codex_rollout(
            ws_path.clone(),
            ws_uri.clone(),
            &lines,
            explicit_title.as_deref(),
        ) else {
            return ("unresolved", "codex_rollout_unreadable".to_owned());
        };
        let (turns, _) = ledger_seed_fingerprint(&decoded);
        if !decoded.events.is_empty()
            && receipt
                .target_seed_turn_count
                .is_none_or(|req| turns >= req)
        {
            return ("promoted", "codex_disk_verified".to_owned());
        }
        return ("unresolved", "codex_disk_seed_mismatch".to_owned());
    }
    if rollout_path.as_ref().is_some_and(|p| p.is_file()) || in_catalog {
        return ("unresolved", "codex_partial_target_state".to_owned());
    }
    if rpc_failed && locks.codex_wal_active {
        return (
            "unresolved",
            "codex_daemon_wal_active_rpc_unavailable".to_owned(),
        );
    }
    ("cleared", "codex_target_absent".to_owned())
}

/// Verify pending handoff receipts against target hosts and promote ready or clear absent entries.
///
/// # Errors
/// Returns [`HostError`] on invalid arguments, lock contention, or I/O errors.
pub fn reconcile_workspace_handoffs(
    paths: &HostEnvironmentPaths,
    workspace: &str,
    source_id: Option<&str>,
    dry_run: bool,
) -> Result<ReconcileReport, HostError> {
    let (ws_path, ws_uri) = parse_ws_ident(workspace)?;
    if let Some(sid) = source_id {
        validate_session_id(sid, "source_id")?;
    }

    let mut promoted = Vec::new();
    let mut cleared = Vec::new();
    let mut unresolved = Vec::new();

    let (handoffs_root, _lock_guard) = open_handoff_lock(paths, ws_path.as_path())?;
    let staging_root = handoffs_root.join("staging");
    let loaded = load_workspace_receipts(paths, &ws_path);
    let pending: Vec<(PathBuf, SyncReceipt)> = loaded
        .into_iter()
        .filter(|(_, rec, _)| {
            rec.status_str() == "pending" && source_id.is_none_or(|sid| rec.source_id == sid)
        })
        .map(|(p, r, _)| (p, r))
        .collect();
    let total_before = pending.len();

    for (r_path, _) in &pending {
        let Ok(fresh) = load_receipt_file(r_path) else {
            continue;
        };
        if fresh.status_str() != "pending" {
            continue;
        }
        let (decision, reason) = if fresh.target_host == HostKind::Antigravity.as_str() {
            verify_agy_pending_receipt(paths, &ws_path, &ws_uri, &fresh, &staging_root)
        } else if fresh.target_host == HostKind::Codex.as_str() {
            verify_codex_pending_receipt(paths, &ws_path, &ws_uri, &fresh)
        } else {
            (
                "unresolved",
                format!("unsupported_target_host:{}", fresh.target_host),
            )
        };

        let mut entry = ReconcileEntry {
            receipt_file: r_path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_owned(),
            source_host: fresh.source_host.clone(),
            source_id: fresh.source_id.clone(),
            target_host: fresh.target_host.clone(),
            target_id: fresh.target_id().map(str::to_owned),
            title: fresh.title.clone(),
            reason,
            staging_cleaned: false,
        };

        let bucket = if dry_run {
            decision
        } else if decision == "promoted" {
            let tid = fresh.target_id().unwrap_or("").to_owned();
            let updated = SyncReceipt {
                state: ReceiptState::Ready {
                    target_id: tid,
                    staging_path: fresh.staging_path().to_owned(),
                },
                ..fresh.clone()
            };
            save_receipt_file(r_path, &updated)?;
            entry.staging_cleaned =
                cleanup_handoff_staging_dir(&staging_root, fresh.staging_path());
            "promoted"
        } else if decision == "cleared" {
            entry.staging_cleaned =
                cleanup_handoff_staging_dir(&staging_root, fresh.staging_path());
            let _ = fs::remove_file(r_path);
            "cleared"
        } else {
            "unresolved"
        };

        match bucket {
            "promoted" => promoted.push(entry),
            "cleared" => cleared.push(entry),
            _ => unresolved.push(entry),
        }
    }

    let total_after = load_workspace_receipts(paths, &ws_path)
        .into_iter()
        .filter(|(_, rec, _)| {
            rec.status_str() == "pending" && source_id.is_none_or(|sid| rec.source_id == sid)
        })
        .count();

    let _ = (read_codex_rollout_lines, json!({}));

    Ok(ReconcileReport {
        workspace: ws_path.to_string(),
        dry_run,
        promoted,
        cleared,
        unresolved,
        total_pending_before: total_before,
        total_pending_after: total_after,
    })
}

#[derive(Debug, Clone, Serialize)]
pub struct StatusReceiptItem {
    pub source_host: String,
    pub source_id: String,
    pub target_host: String,
    pub target_id: Option<String>,
    pub status: String,
    pub title: String,
    pub created_at_iso: String,
    pub chain_depth: i64,
    pub chained_from_source_id: Option<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct StatusPendingItem {
    pub source_host: String,
    pub source_id: String,
    pub target_host: String,
    pub target_id: Option<String>,
    pub status: String,
    pub error: Option<String>,
    pub created_at_iso: String,
    pub title: String,
    pub receipt_file: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct StatusChainItem {
    pub chain_id: String,
    pub tip_status: String,
    pub tip_source_host: String,
    pub tip_source_id: String,
    pub tip_target_host: String,
    pub tip_target_id: Option<String>,
    pub depth: i64,
    pub hop_count: usize,
    pub updated_at_iso: String,
    pub receipts: Vec<StatusReceiptItem>,
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkspaceHandoffsSummary {
    pub handoff_chains: Vec<StatusChainItem>,
    pub pending_handoffs: Vec<StatusPendingItem>,
}

/// Summarize grouped handoff chains and pending receipts for `status`.
///
/// # Errors
/// Returns [`HostError`] if `workspace` is invalid.
pub fn summarize_workspace_handoffs(
    paths: &HostEnvironmentPaths,
    workspace: &str,
) -> Result<WorkspaceHandoffsSummary, HostError> {
    let (ws_path, _) = parse_ws_ident(workspace)?;
    let loaded = load_workspace_receipts(paths, &ws_path);
    let mut grouped: BTreeMap<String, Vec<(PathBuf, SyncReceipt, f64)>> = BTreeMap::new();
    let mut pending_entries: Vec<((String, f64, String), StatusPendingItem)> = Vec::new();

    for (r_path, rec, mtime) in loaded {
        let stem = r_path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_owned();
        let fname = r_path
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_owned();
        let group_key = rec
            .chain_id
            .clone()
            .unwrap_or_else(|| format!("single:{stem}"));
        if rec.status_str() == "pending" {
            pending_entries.push((
                (rec.created_at_iso.clone(), mtime, fname.clone()),
                StatusPendingItem {
                    source_host: rec.source_host.clone(),
                    source_id: rec.source_id.clone(),
                    target_host: rec.target_host.clone(),
                    target_id: rec.target_id().map(str::to_owned),
                    status: rec.status_str().to_owned(),
                    error: rec.error().map(str::to_owned),
                    created_at_iso: rec.created_at_iso.clone(),
                    title: rec.title.clone(),
                    receipt_file: fname,
                },
            ));
        }
        grouped
            .entry(group_key)
            .or_default()
            .push((r_path, rec, mtime));
    }

    let mut chains: Vec<((String, f64, String), StatusChainItem)> = Vec::new();
    for (chain_key, mut items) in grouped {
        items.sort_by(|a, b| {
            a.1.chain_depth
                .cmp(&b.1.chain_depth)
                .then_with(|| a.1.created_at_iso.cmp(&b.1.created_at_iso))
                .then_with(|| a.2.partial_cmp(&b.2).unwrap_or(std::cmp::Ordering::Equal))
                .then_with(|| a.0.cmp(&b.0))
        });
        let tip = &items[items.len() - 1].1;
        let updated_at = items
            .iter()
            .map(|(_, r, _)| &r.created_at_iso)
            .filter(|s| !s.is_empty())
            .max()
            .cloned()
            .unwrap_or_else(|| tip.created_at_iso.clone());
        let max_mtime = items.iter().map(|(_, _, m)| *m).fold(0.0f64, f64::max);
        let receipts_out: Vec<StatusReceiptItem> = items
            .iter()
            .map(|(_, r, _)| StatusReceiptItem {
                source_host: r.source_host.clone(),
                source_id: r.source_id.clone(),
                target_host: r.target_host.clone(),
                target_id: r.target_id().map(str::to_owned),
                status: r.status_str().to_owned(),
                title: r.title.clone(),
                created_at_iso: r.created_at_iso.clone(),
                chain_depth: r.chain_depth,
                chained_from_source_id: r.chained_from_source_id.clone(),
                error: r.error().map(str::to_owned),
            })
            .collect();
        chains.push((
            (updated_at.clone(), max_mtime, chain_key.clone()),
            StatusChainItem {
                chain_id: tip.chain_id.clone().unwrap_or(chain_key),
                tip_status: tip.status_str().to_owned(),
                tip_source_host: tip.source_host.clone(),
                tip_source_id: tip.source_id.clone(),
                tip_target_host: tip.target_host.clone(),
                tip_target_id: tip.target_id().map(str::to_owned),
                depth: tip.chain_depth,
                hop_count: receipts_out.len(),
                updated_at_iso: updated_at,
                receipts: receipts_out,
            },
        ));
    }

    chains.sort_by(|a, b| {
        b.0.0
            .cmp(&a.0.0)
            .then_with(|| {
                b.0.1
                    .partial_cmp(&a.0.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .then_with(|| b.0.2.cmp(&a.0.2))
    });
    pending_entries.sort_by(|a, b| {
        b.0.0
            .cmp(&a.0.0)
            .then_with(|| {
                b.0.1
                    .partial_cmp(&a.0.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .then_with(|| b.0.2.cmp(&a.0.2))
    });

    Ok(WorkspaceHandoffsSummary {
        handoff_chains: chains.into_iter().map(|(_, c)| c).collect(),
        pending_handoffs: pending_entries.into_iter().map(|(_, p)| p).collect(),
    })
}
