//! Zero-I/O domain model, canonical ledger, projections, and sync state machine for `codex-agy-sync`.

pub mod error;
pub mod events;
pub mod id;
pub mod ledger;
pub mod payloads;
pub mod receipt;
pub mod state_machine;
pub mod wire_types;

pub use error::CoreError;
pub use events::{
    AnnotationPbtxtProjection, CanonicalEvent, CascadeSummaryProjection,
    CodexThreadSummaryProjection, CompletedTurnWatermark, EventOriginAnchor, HostExtensionEnvelope,
    INTERRUPTED_TOOL_OUTPUT, INTERRUPTED_TURN_TEXT, MediaAttachment, SubagentLineage,
    SummariesStateProjection, TaskBoundaryKind, TokenUsageAccounting, TranscriptRowProjection,
    canonical_event_digest_payload, is_host_framing_event, is_host_injected_tool_result,
    is_host_injected_user_event, is_nonterminal_work_or_abort_event, is_terminal_completion_event,
    normalize_single_line_query, resolve_canonical_title,
};
pub use id::{
    AgyGuiSafeSource, AgyProjectId, CascadeId, CodexLegacyProjectId, CodexProjectId, CodexThreadId,
    ContentDigest, CsrfSecret, FORBIDDEN_AGY_PROJECT_IDS, HostKind, LedgerSeqNo, PACKAGE_VERSION,
    RolloutLineNo, SessionId, StepIndex, ToolCallId, TrajectoryId, TurnId, WorkspacePath,
    WorkspaceRootUri, lexical_abs_path, normalize_tool_file_path, normalize_workspace_spec,
    parse_workspace_identity, percent_decode, raw_spec_to_candidate_path, resolve_strict_false,
    validate_session_id,
};
pub use ledger::{
    TrajectoryLedger, derive_ledger_preview_text, project_ledger_cascade_summary,
    project_ledger_codex_thread_summary, project_ledger_transcript_rows, rescue_interrupted_ledger,
    rescue_interrupted_turn,
};
pub use payloads::{
    CanonicalToolPayload, FileEditOp, ManageSubagentsAction, ManageTaskAction, QuestionItem,
    QuestionOption, SearchWebDocument, SubagentWorkspaceMode, dump_canonical_json, safe_json_dict,
    serialize_tool_payload_arguments_json, sort_json_value, tool_payload_args_map,
};
pub use receipt::{
    PendingReceiptErrorData, RawSyncReceipt, ReceiptEntry, ReceiptState, SyncReceipt,
    ledger_seed_fingerprint, resolve_chain_tip,
};
pub use state_machine::{
    AgyDaemonEndpoint, CodexDaemonEndpoint, CursorForkStateMachine, CursorTransition,
    ForkResolutionPolicy, HostLockSnapshot, NULL_CASCADE_ID_STR, NULL_CODEX_THREAD_ID_STR,
    PairingPolicy, SessionSyncCursor, SyncDirection, SyncSelector, TransportMode, generate_uuid7,
};
pub use wire_types::{
    UnknownWireField, format_timestamp_seconds_nanos, parse_iso_to_seconds_nanos,
};
