"""Canonical Indexed Event-Ledger IR and pure companion view projections.

Invariants encoded in this module.
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
4. Only completed turns appear in `completed_turn_watermarks`. In-flight turns
   never advance a sync watermark.
5. Companion files (`transcript.jsonl`, `annotations/<id>.pbtxt` with 3-tier title
   precedence, `CascadeTrajectorySummary`, `SummariesState` for `agyhub_summaries_proto.pb`,
   and Codex `threads` row) are pure view projections derived from `TrajectoryLedger`
   rather than independently mutated stores.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
from typing import Literal, Mapping, NewType, Sequence, Tuple, TypeAlias
from urllib.parse import unquote, urlparse

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
      Codex `project_roots.path` and `threads.cwd` normalize to the exact same key.
    - Rejects relative paths that cannot be resolved or empty strings.
    """
    if raw_path_or_uri is None:
        raise ValueError("Workspace path cannot be None.")

    raw_text = str(raw_path_or_uri).strip()
    if not raw_text:
        raise ValueError("Workspace path cannot be empty.")

    if raw_text.startswith("file://"):
        parsed = urlparse(raw_text)
        if parsed.scheme != "file":
            raise ValueError(f"Unsupported URI scheme in {raw_path_or_uri!r}.")
        unquoted_path = unquote(parsed.path)
        if not unquoted_path or not unquoted_path.startswith("/"):
            raise ValueError(f"Invalid file URI {raw_path_or_uri!r}.")
        candidate = Path(unquoted_path)
    else:
        if "://" in raw_text:
            raise ValueError(f"Unsupported URI scheme in {raw_path_or_uri!r}.")
        expanded = os.path.expanduser(os.path.expandvars(raw_text))
        if expanded.startswith("~") or "$" in expanded:
            raise ValueError(f"Unexpanded home or environment variable in {raw_path_or_uri!r}.")
        if raw_text in (".", ".."):
            raise ValueError(f"Ambiguous relative workspace specifier {raw_path_or_uri!r}.")
        candidate = Path(expanded)

    resolved = candidate.resolve(strict=False)
    if str(resolved) == "/" or len(resolved.parts) <= 1:
        raise ValueError(f"Workspace path cannot resolve to filesystem root {raw_path_or_uri!r}.")

    uri = resolved.as_uri().rstrip("/")
    return WorkspacePath(resolved), WorkspaceRootUri(uri)


# ---------------------------------------------------------------------------
# Sidebar-Safe Source, Lossless Wire Envelopes & Provenance Types
# ---------------------------------------------------------------------------


class HostKind(str, Enum):
    """Supported agent host runtimes."""

    ANTIGRAVITY = "antigravity"
    CODEX = "codex"


class AgyGuiSafeSource(Enum):
    """Antigravity trajectory sources guaranteed to pass the Desktop sidebar filter.

    Invariant. `CORTEX_TRAJECTORY_SOURCE_CLI` (enum `17`) is intentionally omitted
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
        return self.value[0]

    @property
    def summary_column_name(self) -> str:
        """String value stored in `conversation_summaries.source`."""
        return self.value[1]


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
    `gen_metadata` and `executor_metadata` blobs across round-trip conversions.
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
# Typed Canonical Tool Payloads (16 `exa.cortex.core.tools.*` <-> Codex tools)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadFilePayload:
    """Domain payload for `exa.cortex.core.tools.ReadFile` <-> Codex `view_file`."""

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
    diff_stats: Tuple[int, int] = (0, 0)
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
    """Domain payload for `exa.cortex.core.tools.ManageTask` <-> Codex `wait` and `send_input`."""

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
    """Domain payload for `exa.cortex.core.tools.Schedule` <-> Codex timer and cron tool."""

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
    """User prompt event (`step_type=14` / `Step.user_input` tag `19` <-> Codex `message` with role `user`)."""

    kind: Literal["user_input"]
    anchor: EventOriginAnchor
    query_text: str
    scope_items: Tuple[str, ...] = ()
    media: Tuple[MediaAttachment, ...] = ()


@dataclass(frozen=True)
class ThinkingEvent:
    """Model reasoning summary (`CortexStepPlannerResponse.thinking` <-> Codex `reasoning`)."""

    kind: Literal["thinking"]
    anchor: EventOriginAnchor
    summary_text: str
    raw_thinking: str | None = None
    thinking_signature: bytes | None = None
    token_usage: TokenUsageAccounting = TokenUsageAccounting()


@dataclass(frozen=True)
class AssistantTextEvent:
    """Assistant prose response (`CortexStepPlannerResponse.response` <-> Codex `message` with role `assistant`)."""

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
    """Task boundary or user notification (`step_type=81` tag `93` or `step_type=82` tag `94` <-> Codex `event_msg`)."""

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
    """Session checkpoint (`step_type=23` `Step.checkpoint` tag `30` <-> Codex `turn_context`)."""

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
# Protobuf Wire Helpers for Companion Projections
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _encode_varint_field(field_number: int, value: int) -> bytes:
    tag = (field_number << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)


def _encode_bytes_field(field_number: int, payload: bytes) -> bytes:
    tag = (field_number << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(payload)) + payload


def _encode_string_field(field_number: int, text: str) -> bytes:
    if not text:
        return b""
    return _encode_bytes_field(field_number, text.encode("utf-8"))


def _parse_iso_to_seconds_nanos(iso_str: str) -> tuple[int, int]:
    cleaned = (iso_str or "").strip()
    if not cleaned:
        return (1700000000, 0)
    try:
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        epoch = dt.timestamp()
        seconds = int(epoch)
        nanos = int(round((epoch - seconds) * 1_000_000_000))
        if nanos < 0:
            nanos = 0
        return (seconds, nanos)
    except ValueError:
        return (1700000000, 0)


def _encode_timestamp_proto(iso_str: str) -> bytes:
    seconds, nanos = _parse_iso_to_seconds_nanos(iso_str)
    buf = bytearray()
    if seconds:
        buf.extend(_encode_varint_field(1, seconds))
    if nanos:
        buf.extend(_encode_varint_field(2, nanos))
    return bytes(buf)


def _encode_annotations_proto(proj: AnnotationPbtxtProjection) -> bytes:
    buf = bytearray()
    if proj.title:
        buf.extend(_encode_string_field(1, proj.title))
    ts_bytes = _encode_timestamp_proto(proj.last_user_view_time_iso)
    if ts_bytes:
        buf.extend(_encode_bytes_field(7, ts_bytes))
    return bytes(buf)


def _encode_trajectory_metadata_proto(
    project_id: AgyProjectId,
    workspace_uris: Tuple[WorkspaceRootUri, ...],
    created_at_iso: str,
    nesting_depth: int = 0,
    parent_conversation_id: str = "",
) -> bytes:
    buf = bytearray()
    for uri in workspace_uris:
        ws_meta = _encode_string_field(1, str(uri)) + _encode_string_field(2, str(uri))
        buf.extend(_encode_bytes_field(1, ws_meta))
    ts_bytes = _encode_timestamp_proto(created_at_iso)
    if ts_bytes:
        buf.extend(_encode_bytes_field(2, ts_bytes))
    if parent_conversation_id:
        buf.extend(_encode_string_field(5, parent_conversation_id))
    for uri in workspace_uris:
        buf.extend(_encode_string_field(7, str(uri)))
    if nesting_depth != 0:
        buf.extend(_encode_varint_field(17, nesting_depth))
    if project_id:
        buf.extend(_encode_string_field(18, str(project_id)))
    return bytes(buf)


def _encode_cascade_summary_proto(proj: CascadeSummaryProjection) -> bytes:
    buf = bytearray()
    if proj.title:
        buf.extend(_encode_string_field(1, proj.title))
    if proj.step_count > 0:
        buf.extend(_encode_varint_field(2, proj.step_count))
    mod_ts = _encode_timestamp_proto(proj.updated_at_iso)
    if mod_ts:
        buf.extend(_encode_bytes_field(3, mod_ts))
    buf.extend(_encode_string_field(4, str(proj.cascade_id)))
    buf.extend(_encode_varint_field(5, 1))
    created_ts = _encode_timestamp_proto(proj.created_at_iso)
    if created_ts:
        buf.extend(_encode_bytes_field(7, created_ts))
    for uri in proj.workspace_uris:
        ws_meta = _encode_string_field(1, str(uri)) + _encode_string_field(2, str(uri))
        buf.extend(_encode_bytes_field(9, ws_meta))
    if mod_ts:
        buf.extend(_encode_bytes_field(10, mod_ts))
    annot_bytes = _encode_annotations_proto(proj.annotations)
    if annot_bytes:
        buf.extend(_encode_bytes_field(15, annot_bytes))
    traj_meta_bytes = _encode_trajectory_metadata_proto(
        project_id=proj.project_id,
        workspace_uris=proj.workspace_uris,
        created_at_iso=proj.created_at_iso,
        nesting_depth=proj.nesting_depth,
    )
    if traj_meta_bytes:
        buf.extend(_encode_bytes_field(17, traj_meta_bytes))
    if proj.source.proto_enum_value != 0:
        buf.extend(_encode_varint_field(20, proj.source.proto_enum_value))
    buf.extend(_encode_varint_field(22, 1))
    return bytes(buf)


def _encode_summaries_state_proto(state: SummariesStateProjection) -> bytes:
    buf = bytearray()
    for cid in sorted(state.summaries.keys()):
        summary_proj = state.summaries[cid]
        entry = (
            _encode_string_field(1, str(cid))
            + _encode_bytes_field(2, summary_proj.summary_proto_bytes)
        )
        buf.extend(_encode_bytes_field(1, entry))
    for path_key in sorted(state.file_mod_times.keys()):
        mtime_val = state.file_mod_times[path_key]
        entry = _encode_string_field(1, path_key) + _encode_varint_field(2, int(mtime_val))
        buf.extend(_encode_bytes_field(2, entry))
    return bytes(buf)


def _read_proto_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos


def _iter_proto_fields(buf: bytes) -> list[tuple[int, int, int, bytes]]:
    fields: list[tuple[int, int, int, bytes]] = []
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _read_proto_varint(buf, pos)
        fn = tag >> 3
        wt = tag & 0x07
        if wt == 0:
            val, pos = _read_proto_varint(buf, pos)
            fields.append((fn, wt, val, b""))
        elif wt == 1:
            fields.append((fn, wt, 0, buf[pos : pos + 8]))
            pos += 8
        elif wt == 2:
            ln, pos = _read_proto_varint(buf, pos)
            fields.append((fn, wt, ln, buf[pos : pos + ln]))
            pos += ln
        elif wt == 5:
            fields.append((fn, wt, 0, buf[pos : pos + 4]))
            pos += 4
        else:
            break
    return fields


def decode_summaries_state_proto(raw_bytes: bytes) -> SummariesStateProjection:
    summaries: dict[CascadeId, CascadeSummaryProjection] = {}
    file_mod_times: dict[str, int] = {}
    for fn, wt, _, payload in _iter_proto_fields(raw_bytes):
        if fn == 1 and wt == 2:
            cid_str = ""
            sum_bytes = b""
            for efn, ewt, _, epayload in _iter_proto_fields(payload):
                if efn == 1 and ewt == 2:
                    cid_str = epayload.decode("utf-8", errors="replace")
                elif efn == 2 and ewt == 2:
                    sum_bytes = bytes(epayload)
            if cid_str:
                placeholder = CascadeSummaryProjection(
                    cascade_id=CascadeId(cid_str),
                    project_id=AgyProjectId("00000000-0000-0000-0000-000000000000"),
                    workspace_uris=(WorkspaceRootUri("file:///"),),
                    source=AgyGuiSafeSource.CASCADE_CLIENT,
                    title="Preserved Session",
                    preview="Preserved Session",
                    step_count=1,
                    created_at_iso="1970-01-01T00:00:00Z",
                    updated_at_iso="1970-01-01T00:00:00Z",
                    annotations=AnnotationPbtxtProjection(
                        cascade_id=CascadeId(cid_str),
                        title="Preserved Session",
                        last_user_view_time_iso="1970-01-01T00:00:00Z",
                    ),
                    nesting_depth=0,
                    summary_proto_bytes=sum_bytes,
                )
                summaries[CascadeId(cid_str)] = placeholder
        elif fn == 2 and wt == 2:
            pkey = ""
            mval = 0
            for efn, ewt, eval_int, epayload in _iter_proto_fields(payload):
                if efn == 1 and ewt == 2:
                    pkey = epayload.decode("utf-8", errors="replace")
                elif efn == 2 and ewt == 0:
                    mval = int(eval_int)
            if pkey:
                file_mod_times[pkey] = mval
    return SummariesStateProjection(
        summaries=summaries,
        file_mod_times=file_mod_times,
        proto_bytes=raw_bytes,
    )


# ---------------------------------------------------------------------------
# Completed-Turn Watermark & Companion View Projections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletedTurnWatermark:
    """Immutable boundary record for one completed turn inside `TrajectoryLedger`.

    Invariant. Only turns whose tool calls have all resolved and whose terminal
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

    def to_json_line(self) -> str:
        """Render one compact JSONL line matching Antigravity `transcript.jsonl` format."""
        obj: dict[str, object] = {
            "step_index": int(self.step_index),
            "source": self.source,
            "type": self.step_type_name,
            "status": self.status_name,
            "created_at": self.created_at,
            "content": self.content,
        }
        if self.thinking is not None:
            obj["thinking"] = self.thinking
        if self.tool_calls_json:
            obj["tool_calls"] = list(self.tool_calls_json)
        if self.media_uris:
            obj["media"] = list(self.media_uris)
        if self.input_tokens or self.output_tokens or self.cache_read_tokens:
            obj["input_tokens"] = self.input_tokens
            obj["cache_read_tokens"] = self.cache_read_tokens
            obj["output_tokens"] = self.output_tokens
        if self.truncated_fields:
            obj["truncated_fields"] = list(self.truncated_fields)
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class AnnotationPbtxtProjection:
    """Pure view projection for `~/.gemini/antigravity/annotations/<cascade_id>.pbtxt`.

    Invariant. Title matches `CascadeSummaryProjection.title` so all three
    Antigravity title precedence slots agree.
    """

    cascade_id: CascadeId
    title: str
    last_user_view_time_iso: str

    def render_pbtxt(self) -> str:
        """Serialize `exa.jetski_cortex_pb.ConversationAnnotations` textproto."""
        escaped_title = self.title.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        seconds, nanos = _parse_iso_to_seconds_nanos(self.last_user_view_time_iso)
        if nanos > 0:
            return (
                f'title: "{escaped_title}"\n'
                f"last_user_view_time {{\n"
                f"  seconds: {seconds}\n"
                f"  nanos: {nanos}\n"
                f"}}\n"
            )
        return (
            f'title: "{escaped_title}"\n'
            f"last_user_view_time {{\n"
            f"  seconds: {seconds}\n"
            f"}}\n"
        )


_FORBIDDEN_PROJECT_BUCKETS = frozenset(
    {"", "default-cli-project", "outside-of-project", "null", "none"}
)


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
    summary_proto_bytes: bytes = b""

    def __post_init__(self) -> None:
        if str(self.project_id).strip() in _FORBIDDEN_PROJECT_BUCKETS:
            raise ValueError(f"Forbidden Antigravity project_id {self.project_id!r}.")
        if not self.workspace_uris:
            raise ValueError("CascadeSummaryProjection requires non-empty workspace_uris.")
        if self.nesting_depth != 0:
            raise ValueError("CascadeSummaryProjection requires nesting_depth == 0.")
        if not isinstance(self.source, AgyGuiSafeSource) or self.source.proto_enum_value == 17:
            raise ValueError(f"Illegal CLI trajectory source {self.source!r}.")
        if not self.summary_proto_bytes:
            object.__setattr__(self, "summary_proto_bytes", _encode_cascade_summary_proto(self))


@dataclass(frozen=True)
class SummariesStateProjection:
    """Pure view projection for `~/.gemini/antigravity/agyhub_summaries_proto.pb` (grafted from Candidate 1).

    Corresponds to `jetbox_summaries_pb.SummariesState` (field 1 `summaries`, field 2 `file_mod_times`).
    """

    summaries: Mapping[CascadeId, CascadeSummaryProjection]
    file_mod_times: Mapping[str, int]
    proto_bytes: bytes = b""

    def __post_init__(self) -> None:
        if not self.proto_bytes:
            object.__setattr__(self, "proto_bytes", _encode_summaries_state_proto(self))


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

    def __post_init__(self) -> None:
        if self.archived != 0:
            raise ValueError("CodexThreadSummaryProjection requires archived == 0.")
        if not self.preview or not self.preview.strip():
            raise ValueError("CodexThreadSummaryProjection requires non-empty preview.")
        if not self.project_id or not str(self.project_id).strip():
            raise ValueError("CodexThreadSummaryProjection requires non-empty project_id.")


def _normalize_single_line_query(query_text: str, limit: int = 80) -> str:
    lines = []
    for raw_line in query_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped in ("<USER_REQUEST>", "</USER_REQUEST>"):
            continue
        lines.append(stripped)
    joined = " ".join(" ".join(lines).split())
    return joined[:limit].strip()


def resolve_canonical_title(
    events: Sequence[CanonicalEvent],
    *,
    annotations_pbtxt_title: str | None = None,
    summary_proto_annotation_title: str | None = None,
    summary_db_or_thread_title: str | None = None,
    annotation_pbtxt_title: str | None = None,
    cascade_summary_annotation_title: str | None = None,
    db_summary_row_title: str | None = None,
    explicit_title_override: str | None = None,
) -> str:
    """Resolve session title enforcing `sidebar_visibility_rules.json` `title_resolution_order`.

    Precedence order:
    1. `explicit_title_override` or `annotations_pbtxt_title` (`~/.gemini/antigravity/annotations/<id>.pbtxt`, field 1).
    2. `summary_proto_annotation_title` (`CascadeTrajectorySummary.annotations.title`, field 15 -> 1).
    3. `summary_db_or_thread_title` (`conversation_summaries.title` or Codex `threads.title`).
    4. Latest `CheckpointEvent.conversation_title` in `events` (`CortexStepCheckpoint.conversation_title`, field 10).
    5. First `UserInputEvent.query_text` single-line prefix (up to 80 chars).
    6. Fallback `"Untitled Session"`.
    """
    candidates = (
        explicit_title_override,
        annotations_pbtxt_title,
        annotation_pbtxt_title,
        summary_proto_annotation_title,
        cascade_summary_annotation_title,
        summary_db_or_thread_title,
        db_summary_row_title,
    )
    for candidate in candidates:
        if candidate is not None:
            cleaned = candidate.strip()
            if cleaned:
                return cleaned

    for ev in reversed(events):
        if isinstance(ev, CheckpointEvent) and ev.conversation_title:
            cleaned = ev.conversation_title.strip()
            if cleaned:
                return cleaned

    for ev in events:
        if isinstance(ev, UserInputEvent) and ev.query_text:
            normalized = _normalize_single_line_query(ev.query_text, limit=80)
            if normalized:
                return normalized

    return "Untitled Session"


def _canonical_event_digest_payload(ev: CanonicalEvent) -> Mapping[str, object]:
    if isinstance(ev, UserInputEvent):
        return {
            "kind": ev.kind,
            "turn_id": str(ev.anchor.turn_id),
            "query_text": ev.query_text,
            "scope_items": list(ev.scope_items),
        }
    if isinstance(ev, ThinkingEvent):
        return {
            "kind": ev.kind,
            "turn_id": str(ev.anchor.turn_id),
            "summary_text": ev.summary_text,
        }
    if isinstance(ev, AssistantTextEvent):
        return {
            "kind": ev.kind,
            "turn_id": str(ev.anchor.turn_id),
            "text": ev.text,
        }
    if isinstance(ev, ToolCallEvent):
        return {
            "kind": ev.kind,
            "turn_id": str(ev.anchor.turn_id),
            "call_id": str(ev.call_id),
            "tool_name": ev.tool_name,
            "payload_kind": ev.payload.kind,
        }
    if isinstance(ev, ToolResultEvent):
        return {
            "kind": ev.kind,
            "turn_id": str(ev.anchor.turn_id),
            "call_id": str(ev.call_id),
            "tool_name": ev.tool_name,
            "output_text": ev.output_text,
            "is_error": ev.is_error,
        }
    if isinstance(ev, TaskBoundaryEvent):
        return {
            "kind": ev.kind,
            "turn_id": str(ev.anchor.turn_id),
            "boundary_kind": ev.boundary_kind,
            "task_name": ev.task_name,
            "task_status": ev.task_status,
            "task_summary": ev.task_summary,
        }
    if isinstance(ev, CheckpointEvent):
        return {
            "kind": ev.kind,
            "turn_id": str(ev.anchor.turn_id),
            "checkpoint_index": ev.checkpoint_index,
            "user_intent": ev.user_intent,
            "session_summary": ev.session_summary,
            "conversation_title": ev.conversation_title or "",
        }
    if isinstance(ev, SystemEvent):
        return {
            "kind": ev.kind,
            "turn_id": str(ev.anchor.turn_id),
            "event_type": ev.event_type,
            "message_text": ev.message_text,
        }
    return {"kind": "unknown"}


def _serialize_tool_payload_to_arguments_json(payload: CanonicalToolPayload) -> str:
    if isinstance(payload, OpaqueToolPayload):
        return payload.raw_arguments_json or "{}"
    if isinstance(payload, FinishPayload):
        return payload.arguments_json or "{}"
    data = dataclasses.asdict(payload)
    data.pop("kind", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Indexed Canonical Event Ledger (`TrajectoryLedger`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrajectoryLedger:
    """Append-only canonical event ledger with O(1) secondary lookup indices.

    Dominant access patterns served in O(1) without rescanning `events`.
    1. `by_step_index` maps an Antigravity `StepIndex` to all `LedgerSeqNo` entries
       produced by that step (resolving the 1-to-N fanout of `step_type = 15` into
       `ThinkingEvent`, `AssistantTextEvent`, and N `ToolCallEvent` nodes, or
       `step_type = 132` into its `ToolResultEvent`).
    2. `by_call_id` maps a `ToolCallId` to its `(call_seq_no, result_seq_no | None)`
       pair so `CortexStepGeneric` (`step_type = 132`) results join directly to their
       declaring `ChatToolCall` in `step_type = 15`.
    3. `by_turn_id` groups all `LedgerSeqNo` entries belonging to one logical turn.
    4. `completed_turn_watermarks` holds ordered watermarks of completed turns used by
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
        """Build a `TrajectoryLedger`, resolve canonical title, and populate all 4 indices in one pass."""
        resolved_title = resolve_canonical_title(
            events,
            explicit_title_override=explicit_title_override,
        )

        normalized_events: list[CanonicalEvent] = []
        by_step_index_mut: dict[StepIndex, list[LedgerSeqNo]] = {}
        by_call_id_mut: dict[ToolCallId, tuple[LedgerSeqNo, LedgerSeqNo | None]] = {}
        by_turn_id_mut: dict[TurnId, list[LedgerSeqNo]] = {}

        next_synthetic_step = 0
        next_synthetic_line = 1
        active_planner_step: StepIndex | None = None
        active_planner_turn: TurnId | None = None
        active_planner_has_text = False
        active_planner_has_tools = False

        for idx, ev in enumerate(events):
            seq_no = LedgerSeqNo(idx)
            anchor = ev.anchor

            if anchor.agy_step_index is not None:
                step_idx = anchor.agy_step_index
                next_synthetic_step = max(next_synthetic_step, int(step_idx) + 1)
                active_planner_step = None
            else:
                if isinstance(ev, (ThinkingEvent, AssistantTextEvent, ToolCallEvent)):
                    can_reuse = (
                        active_planner_step is not None
                        and active_planner_turn == anchor.turn_id
                        and (
                            (isinstance(ev, ThinkingEvent) and not active_planner_has_text and not active_planner_has_tools)
                            or (isinstance(ev, AssistantTextEvent) and not active_planner_has_text and not active_planner_has_tools)
                            or isinstance(ev, ToolCallEvent)
                        )
                    )
                    if not can_reuse:
                        active_planner_step = StepIndex(next_synthetic_step)
                        next_synthetic_step += 1
                        active_planner_turn = anchor.turn_id
                        active_planner_has_text = False
                        active_planner_has_tools = False
                    if isinstance(ev, AssistantTextEvent):
                        active_planner_has_text = True
                    elif isinstance(ev, ToolCallEvent):
                        active_planner_has_tools = True
                    step_idx = active_planner_step
                else:
                    active_planner_step = None
                    step_idx = StepIndex(next_synthetic_step)
                    next_synthetic_step += 1

            if anchor.codex_line_no is not None:
                line_no = anchor.codex_line_no
                next_synthetic_line = max(next_synthetic_line, int(line_no) + 1)
            else:
                line_no = RolloutLineNo(next_synthetic_line)
                next_synthetic_line += 1

            if anchor.agy_step_index is None or anchor.codex_line_no is None:
                new_anchor = dataclasses.replace(
                    anchor,
                    agy_step_index=step_idx,
                    codex_line_no=line_no,
                )
                ev = dataclasses.replace(ev, anchor=new_anchor)

            normalized_events.append(ev)
            by_step_index_mut.setdefault(step_idx, []).append(seq_no)
            by_turn_id_mut.setdefault(ev.anchor.turn_id, []).append(seq_no)

            if isinstance(ev, ToolCallEvent):
                by_call_id_mut[ev.call_id] = (seq_no, None)
            elif isinstance(ev, ToolResultEvent):
                existing = by_call_id_mut.get(ev.call_id)
                if existing is not None:
                    by_call_id_mut[ev.call_id] = (existing[0], seq_no)
                else:
                    by_call_id_mut[ev.call_id] = (seq_no, seq_no)

        watermarks: list[CompletedTurnWatermark] = []
        cumulative_hasher = hashlib.sha256()
        has_inflight = False

        turn_items = list(by_turn_id_mut.items())
        for turn_pos, (turn_id, seq_list) in enumerate(turn_items):
            is_last_turn = turn_pos == len(turn_items) - 1
            turn_events = [normalized_events[int(s)] for s in seq_list]

            all_calls_resolved = True
            has_completion_event = False
            has_user_or_task_start = any(
                isinstance(tev, UserInputEvent)
                or (
                    isinstance(tev, TaskBoundaryEvent)
                    and tev.task_status.lower() in ("task_started", "running", "in_progress", "starting")
                )
                for tev in turn_events
            )
            for tev in turn_events:
                if isinstance(tev, ToolCallEvent):
                    call_pair = by_call_id_mut.get(tev.call_id)
                    if call_pair is None or call_pair[1] is None:
                        all_calls_resolved = False
                elif isinstance(tev, (AssistantTextEvent, ToolResultEvent)):
                    has_completion_event = True
                elif isinstance(tev, (CheckpointEvent, SystemEvent)):
                    if not has_user_or_task_start:
                        has_completion_event = True
                elif isinstance(tev, TaskBoundaryEvent):
                    if tev.task_status.lower() not in ("task_started", "running", "in_progress", "starting"):
                        has_completion_event = True

            if is_last_turn and turn_events:
                last_ev = turn_events[-1]
                if isinstance(last_ev, TaskBoundaryEvent) and last_ev.task_status.lower() == "task_started":
                    has_completion_event = False

            turn_completed = all_calls_resolved and has_completion_event
            if not turn_completed:
                has_inflight = True
                break

            max_step_idx = StepIndex(0)
            max_line_no = RolloutLineNo(1)
            for tev in turn_events:
                payload_json = json.dumps(
                    _canonical_event_digest_payload(tev),
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                cumulative_hasher.update(payload_json.encode("utf-8"))
                cumulative_hasher.update(b"\n")
                if tev.anchor.agy_step_index is not None and int(tev.anchor.agy_step_index) > int(max_step_idx):
                    max_step_idx = tev.anchor.agy_step_index
                if tev.anchor.codex_line_no is not None and int(tev.anchor.codex_line_no) > int(max_line_no):
                    max_line_no = tev.anchor.codex_line_no

            watermarks.append(
                CompletedTurnWatermark(
                    turn_id=turn_id,
                    last_ledger_seq=seq_list[-1],
                    last_agy_step_index=max_step_idx,
                    last_codex_line_no=max_line_no,
                    boundary_digest=ContentDigest(cumulative_hasher.hexdigest()),
                )
            )

        return cls(
            workspace_path=workspace_path,
            workspace_uri=workspace_uri,
            title=resolved_title,
            events=tuple(normalized_events),
            by_step_index={k: tuple(v) for k, v in by_step_index_mut.items()},
            by_call_id=dict(by_call_id_mut),
            by_turn_id={k: tuple(v) for k, v in by_turn_id_mut.items()},
            completed_turn_watermarks=tuple(watermarks),
            has_inflight_turn=has_inflight,
        )

    def latest_watermark(self) -> CompletedTurnWatermark | None:
        """Return the watermark of the last completed turn, or `None` if no turn has completed."""
        return self.completed_turn_watermarks[-1] if self.completed_turn_watermarks else None

    def find_watermark(self, turn_id: TurnId) -> CompletedTurnWatermark | None:
        """Look up a specific completed turn's watermark by `TurnId`."""
        for wm in self.completed_turn_watermarks:
            if wm.turn_id == turn_id:
                return wm
        return None

    def events_for_step(self, step_index: StepIndex) -> Tuple[CanonicalEvent, ...]:
        """Return all `CanonicalEvent` nodes sharing `step_index` in O(1) via `by_step_index`."""
        seqs = self.by_step_index.get(step_index, ())
        return tuple(self.events[int(s)] for s in seqs)

    def call_and_result(self, call_id: ToolCallId) -> Tuple[ToolCallEvent, ToolResultEvent | None]:
        """Return `(ToolCallEvent, ToolResultEvent | None)` for `call_id` in O(1) via `by_call_id`."""
        if call_id not in self.by_call_id:
            raise KeyError(f"ToolCallId {call_id!r} not found in ledger.")
        call_seq, res_seq = self.by_call_id[call_id]
        call_ev = self.events[int(call_seq)]
        if not isinstance(call_ev, ToolCallEvent):
            raise ValueError(f"Expected ToolCallEvent at seq {call_seq}, found {type(call_ev).__name__}.")
        res_ev: ToolResultEvent | None = None
        if res_seq is not None and res_seq != call_seq:
            candidate = self.events[int(res_seq)]
            if isinstance(candidate, ToolResultEvent):
                res_ev = candidate
        return (call_ev, res_ev)

    def slice_after_watermark(
        self,
        cursor_turn_id: TurnId | CompletedTurnWatermark | None,
    ) -> Tuple[CanonicalEvent, ...]:
        """Return the ordered events belonging to completed turns strictly after `cursor_turn_id`."""
        if not self.completed_turn_watermarks:
            if cursor_turn_id is not None:
                raise KeyError(f"Turn {cursor_turn_id!r} not found in empty watermarks.")
            return ()

        last_completed_seq = int(self.completed_turn_watermarks[-1].last_ledger_seq)
        if cursor_turn_id is None:
            return self.events[: last_completed_seq + 1]

        target_turn_id = (
            cursor_turn_id.turn_id
            if isinstance(cursor_turn_id, CompletedTurnWatermark)
            else cursor_turn_id
        )
        matched_wm = self.find_watermark(target_turn_id)
        if matched_wm is None:
            raise KeyError(f"TurnId {target_turn_id!r} not found in completed_turn_watermarks.")

        start_seq = int(matched_wm.last_ledger_seq) + 1
        if start_seq > last_completed_seq:
            return ()
        return self.events[start_seq : last_completed_seq + 1]

    def project_transcript_rows(self) -> Tuple[TranscriptRowProjection, ...]:
        """Project `brain/<id>/.system_generated/logs/transcript.jsonl` rows from the ledger."""
        rows: list[TranscriptRowProjection] = []
        for step_idx in sorted(self.by_step_index.keys(), key=int):
            step_events = self.events_for_step(step_idx)
            if not step_events:
                continue
            first = step_events[0]
            created_at = first.anchor.timestamp_iso or "2026-01-01T00:00:00Z"

            if isinstance(first, (ThinkingEvent, AssistantTextEvent, ToolCallEvent)):
                thinking_parts: list[str] = []
                text_parts: list[str] = []
                tool_calls: list[Mapping[str, object]] = []
                in_tokens = 0
                out_tokens = 0
                cache_tokens = 0

                for ev in step_events:
                    if isinstance(ev, ThinkingEvent):
                        if ev.summary_text:
                            thinking_parts.append(ev.summary_text)
                        elif ev.raw_thinking:
                            thinking_parts.append(ev.raw_thinking)
                        in_tokens = max(in_tokens, ev.token_usage.input_tokens)
                        out_tokens = max(out_tokens, ev.token_usage.output_tokens)
                        cache_tokens = max(cache_tokens, ev.token_usage.cache_read_tokens)
                    elif isinstance(ev, AssistantTextEvent):
                        if ev.text:
                            text_parts.append(ev.text)
                        in_tokens = max(in_tokens, ev.token_usage.input_tokens)
                        out_tokens = max(out_tokens, ev.token_usage.output_tokens)
                        cache_tokens = max(cache_tokens, ev.token_usage.cache_read_tokens)
                    elif isinstance(ev, ToolCallEvent):
                        tool_calls.append(
                            {
                                "id": str(ev.call_id),
                                "name": ev.tool_name,
                                "arguments_json": _serialize_tool_payload_to_arguments_json(ev.payload),
                            }
                        )

                rows.append(
                    TranscriptRowProjection(
                        step_index=step_idx,
                        source="MODEL",
                        step_type_name="PLANNER_RESPONSE",
                        status_name="DONE",
                        created_at=created_at,
                        content="\n".join(text_parts),
                        thinking="\n".join(thinking_parts) if thinking_parts else None,
                        tool_calls_json=tuple(tool_calls),
                        media_uris=(),
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        cache_read_tokens=cache_tokens,
                    )
                )
            elif isinstance(first, UserInputEvent):
                media_items = tuple(
                    {"mime_type": m.mime_type, "uri": m.uri} for m in first.media
                )
                rows.append(
                    TranscriptRowProjection(
                        step_index=step_idx,
                        source="USER_EXPLICIT",
                        step_type_name="USER_INPUT",
                        status_name="DONE",
                        created_at=created_at,
                        content=first.query_text,
                        thinking=None,
                        tool_calls_json=(),
                        media_uris=media_items,
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                    )
                )
            elif isinstance(first, ToolResultEvent):
                rows.append(
                    TranscriptRowProjection(
                        step_index=step_idx,
                        source="MODEL",
                        step_type_name="GENERIC",
                        status_name="ERROR" if first.is_error else "DONE",
                        created_at=created_at,
                        content=first.output_text,
                        thinking=None,
                        tool_calls_json=(),
                        media_uris=(),
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                    )
                )
            elif isinstance(first, CheckpointEvent):
                rows.append(
                    TranscriptRowProjection(
                        step_index=step_idx,
                        source="SYSTEM",
                        step_type_name="CHECKPOINT",
                        status_name="DONE",
                        created_at=created_at,
                        content=first.session_summary or first.user_intent,
                        thinking=None,
                        tool_calls_json=(),
                        media_uris=(),
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                    )
                )
            elif isinstance(first, TaskBoundaryEvent):
                step_label = "NOTIFY_USER" if first.boundary_kind == "notify_user" else "TASK_BOUNDARY"
                rows.append(
                    TranscriptRowProjection(
                        step_index=step_idx,
                        source="MODEL",
                        step_type_name=step_label,
                        status_name="DONE",
                        created_at=created_at,
                        content=first.task_summary or first.task_name,
                        thinking=None,
                        tool_calls_json=(),
                        media_uris=(),
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                    )
                )
            elif isinstance(first, SystemEvent):
                rows.append(
                    TranscriptRowProjection(
                        step_index=step_idx,
                        source="SYSTEM",
                        step_type_name="SYSTEM_MESSAGE",
                        status_name="DONE",
                        created_at=created_at,
                        content=first.message_text,
                        thinking=None,
                        tool_calls_json=(),
                        media_uris=(),
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                    )
                )
        return tuple(rows)

    def project_annotation_pbtxt(
        self,
        cascade_id: CascadeId,
        viewed_at_iso: str | None = None,
    ) -> AnnotationPbtxtProjection:
        """Project `~/.gemini/antigravity/annotations/<cascade_id>.pbtxt` from the ledger."""
        ts = viewed_at_iso or (
            self.events[-1].anchor.timestamp_iso if self.events else "2026-01-01T00:00:00Z"
        )
        return AnnotationPbtxtProjection(
            cascade_id=cascade_id,
            title=self.title,
            last_user_view_time_iso=ts,
        )

    def project_agy_annotations(
        self,
        cascade_id: CascadeId,
        viewed_at_iso: str | None = None,
    ) -> AnnotationPbtxtProjection:
        """Alias for `project_annotation_pbtxt`."""
        return self.project_annotation_pbtxt(cascade_id=cascade_id, viewed_at_iso=viewed_at_iso)

    def project_agy_cascade_summary(
        self,
        cascade_id: CascadeId,
        project_id: AgyProjectId,
        viewed_at_iso: str | None = None,
        source: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
    ) -> CascadeSummaryProjection:
        """Project `CascadeTrajectorySummary` with 3-way title and sidebar-visibility invariants enforced."""
        created_iso = self.events[0].anchor.timestamp_iso if self.events else "2026-01-01T00:00:00Z"
        updated_iso = (
            viewed_at_iso
            or (self.events[-1].anchor.timestamp_iso if self.events else created_iso)
        )
        annot = self.project_annotation_pbtxt(cascade_id=cascade_id, viewed_at_iso=updated_iso)
        preview = self._derive_preview_text()
        return CascadeSummaryProjection(
            cascade_id=cascade_id,
            project_id=project_id,
            workspace_uris=(self.workspace_uri,),
            source=source,
            title=self.title,
            preview=preview,
            step_count=len(self.by_step_index),
            created_at_iso=created_iso,
            updated_at_iso=updated_iso,
            annotations=annot,
            nesting_depth=0,
        )

    def project_agy_summary(
        self,
        cascade_id: CascadeId,
        project_id: AgyProjectId,
        viewed_at_iso: str | None = None,
        source: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
    ) -> CascadeSummaryProjection:
        """Alias for `project_agy_cascade_summary`."""
        return self.project_agy_cascade_summary(
            cascade_id=cascade_id,
            project_id=project_id,
            viewed_at_iso=viewed_at_iso,
            source=source,
        )

    def project_agy_hub_summaries_state(
        self,
        existing_state: SummariesStateProjection | None,
        updated_summary: CascadeSummaryProjection,
        conv_db_path: str,
        conv_db_mtime: int,
    ) -> SummariesStateProjection:
        """Project updated `SummariesStateProjection` for `agyhub_summaries_proto.pb` (grafted from Candidate 1)."""
        merged_summaries: dict[CascadeId, CascadeSummaryProjection] = {}
        merged_mtimes: dict[str, int] = {}
        if existing_state is not None:
            merged_summaries.update(existing_state.summaries)
            merged_mtimes.update(existing_state.file_mod_times)
        merged_summaries[updated_summary.cascade_id] = updated_summary
        merged_mtimes[str(conv_db_path)] = int(conv_db_mtime)
        return SummariesStateProjection(
            summaries=merged_summaries,
            file_mod_times=merged_mtimes,
        )

    def project_codex_thread_summary(
        self,
        thread_id: CodexThreadId,
        project_id: CodexProjectId,
    ) -> CodexThreadSummaryProjection:
        """Project Codex `threads` row with non-empty `preview` and `archived=0` invariants."""
        created_iso = self.events[0].anchor.timestamp_iso if self.events else "2026-01-01T00:00:00Z"
        updated_iso = self.events[-1].anchor.timestamp_iso if self.events else created_iso
        c_sec, c_nanos = _parse_iso_to_seconds_nanos(created_iso)
        u_sec, u_nanos = _parse_iso_to_seconds_nanos(updated_iso)
        created_ms = c_sec * 1000 + (c_nanos // 1_000_000)
        updated_ms = max(created_ms, u_sec * 1000 + (u_nanos // 1_000_000))
        preview = self._derive_preview_text()
        return CodexThreadSummaryProjection(
            thread_id=thread_id,
            project_id=project_id,
            cwd=self.workspace_path,
            title=self.title,
            preview=preview,
            created_at_ms=created_ms,
            updated_at_ms=updated_ms,
            archived=0,
        )

    def _derive_preview_text(self) -> str:
        for ev in self.events:
            if isinstance(ev, UserInputEvent) and ev.query_text.strip():
                normalized = _normalize_single_line_query(ev.query_text, limit=160)
                if normalized:
                    return normalized
        for ev in self.events:
            if isinstance(ev, AssistantTextEvent) and ev.text.strip():
                normalized = _normalize_single_line_query(ev.text, limit=160)
                if normalized:
                    return normalized
        return self.title if self.title.strip() else "Synchronized Session"
