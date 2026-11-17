//! Canonical event model, host projections, and domain predicates.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::error::CoreError;
use crate::id::{
    AgyGuiSafeSource, AgyProjectId, CascadeId, CodexProjectId, CodexThreadId, ContentDigest,
    FORBIDDEN_AGY_PROJECT_IDS, HostKind, LedgerSeqNo, RolloutLineNo, StepIndex, ToolCallId,
    TrajectoryId, TurnId, WorkspacePath, WorkspaceRootUri,
};
use crate::payloads::{
    CanonicalToolPayload, safe_json_dict, serialize_tool_payload_arguments_json,
};
use crate::wire_types::UnknownWireField;

pub const INTERRUPTED_TOOL_OUTPUT: &str = "[Interrupted before tool completion]";
pub const INTERRUPTED_TURN_TEXT: &str = "[Turn interrupted before completion]";

/// Opaque or typed host extension envelope attached to an event anchor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostExtensionEnvelope {
    pub host: HostKind,
    pub extension_type: String,
    pub raw_payload: Vec<u8>,
    pub metadata: Vec<(String, String)>,
}

/// Origin provenance and round-trip metadata for a canonical event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventOriginAnchor {
    pub origin_host: HostKind,
    pub turn_id: TurnId,
    pub timestamp_iso: String,
    pub agy_step_index: Option<StepIndex>,
    pub codex_line_no: Option<RolloutLineNo>,
    pub client_message_id: Option<String>,
    pub unknown_wire_fields: Vec<UnknownWireField>,
    pub host_extensions: Vec<HostExtensionEnvelope>,
}

impl EventOriginAnchor {
    #[must_use]
    pub fn new(origin_host: HostKind, turn_id: TurnId, timestamp_iso: impl Into<String>) -> Self {
        Self {
            origin_host,
            turn_id,
            timestamp_iso: timestamp_iso.into(),
            agy_step_index: None,
            codex_line_no: None,
            client_message_id: None,
            unknown_wire_fields: Vec::new(),
            host_extensions: Vec::new(),
        }
    }
}

/// Token accounting attached to model thinking/assistant steps.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct TokenUsageAccounting {
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_read_tokens: i64,
    pub thinking_duration_ms: Option<i64>,
}

/// Media attachment on a user input event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaAttachment {
    pub mime_type: String,
    pub uri: String,
    pub byte_size: Option<i64>,
    pub caption: Option<String>,
}

/// Orthogonal subagent parent/child lineage.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SubagentLineage {
    pub parent_cascade_id: Option<CascadeId>,
    pub root_cascade_id: Option<CascadeId>,
    pub parent_codex_thread_id: Option<CodexThreadId>,
    pub nesting_depth: u32,
    pub agent_type_name: String,
    pub agent_role: String,
    pub spawn_step_index: Option<StepIndex>,
}

impl SubagentLineage {
    #[must_use]
    pub fn is_subagent(&self) -> bool {
        self.nesting_depth > 0 || self.spawn_step_index.is_some()
    }
}

/// Boundary variant for [`CanonicalEvent::TaskBoundary`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TaskBoundaryKind {
    TaskBoundary,
    NotifyUser,
}

impl TaskBoundaryKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::TaskBoundary => "task_boundary",
            Self::NotifyUser => "notify_user",
        }
    }
}

/// Unified host-neutral trajectory event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CanonicalEvent {
    UserInput {
        anchor: EventOriginAnchor,
        query_text: String,
        scope_items: Vec<String>,
        media: Vec<MediaAttachment>,
    },
    Thinking {
        anchor: EventOriginAnchor,
        summary_text: String,
        raw_thinking: Option<String>,
        thinking_signature: Option<Vec<u8>>,
        token_usage: TokenUsageAccounting,
    },
    AssistantText {
        anchor: EventOriginAnchor,
        text: String,
        message_id: Option<String>,
        stop_reason: Option<String>,
        generator_model: Option<String>,
        token_usage: TokenUsageAccounting,
    },
    ToolCall {
        anchor: EventOriginAnchor,
        call_id: ToolCallId,
        tool_name: String,
        tool_summary: String,
        payload: CanonicalToolPayload,
        thinking_signature: Option<Vec<u8>>,
    },
    ToolResult {
        anchor: EventOriginAnchor,
        call_id: ToolCallId,
        tool_name: String,
        output_text: String,
        is_error: bool,
        payload: CanonicalToolPayload,
        full_output_uri: Option<String>,
    },
    TaskBoundary {
        anchor: EventOriginAnchor,
        boundary_kind: TaskBoundaryKind,
        task_name: String,
        task_status: String,
        task_summary: String,
        mode: Option<String>,
        blocked_on_user: bool,
        review_uris: Vec<String>,
    },
    Checkpoint {
        anchor: EventOriginAnchor,
        checkpoint_index: i64,
        user_intent: String,
        session_summary: String,
        code_change_summary: String,
        conversation_title: Option<String>,
    },
    SystemMessage {
        anchor: EventOriginAnchor,
        event_type: String,
        message_text: String,
    },
}

impl CanonicalEvent {
    #[must_use]
    pub const fn kind_str(&self) -> &'static str {
        match self {
            Self::UserInput { .. } => "user_input",
            Self::Thinking { .. } => "thinking",
            Self::AssistantText { .. } => "assistant_text",
            Self::ToolCall { .. } => "tool_call",
            Self::ToolResult { .. } => "tool_result",
            Self::TaskBoundary { .. } => "task_boundary",
            Self::Checkpoint { .. } => "checkpoint",
            Self::SystemMessage { .. } => "system_message",
        }
    }

    #[must_use]
    pub fn anchor(&self) -> &EventOriginAnchor {
        match self {
            Self::UserInput { anchor, .. }
            | Self::Thinking { anchor, .. }
            | Self::AssistantText { anchor, .. }
            | Self::ToolCall { anchor, .. }
            | Self::ToolResult { anchor, .. }
            | Self::TaskBoundary { anchor, .. }
            | Self::Checkpoint { anchor, .. }
            | Self::SystemMessage { anchor, .. } => anchor,
        }
    }

    pub fn anchor_mut(&mut self) -> &mut EventOriginAnchor {
        match self {
            Self::UserInput { anchor, .. }
            | Self::Thinking { anchor, .. }
            | Self::AssistantText { anchor, .. }
            | Self::ToolCall { anchor, .. }
            | Self::ToolResult { anchor, .. }
            | Self::TaskBoundary { anchor, .. }
            | Self::Checkpoint { anchor, .. }
            | Self::SystemMessage { anchor, .. } => anchor,
        }
    }
}

/// Completed turn boundary watermark used by [`crate::state_machine::CursorForkStateMachine`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompletedTurnWatermark {
    pub turn_id: TurnId,
    pub last_ledger_seq: LedgerSeqNo,
    pub last_agy_step_index: StepIndex,
    pub last_codex_line_no: RolloutLineNo,
    pub boundary_digest: ContentDigest,
}

/// Pure view row for `brain/<cascade_id>/.system_generated/logs/transcript.jsonl`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TranscriptRowProjection {
    pub step_index: StepIndex,
    pub source: String,
    pub step_type_name: String,
    pub status_name: String,
    pub created_at: String,
    pub content: String,
    pub thinking: Option<String>,
    pub tool_calls_json: Vec<BTreeMap<String, Value>>,
    pub media_uris: Vec<BTreeMap<String, String>>,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_read_tokens: i64,
    pub truncated_fields: Vec<String>,
}

/// Pure view projection for `~/.gemini/antigravity/annotations/<cascade_id>.pbtxt`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnnotationPbtxtProjection {
    pub cascade_id: CascadeId,
    pub title: String,
    pub last_user_view_time_iso: String,
    pub unknown_wire_fields: Vec<UnknownWireField>,
}

/// Pure view projection for `conversation_summaries.db` and `agyhub_summaries_proto.pb`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CascadeSummaryProjection {
    pub cascade_id: CascadeId,
    pub project_id: AgyProjectId,
    pub workspace_uris: Vec<WorkspaceRootUri>,
    pub source: AgyGuiSafeSource,
    pub title: String,
    pub preview: String,
    pub step_count: u32,
    pub created_at_iso: String,
    pub updated_at_iso: String,
    pub annotations: AnnotationPbtxtProjection,
    pub nesting_depth: u32,
    pub parent_cascade_id: Option<CascadeId>,
    pub root_cascade_id: Option<CascadeId>,
    pub agent_name: String,
    pub agent_type_name: String,
    pub agent_role: String,
    pub status: i32,
    pub trajectory_id: Option<TrajectoryId>,
    pub trajectory_type: i32,
    pub last_user_input_time_iso: String,
    pub last_user_input_step_index: Option<i32>,
    pub trajectory_metadata_unknown_fields: Vec<UnknownWireField>,
    pub unknown_wire_fields: Vec<UnknownWireField>,
}

impl CascadeSummaryProjection {
    /// Construct a validated summary enforcing Antigravity sidebar visibility invariants.
    ///
    /// # Errors
    /// Returns [`CoreError::InvalidSummary`] if `project_id` is in `FORBIDDEN_AGY_PROJECT_IDS`
    /// or `workspace_uris` is empty.
    #[allow(clippy::too_many_arguments)]
    pub fn try_new(
        cascade_id: CascadeId,
        project_id: AgyProjectId,
        workspace_uris: Vec<WorkspaceRootUri>,
        source: AgyGuiSafeSource,
        title: String,
        preview: String,
        step_count: u32,
        created_at_iso: String,
        updated_at_iso: String,
        mut annotations: AnnotationPbtxtProjection,
        nesting_depth: u32,
        parent_cascade_id: Option<CascadeId>,
        root_cascade_id: Option<CascadeId>,
        agent_name: String,
        agent_type_name: String,
        agent_role: String,
    ) -> Result<Self, CoreError> {
        if FORBIDDEN_AGY_PROJECT_IDS.contains(&project_id.as_str().trim()) {
            return Err(CoreError::InvalidSummary(format!(
                "Forbidden Antigravity project_id {:?}.",
                project_id.as_str()
            )));
        }
        if workspace_uris.is_empty() {
            return Err(CoreError::InvalidSummary(
                "CascadeSummaryProjection requires non-empty workspace_uris.".to_owned(),
            ));
        }
        if !title.is_empty() && annotations.title != title {
            annotations.title.clone_from(&title);
        }
        Ok(Self {
            cascade_id,
            project_id,
            workspace_uris,
            source,
            title,
            preview,
            step_count,
            created_at_iso,
            updated_at_iso,
            annotations,
            nesting_depth,
            parent_cascade_id,
            root_cascade_id,
            agent_name,
            agent_type_name,
            agent_role,
            status: 1,
            trajectory_id: None,
            trajectory_type: 1,
            last_user_input_time_iso: String::new(),
            last_user_input_step_index: None,
            trajectory_metadata_unknown_fields: Vec::new(),
            unknown_wire_fields: Vec::new(),
        })
    }
}

/// Pure view projection for `jetbox_summaries_pb.SummariesState` (`agyhub_summaries_proto.pb`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SummariesStateProjection {
    pub summaries: BTreeMap<CascadeId, CascadeSummaryProjection>,
    pub file_mod_times: BTreeMap<String, i64>,
    pub unknown_wire_fields: Vec<UnknownWireField>,
}

/// Pure view projection for Codex `state_5.sqlite` `threads` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodexThreadSummaryProjection {
    pub thread_id: CodexThreadId,
    pub project_id: CodexProjectId,
    pub cwd: WorkspacePath,
    pub title: String,
    pub preview: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub archived: i32,
}

impl CodexThreadSummaryProjection {
    /// Construct a validated Codex thread summary projection.
    ///
    /// # Errors
    /// Returns [`CoreError::InvalidSummary`] if `preview` is empty or `archived != 0`.
    #[allow(clippy::too_many_arguments)]
    pub fn try_new(
        thread_id: CodexThreadId,
        project_id: CodexProjectId,
        cwd: WorkspacePath,
        title: String,
        preview: String,
        created_at_ms: i64,
        updated_at_ms: i64,
    ) -> Result<Self, CoreError> {
        if preview.trim().is_empty() {
            return Err(CoreError::InvalidSummary(
                "CodexThreadSummaryProjection requires non-empty preview.".to_owned(),
            ));
        }
        Ok(Self {
            thread_id,
            project_id,
            cwd,
            title,
            preview,
            created_at_ms,
            updated_at_ms,
            archived: 0,
        })
    }
}

/// Collapse whitespace to a single line and truncate safely on UTF-8 character boundaries.
#[must_use]
pub fn normalize_single_line_query(query_text: &str, limit: usize) -> String {
    let mut words = Vec::new();
    for raw in query_text.lines() {
        let s = raw.trim();
        if !s.is_empty() && s != "<USER_REQUEST>" && s != "</USER_REQUEST>" {
            words.extend(s.split_whitespace());
        }
    }
    let joined = words.join(" ");
    let truncated: String = joined.chars().take(limit).collect();
    truncated.trim().to_owned()
}

fn collapse_whitespace(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Resolve canonical session title matching `sidebar_visibility_rules.json` order.
#[must_use]
pub fn resolve_canonical_title(
    events: &[CanonicalEvent],
    explicit_title_override: Option<&str>,
    annotation_pbtxt_title: Option<&str>,
    cascade_summary_annotation_title: Option<&str>,
    db_summary_row_title: Option<&str>,
) -> String {
    for candidate in [
        explicit_title_override,
        annotation_pbtxt_title,
        cascade_summary_annotation_title,
        db_summary_row_title,
    ]
    .into_iter()
    .flatten()
    {
        let cleaned = collapse_whitespace(candidate);
        if !cleaned.is_empty() {
            return cleaned;
        }
    }
    for ev in events.iter().rev() {
        if let CanonicalEvent::Checkpoint {
            conversation_title: Some(ct),
            ..
        } = ev
        {
            let cleaned = collapse_whitespace(ct);
            if !cleaned.is_empty() {
                return cleaned;
            }
        }
    }
    for ev in events {
        if let CanonicalEvent::UserInput { query_text, .. } = ev {
            if !query_text.is_empty() {
                let normalized = normalize_single_line_query(query_text, 80);
                if !normalized.is_empty() {
                    return normalized;
                }
            }
        }
    }
    "Untitled Session".to_owned()
}

#[must_use]
pub fn is_host_framing_event(ev: &CanonicalEvent) -> bool {
    match ev {
        CanonicalEvent::SystemMessage { event_type, .. } => {
            event_type == "session_meta" || event_type == "world_state"
        }
        CanonicalEvent::ToolResult { anchor, .. } => anchor
            .host_extensions
            .iter()
            .any(|ext| ext.extension_type == "synthesized_dangling_output"),
        CanonicalEvent::TaskBoundary {
            boundary_kind,
            task_name,
            task_status,
            task_summary,
            ..
        } => {
            task_name.trim().is_empty()
                && matches!(task_summary.trim(), "" | "Turn complete")
                && (*boundary_kind == TaskBoundaryKind::TaskBoundary
                    && (task_status == "task_started" || task_status == "task_complete"))
        }
        _ => false,
    }
}

#[must_use]
pub fn is_terminal_completion_event(ev: &CanonicalEvent) -> bool {
    match ev {
        CanonicalEvent::AssistantText { .. } => true,
        CanonicalEvent::TaskBoundary {
            boundary_kind,
            task_status,
            ..
        } => {
            *boundary_kind == TaskBoundaryKind::NotifyUser
                || matches!(
                    task_status.to_ascii_lowercase().as_str(),
                    "task_complete" | "done" | "completed"
                )
        }
        _ => false,
    }
}

#[must_use]
pub fn is_nonterminal_work_or_abort_event(ev: &CanonicalEvent) -> bool {
    match ev {
        CanonicalEvent::UserInput { .. }
        | CanonicalEvent::Thinking { .. }
        | CanonicalEvent::ToolCall { .. }
        | CanonicalEvent::ToolResult { .. } => true,
        CanonicalEvent::SystemMessage { event_type, .. } => matches!(
            event_type.as_str(),
            "error_message" | "codex_event_msg:turn_aborted" | "codex_event_msg:error"
        ),
        CanonicalEvent::TaskBoundary {
            boundary_kind,
            task_status,
            ..
        } => {
            *boundary_kind != TaskBoundaryKind::NotifyUser
                && matches!(
                    task_status.to_ascii_lowercase().as_str(),
                    "turn_aborted"
                        | "interrupted"
                        | "failed"
                        | "canceled"
                        | "cancelled"
                        | "task_started"
                        | "running"
                        | "in_progress"
                        | "starting"
                )
        }
        _ => false,
    }
}

/// Return `true` when a Codex user message contains solely `agents_md` or `harness` provenance.
#[must_use]
pub fn is_host_injected_user_event(ev: &CanonicalEvent) -> bool {
    let CanonicalEvent::UserInput { anchor, .. } = ev else {
        return false;
    };
    if anchor.origin_host != HostKind::Codex {
        return false;
    }
    for extension in &anchor.host_extensions {
        if extension.extension_type != "codex_message_provenance" {
            continue;
        }
        let raw_str = String::from_utf8_lossy(&extension.raw_payload);
        let data = safe_json_dict(&raw_str);
        if let Some(Value::Array(kinds)) = data.get("content_item_kinds") {
            if kinds
                .iter()
                .any(|k| k.as_str().is_some_and(|s| s.starts_with("user.")))
            {
                return false;
            }
        }
        let Some(Value::Array(metadata)) = data.get("content_item_metadata") else {
            return false;
        };
        if metadata.is_empty() {
            return false;
        }
        return metadata.iter().all(|item| {
            item.as_object()
                .and_then(|obj| obj.get("provenance"))
                .and_then(Value::as_object)
                .and_then(|prov| prov.get("type"))
                .and_then(Value::as_str)
                .is_some_and(|t| t == "agents_md" || t == "harness")
        });
    }
    false
}

/// Return `true` when a Codex tool result originated from host-injected app tools without a call.
#[must_use]
pub fn is_host_injected_tool_result(ev: &CanonicalEvent) -> bool {
    let CanonicalEvent::ToolResult { anchor, .. } = ev else {
        return false;
    };
    if anchor.origin_host != HostKind::Codex {
        return false;
    }
    for extension in &anchor.host_extensions {
        if extension.extension_type != "codex_unpaired_tool_output" {
            continue;
        }
        let raw_str = String::from_utf8_lossy(&extension.raw_payload);
        let data = safe_json_dict(&raw_str);
        let no_call_id = data
            .get("call_id")
            .is_none_or(|v| v.is_null() || v.as_str().is_some_and(str::is_empty));
        let is_agent_ns = data.get("namespace").and_then(Value::as_str) == Some("agent");
        let is_apps_name = data
            .get("name")
            .and_then(Value::as_str)
            .is_some_and(|n| n.starts_with("codex_apps_"));
        return no_call_id && is_agent_ns && is_apps_name;
    }
    false
}

/// Canonical digest dictionary matching Python `events.canonical_event_digest_payload`.
#[must_use]
pub fn canonical_event_digest_payload(ev: &CanonicalEvent) -> BTreeMap<String, Value> {
    let mut base = BTreeMap::new();
    base.insert("kind".to_owned(), Value::String(ev.kind_str().to_owned()));
    base.insert(
        "turn_id".to_owned(),
        Value::String(ev.anchor().turn_id.as_str().to_owned()),
    );

    match ev {
        CanonicalEvent::UserInput {
            query_text,
            scope_items,
            media,
            ..
        } => {
            base.insert("query_text".to_owned(), Value::String(query_text.clone()));
            base.insert(
                "scope_items".to_owned(),
                Value::Array(scope_items.iter().cloned().map(Value::String).collect()),
            );
            if !media.is_empty() {
                let media_vals = media
                    .iter()
                    .map(|m| {
                        let mut mm = serde_json::Map::new();
                        mm.insert("mime_type".to_owned(), Value::String(m.mime_type.clone()));
                        mm.insert("uri".to_owned(), Value::String(m.uri.clone()));
                        Value::Object(mm)
                    })
                    .collect();
                base.insert("media".to_owned(), Value::Array(media_vals));
            }
        }
        CanonicalEvent::Thinking { summary_text, .. } => {
            base.insert(
                "summary_text".to_owned(),
                Value::String(summary_text.clone()),
            );
        }
        CanonicalEvent::AssistantText { text, .. } => {
            base.insert("text".to_owned(), Value::String(text.clone()));
        }
        CanonicalEvent::ToolCall {
            call_id,
            tool_name,
            payload,
            ..
        } => {
            let pkind = payload.kind_str();
            let cname = if pkind != "opaque" {
                pkind
            } else {
                tool_name.as_str()
            };
            let args = serialize_tool_payload_arguments_json(payload);
            base.insert("arguments_json".to_owned(), Value::String(args));
            base.insert(
                "call_id".to_owned(),
                Value::String(call_id.as_str().to_owned()),
            );
            base.insert("payload_kind".to_owned(), Value::String(pkind.to_owned()));
            base.insert("tool_name".to_owned(), Value::String(cname.to_owned()));
        }
        CanonicalEvent::ToolResult {
            call_id,
            tool_name,
            output_text,
            is_error,
            payload,
            ..
        } => {
            let pkind = payload.kind_str();
            let cname = if pkind != "opaque" {
                pkind
            } else {
                tool_name.as_str()
            };
            base.insert(
                "call_id".to_owned(),
                Value::String(call_id.as_str().to_owned()),
            );
            base.insert("is_error".to_owned(), Value::Bool(*is_error));
            base.insert("output_text".to_owned(), Value::String(output_text.clone()));
            base.insert("tool_name".to_owned(), Value::String(cname.to_owned()));
        }
        CanonicalEvent::TaskBoundary {
            boundary_kind,
            task_name,
            task_status,
            task_summary,
            ..
        } => {
            base.insert(
                "boundary_kind".to_owned(),
                Value::String(boundary_kind.as_str().to_owned()),
            );
            base.insert("task_name".to_owned(), Value::String(task_name.clone()));
            base.insert("task_status".to_owned(), Value::String(task_status.clone()));
            base.insert(
                "task_summary".to_owned(),
                Value::String(task_summary.clone()),
            );
        }
        CanonicalEvent::Checkpoint {
            checkpoint_index,
            user_intent,
            session_summary,
            conversation_title,
            ..
        } => {
            base.insert(
                "checkpoint_index".to_owned(),
                Value::from(*checkpoint_index),
            );
            base.insert(
                "conversation_title".to_owned(),
                Value::String(conversation_title.clone().unwrap_or_default()),
            );
            base.insert(
                "session_summary".to_owned(),
                Value::String(session_summary.clone()),
            );
            base.insert("user_intent".to_owned(), Value::String(user_intent.clone()));
        }
        CanonicalEvent::SystemMessage {
            event_type,
            message_text,
            ..
        } => {
            base.insert("event_type".to_owned(), Value::String(event_type.clone()));
            base.insert(
                "message_text".to_owned(),
                Value::String(message_text.clone()),
            );
        }
    }
    base
}
