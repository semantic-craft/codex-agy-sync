"""Canonical Indexed Event-Ledger IR and pure companion view projections."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Literal, Mapping, NewType, Sequence, Tuple, TypeAlias
from urllib.parse import unquote, urlsplit

from ._wire import UnknownWireField
from .payloads import (
    AskQuestionPayload, CanonicalToolPayload, DefineSubagentPayload, FileEditPayload,
    FinishPayload, GenerateImagePayload, InvokeSubagentPayload, ListResourcesPayload,
    ManageSubagentsPayload, ManageTaskPayload, MomaSearchPayload, OpaqueToolPayload,
    QuestionItem, QuestionOption, ReadFilePayload, ReadResourcePayload, RunCommandPayload,
    SchedulePayload, SearchWebDocument, SearchWebPayload, SendMessagePayload,
)

INTERRUPTED_TOOL_OUTPUT = "[Interrupted before tool completion]"
INTERRUPTED_TURN_TEXT = "[Turn interrupted before completion]"

WorkspaceRootUri = NewType("WorkspaceRootUri", str)
WorkspacePath = NewType("WorkspacePath", Path)
AgyProjectId = NewType("AgyProjectId", str)
CodexProjectId = NewType("CodexProjectId", str)
CodexLegacyProjectId = NewType("CodexLegacyProjectId", str)
CascadeId = NewType("CascadeId", str)
TrajectoryId = NewType("TrajectoryId", str)
CodexThreadId = NewType("CodexThreadId", str)
StepIndex = NewType("StepIndex", int)
RolloutLineNo = NewType("RolloutLineNo", int)
TurnId = NewType("TurnId", str)
ToolCallId = NewType("ToolCallId", str)
LedgerSeqNo = NewType("LedgerSeqNo", int)
ContentDigest = NewType("ContentDigest", str)

_FORBIDDEN_PROJECT_BUCKETS = frozenset({"", "default-cli-project", "outside-of-project", "null", "none"})
_LINE_ANNOT_RE = re.compile(r"\s+\((?:lines?|L)\b[^)]*\)\s*$", flags=re.IGNORECASE)
_MD_FILE_LINK_RE = re.compile(r"^\[[^\]]*\]\(\s*(file://.+?|/[^)]+?)\s*\)$", flags=re.IGNORECASE)


def parse_workspace_identity(raw_path_or_uri: str | Path) -> tuple[WorkspacePath, WorkspaceRootUri]:
    """Parse and canonicalize an untrusted path or `file://` URI at the boundary."""
    if raw_path_or_uri is None:
        raise ValueError("Workspace path cannot be None.")
    raw_text = str(raw_path_or_uri).strip()
    if not raw_text:
        raise ValueError("Workspace path cannot be empty.")
    if raw_text.startswith("file://"):
        parsed = urlsplit(raw_text)
        if parsed.netloc.lower() not in ("", "localhost"):
            raise ValueError(f"Unsupported non-local file URI authority: {parsed.netloc!r}")
        unquoted = unquote(parsed.path)
        if parsed.scheme != "file" or not unquoted.startswith("/"):
            raise ValueError(f"Invalid file URI {raw_path_or_uri!r}.")
        candidate = Path(unquoted)
    else:
        if "://" in raw_text:
            raise ValueError(f"Unsupported URI scheme in {raw_path_or_uri!r}.")
        expanded = os.path.expanduser(os.path.expandvars(raw_text))
        if expanded.startswith("~") or "$" in expanded or raw_text in (".", ".."):
            raise ValueError(f"Invalid or unexpanded workspace specifier {raw_path_or_uri!r}.")
        candidate = Path(expanded)
    resolved = candidate.resolve(strict=False)
    if str(resolved) == "/" or len(resolved.parts) <= 1:
        raise ValueError(f"Workspace path cannot resolve to filesystem root {raw_path_or_uri!r}.")
    return WorkspacePath(resolved), WorkspaceRootUri(resolved.as_uri().rstrip("/"))


def validate_session_id(raw_id: str, label: str = "session_id") -> str:
    """Validate an untrusted session or thread identifier at the boundary."""
    if (
        not isinstance(raw_id, str) or not raw_id or raw_id != raw_id.strip() or len(raw_id) > 128
        or any(sep in raw_id for sep in ("..", "/", "\\")) or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_id)
    ):
        raise ValueError(f"Invalid {label}: {raw_id!r}")
    return raw_id


def normalize_tool_file_path(raw_path: str) -> str:
    """Normalize a tool file path or `file://` URI at the codec boundary."""
    if not raw_path:
        return ""
    cleaned = str(raw_path).replace("\r", "").replace("\n", "").replace("\x00", "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("`"):
        end_bt = cleaned.find("`", 1)
        cleaned = cleaned[1:end_bt].strip() if end_bt != -1 else cleaned.strip("`").strip()
    cleaned = _LINE_ANNOT_RE.sub("", cleaned)
    if md_match := _MD_FILE_LINK_RE.match(cleaned):
        cleaned = md_match.group(1).strip()
    if cleaned.lower().startswith("file://"):
        split = urlsplit(_LINE_ANNOT_RE.sub("", cleaned))
        netloc = split.netloc
        unquoted_path = unquote(split.path)
        if netloc.lower() not in ("", "localhost"):
            raw_out = f"//{netloc}{unquoted_path}"
        elif len(unquoted_path) >= 3 and unquoted_path[0] == "/" and unquoted_path[1].isalpha() and unquoted_path[2] == ":":
            raw_out = unquoted_path[1:]
        else:
            raw_out = unquoted_path
        return raw_out.replace("\r", "").replace("\n", "").replace("\x00", "").strip()
    return cleaned


class HostKind(str, Enum):
    ANTIGRAVITY = "antigravity"
    CODEX = "codex"


class AgyGuiSafeSource(Enum):
    """Antigravity trajectory sources passing the Desktop sidebar filter (`17` CLI excluded)."""

    UNSPECIFIED = (0, "")
    CASCADE_CLIENT = (1, "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT")
    JETBOX = (18, "CORTEX_TRAJECTORY_SOURCE_JETBOX")
    AGENT_API = (19, "CORTEX_TRAJECTORY_SOURCE_AGENT_API")
    CIDER = (20, "CORTEX_TRAJECTORY_SOURCE_CIDER")

    @property
    def proto_enum_value(self) -> int:
        return self.value[0]

    @property
    def summary_column_name(self) -> str:
        return self.value[1]


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

    def to_json_line(self) -> str:
        from .codecs import render_transcript_row_jsonl

        return render_transcript_row_jsonl(self)


@dataclass(frozen=True)
class AnnotationPbtxtProjection:
    """Pure view projection for `~/.gemini/antigravity/annotations/<cascade_id>.pbtxt`."""

    cascade_id: CascadeId
    title: str
    last_user_view_time_iso: str
    unknown_wire_fields: Tuple[UnknownWireField, ...] = ()

    def render_pbtxt(self) -> str:
        from .codecs import render_annotation_pbtxt

        return render_annotation_pbtxt(self)


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
        if str(self.project_id).strip() in _FORBIDDEN_PROJECT_BUCKETS:
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
    def _from_wire_fields(cls, **kwargs: Any) -> CascadeSummaryProjection:
        proj = object.__new__(cls)
        for field in dataclasses.fields(cls):
            object.__setattr__(proj, field.name, kwargs[field.name] if field.name in kwargs else field.default)
        if proj.nesting_depth < 0:
            raise ValueError("CascadeSummaryProjection requires nesting_depth >= 0.")
        return proj

    @property
    def summary_proto_bytes(self) -> bytes:
        from .codecs import encode_cascade_summary_proto

        return encode_cascade_summary_proto(self)


@dataclass(frozen=True)
class SummariesStateProjection:
    """Pure view projection for `jetbox_summaries_pb.SummariesState` (`agyhub_summaries_proto.pb`)."""

    summaries: Mapping[CascadeId, CascadeSummaryProjection]
    file_mod_times: Mapping[str, int]
    unknown_wire_fields: Tuple[UnknownWireField, ...] = ()

    @property
    def proto_bytes(self) -> bytes:
        from .codecs import encode_summaries_state_proto

        return encode_summaries_state_proto(self)


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


def _normalize_single_line_query(query_text: str, limit: int = 80) -> str:
    lines = [s for raw in query_text.splitlines() if (s := raw.strip()) and s not in ("<USER_REQUEST>", "</USER_REQUEST>")]
    return " ".join(" ".join(lines).split())[:limit].strip()


def resolve_canonical_title(
    events: Sequence[CanonicalEvent], *, explicit_title_override: str | None = None,
    annotation_pbtxt_title: str | None = None, cascade_summary_annotation_title: str | None = None,
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
        if isinstance(ev, UserInputEvent) and ev.query_text and (normalized := _normalize_single_line_query(ev.query_text, limit=80)):
            return normalized
    return "Untitled Session"


_TERMINAL_TASK_STATUSES = frozenset({"task_complete", "done", "completed"})
_ABORT_EVENT_TYPES = frozenset({"error_message", "codex_event_msg:turn_aborted", "codex_event_msg:error"})
_NONTERMINAL_TASK_STATUSES = frozenset({
    "turn_aborted", "interrupted", "failed", "canceled", "cancelled",
    "task_started", "running", "in_progress", "starting",
})


def _is_host_framing_event(ev: CanonicalEvent) -> bool:
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


def _is_terminal_completion_event(ev: CanonicalEvent) -> bool:
    if isinstance(ev, AssistantTextEvent):
        return True
    return isinstance(ev, TaskBoundaryEvent) and (
        ev.boundary_kind == "notify_user" or ev.task_status.lower() in _TERMINAL_TASK_STATUSES
    )


def _is_nonterminal_work_or_abort_event(ev: CanonicalEvent) -> bool:
    if isinstance(ev, (UserInputEvent, ThinkingEvent, ToolCallEvent, ToolResultEvent)):
        return True
    if isinstance(ev, SystemEvent) and ev.event_type in _ABORT_EVENT_TYPES:
        return True
    return isinstance(ev, TaskBoundaryEvent) and ev.boundary_kind != "notify_user" and ev.task_status.lower() in _NONTERMINAL_TASK_STATUSES


def _canonical_event_digest_payload(ev: CanonicalEvent) -> Mapping[str, object]:
    from .codecs import serialize_tool_payload_arguments_json

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


def _index_ledger_events(
    events: Sequence[CanonicalEvent],
    *,
    recompute_agy_step_indices: bool,
) -> tuple[
    list[CanonicalEvent],
    dict[StepIndex, list[LedgerSeqNo]],
    dict[ToolCallId, tuple[LedgerSeqNo, LedgerSeqNo | None]],
    dict[TurnId, list[LedgerSeqNo]],
]:
    normalized: list[CanonicalEvent] = []
    by_step: dict[StepIndex, list[LedgerSeqNo]] = {}
    by_call: dict[ToolCallId, tuple[LedgerSeqNo, LedgerSeqNo | None]] = {}
    by_turn: dict[TurnId, list[LedgerSeqNo]] = {}
    next_step, next_line = 0, 1
    planner_step: StepIndex | None = None
    planner_turn: TurnId | None = None
    planner_flags = (False, False, False)

    for idx, ev in enumerate(events):
        seq_no, anchor = LedgerSeqNo(idx), ev.anchor
        if not recompute_agy_step_indices and anchor.agy_step_index is not None:
            step_idx, next_step = anchor.agy_step_index, max(next_step, int(anchor.agy_step_index) + 1)
            planner_step, planner_flags = None, (False, False, False)
        elif isinstance(ev, (ThinkingEvent, AssistantTextEvent, ToolCallEvent)):
            has_think, has_text, has_tools = planner_flags
            can_reuse = (
                planner_step is not None
                and planner_turn == anchor.turn_id
                and (
                    (isinstance(ev, ThinkingEvent) and not (has_think or has_text or has_tools))
                    or (isinstance(ev, AssistantTextEvent) and not (has_text or has_tools))
                    or isinstance(ev, ToolCallEvent)
                )
            )
            if not can_reuse:
                planner_step, next_step, planner_turn = StepIndex(next_step), next_step + 1, anchor.turn_id
                has_think, has_text, has_tools = (False, False, False)
            planner_flags = (
                has_think or isinstance(ev, ThinkingEvent),
                has_text or isinstance(ev, AssistantTextEvent),
                has_tools or isinstance(ev, ToolCallEvent),
            )
            step_idx = planner_step
        else:
            planner_step, planner_flags = None, (False, False, False)
            step_idx, next_step = StepIndex(next_step), next_step + 1

        if anchor.codex_line_no is not None:
            line_no, next_line = anchor.codex_line_no, max(next_line, int(anchor.codex_line_no) + 1)
        else:
            line_no, next_line = RolloutLineNo(next_line), next_line + 1

        if recompute_agy_step_indices or anchor.agy_step_index != step_idx or anchor.codex_line_no != line_no:
            ev = dataclasses.replace(ev, anchor=dataclasses.replace(anchor, agy_step_index=step_idx, codex_line_no=line_no))
        normalized.append(ev)
        by_step.setdefault(step_idx, []).append(seq_no)
        by_turn.setdefault(ev.anchor.turn_id, []).append(seq_no)
        if isinstance(ev, ToolCallEvent):
            by_call[ev.call_id] = (seq_no, None)
        elif isinstance(ev, ToolResultEvent):
            existing = by_call.get(ev.call_id)
            by_call[ev.call_id] = (existing[0], seq_no) if existing is not None else (seq_no, seq_no)
    return normalized, by_step, by_call, by_turn


def _is_turn_completed(
    turn_events: Sequence[CanonicalEvent],
    by_call: Mapping[ToolCallId, tuple[LedgerSeqNo, LedgerSeqNo | None]],
) -> bool:
    if not turn_events:
        return True
    all_calls_resolved = all(
        (pair := by_call.get(tev.call_id)) is not None and pair[1] is not None
        for tev in turn_events if isinstance(tev, ToolCallEvent)
    )
    has_active_agy = any(
        ext.extension_type == "agy_step_status"
        for tev in turn_events for ext in tev.anchor.host_extensions
    )
    last_work = max((i for i, tev in enumerate(turn_events) if _is_nonterminal_work_or_abort_event(tev)), default=-1)
    last_comp = max((i for i, tev in enumerate(turn_events) if _is_terminal_completion_event(tev)), default=-1)
    return all_calls_resolved and not has_active_agy and last_work <= last_comp


def _build_turn_watermarks(
    normalized_events: Sequence[CanonicalEvent],
    by_turn: Mapping[TurnId, Sequence[LedgerSeqNo]],
    by_call: Mapping[ToolCallId, tuple[LedgerSeqNo, LedgerSeqNo | None]],
) -> tuple[Tuple[CompletedTurnWatermark, ...], bool]:
    watermarks: list[CompletedTurnWatermark] = []
    cumulative_hasher = hashlib.sha256()
    turn_items = list(by_turn.items())

    for turn_pos, (turn_id, seq_list) in enumerate(turn_items):
        turn_events = [normalized_events[int(s)] for s in seq_list]
        if turn_pos == len(turn_items) - 1 and not _is_turn_completed(turn_events, by_call):
            return tuple(watermarks), True

        max_step_idx, max_line_no = StepIndex(0), RolloutLineNo(1)
        for tev in turn_events:
            if not _is_host_framing_event(tev):
                payload_bytes = json.dumps(
                    _canonical_event_digest_payload(tev), sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                cumulative_hasher.update(payload_bytes + b"\n")
            if tev.anchor.agy_step_index is not None and int(tev.anchor.agy_step_index) > int(max_step_idx):
                max_step_idx = tev.anchor.agy_step_index
            if tev.anchor.codex_line_no is not None and int(tev.anchor.codex_line_no) > int(max_line_no):
                max_line_no = tev.anchor.codex_line_no
        watermarks.append(
            CompletedTurnWatermark(
                turn_id, seq_list[-1], max_step_idx, max_line_no, ContentDigest(cumulative_hasher.hexdigest())
            )
        )
    return tuple(watermarks), False


def rescue_interrupted_turn(
    ledger: TrajectoryLedger,
    target_turn_id: TurnId | None = None,
) -> tuple[TrajectoryLedger, bool]:
    """Seal an incomplete turn (final or targeted `target_turn_id`) with canceled tool outputs and interruption text."""
    if not ledger.events or not ledger.by_turn_id:
        return ledger, False
    if target_turn_id is None:
        if not ledger.has_inflight_turn:
            return ledger, False
        target_tid = list(ledger.by_turn_id.keys())[-1]
    else:
        if target_turn_id not in ledger.by_turn_id:
            return ledger, False
        target_tid = target_turn_id
    raw_target = [ledger.events[int(s)] for s in ledger.by_turn_id[target_tid]]
    if _is_turn_completed(raw_target, ledger.by_call_id):
        return ledger, False

    target_seqs = {int(s) for s in ledger.by_turn_id[target_tid]}
    max_target_seq = max(target_seqs)
    rescued_events: list[CanonicalEvent] = []
    turn_slice: list[CanonicalEvent] = []
    stripped_on_asst = False
    for idx, ev in enumerate(ledger.events[: max_target_seq + 1]):
        if idx in target_seqs and any(ext.extension_type == "agy_step_status" for ext in ev.anchor.host_extensions):
            kept = tuple(ext for ext in ev.anchor.host_extensions if ext.extension_type != "agy_step_status")
            stripped_on_asst = stripped_on_asst or isinstance(ev, AssistantTextEvent)
            ev = dataclasses.replace(ev, anchor=dataclasses.replace(ev.anchor, host_extensions=kept))
        rescued_events.append(ev)
        if idx in target_seqs:
            turn_slice.append(ev)

    last_ev = turn_slice[-1]
    last_ts = last_ev.anchor.timestamp_iso
    for tev in list(turn_slice):
        if isinstance(tev, ToolCallEvent) and ((pair := ledger.by_call_id.get(tev.call_id)) is None or pair[1] is None):
            res_ev = ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(origin_host=tev.anchor.origin_host, turn_id=target_tid, timestamp_iso=last_ts),
                call_id=tev.call_id, tool_name=tev.tool_name, output_text=INTERRUPTED_TOOL_OUTPUT,
                is_error=True, payload=tev.payload,
            )
            rescued_events.append(res_ev)
            turn_slice.append(res_ev)
    last_work = max((i for i, e in enumerate(turn_slice) if _is_nonterminal_work_or_abort_event(e)), default=-1)
    last_asst = max((i for i, e in enumerate(turn_slice) if isinstance(e, AssistantTextEvent)), default=-1)
    if stripped_on_asst or last_asst == -1 or last_work > last_asst:
        rescued_events.append(
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(origin_host=last_ev.anchor.origin_host, turn_id=target_tid, timestamp_iso=last_ts),
                text=INTERRUPTED_TURN_TEXT,
            )
        )
    for trailing in ledger.events[max_target_seq + 1 :]:
        rescued_events.append(
            dataclasses.replace(trailing, anchor=dataclasses.replace(trailing.anchor, agy_step_index=None, codex_line_no=None))
        )
    return TrajectoryLedger.from_events(ledger.workspace_path, ledger.workspace_uri, rescued_events, explicit_title_override=ledger.title), True


def rescue_interrupted_ledger(ledger: TrajectoryLedger) -> tuple[TrajectoryLedger, bool]:
    """Seal a stopped trailing incomplete turn with canceled tool outputs and an interruption notice."""
    return rescue_interrupted_turn(ledger, None)


rescue_inflight_final_turn = rescue_interrupted_ledger


@dataclass(frozen=True)
class TrajectoryLedger:
    """Append-only canonical event ledger with O(1) secondary lookup indices."""

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
        cls, workspace_path: WorkspacePath, workspace_uri: WorkspaceRootUri, events: Sequence[CanonicalEvent],
        *, explicit_title_override: str | None = None, recompute_agy_step_indices: bool = False,
    ) -> TrajectoryLedger:
        resolved_title = resolve_canonical_title(events, explicit_title_override=explicit_title_override)
        normalized, by_step, by_call, by_turn = _index_ledger_events(events, recompute_agy_step_indices=recompute_agy_step_indices)
        watermarks, has_inflight = _build_turn_watermarks(normalized, by_turn, by_call)
        return cls(
            workspace_path=workspace_path, workspace_uri=workspace_uri, title=resolved_title,
            events=tuple(normalized), by_step_index={k: tuple(v) for k, v in by_step.items()},
            by_call_id=dict(by_call), by_turn_id={k: tuple(v) for k, v in by_turn.items()},
            completed_turn_watermarks=watermarks, has_inflight_turn=has_inflight,
        )

    def latest_watermark(self) -> CompletedTurnWatermark | None:
        return self.completed_turn_watermarks[-1] if self.completed_turn_watermarks else None

    def find_watermark(self, turn_id: TurnId) -> CompletedTurnWatermark | None:
        return next((wm for wm in self.completed_turn_watermarks if wm.turn_id == turn_id), None)

    def events_for_step(self, step_index: StepIndex) -> Tuple[CanonicalEvent, ...]:
        return tuple(self.events[int(s)] for s in self.by_step_index.get(step_index, ()))

    def call_and_result(self, call_id: ToolCallId) -> Tuple[ToolCallEvent, ToolResultEvent | None]:
        if call_id not in self.by_call_id:
            raise KeyError(f"ToolCallId {call_id!r} not found in ledger.")
        call_seq, res_seq = self.by_call_id[call_id]
        call_ev = self.events[int(call_seq)]
        if not isinstance(call_ev, ToolCallEvent):
            raise ValueError(f"Expected ToolCallEvent at seq {call_seq}, found {type(call_ev).__name__}.")
        res_ev = self.events[int(res_seq)] if res_seq is not None and res_seq != call_seq else None
        return (call_ev, res_ev if isinstance(res_ev, ToolResultEvent) else None)

    def slice_after_watermark(self, cursor_turn_id: TurnId | CompletedTurnWatermark | None) -> Tuple[CanonicalEvent, ...]:
        if not self.completed_turn_watermarks:
            if cursor_turn_id is not None:
                raise KeyError(f"Turn {cursor_turn_id!r} not found in empty watermarks.")
            return ()
        last_completed_seq = int(self.completed_turn_watermarks[-1].last_ledger_seq)
        if cursor_turn_id is None:
            return self.events[: last_completed_seq + 1]
        target_id = cursor_turn_id.turn_id if isinstance(cursor_turn_id, CompletedTurnWatermark) else cursor_turn_id
        matched_wm = self.find_watermark(target_id)
        if matched_wm is None:
            raise KeyError(f"TurnId {target_id!r} not found in completed_turn_watermarks.")
        start_seq = int(matched_wm.last_ledger_seq) + 1
        return () if start_seq > last_completed_seq else self.events[start_seq : last_completed_seq + 1]

    def project_transcript_rows(self) -> Tuple[TranscriptRowProjection, ...]:
        from .codecs.projections import project_ledger_transcript_rows
        return project_ledger_transcript_rows(self)

    def project_annotation_pbtxt(self, cascade_id: CascadeId, viewed_at_iso: str | None = None) -> AnnotationPbtxtProjection:
        ts = viewed_at_iso or (self.events[-1].anchor.timestamp_iso if self.events else "2026-01-01T00:00:00Z")
        return AnnotationPbtxtProjection(cascade_id=cascade_id, title=self.title, last_user_view_time_iso=ts)

    def project_agy_cascade_summary(
        self, cascade_id: CascadeId, project_id: AgyProjectId, viewed_at_iso: str | None = None,
        source: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT, lineage: SubagentLineage | None = None,
    ) -> CascadeSummaryProjection:
        from .codecs.projections import project_ledger_cascade_summary
        return project_ledger_cascade_summary(self, cascade_id, project_id, viewed_at_iso=viewed_at_iso, source=source, lineage=lineage)

    def project_agy_hub_summaries_state(
        self, existing_state: SummariesStateProjection | None, updated_summary: CascadeSummaryProjection,
        conv_db_path: str, conv_db_mtime: int,
    ) -> SummariesStateProjection:
        merged_summaries = dict(existing_state.summaries) if existing_state is not None else {}
        merged_mtimes = dict(existing_state.file_mod_times) if existing_state is not None else {}
        unknowns = existing_state.unknown_wire_fields if existing_state is not None else ()
        merged_summaries[updated_summary.cascade_id] = updated_summary
        merged_mtimes[str(conv_db_path)] = int(conv_db_mtime)
        return SummariesStateProjection(summaries=merged_summaries, file_mod_times=merged_mtimes, unknown_wire_fields=unknowns)

    def project_codex_thread_summary(self, thread_id: CodexThreadId, project_id: CodexProjectId) -> CodexThreadSummaryProjection:
        from .codecs.projections import project_ledger_codex_thread_summary
        return project_ledger_codex_thread_summary(self, thread_id, project_id)

    def _derive_preview_text(self) -> str:
        from .codecs.projections import derive_ledger_preview_text
        return derive_ledger_preview_text(self)
