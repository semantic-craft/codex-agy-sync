"""Tier 1 canonical event types, pure host projection value objects, and domain predicates."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence, Tuple, TypeAlias

from ._wire import UnknownWireField
from .payloads import CanonicalToolPayload, safe_json_dict, serialize_tool_payload_arguments_json
from .workspace import (
    FORBIDDEN_AGY_PROJECT_IDS,
    AgyGuiSafeSource,
    AgyProjectId,
    CascadeId,
    CodexProjectId,
    CodexThreadId,
    ContentDigest,
    HostKind,
    LedgerSeqNo,
    RolloutLineNo,
    StepIndex,
    ToolCallId,
    TrajectoryId,
    TurnId,
    WorkspacePath,
    WorkspaceRootUri,
)

INTERRUPTED_TOOL_OUTPUT = "[Interrupted before tool completion]"
INTERRUPTED_TURN_TEXT = "[Turn interrupted before completion]"


@dataclass(frozen=True)
class HostExtensionEnvelope:
    host: HostKind
    extension_type: str
    raw_payload: bytes
    metadata: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EventOriginAnchor:
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
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    thinking_duration_ms: int | None = None


@dataclass(frozen=True)
class MediaAttachment:
    mime_type: str
    uri: str
    byte_size: int | None = None
    caption: str | None = None


@dataclass(frozen=True)
class SubagentLineage:
    parent_cascade_id: CascadeId | None = None
    root_cascade_id: CascadeId | None = None
    parent_codex_thread_id: CodexThreadId | None = None
    nesting_depth: int = 0
    agent_type_name: str = ""
    agent_role: str = ""
    spawn_step_index: StepIndex | None = None

    def __post_init__(self) -> None:
        if self.nesting_depth < 0:
            raise ValueError("SubagentLineage.nesting_depth must be >= 0.")

    @property
    def is_subagent(self) -> bool:
        return self.nesting_depth > 0 or self.spawn_step_index is not None


@dataclass(frozen=True)
class UserInputEvent:
    kind: Literal["user_input"]
    anchor: EventOriginAnchor
    query_text: str
    scope_items: Tuple[str, ...] = ()
    media: Tuple[MediaAttachment, ...] = ()


@dataclass(frozen=True)
class ThinkingEvent:
    kind: Literal["thinking"]
    anchor: EventOriginAnchor
    summary_text: str
    raw_thinking: str | None = None
    thinking_signature: bytes | None = None
    token_usage: TokenUsageAccounting = TokenUsageAccounting()


@dataclass(frozen=True)
class AssistantTextEvent:
    kind: Literal["assistant_text"]
    anchor: EventOriginAnchor
    text: str
    message_id: str | None = None
    stop_reason: str | None = None
    generator_model: str | None = None
    token_usage: TokenUsageAccounting = TokenUsageAccounting()


@dataclass(frozen=True)
class ToolCallEvent:
    kind: Literal["tool_call"]
    anchor: EventOriginAnchor
    call_id: ToolCallId
    tool_name: str
    tool_summary: str
    payload: CanonicalToolPayload
    thinking_signature: bytes | None = None


@dataclass(frozen=True)
class ToolResultEvent:
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
    kind: Literal["checkpoint"]
    anchor: EventOriginAnchor
    checkpoint_index: int
    user_intent: str
    session_summary: str
    code_change_summary: str
    conversation_title: str | None = None


@dataclass(frozen=True)
class SystemEvent:
    kind: Literal["system_message"]
    anchor: EventOriginAnchor
    event_type: str
    message_text: str


CanonicalEvent: TypeAlias = (
    UserInputEvent | ThinkingEvent | AssistantTextEvent | ToolCallEvent
    | ToolResultEvent | TaskBoundaryEvent | CheckpointEvent | SystemEvent
)


@dataclass(frozen=True)
class CompletedTurnWatermark:
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
    thinking: str | None = None
    tool_calls_json: Tuple[Mapping[str, object], ...] = ()
    media_uris: Tuple[Mapping[str, str], ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    truncated_fields: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AnnotationPbtxtProjection:
    """Pure view projection for `~/.gemini/antigravity/annotations/<cascade_id>.pbtxt`."""

    cascade_id: CascadeId
    title: str
    last_user_view_time_iso: str
    unknown_wire_fields: Tuple[UnknownWireField, ...] = ()


@dataclass(frozen=True)
class CascadeSummaryProjection:
    """Pure view projection for `conversation_summaries.db` and `agyhub_summaries_proto.pb`."""

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
    nesting_depth: int = 0
    parent_cascade_id: CascadeId | None = None
    root_cascade_id: CascadeId | None = None
    agent_name: str = ""
    agent_type_name: str = ""
    agent_role: str = ""
    status: int = 1
    trajectory_id: TrajectoryId | None = None
    trajectory_type: int = 1
    last_user_input_time_iso: str = ""
    last_user_input_step_index: int | None = None
    trajectory_metadata_unknown_fields: Tuple[UnknownWireField, ...] = ()
    unknown_wire_fields: Tuple[UnknownWireField, ...] = ()

    def __post_init__(self) -> None:
        if str(self.project_id).strip() in FORBIDDEN_AGY_PROJECT_IDS:
            raise ValueError(f"Forbidden Antigravity project_id {self.project_id!r}.")
        if not self.workspace_uris:
            raise ValueError("CascadeSummaryProjection requires non-empty workspace_uris.")
        if self.nesting_depth < 0:
            raise ValueError("CascadeSummaryProjection requires nesting_depth >= 0.")
        if not isinstance(self.source, AgyGuiSafeSource) or self.source.proto_enum_value == 17:
            raise ValueError(f"Illegal CLI trajectory source {self.source!r}.")
        if self.title and self.annotations.title != self.title:
            object.__setattr__(self, "annotations", dataclasses.replace(self.annotations, title=self.title))

    @classmethod
    def from_wire_fields(cls, **kwargs: Any) -> CascadeSummaryProjection:
        proj = object.__new__(cls)
        for field in dataclasses.fields(cls):
            object.__setattr__(proj, field.name, kwargs[field.name] if field.name in kwargs else field.default)
        if proj.nesting_depth < 0:
            raise ValueError("CascadeSummaryProjection requires nesting_depth >= 0.")
        return proj


@dataclass(frozen=True)
class SummariesStateProjection:
    """Pure view projection for `jetbox_summaries_pb.SummariesState` (`agyhub_summaries_proto.pb`)."""

    summaries: Mapping[CascadeId, CascadeSummaryProjection]
    file_mod_times: Mapping[str, int]
    unknown_wire_fields: Tuple[UnknownWireField, ...] = ()


@dataclass(frozen=True)
class CodexThreadSummaryProjection:
    """Pure view projection for Codex `state_5.sqlite` `threads` row."""

    thread_id: CodexThreadId
    project_id: CodexProjectId
    cwd: WorkspacePath
    title: str
    preview: str
    created_at_ms: int
    updated_at_ms: int
    archived: Literal[0] = 0

    def __post_init__(self) -> None:
        if self.archived != 0:
            raise ValueError("CodexThreadSummaryProjection requires archived == 0.")
        if not self.preview or not self.preview.strip():
            raise ValueError("CodexThreadSummaryProjection requires non-empty preview.")
        if not self.project_id or not str(self.project_id).strip():
            raise ValueError("CodexThreadSummaryProjection requires non-empty project_id.")


def normalize_single_line_query(query_text: str, limit: int = 80) -> str:
    lines = [s for raw in query_text.splitlines() if (s := raw.strip()) and s not in ("<USER_REQUEST>", "</USER_REQUEST>")]
    return " ".join(" ".join(lines).split())[:limit].strip()


def resolve_canonical_title(
    events: Sequence[CanonicalEvent],
    *,
    explicit_title_override: str | None = None,
    annotation_pbtxt_title: str | None = None,
    cascade_summary_annotation_title: str | None = None,
    db_summary_row_title: str | None = None,
) -> str:
    """Resolve session title enforcing `sidebar_visibility_rules.json` `title_resolution_order`."""
    for candidate in (explicit_title_override, annotation_pbtxt_title, cascade_summary_annotation_title, db_summary_row_title):
        if candidate is not None and (cleaned := " ".join(candidate.split())):
            return cleaned
    for ev in reversed(events):
        if isinstance(ev, CheckpointEvent) and ev.conversation_title and (cleaned := " ".join(ev.conversation_title.split())):
            return cleaned
    for ev in events:
        if isinstance(ev, UserInputEvent) and ev.query_text and (normalized := normalize_single_line_query(ev.query_text, limit=80)):
            return normalized
    return "Untitled Session"


_TERMINAL_TASK_STATUSES = frozenset({"task_complete", "done", "completed"})
_ABORT_EVENT_TYPES = frozenset({"error_message", "codex_event_msg:turn_aborted", "codex_event_msg:error"})
_NONTERMINAL_TASK_STATUSES = frozenset({
    "turn_aborted", "interrupted", "failed", "canceled", "cancelled",
    "task_started", "running", "in_progress", "starting",
})


def is_host_framing_event(ev: CanonicalEvent) -> bool:
    if isinstance(ev, SystemEvent) and ev.event_type in ("session_meta", "world_state"):
        return True
    if isinstance(ev, ToolResultEvent) and any(ext.extension_type == "synthesized_dangling_output" for ext in ev.anchor.host_extensions):
        return True
    return (
        isinstance(ev, TaskBoundaryEvent)
        and not ev.task_name.strip()
        and ev.task_summary.strip() in ("", "Turn complete")
        and (ev.boundary_kind in ("task_started", "task_complete") or ev.task_status in ("task_started", "task_complete"))
    )


def is_terminal_completion_event(ev: CanonicalEvent) -> bool:
    if isinstance(ev, AssistantTextEvent):
        return True
    return isinstance(ev, TaskBoundaryEvent) and (
        ev.boundary_kind == "notify_user" or ev.task_status.lower() in _TERMINAL_TASK_STATUSES
    )


def is_nonterminal_work_or_abort_event(ev: CanonicalEvent) -> bool:
    if isinstance(ev, (UserInputEvent, ThinkingEvent, ToolCallEvent, ToolResultEvent)):
        return True
    if isinstance(ev, SystemEvent) and ev.event_type in _ABORT_EVENT_TYPES:
        return True
    return isinstance(ev, TaskBoundaryEvent) and ev.boundary_kind != "notify_user" and ev.task_status.lower() in _NONTERMINAL_TASK_STATUSES


def is_host_injected_user_event(ev: UserInputEvent) -> bool:
    """Return `True` when a Codex user message contains solely `agents_md` or `harness` provenance."""
    if ev.anchor.origin_host != HostKind.CODEX:
        return False
    for extension in ev.anchor.host_extensions:
        if extension.extension_type != "codex_message_provenance":
            continue
        data = safe_json_dict(extension.raw_payload.decode("utf-8", errors="replace"))
        metadata = data.get("content_item_metadata")
        kinds = data.get("content_item_kinds") or []
        if any(isinstance(kind, str) and kind.startswith("user.") for kind in kinds):
            return False
        return (
            isinstance(metadata, list)
            and bool(metadata)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("provenance"), dict)
                and item["provenance"].get("type") in {"agents_md", "harness"}
                for item in metadata
            )
        )
    return False


def is_host_injected_tool_result(ev: ToolResultEvent) -> bool:
    """Return `True` when a Codex tool result originated from host-injected app tools without a call."""
    if ev.anchor.origin_host != HostKind.CODEX:
        return False
    for extension in ev.anchor.host_extensions:
        if extension.extension_type != "codex_unpaired_tool_output":
            continue
        data = safe_json_dict(extension.raw_payload.decode("utf-8", errors="replace"))
        return (
            not data.get("call_id")
            and data.get("namespace") == "agent"
            and isinstance(data.get("name"), str)
            and data["name"].startswith("codex_apps_")
        )
    return False


def canonical_event_digest_payload(ev: CanonicalEvent) -> Mapping[str, object]:
    base: dict[str, object] = {"kind": ev.kind, "turn_id": str(ev.anchor.turn_id)}
    if isinstance(ev, UserInputEvent):
        out: dict[str, object] = {**base, "query_text": ev.query_text, "scope_items": list(ev.scope_items)}
        if ev.media:
            out["media"] = [{"mime_type": m.mime_type, "uri": m.uri} for m in ev.media]
        return out
    if isinstance(ev, ThinkingEvent):
        return {**base, "summary_text": ev.summary_text}
    if isinstance(ev, AssistantTextEvent):
        return {**base, "text": ev.text}
    if isinstance(ev, ToolCallEvent):
        cname = ev.payload.kind if ev.payload.kind != "opaque" else ev.tool_name
        args = serialize_tool_payload_arguments_json(ev.payload)
        return {**base, "call_id": str(ev.call_id), "tool_name": cname, "payload_kind": ev.payload.kind, "arguments_json": args}
    if isinstance(ev, ToolResultEvent):
        cname = ev.payload.kind if ev.payload.kind != "opaque" else ev.tool_name
        return {**base, "call_id": str(ev.call_id), "tool_name": cname, "output_text": ev.output_text, "is_error": ev.is_error}
    if isinstance(ev, TaskBoundaryEvent):
        return {**base, "boundary_kind": ev.boundary_kind, "task_name": ev.task_name, "task_status": ev.task_status, "task_summary": ev.task_summary}
    if isinstance(ev, CheckpointEvent):
        return {**base, "checkpoint_index": ev.checkpoint_index, "user_intent": ev.user_intent, "session_summary": ev.session_summary, "conversation_title": ev.conversation_title or ""}
    if isinstance(ev, SystemEvent):
        return {**base, "event_type": ev.event_type, "message_text": ev.message_text}
    return {"kind": "unknown"}


__all__ = [
    "AnnotationPbtxtProjection",
    "AssistantTextEvent",
    "CanonicalEvent",
    "CascadeSummaryProjection",
    "CheckpointEvent",
    "CodexThreadSummaryProjection",
    "CompletedTurnWatermark",
    "EventOriginAnchor",
    "HostExtensionEnvelope",
    "INTERRUPTED_TOOL_OUTPUT",
    "INTERRUPTED_TURN_TEXT",
    "MediaAttachment",
    "SubagentLineage",
    "SummariesStateProjection",
    "SystemEvent",
    "TaskBoundaryEvent",
    "ThinkingEvent",
    "TokenUsageAccounting",
    "ToolCallEvent",
    "ToolResultEvent",
    "TranscriptRowProjection",
    "UserInputEvent",
    "canonical_event_digest_payload",
    "is_host_framing_event",
    "is_host_injected_tool_result",
    "is_host_injected_user_event",
    "is_nonterminal_work_or_abort_event",
    "is_terminal_completion_event",
    "normalize_single_line_query",
    "resolve_canonical_title",
]
