"""Canonical Indexed Event-Ledger IR and pure companion view projections.

Invariants encoded in this module:
1. Branded semantic primitives prevent mixing Antigravity and Codex identifiers,
   indices, and digests across host boundaries (per `principle-type-system-discipline`).
2. `TrajectoryLedger` is an immutable, append-only indexed event sequence whose
   secondary lookup tables (`by_step_index`, `by_call_id`, `by_turn_id`,
   `completed_turn_watermarks`) resolve the 1-to-N fanout between a single
   Antigravity `PLANNER_RESPONSE` (`step_type = 15`) and N `GENERIC` tool execution
   steps (`step_type = 132`) in O(1) time without losing exact `StepIndex` or
   Codex `RolloutLineNo` ordering (per `principle-model-the-domain`).
3. Every `EventOriginAnchor` preserves unknown protobuf wire fields (`UnknownWireField`)
   and host-specific extension envelopes (`HostExtensionEnvelope`, such as Codex
   `encrypted_content` or `world_state`) for lossless round-trip fidelity (grafted from
   Candidate 1).
4. Only completed turns appear in `completed_turn_watermarks`; in-flight turns
   never advance a sync watermark.
5. Companion files (`transcript.jsonl`, `annotations/<id>.pbtxt` with 3-tier title
   precedence, `CascadeTrajectorySummary`, `SummariesState` for `agyhub_summaries_proto.pb`,
   and Codex `threads` row) are pure view projections derived from `TrajectoryLedger`
   rather than independently mutated stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Mapping, NewType, Sequence, Tuple, TypeAlias

# ---------------------------------------------------------------------------
# Branded Semantic Primitives (per principle-type-system-discipline)
# ---------------------------------------------------------------------------

WorkspaceRootUri = NewType("WorkspaceRootUri", str)
"""Normalized `file:///abs/path` URI with symlinks resolved and no trailing slash."""

WorkspacePath = NewType("WorkspacePath", Path)
"""Canonicalized absolute `Path` (`Path.resolve()`) of a local workspace directory."""

AgyProjectId = NewType("AgyProjectId", str)
"""UUID string identifying an Antigravity 2.30+ project in `~/.gemini/config/projects/<id>.json`."""

CodexProjectId = NewType("CodexProjectId", str)
"""Identifier of a Codex 0.166+ project in `state_5.sqlite` (`projects.id`)."""

CodexLegacyProjectId = NewType("CodexLegacyProjectId", str)
"""Legacy project UUID in `.codex-global-state.json` mapped to `CodexProjectId`."""

CascadeId = NewType("CascadeId", str)
"""Antigravity conversation UUID (`conversations/<cascade_id>.db`)."""

TrajectoryId = NewType("TrajectoryId", str)
"""Antigravity `trajectory_meta.trajectory_id` UUID associated with a `CascadeId`."""

CodexThreadId = NewType("CodexThreadId", str)
"""Codex thread UUID (`threads.id` and `rollout-*-<thread_id>.jsonl`)."""

StepIndex = NewType("StepIndex", int)
"""0-based monotonic row index (`steps.idx`) in Antigravity `conversations/<id>.db`."""

RolloutLineNo = NewType("RolloutLineNo", int)
"""1-based monotonic JSONL line number in Codex `rollout-*.jsonl`."""

TurnId = NewType("TurnId", str)
"""Stable identifier of one logical user-initiated turn across planner and tool steps."""

ToolCallId = NewType("ToolCallId", str)
"""Correlation identifier joining a `ToolCallEvent` (`step_type=15`) to its `ToolResultEvent` (`step_type=132`)."""

LedgerSeqNo = NewType("LedgerSeqNo", int)
"""0-based position of a `CanonicalEvent` inside `TrajectoryLedger.events`."""

ContentDigest = NewType("ContentDigest", str)
"""SHA-256 hex digest over canonicalized event content at a turn watermark."""


def parse_workspace_identity(raw_path_or_uri: str | Path) -> tuple[WorkspacePath, WorkspaceRootUri]:
    """Parse and canonicalize an untrusted path or `file://` URI at the boundary.

    Invariants:
    - Expands `~` and environment variables via runtime discovery without hardcoded usernames.
    - Resolves symlinks and strips trailing slashes so Antigravity `folderUri` and
      Codex `project_roots.path` / `threads.cwd` normalize to the exact same key.
    - Rejects relative paths that cannot be resolved or empty strings.
    """
    # TODO:
    # 1. If str starts with "file://", unquote URI path and convert to Path; else Path(raw_path_or_uri).expanduser().
    # 2. Resolve via path.resolve(strict=False) and reject root/empty anomalies.
    # 3. Build WorkspacePath(resolved) and WorkspaceRootUri(resolved.as_uri().rstrip("/")).
    raise NotImplementedError("parse_workspace_identity is a boundary parser stub")


# ---------------------------------------------------------------------------
# Sidebar-Safe Source, Lossless Wire Envelopes & Provenance Types
# ---------------------------------------------------------------------------


class HostKind(str, Enum):
    """Supported agent host runtimes."""

    ANTIGRAVITY = "antigravity"
    CODEX = "codex"


class AgyGuiSafeSource(Enum):
    """Antigravity trajectory sources guaranteed to pass the Desktop sidebar filter.

    Invariant: `CORTEX_TRAJECTORY_SOURCE_CLI` (enum `17`) is intentionally omitted
    so illegal CLI-only provenance cannot be constructed on the write path
    (per `principle-type-system-discipline`).
    """

    UNSPECIFIED = (0, "")
    CASCADE_CLIENT = (1, "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT")
    JETBOX = (18, "CORTEX_TRAJECTORY_SOURCE_JETBOX")
    AGENT_API = (19, "CORTEX_TRAJECTORY_SOURCE_AGENT_API")
    CIDER = (20, "CORTEX_TRAJECTORY_SOURCE_CIDER")

    @property
    def proto_enum_value(self) -> int:
        """Numeric value for `CortexTrajectoryMetadata.source`."""
        # TODO: return self.value[0]
        raise NotImplementedError("AgyGuiSafeSource.proto_enum_value stub")

    @property
    def summary_column_name(self) -> str:
        """String value stored in `conversation_summaries.source`."""
        # TODO: return self.value[1]
        raise NotImplementedError("AgyGuiSafeSource.summary_column_name stub")


@dataclass(frozen=True)
class UnknownWireField:
    """Opaque unknown protobuf field preserved during decoding (grafted from Candidate 1).

    Ensures forward compatibility when Antigravity adds new protobuf fields in minor
    releases (such as `exa.project_pb.Project.coordinator` tag 15).
    """

    field_tag: int
    wire_type: int
    raw_bytes: bytes


@dataclass(frozen=True)
class HostExtensionEnvelope:
    """Opaque host-specific payload preserved for round-trip fidelity (grafted from Candidate 1).

    Used to preserve Codex `encrypted_content`, `world_state`, or Antigravity
    `gen_metadata` / `executor_metadata` blobs across round-trip conversions.
    """

    host: HostKind
    extension_type: str
    raw_payload: bytes
    metadata: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EventOriginAnchor:
    """Exact source coordinates and opaque extensions preserved on every `CanonicalEvent`.

    Invariants:
    - An event originating from Antigravity always carries `agy_step_index`.
    - When a single `PLANNER_RESPONSE` (`step_type = 15`) decomposes into a
      `ThinkingEvent`, `AssistantTextEvent`, and `N` `ToolCallEvent` items, all of
      those events share the same `agy_step_index` while each corresponding
      `ToolResultEvent` carries its own `GENERIC` (`step_type = 132`) `agy_step_index`.
    - An event originating from Codex carries `codex_line_no` from `rollout-*.jsonl`.
    """

    origin_host: HostKind
    turn_id: TurnId
    timestamp_iso: str
    agy_step_index: StepIndex | None = None
    codex_line_no: RolloutLineNo | None = None
    client_message_id: str | None = None
    unknown_wire_fields: Tuple[UnknownWireField, ...] = ()
    host_extensions: Tuple[HostExtensionEnvelope, ...] = ()


@dataclass(frozen=True)
class TokenUsageAccounting:
    """Per-step token usage preserved for `transcript.jsonl` and summary rollups."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    thinking_duration_ms: int | None = None


@dataclass(frozen=True)
class MediaAttachment:
    """Media reference attached to user input or tool outputs."""

    mime_type: str
    uri: str
    byte_size: int | None = None
    caption: str | None = None


# ---------------------------------------------------------------------------
# Typed Canonical Tool Payloads (10 `exa.cortex.core.tools.*` <-> Codex tools)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadFilePayload:
    """Domain payload for `exa.cortex.core.tools.ReadFile` <-> Codex `view_file` / `cat`."""

    kind: Literal["read_file"]
    path: str
    start_line: int | None = None
    end_line: int | None = None
    byte_offset: int | None = None
    file_num_lines: int | None = None
    file_num_bytes: int | None = None
    content_excerpt: str | None = None


@dataclass(frozen=True)
class FileEditPayload:
    """Domain payload for `exa.cortex.core.tools.FileEdit` <-> Codex `apply_patch`."""

    kind: Literal["file_edit"]
    path: str
    edit_op: Literal["CREATE", "MODIFY", "DELETE"]
    unified_diff: str
    diff_stats: Tuple[int, int] = (0, 0)  # (lines_added, lines_removed), grafted from Candidate 1
    is_artifact: bool = False
    artifact_summary: str | None = None


@dataclass(frozen=True)
class RunCommandPayload:
    """Domain payload for `exa.cortex.core.tools.RunCommand` <-> Codex `exec_command`."""

    kind: Literal["run_command"]
    command_line: str
    cwd: str
    exit_code: int | None = None
    timed_out: bool = False
    is_daemon: bool = False
    shell_name: str | None = None
    ran_in_sandbox: bool = False
    sandbox_policy: str | None = None


@dataclass(frozen=True)
class SearchWebDocument:
    """Cited document inside a `SearchWebPayload`."""

    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class SearchWebPayload:
    """Domain payload for `exa.cortex.core.tools.SearchWeb` <-> Codex `web_search`."""

    kind: Literal["search_web"]
    query: str
    domain_filter: str | None = None
    documents: Tuple[SearchWebDocument, ...] = ()
    summary: str | None = None


@dataclass(frozen=True)
class InvokeSubagentPayload:
    """Domain payload for `exa.cortex.core.tools.InvokeSubagent` <-> Codex `spawn_agent`."""

    kind: Literal["invoke_subagent"]
    subagent_type: str
    role: str
    prompt: str
    spawned_conversation_id: str | None = None
    workspace_mode: Literal["inherit", "branch", "share"] = "inherit"
    task_mode: bool = False


@dataclass(frozen=True)
class DefineSubagentPayload:
    """Domain payload for `exa.cortex.core.tools.DefineSubagent` <-> Codex custom agent definition."""

    kind: Literal["define_subagent"]
    name: str
    tool_names: Tuple[str, ...] = ()
    agent_path: str | None = None


@dataclass(frozen=True)
class ManageSubagentsPayload:
    """Domain payload for `exa.cortex.core.tools.ManageSubagents` <-> Codex agent control."""

    kind: Literal["manage_subagents"]
    action: Literal["list", "kill", "kill_all"]
    target_conversation_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ManageTaskPayload:
    """Domain payload for `exa.cortex.core.tools.ManageTask` <-> Codex `wait` / `send_input`."""

    kind: Literal["manage_task"]
    action: Literal["list", "status", "send_input", "kill"]
    task_id: str | None = None
    input_text: str | None = None


@dataclass(frozen=True)
class SendMessagePayload:
    """Domain payload for `exa.cortex.core.tools.SendMessage` <-> Codex `send_message`."""

    kind: Literal["send_message"]
    recipient_id: str
    message_body: str
    recipient_name: str | None = None


@dataclass(frozen=True)
class SchedulePayload:
    """Domain payload for `exa.cortex.core.tools.Schedule` <-> Codex timer/cron tool."""

    kind: Literal["schedule"]
    prompt: str
    duration_seconds: int | None = None
    cron_expression: str | None = None
    timer_condition: str = "never"
    is_daemon: bool = False


@dataclass(frozen=True)
class QuestionOption:
    """Structured option in an interactive user question (grafted from Candidate 1)."""

    label: str
    description: str = ""


@dataclass(frozen=True)
class QuestionItem:
    """Single multiple-choice question inside `AskQuestionPayload` (grafted from Candidate 1)."""

    question: str
    options: Tuple[QuestionOption, ...] = ()
    is_multi_select: bool = False
    user_answers: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AskQuestionPayload:
    """Domain payload for `exa.cortex.core.tools.AskQuestion` <-> Codex user prompt tool."""

    kind: Literal["ask_question"]
    questions: Tuple[QuestionItem, ...] = ()


@dataclass(frozen=True)
class ListResourcesPayload:
    """Domain payload for `exa.cortex.core.tools.ListResources` <-> Codex MCP `list_mcp_resources`."""

    kind: Literal["list_resources"]
    server_name: str
    resource_uris: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadResourcePayload:
    """Domain payload for `exa.cortex.core.tools.ReadResource` <-> Codex MCP `read_mcp_resource`."""

    kind: Literal["read_resource"]
    server_name: str
    uri: str
    contents_text: Tuple[str, ...] = ()


@dataclass(frozen=True)
class GenerateImagePayload:
    """Domain payload for `exa.cortex.core.tools.GenerateImage` <-> Codex image generation tool."""

    kind: Literal["generate_image"]
    prompt: str
    image_paths: Tuple[str, ...] = ()
    aspect_ratio: str = "1:1"
    image_uri: str | None = None
    model_name: str | None = None


@dataclass(frozen=True)
class MomaSearchPayload:
    """Domain payload for `exa.cortex.core.tools.MomaSearch` <-> Codex internal search tool."""

    kind: Literal["moma_search"]
    query: str
    result_count: int = 0


@dataclass(frozen=True)
class FinishPayload:
    """Domain payload for `exa.cortex.core.tools.Finish` <-> Codex structured finish tool."""

    kind: Literal["finish"]
    arguments_json: str = "{}"
    has_result_schema: bool = False


@dataclass(frozen=True)
class OpaqueToolPayload:
    """Lossless fallback for unrecognized `google.protobuf.Any` or custom MCP tools."""

    kind: Literal["opaque"]
    tool_name: str
    raw_arguments_json: str
    type_url: str | None = None
    raw_any_bytes: bytes | None = None


CanonicalToolPayload: TypeAlias = (
    ReadFilePayload
    | FileEditPayload
    | RunCommandPayload
    | SearchWebPayload
    | InvokeSubagentPayload
    | DefineSubagentPayload
    | ManageSubagentsPayload
    | ManageTaskPayload
    | SendMessagePayload
    | SchedulePayload
    | AskQuestionPayload
    | ListResourcesPayload
    | ReadResourcePayload
    | GenerateImagePayload
    | MomaSearchPayload
    | FinishPayload
    | OpaqueToolPayload
)


# ---------------------------------------------------------------------------
# Discriminated Canonical Event Union (`CanonicalEvent`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserInputEvent:
    """User prompt event (`step_type=14` / `Step.user_input` tag `19` <-> Codex `message:user`)."""

    kind: Literal["user_input"]
    anchor: EventOriginAnchor
    query_text: str
    scope_items: Tuple[str, ...] = ()
    media: Tuple[MediaAttachment, ...] = ()


@dataclass(frozen=True)
class ThinkingEvent:
    """Model reasoning summary (`CortexStepPlannerResponse.thinking` <-> Codex `reasoning`/`magpie_reasoning`)."""

    kind: Literal["thinking"]
    anchor: EventOriginAnchor
    summary_text: str
    raw_thinking: str | None = None
    thinking_signature: bytes | None = None
    token_usage: TokenUsageAccounting = TokenUsageAccounting()


@dataclass(frozen=True)
class AssistantTextEvent:
    """Assistant prose response (`CortexStepPlannerResponse.response` <-> Codex `message:assistant`)."""

    kind: Literal["assistant_text"]
    anchor: EventOriginAnchor
    text: str
    message_id: str | None = None
    stop_reason: str | None = None
    generator_model: str | None = None
    token_usage: TokenUsageAccounting = TokenUsageAccounting()


@dataclass(frozen=True)
class ToolCallEvent:
    """Tool invocation declared inside `CortexStepPlannerResponse.tool_calls` or Codex `function_call`."""

    kind: Literal["tool_call"]
    anchor: EventOriginAnchor
    call_id: ToolCallId
    tool_name: str
    tool_summary: str
    payload: CanonicalToolPayload


@dataclass(frozen=True)
class ToolResultEvent:
    """Tool execution output (`step_type=132` `Step.generic` tag `140` <-> Codex `function_call_output`)."""

    kind: Literal["tool_result"]
    anchor: EventOriginAnchor
    call_id: ToolCallId
    tool_name: str
    output_text: str
    is_error: bool
    payload: CanonicalToolPayload
    full_output_uri: str | None = None


@dataclass(frozen=True)
class TaskBoundaryEvent:
    """Task boundary or user notification (`step_type=81` tag `93` / `step_type=82` tag `94` <-> Codex `event_msg`)."""

    kind: Literal["task_boundary"]
    anchor: EventOriginAnchor
    boundary_kind: Literal["task_boundary", "notify_user"]
    task_name: str
    task_status: str
    task_summary: str
    mode: str | None = None
    blocked_on_user: bool = False
    review_uris: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckpointEvent:
    """Session checkpoint (`step_type=23` `Step.checkpoint` tag `30` <-> Codex `turn_context`/`world_state`)."""

    kind: Literal["checkpoint"]
    anchor: EventOriginAnchor
    checkpoint_index: int
    user_intent: str
    session_summary: str
    code_change_summary: str
    conversation_title: str | None = None


@dataclass(frozen=True)
class SystemEvent:
    """System message (`step_type=101` `Step.system_message` tag `114` <-> Codex `session_meta`)."""

    kind: Literal["system_message"]
    anchor: EventOriginAnchor
    event_type: str
    message_text: str


CanonicalEvent: TypeAlias = (
    UserInputEvent
    | ThinkingEvent
    | AssistantTextEvent
    | ToolCallEvent
    | ToolResultEvent
    | TaskBoundaryEvent
    | CheckpointEvent
    | SystemEvent
)


# ---------------------------------------------------------------------------
# Completed-Turn Watermark & Companion View Projections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletedTurnWatermark:
    """Immutable boundary record for one completed turn inside `TrajectoryLedger`.

    Invariant: Only turns whose tool calls have all resolved and whose terminal
    assistant response or `task_complete` marker has arrived produce a watermark.
    """

    turn_id: TurnId
    last_ledger_seq: LedgerSeqNo
    last_agy_step_index: StepIndex
    last_codex_line_no: RolloutLineNo
    boundary_digest: ContentDigest


@dataclass(frozen=True)
class TranscriptRowProjection:
    """Pure view row for `brain/<cascade_id>/.system_generated/logs/transcript.jsonl`."""

    step_index: StepIndex
    source: str
    step_type_name: str
    status_name: str
    created_at: str
    content: str
    thinking: str | None
    tool_calls_json: Tuple[Mapping[str, object], ...]
    media_uris: Tuple[Mapping[str, str], ...]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    truncated_fields: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AnnotationPbtxtProjection:
    """Pure view projection for `~/.gemini/antigravity/annotations/<cascade_id>.pbtxt`.

    Invariant: Title matches `CascadeSummaryProjection.title` so all three
    Antigravity title precedence slots agree.
    """

    cascade_id: CascadeId
    title: str
    last_user_view_time_iso: str

    def render_pbtxt(self) -> str:
        """Serialize `exa.jetski_cortex_pb.ConversationAnnotations` textproto."""
        # TODO:
        # Format deterministic textproto with field `title: "<escaped>"` and
        # `last_user_view_time` timestamp.
        raise NotImplementedError("AnnotationPbtxtProjection.render_pbtxt stub")


@dataclass(frozen=True)
class CascadeSummaryProjection:
    """Pure view projection for `conversation_summaries.db` and `agyhub_summaries_proto.pb`.

    Invariants:
    - `project_id` is a valid non-CLI `AgyProjectId`.
    - `source` is an `AgyGuiSafeSource` (never enum `17`).
    - `nesting_depth == 0` and `workspace_uris` is non-empty.
    - `title` is identical across `annotations/<id>.pbtxt`, `CascadeTrajectorySummary.annotations.title`,
      and `conversation_summaries.title`.
    """

    cascade_id: CascadeId
    project_id: AgyProjectId
    workspace_uris: Tuple[WorkspaceRootUri, ...]
    source: AgyGuiSafeSource
    title: str
    preview: str
    step_count: int
    created_at_iso: str
    updated_at_iso: str
    annotations: AnnotationPbtxtProjection
    nesting_depth: Literal[0] = 0


@dataclass(frozen=True)
class SummariesStateProjection:
    """Pure view projection for `~/.gemini/antigravity/agyhub_summaries_proto.pb` (grafted from Candidate 1).

    Corresponds to `jetbox_summaries_pb.SummariesState` (field 1 `summaries`, field 2 `file_mod_times`).
    """

    summaries: Mapping[CascadeId, CascadeSummaryProjection]
    file_mod_times: Mapping[str, int]


@dataclass(frozen=True)
class CodexThreadSummaryProjection:
    """Pure view projection for Codex `state_5.sqlite` `threads` row.

    Invariants (`idx_threads_project_recency` & `idx_threads_visible_recency_at_ms`):
    - `archived == 0`
    - `preview` is non-empty (`preview != ""`)
    - `project_id` is non-empty and matches `projects.id`
    - `cwd` equals the normalized workspace path.
    """

    thread_id: CodexThreadId
    project_id: CodexProjectId
    cwd: WorkspacePath
    title: str
    preview: str
    created_at_ms: int
    updated_at_ms: int
    archived: Literal[0] = 0


def resolve_canonical_title(
    events: Sequence[CanonicalEvent],
    *,
    annotations_pbtxt_title: str | None = None,
    summary_proto_annotation_title: str | None = None,
    summary_db_or_thread_title: str | None = None,
) -> str:
    """Resolve session title enforcing `sidebar_visibility_rules.json` `title_resolution_order`.

    Precedence order:
    1. `annotations_pbtxt_title` (`~/.gemini/antigravity/annotations/<id>.pbtxt`, field 1).
    2. `summary_proto_annotation_title` (`CascadeTrajectorySummary.annotations.title`, field 15 -> 1).
    3. `summary_db_or_thread_title` (`conversation_summaries.title` or Codex `threads.title`).
    4. Latest `CheckpointEvent.conversation_title` in `events` (`CortexStepCheckpoint.conversation_title`, field 10).
    5. First `UserInputEvent.query_text` single-line prefix (truncated to 60 chars).
    6. Fallback `"Untitled Session"`.
    """
    # TODO:
    # 1. Return the first non-empty stripped string among (annotations_pbtxt_title,
    #    summary_proto_annotation_title, summary_db_or_thread_title).
    # 2. Scan events in reverse for CheckpointEvent with non-empty conversation_title.
    # 3. Scan events forward for first UserInputEvent with non-empty query_text and truncate to 60 chars.
    # 4. Return "Untitled Session".
    raise NotImplementedError("resolve_canonical_title stub")


# ---------------------------------------------------------------------------
# Indexed Canonical Event Ledger (`TrajectoryLedger`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrajectoryLedger:
    """Append-only canonical event ledger with O(1) secondary lookup indices.

    Dominant access patterns served in O(1) without rescanning `events`:
    1. `by_step_index`: maps an Antigravity `StepIndex` to all `LedgerSeqNo` entries
       produced by that step (resolving the 1-to-N fanout of `step_type = 15` into
       `ThinkingEvent`, `AssistantTextEvent`, and N `ToolCallEvent` nodes, or
       `step_type = 132` into its `ToolResultEvent`).
    2. `by_call_id`: maps a `ToolCallId` to its `(call_seq_no, result_seq_no | None)`
       pair so `CortexStepGeneric` (`step_type = 132`) results join directly to their
       declaring `ChatToolCall` in `step_type = 15`.
    3. `by_turn_id`: groups all `LedgerSeqNo` entries belonging to one logical turn.
    4. `completed_turn_watermarks`: ordered watermarks of completed turns used by
       `CursorForkStateMachine` to slice incremental deltas without splitting in-flight turns.
    """

    workspace_path: WorkspacePath
    workspace_uri: WorkspaceRootUri
    title: str
    events: Tuple[CanonicalEvent, ...]
    by_step_index: Mapping[StepIndex, Tuple[LedgerSeqNo, ...]]
    by_call_id: Mapping[ToolCallId, Tuple[LedgerSeqNo, LedgerSeqNo | None]]
    by_turn_id: Mapping[TurnId, Tuple[LedgerSeqNo, ...]]
    completed_turn_watermarks: Tuple[CompletedTurnWatermark, ...]
    has_inflight_turn: bool

    @classmethod
    def from_events(
        cls,
        workspace_path: WorkspacePath,
        workspace_uri: WorkspaceRootUri,
        events: Sequence[CanonicalEvent],
        *,
        explicit_title_override: str | None = None,
    ) -> TrajectoryLedger:
        """Build a `TrajectoryLedger`, resolve canonical title, and populate all 4 indices in one pass.

        Invariants:
        - Resolves `title` via `resolve_canonical_title(events, explicit_title_override=...)`.
        - Every `ToolResultEvent` must reference a `ToolCallId` already declared by an
          earlier `ToolCallEvent` in the same turn, or synthesize a paired placeholder.
        - A turn is marked completed and appended to `completed_turn_watermarks` only when
          every `ToolCallId` opened in that turn has a non-None `result_seq_no` and the
          turn closes with an `AssistantTextEvent` or completion `TaskBoundaryEvent`.
        - Synthetic `StepIndex` and `RolloutLineNo` coordinates are deterministically
          assigned for events originating from the opposite host so watermark slicing is
          total over both coordinate spaces.
        """
        # TODO:
        # 1. Resolve canonical title via `resolve_canonical_title(events, explicit_title_override=explicit_title_override)`.
        # 2. Iterate `events` with `LedgerSeqNo(i)`, populating `by_step_index`, `by_call_id`,
        #    and `by_turn_id`.
        # 3. For each turn in `by_turn_id`, check whether all `ToolCallEvent` items in that
        #    turn have matching `ToolResultEvent` items and a terminal assistant/completion event.
        # 4. Compute deterministic SHA-256 `ContentDigest` over canonical event tuples up to
        #    the end of each completed turn and append `CompletedTurnWatermark`.
        # 5. Set `has_inflight_turn = True` if the trailing turn is incomplete.
        raise NotImplementedError("TrajectoryLedger.from_events stub")

    def latest_watermark(self) -> CompletedTurnWatermark | None:
        """Return the watermark of the last completed turn, or `None` if no turn has completed."""
        # TODO: return self.completed_turn_watermarks[-1] if self.completed_turn_watermarks else None
        raise NotImplementedError("TrajectoryLedger.latest_watermark stub")

    def find_watermark(self, turn_id: TurnId) -> CompletedTurnWatermark | None:
        """Look up a specific completed turn's watermark by `TurnId`."""
        # TODO: Return matching CompletedTurnWatermark or None.
        raise NotImplementedError("TrajectoryLedger.find_watermark stub")

    def slice_after_watermark(
        self,
        cursor_turn_id: TurnId | None,
    ) -> Tuple[CanonicalEvent, ...]:
        """Return the ordered events belonging to completed turns strictly after `cursor_turn_id`.

        Invariants:
        - Events belonging to a trailing in-flight turn (`has_inflight_turn == True`) are
          excluded so partial turns are never written to the peer host.
        - Raises `KeyError` if `cursor_turn_id` is not present in `completed_turn_watermarks`.
        """
        # TODO:
        # 1. Locate `cursor_turn_id` in `completed_turn_watermarks` (or start at 0 if None).
        # 2. Take events from `after_seq` through `latest_watermark().last_ledger_seq`.
        raise NotImplementedError("TrajectoryLedger.slice_after_watermark stub")

    def project_transcript_rows(self) -> Tuple[TranscriptRowProjection, ...]:
        """Project `brain/<id>/.system_generated/logs/transcript.jsonl` rows from the ledger.

        Invariant: Coalesces each `(ThinkingEvent, AssistantTextEvent, ToolCallEvent...)`
        group sharing an `agy_step_index` back into a single `PLANNER_RESPONSE` transcript
        row so `agy --conversation=<id>` sees a 1-to-1 match with `steps.idx`.
        """
        # TODO:
        # Group events by `agy_step_index` via `self.by_step_index` and emit one
        # `TranscriptRowProjection` per step index in ascending order.
        raise NotImplementedError("TrajectoryLedger.project_transcript_rows stub")

    def project_agy_annotations(
        self,
        cascade_id: CascadeId,
        viewed_at_iso: str,
    ) -> AnnotationPbtxtProjection:
        """Project `~/.gemini/antigravity/annotations/<cascade_id>.pbtxt` from the ledger."""
        # TODO: return AnnotationPbtxtProjection(cascade_id=cascade_id, title=self.title, last_user_view_time_iso=viewed_at_iso)
        raise NotImplementedError("TrajectoryLedger.project_agy_annotations stub")

    def project_agy_summary(
        self,
        cascade_id: CascadeId,
        project_id: AgyProjectId,
        viewed_at_iso: str,
        source: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
    ) -> CascadeSummaryProjection:
        """Project `CascadeTrajectorySummary` with 3-way title and sidebar-visibility invariants enforced."""
        # TODO:
        # Derive `step_count` from `len(self.by_step_index)`, `annotations` via `project_agy_annotations`,
        # and timestamps from first/last event anchors.
        raise NotImplementedError("TrajectoryLedger.project_agy_summary stub")

    def project_agy_hub_summaries_state(
        self,
        existing_state: SummariesStateProjection | None,
        updated_summary: CascadeSummaryProjection,
        conv_db_path: str,
        conv_db_mtime: int,
    ) -> SummariesStateProjection:
        """Project updated `SummariesStateProjection` for `agyhub_summaries_proto.pb` (grafted from Candidate 1)."""
        # TODO:
        # Merge `updated_summary` into `existing_state.summaries` and update `file_mod_times[conv_db_path] = conv_db_mtime`.
        raise NotImplementedError("TrajectoryLedger.project_agy_hub_summaries_state stub")

    def project_codex_thread_summary(
        self,
        thread_id: CodexThreadId,
        project_id: CodexProjectId,
    ) -> CodexThreadSummaryProjection:
        """Project Codex `threads` row with non-empty `preview` and `archived=0` invariants."""
        # TODO:
        # Derive non-empty `preview` from the first `UserInputEvent.query_text` (or fallback to `self.title`)
        # so `threads.preview <> ''` always holds.
        raise NotImplementedError("TrajectoryLedger.project_codex_thread_summary stub")
