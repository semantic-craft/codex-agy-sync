//! Dual-mode host transport reading via `?mode=ro` / loopback RPC and writing all offline stores atomically.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use cas_codecs::{
    AgyRawStepRow, TrajectoryMetadataEncodeParams, bind_agy_step, compress_zstd_rollout_bytes,
    decode_agy_trajectory, decode_cascade_summary_proto, decode_codex_rollout,
    decode_codex_rpc_thread_to_ledger, decode_summaries_state_proto, encode_agy_trajectory,
    encode_cascade_summary_proto, encode_codex_rollout, encode_string_field,
    encode_summaries_state_proto, encode_trajectory_metadata_proto, parse_annotation_pbtxt_title,
    read_codex_rollout_lines, render_annotation_pbtxt, render_transcript_row_jsonl,
    splice_rollout_completed_prefix,
};
use cas_core::{
    AgyGuiSafeSource, CanonicalEvent, CascadeId, CascadeSummaryProjection, CodexThreadId, HostKind,
    HostLockSnapshot, RolloutLineNo, StepIndex, SubagentLineage, SummariesStateProjection,
    TrajectoryId, TrajectoryLedger, TransportMode, generate_uuid7, resolve_strict_false,
    validate_session_id,
};
use chrono::Utc;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::error::HostError;
use crate::locks::PresenceWalGuard;
use crate::paths::{
    HostEnvironmentPaths, atomic_write_bytes, atomic_write_text, deterministic_uuid,
    expand_existing_with_sidecars, now_iso, validated_child_path,
};
use crate::registry::{ProjectRegistry, WorkspaceProjectBinding};
use crate::rpc_agy::fetch_agy_trajectory_via_rpc;
use crate::rpc_codex::fetch_codex_thread_via_rpc;
use crate::sqlite::{
    ensure_agy_conversation_db_schema, ensure_agy_summaries_db_schema,
    ensure_codex_state_db_schema, open_locked_rw_sqlite, open_readonly_sqlite,
};

/// Atomically write a Codex rollout file (`.jsonl` or `.jsonl.zst`).
///
/// # Errors
/// Returns [`HostError`] on compression or filesystem failure.
pub fn atomic_write_rollout(path: &Path, text: &str) -> Result<(), HostError> {
    if path
        .file_name()
        .and_then(|n| n.to_str())
        .is_some_and(|n| n.ends_with(".zst"))
    {
        let compressed = compress_zstd_rollout_bytes(text)?;
        atomic_write_bytes(path, &compressed)
    } else {
        atomic_write_text(path, text)
    }
}

/// Resolve effective Antigravity subagent lineage validating IDs at the boundary.
///
/// # Errors
/// Returns [`HostError::Core`] if any parent/root cascade ID is malformed.
pub fn resolve_effective_agy_lineage(
    parent_cascade_id: Option<&CascadeId>,
    lineage: Option<&SubagentLineage>,
) -> Result<SubagentLineage, HostError> {
    let eff_parent = parent_cascade_id
        .cloned()
        .or_else(|| lineage.and_then(|l| l.parent_cascade_id.clone()));
    if let Some(ref p) = eff_parent {
        validate_session_id(p.as_str(), "parent_cascade_id")?;
    }
    let eff_root = lineage
        .and_then(|l| l.root_cascade_id.clone())
        .or_else(|| eff_parent.clone());
    if let Some(ref r) = eff_root {
        validate_session_id(r.as_str(), "root_cascade_id")?;
    }
    Ok(SubagentLineage {
        parent_cascade_id: eff_parent,
        root_cascade_id: eff_root,
        parent_codex_thread_id: lineage.and_then(|l| l.parent_codex_thread_id.clone()),
        nesting_depth: lineage.map_or(0, |l| l.nesting_depth),
        agent_type_name: lineage.map_or_else(String::new, |l| l.agent_type_name.clone()),
        agent_role: lineage.map_or_else(String::new, |l| l.agent_role.clone()),
        spawn_step_index: lineage.and_then(|l| l.spawn_step_index),
    })
}

fn collect_matching_files_recursive(
    root: &Path,
    resolved_root: &Path,
    suffixes: &[String],
    out: &mut Vec<PathBuf>,
) {
    let mut stack = vec![root.to_path_buf()];
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
                if suffixes.iter().any(|suf| fname.ends_with(suf))
                    && resolve_strict_false(&p).starts_with(resolved_root)
                {
                    out.push(p);
                }
            }
        }
    }
    out.sort();
}

/// Candidate target file paths and cleanup globs for offline backup manifests.
pub type CandidateTargetPaths = (Vec<PathBuf>, Vec<(PathBuf, String)>);

/// Dual-mode transport reading via `?mode=ro` SQLite/JSONL/RPC and writing offline host stores.
#[derive(Debug, Clone)]
pub struct DefaultHostTransport {
    paths: HostEnvironmentPaths,
}

impl DefaultHostTransport {
    #[must_use]
    pub const fn new(paths: HostEnvironmentPaths) -> Self {
        Self { paths }
    }

    #[must_use]
    pub const fn paths(&self) -> &HostEnvironmentPaths {
        &self.paths
    }

    /// Read explicit title override for an Antigravity session from annotations and summary DB.
    ///
    /// # Errors
    /// Returns [`HostError`] if `cascade_id` violates path boundary rules.
    pub fn read_agy_explicit_title(
        &self,
        cascade_id: &CascadeId,
    ) -> Result<Option<String>, HostError> {
        validate_session_id(cascade_id.as_str(), "cascade_id")?;
        let annot_path = validated_child_path(
            &self.paths.agy_data_dir,
            &["annotations", &format!("{}.pbtxt", cascade_id.as_str())],
            "cascade_id",
        )?;
        let pbtxt_title = if annot_path.is_file() {
            fs::read_to_string(&annot_path)
                .ok()
                .and_then(|t| parse_annotation_pbtxt_title(&t))
                .map(|s| s.trim().to_owned())
                .filter(|s| !s.is_empty())
        } else {
            None
        };
        if pbtxt_title.is_some() {
            return Ok(pbtxt_title);
        }

        let sum_db = self.paths.agy_data_dir.join("conversation_summaries.db");
        if sum_db.is_file()
            && let Ok(conn) = open_readonly_sqlite(&sum_db)
            && let Ok((db_title, raw_blob)) = conn.query_row(
                "SELECT title, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                [cascade_id.as_str()],
                |r| {
                    Ok((
                        r.get::<_, Option<String>>(0)?.unwrap_or_default(),
                        r.get::<_, Option<Vec<u8>>>(1)?.unwrap_or_default(),
                    ))
                },
            )
        {
            if !raw_blob.is_empty()
                && let Ok(proj) = decode_cascade_summary_proto(&raw_blob, Some(cascade_id))
            {
                let t = proj.annotations.title.trim();
                if !t.is_empty() {
                    return Ok(Some(t.to_owned()));
                }
            }
            let dbt = db_title.trim();
            if !dbt.is_empty() {
                return Ok(Some(dbt.to_owned()));
            }
        }
        Ok(None)
    }

    /// Read persisted Antigravity steps from `conversations/<cid>.db` strictly read-only.
    ///
    /// # Errors
    /// Returns [`HostError`] if SQLite open or query fails.
    pub fn read_persisted_agy_steps(
        &self,
        conv_db: &Path,
    ) -> Result<Vec<AgyRawStepRow>, HostError> {
        let conn = open_readonly_sqlite(conv_db)?;
        let mut stmt = conn.prepare(
            "SELECT idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format FROM steps ORDER BY idx ASC",
        )?;
        let mut rows = stmt.query([])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            let idx: i64 = row.get(0)?;
            let step_type: i32 = row.get(1)?;
            let status: i32 = row.get(2)?;
            let has_sub: i64 = row.get::<_, Option<i64>>(3)?.unwrap_or(0);
            let metadata_blob: Vec<u8> = row.get::<_, Option<Vec<u8>>>(4)?.unwrap_or_default();
            let step_payload_blob: Vec<u8> = row.get::<_, Option<Vec<u8>>>(5)?.unwrap_or_default();
            let step_format: i32 = row.get::<_, Option<i32>>(6)?.unwrap_or(1);
            out.push(AgyRawStepRow {
                idx: StepIndex(idx.max(0) as u32),
                step_type,
                status,
                has_subtrajectory: has_sub != 0,
                metadata_blob,
                step_payload_blob,
                step_format,
            });
        }
        Ok(out)
    }

    fn read_agy_ledger_disk(
        &self,
        binding: &WorkspaceProjectBinding,
        cascade_id: &CascadeId,
    ) -> Result<TrajectoryLedger, HostError> {
        let conv_db = validated_child_path(
            &self.paths.agy_data_dir,
            &["conversations", &format!("{}.db", cascade_id.as_str())],
            "cascade_id",
        )?;
        if !conv_db.is_file() {
            return Err(HostError::FileNotFound(format!(
                "Antigravity conversation database not found: {}",
                conv_db.display()
            )));
        }
        let steps = self.read_persisted_agy_steps(&conv_db)?;
        let explicit_title = self.read_agy_explicit_title(cascade_id)?;
        Ok(decode_agy_trajectory(
            binding.parsed_path(),
            binding.parsed_uri(),
            &steps,
            explicit_title.as_deref(),
        )?)
    }

    /// Dual-mode read for Antigravity returning `(ledger, used_mode, is_inflight)`.
    ///
    /// # Errors
    /// Returns [`HostError`] on missing trajectory, unavailable forced RPC, or decode failure.
    pub fn read_agy_ledger_with_meta(
        &self,
        binding: &WorkspaceProjectBinding,
        cascade_id: &CascadeId,
        lock_snapshot: &HostLockSnapshot,
        mode: TransportMode,
    ) -> Result<(TrajectoryLedger, TransportMode, bool), HostError> {
        let try_rpc = matches!(mode, TransportMode::OnlineRpc)
            || (matches!(mode, TransportMode::Auto) && lock_snapshot.agy_daemon.is_some());

        if try_rpc {
            match fetch_agy_trajectory_via_rpc(
                lock_snapshot.agy_daemon.as_ref(),
                cascade_id,
                5.0,
                true,
                None,
            ) {
                Ok(rpc_res) => {
                    let is_active = rpc_res.is_active();
                    let explicit_title = self.read_agy_explicit_title(cascade_id)?;
                    let mut ledger = decode_agy_trajectory(
                        binding.parsed_path(),
                        binding.parsed_uri(),
                        &rpc_res.raw_steps,
                        explicit_title.as_deref(),
                    )?;
                    if is_active && !ledger.has_inflight_turn {
                        ledger.has_inflight_turn = true;
                    }
                    return Ok((ledger, TransportMode::OnlineRpc, is_active));
                }
                Err(HostError::OnlineRpcUnavailable(msg)) => {
                    if matches!(mode, TransportMode::OnlineRpc) {
                        return Err(HostError::OnlineRpcUnavailable(msg));
                    }
                }
                Err(e) => {
                    if matches!(mode, TransportMode::OnlineRpc) {
                        return Err(e);
                    }
                }
            }
        }

        let ledger = self.read_agy_ledger_disk(binding, cascade_id)?;
        Ok((ledger, TransportMode::OfflineStorage, false))
    }

    fn resolve_codex_rollout_candidate(
        &self,
        raw_path: &str,
        resolved_codex_home: &Path,
    ) -> Option<PathBuf> {
        let p = PathBuf::from(raw_path);
        let zst_p = PathBuf::from(format!("{raw_path}.zst"));
        [p, zst_p].into_iter().find(|cand| {
            cand.is_file()
                && resolve_strict_false(cand)
                    .starts_with(resolved_root_or_self(resolved_codex_home))
        })
    }

    /// Probe Codex `state_5.sqlite` + `session_index.jsonl` for rollout path & title without throwing.
    ///
    /// # Errors
    /// Returns [`HostError`] only if `thread_id` fails boundary validation.
    pub fn probe_codex_rollout_and_title(
        &self,
        thread_id: &CodexThreadId,
    ) -> Result<(Option<PathBuf>, Option<String>), HostError> {
        validate_session_id(thread_id.as_str(), "codex_thread_id")?;
        let resolved_codex_home = resolve_strict_false(&self.paths.codex_home_dir);
        let state_db = self.paths.codex_home_dir.join("state_5.sqlite");
        let mut rollout_path: Option<PathBuf> = None;
        let mut db_title: Option<String> = None;

        if state_db.is_file()
            && let Ok(conn) = open_readonly_sqlite(&state_db)
            && let Ok((rp_raw, title_raw, name_raw)) = conn.query_row(
                "SELECT rollout_path, title, name FROM threads WHERE id = ?",
                [thread_id.as_str()],
                |r| {
                    Ok((
                        r.get::<_, Option<String>>(0)?.unwrap_or_default(),
                        r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                        r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    ))
                },
            )
        {
            if !rp_raw.trim().is_empty() {
                rollout_path =
                    self.resolve_codex_rollout_candidate(rp_raw.trim(), &resolved_codex_home);
            }
            db_title = [name_raw, title_raw]
                .into_iter()
                .map(|s| s.trim().to_owned())
                .find(|s| !s.is_empty());
        }

        let session_idx = self.paths.codex_home_dir.join("session_index.jsonl");
        if db_title.is_none()
            && session_idx.is_file()
            && let Ok(text) = fs::read_to_string(&session_idx)
        {
            for line in text.lines() {
                if !line.contains(thread_id.as_str()) {
                    continue;
                }
                if let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(line)
                    && obj.get("id").and_then(Value::as_str) == Some(thread_id.as_str())
                    && let Some(tname) = obj
                        .get("thread_name")
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                {
                    db_title = Some(tname.to_owned());
                    break;
                }
            }
        }
        Ok((rollout_path, db_title))
    }

    /// Locate a Codex thread's rollout file and optional explicit title.
    ///
    /// # Errors
    /// Returns [`HostError::FileNotFound`] if no rollout file exists on disk.
    pub fn find_codex_rollout_and_title(
        &self,
        thread_id: &CodexThreadId,
    ) -> Result<(PathBuf, Option<String>), HostError> {
        let (mut rollout_path, db_title) = self.probe_codex_rollout_and_title(thread_id)?;
        if rollout_path.is_none() {
            let suffixes = [
                format!("{}.jsonl", thread_id.as_str()),
                format!("{}.jsonl.zst", thread_id.as_str()),
            ];
            for sub in ["sessions", "archived_sessions"] {
                let sroot = self.paths.codex_home_dir.join(sub);
                if sroot.is_dir() {
                    let resolved_sroot = resolve_strict_false(&sroot);
                    let mut matches = Vec::new();
                    collect_matching_files_recursive(
                        &sroot,
                        &resolved_sroot,
                        &suffixes,
                        &mut matches,
                    );
                    if let Some(last) = matches.pop() {
                        rollout_path = Some(last);
                        break;
                    }
                }
            }
        }
        let Some(rp) = rollout_path.filter(|p| p.is_file()) else {
            return Err(HostError::FileNotFound(format!(
                "Codex rollout JSONL file not found for thread {}",
                thread_id.as_str()
            )));
        };
        Ok((rp, db_title))
    }

    /// Dual-mode read for Codex returning `(ledger, used_mode, is_inflight)`.
    ///
    /// # Errors
    /// Returns [`HostError`] on missing rollout/thread, unavailable forced RPC, or decode failure.
    pub fn read_codex_ledger_with_meta(
        &self,
        binding: &WorkspaceProjectBinding,
        thread_id: &CodexThreadId,
        lock_snapshot: &HostLockSnapshot,
        mode: TransportMode,
    ) -> Result<(TrajectoryLedger, TransportMode, bool), HostError> {
        let try_rpc = matches!(mode, TransportMode::OnlineRpc)
            || (matches!(mode, TransportMode::Auto) && lock_snapshot.codex_daemon.is_some());

        if try_rpc {
            match fetch_codex_thread_via_rpc(lock_snapshot.codex_daemon.as_ref(), thread_id, 5.0) {
                Ok(rpc_res) => {
                    let (_, opt_title) = self.probe_codex_rollout_and_title(thread_id)?;
                    let rpc_ledger = decode_codex_rpc_thread_to_ledger(
                        binding.parsed_path(),
                        binding.parsed_uri(),
                        &rpc_res.thread,
                        opt_title.as_deref(),
                    )?;
                    let resolved_codex_home = resolve_strict_false(&self.paths.codex_home_dir);
                    let rpc_path =
                        rpc_res
                            .thread
                            .get("path")
                            .and_then(Value::as_str)
                            .and_then(|p| {
                                self.resolve_codex_rollout_candidate(p, &resolved_codex_home)
                            });
                    let disk_rollout = rpc_path.or_else(|| {
                        self.find_codex_rollout_and_title(thread_id)
                            .ok()
                            .map(|(p, _)| p)
                    });
                    let mut final_ledger = if let Some(rp) = disk_rollout {
                        if let Ok(lines) = read_codex_rollout_lines(&rp)
                            && let Ok(disk_ledger) = decode_codex_rollout(
                                binding.parsed_path(),
                                binding.parsed_uri(),
                                &lines,
                                opt_title.as_deref(),
                            )
                        {
                            splice_rollout_completed_prefix(
                                &disk_ledger,
                                &rpc_ledger,
                                rpc_res.is_active,
                                opt_title.as_deref(),
                            )
                        } else {
                            rpc_ledger
                        }
                    } else {
                        rpc_ledger
                    };
                    if rpc_res.is_active && !final_ledger.has_inflight_turn {
                        final_ledger.has_inflight_turn = true;
                    }
                    return Ok((final_ledger, TransportMode::OnlineRpc, rpc_res.is_active));
                }
                Err(HostError::OnlineRpcUnavailable(msg)) => {
                    if matches!(mode, TransportMode::OnlineRpc) {
                        return Err(HostError::OnlineRpcUnavailable(msg));
                    }
                }
                Err(e) => {
                    if matches!(mode, TransportMode::OnlineRpc) {
                        return Err(e);
                    }
                }
            }
        }

        let (rp, title) = self.find_codex_rollout_and_title(thread_id)?;
        let lines = read_codex_rollout_lines(&rp)?;
        let ledger = decode_codex_rollout(
            binding.parsed_path(),
            binding.parsed_uri(),
            &lines,
            title.as_deref(),
        )?;
        Ok((ledger, TransportMode::OfflineStorage, false))
    }

    /// Return candidate host files & cleanup globs for offline backup manifests.
    ///
    /// # Errors
    /// Returns [`HostError`] if any session ID violates path boundary checks.
    pub fn candidate_target_paths(
        &self,
        target_host: HostKind,
        cascade_id: Option<&CascadeId>,
        codex_thread_id: Option<&CodexThreadId>,
        binding: Option<&WorkspaceProjectBinding>,
        lineage: Option<&SubagentLineage>,
        include_primary_store: bool,
    ) -> Result<CandidateTargetPaths, HostError> {
        if let Some(cid) = cascade_id {
            validate_session_id(cid.as_str(), "cascade_id")?;
        }
        if let Some(tid) = codex_thread_id {
            validate_session_id(tid.as_str(), "codex_thread_id")?;
        }
        if target_host == HostKind::Antigravity {
            let mut files = vec![
                self.paths.agy_data_dir.join("conversation_summaries.db"),
                self.paths.agy_data_dir.join("agyhub_summaries_proto.pb"),
            ];
            if let Some(b) = binding {
                files.push(
                    self.paths
                        .gemini_config_dir
                        .join("projects")
                        .join(format!("{}.json", b.agy_project_id.as_str())),
                );
            }
            if let Some(cid) = cascade_id {
                for rdir in [
                    Some(&self.paths.agy_data_dir),
                    self.paths.agy_cli_data_dir.as_ref(),
                ]
                .into_iter()
                .flatten()
                {
                    files.push(validated_child_path(
                        rdir,
                        &[
                            "brain",
                            cid.as_str(),
                            ".system_generated",
                            "logs",
                            "transcript.jsonl",
                        ],
                        "cascade_id",
                    )?);
                }
                if include_primary_store {
                    files.push(validated_child_path(
                        &self.paths.agy_data_dir,
                        &["conversations", &format!("{}.db", cid.as_str())],
                        "cascade_id",
                    )?);
                }
                files.push(validated_child_path(
                    &self.paths.agy_data_dir,
                    &["annotations", &format!("{}.pbtxt", cid.as_str())],
                    "cascade_id",
                )?);
                if let Some(lin) = lineage
                    && lin.is_subagent()
                    && let Some(ref parent_cid) = lin.parent_cascade_id
                {
                    files.push(validated_child_path(
                        &self.paths.agy_data_dir,
                        &[
                            "brain",
                            parent_cid.as_str(),
                            ".system_generated",
                            "subagents",
                            &format!("{}.json", cid.as_str()),
                        ],
                        "cascade_id",
                    )?);
                }
            }
            return Ok((files, Vec::new()));
        }

        let mut files = vec![
            self.paths.codex_home_dir.join("state_5.sqlite"),
            self.paths.codex_home_dir.join(".codex-global-state.json"),
            self.paths.codex_home_dir.join("session_index.jsonl"),
        ];
        if !include_primary_store || codex_thread_id.is_none() {
            return Ok((files, Vec::new()));
        }
        let tid = codex_thread_id.unwrap_or_else(|| unreachable!());
        let suffixes = [
            format!("{}.jsonl", tid.as_str()),
            format!("{}.jsonl.zst", tid.as_str()),
        ];
        for sub in ["sessions", "archived_sessions"] {
            let sroot = self.paths.codex_home_dir.join(sub);
            if sroot.is_dir() {
                let rsroot = resolve_strict_false(&sroot);
                collect_matching_files_recursive(&sroot, &rsroot, &suffixes, &mut files);
            }
        }
        let globs = vec![
            (
                self.paths.codex_home_dir.join("sessions"),
                format!("*{}.jsonl", tid.as_str()),
            ),
            (
                self.paths.codex_home_dir.join("sessions"),
                format!("*{}.jsonl.zst", tid.as_str()),
            ),
        ];
        Ok((files, globs))
    }

    #[allow(clippy::too_many_arguments)]
    pub fn write_agy_conversation_db(
        &self,
        binding: &WorkspaceProjectBinding,
        target_cid: &CascadeId,
        events: &[CanonicalEvent],
        full_ledger: &TrajectoryLedger,
        source_tag: AgyGuiSafeSource,
        incremental: bool,
        effective_lineage: &SubagentLineage,
        forked_from_cascade_id: Option<&CascadeId>,
    ) -> Result<(PathBuf, Vec<AgyRawStepRow>), HostError> {
        let conv_db = validated_child_path(
            &self.paths.agy_data_dir,
            &["conversations", &format!("{}.db", target_cid.as_str())],
            "cascade_id",
        )?;
        let mut traj_id =
            TrajectoryId::parse(&deterministic_uuid("agy-trajectory", target_cid.as_str()))?;
        let is_incremental = incremental && conv_db.is_file() && !events.is_empty();
        let conn = open_locked_rw_sqlite(&conv_db)?;
        ensure_agy_conversation_db_schema(&conn)?;

        if let Ok(existing_tid) = conn.query_row(
            "SELECT trajectory_id FROM trajectory_meta LIMIT 1",
            [],
            |r| r.get::<_, String>(0),
        ) && let Ok(parsed) = TrajectoryId::parse(&existing_tid)
        {
            traj_id = parsed;
        }

        let step_rows = if is_incremental {
            let max_idx: i64 = conn
                .query_row("SELECT COALESCE(MAX(idx), -1) FROM steps", [], |r| r.get(0))
                .unwrap_or(-1);
            let start = StepIndex((max_idx + 1).max(0) as u32);
            encode_agy_trajectory(events, start)
        } else {
            conn.execute("DELETE FROM steps", [])?;
            encode_agy_trajectory(&full_ledger.events, StepIndex(0))
        };

        for raw_row in &step_rows {
            let r = bind_agy_step(raw_row, traj_id.as_str(), target_cid.as_str())?;
            conn.execute(
                "INSERT OR REPLACE INTO steps (idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rusqlite::params![
                    i64::from(r.idx.0),
                    r.step_type,
                    r.status,
                    if r.has_subtrajectory { 1i64 } else { 0i64 },
                    r.metadata_blob,
                    r.step_payload_blob,
                    r.step_format
                ],
            )?;
        }

        conn.execute("DELETE FROM trajectory_meta", [])?;
        conn.execute(
            "INSERT INTO trajectory_meta (trajectory_id, cascade_id, trajectory_type, source) VALUES (?, ?, 4, ?)",
            rusqlite::params![
                traj_id.as_str(),
                target_cid.as_str(),
                i64::from(source_tag.proto_enum_value())
            ],
        )?;

        let created_iso = full_ledger
            .events
            .first()
            .map_or_else(now_iso, |e| e.anchor().timestamp_iso.clone());
        let parent_str = effective_lineage
            .parent_cascade_id
            .as_ref()
            .map_or("", CascadeId::as_str);
        let root_str = effective_lineage
            .root_cascade_id
            .as_ref()
            .unwrap_or(target_cid)
            .as_str();
        let ws_uris = [binding.parsed_uri()];
        let tmeta_blob = encode_trajectory_metadata_proto(&TrajectoryMetadataEncodeParams {
            project_id: &binding.agy_project_id,
            workspace_uris: &ws_uris,
            created_at_iso: &created_iso,
            nesting_depth: i64::from(effective_lineage.nesting_depth),
            parent_conversation_id: parent_str,
            root_conversation_id: root_str,
            agent_type_name: &effective_lineage.agent_type_name,
            agent_role: &effective_lineage.agent_role,
            unknown_wire_fields: &[],
        });
        conn.execute(
            "INSERT OR REPLACE INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)",
            rusqlite::params![tmeta_blob],
        )?;
        if let Some(fk) = forked_from_cascade_id {
            conn.execute("DELETE FROM parent_references", [])?;
            conn.execute(
                "INSERT INTO parent_references (idx, data) VALUES (0, ?)",
                rusqlite::params![encode_string_field(1, fk.as_str())],
            )?;
        }

        let persisted = self.read_persisted_agy_steps(&conv_db)?;
        Ok((conv_db, persisted))
    }

    fn load_or_seed_hub_summaries_state(
        &self,
        hub_pb_path: &Path,
        sum_db: &Path,
    ) -> Option<SummariesStateProjection> {
        if hub_pb_path.is_file()
            && let Ok(bytes) = fs::read(hub_pb_path)
            && let Ok(decoded) = decode_summaries_state_proto(&bytes)
        {
            return Some(decoded);
        }
        if !sum_db.is_file() {
            return None;
        }
        let conn = open_readonly_sqlite(sum_db).ok()?;
        let mut stmt = conn
            .prepare("SELECT conversation_id, raw_summary FROM conversation_summaries")
            .ok()?;
        let mut rows = stmt.query([]).ok()?;
        let mut seeded = std::collections::BTreeMap::new();
        while let Ok(Some(row)) = rows.next() {
            let raw_cid: String = row
                .get::<_, Option<String>>(0)
                .ok()
                .flatten()
                .unwrap_or_default();
            let raw_blob: Vec<u8> = row
                .get::<_, Option<Vec<u8>>>(1)
                .ok()
                .flatten()
                .unwrap_or_default();
            if !raw_cid.is_empty()
                && !raw_blob.is_empty()
                && let Ok(cid) = CascadeId::parse(raw_cid.trim())
                && let Ok(proj) = decode_cascade_summary_proto(&raw_blob, Some(&cid))
            {
                seeded.insert(cid, proj);
            }
        }
        if seeded.is_empty() {
            None
        } else {
            Some(SummariesStateProjection {
                summaries: seeded,
                file_mod_times: std::collections::BTreeMap::new(),
                unknown_wire_fields: Vec::new(),
            })
        }
    }

    pub fn write_agy_brain_and_pbtxt(
        &self,
        binding: &WorkspaceProjectBinding,
        target_cid: &CascadeId,
        persisted_ledger: &TrajectoryLedger,
        summary_proj: &CascadeSummaryProjection,
        viewed_iso: &str,
        effective_lineage: &SubagentLineage,
    ) -> Result<(), HostError> {
        let annot_path = validated_child_path(
            &self.paths.agy_data_dir,
            &["annotations", &format!("{}.pbtxt", target_cid.as_str())],
            "cascade_id",
        )?;
        let pbtxt = render_annotation_pbtxt(
            &persisted_ledger.project_annotation_pbtxt(target_cid, Some(viewed_iso)),
        );
        atomic_write_text(&annot_path, &pbtxt)?;

        let tx_rows = persisted_ledger.project_transcript_rows();
        let mut tx_text = tx_rows
            .iter()
            .map(render_transcript_row_jsonl)
            .collect::<Vec<_>>()
            .join("\n");
        if !tx_rows.is_empty() {
            tx_text.push('\n');
        }
        for root_dir in [
            Some(&self.paths.agy_data_dir),
            self.paths.agy_cli_data_dir.as_ref(),
        ]
        .into_iter()
        .flatten()
        {
            let tx_path = validated_child_path(
                root_dir,
                &[
                    "brain",
                    target_cid.as_str(),
                    ".system_generated",
                    "logs",
                    "transcript.jsonl",
                ],
                "cascade_id",
            )?;
            atomic_write_text(&tx_path, &tx_text)?;
        }

        if let Some(ref eff_parent) = effective_lineage.parent_cascade_id
            && effective_lineage.is_subagent()
        {
            let sidecar_path = validated_child_path(
                &self.paths.agy_data_dir,
                &[
                    "brain",
                    eff_parent.as_str(),
                    ".system_generated",
                    "subagents",
                    &format!("{}.json", target_cid.as_str()),
                ],
                "cascade_id",
            )?;
            let mut sidecar_doc = Map::new();
            sidecar_doc.insert("conversationId".into(), json!(target_cid.as_str()));
            let type_name = if effective_lineage.agent_type_name.is_empty() {
                "self"
            } else {
                effective_lineage.agent_type_name.as_str()
            };
            let role = if effective_lineage.agent_role.is_empty() {
                summary_proj.title.as_str()
            } else {
                effective_lineage.agent_role.as_str()
            };
            sidecar_doc.insert(
                "subagentDescriptor".into(),
                json!({"typeName": type_name, "role": role}),
            );
            sidecar_doc.insert("state".into(), json!("SUBAGENT_STATE_ALIVE"));
            sidecar_doc.insert("workspaceUris".into(), json!([binding.workspace_uri]));
            if let Some(sidx) = effective_lineage.spawn_step_index {
                sidecar_doc.insert("spawnStepIndex".into(), json!(sidx.0));
            }
            let rendered = format!("{}\n", Value::Object(sidecar_doc));
            atomic_write_text(&sidecar_path, &rendered)?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn write_agy_companion_stores(
        &self,
        binding: &WorkspaceProjectBinding,
        target_cid: &CascadeId,
        persisted_ledger: &TrajectoryLedger,
        persisted_step_count: u32,
        conv_db: &Path,
        source_tag: AgyGuiSafeSource,
        effective_lineage: &SubagentLineage,
    ) -> Result<(), HostError> {
        let viewed_iso = now_iso();
        let mut summary_proj = persisted_ledger.project_agy_cascade_summary(
            target_cid,
            &binding.agy_project_id,
            Some(&viewed_iso),
            source_tag,
            Some(effective_lineage),
        )?;
        summary_proj.step_count = persisted_step_count;

        self.write_agy_brain_and_pbtxt(
            binding,
            target_cid,
            persisted_ledger,
            &summary_proj,
            &viewed_iso,
            effective_lineage,
        )?;

        let mut last_user_time = summary_proj.created_at_iso.clone();
        let mut last_user_idx = -1i64;
        for ev in &persisted_ledger.events {
            if matches!(ev, CanonicalEvent::UserInput { .. }) {
                if !ev.anchor().timestamp_iso.is_empty() {
                    last_user_time.clone_from(&ev.anchor().timestamp_iso);
                }
                if let Some(sidx) = ev.anchor().agy_step_index {
                    last_user_idx = i64::from(sidx.0);
                }
            }
        }

        let sum_db = self.paths.agy_data_dir.join("conversation_summaries.db");
        let sconn = open_locked_rw_sqlite(&sum_db)?;
        ensure_agy_summaries_db_schema(&sconn)?;
        let ws_uris_json = serde_json::to_string(
            &summary_proj
                .workspace_uris
                .iter()
                .map(|u| u.as_str())
                .collect::<Vec<_>>(),
        )?;
        let parent_str = effective_lineage
            .parent_cascade_id
            .as_ref()
            .map_or("", CascadeId::as_str);
        let raw_summary = encode_cascade_summary_proto(&summary_proj);
        sconn.execute(
            r#"
            INSERT OR REPLACE INTO conversation_summaries (
                conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, source,
                project_id, agent_name, parent_conversation_id, nesting_depth, battle_id, winning_conversation_id,
                not_fully_idle, killed, last_user_input_time, last_user_input_step_index, app_data_dir, raw_summary, group_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'CASCADE_RUN_STATUS_IDLE', ?, ?, ?, ?, ?, '', '', 0, 0, ?, ?, ?, ?, '')
            "#,
            rusqlite::params![
                target_cid.as_str(),
                summary_proj.title,
                summary_proj.preview,
                i64::from(summary_proj.step_count),
                summary_proj.updated_at_iso,
                ws_uris_json,
                summary_proj.source.summary_column_name(),
                summary_proj.project_id.as_str(),
                summary_proj.agent_name,
                parent_str,
                i64::from(summary_proj.nesting_depth),
                last_user_time,
                last_user_idx,
                self.paths.agy_data_dir.display().to_string(),
                raw_summary,
            ],
        )?;
        drop(sconn);

        let hub_pb_path = self.paths.agy_data_dir.join("agyhub_summaries_proto.pb");
        let existing_hub = self.load_or_seed_hub_summaries_state(&hub_pb_path, &sum_db);
        let conv_mtime = fs::metadata(conv_db)
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map_or_else(
                || {
                    SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .map_or(0, |d| d.as_secs() as i64)
                },
                |d| d.as_secs() as i64,
            );
        let hub_proj = persisted_ledger.project_agy_hub_summaries_state(
            existing_hub.as_ref(),
            summary_proj,
            &conv_db.display().to_string(),
            conv_mtime,
        );
        atomic_write_bytes(&hub_pb_path, &encode_summaries_state_proto(&hub_proj))?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn apply_to_agy(
        &self,
        binding: &WorkspaceProjectBinding,
        cascade_id: Option<&CascadeId>,
        events: &[CanonicalEvent],
        full_ledger: &TrajectoryLedger,
        lock_snapshot: &HostLockSnapshot,
        mode: TransportMode,
        parent_cascade_id: Option<&CascadeId>,
        source_tag: AgyGuiSafeSource,
        incremental: bool,
        lineage: Option<&SubagentLineage>,
        forked_from_cascade_id: Option<&CascadeId>,
    ) -> Result<CascadeId, HostError> {
        if mode != TransportMode::OfflineStorage {
            return Err(HostError::Runtime(format!(
                "apply_to_agy requires TransportMode.OFFLINE_STORAGE (got {}); refusing to raw-write active WAL storage.",
                mode.as_str()
            )));
        }
        if !lock_snapshot.can_write_agy_offline() {
            return Err(HostError::Runtime(
                "Cannot raw-write Antigravity storage while presence lock, daemon WAL, or in-flight turn is active.".to_owned(),
            ));
        }
        let target_cid = match cascade_id {
            Some(c) => {
                validate_session_id(c.as_str(), "cascade_id")?;
                c.clone()
            }
            None => CascadeId::parse(&Uuid::new_v4().to_string())?,
        };
        if let Some(fk) = forked_from_cascade_id {
            validate_session_id(fk.as_str(), "forked_from_cascade_id")?;
        }
        let eff_lineage = resolve_effective_agy_lineage(parent_cascade_id, lineage)?;
        let (conv_db, raw_persisted) = self.write_agy_conversation_db(
            binding,
            &target_cid,
            events,
            full_ledger,
            source_tag,
            incremental,
            &eff_lineage,
            forked_from_cascade_id,
        )?;
        let persisted_ledger = decode_agy_trajectory(
            binding.parsed_path(),
            binding.parsed_uri(),
            &raw_persisted,
            Some(&full_ledger.title),
        )?;
        self.write_agy_companion_stores(
            binding,
            &target_cid,
            &persisted_ledger,
            raw_persisted.len() as u32,
            &conv_db,
            source_tag,
            &eff_lineage,
        )?;
        Ok(target_cid)
    }

    fn resolve_or_allocate_codex_rollout_path(
        &self,
        target_tid: &CodexThreadId,
        had_explicit_tid: bool,
    ) -> Result<PathBuf, HostError> {
        let resolved_codex_home = resolve_strict_false(&self.paths.codex_home_dir);
        let state_db = self.paths.codex_home_dir.join("state_5.sqlite");
        if had_explicit_tid
            && state_db.is_file()
            && let Ok(conn) = open_readonly_sqlite(&state_db)
            && let Ok(raw) = conn.query_row(
                "SELECT rollout_path FROM threads WHERE id = ?",
                [target_tid.as_str()],
                |r| r.get::<_, String>(0),
            )
            && let Some(cand) =
                self.resolve_codex_rollout_candidate(raw.trim(), &resolved_codex_home)
        {
            return Ok(cand);
        }

        let now_dt = Utc::now();
        let yr = now_dt.format("%Y").to_string();
        let mo = now_dt.format("%m").to_string();
        let dy = now_dt.format("%d").to_string();
        let stamp = now_dt.format("%Y-%m-%dT%H-%M-%S").to_string();
        let rp = validated_child_path(
            &self.paths.codex_home_dir,
            &[
                "sessions",
                &yr,
                &mo,
                &dy,
                &format!("rollout-{stamp}-{}.jsonl", target_tid.as_str()),
            ],
            "codex_thread_id",
        )?;
        if let Some(parent) = rp.parent() {
            fs::create_dir_all(parent)?;
        }
        Ok(rp)
    }

    pub fn write_codex_index_and_global_state(
        &self,
        binding: &WorkspaceProjectBinding,
        target_tid: &CodexThreadId,
        title: &str,
    ) -> Result<(), HostError> {
        let session_idx_path = self.paths.codex_home_dir.join("session_index.jsonl");
        let mut kept_lines = Vec::new();
        if session_idx_path.is_file()
            && let Ok(text) = fs::read_to_string(&session_idx_path)
        {
            for ln in text.lines() {
                let trimmed = ln.trim();
                if trimmed.is_empty() {
                    continue;
                }
                if let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(trimmed)
                    && obj.get("id").and_then(Value::as_str) == Some(target_tid.as_str())
                {
                    continue;
                }
                kept_lines.push(ln.to_owned());
            }
        }
        kept_lines.push(
            json!({
                "id": target_tid.as_str(),
                "thread_name": title,
                "updated_at": now_iso(),
            })
            .to_string(),
        );
        let mut idx_text = kept_lines.join("\n");
        idx_text.push('\n');
        atomic_write_text(&session_idx_path, &idx_text)?;

        let gs_path = self.paths.codex_home_dir.join(".codex-global-state.json");
        let mut gs_data: Map<String, Value> = if gs_path.is_file() {
            fs::read_to_string(&gs_path)
                .ok()
                .and_then(|t| serde_json::from_str::<Value>(&t).ok())
                .and_then(|v| v.as_object().cloned())
                .unwrap_or_default()
        } else {
            Map::new()
        };
        let assignments = gs_data
            .entry("thread-project-assignments")
            .or_insert_with(|| Value::Object(Map::new()));
        if !assignments.is_object() {
            *assignments = Value::Object(Map::new());
        }
        if let Some(aobj) = assignments.as_object_mut() {
            aobj.insert(
                target_tid.as_str().to_owned(),
                json!({
                    "projectKind": "local",
                    "projectId": binding.codex_legacy_project_id.as_str(),
                }),
            );
        }
        let gs_text = format!(
            "{}\n",
            serde_json::to_string_pretty(&Value::Object(gs_data))?
        );
        atomic_write_text(&gs_path, &gs_text)
    }

    pub fn write_codex_companion_stores(
        &self,
        binding: &WorkspaceProjectBinding,
        target_tid: &CodexThreadId,
        rollout_path: &Path,
        full_ledger: &TrajectoryLedger,
        lineage: Option<&SubagentLineage>,
        effective_parent_tid: Option<&CodexThreadId>,
    ) -> Result<(), HostError> {
        let thread_proj =
            full_ledger.project_codex_thread_summary(target_tid, &binding.codex_project_id)?;
        let created_sec = (thread_proj.created_at_ms / 1000).max(1);
        let updated_sec = (thread_proj.updated_at_ms / 1000).max(1);
        let eff_agent_role = lineage
            .map(|l| l.agent_type_name.as_str())
            .filter(|s| !s.is_empty());
        let eff_agent_nick = lineage
            .map(|l| l.agent_role.as_str())
            .filter(|s| !s.is_empty());
        let thread_source_val = if let Some(lin) = lineage
            && lin.is_subagent()
        {
            json!({
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": effective_parent_tid.map(CodexThreadId::as_str),
                        "depth": lin.nesting_depth.max(1),
                        "agent_nickname": eff_agent_nick,
                        "agent_role": eff_agent_role,
                    }
                }
            })
            .to_string()
        } else {
            "desktop".to_owned()
        };

        let state_db = self.paths.codex_home_dir.join("state_5.sqlite");
        let conn = open_locked_rw_sqlite(&state_db)?;
        ensure_codex_state_db_schema(&conn)?;
        conn.execute(
            r#"
            INSERT OR REPLACE INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                first_user_message, agent_nickname, agent_role, created_at_ms, updated_at_ms,
                preview, recency_at, recency_at_ms, name, project_id, originator, attached_thread_parent_id
            ) VALUES (?, ?, ?, ?, ?, 'openai', ?, ?, 'workspace-write', 'never', 0, 1, 0, '0.166.0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'codex_vscode', ?)
            "#,
            rusqlite::params![
                target_tid.as_str(),
                rollout_path.display().to_string(),
                created_sec,
                updated_sec,
                thread_source_val,
                thread_proj.cwd.to_string(),
                thread_proj.title,
                thread_proj.preview,
                eff_agent_nick,
                eff_agent_role,
                thread_proj.created_at_ms,
                thread_proj.updated_at_ms,
                thread_proj.preview,
                updated_sec,
                thread_proj.updated_at_ms,
                thread_proj.title,
                binding.codex_project_id.as_str(),
                effective_parent_tid.map(CodexThreadId::as_str),
            ],
        )?;
        drop(conn);

        self.write_codex_index_and_global_state(binding, target_tid, &thread_proj.title)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn apply_to_codex(
        &self,
        binding: &WorkspaceProjectBinding,
        thread_id: Option<&CodexThreadId>,
        events: &[CanonicalEvent],
        full_ledger: &TrajectoryLedger,
        lock_snapshot: &HostLockSnapshot,
        mode: TransportMode,
        parent_thread_id: Option<&CodexThreadId>,
        incremental: bool,
        lineage: Option<&SubagentLineage>,
    ) -> Result<CodexThreadId, HostError> {
        if mode != TransportMode::OfflineStorage {
            return Err(HostError::Runtime(format!(
                "apply_to_codex requires TransportMode.OFFLINE_STORAGE (got {}); refusing to raw-write active WAL storage.",
                mode.as_str()
            )));
        }
        if !lock_snapshot.can_write_codex_offline() {
            return Err(HostError::Runtime(
                "Cannot raw-write Codex storage while app-server daemon, WAL lock, or in-flight turn is active.".to_owned(),
            ));
        }
        let target_tid = match thread_id {
            Some(t) => {
                validate_session_id(t.as_str(), "codex_thread_id")?;
                t.clone()
            }
            None => CodexThreadId::parse(&generate_uuid7())?,
        };
        let eff_parent_tid = parent_thread_id
            .cloned()
            .or_else(|| lineage.and_then(|l| l.parent_codex_thread_id.clone()));
        if let Some(ref pt) = eff_parent_tid {
            validate_session_id(pt.as_str(), "parent_codex_thread_id")?;
        }
        let rollout_path =
            self.resolve_or_allocate_codex_rollout_path(&target_tid, thread_id.is_some())?;
        let ws_str = binding.workspace_path.clone();
        if incremental && rollout_path.is_file() && !events.is_empty() {
            let prior_lines = read_codex_rollout_lines(&rollout_path)?;
            let start_line = RolloutLineNo(prior_lines.len() as u64 + 1);
            let delta =
                encode_codex_rollout(events, start_line, Some(target_tid.as_str()), Some(&ws_str));
            let mut combined: Vec<String> = prior_lines.into_iter().map(|r| r.raw_json).collect();
            combined.extend(delta.into_iter().map(|r| r.raw_json));
            let mut body = combined.join("\n");
            if !body.is_empty() {
                body.push('\n');
            }
            atomic_write_rollout(&rollout_path, &body)?;
        } else {
            let full = encode_codex_rollout(
                &full_ledger.events,
                RolloutLineNo(1),
                Some(target_tid.as_str()),
                Some(&ws_str),
            );
            let mut body = full
                .into_iter()
                .map(|r| r.raw_json)
                .collect::<Vec<_>>()
                .join("\n");
            if !body.is_empty() {
                body.push('\n');
            }
            atomic_write_rollout(&rollout_path, &body)?;
        }
        self.write_codex_companion_stores(
            binding,
            &target_tid,
            &rollout_path,
            full_ledger,
            lineage,
            eff_parent_tid.as_ref(),
        )?;
        Ok(target_tid)
    }

    #[must_use]
    pub fn agy_companions_missing(
        &self,
        binding: &WorkspaceProjectBinding,
        cascade_id: &CascadeId,
        lineage: &SubagentLineage,
    ) -> bool {
        let Ok((cands, _)) = self.candidate_target_paths(
            HostKind::Antigravity,
            Some(cascade_id),
            None,
            Some(binding),
            Some(lineage),
            false,
        ) else {
            return true;
        };
        if cands.iter().any(|p| !p.is_file()) {
            return true;
        }
        let sum_db = self.paths.agy_data_dir.join("conversation_summaries.db");
        let hub_pb = self.paths.agy_data_dir.join("agyhub_summaries_proto.pb");
        let Ok(conn) = open_readonly_sqlite(&sum_db) else {
            return true;
        };
        let has_row = conn
            .query_row(
                "SELECT 1 FROM conversation_summaries WHERE conversation_id = ?",
                [cascade_id.as_str()],
                |_| Ok(true),
            )
            .unwrap_or(false);
        if !has_row {
            return true;
        }
        let Ok(bytes) = fs::read(&hub_pb) else {
            return true;
        };
        let Ok(hub) = decode_summaries_state_proto(&bytes) else {
            return true;
        };
        !hub.summaries.contains_key(cascade_id)
    }

    #[must_use]
    pub fn codex_companions_missing(
        &self,
        binding: &WorkspaceProjectBinding,
        thread_id: &CodexThreadId,
    ) -> bool {
        let state_db = self.paths.codex_home_dir.join("state_5.sqlite");
        let session_idx = self.paths.codex_home_dir.join("session_index.jsonl");
        let gs_path = self.paths.codex_home_dir.join(".codex-global-state.json");
        if !(state_db.is_file() && session_idx.is_file() && gs_path.is_file()) {
            return true;
        }
        let Ok(conn) = open_readonly_sqlite(&state_db) else {
            return true;
        };
        let has_thread = conn
            .query_row(
                "SELECT 1 FROM threads WHERE id = ?",
                [thread_id.as_str()],
                |_| Ok(true),
            )
            .unwrap_or(false);
        let has_proj = conn
            .query_row(
                "SELECT 1 FROM project_roots WHERE project_id = ?",
                [binding.codex_project_id.as_str()],
                |_| Ok(true),
            )
            .unwrap_or(false);
        if !(has_thread && has_proj) {
            return true;
        }
        let Ok(idx_text) = fs::read_to_string(&session_idx) else {
            return true;
        };
        let found_idx = idx_text.lines().any(|ln| {
            let t = ln.trim();
            t.starts_with('{')
                && t.contains(thread_id.as_str())
                && serde_json::from_str::<Value>(t)
                    .ok()
                    .and_then(|v| v.get("id").and_then(Value::as_str).map(str::to_owned))
                    .as_deref()
                    == Some(thread_id.as_str())
        });
        if !found_idx {
            return true;
        }
        let Ok(gs_text) = fs::read_to_string(&gs_path) else {
            return true;
        };
        let Ok(Value::Object(gs)) = serde_json::from_str::<Value>(&gs_text) else {
            return true;
        };
        let has_assign = gs
            .get("thread-project-assignments")
            .and_then(Value::as_object)
            .is_some_and(|m| m.contains_key(thread_id.as_str()));
        let has_local = gs
            .get("local-projects")
            .and_then(Value::as_object)
            .is_some_and(|m| m.contains_key(binding.codex_legacy_project_id.as_str()));
        !(has_assign && has_local)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn ensure_companion_stores_complete(
        &self,
        mut binding: WorkspaceProjectBinding,
        cascade_id: &CascadeId,
        codex_thread_id: &CodexThreadId,
        agy_ledger: Option<&TrajectoryLedger>,
        codex_ledger: Option<&TrajectoryLedger>,
        locks: &HostLockSnapshot,
        mode: TransportMode,
        source_tag: AgyGuiSafeSource,
        lineage: &SubagentLineage,
        guard: &PresenceWalGuard,
        registry: &ProjectRegistry,
    ) -> WorkspaceProjectBinding {
        if mode == TransportMode::OnlineRpc || agy_ledger.is_none() || codex_ledger.is_none() {
            return binding;
        }
        let a_ledger = agy_ledger.unwrap_or_else(|| unreachable!());
        let c_ledger = codex_ledger.unwrap_or_else(|| unreachable!());

        if let Ok(conv_db) = validated_child_path(
            &self.paths.agy_data_dir,
            &["conversations", &format!("{}.db", cascade_id.as_str())],
            "cascade_id",
        ) && conv_db.is_file()
            && locks.can_write_agy_offline()
            && self.agy_companions_missing(&binding, cascade_id, lineage)
            && let Ok(_lock) = guard.acquire_offline_write_locks(
                HostKind::Antigravity,
                Some(cascade_id),
                Some(codex_thread_id),
                false,
                false,
            )
            && let Ok((cands, _)) = self.candidate_target_paths(
                HostKind::Antigravity,
                Some(cascade_id),
                None,
                Some(&binding),
                Some(lineage),
                false,
            )
            && let Ok(backup) = guard.create_offline_backup(
                HostKind::Antigravity,
                cascade_id.as_str(),
                &expand_existing_with_sidecars(&cands),
                &cands,
                &[],
            )
        {
            let res: Result<WorkspaceProjectBinding, HostError> = (|| {
                let updated = registry.ensure_agy_binding(&binding.workspace_path)?;
                let raw_rows = self.read_persisted_agy_steps(&conv_db)?;
                let persisted = decode_agy_trajectory(
                    updated.parsed_path(),
                    updated.parsed_uri(),
                    &raw_rows,
                    Some(&a_ledger.title),
                )?;
                let eff_lin = resolve_effective_agy_lineage(
                    lineage.parent_cascade_id.as_ref(),
                    Some(lineage),
                )?;
                self.write_agy_companion_stores(
                    &updated,
                    cascade_id,
                    &persisted,
                    raw_rows.len() as u32,
                    &conv_db,
                    source_tag,
                    &eff_lin,
                )?;
                Ok(updated)
            })();
            match res {
                Ok(b) => binding = b,
                Err(_) => {
                    backup.restore();
                }
            }
        }

        if locks.can_write_codex_offline()
            && self.codex_companions_missing(&binding, codex_thread_id)
            && let Ok(_lock) = guard.acquire_offline_write_locks(
                HostKind::Codex,
                Some(cascade_id),
                Some(codex_thread_id),
                false,
                false,
            )
            && let Ok((rollout_path, _)) = self.find_codex_rollout_and_title(codex_thread_id)
            && let Ok((cands, _)) =
                self.candidate_target_paths(HostKind::Codex, None, None, None, None, false)
            && let Ok(backup) = guard.create_offline_backup(
                HostKind::Codex,
                codex_thread_id.as_str(),
                &expand_existing_with_sidecars(&cands),
                &cands,
                &[],
            )
        {
            let res: Result<WorkspaceProjectBinding, HostError> = (|| {
                let updated = registry.ensure_codex_binding(&binding.workspace_path)?;
                self.write_codex_companion_stores(
                    &updated,
                    codex_thread_id,
                    &rollout_path,
                    c_ledger,
                    Some(lineage),
                    lineage.parent_codex_thread_id.as_ref(),
                )?;
                Ok(updated)
            })();
            match res {
                Ok(b) => binding = b,
                Err(_) => {
                    backup.restore();
                }
            }
        }

        let _ = HashMap::<String, String>::new();
        binding
    }
}

fn resolved_root_or_self(p: &Path) -> &Path {
    p
}
