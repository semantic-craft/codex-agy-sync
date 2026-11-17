//! Schema-verified protobuf wire tags matching `schema/antigravity_2_30.proto` and `codecs/schema_tags.py`.

use cas_core::{RolloutLineNo, StepIndex};

/// Schema-verified `exa.cortex_pb.CortexStepType` enum values.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u32)]
pub enum CortexStepTypeCode {
    UserInput = 14,
    PlannerResponse = 15,
    ErrorMessage = 17,
    Checkpoint = 23,
    TaskBoundary = 81,
    NotifyUser = 82,
    SystemMessage = 101,
    Generic = 132,
}

impl CortexStepTypeCode {
    #[must_use]
    pub const fn from_u32(val: u32) -> Option<Self> {
        match val {
            14 => Some(Self::UserInput),
            15 => Some(Self::PlannerResponse),
            17 => Some(Self::ErrorMessage),
            23 => Some(Self::Checkpoint),
            81 => Some(Self::TaskBoundary),
            82 => Some(Self::NotifyUser),
            101 => Some(Self::SystemMessage),
            132 => Some(Self::Generic),
            _ => None,
        }
    }
}

/// Schema-verified `gemini_coder.Step` oneof field tags.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u32)]
pub enum GeminiStepWireTag {
    UserInput = 19,
    PlannerResponse = 20,
    ErrorMessage = 24,
    Checkpoint = 30,
    TaskBoundary = 93,
    NotifyUser = 94,
    SystemMessage = 114,
    Generic = 140,
}

impl GeminiStepWireTag {
    #[must_use]
    pub const fn from_u32(val: u32) -> Option<Self> {
        match val {
            19 => Some(Self::UserInput),
            20 => Some(Self::PlannerResponse),
            24 => Some(Self::ErrorMessage),
            30 => Some(Self::Checkpoint),
            93 => Some(Self::TaskBoundary),
            94 => Some(Self::NotifyUser),
            114 => Some(Self::SystemMessage),
            140 => Some(Self::Generic),
            _ => None,
        }
    }
}

pub mod step_field {
    pub const TYPE: u32 = 1;
    pub const STATUS: u32 = 4;
    pub const METADATA: u32 = 5;
    pub const SUBTRAJECTORY: u32 = 6;
    pub const ERROR: u32 = 31;
}

pub mod step_status {
    pub const PENDING: i32 = 1;
    pub const RUNNING: i32 = 2;
    pub const DONE: i32 = 3;
    pub const INVALID: i32 = 4;
    pub const CLEARED: i32 = 5;
    pub const CANCELED: i32 = 6;
    pub const ERROR: i32 = 7;
    pub const GENERATING: i32 = 8;
    pub const WAITING: i32 = 9;
    pub const QUEUED: i32 = 11;
    pub const INTERRUPTED: i32 = 12;

    #[must_use]
    pub const fn is_active(code: i32) -> bool {
        matches!(code, PENDING | RUNNING | GENERATING | WAITING | QUEUED)
    }
}

pub mod step_source {
    pub const MODEL: i64 = 2;
    pub const USER_EXPLICIT: i64 = 4;
    pub const SYSTEM: i64 = 5;
}

pub mod step_meta {
    pub const CREATED_AT: u32 = 1;
    pub const SOURCE: u32 = 3;
    pub const TOOL_CALL: u32 = 4;
    pub const MODEL_USAGE: u32 = 9;
    pub const GENERATOR_MODEL: u32 = 11;
    pub const TOOL_SUMMARY: u32 = 30;
    pub const MAX_KNOWN_TAG: u32 = 39;
}

pub mod model_usage {
    pub const MODEL: u32 = 1;
    pub const INPUT_TOKENS: u32 = 2;
    pub const OUTPUT_TOKENS: u32 = 3;
    pub const CACHE_READ_TOKENS: u32 = 5;
}

pub mod chat_tool_call {
    pub const ID: u32 = 1;
    pub const NAME: u32 = 2;
    pub const ARGUMENTS_JSON: u32 = 3;
    pub const THOUGHT_SIGNATURE: u32 = 6;
    pub const THINKING_SIGNATURE: u32 = 7;
    pub const ORIGINAL_ARGUMENTS_JSON: u32 = 8;
    pub const ORIGINAL_NAME: u32 = 9;
}

pub mod user_input {
    pub const QUERY: u32 = 1;
    pub const USER_RESPONSE: u32 = 2;
    pub const ITEMS: u32 = 3;
    pub const MEDIA: u32 = 9;
    pub const CLIENT_MESSAGE_ID: u32 = 19;
}

pub mod text_or_scope {
    pub const TEXT: u32 = 1;
    pub const ITEM: u32 = 2;
}

pub mod context_scope {
    pub const FILE: u32 = 1;
}

pub mod path_scope {
    pub const ABSOLUTE_PATH: u32 = 1;
    pub const ABSOLUTE_URI: u32 = 5;
}

pub mod media_tag {
    pub const MIME_TYPE: u32 = 1;
    pub const URI: u32 = 5;
}

pub mod planner_response {
    pub const RESPONSE: u32 = 1;
    pub const THINKING: u32 = 3;
    pub const SIGNATURE: u32 = 4;
    pub const MESSAGE_ID: u32 = 6;
    pub const TOOL_CALLS: u32 = 7;
    pub const MODIFIED_RESPONSE: u32 = 8;
    pub const THINKING_DURATION: u32 = 11;
    pub const STOP_REASON: u32 = 12;
    pub const THINKING_SIGNATURE: u32 = 14;
    pub const RAW_THINKING: u32 = 16;
    pub const MAX_KNOWN_TAG: u32 = 20;
}

pub mod generic_step {
    pub const ARGS: u32 = 1;
    pub const RESULT: u32 = 2;
}

pub mod generic_result {
    pub const RESULT: u32 = 1;
    pub const PAYLOAD: u32 = 6;
    pub const FULL_OUTPUT_URI: u32 = 7;
}

pub mod error_message_step {
    pub const ERROR: u32 = 3;
    pub const SHOULD_SHOW_MODEL: u32 = 4;
    pub const SHOULD_SHOW_USER: u32 = 5;
}

pub mod error_details {
    pub const USER_ERROR_MESSAGE: u32 = 1;
    pub const SHORT_ERROR: u32 = 2;
    pub const FULL_ERROR: u32 = 3;
}

pub mod checkpoint_tag {
    pub const CHECKPOINT_INDEX: u32 = 1;
    pub const USER_INTENT: u32 = 4;
    pub const SESSION_SUMMARY: u32 = 5;
    pub const CODE_CHANGE_SUMMARY: u32 = 6;
    pub const CONVERSATION_TITLE: u32 = 10;
    pub const MAX_KNOWN_TAG: u32 = 31;
}

pub mod task_boundary_tag {
    pub const TASK_NAME: u32 = 1;
    pub const TASK_STATUS: u32 = 2;
    pub const TASK_SUMMARY: u32 = 3;
    pub const MODE: u32 = 5;
    pub const MAX_KNOWN_TAG: u32 = 7;
}

pub mod notify_user_tag {
    pub const REVIEW_ABSOLUTE_URIS: u32 = 1;
    pub const NOTIFICATION_CONTENT: u32 = 2;
    pub const IS_BLOCKING: u32 = 3;
    pub const MAX_KNOWN_TAG: u32 = 8;
}

pub mod system_message_tag {
    pub const MESSAGE: u32 = 1;
    pub const EVENT_TYPE: u32 = 3;
    pub const MAX_KNOWN_TAG: u32 = 6;
}

pub mod any_envelope {
    pub const TYPE_URL: u32 = 1;
    pub const VALUE: u32 = 2;
}

pub mod map_entry {
    pub const KEY: u32 = 1;
    pub const VALUE: u32 = 2;
}

pub mod duration_proto {
    pub const SECONDS: u32 = 1;
    pub const NANOS: u32 = 2;
}

pub mod workspace_info_tag {
    pub const WORKSPACE_FOLDER_ABSOLUTE_URI: u32 = 1;
    pub const GIT_ROOT_ABSOLUTE_URI: u32 = 2;
}

pub mod annotations_tag {
    pub const TITLE: u32 = 1;
    pub const LAST_USER_VIEW_TIME: u32 = 7;
}

pub mod trajectory_meta_tag {
    pub const WORKSPACES: u32 = 1;
    pub const CREATED_AT: u32 = 2;
    pub const PARENT_CONVERSATION_ID: u32 = 5;
    pub const ROOT_CONVERSATION_ID: u32 = 6;
    pub const WORKSPACE_URIS: u32 = 7;
    pub const SUBAGENT_SPEC: u32 = 8;
    pub const NESTING_DEPTH: u32 = 17;
    pub const PROJECT_ID: u32 = 18;
    pub const ALL: &[u32] = &[1, 2, 5, 6, 7, 8, 17, 18];
}

pub mod subagent_meta_spec_tag {
    pub const TYPE_NAME: u32 = 1;
    pub const ROLE: u32 = 2;
}

pub mod summary_field_tag {
    pub const SUMMARY: u32 = 1;
    pub const STEP_COUNT: u32 = 2;
    pub const LAST_MODIFIED_TIME: u32 = 3;
    pub const TRAJECTORY_ID: u32 = 4;
    pub const STATUS: u32 = 5;
    pub const CREATED_TIME: u32 = 7;
    pub const WORKSPACES: u32 = 9;
    pub const LAST_USER_INPUT_TIME: u32 = 10;
    pub const ANNOTATIONS: u32 = 15;
    pub const LAST_USER_INPUT_STEP_INDEX: u32 = 16;
    pub const TRAJECTORY_METADATA: u32 = 17;
    pub const TRAJECTORY_SOURCE: u32 = 20;
    pub const TRAJECTORY_TYPE: u32 = 22;
    pub const ALL: &[u32] = &[1, 2, 3, 4, 5, 7, 9, 10, 15, 16, 17, 20, 22];
}

pub mod summaries_state_tag {
    pub const SUMMARIES: u32 = 1;
    pub const FILE_MOD_TIMES: u32 = 2;
    pub const ALL: &[u32] = &[1, 2];
}

pub mod tool_tags {
    pub mod read_file {
        pub const FILE_NUM_LINES: u32 = 1;
        pub const FILE_NUM_BYTES: u32 = 2;
        pub const PATH: u32 = 3;
        pub const START_LINE: u32 = 4;
        pub const END_LINE: u32 = 5;
    }
    pub mod file_edit {
        pub const KIND: u32 = 1;
        pub const PATH: u32 = 2;
        pub const DIFF: u32 = 3;
        pub const DIFF_STATS: u32 = 4;
        pub const ARTIFACT_METADATA: u32 = 5;
        pub const IS_ARTIFACT_FILE: u32 = 6;

        pub const KIND_UNSPECIFIED: u64 = 0;
        pub const KIND_CREATE: u64 = 1;
        pub const KIND_OVERWRITE: u64 = 2;
        pub const KIND_EDIT: u64 = 3;
    }
    pub mod diff_block {
        pub const UNIFIED_DIFF: u32 = 3;
    }
    pub mod unified_diff {
        pub const LINES: u32 = 3;
    }
    pub mod unified_diff_line {
        pub const TEXT: u32 = 1;
        pub const TYPE: u32 = 2;

        pub const TYPE_UNSPECIFIED: i64 = 0;
        pub const TYPE_INSERT: i64 = 1;
        pub const TYPE_DELETE: i64 = 2;
        pub const TYPE_UNCHANGED: i64 = 3;
    }
    pub mod diff_stats {
        pub const ADDITIONS: u32 = 1;
        pub const DELETIONS: u32 = 2;
    }
    pub mod artifact_meta {
        pub const SUMMARY: u32 = 2;
    }
    pub mod run_command {
        pub const EXIT_CODE: u32 = 1;
        pub const TIMED_OUT: u32 = 2;
        pub const SANDBOX_DETAILS: u32 = 3;
        pub const SHELL_NAME: u32 = 5;
        pub const COMMAND_LINE: u32 = 6;
        pub const CWD: u32 = 7;
        pub const EXIT_CODE_UNKNOWN: u32 = 8;
    }
    pub mod sandbox_details {
        pub const RAN_IN_SANDBOX: u32 = 1;
        pub const SANDBOX_ERRORS: u32 = 2;
    }
    pub mod search_web {
        pub const QUERY: u32 = 1;
        pub const DOMAIN: u32 = 2;
        pub const WEB_DOCUMENTS: u32 = 3;
        pub const SUMMARY: u32 = 4;
    }
    pub mod search_doc {
        pub const SNIPPET: u32 = 2;
        pub const URL: u32 = 3;
        pub const TITLE: u32 = 4;
        pub const FALLBACK_TEXT: u32 = 7;
    }
    pub mod invoke_subagent {
        pub const SUBAGENTS: u32 = 1;
        pub const RESULTS: u32 = 2;
        pub const TASK_MODE: u32 = 3;
    }
    pub mod subagent_spec {
        pub const TYPE_NAME: u32 = 1;
        pub const ROLE: u32 = 2;
        pub const PROMPT: u32 = 3;
        pub const INHERIT_WORKSPACE: u32 = 4;
        pub const BRANCH_WORKSPACE: u32 = 5;
        pub const SHARE_WORKSPACE: u32 = 10;
    }
    pub mod subagent_result {
        pub const CONVERSATION_ID: u32 = 1;
    }
    pub mod define_subagent {
        pub const NAME: u32 = 1;
        pub const TOOL_NAMES: u32 = 2;
        pub const AGENT_PATH: u32 = 3;
    }
    pub mod manage_subagents {
        pub const ACTION: u32 = 1;
        pub const CONVERSATION_IDS: u32 = 2;
        pub const SUBAGENTS: u32 = 3;
    }
    pub mod subagent_state {
        pub const CONVERSATION_ID: u32 = 1;
    }
    pub mod manage_task {
        pub const ACTION: u32 = 1;
        pub const TASK_IDS: u32 = 2;
        pub const INPUT: u32 = 3;
    }
    pub mod send_message {
        pub const RECIPIENT: u32 = 1;
        pub const RECIPIENT_NAME: u32 = 2;
        pub const MESSAGE: u32 = 3;
    }
    pub mod schedule {
        pub const MODE: u32 = 1;
        pub const ITERATIONS: u32 = 3;
    }
    pub mod ask_question {
        pub const QUESTIONS: u32 = 1;
    }
    pub mod ask_question_entry {
        pub const QUESTION: u32 = 1;
        pub const OPTIONS: u32 = 2;
        pub const IS_MULTI_SELECT: u32 = 3;
        pub const USER_ANSWERS: u32 = 4;
        pub const ALT_ANSWERS: u32 = 5;
    }
    pub mod question_option {
        pub const ID: u32 = 1;
        pub const TEXT: u32 = 2;
    }
    pub mod list_resources {
        pub const SERVER_NAME: u32 = 1;
        pub const RESOURCES: u32 = 2;
    }
    pub mod mcp_resource {
        pub const URI: u32 = 1;
    }
    pub mod read_resource {
        pub const SERVER_NAME: u32 = 1;
        pub const URI: u32 = 2;
        pub const CONTENTS: u32 = 3;
    }
    pub mod mcp_resource_content {
        pub const URI: u32 = 1;
        pub const TEXT_CONTENT: u32 = 2;
    }
    pub mod mcp_text_content {
        pub const TEXT: u32 = 1;
    }
    pub mod generate_image {
        pub const PROMPT: u32 = 1;
        pub const IMAGE_PATHS: u32 = 2;
        pub const ASPECT_RATIO: u32 = 3;
        pub const IMAGE_URI: u32 = 4;
        pub const MODEL_NAME: u32 = 6;
    }
    pub mod moma_search {
        pub const QUERY: u32 = 1;
        pub const RESULTS: u32 = 2;
    }
    pub mod finish {
        pub const ARGUMENTS_JSON: u32 = 1;
        pub const HAS_RESULT_SCHEMA: u32 = 2;
    }
}

/// Declarative specification for one `gemini_coder.Step` active variant.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StepWireSpec {
    pub step_type: CortexStepTypeCode,
    pub step_oneof_tag: GeminiStepWireTag,
    pub oneof_field_name: &'static str,
    pub proto_message_full_name: &'static str,
    pub transcript_type_label: &'static str,
}

pub const STEP_WIRE_TABLE: &[StepWireSpec] = &[
    StepWireSpec {
        step_type: CortexStepTypeCode::UserInput,
        step_oneof_tag: GeminiStepWireTag::UserInput,
        oneof_field_name: "user_input",
        proto_message_full_name: "exa.cortex_pb.CortexStepUserInput",
        transcript_type_label: "USER_INPUT",
    },
    StepWireSpec {
        step_type: CortexStepTypeCode::PlannerResponse,
        step_oneof_tag: GeminiStepWireTag::PlannerResponse,
        oneof_field_name: "planner_response",
        proto_message_full_name: "exa.cortex_pb.CortexStepPlannerResponse",
        transcript_type_label: "PLANNER_RESPONSE",
    },
    StepWireSpec {
        step_type: CortexStepTypeCode::Checkpoint,
        step_oneof_tag: GeminiStepWireTag::Checkpoint,
        oneof_field_name: "checkpoint",
        proto_message_full_name: "exa.cortex_pb.CortexStepCheckpoint",
        transcript_type_label: "CHECKPOINT",
    },
    StepWireSpec {
        step_type: CortexStepTypeCode::TaskBoundary,
        step_oneof_tag: GeminiStepWireTag::TaskBoundary,
        oneof_field_name: "task_boundary",
        proto_message_full_name: "exa.cortex_pb.CortexStepTaskBoundary",
        transcript_type_label: "TASK_BOUNDARY",
    },
    StepWireSpec {
        step_type: CortexStepTypeCode::NotifyUser,
        step_oneof_tag: GeminiStepWireTag::NotifyUser,
        oneof_field_name: "notify_user",
        proto_message_full_name: "exa.cortex_pb.CortexStepNotifyUser",
        transcript_type_label: "NOTIFY_USER",
    },
    StepWireSpec {
        step_type: CortexStepTypeCode::SystemMessage,
        step_oneof_tag: GeminiStepWireTag::SystemMessage,
        oneof_field_name: "system_message",
        proto_message_full_name: "exa.cortex_pb.CortexStepSystemMessage",
        transcript_type_label: "SYSTEM_MESSAGE",
    },
    StepWireSpec {
        step_type: CortexStepTypeCode::Generic,
        step_oneof_tag: GeminiStepWireTag::Generic,
        oneof_field_name: "generic",
        proto_message_full_name: "exa.cortex_pb.CortexStepGeneric",
        transcript_type_label: "GENERIC",
    },
];

#[must_use]
pub fn find_step_spec_by_type(step_type: i32) -> Option<&'static StepWireSpec> {
    let code = CortexStepTypeCode::from_u32(u32::try_from(step_type).ok()?)?;
    STEP_WIRE_TABLE.iter().find(|s| s.step_type == code)
}

#[must_use]
pub fn find_step_spec_by_tag(tag: u32) -> Option<&'static StepWireSpec> {
    let wire_tag = GeminiStepWireTag::from_u32(tag)?;
    STEP_WIRE_TABLE
        .iter()
        .find(|s| s.step_oneof_tag == wire_tag)
}

/// Raw row read from or written to `conversations/<cascade_id>.db` `steps` table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgyRawStepRow {
    pub idx: StepIndex,
    pub step_type: i32,
    pub status: i32,
    pub has_subtrajectory: bool,
    pub metadata_blob: Vec<u8>,
    pub step_payload_blob: Vec<u8>,
    pub step_format: i32,
}

/// Single JSONL line read from or written to `~/.codex/sessions/**/rollout-*.jsonl`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodexRawRolloutLine {
    pub line_no: RolloutLineNo,
    pub raw_json: String,
}
