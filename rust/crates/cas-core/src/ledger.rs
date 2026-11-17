//! Canonical indexed event-ledger IR, `B2` interrupted-turn rescue, and pure host projections.

use std::collections::{BTreeMap, HashMap, HashSet};

use indexmap::IndexMap;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::error::CoreError;
use crate::events::{
    AnnotationPbtxtProjection, CanonicalEvent, CascadeSummaryProjection,
    CodexThreadSummaryProjection, CompletedTurnWatermark, EventOriginAnchor,
    INTERRUPTED_TOOL_OUTPUT, INTERRUPTED_TURN_TEXT, SubagentLineage, SummariesStateProjection,
    TaskBoundaryKind, TranscriptRowProjection, canonical_event_digest_payload,
    is_host_framing_event, is_nonterminal_work_or_abort_event, is_terminal_completion_event,
    normalize_single_line_query, resolve_canonical_title,
};
use crate::id::{
    AgyGuiSafeSource, AgyProjectId, CascadeId, CodexProjectId, CodexThreadId, ContentDigest,
    LedgerSeqNo, RolloutLineNo, StepIndex, ToolCallId, TurnId, WorkspacePath, WorkspaceRootUri,
};
use crate::payloads::{dump_canonical_json, serialize_tool_payload_arguments_json};
use crate::wire_types::parse_iso_to_seconds_nanos;

type IndexResult = (
    Vec<CanonicalEvent>,
    BTreeMap<StepIndex, Vec<LedgerSeqNo>>,
    HashMap<ToolCallId, (LedgerSeqNo, Option<LedgerSeqNo>)>,
    IndexMap<TurnId, Vec<LedgerSeqNo>>,
);

fn index_ledger_events(events: &[CanonicalEvent], recompute_agy_step_indices: bool) -> IndexResult {
    let mut normalized = Vec::with_capacity(events.len());
    let mut by_step: BTreeMap<StepIndex, Vec<LedgerSeqNo>> = BTreeMap::new();
    let mut by_call: HashMap<ToolCallId, (LedgerSeqNo, Option<LedgerSeqNo>)> = HashMap::new();
    let mut by_turn: IndexMap<TurnId, Vec<LedgerSeqNo>> = IndexMap::new();

    let mut next_step: u32 = 0;
    let mut next_line: u64 = 1;
    let mut planner_step: Option<StepIndex> = None;
    let mut planner_turn: Option<TurnId> = None;
    let mut planner_flags = (false, false, false);

    for (idx, ev_ref) in events.iter().enumerate() {
        let seq_no = LedgerSeqNo(idx);
        let mut ev = ev_ref.clone();
        let anchor = ev.anchor();
        let turn_id = anchor.turn_id.clone();

        let step_idx = if !recompute_agy_step_indices && anchor.agy_step_index.is_some() {
            let s = anchor.agy_step_index.unwrap_or(StepIndex(0));
            next_step = next_step.max(s.0 + 1);
            planner_step = None;
            planner_flags = (false, false, false);
            s
        } else if matches!(
            ev,
            CanonicalEvent::Thinking { .. }
                | CanonicalEvent::AssistantText { .. }
                | CanonicalEvent::ToolCall { .. }
        ) {
            let (has_think, has_text, has_tools) = planner_flags;
            let can_reuse = planner_step.is_some()
                && planner_turn.as_ref() == Some(&turn_id)
                && match ev {
                    CanonicalEvent::Thinking { .. } => !(has_think || has_text || has_tools),
                    CanonicalEvent::AssistantText { .. } => !(has_text || has_tools),
                    CanonicalEvent::ToolCall { .. } => true,
                    _ => false,
                };
            if !can_reuse {
                planner_step = Some(StepIndex(next_step));
                next_step += 1;
                planner_turn = Some(turn_id.clone());
                planner_flags = (false, false, false);
            }
            let (ht, hx, hl) = planner_flags;
            planner_flags = (
                ht || matches!(ev, CanonicalEvent::Thinking { .. }),
                hx || matches!(ev, CanonicalEvent::AssistantText { .. }),
                hl || matches!(ev, CanonicalEvent::ToolCall { .. }),
            );
            planner_step.unwrap_or(StepIndex(0))
        } else {
            planner_step = None;
            planner_flags = (false, false, false);
            let s = StepIndex(next_step);
            next_step += 1;
            s
        };

        let line_no = if let Some(ln) = anchor.codex_line_no {
            next_line = next_line.max(ln.0 + 1);
            ln
        } else {
            let ln = RolloutLineNo(next_line);
            next_line += 1;
            ln
        };

        if recompute_agy_step_indices
            || ev.anchor().agy_step_index != Some(step_idx)
            || ev.anchor().codex_line_no != Some(line_no)
        {
            let a = ev.anchor_mut();
            a.agy_step_index = Some(step_idx);
            a.codex_line_no = Some(line_no);
        }

        match &ev {
            CanonicalEvent::ToolCall { call_id, .. } => {
                by_call.insert(call_id.clone(), (seq_no, None));
            }
            CanonicalEvent::ToolResult { call_id, .. } => {
                let first_seq = by_call.get(call_id).map_or(seq_no, |(c, _)| *c);
                by_call.insert(call_id.clone(), (first_seq, Some(seq_no)));
            }
            _ => {}
        }

        by_step.entry(step_idx).or_default().push(seq_no);
        by_turn.entry(turn_id).or_default().push(seq_no);
        normalized.push(ev);
    }

    (normalized, by_step, by_call, by_turn)
}

fn is_turn_completed(
    turn_events: &[&CanonicalEvent],
    by_call: &HashMap<ToolCallId, (LedgerSeqNo, Option<LedgerSeqNo>)>,
) -> bool {
    if turn_events.is_empty() {
        return true;
    }
    let all_calls_resolved = turn_events.iter().all(|tev| match tev {
        CanonicalEvent::ToolCall { call_id, .. } => {
            by_call.get(call_id).is_some_and(|(_, res)| res.is_some())
        }
        _ => true,
    });
    let has_active_agy = turn_events.iter().any(|tev| {
        tev.anchor()
            .host_extensions
            .iter()
            .any(|ext| ext.extension_type == "agy_step_status")
    });
    let last_work = turn_events
        .iter()
        .enumerate()
        .filter(|(_, tev)| is_nonterminal_work_or_abort_event(tev))
        .map(|(i, _)| i as isize)
        .max()
        .unwrap_or(-1);
    let last_comp = turn_events
        .iter()
        .enumerate()
        .filter(|(_, tev)| is_terminal_completion_event(tev))
        .map(|(i, _)| i as isize)
        .max()
        .unwrap_or(-1);

    all_calls_resolved && !has_active_agy && last_work <= last_comp
}

fn build_turn_watermarks(
    normalized_events: &[CanonicalEvent],
    by_turn: &IndexMap<TurnId, Vec<LedgerSeqNo>>,
    by_call: &HashMap<ToolCallId, (LedgerSeqNo, Option<LedgerSeqNo>)>,
) -> (Vec<CompletedTurnWatermark>, bool) {
    let mut watermarks = Vec::new();
    let mut hasher = Sha256::new();
    let turn_count = by_turn.len();

    for (turn_pos, (turn_id, seq_list)) in by_turn.iter().enumerate() {
        let turn_events: Vec<&CanonicalEvent> =
            seq_list.iter().map(|s| &normalized_events[s.0]).collect();
        if turn_pos + 1 == turn_count && !is_turn_completed(&turn_events, by_call) {
            return (watermarks, true);
        }

        let mut max_step_idx = StepIndex(0);
        let mut max_line_no = RolloutLineNo(1);
        for tev in &turn_events {
            if !is_host_framing_event(tev) {
                let digest_map = canonical_event_digest_payload(tev);
                let json_str = dump_canonical_json(&digest_map);
                hasher.update(json_str.as_bytes());
                hasher.update(b"\n");
            }
            if let Some(s) = tev.anchor().agy_step_index {
                if s > max_step_idx {
                    max_step_idx = s;
                }
            }
            if let Some(ln) = tev.anchor().codex_line_no {
                if ln > max_line_no {
                    max_line_no = ln;
                }
            }
        }
        let digest_hex = hex::encode(hasher.clone().finalize());
        if let (Some(&last_seq), Ok(digest)) = (seq_list.last(), ContentDigest::parse(&digest_hex))
        {
            watermarks.push(CompletedTurnWatermark {
                turn_id: turn_id.clone(),
                last_ledger_seq: last_seq,
                last_agy_step_index: max_step_idx,
                last_codex_line_no: max_line_no,
                boundary_digest: digest,
            });
        }
    }

    (watermarks, false)
}

/// Append-only canonical event ledger with deterministic secondary lookup indices.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrajectoryLedger {
    pub workspace_path: WorkspacePath,
    pub workspace_uri: WorkspaceRootUri,
    pub title: String,
    pub events: Vec<CanonicalEvent>,
    pub by_step_index: BTreeMap<StepIndex, Vec<LedgerSeqNo>>,
    pub by_call_id: HashMap<ToolCallId, (LedgerSeqNo, Option<LedgerSeqNo>)>,
    pub by_turn_id: IndexMap<TurnId, Vec<LedgerSeqNo>>,
    pub completed_turn_watermarks: Vec<CompletedTurnWatermark>,
    pub has_inflight_turn: bool,
}

impl TrajectoryLedger {
    /// Index a sequence of canonical events and compute completed-turn watermarks.
    #[must_use]
    pub fn from_events(
        workspace_path: WorkspacePath,
        workspace_uri: WorkspaceRootUri,
        events: &[CanonicalEvent],
        explicit_title_override: Option<&str>,
        recompute_agy_step_indices: bool,
    ) -> Self {
        let resolved_title =
            resolve_canonical_title(events, explicit_title_override, None, None, None);
        let (normalized, by_step_index, by_call_id, by_turn_id) =
            index_ledger_events(events, recompute_agy_step_indices);
        let (completed_turn_watermarks, has_inflight_turn) =
            build_turn_watermarks(&normalized, &by_turn_id, &by_call_id);

        Self {
            workspace_path,
            workspace_uri,
            title: resolved_title,
            events: normalized,
            by_step_index,
            by_call_id,
            by_turn_id,
            completed_turn_watermarks,
            has_inflight_turn,
        }
    }

    #[must_use]
    pub fn latest_watermark(&self) -> Option<&CompletedTurnWatermark> {
        self.completed_turn_watermarks.last()
    }

    #[must_use]
    pub fn find_watermark(&self, turn_id: &TurnId) -> Option<&CompletedTurnWatermark> {
        self.completed_turn_watermarks
            .iter()
            .find(|wm| &wm.turn_id == turn_id)
    }

    #[must_use]
    pub fn events_for_step(&self, step_index: StepIndex) -> Vec<&CanonicalEvent> {
        self.by_step_index
            .get(&step_index)
            .map(|seqs| seqs.iter().map(|s| &self.events[s.0]).collect())
            .unwrap_or_default()
    }

    /// Return the `(ToolCall, Option<ToolResult>)` pair for `call_id`.
    ///
    /// # Errors
    /// Returns [`CoreError::LedgerLookup`] if `call_id` is absent or does not point to a tool call.
    pub fn call_and_result(
        &self,
        call_id: &ToolCallId,
    ) -> Result<(&CanonicalEvent, Option<&CanonicalEvent>), CoreError> {
        let Some(&(call_seq, res_seq)) = self.by_call_id.get(call_id) else {
            return Err(CoreError::LedgerLookup(format!(
                "ToolCallId {call_id:?} not found in ledger."
            )));
        };
        let call_ev = &self.events[call_seq.0];
        if !matches!(call_ev, CanonicalEvent::ToolCall { .. }) {
            return Err(CoreError::LedgerLookup(format!(
                "Expected ToolCall at seq {}, found {}",
                call_seq.0,
                call_ev.kind_str()
            )));
        }
        let res_ev = res_seq
            .filter(|rs| *rs != call_seq)
            .map(|rs| &self.events[rs.0])
            .filter(|ev| matches!(ev, CanonicalEvent::ToolResult { .. }));
        Ok((call_ev, res_ev))
    }

    /// Return completed events strictly after `cursor_turn_id` (or all completed events when `None`).
    ///
    /// # Errors
    /// Returns [`CoreError::LedgerLookup`] if `cursor_turn_id` is `Some` and not present in completed watermarks.
    pub fn slice_after_watermark(
        &self,
        cursor_turn_id: Option<&TurnId>,
    ) -> Result<&[CanonicalEvent], CoreError> {
        let Some(last_wm) = self.completed_turn_watermarks.last() else {
            if let Some(tid) = cursor_turn_id {
                return Err(CoreError::LedgerLookup(format!(
                    "Turn {tid:?} not found in empty watermarks."
                )));
            }
            return Ok(&[]);
        };
        let last_completed_seq = last_wm.last_ledger_seq.0;
        let Some(tid) = cursor_turn_id else {
            return Ok(&self.events[..=last_completed_seq]);
        };
        let Some(matched_wm) = self.find_watermark(tid) else {
            return Err(CoreError::LedgerLookup(format!(
                "TurnId {tid:?} not found in completed_turn_watermarks."
            )));
        };
        let start_seq = matched_wm.last_ledger_seq.0 + 1;
        if start_seq > last_completed_seq {
            Ok(&[])
        } else {
            Ok(&self.events[start_seq..=last_completed_seq])
        }
    }

    #[must_use]
    pub fn project_transcript_rows(&self) -> Vec<TranscriptRowProjection> {
        project_ledger_transcript_rows(self)
    }

    #[must_use]
    pub fn project_annotation_pbtxt(
        &self,
        cascade_id: &CascadeId,
        viewed_at_iso: Option<&str>,
    ) -> AnnotationPbtxtProjection {
        let ts = viewed_at_iso
            .map(str::to_owned)
            .or_else(|| self.events.last().map(|e| e.anchor().timestamp_iso.clone()))
            .unwrap_or_else(|| "2026-01-01T00:00:00Z".to_owned());
        AnnotationPbtxtProjection {
            cascade_id: cascade_id.clone(),
            title: self.title.clone(),
            last_user_view_time_iso: ts,
            unknown_wire_fields: Vec::new(),
        }
    }

    /// Project this ledger into a validated [`CascadeSummaryProjection`].
    ///
    /// # Errors
    /// Propagates validation errors from [`CascadeSummaryProjection::try_new`].
    pub fn project_agy_cascade_summary(
        &self,
        cascade_id: &CascadeId,
        project_id: &AgyProjectId,
        viewed_at_iso: Option<&str>,
        source: AgyGuiSafeSource,
        lineage: Option<&SubagentLineage>,
    ) -> Result<CascadeSummaryProjection, CoreError> {
        project_ledger_cascade_summary(self, cascade_id, project_id, viewed_at_iso, source, lineage)
    }

    /// Merge an updated cascade summary into an existing Hub summaries state.
    #[must_use]
    pub fn project_agy_hub_summaries_state(
        &self,
        existing_state: Option<&SummariesStateProjection>,
        updated_summary: CascadeSummaryProjection,
        conv_db_path: &str,
        conv_db_mtime: i64,
    ) -> SummariesStateProjection {
        let mut summaries = existing_state.map_or_else(BTreeMap::new, |s| s.summaries.clone());
        let mut file_mod_times =
            existing_state.map_or_else(BTreeMap::new, |s| s.file_mod_times.clone());
        let unknown_wire_fields =
            existing_state.map_or_else(Vec::new, |s| s.unknown_wire_fields.clone());
        summaries.insert(updated_summary.cascade_id.clone(), updated_summary);
        file_mod_times.insert(conv_db_path.to_owned(), conv_db_mtime);
        SummariesStateProjection {
            summaries,
            file_mod_times,
            unknown_wire_fields,
        }
    }

    /// Project this ledger into a [`CodexThreadSummaryProjection`].
    ///
    /// # Errors
    /// Propagates validation errors from [`CodexThreadSummaryProjection::try_new`].
    pub fn project_codex_thread_summary(
        &self,
        thread_id: &CodexThreadId,
        project_id: &CodexProjectId,
    ) -> Result<CodexThreadSummaryProjection, CoreError> {
        project_ledger_codex_thread_summary(self, thread_id, project_id)
    }
}

/// Seal an incomplete turn (final when `target_turn_id` is `None`) with canceled tool outputs and interruption text.
#[must_use]
pub fn rescue_interrupted_turn(
    ledger: &TrajectoryLedger,
    target_turn_id: Option<&TurnId>,
) -> (TrajectoryLedger, bool) {
    if ledger.events.is_empty() || ledger.by_turn_id.is_empty() {
        return (ledger.clone(), false);
    }
    let target_tid = match target_turn_id {
        None => {
            if !ledger.has_inflight_turn {
                return (ledger.clone(), false);
            }
            let Some((tid, _)) = ledger.by_turn_id.last() else {
                return (ledger.clone(), false);
            };
            tid.clone()
        }
        Some(tid) => {
            if !ledger.by_turn_id.contains_key(tid) {
                return (ledger.clone(), false);
            }
            tid.clone()
        }
    };

    let seq_list = &ledger.by_turn_id[&target_tid];
    let raw_target: Vec<&CanonicalEvent> = seq_list.iter().map(|s| &ledger.events[s.0]).collect();
    if is_turn_completed(&raw_target, &ledger.by_call_id) {
        return (ledger.clone(), false);
    }

    let target_seqs: HashSet<usize> = seq_list.iter().map(|s| s.0).collect();
    let max_target_seq = target_seqs.iter().copied().max().unwrap_or(0);
    let mut rescued_events = Vec::with_capacity(ledger.events.len() + 4);
    let mut turn_slice: Vec<CanonicalEvent> = Vec::new();
    let mut stripped_on_asst = false;

    for (idx, ev_ref) in ledger.events[..=max_target_seq].iter().enumerate() {
        let mut ev = ev_ref.clone();
        if target_seqs.contains(&idx)
            && ev
                .anchor()
                .host_extensions
                .iter()
                .any(|ext| ext.extension_type == "agy_step_status")
        {
            if matches!(ev, CanonicalEvent::AssistantText { .. }) {
                stripped_on_asst = true;
            }
            ev.anchor_mut()
                .host_extensions
                .retain(|ext| ext.extension_type != "agy_step_status");
        }
        if target_seqs.contains(&idx) {
            turn_slice.push(ev.clone());
        }
        rescued_events.push(ev);
    }

    let Some(last_ev) = turn_slice.last().cloned() else {
        return (ledger.clone(), false);
    };
    let last_ts = last_ev.anchor().timestamp_iso.clone();
    let snapshot_slice = turn_slice.clone();
    for tev in snapshot_slice {
        if let CanonicalEvent::ToolCall {
            anchor,
            call_id,
            tool_name,
            payload,
            ..
        } = tev
        {
            let unresolved = ledger
                .by_call_id
                .get(&call_id)
                .is_none_or(|(_, res)| res.is_none());
            if unresolved {
                let res_ev = CanonicalEvent::ToolResult {
                    anchor: EventOriginAnchor::new(
                        anchor.origin_host,
                        target_tid.clone(),
                        last_ts.clone(),
                    ),
                    call_id,
                    tool_name,
                    output_text: INTERRUPTED_TOOL_OUTPUT.to_owned(),
                    is_error: true,
                    payload,
                    full_output_uri: None,
                };
                turn_slice.push(res_ev.clone());
                rescued_events.push(res_ev);
            }
        }
    }

    let last_work = turn_slice
        .iter()
        .enumerate()
        .filter(|(_, e)| is_nonterminal_work_or_abort_event(e))
        .map(|(i, _)| i as isize)
        .max()
        .unwrap_or(-1);
    let last_asst = turn_slice
        .iter()
        .enumerate()
        .filter(|(_, e)| matches!(e, CanonicalEvent::AssistantText { .. }))
        .map(|(i, _)| i as isize)
        .max()
        .unwrap_or(-1);
    if stripped_on_asst || last_asst == -1 || last_work > last_asst {
        rescued_events.push(CanonicalEvent::AssistantText {
            anchor: EventOriginAnchor::new(last_ev.anchor().origin_host, target_tid, last_ts),
            text: INTERRUPTED_TURN_TEXT.to_owned(),
            message_id: None,
            stop_reason: None,
            generator_model: None,
            token_usage: Default::default(),
        });
    }

    for trailing in &ledger.events[max_target_seq + 1..] {
        let mut t = trailing.clone();
        t.anchor_mut().agy_step_index = None;
        t.anchor_mut().codex_line_no = None;
        rescued_events.push(t);
    }

    (
        TrajectoryLedger::from_events(
            ledger.workspace_path.clone(),
            ledger.workspace_uri.clone(),
            &rescued_events,
            Some(&ledger.title),
            false,
        ),
        true,
    )
}

/// Seal a stopped trailing incomplete turn with canceled tool outputs and interruption notice.
#[must_use]
pub fn rescue_interrupted_ledger(ledger: &TrajectoryLedger) -> (TrajectoryLedger, bool) {
    rescue_interrupted_turn(ledger, None)
}

/// Derive single-line session preview text from user inputs, assistant messages, or title.
#[must_use]
pub fn derive_ledger_preview_text(ledger: &TrajectoryLedger) -> String {
    for ev in &ledger.events {
        if let CanonicalEvent::UserInput { query_text, .. } = ev {
            if !query_text.trim().is_empty() {
                let normalized = normalize_single_line_query(query_text, 160);
                if !normalized.is_empty() {
                    return normalized;
                }
            }
        }
    }
    for ev in &ledger.events {
        if let CanonicalEvent::AssistantText { text, .. } = ev {
            if !text.trim().is_empty() {
                let normalized = normalize_single_line_query(text, 160);
                if !normalized.is_empty() {
                    return normalized;
                }
            }
        }
    }
    if !ledger.title.trim().is_empty() {
        ledger.title.clone()
    } else {
        "Synchronized Session".to_owned()
    }
}

fn project_planner_transcript_row(
    step_idx: StepIndex,
    created_at: String,
    step_events: &[&CanonicalEvent],
) -> TranscriptRowProjection {
    let mut thinking_parts = Vec::new();
    let mut text_parts = Vec::new();
    let mut tool_calls = Vec::new();
    let mut in_tok = 0_i64;
    let mut out_tok = 0_i64;
    let mut cache_tok = 0_i64;

    for ev in step_events {
        match ev {
            CanonicalEvent::Thinking {
                summary_text,
                raw_thinking,
                token_usage,
                ..
            } => {
                if !summary_text.is_empty() {
                    thinking_parts.push(summary_text.clone());
                } else if let Some(rt) = raw_thinking {
                    if !rt.is_empty() {
                        thinking_parts.push(rt.clone());
                    }
                }
                in_tok = in_tok.max(token_usage.input_tokens);
                out_tok = out_tok.max(token_usage.output_tokens);
                cache_tok = cache_tok.max(token_usage.cache_read_tokens);
            }
            CanonicalEvent::AssistantText {
                text, token_usage, ..
            } => {
                if !text.is_empty() {
                    text_parts.push(text.clone());
                }
                in_tok = in_tok.max(token_usage.input_tokens);
                out_tok = out_tok.max(token_usage.output_tokens);
                cache_tok = cache_tok.max(token_usage.cache_read_tokens);
            }
            CanonicalEvent::ToolCall {
                call_id,
                tool_name,
                payload,
                ..
            } => {
                let args = serialize_tool_payload_arguments_json(payload);
                let mut tc = BTreeMap::new();
                tc.insert("id".to_owned(), Value::String(call_id.as_str().to_owned()));
                tc.insert("name".to_owned(), Value::String(tool_name.clone()));
                tc.insert("arguments_json".to_owned(), Value::String(args));
                tool_calls.push(tc);
            }
            _ => {}
        }
    }

    TranscriptRowProjection {
        step_index: step_idx,
        source: "MODEL".to_owned(),
        step_type_name: "PLANNER_RESPONSE".to_owned(),
        status_name: "DONE".to_owned(),
        created_at,
        content: text_parts.join("\n"),
        thinking: if thinking_parts.is_empty() {
            None
        } else {
            Some(thinking_parts.join("\n"))
        },
        tool_calls_json: tool_calls,
        media_uris: Vec::new(),
        input_tokens: in_tok,
        output_tokens: out_tok,
        cache_read_tokens: cache_tok,
        truncated_fields: Vec::new(),
    }
}

/// Project [`TrajectoryLedger`] steps into Antigravity `transcript.jsonl` view rows.
#[must_use]
pub fn project_ledger_transcript_rows(ledger: &TrajectoryLedger) -> Vec<TranscriptRowProjection> {
    let mut rows = Vec::new();
    for &step_idx in ledger.by_step_index.keys() {
        let step_events = ledger.events_for_step(step_idx);
        let Some(first) = step_events.first() else {
            continue;
        };
        let created_at = if first.anchor().timestamp_iso.is_empty() {
            "2026-01-01T00:00:00Z".to_owned()
        } else {
            first.anchor().timestamp_iso.clone()
        };
        match first {
            CanonicalEvent::Thinking { .. }
            | CanonicalEvent::AssistantText { .. }
            | CanonicalEvent::ToolCall { .. } => {
                rows.push(project_planner_transcript_row(
                    step_idx,
                    created_at,
                    &step_events,
                ));
            }
            CanonicalEvent::UserInput {
                query_text, media, ..
            } => {
                let media_uris = media
                    .iter()
                    .map(|m| {
                        let mut mm = BTreeMap::new();
                        mm.insert("mime_type".to_owned(), m.mime_type.clone());
                        mm.insert("uri".to_owned(), m.uri.clone());
                        mm
                    })
                    .collect();
                rows.push(TranscriptRowProjection {
                    step_index: step_idx,
                    source: "USER_EXPLICIT".to_owned(),
                    step_type_name: "USER_INPUT".to_owned(),
                    status_name: "DONE".to_owned(),
                    created_at,
                    content: query_text.clone(),
                    thinking: None,
                    tool_calls_json: Vec::new(),
                    media_uris,
                    input_tokens: 0,
                    output_tokens: 0,
                    cache_read_tokens: 0,
                    truncated_fields: Vec::new(),
                });
            }
            CanonicalEvent::ToolResult {
                output_text,
                is_error,
                ..
            } => {
                rows.push(TranscriptRowProjection {
                    step_index: step_idx,
                    source: "MODEL".to_owned(),
                    step_type_name: "GENERIC".to_owned(),
                    status_name: if *is_error {
                        "ERROR".to_owned()
                    } else {
                        "DONE".to_owned()
                    },
                    created_at,
                    content: output_text.clone(),
                    thinking: None,
                    tool_calls_json: Vec::new(),
                    media_uris: Vec::new(),
                    input_tokens: 0,
                    output_tokens: 0,
                    cache_read_tokens: 0,
                    truncated_fields: Vec::new(),
                });
            }
            CanonicalEvent::Checkpoint {
                session_summary,
                user_intent,
                ..
            } => {
                rows.push(TranscriptRowProjection {
                    step_index: step_idx,
                    source: "SYSTEM".to_owned(),
                    step_type_name: "CHECKPOINT".to_owned(),
                    status_name: "DONE".to_owned(),
                    created_at,
                    content: if session_summary.is_empty() {
                        user_intent.clone()
                    } else {
                        session_summary.clone()
                    },
                    thinking: None,
                    tool_calls_json: Vec::new(),
                    media_uris: Vec::new(),
                    input_tokens: 0,
                    output_tokens: 0,
                    cache_read_tokens: 0,
                    truncated_fields: Vec::new(),
                });
            }
            CanonicalEvent::TaskBoundary {
                boundary_kind,
                task_summary,
                task_name,
                ..
            } => {
                rows.push(TranscriptRowProjection {
                    step_index: step_idx,
                    source: "MODEL".to_owned(),
                    step_type_name: if *boundary_kind == TaskBoundaryKind::NotifyUser {
                        "NOTIFY_USER".to_owned()
                    } else {
                        "TASK_BOUNDARY".to_owned()
                    },
                    status_name: "DONE".to_owned(),
                    created_at,
                    content: if task_summary.is_empty() {
                        task_name.clone()
                    } else {
                        task_summary.clone()
                    },
                    thinking: None,
                    tool_calls_json: Vec::new(),
                    media_uris: Vec::new(),
                    input_tokens: 0,
                    output_tokens: 0,
                    cache_read_tokens: 0,
                    truncated_fields: Vec::new(),
                });
            }
            CanonicalEvent::SystemMessage { message_text, .. } => {
                rows.push(TranscriptRowProjection {
                    step_index: step_idx,
                    source: "SYSTEM".to_owned(),
                    step_type_name: "SYSTEM_MESSAGE".to_owned(),
                    status_name: "DONE".to_owned(),
                    created_at,
                    content: message_text.clone(),
                    thinking: None,
                    tool_calls_json: Vec::new(),
                    media_uris: Vec::new(),
                    input_tokens: 0,
                    output_tokens: 0,
                    cache_read_tokens: 0,
                    truncated_fields: Vec::new(),
                });
            }
        }
    }
    rows
}

/// Project [`TrajectoryLedger`] into [`CascadeSummaryProjection`].
///
/// # Errors
/// Propagates [`CoreError::InvalidSummary`] if invariants fail.
pub fn project_ledger_cascade_summary(
    ledger: &TrajectoryLedger,
    cascade_id: &CascadeId,
    project_id: &AgyProjectId,
    viewed_at_iso: Option<&str>,
    source: AgyGuiSafeSource,
    lineage: Option<&SubagentLineage>,
) -> Result<CascadeSummaryProjection, CoreError> {
    let created_iso = ledger.events.first().map_or_else(
        || "2026-01-01T00:00:00Z".to_owned(),
        |e| e.anchor().timestamp_iso.clone(),
    );
    let updated_iso = viewed_at_iso.map_or_else(
        || {
            ledger
                .events
                .last()
                .map_or_else(|| created_iso.clone(), |e| e.anchor().timestamp_iso.clone())
        },
        str::to_owned,
    );
    let annot = ledger.project_annotation_pbtxt(cascade_id, Some(&updated_iso));
    let parent_cid = lineage.and_then(|l| l.parent_cascade_id.clone());
    let root_cid = lineage.and_then(|l| l.root_cascade_id.clone().or_else(|| parent_cid.clone()));
    let (type_name, role) = lineage.map_or_else(
        || (String::new(), String::new()),
        |l| (l.agent_type_name.clone(), l.agent_role.clone()),
    );
    let agent_name = if !role.is_empty() {
        role.clone()
    } else {
        type_name.clone()
    };

    CascadeSummaryProjection::try_new(
        cascade_id.clone(),
        project_id.clone(),
        vec![ledger.workspace_uri.clone()],
        source,
        ledger.title.clone(),
        derive_ledger_preview_text(ledger),
        ledger.by_step_index.len() as u32,
        created_iso,
        updated_iso,
        annot,
        lineage.map_or(0, |l| l.nesting_depth),
        parent_cid,
        root_cid,
        agent_name,
        type_name,
        role,
    )
}

/// Project [`TrajectoryLedger`] into [`CodexThreadSummaryProjection`].
///
/// # Errors
/// Propagates [`CoreError::InvalidSummary`] if invariants fail.
pub fn project_ledger_codex_thread_summary(
    ledger: &TrajectoryLedger,
    thread_id: &CodexThreadId,
    project_id: &CodexProjectId,
) -> Result<CodexThreadSummaryProjection, CoreError> {
    let created_iso = ledger.events.first().map_or_else(
        || "2026-01-01T00:00:00Z".to_owned(),
        |e| e.anchor().timestamp_iso.clone(),
    );
    let updated_iso = ledger
        .events
        .last()
        .map_or_else(|| created_iso.clone(), |e| e.anchor().timestamp_iso.clone());
    let (c_sec, c_nanos) = parse_iso_to_seconds_nanos(&created_iso);
    let (u_sec, u_nanos) = parse_iso_to_seconds_nanos(&updated_iso);
    let created_ms = c_sec * 1000 + i64::from(c_nanos / 1_000_000);
    let updated_ms = created_ms.max(u_sec * 1000 + i64::from(u_nanos / 1_000_000));
    CodexThreadSummaryProjection::try_new(
        thread_id.clone(),
        project_id.clone(),
        ledger.workspace_path.clone(),
        ledger.title.clone(),
        derive_ledger_preview_text(ledger),
        created_ms,
        updated_ms,
    )
}
