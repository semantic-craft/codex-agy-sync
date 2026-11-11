"""Table-driven wire codecs for Antigravity `gemini_coder.Step` and Codex `rollout-*.jsonl`.

Invariants encoded in this module.
1. `STEP_WIRE_TABLE` is the single authoritative mapping between `steps.step_type`
   (`exa.cortex_pb.CortexStepType` enum values 14, 15, 23, 81, 82, 101, 132) and
   `gemini_coder.Step` protobuf oneof field tags (19, 20, 30, 93, 94, 114, 140),
   eliminating scattered `if/elif` chains across encoders and decoders
   (per `principle-model-the-domain`).
2. `TOOL_CODEC_TABLE` is the single authoritative registry mapping all 16
   `exa.cortex.core.tools.*` `google.protobuf.Any` type URLs to their canonical
   `CanonicalToolPayload` domain types and corresponding Codex `function_call` and
   `custom_tool_call` schemas.
3. All wire parsing and serialization happens strictly at the codec boundary
   (per `principle-boundary-discipline`). No protobuf tags or raw JSONL dicts leak
   into `TrajectoryLedger`, while unknown protobuf tags and host extensions are
   captured into `UnknownWireField` and `HostExtensionEnvelope`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import IntEnum
import json
import re
from typing import Any, Literal, Mapping, Sequence, Tuple

from ._wire import (
    UnknownWireField,
    decode_timestamp_proto,
    encode_bytes_field,
    encode_string_field,
    encode_timestamp_proto,
    encode_unknown_field,
    encode_unknown_fields,
    encode_varint,
    encode_varint_field,
    iter_wire_fields,
    parse_iso_to_seconds_nanos,
    parse_proto_fields,
    read_varint,
    signed_int32,
    signed_int64,
)
from .ledger import (
    AgyGuiSafeSource,
    AgyProjectId,
    AnnotationPbtxtProjection,
    AskQuestionPayload,
    AssistantTextEvent,
    CanonicalEvent,
    CanonicalToolPayload,
    CascadeId,
    CascadeSummaryProjection,
    CheckpointEvent,
    DefineSubagentPayload,
    EventOriginAnchor,
    FileEditPayload,
    FinishPayload,
    GenerateImagePayload,
    HostExtensionEnvelope,
    HostKind,
    InvokeSubagentPayload,
    ListResourcesPayload,
    ManageSubagentsPayload,
    ManageTaskPayload,
    MediaAttachment,
    MomaSearchPayload,
    OpaqueToolPayload,
    QuestionItem,
    QuestionOption,
    ReadFilePayload,
    ReadResourcePayload,
    RolloutLineNo,
    RunCommandPayload,
    SchedulePayload,
    SearchWebDocument,
    SearchWebPayload,
    SendMessagePayload,
    StepIndex,
    SummariesStateProjection,
    SystemEvent,
    TaskBoundaryEvent,
    ThinkingEvent,
    TokenUsageAccounting,
    ToolCallEvent,
    ToolCallId,
    ToolResultEvent,
    TrajectoryId,
    TrajectoryLedger,
    TranscriptRowProjection,
    TurnId,
    UserInputEvent,
    WorkspacePath,
    WorkspaceRootUri,
    normalize_tool_file_path,
)

# ---------------------------------------------------------------------------
# Authoritative `CortexStepType` <-> `gemini_coder.Step` Oneof Wire Table
# ---------------------------------------------------------------------------


class CortexStepTypeCode(IntEnum):
    """`exa.cortex_pb.CortexStepType` integer values stored in `steps.step_type`."""

    USER_INPUT = 14
    PLANNER_RESPONSE = 15
    CHECKPOINT = 23
    TASK_BOUNDARY = 81
    NOTIFY_USER = 82
    SYSTEM_MESSAGE = 101
    GENERIC = 132


class GeminiStepWireTag(IntEnum):
    """Protobuf field numbers inside `gemini_coder.Step` (`steps.step_payload`)."""

    USER_INPUT_TAG = 19
    PLANNER_RESPONSE_TAG = 20
    CHECKPOINT_TAG = 30
    TASK_BOUNDARY_TAG = 93
    NOTIFY_USER_TAG = 94
    SYSTEM_MESSAGE_TAG = 114
    GENERIC_TAG = 140


@dataclass(frozen=True)
class StepWireSpec:
    """Declarative descriptor binding `steps.step_type` to `gemini_coder.Step` wire layout."""

    step_type: CortexStepTypeCode
    step_oneof_tag: GeminiStepWireTag
    oneof_field_name: str
    proto_message_full_name: str
    transcript_type_label: str
    codex_record_kind: Literal["message", "response_item_bundle", "turn_context", "event_msg", "session_meta"]
    emits_multiple_canonical_events: bool


STEP_WIRE_TABLE: Mapping[CortexStepTypeCode, StepWireSpec] = {
    CortexStepTypeCode.USER_INPUT: StepWireSpec(
        step_type=CortexStepTypeCode.USER_INPUT,
        step_oneof_tag=GeminiStepWireTag.USER_INPUT_TAG,
        oneof_field_name="user_input",
        proto_message_full_name="exa.cortex_pb.CortexStepUserInput",
        transcript_type_label="USER_INPUT",
        codex_record_kind="message",
        emits_multiple_canonical_events=False,
    ),
    CortexStepTypeCode.PLANNER_RESPONSE: StepWireSpec(
        step_type=CortexStepTypeCode.PLANNER_RESPONSE,
        step_oneof_tag=GeminiStepWireTag.PLANNER_RESPONSE_TAG,
        oneof_field_name="planner_response",
        proto_message_full_name="exa.cortex_pb.CortexStepPlannerResponse",
        transcript_type_label="PLANNER_RESPONSE",
        codex_record_kind="response_item_bundle",
        emits_multiple_canonical_events=True,
    ),
    CortexStepTypeCode.CHECKPOINT: StepWireSpec(
        step_type=CortexStepTypeCode.CHECKPOINT,
        step_oneof_tag=GeminiStepWireTag.CHECKPOINT_TAG,
        oneof_field_name="checkpoint",
        proto_message_full_name="exa.cortex_pb.CortexStepCheckpoint",
        transcript_type_label="CHECKPOINT",
        codex_record_kind="turn_context",
        emits_multiple_canonical_events=False,
    ),
    CortexStepTypeCode.TASK_BOUNDARY: StepWireSpec(
        step_type=CortexStepTypeCode.TASK_BOUNDARY,
        step_oneof_tag=GeminiStepWireTag.TASK_BOUNDARY_TAG,
        oneof_field_name="task_boundary",
        proto_message_full_name="exa.cortex_pb.CortexStepTaskBoundary",
        transcript_type_label="TASK_BOUNDARY",
        codex_record_kind="event_msg",
        emits_multiple_canonical_events=False,
    ),
    CortexStepTypeCode.NOTIFY_USER: StepWireSpec(
        step_type=CortexStepTypeCode.NOTIFY_USER,
        step_oneof_tag=GeminiStepWireTag.NOTIFY_USER_TAG,
        oneof_field_name="notify_user",
        proto_message_full_name="exa.cortex_pb.CortexStepNotifyUser",
        transcript_type_label="NOTIFY_USER",
        codex_record_kind="event_msg",
        emits_multiple_canonical_events=False,
    ),
    CortexStepTypeCode.SYSTEM_MESSAGE: StepWireSpec(
        step_type=CortexStepTypeCode.SYSTEM_MESSAGE,
        step_oneof_tag=GeminiStepWireTag.SYSTEM_MESSAGE_TAG,
        oneof_field_name="system_message",
        proto_message_full_name="exa.cortex_pb.CortexStepSystemMessage",
        transcript_type_label="SYSTEM_MESSAGE",
        codex_record_kind="session_meta",
        emits_multiple_canonical_events=False,
    ),
    CortexStepTypeCode.GENERIC: StepWireSpec(
        step_type=CortexStepTypeCode.GENERIC,
        step_oneof_tag=GeminiStepWireTag.GENERIC_TAG,
        oneof_field_name="generic",
        proto_message_full_name="exa.cortex_pb.CortexStepGeneric",
        transcript_type_label="GENERIC",
        codex_record_kind="response_item_bundle",
        emits_multiple_canonical_events=False,
    ),
}

STEP_TAG_TO_SPEC: Mapping[GeminiStepWireTag, StepWireSpec] = {
    spec.step_oneof_tag: spec for spec in STEP_WIRE_TABLE.values()
}


# ---------------------------------------------------------------------------
# Authoritative `exa.cortex.core.tools.*` <-> Codex Tool Codec Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCodecSpec:
    """Declarative mapping between an Antigravity tool payload and Codex tool call."""

    payload_kind: str
    agy_tool_name: str
    agy_any_type_url: str
    codex_call_kind: Literal["function_call", "custom_tool_call"]
    codex_tool_name: str
    codex_fallback_tool_names: Tuple[str, ...] = ()


TOOL_CODEC_TABLE: Tuple[ToolCodecSpec, ...] = (
    ToolCodecSpec(
        payload_kind="read_file",
        agy_tool_name="read_file",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.ReadFile",
        codex_call_kind="function_call",
        codex_tool_name="view_file",
        codex_fallback_tool_names=("exec_command",),
    ),
    ToolCodecSpec(
        payload_kind="file_edit",
        agy_tool_name="edit_file",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.FileEdit",
        codex_call_kind="custom_tool_call",
        codex_tool_name="apply_patch",
        codex_fallback_tool_names=("write_file",),
    ),
    ToolCodecSpec(
        payload_kind="run_command",
        agy_tool_name="run_command",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.RunCommand",
        codex_call_kind="function_call",
        codex_tool_name="exec_command",
        codex_fallback_tool_names=("custom_tool_call:exec", "shell", "exec"),
    ),
    ToolCodecSpec(
        payload_kind="search_web",
        agy_tool_name="search_web",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.SearchWeb",
        codex_call_kind="function_call",
        codex_tool_name="web_search",
    ),
    ToolCodecSpec(
        payload_kind="invoke_subagent",
        agy_tool_name="invoke_subagent",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.InvokeSubagent",
        codex_call_kind="function_call",
        codex_tool_name="spawn_agent",
    ),
    ToolCodecSpec(
        payload_kind="define_subagent",
        agy_tool_name="define_subagent",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.DefineSubagent",
        codex_call_kind="function_call",
        codex_tool_name="define_subagent",
    ),
    ToolCodecSpec(
        payload_kind="manage_subagents",
        agy_tool_name="manage_subagents",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.ManageSubagents",
        codex_call_kind="function_call",
        codex_tool_name="list_agents",
        codex_fallback_tool_names=("wait_agent", "interrupt_agent"),
    ),
    ToolCodecSpec(
        payload_kind="manage_task",
        agy_tool_name="manage_task",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.ManageTask",
        codex_call_kind="function_call",
        codex_tool_name="wait",
        codex_fallback_tool_names=("send_input",),
    ),
    ToolCodecSpec(
        payload_kind="send_message",
        agy_tool_name="send_message",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.SendMessage",
        codex_call_kind="function_call",
        codex_tool_name="send_message",
    ),
    ToolCodecSpec(
        payload_kind="schedule",
        agy_tool_name="schedule",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.Schedule",
        codex_call_kind="function_call",
        codex_tool_name="schedule",
    ),
    ToolCodecSpec(
        payload_kind="ask_question",
        agy_tool_name="ask_question",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.AskQuestion",
        codex_call_kind="function_call",
        codex_tool_name="ask_question",
    ),
    ToolCodecSpec(
        payload_kind="list_resources",
        agy_tool_name="list_resources",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.ListResources",
        codex_call_kind="function_call",
        codex_tool_name="list_mcp_resources",
    ),
    ToolCodecSpec(
        payload_kind="read_resource",
        agy_tool_name="read_resource",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.ReadResource",
        codex_call_kind="function_call",
        codex_tool_name="read_mcp_resource",
    ),
    ToolCodecSpec(
        payload_kind="generate_image",
        agy_tool_name="generate_image",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.GenerateImage",
        codex_call_kind="function_call",
        codex_tool_name="generate_image",
    ),
    ToolCodecSpec(
        payload_kind="moma_search",
        agy_tool_name="moma_search",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.MomaSearch",
        codex_call_kind="function_call",
        codex_tool_name="moma_search",
    ),
    ToolCodecSpec(
        payload_kind="finish",
        agy_tool_name="finish",
        agy_any_type_url="type.googleapis.com/exa.cortex.core.tools.Finish",
        codex_call_kind="function_call",
        codex_tool_name="finish",
    ),
)

TOOL_SPEC_BY_TYPE_URL: Mapping[str, ToolCodecSpec] = {
    spec.agy_any_type_url: spec for spec in TOOL_CODEC_TABLE
}
TOOL_SPEC_BY_AGY_NAME: Mapping[str, ToolCodecSpec] = {
    spec.agy_tool_name: spec for spec in TOOL_CODEC_TABLE
}
TOOL_SPEC_BY_CODEX_NAME: Mapping[str, ToolCodecSpec] = {
    spec.codex_tool_name: spec for spec in TOOL_CODEC_TABLE
}
TOOL_SPEC_BY_PAYLOAD_KIND: Mapping[str, ToolCodecSpec] = {
    spec.payload_kind: spec for spec in TOOL_CODEC_TABLE
}
_TOOL_NAME_LOOKUP: dict[str, ToolCodecSpec] = {}
for _spec in TOOL_CODEC_TABLE:
    for _fb in _spec.codex_fallback_tool_names:
        _TOOL_NAME_LOOKUP.setdefault(_fb, _spec)
    _TOOL_NAME_LOOKUP[_spec.codex_tool_name] = _spec
    _TOOL_NAME_LOOKUP[_spec.agy_tool_name] = _spec


# ---------------------------------------------------------------------------
# Boundary WireEnvelopes (Private to Codec and Transport Layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgyRawStepRow:
    """Raw row from Antigravity `steps` table or `GetCascadeTrajectorySteps` RPC."""

    idx: StepIndex
    step_type: int
    status: int
    has_subtrajectory: bool
    metadata_blob: bytes
    step_payload_blob: bytes
    step_format: int = 1


@dataclass(frozen=True)
class CodexRawRolloutLine:
    """Raw line from Codex `rollout-*.jsonl` or `thread/read` RPC."""

    line_no: RolloutLineNo
    raw_json: str


def _decode_duration_ms(buf: bytes) -> int:
    seconds = 0
    nanos = 0
    for fn, wt, ival, _ in iter_wire_fields(buf):
        if fn == 1 and wt == 0:
            seconds = signed_int64(ival)
        elif fn == 2 and wt == 0:
            nanos = signed_int32(ival)
    return int(seconds * 1000 + (nanos // 1_000_000))


def _encode_duration_proto(duration_ms: int) -> bytes:
    if duration_ms <= 0:
        return b""
    seconds = duration_ms // 1000
    nanos = (duration_ms % 1000) * 1_000_000
    out = bytearray()
    if seconds:
        out.extend(encode_varint_field(1, seconds))
    if nanos:
        out.extend(encode_varint_field(2, nanos))
    return bytes(out)


def _decode_chat_tool_call(buf: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {"id": "", "name": "", "arguments_json": "{}"}
    for fn, wt, _, raw in iter_wire_fields(buf):
        if fn == 1 and wt == 2:
            out["id"] = raw.decode("utf-8", errors="replace")
        elif fn == 2 and wt == 2:
            out["name"] = raw.decode("utf-8", errors="replace")
        elif fn == 3 and wt == 2:
            out["arguments_json"] = raw.decode("utf-8", errors="replace")
        elif fn == 7 and wt == 2:
            out["thinking_signature"] = raw
    return out


def _encode_chat_tool_call(call_id: str, name: str, arguments_json: str) -> bytes:
    buf = bytearray()
    buf.extend(encode_string_field(1, call_id))
    buf.extend(encode_string_field(2, name))
    buf.extend(encode_string_field(3, arguments_json))
    return bytes(buf)


def _decode_step_metadata(buf: bytes) -> tuple[dict[str, Any], list[UnknownWireField]]:
    known_tags = set(range(1, 40))
    meta: dict[str, Any] = {
        "created_at_iso": "2026-01-01T00:00:00Z",
        "source": 0,
        "tool_call": None,
        "tool_summary": "",
        "generator_model": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        " preserved_fields": [],
    }
    unknowns: list[UnknownWireField] = []
    for fn, wt, ival, raw in iter_wire_fields(buf):
        if fn not in known_tags:
            unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
            continue
        if fn == 1 and wt == 2:
            meta["created_at_iso"] = decode_timestamp_proto(raw)
        elif fn == 3 and wt == 0:
            meta["source"] = ival
        elif fn == 4 and wt == 2:
            meta["tool_call"] = _decode_chat_tool_call(raw)
        elif fn == 9 and wt == 2:
            for ufn, uwt, uival, _ in iter_wire_fields(raw):
                if ufn == 2 and uwt == 0:
                    meta["input_tokens"] = uival
                elif ufn == 3 and uwt == 0:
                    meta["output_tokens"] = uival
                elif ufn == 5 and uwt == 0:
                    meta["cache_read_tokens"] = uival
        elif fn == 11 and wt == 0:
            meta["generator_model"] = str(ival)
        elif fn == 30 and wt == 2:
            meta["tool_summary"] = raw.decode("utf-8", errors="replace")
        else:
            meta[" preserved_fields"].append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
    return meta, unknowns


def _encode_step_metadata(
    created_at_iso: str,
    source_enum: int,
    *,
    tool_call: dict[str, str] | None = None,
    tool_summary: str = "",
    token_usage: TokenUsageAccounting | None = None,
    generator_model: str | None = None,
    extra_fields: Sequence[UnknownWireField] = (),
) -> bytes:
    buf = bytearray()
    ts_bytes = encode_timestamp_proto(created_at_iso)
    if ts_bytes:
        buf.extend(encode_bytes_field(1, ts_bytes))
    if source_enum:
        buf.extend(encode_varint_field(3, source_enum))
    if tool_call is not None:
        tc_bytes = _encode_chat_tool_call(
            tool_call.get("id", ""),
            tool_call.get("name", ""),
            tool_call.get("arguments_json", "{}"),
        )
        buf.extend(encode_bytes_field(4, tc_bytes))
    if token_usage is not None and (
        token_usage.input_tokens or token_usage.output_tokens or token_usage.cache_read_tokens
    ):
        usage_buf = bytearray()
        if token_usage.input_tokens:
            usage_buf.extend(encode_varint_field(2, token_usage.input_tokens))
        if token_usage.output_tokens:
            usage_buf.extend(encode_varint_field(3, token_usage.output_tokens))
        if token_usage.cache_read_tokens:
            usage_buf.extend(encode_varint_field(5, token_usage.cache_read_tokens))
        buf.extend(encode_bytes_field(9, bytes(usage_buf)))
    if generator_model and generator_model.isdigit():
        buf.extend(encode_varint_field(11, int(generator_model)))
    if tool_summary:
        buf.extend(encode_string_field(30, tool_summary))
    for uf in extra_fields:
        buf.extend(encode_unknown_field(uf))
    return bytes(buf)


# ---------------------------------------------------------------------------
# Tool Any-Payload Encoders and Decoders (All 16 `exa.cortex.core.tools.*`)
# ---------------------------------------------------------------------------


def _safe_json_dict(raw_json: str) -> dict[str, Any]:
    if not raw_json or not raw_json.strip():
        return {}
    try:
        parsed = json.loads(raw_json)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {}


def _decode_any_envelope(buf: bytes) -> tuple[str, bytes]:
    type_url = ""
    value_bytes = b""
    for fn, wt, _, raw in iter_wire_fields(buf):
        if fn == 1 and wt == 2:
            type_url = raw.decode("utf-8", errors="replace")
        elif fn == 2 and wt == 2:
            value_bytes = raw
    return type_url, value_bytes


def _encode_any_envelope(type_url: str, value_bytes: bytes) -> bytes:
    return encode_string_field(1, type_url) + encode_bytes_field(2, value_bytes)


def _decode_unified_diff_block(diff_block_bytes: bytes) -> str:
    lines_out: list[str] = []
    for fn, wt, _, raw in iter_wire_fields(diff_block_bytes):
        if fn == 3 and wt == 2:
            for ufn, uwt, _, uraw in iter_wire_fields(raw):
                if ufn == 3 and uwt == 2:
                    l_text = ""
                    l_type = 0
                    for lfn, lwt, lival, lraw in iter_wire_fields(uraw):
                        if lfn == 1 and lwt == 2:
                            l_text = lraw.decode("utf-8", errors="replace")
                        elif lfn == 2 and lwt == 0:
                            l_type = lival
                    prefix = "+" if l_type == 1 else ("-" if l_type == 2 else " ")
                    lines_out.append(f"{prefix}{l_text}")
    return "\n".join(lines_out)


def _encode_unified_diff_block(unified_diff: str) -> bytes:
    if not unified_diff:
        return b""
    ud_buf = bytearray()
    for line in unified_diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            l_type = 1
            l_text = line[1:]
        elif line.startswith("-") and not line.startswith("---"):
            l_type = 2
            l_text = line[1:]
        else:
            l_type = 3
            l_text = line[1:] if line.startswith(" ") else line
        line_msg = encode_string_field(1, l_text) + encode_varint_field(2, l_type)
        ud_buf.extend(encode_bytes_field(3, line_msg))
    return encode_bytes_field(3, bytes(ud_buf))


def _decode_tool_any_bytes(
    spec: ToolCodecSpec,
    args_dict: Mapping[str, Any],
    raw_arguments_json: str,
    any_value: bytes,
) -> CanonicalToolPayload:
    kind = spec.payload_kind
    f_items = iter_wire_fields(any_value) if any_value else []

    if kind == "read_file":
        path = str(args_dict.get("Path") or args_dict.get("path") or "")
        start_line = (
            args_dict.get("StartLine")
            if args_dict.get("StartLine") is not None
            else args_dict.get("start_line")
        )
        end_line = (
            args_dict.get("EndLine")
            if args_dict.get("EndLine") is not None
            else args_dict.get("end_line")
        )
        byte_offset = (
            args_dict.get("ByteOffset")
            if args_dict.get("ByteOffset") is not None
            else args_dict.get("byte_offset")
        )
        file_num_lines: int | None = args_dict.get("file_num_lines")
        file_num_bytes: int | None = args_dict.get("file_num_bytes")
        for fn, wt, ival, raw in f_items:
            if fn == 1 and wt == 0:
                file_num_lines = ival
            elif fn == 2 and wt == 0:
                file_num_bytes = ival
            elif fn == 3 and wt == 2:
                path = raw.decode("utf-8", errors="replace") or path
            elif fn == 4 and wt == 0:
                start_line = signed_int32(ival)
            elif fn == 5 and wt == 0:
                end_line = signed_int32(ival)
        return ReadFilePayload(
            kind="read_file",
            path=normalize_tool_file_path(path),
            start_line=int(start_line) if start_line is not None else None,
            end_line=int(end_line) if end_line is not None else None,
            byte_offset=int(byte_offset) if byte_offset is not None else None,
            file_num_lines=int(file_num_lines) if file_num_lines is not None else None,
            file_num_bytes=int(file_num_bytes) if file_num_bytes is not None else None,
            content_excerpt=args_dict.get("content_excerpt"),
        )

    if kind == "file_edit":
        path = str(args_dict.get("Path") or args_dict.get("path") or "")
        edit_op: Literal["CREATE", "MODIFY", "DELETE"] = "MODIFY"
        prior_edit_op = str(args_dict.get("edit_op") or args_dict.get("EditOp") or "").upper()
        if prior_edit_op in ("CREATE", "MODIFY", "DELETE"):
            edit_op = prior_edit_op  # type: ignore[assignment]
        has_canonical_diff = "unified_diff" in args_dict and args_dict.get("unified_diff") is not None
        unified_diff = str(args_dict.get("unified_diff") or "")
        if not unified_diff and ("Target" in args_dict or "Replacement" in args_dict or "Content" in args_dict):
            if "Content" in args_dict and "Target" not in args_dict:
                edit_op = "CREATE"
                unified_diff = "\n".join(f"+{l}" for l in str(args_dict.get("Content", "")).splitlines())
            else:
                tgt = str(args_dict.get("Target", ""))
                rep = str(args_dict.get("Replacement", ""))
                unified_diff = "\n".join(
                    [f"-{l}" for l in tgt.splitlines()] + [f"+{l}" for l in rep.splitlines()]
                )
        additions = 0
        deletions = 0
        raw_stats = args_dict.get("diff_stats")
        if isinstance(raw_stats, (list, tuple)) and len(raw_stats) == 2:
            additions, deletions = int(raw_stats[0]), int(raw_stats[1])
        is_artifact = bool(args_dict.get("is_artifact", False))
        artifact_summary: str | None = args_dict.get("artifact_summary")

        for fn, wt, ival, raw in f_items:
            if fn == 1 and wt == 0:
                edit_op = "CREATE" if ival == 1 else ("DELETE" if prior_edit_op == "DELETE" else "MODIFY")
            elif fn == 2 and wt == 2:
                path = raw.decode("utf-8", errors="replace") or path
            elif fn == 3 and wt == 2:
                decoded_diff = _decode_unified_diff_block(raw)
                if decoded_diff and not has_canonical_diff:
                    unified_diff = decoded_diff
            elif fn == 4 and wt == 2:
                for sfn, swt, sival, _ in iter_wire_fields(raw):
                    if sfn == 1 and swt == 0:
                        additions = signed_int32(sival)
                    elif sfn == 2 and swt == 0:
                        deletions = signed_int32(sival)
            elif fn == 5 and wt == 2:
                for afn, awt, _, araw in iter_wire_fields(raw):
                    if afn == 2 and awt == 2:
                        artifact_summary = araw.decode("utf-8", errors="replace")
            elif fn == 6 and wt == 0:
                is_artifact = bool(ival)
        if unified_diff:
            header_match = re.search(
                r"^\*\*\* (Add|Update|Delete) File:\s*(.+)$",
                unified_diff,
                flags=re.MULTILINE,
            )
            if header_match:
                if not path:
                    path = header_match.group(2)
                verb = header_match.group(1)
                if verb == "Add":
                    edit_op = "CREATE"
                elif verb == "Delete":
                    edit_op = "DELETE"
                unified_diff = re.sub(
                    r"^(\*\*\* (?:Add|Update|Delete) File:\s*)(.+)$",
                    lambda m: m.group(1) + normalize_tool_file_path(m.group(2)),
                    unified_diff,
                    flags=re.MULTILINE,
                )
        return FileEditPayload(
            kind="file_edit",
            path=normalize_tool_file_path(path),
            edit_op=edit_op,
            unified_diff=unified_diff,
            diff_stats=(additions, deletions),
            is_artifact=is_artifact,
            artifact_summary=artifact_summary,
        )

    if kind == "run_command":
        cmd = str(
            args_dict.get("CommandLine")
            or args_dict.get("command_line")
            or args_dict.get("cmd")
            or args_dict.get("command")
            or ""
        )
        cwd = str(args_dict.get("Cwd") or args_dict.get("cwd") or args_dict.get("workdir") or "")
        exit_code: int | None = args_dict.get("exit_code")
        timed_out = bool(args_dict.get("timed_out", False))
        is_daemon = bool(args_dict.get("IsDaemon") or args_dict.get("is_daemon") or False)
        shell_name: str | None = args_dict.get("shell_name")
        ran_in_sandbox = bool(args_dict.get("ran_in_sandbox", False))
        sandbox_policy: str | None = args_dict.get("sandbox_policy")

        for fn, wt, ival, raw in f_items:
            if fn == 1 and wt == 0:
                exit_code = signed_int32(ival)
            elif fn == 2 and wt == 0:
                timed_out = bool(ival)
            elif fn == 3 and wt == 2:
                for sfn, swt, sival, sraw in iter_wire_fields(raw):
                    if sfn == 1 and swt == 0:
                        ran_in_sandbox = bool(sival)
                    elif sfn == 2 and swt == 2:
                        sandbox_policy = sraw.decode("utf-8", errors="replace")
            elif fn == 5 and wt == 2:
                shell_name = raw.decode("utf-8", errors="replace")
            elif fn == 6 and wt == 2:
                cmd = raw.decode("utf-8", errors="replace") or cmd
            elif fn == 7 and wt == 2:
                cwd = raw.decode("utf-8", errors="replace") or cwd
            elif fn == 8 and wt == 0 and ival:
                exit_code = None
        if exit_code is None and any_value and not any(fn == 8 for fn, _, _, _ in f_items):
            if any(fn in (3, 4, 5, 6, 7) for fn, _, _, _ in f_items):
                exit_code = 0
        return RunCommandPayload(
            kind="run_command",
            command_line=cmd,
            cwd=normalize_tool_file_path(cwd),
            exit_code=int(exit_code) if exit_code is not None else None,
            timed_out=timed_out,
            is_daemon=is_daemon,
            shell_name=shell_name,
            ran_in_sandbox=ran_in_sandbox,
            sandbox_policy=sandbox_policy,
        )

    if kind == "search_web":
        query = str(args_dict.get("query") or "")
        domain = args_dict.get("domain") or args_dict.get("domain_filter")
        summary: str | None = args_dict.get("summary")
        docs: list[SearchWebDocument] = []
        raw_docs = args_dict.get("documents", [])
        if isinstance(raw_docs, list):
            for d in raw_docs:
                if isinstance(d, dict):
                    docs.append(
                        SearchWebDocument(
                            title=str(d.get("title", "")),
                            url=str(d.get("url", "")),
                            snippet=str(d.get("snippet", "")),
                        )
                    )
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                query = raw.decode("utf-8", errors="replace") or query
            elif fn == 2 and wt == 2:
                domain = raw.decode("utf-8", errors="replace")
            elif fn == 3 and wt == 2:
                d_title = ""
                d_url = ""
                d_text = ""
                for dfn, dwt, _, draw in iter_wire_fields(raw):
                    if dfn == 2 and dwt == 2:
                        d_text = draw.decode("utf-8", errors="replace")
                    elif dfn == 3 and dwt == 2:
                        d_url = draw.decode("utf-8", errors="replace")
                    elif dfn == 4 and dwt == 2:
                        d_title = draw.decode("utf-8", errors="replace")
                    elif dfn == 7 and dwt == 2 and not d_text:
                        d_text = draw.decode("utf-8", errors="replace")
                docs.append(SearchWebDocument(title=d_title, url=d_url, snippet=d_text))
            elif fn == 4 and wt == 2:
                summary = raw.decode("utf-8", errors="replace")
        return SearchWebPayload(
            kind="search_web",
            query=query,
            domain_filter=str(domain) if domain else None,
            documents=tuple(docs),
            summary=summary,
        )

    if kind == "invoke_subagent":
        sub_type = str(args_dict.get("TypeName") or args_dict.get("subagent_type") or "")
        role = str(args_dict.get("Role") or args_dict.get("role") or "")
        prompt = str(args_dict.get("Prompt") or args_dict.get("prompt") or "")
        ws_mode: Literal["inherit", "branch", "share"] = "inherit"
        raw_ws = str(args_dict.get("Workspace") or args_dict.get("workspace_mode") or "inherit")
        if raw_ws in ("inherit", "branch", "share"):
            ws_mode = raw_ws  # type: ignore[assignment]
        spawned_cid: str | None = args_dict.get("spawned_conversation_id")
        task_mode = bool(args_dict.get("task_mode", False))

        for fn, wt, ival, raw in f_items:
            if fn == 1 and wt == 2:
                for sfn, swt, _, sraw in iter_wire_fields(raw):
                    if sfn == 1 and swt == 2:
                        sub_type = sraw.decode("utf-8", errors="replace") or sub_type
                    elif sfn == 2 and swt == 2:
                        role = sraw.decode("utf-8", errors="replace") or role
                    elif sfn == 3 and swt == 2:
                        prompt = sraw.decode("utf-8", errors="replace") or prompt
                    elif sfn == 5 and swt == 2:
                        ws_mode = "branch"
                    elif sfn == 10 and swt == 2:
                        ws_mode = "share"
            elif fn == 2 and wt == 2:
                for rfn, rwt, _, rraw in iter_wire_fields(raw):
                    if rfn == 1 and rwt == 2:
                        spawned_cid = rraw.decode("utf-8", errors="replace")
            elif fn == 3 and wt == 0:
                task_mode = bool(ival)
        return InvokeSubagentPayload(
            kind="invoke_subagent",
            subagent_type=sub_type,
            role=role,
            prompt=prompt,
            spawned_conversation_id=spawned_cid,
            workspace_mode=ws_mode,
            task_mode=task_mode,
        )

    if kind == "define_subagent":
        name = str(args_dict.get("Name") or args_dict.get("name") or "")
        tool_names: list[str] = list(args_dict.get("tool_names", []))
        agent_path: str | None = args_dict.get("agent_path")
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                name = raw.decode("utf-8", errors="replace") or name
            elif fn == 2 and wt == 2:
                tool_names.append(raw.decode("utf-8", errors="replace"))
            elif fn == 3 and wt == 2:
                agent_path = raw.decode("utf-8", errors="replace")
        return DefineSubagentPayload(
            kind="define_subagent",
            name=name,
            tool_names=tuple(tool_names),
            agent_path=normalize_tool_file_path(agent_path) if agent_path is not None else None,
        )

    if kind == "manage_subagents":
        raw_action = str(args_dict.get("Action") or args_dict.get("action") or "list")
        cids: list[str] = list(args_dict.get("ConversationIds") or args_dict.get("target_conversation_ids") or [])
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                raw_action = raw.decode("utf-8", errors="replace") or raw_action
            elif fn == 2 and wt == 2:
                cids.append(raw.decode("utf-8", errors="replace"))
        action_norm: Literal["list", "kill", "kill_all"] = "list"
        if raw_action in ("list", "kill", "kill_all"):
            action_norm = raw_action  # type: ignore[assignment]
        return ManageSubagentsPayload(
            kind="manage_subagents",
            action=action_norm,
            target_conversation_ids=tuple(cids),
        )

    if kind == "manage_task":
        raw_action = str(args_dict.get("Action") or args_dict.get("action") or "status")
        task_id: str | None = args_dict.get("TaskId") or args_dict.get("task_id")
        input_text: str | None = args_dict.get("Input") or args_dict.get("input_text")
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                raw_action = raw.decode("utf-8", errors="replace") or raw_action
            elif fn == 2 and wt == 2 and not task_id:
                task_id = raw.decode("utf-8", errors="replace")
            elif fn == 3 and wt == 2:
                input_text = raw.decode("utf-8", errors="replace")
        action_task: Literal["list", "status", "send_input", "kill"] = "status"
        if raw_action in ("list", "status", "send_input", "kill"):
            action_task = raw_action  # type: ignore[assignment]
        return ManageTaskPayload(
            kind="manage_task",
            action=action_task,
            task_id=str(task_id) if task_id else None,
            input_text=str(input_text) if input_text else None,
        )

    if kind == "send_message":
        recipient = str(
            args_dict.get("Recipient")
            or args_dict.get("recipient_id")
            or args_dict.get("recipient")
            or args_dict.get("to")
            or ""
        )
        msg_body = str(
            args_dict.get("Message")
            or args_dict.get("message_body")
            or args_dict.get("message")
            or args_dict.get("prompt")
            or ""
        )
        recipient_name: str | None = args_dict.get("recipient_name") or args_dict.get("reply_to")
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                recipient = raw.decode("utf-8", errors="replace") or recipient
            elif fn == 2 and wt == 2:
                recipient_name = raw.decode("utf-8", errors="replace")
            elif fn == 3 and wt == 2:
                msg_body = raw.decode("utf-8", errors="replace") or msg_body
        return SendMessagePayload(
            kind="send_message",
            recipient_id=recipient,
            message_body=msg_body,
            recipient_name=str(recipient_name) if recipient_name else None,
        )

    if kind == "schedule":
        prompt = str(args_dict.get("Prompt") or args_dict.get("prompt") or "")
        dur = args_dict.get("DurationSeconds") or args_dict.get("duration_seconds")
        cron = args_dict.get("CronExpression") or args_dict.get("cron_expression")
        cond = str(args_dict.get("TimerCondition") or args_dict.get("timer_condition") or "never")
        is_daemon = bool(args_dict.get("IsDaemon") or args_dict.get("is_daemon") or False)
        for fn, wt, _, raw in f_items:
            if fn == 2 and wt == 2 and not prompt:
                prompt = raw.decode("utf-8", errors="replace")
        return SchedulePayload(
            kind="schedule",
            prompt=prompt,
            duration_seconds=int(dur) if dur is not None else None,
            cron_expression=str(cron) if cron else None,
            timer_condition=cond,
            is_daemon=is_daemon,
        )

    if kind == "ask_question":
        questions: list[QuestionItem] = []
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                q_text = ""
                q_opts: list[QuestionOption] = []
                q_multi = False
                q_ans: list[str] = []
                for qfn, qwt, qival, qraw in iter_wire_fields(raw):
                    if qfn == 1 and qwt == 2:
                        q_text = qraw.decode("utf-8", errors="replace")
                    elif qfn == 2 and qwt == 2:
                        opt_id = ""
                        opt_txt = ""
                        for ofn, owt, _, oraw in iter_wire_fields(qraw):
                            if ofn == 1 and owt == 2:
                                opt_id = oraw.decode("utf-8", errors="replace")
                            elif ofn == 2 and owt == 2:
                                opt_txt = oraw.decode("utf-8", errors="replace")
                        q_opts.append(QuestionOption(label=opt_txt or opt_id, description=opt_id))
                    elif qfn == 3 and qwt == 0:
                        q_multi = bool(qival)
                    elif qfn in (4, 5) and qwt == 2:
                        q_ans.append(qraw.decode("utf-8", errors="replace"))
                questions.append(
                    QuestionItem(
                        question=q_text,
                        options=tuple(q_opts),
                        is_multi_select=q_multi,
                        user_answers=tuple(q_ans),
                    )
                )
        if not questions and isinstance(args_dict.get("questions"), list):
            for q in args_dict["questions"]:
                if isinstance(q, dict):
                    raw_opts = q.get("options", [])
                    parsed_opts = [
                        QuestionOption(
                            label=str(o.get("label", o.get("text", ""))),
                            description=str(o.get("description", "")),
                        )
                        if isinstance(o, dict)
                        else QuestionOption(label=str(o))
                        for o in raw_opts
                    ]
                    questions.append(
                        QuestionItem(
                            question=str(q.get("question", "")),
                            options=tuple(parsed_opts),
                            is_multi_select=bool(q.get("is_multi_select", False)),
                            user_answers=tuple(str(a) for a in q.get("user_answers", [])),
                        )
                    )
        return AskQuestionPayload(kind="ask_question", questions=tuple(questions))

    if kind == "list_resources":
        server_name = str(args_dict.get("ServerName") or args_dict.get("server_name") or "")
        uris: list[str] = list(args_dict.get("resource_uris", []))
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                server_name = raw.decode("utf-8", errors="replace") or server_name
            elif fn == 2 and wt == 2:
                for rfn, rwt, _, rraw in iter_wire_fields(raw):
                    if rfn == 1 and rwt == 2:
                        uris.append(rraw.decode("utf-8", errors="replace"))
        return ListResourcesPayload(
            kind="list_resources",
            server_name=server_name,
            resource_uris=tuple(uris),
        )

    if kind == "read_resource":
        server_name = str(args_dict.get("ServerName") or args_dict.get("server_name") or "")
        uri = str(args_dict.get("Uri") or args_dict.get("uri") or "")
        texts: list[str] = list(args_dict.get("contents_text", []))
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                server_name = raw.decode("utf-8", errors="replace") or server_name
            elif fn == 2 and wt == 2:
                uri = raw.decode("utf-8", errors="replace") or uri
            elif fn == 3 and wt == 2:
                for cfn, cwt, _, craw in iter_wire_fields(raw):
                    if cfn == 2 and cwt == 2:
                        for tfn, twt, _, traw in iter_wire_fields(craw):
                            if tfn == 1 and twt == 2:
                                texts.append(traw.decode("utf-8", errors="replace"))
        return ReadResourcePayload(
            kind="read_resource",
            server_name=server_name,
            uri=uri,
            contents_text=tuple(texts),
        )

    if kind == "generate_image":
        prompt = str(args_dict.get("prompt") or "")
        img_paths: list[str] = list(args_dict.get("image_paths", []))
        aspect = str(args_dict.get("aspect_ratio") or "1:1")
        img_uri: str | None = args_dict.get("image_uri")
        model_name: str | None = args_dict.get("model_name")
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                prompt = raw.decode("utf-8", errors="replace") or prompt
            elif fn == 2 and wt == 2:
                img_paths.append(raw.decode("utf-8", errors="replace"))
            elif fn == 3 and wt == 2:
                aspect = raw.decode("utf-8", errors="replace") or aspect
            elif fn == 4 and wt == 2:
                img_uri = raw.decode("utf-8", errors="replace")
            elif fn == 6 and wt == 2:
                model_name = raw.decode("utf-8", errors="replace")
        return GenerateImagePayload(
            kind="generate_image",
            prompt=prompt,
            image_paths=tuple(normalize_tool_file_path(p) for p in img_paths),
            aspect_ratio=aspect,
            image_uri=img_uri,
            model_name=model_name,
        )

    if kind == "moma_search":
        query = str(args_dict.get("query") or "")
        result_count = int(args_dict.get("result_count", 0))
        proto_results = 0
        for fn, wt, _, raw in f_items:
            if fn == 1 and wt == 2:
                query = raw.decode("utf-8", errors="replace") or query
            elif fn == 2 and wt == 2:
                proto_results += 1
        if proto_results:
            result_count = proto_results
        return MomaSearchPayload(
            kind="moma_search",
            query=query,
            result_count=result_count,
        )

    if kind == "finish":
        finish_args = raw_arguments_json or "{}"
        has_schema = bool(args_dict.get("has_result_schema", False))
        for fn, wt, ival, raw in f_items:
            if fn == 1 and wt == 2:
                finish_args = raw.decode("utf-8", errors="replace") or finish_args
            elif fn == 2 and wt == 0:
                has_schema = bool(ival)
        return FinishPayload(
            kind="finish",
            arguments_json=finish_args,
            has_result_schema=has_schema,
        )

    return OpaqueToolPayload(
        kind="opaque",
        tool_name=spec.agy_tool_name,
        raw_arguments_json=raw_arguments_json,
        type_url=spec.agy_any_type_url,
        raw_any_bytes=any_value,
    )


def _encode_tool_payload_to_any(payload: CanonicalToolPayload) -> bytes:
    if isinstance(payload, OpaqueToolPayload):
        if payload.type_url and payload.raw_any_bytes is not None:
            return _encode_any_envelope(payload.type_url, payload.raw_any_bytes)
        return b""

    spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(payload.kind)
    if spec is None:
        return b""

    inner = bytearray()
    if isinstance(payload, ReadFilePayload):
        if payload.file_num_lines:
            inner.extend(encode_varint_field(1, payload.file_num_lines))
        if payload.file_num_bytes:
            inner.extend(encode_varint_field(2, payload.file_num_bytes))
        inner.extend(encode_string_field(3, normalize_tool_file_path(payload.path)))
        if payload.start_line is not None:
            inner.extend(encode_varint_field(4, payload.start_line))
        if payload.end_line is not None:
            inner.extend(encode_varint_field(5, payload.end_line))

    elif isinstance(payload, FileEditPayload):
        kind_code = 1 if payload.edit_op == "CREATE" else 3
        inner.extend(encode_varint_field(1, kind_code))
        inner.extend(encode_string_field(2, normalize_tool_file_path(payload.path)))
        if payload.unified_diff:
            diff_block = _encode_unified_diff_block(payload.unified_diff)
            if diff_block:
                inner.extend(encode_bytes_field(3, diff_block))
        adds, dels = payload.diff_stats
        if adds or dels:
            stats_buf = encode_varint_field(1, adds) + encode_varint_field(2, dels)
            inner.extend(encode_bytes_field(4, stats_buf))
        if payload.artifact_summary:
            art_buf = encode_string_field(2, payload.artifact_summary)
            inner.extend(encode_bytes_field(5, art_buf))
        if payload.is_artifact:
            inner.extend(encode_varint_field(6, 1))

    elif isinstance(payload, RunCommandPayload):
        if payload.exit_code is not None:
            inner.extend(encode_varint_field(1, payload.exit_code))
        else:
            inner.extend(encode_varint_field(8, 1))
        if payload.timed_out:
            inner.extend(encode_varint_field(2, 1))
        if payload.ran_in_sandbox or payload.sandbox_policy:
            sb_buf = bytearray()
            if payload.ran_in_sandbox:
                sb_buf.extend(encode_varint_field(1, 1))
            if payload.sandbox_policy:
                sb_buf.extend(encode_string_field(2, payload.sandbox_policy))
            inner.extend(encode_bytes_field(3, bytes(sb_buf)))
        if payload.shell_name:
            inner.extend(encode_string_field(5, payload.shell_name))
        inner.extend(encode_string_field(6, payload.command_line))
        inner.extend(encode_string_field(7, normalize_tool_file_path(payload.cwd)))

    elif isinstance(payload, SearchWebPayload):
        inner.extend(encode_string_field(1, payload.query))
        if payload.domain_filter:
            inner.extend(encode_string_field(2, payload.domain_filter))
        for doc in payload.documents:
            doc_buf = (
                encode_string_field(2, doc.snippet)
                + encode_string_field(3, doc.url)
                + encode_string_field(4, doc.title)
            )
            inner.extend(encode_bytes_field(3, doc_buf))
        if payload.summary:
            inner.extend(encode_string_field(4, payload.summary))

    elif isinstance(payload, InvokeSubagentPayload):
        sub_buf = (
            encode_string_field(1, payload.subagent_type)
            + encode_string_field(2, payload.role)
            + encode_string_field(3, payload.prompt)
        )
        if payload.workspace_mode == "branch":
            sub_buf += encode_string_field(5, "branch")
        elif payload.workspace_mode == "share":
            sub_buf += encode_string_field(10, "share")
        else:
            sub_buf += encode_varint_field(4, 1)
        inner.extend(encode_bytes_field(1, sub_buf))
        if payload.spawned_conversation_id:
            res_buf = encode_string_field(1, payload.spawned_conversation_id)
            inner.extend(encode_bytes_field(2, res_buf))
        if payload.task_mode:
            inner.extend(encode_varint_field(3, 1))

    elif isinstance(payload, DefineSubagentPayload):
        inner.extend(encode_string_field(1, payload.name))
        for tname in payload.tool_names:
            inner.extend(encode_string_field(2, tname))
        if payload.agent_path:
            inner.extend(encode_string_field(3, normalize_tool_file_path(payload.agent_path)))

    elif isinstance(payload, ManageSubagentsPayload):
        inner.extend(encode_string_field(1, payload.action))
        for cid in payload.target_conversation_ids:
            inner.extend(encode_string_field(2, cid))

    elif isinstance(payload, ManageTaskPayload):
        inner.extend(encode_string_field(1, payload.action))
        if payload.task_id:
            inner.extend(encode_string_field(2, payload.task_id))
        if payload.input_text:
            inner.extend(encode_string_field(3, payload.input_text))

    elif isinstance(payload, SendMessagePayload):
        inner.extend(encode_string_field(1, payload.recipient_id))
        if payload.recipient_name:
            inner.extend(encode_string_field(2, payload.recipient_name))
        inner.extend(encode_string_field(3, payload.message_body))

    elif isinstance(payload, SchedulePayload):
        mode = "cron" if payload.cron_expression else "timer"
        inner.extend(encode_string_field(1, mode))
        inner.extend(encode_string_field(2, payload.prompt))

    elif isinstance(payload, AskQuestionPayload):
        for q in payload.questions:
            q_buf = bytearray()
            q_buf.extend(encode_string_field(1, q.question))
            for opt in q.options:
                opt_buf = encode_string_field(1, opt.description or opt.label) + encode_string_field(2, opt.label)
                q_buf.extend(encode_bytes_field(2, opt_buf))
            if q.is_multi_select:
                q_buf.extend(encode_varint_field(3, 1))
            for ans in q.user_answers:
                q_buf.extend(encode_string_field(4, ans))
            inner.extend(encode_bytes_field(1, bytes(q_buf)))

    elif isinstance(payload, ListResourcesPayload):
        inner.extend(encode_string_field(1, payload.server_name))
        for uri in payload.resource_uris:
            res_buf = encode_string_field(1, uri)
            inner.extend(encode_bytes_field(2, res_buf))

    elif isinstance(payload, ReadResourcePayload):
        inner.extend(encode_string_field(1, payload.server_name))
        inner.extend(encode_string_field(2, payload.uri))
        for txt in payload.contents_text:
            text_data = encode_string_field(1, txt)
            content_item = encode_string_field(1, payload.uri) + encode_bytes_field(2, text_data)
            inner.extend(encode_bytes_field(3, content_item))

    elif isinstance(payload, GenerateImagePayload):
        inner.extend(encode_string_field(1, payload.prompt))
        for p in payload.image_paths:
            inner.extend(encode_string_field(2, normalize_tool_file_path(p)))
        inner.extend(encode_string_field(3, payload.aspect_ratio))
        if payload.image_uri:
            inner.extend(encode_string_field(4, payload.image_uri))
        if payload.model_name:
            inner.extend(encode_string_field(6, payload.model_name))

    elif isinstance(payload, MomaSearchPayload):
        inner.extend(encode_string_field(1, payload.query))
        for idx in range(payload.result_count):
            res_buf = encode_string_field(2, f"result-{idx}")
            inner.extend(encode_bytes_field(2, res_buf))

    elif isinstance(payload, FinishPayload):
        inner.extend(encode_string_field(1, payload.arguments_json))
        if payload.has_result_schema:
            inner.extend(encode_varint_field(2, 1))

    return _encode_any_envelope(spec.agy_any_type_url, bytes(inner))


def _serialize_agy_chat_tool_arguments(payload: CanonicalToolPayload) -> str:
    if isinstance(payload, OpaqueToolPayload):
        return payload.raw_arguments_json or "{}"
    if isinstance(payload, FinishPayload):
        return payload.arguments_json or "{}"
    data = _safe_json_dict(serialize_tool_payload_arguments_json(payload))
    if isinstance(payload, RunCommandPayload):
        data["CommandLine"] = payload.command_line
        data["Cwd"] = normalize_tool_file_path(payload.cwd)
    elif isinstance(payload, FileEditPayload):
        data["Path"] = normalize_tool_file_path(payload.path)
        data["EditOp"] = payload.edit_op
    elif isinstance(payload, ReadFilePayload):
        data["Path"] = normalize_tool_file_path(payload.path)
        if payload.start_line is not None:
            data["StartLine"] = payload.start_line
        if payload.end_line is not None:
            data["EndLine"] = payload.end_line
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _synthesize_codex_patch_input(payload: FileEditPayload) -> str:
    if "*** Begin Patch" in payload.unified_diff:
        return re.sub(
            r"^(\*\*\* (?:Add|Update|Delete) File:\s*)(.+)$",
            lambda m: m.group(1) + normalize_tool_file_path(m.group(2)),
            payload.unified_diff,
            flags=re.MULTILINE,
        )
    norm_path = normalize_tool_file_path(payload.path) or "patch_target"
    verb = (
        "Add"
        if payload.edit_op == "CREATE"
        else ("Delete" if payload.edit_op == "DELETE" else "Update")
    )
    diff_body = payload.unified_diff.rstrip("\r\n")
    if diff_body:
        return f"*** Begin Patch\n*** {verb} File: {norm_path}\n{diff_body}\n*** End Patch"
    return f"*** Begin Patch\n*** {verb} File: {norm_path}\n*** End Patch"


# ---------------------------------------------------------------------------
# Pure Table-Driven Trajectory Codecs
# ---------------------------------------------------------------------------


class TrajectoryWireCodec:
    """Stateless, table-driven bidirectional translator between host wire formats and `TrajectoryLedger`."""

    @staticmethod
    def decode_tool_payload(
        tool_name: str,
        arguments_json: str,
        any_type_url: str | None = None,
        any_value_bytes: bytes | None = None,
    ) -> CanonicalToolPayload:
        """Decode a tool call or result into a typed `CanonicalToolPayload` via `TOOL_CODEC_TABLE`."""
        spec: ToolCodecSpec | None = None
        if any_type_url:
            spec = TOOL_SPEC_BY_TYPE_URL.get(any_type_url)
        if spec is None and tool_name:
            spec = _TOOL_NAME_LOOKUP.get(tool_name)

        args_dict = _safe_json_dict(arguments_json)
        if spec is None:
            return OpaqueToolPayload(
                kind="opaque",
                tool_name=tool_name or "unknown_tool",
                raw_arguments_json=arguments_json or "{}",
                type_url=any_type_url,
                raw_any_bytes=any_value_bytes,
            )

        return _decode_tool_any_bytes(
            spec=spec,
            args_dict=args_dict,
            raw_arguments_json=arguments_json or "{}",
            any_value=any_value_bytes or b"",
        )

    @staticmethod
    def decode_agy_steps_to_ledger(
        workspace_path: WorkspacePath,
        workspace_uri: WorkspaceRootUri,
        raw_steps: Sequence[AgyRawStepRow],
        *,
        explicit_title_override: str | None = None,
    ) -> TrajectoryLedger:
        """Parse raw Antigravity `steps` rows into an indexed `TrajectoryLedger`."""
        events: list[CanonicalEvent] = []
        call_index_by_id: dict[ToolCallId, int] = {}
        unmatched_synthetic_call_ids: list[ToolCallId] = []

        turn_num = 1
        current_turn_id = TurnId(f"turn-{turn_num}")
        turn_has_response = False

        known_step_tags = {1, 4, 5, 6, 31, 56, 69, 133, 147, 148, 155, 158, 159} | {
            int(t) for t in GeminiStepWireTag
        }

        def _sub_unknown_exts(sub_unknowns: Sequence[UnknownWireField]) -> Tuple[HostExtensionEnvelope, ...]:
            if not sub_unknowns:
                return ()
            return (
                HostExtensionEnvelope(
                    host=HostKind.ANTIGRAVITY,
                    extension_type="agy_submessage_unknowns",
                    raw_payload=b"".join(encode_unknown_field(u) for u in sub_unknowns),
                ),
            )

        for row in raw_steps:
            meta_dict, _meta_unknowns = (
                _decode_step_metadata(row.metadata_blob)
                if row.metadata_blob
                else (
                    {
                        "created_at_iso": "2026-01-01T00:00:00Z",
                        "source": 0,
                        "tool_call": None,
                        "tool_summary": "",
                        "generator_model": None,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        " preserved_fields": [],
                    },
                    [],
                )
            )
            step_fields = iter_wire_fields(row.step_payload_blob) if row.step_payload_blob else []
            step_unknowns: list[UnknownWireField] = []

            spec: StepWireSpec | None = None
            if row.step_type in STEP_WIRE_TABLE:
                spec = STEP_WIRE_TABLE[CortexStepTypeCode(row.step_type)]
            oneof_bytes = b""
            for fn, wt, _, raw in step_fields:
                if fn not in known_step_tags:
                    step_unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
                elif spec is not None and fn == int(spec.step_oneof_tag) and wt == 2:
                    oneof_bytes = raw
                elif spec is None and fn in STEP_TAG_TO_SPEC and wt == 2:
                    spec = STEP_TAG_TO_SPEC[GeminiStepWireTag(fn)]
                    oneof_bytes = raw

            if spec is None:
                continue

            created_at_iso = meta_dict["created_at_iso"]
            token_usage = TokenUsageAccounting(
                input_tokens=int(meta_dict["input_tokens"]),
                output_tokens=int(meta_dict["output_tokens"]),
                cache_read_tokens=int(meta_dict["cache_read_tokens"]),
            )

            if spec.step_type == CortexStepTypeCode.USER_INPUT:
                if turn_has_response:
                    turn_num += 1
                    current_turn_id = TurnId(f"turn-{turn_num}")
                    turn_has_response = False

                query_str = ""
                user_resp = ""
                item_texts: list[str] = []
                scope_uris: list[str] = []
                media_list: list[MediaAttachment] = []
                client_msg_id: str | None = None
                known_ui_tags = set(range(1, 20))
                ui_unknowns: list[UnknownWireField] = []

                for fn, wt, _, raw in iter_wire_fields(oneof_bytes):
                    if fn not in known_ui_tags:
                        ui_unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
                    elif fn == 1 and wt == 2:
                        query_str = raw.decode("utf-8", errors="replace")
                    elif fn == 2 and wt == 2:
                        user_resp = raw.decode("utf-8", errors="replace")
                    elif fn == 3 and wt == 2:
                        for ifn, iwt, _, iraw in iter_wire_fields(raw):
                            if ifn == 1 and iwt == 2:
                                item_texts.append(iraw.decode("utf-8", errors="replace"))
                            elif ifn == 2 and iwt == 2:
                                scope_uris.append(iraw.hex())
                    elif fn == 9 and wt == 2:
                        m_mime = ""
                        m_uri = ""
                        for mfn, mwt, _, mraw in iter_wire_fields(raw):
                            if mfn == 1 and mwt == 2:
                                m_mime = mraw.decode("utf-8", errors="replace")
                            elif mfn == 5 and mwt == 2:
                                m_uri = mraw.decode("utf-8", errors="replace")
                        media_list.append(MediaAttachment(mime_type=m_mime, uri=m_uri))
                    elif fn == 19 and wt == 2:
                        client_msg_id = raw.decode("utf-8", errors="replace")

                final_query = user_resp or "\n".join(t for t in item_texts if t) or query_str
                anchor = EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=current_turn_id,
                    timestamp_iso=created_at_iso,
                    agy_step_index=row.idx,
                    client_message_id=client_msg_id,
                    unknown_wire_fields=tuple(step_unknowns),
                    host_extensions=_sub_unknown_exts(ui_unknowns),
                )
                events.append(
                    UserInputEvent(
                        kind="user_input",
                        anchor=anchor,
                        query_text=final_query,
                        scope_items=tuple(scope_uris),
                        media=tuple(media_list),
                    )
                )

            elif spec.step_type == CortexStepTypeCode.PLANNER_RESPONSE:
                turn_has_response = True
                resp_text = ""
                mod_resp = ""
                thinking_text = ""
                raw_thinking: str | None = None
                sig_str = ""
                thinking_sig_bytes: bytes | None = None
                msg_id: str | None = None
                stop_reason: str | None = None
                thinking_dur_ms: int | None = None
                tool_calls_raw: list[dict[str, Any]] = []
                known_pr_tags = set(range(1, 21))
                pr_unknowns: list[UnknownWireField] = []

                for fn, wt, ival, raw in iter_wire_fields(oneof_bytes):
                    if fn not in known_pr_tags:
                        pr_unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
                    elif fn == 1 and wt == 2:
                        resp_text = raw.decode("utf-8", errors="replace")
                    elif fn == 3 and wt == 2:
                        thinking_text = raw.decode("utf-8", errors="replace")
                    elif fn == 4 and wt == 2:
                        sig_str = raw.decode("utf-8", errors="replace")
                    elif fn == 6 and wt == 2:
                        msg_id = raw.decode("utf-8", errors="replace")
                    elif fn == 7 and wt == 2:
                        tool_calls_raw.append(_decode_chat_tool_call(raw))
                    elif fn == 8 and wt == 2:
                        mod_resp = raw.decode("utf-8", errors="replace")
                    elif fn == 11 and wt == 2:
                        thinking_dur_ms = _decode_duration_ms(raw)
                    elif fn == 12 and wt == 0:
                        stop_reason = str(ival)
                    elif fn == 14 and wt == 2:
                        thinking_sig_bytes = raw
                    elif fn == 16 and wt == 2:
                        raw_thinking = raw.decode("utf-8", errors="replace")

                if thinking_sig_bytes is None and sig_str:
                    thinking_sig_bytes = sig_str.encode("utf-8")

                usage_with_dur = dataclasses.replace(token_usage, thinking_duration_ms=thinking_dur_ms)
                base_anchor = EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=current_turn_id,
                    timestamp_iso=created_at_iso,
                    agy_step_index=row.idx,
                    unknown_wire_fields=tuple(step_unknowns),
                    host_extensions=_sub_unknown_exts(pr_unknowns),
                )

                emitted_in_step = False
                if thinking_text or raw_thinking or thinking_sig_bytes:
                    events.append(
                        ThinkingEvent(
                            kind="thinking",
                            anchor=base_anchor,
                            summary_text=thinking_text or (raw_thinking or ""),
                            raw_thinking=raw_thinking,
                            thinking_signature=thinking_sig_bytes,
                            token_usage=usage_with_dur,
                        )
                    )
                    emitted_in_step = True

                final_resp = resp_text or mod_resp
                if final_resp or (not emitted_in_step and not tool_calls_raw):
                    events.append(
                        AssistantTextEvent(
                            kind="assistant_text",
                            anchor=base_anchor,
                            text=final_resp,
                            message_id=msg_id,
                            stop_reason=stop_reason,
                            generator_model=meta_dict["generator_model"],
                            token_usage=usage_with_dur,
                        )
                    )

                for tc in tool_calls_raw:
                    raw_cid_str = str(tc.get("id") or "").strip()
                    if raw_cid_str:
                        cid = ToolCallId(raw_cid_str)
                    else:
                        cid = ToolCallId(f"call-step-{int(row.idx)}-{len(events)}")
                        unmatched_synthetic_call_ids.append(cid)
                    tname = tc.get("name") or "unknown_tool"
                    args_json = tc.get("arguments_json") or "{}"
                    args_obj = _safe_json_dict(args_json)
                    summary_label = str(
                        args_obj.get("toolSummary")
                        or args_obj.get("tool_summary")
                        or meta_dict["tool_summary"]
                        or tname
                    )
                    payload = TrajectoryWireCodec.decode_tool_payload(tname, args_json)
                    call_index_by_id[cid] = len(events)
                    events.append(
                        ToolCallEvent(
                            kind="tool_call",
                            anchor=base_anchor,
                            call_id=cid,
                            tool_name=tname,
                            tool_summary=summary_label,
                            payload=payload,
                        )
                    )

            elif spec.step_type == CortexStepTypeCode.GENERIC:
                turn_has_response = True
                tc_meta = meta_dict["tool_call"] or {}
                raw_meta_cid = str(tc_meta.get("id") or "").strip()
                if raw_meta_cid:
                    cid = ToolCallId(raw_meta_cid)
                elif unmatched_synthetic_call_ids:
                    cid = unmatched_synthetic_call_ids.pop(0)
                else:
                    cid = ToolCallId(f"call-generic-{int(row.idx)}")
                tname = tc_meta.get("name") or "unknown_tool"
                args_json = tc_meta.get("arguments_json") or "{}"
                tool_summary = str(meta_dict["tool_summary"] or tname)

                args_map: dict[str, str] = {}
                result_text = ""
                any_url: str | None = None
                any_val: bytes | None = None
                full_uri: str | None = None
                known_gen_tags = {1, 2}
                gen_unknowns: list[UnknownWireField] = []

                for fn, wt, _, raw in iter_wire_fields(oneof_bytes):
                    if fn not in known_gen_tags:
                        gen_unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
                    elif fn == 1 and wt == 2:
                        k_str = ""
                        v_str = ""
                        for afn, awt, _, araw in iter_wire_fields(raw):
                            if afn == 1 and awt == 2:
                                k_str = araw.decode("utf-8", errors="replace")
                            elif afn == 2 and awt == 2:
                                v_str = araw.decode("utf-8", errors="replace")
                        if k_str:
                            args_map[k_str] = v_str
                    elif fn == 2 and wt == 2:
                        for rfn, rwt, _, rraw in iter_wire_fields(raw):
                            if rfn == 1 and rwt == 2:
                                result_text = rraw.decode("utf-8", errors="replace")
                            elif rfn == 6 and rwt == 2:
                                any_url, any_val = _decode_any_envelope(rraw)
                            elif rfn == 7 and rwt == 2:
                                full_uri = rraw.decode("utf-8", errors="replace")

                if (not args_json or args_json == "{}") and args_map:
                    args_json = json.dumps(args_map, ensure_ascii=False, sort_keys=True)

                rich_payload = TrajectoryWireCodec.decode_tool_payload(
                    tool_name=tname,
                    arguments_json=args_json,
                    any_type_url=any_url,
                    any_value_bytes=any_val,
                )

                if cid in call_index_by_id:
                    call_pos = call_index_by_id[cid]
                    prior_call = events[call_pos]
                    if isinstance(prior_call, ToolCallEvent):
                        upgraded_summary = (
                            tool_summary
                            if tool_summary and tool_summary != tname
                            else prior_call.tool_summary
                        )
                        events[call_pos] = dataclasses.replace(
                            prior_call,
                            tool_summary=upgraded_summary,
                            payload=rich_payload,
                        )

                anchor = EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=current_turn_id,
                    timestamp_iso=created_at_iso,
                    agy_step_index=row.idx,
                    unknown_wire_fields=tuple(step_unknowns),
                    host_extensions=_sub_unknown_exts(gen_unknowns),
                )
                events.append(
                    ToolResultEvent(
                        kind="tool_result",
                        anchor=anchor,
                        call_id=cid,
                        tool_name=tname,
                        output_text=result_text,
                        is_error=(row.status == 7),
                        payload=rich_payload,
                        full_output_uri=full_uri,
                    )
                )

            elif spec.step_type == CortexStepTypeCode.CHECKPOINT:
                cp_idx = 0
                u_intent = ""
                s_summary = ""
                c_summary = ""
                conv_title: str | None = None
                known_cp_tags = set(range(1, 32))
                cp_unknowns: list[UnknownWireField] = []

                for fn, wt, ival, raw in iter_wire_fields(oneof_bytes):
                    if fn not in known_cp_tags:
                        cp_unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
                    elif fn == 1 and wt == 0:
                        cp_idx = ival
                    elif fn == 4 and wt == 2:
                        u_intent = raw.decode("utf-8", errors="replace")
                    elif fn == 5 and wt == 2:
                        s_summary = raw.decode("utf-8", errors="replace")
                    elif fn == 6 and wt == 2:
                        c_summary = raw.decode("utf-8", errors="replace")
                    elif fn == 10 and wt == 2:
                        conv_title = raw.decode("utf-8", errors="replace")

                anchor = EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=current_turn_id,
                    timestamp_iso=created_at_iso,
                    agy_step_index=row.idx,
                    unknown_wire_fields=tuple(step_unknowns),
                    host_extensions=_sub_unknown_exts(cp_unknowns),
                )
                events.append(
                    CheckpointEvent(
                        kind="checkpoint",
                        anchor=anchor,
                        checkpoint_index=cp_idx,
                        user_intent=u_intent,
                        session_summary=s_summary,
                        code_change_summary=c_summary,
                        conversation_title=conv_title,
                    )
                )

            elif spec.step_type == CortexStepTypeCode.TASK_BOUNDARY:
                t_name = ""
                t_status = ""
                t_summary = ""
                t_mode: str | None = None
                known_tb_tags = set(range(1, 8))
                tb_unknowns: list[UnknownWireField] = []

                for fn, wt, ival, raw in iter_wire_fields(oneof_bytes):
                    if fn not in known_tb_tags:
                        tb_unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
                    elif fn == 1 and wt == 2:
                        t_name = raw.decode("utf-8", errors="replace")
                    elif fn == 2 and wt == 2:
                        t_status = raw.decode("utf-8", errors="replace")
                    elif fn == 3 and wt == 2:
                        t_summary = raw.decode("utf-8", errors="replace")
                    elif fn == 5 and wt == 0:
                        t_mode = str(ival)

                if t_status.lower() in ("task_started", "running", "in_progress", "starting"):
                    if turn_has_response:
                        turn_num += 1
                        current_turn_id = TurnId(f"turn-{turn_num}")
                        turn_has_response = False
                else:
                    turn_has_response = True

                anchor = EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=current_turn_id,
                    timestamp_iso=created_at_iso,
                    agy_step_index=row.idx,
                    unknown_wire_fields=tuple(step_unknowns),
                    host_extensions=_sub_unknown_exts(tb_unknowns),
                )
                events.append(
                    TaskBoundaryEvent(
                        kind="task_boundary",
                        anchor=anchor,
                        boundary_kind="task_boundary",
                        task_name=t_name,
                        task_status=t_status or "DONE",
                        task_summary=t_summary,
                        mode=t_mode,
                    )
                )

            elif spec.step_type == CortexStepTypeCode.NOTIFY_USER:
                turn_has_response = True
                review_uris: list[str] = []
                notif_content = ""
                is_blocking = False
                known_nu_tags = set(range(1, 9))
                nu_unknowns: list[UnknownWireField] = []

                for fn, wt, ival, raw in iter_wire_fields(oneof_bytes):
                    if fn not in known_nu_tags:
                        nu_unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
                    elif fn == 1 and wt == 2:
                        review_uris.append(raw.decode("utf-8", errors="replace"))
                    elif fn == 2 and wt == 2:
                        notif_content = raw.decode("utf-8", errors="replace")
                    elif fn == 3 and wt == 0:
                        is_blocking = bool(ival)

                anchor = EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=current_turn_id,
                    timestamp_iso=created_at_iso,
                    agy_step_index=row.idx,
                    unknown_wire_fields=tuple(step_unknowns),
                    host_extensions=_sub_unknown_exts(nu_unknowns),
                )
                events.append(
                    TaskBoundaryEvent(
                        kind="task_boundary",
                        anchor=anchor,
                        boundary_kind="notify_user",
                        task_name="notify_user",
                        task_status="DONE",
                        task_summary=notif_content,
                        blocked_on_user=is_blocking,
                        review_uris=tuple(review_uris),
                    )
                )

            elif spec.step_type == CortexStepTypeCode.SYSTEM_MESSAGE:
                msg_text = ""
                ev_type = ""
                known_sm_tags = set(range(1, 7))
                sm_unknowns: list[UnknownWireField] = []

                for fn, wt, _, raw in iter_wire_fields(oneof_bytes):
                    if fn not in known_sm_tags:
                        sm_unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
                    elif fn == 1 and wt == 2:
                        msg_text = raw.decode("utf-8", errors="replace")
                    elif fn == 3 and wt == 2:
                        ev_type = raw.decode("utf-8", errors="replace")

                anchor = EventOriginAnchor(
                    origin_host=HostKind.ANTIGRAVITY,
                    turn_id=current_turn_id,
                    timestamp_iso=created_at_iso,
                    agy_step_index=row.idx,
                    unknown_wire_fields=tuple(step_unknowns),
                    host_extensions=_sub_unknown_exts(sm_unknowns),
                )
                events.append(
                    SystemEvent(
                        kind="system_message",
                        anchor=anchor,
                        event_type=ev_type or "system_message",
                        message_text=msg_text,
                    )
                )

        return TrajectoryLedger.from_events(
            workspace_path=workspace_path,
            workspace_uri=workspace_uri,
            events=events,
            explicit_title_override=explicit_title_override,
        )

    @staticmethod
    def encode_events_to_agy_steps(
        events: Sequence[CanonicalEvent],
        starting_step_idx: StepIndex = StepIndex(0),
    ) -> Tuple[AgyRawStepRow, ...]:
        """Serialize a slice of `CanonicalEvent` nodes into Antigravity `steps` rows."""
        rows: list[AgyRawStepRow] = []
        next_idx = int(starting_step_idx)
        pos = 0
        n = len(events)

        while pos < n:
            ev = events[pos]

            if isinstance(ev, (ThinkingEvent, AssistantTextEvent, ToolCallEvent)):
                group_turn = ev.anchor.turn_id
                group_step_idx = ev.anchor.agy_step_index if ev.anchor.origin_host == HostKind.ANTIGRAVITY else None
                thinking_ev: ThinkingEvent | None = None
                text_ev: AssistantTextEvent | None = None
                tool_evs: list[ToolCallEvent] = []
                unknowns: list[UnknownWireField] = []
                seen_unknown_keys: set[tuple[int, int, bytes]] = set()

                while pos < n:
                    cand = events[pos]
                    if not isinstance(cand, (ThinkingEvent, AssistantTextEvent, ToolCallEvent)):
                        break
                    if cand.anchor.turn_id != group_turn:
                        break
                    if group_step_idx is not None and cand.anchor.origin_host == HostKind.ANTIGRAVITY:
                        if cand.anchor.agy_step_index != group_step_idx:
                            break
                    else:
                        if isinstance(cand, ThinkingEvent) and (
                            thinking_ev is not None or text_ev is not None or tool_evs
                        ):
                            break
                        if isinstance(cand, AssistantTextEvent) and (text_ev is not None or tool_evs):
                            break

                    if isinstance(cand, ThinkingEvent):
                        thinking_ev = cand
                    elif isinstance(cand, AssistantTextEvent):
                        text_ev = cand
                    elif isinstance(cand, ToolCallEvent):
                        tool_evs.append(cand)

                    for uf in cand.anchor.unknown_wire_fields:
                        ukey = (uf.field_tag, uf.wire_type, uf.raw_bytes)
                        if ukey not in seen_unknown_keys:
                            seen_unknown_keys.add(ukey)
                            unknowns.append(uf)
                    pos += 1

                first_ev = thinking_ev or text_ev or tool_evs[0]
                created_iso = first_ev.anchor.timestamp_iso
                token_usage = (
                    text_ev.token_usage
                    if text_ev is not None
                    else (thinking_ev.token_usage if thinking_ev is not None else TokenUsageAccounting())
                )
                gen_model = text_ev.generator_model if text_ev is not None else None

                pr_buf = bytearray()
                if text_ev is not None and text_ev.text:
                    pr_buf.extend(encode_string_field(1, text_ev.text))
                if thinking_ev is not None and thinking_ev.summary_text:
                    pr_buf.extend(encode_string_field(3, thinking_ev.summary_text))
                if text_ev is not None and text_ev.message_id:
                    pr_buf.extend(encode_string_field(6, text_ev.message_id))
                for tc_ev in tool_evs:
                    spec_for_call = TOOL_SPEC_BY_PAYLOAD_KIND.get(tc_ev.payload.kind)
                    agy_tname = spec_for_call.agy_tool_name if spec_for_call else tc_ev.tool_name
                    args_json = _serialize_agy_chat_tool_arguments(tc_ev.payload)
                    pr_buf.extend(
                        encode_bytes_field(
                            7,
                            _encode_chat_tool_call(str(tc_ev.call_id), agy_tname, args_json),
                        )
                    )
                if token_usage.thinking_duration_ms:
                    dur_bytes = _encode_duration_proto(token_usage.thinking_duration_ms)
                    if dur_bytes:
                        pr_buf.extend(encode_bytes_field(11, dur_bytes))
                if text_ev is not None and text_ev.stop_reason and text_ev.stop_reason.isdigit():
                    pr_buf.extend(encode_varint_field(12, int(text_ev.stop_reason)))
                if thinking_ev is not None and thinking_ev.thinking_signature:
                    pr_buf.extend(encode_bytes_field(14, thinking_ev.thinking_signature))
                if thinking_ev is not None and thinking_ev.raw_thinking:
                    pr_buf.extend(encode_string_field(16, thinking_ev.raw_thinking))
                for ext in first_ev.anchor.host_extensions:
                    if ext.extension_type == "agy_submessage_unknowns" and ext.raw_payload:
                        pr_buf.extend(ext.raw_payload)

                meta_blob = _encode_step_metadata(
                    created_at_iso=created_iso,
                    source_enum=2,
                    token_usage=token_usage,
                    generator_model=gen_model,
                )
                step_buf = bytearray()
                step_buf.extend(encode_varint_field(1, int(CortexStepTypeCode.PLANNER_RESPONSE)))
                step_buf.extend(encode_varint_field(4, 3))
                step_buf.extend(encode_bytes_field(5, meta_blob))
                step_buf.extend(encode_bytes_field(int(GeminiStepWireTag.PLANNER_RESPONSE_TAG), bytes(pr_buf)))
                for uf in unknowns:
                    step_buf.extend(encode_unknown_field(uf))

                rows.append(
                    AgyRawStepRow(
                        idx=StepIndex(next_idx),
                        step_type=int(CortexStepTypeCode.PLANNER_RESPONSE),
                        status=3,
                        has_subtrajectory=False,
                        metadata_blob=meta_blob,
                        step_payload_blob=bytes(step_buf),
                    )
                )
                next_idx += 1
                continue

            if isinstance(ev, UserInputEvent):
                ui_buf = bytearray()
                ui_buf.extend(encode_string_field(1, ev.query_text))
                ui_buf.extend(encode_string_field(2, ev.query_text))
                item_msg = encode_string_field(1, ev.query_text)
                ui_buf.extend(encode_bytes_field(3, item_msg))
                for m in ev.media:
                    m_msg = encode_string_field(1, m.mime_type) + encode_string_field(5, m.uri)
                    ui_buf.extend(encode_bytes_field(9, m_msg))
                if ev.anchor.client_message_id:
                    ui_buf.extend(encode_string_field(19, ev.anchor.client_message_id))
                for ext in ev.anchor.host_extensions:
                    if ext.extension_type == "agy_submessage_unknowns" and ext.raw_payload:
                        ui_buf.extend(ext.raw_payload)

                meta_blob = _encode_step_metadata(
                    created_at_iso=ev.anchor.timestamp_iso,
                    source_enum=4,
                )
                step_buf = bytearray()
                step_buf.extend(encode_varint_field(1, int(CortexStepTypeCode.USER_INPUT)))
                step_buf.extend(encode_varint_field(4, 3))
                step_buf.extend(encode_bytes_field(5, meta_blob))
                step_buf.extend(encode_bytes_field(int(GeminiStepWireTag.USER_INPUT_TAG), bytes(ui_buf)))
                for uf in ev.anchor.unknown_wire_fields:
                    step_buf.extend(encode_unknown_field(uf))

                rows.append(
                    AgyRawStepRow(
                        idx=StepIndex(next_idx),
                        step_type=int(CortexStepTypeCode.USER_INPUT),
                        status=3,
                        has_subtrajectory=False,
                        metadata_blob=meta_blob,
                        step_payload_blob=bytes(step_buf),
                    )
                )
                next_idx += 1
                pos += 1
                continue

            if isinstance(ev, ToolResultEvent):
                spec_for_res = TOOL_SPEC_BY_PAYLOAD_KIND.get(ev.payload.kind)
                agy_tname = spec_for_res.agy_tool_name if spec_for_res else ev.tool_name
                args_json = _serialize_agy_chat_tool_arguments(ev.payload)
                args_dict = _safe_json_dict(args_json)

                gen_buf = bytearray()
                for k in sorted(args_dict.keys()):
                    val_str = (
                        args_dict[k]
                        if isinstance(args_dict[k], str)
                        else json.dumps(args_dict[k], ensure_ascii=False)
                    )
                    entry = encode_string_field(1, str(k)) + encode_string_field(2, str(val_str))
                    gen_buf.extend(encode_bytes_field(1, entry))

                res_buf = bytearray()
                res_buf.extend(encode_string_field(1, ev.output_text))
                any_bytes = _encode_tool_payload_to_any(ev.payload)
                if any_bytes:
                    res_buf.extend(encode_bytes_field(6, any_bytes))
                if ev.full_output_uri:
                    res_buf.extend(encode_string_field(7, ev.full_output_uri))
                gen_buf.extend(encode_bytes_field(2, bytes(res_buf)))
                for ext in ev.anchor.host_extensions:
                    if ext.extension_type == "agy_submessage_unknowns" and ext.raw_payload:
                        gen_buf.extend(ext.raw_payload)

                meta_blob = _encode_step_metadata(
                    created_at_iso=ev.anchor.timestamp_iso,
                    source_enum=2,
                    tool_call={
                        "id": str(ev.call_id),
                        "name": agy_tname,
                        "arguments_json": args_json,
                    },
                    tool_summary=agy_tname,
                )
                status_code = 7 if ev.is_error else 3
                step_buf = bytearray()
                step_buf.extend(encode_varint_field(1, int(CortexStepTypeCode.GENERIC)))
                step_buf.extend(encode_varint_field(4, status_code))
                step_buf.extend(encode_bytes_field(5, meta_blob))
                step_buf.extend(encode_bytes_field(int(GeminiStepWireTag.GENERIC_TAG), bytes(gen_buf)))
                for uf in ev.anchor.unknown_wire_fields:
                    step_buf.extend(encode_unknown_field(uf))

                rows.append(
                    AgyRawStepRow(
                        idx=StepIndex(next_idx),
                        step_type=int(CortexStepTypeCode.GENERIC),
                        status=status_code,
                        has_subtrajectory=False,
                        metadata_blob=meta_blob,
                        step_payload_blob=bytes(step_buf),
                    )
                )
                next_idx += 1
                pos += 1
                continue

            if isinstance(ev, CheckpointEvent):
                cp_buf = bytearray()
                if ev.checkpoint_index:
                    cp_buf.extend(encode_varint_field(1, ev.checkpoint_index))
                cp_buf.extend(encode_string_field(4, ev.user_intent))
                cp_buf.extend(encode_string_field(5, ev.session_summary))
                cp_buf.extend(encode_string_field(6, ev.code_change_summary))
                if ev.conversation_title:
                    cp_buf.extend(encode_string_field(10, ev.conversation_title))
                for ext in ev.anchor.host_extensions:
                    if ext.extension_type == "agy_submessage_unknowns" and ext.raw_payload:
                        cp_buf.extend(ext.raw_payload)

                meta_blob = _encode_step_metadata(
                    created_at_iso=ev.anchor.timestamp_iso,
                    source_enum=5,
                )
                step_buf = bytearray()
                step_buf.extend(encode_varint_field(1, int(CortexStepTypeCode.CHECKPOINT)))
                step_buf.extend(encode_varint_field(4, 3))
                step_buf.extend(encode_bytes_field(5, meta_blob))
                step_buf.extend(encode_bytes_field(int(GeminiStepWireTag.CHECKPOINT_TAG), bytes(cp_buf)))
                for uf in ev.anchor.unknown_wire_fields:
                    step_buf.extend(encode_unknown_field(uf))

                rows.append(
                    AgyRawStepRow(
                        idx=StepIndex(next_idx),
                        step_type=int(CortexStepTypeCode.CHECKPOINT),
                        status=3,
                        has_subtrajectory=False,
                        metadata_blob=meta_blob,
                        step_payload_blob=bytes(step_buf),
                    )
                )
                next_idx += 1
                pos += 1
                continue

            if isinstance(ev, TaskBoundaryEvent):
                meta_blob = _encode_step_metadata(
                    created_at_iso=ev.anchor.timestamp_iso,
                    source_enum=2,
                )
                step_buf = bytearray()
                if ev.boundary_kind == "notify_user":
                    nu_buf = bytearray()
                    for ruri in ev.review_uris:
                        nu_buf.extend(encode_string_field(1, ruri))
                    nu_buf.extend(encode_string_field(2, ev.task_summary))
                    if ev.blocked_on_user:
                        nu_buf.extend(encode_varint_field(3, 1))
                    for ext in ev.anchor.host_extensions:
                        if ext.extension_type == "agy_submessage_unknowns" and ext.raw_payload:
                            nu_buf.extend(ext.raw_payload)
                    step_type_int = int(CortexStepTypeCode.NOTIFY_USER)
                    step_buf.extend(encode_varint_field(1, step_type_int))
                    step_buf.extend(encode_varint_field(4, 3))
                    step_buf.extend(encode_bytes_field(5, meta_blob))
                    step_buf.extend(encode_bytes_field(int(GeminiStepWireTag.NOTIFY_USER_TAG), bytes(nu_buf)))
                else:
                    tb_buf = bytearray()
                    tb_buf.extend(encode_string_field(1, ev.task_name))
                    tb_buf.extend(encode_string_field(2, ev.task_status))
                    tb_buf.extend(encode_string_field(3, ev.task_summary))
                    if ev.mode and ev.mode.isdigit():
                        tb_buf.extend(encode_varint_field(5, int(ev.mode)))
                    for ext in ev.anchor.host_extensions:
                        if ext.extension_type == "agy_submessage_unknowns" and ext.raw_payload:
                            tb_buf.extend(ext.raw_payload)
                    step_type_int = int(CortexStepTypeCode.TASK_BOUNDARY)
                    step_buf.extend(encode_varint_field(1, step_type_int))
                    step_buf.extend(encode_varint_field(4, 3))
                    step_buf.extend(encode_bytes_field(5, meta_blob))
                    step_buf.extend(encode_bytes_field(int(GeminiStepWireTag.TASK_BOUNDARY_TAG), bytes(tb_buf)))
                for uf in ev.anchor.unknown_wire_fields:
                    step_buf.extend(encode_unknown_field(uf))

                rows.append(
                    AgyRawStepRow(
                        idx=StepIndex(next_idx),
                        step_type=step_type_int,
                        status=3,
                        has_subtrajectory=False,
                        metadata_blob=meta_blob,
                        step_payload_blob=bytes(step_buf),
                    )
                )
                next_idx += 1
                pos += 1
                continue

            if isinstance(ev, SystemEvent):
                sm_buf = bytearray(encode_string_field(1, ev.message_text) + encode_string_field(3, ev.event_type))
                for ext in ev.anchor.host_extensions:
                    if ext.extension_type == "agy_submessage_unknowns" and ext.raw_payload:
                        sm_buf.extend(ext.raw_payload)
                meta_blob = _encode_step_metadata(
                    created_at_iso=ev.anchor.timestamp_iso,
                    source_enum=5,
                )
                step_buf = bytearray()
                step_buf.extend(encode_varint_field(1, int(CortexStepTypeCode.SYSTEM_MESSAGE)))
                step_buf.extend(encode_varint_field(4, 3))
                step_buf.extend(encode_bytes_field(5, meta_blob))
                step_buf.extend(encode_bytes_field(int(GeminiStepWireTag.SYSTEM_MESSAGE_TAG), bytes(sm_buf)))
                for uf in ev.anchor.unknown_wire_fields:
                    step_buf.extend(encode_unknown_field(uf))

                rows.append(
                    AgyRawStepRow(
                        idx=StepIndex(next_idx),
                        step_type=int(CortexStepTypeCode.SYSTEM_MESSAGE),
                        status=3,
                        has_subtrajectory=False,
                        metadata_blob=meta_blob,
                        step_payload_blob=bytes(step_buf),
                    )
                )
                next_idx += 1
                pos += 1
                continue

            pos += 1

        return tuple(rows)

    @staticmethod
    def encode_ledger_to_agy_steps(
        ledger_or_events: TrajectoryLedger | Sequence[CanonicalEvent],
        starting_step_idx: StepIndex = StepIndex(0),
    ) -> Tuple[AgyRawStepRow, ...]:
        """Encode a `TrajectoryLedger` or event sequence into Antigravity `steps` rows."""
        events = (
            ledger_or_events.events
            if isinstance(ledger_or_events, TrajectoryLedger)
            else ledger_or_events
        )
        return TrajectoryWireCodec.encode_events_to_agy_steps(events, starting_step_idx)

    @staticmethod
    def decode_codex_rollout_to_ledger(
        workspace_path: WorkspacePath,
        workspace_uri: WorkspaceRootUri,
        raw_lines: Sequence[CodexRawRolloutLine],
        *,
        explicit_title_override: str | None = None,
    ) -> TrajectoryLedger:
        """Parse Codex `rollout-*.jsonl` lines into an indexed `TrajectoryLedger`."""
        events: list[CanonicalEvent] = []
        call_index_by_id: dict[ToolCallId, int] = {}
        call_has_explicit_exit_code: set[ToolCallId] = set()

        turn_num = 1
        current_turn_id = TurnId(f"turn-{turn_num}")
        turn_has_response = False
        checkpoint_counter = 0
        raw_turn_to_canonical: dict[str, TurnId] = {}

        def _canonical_turn_for_raw(raw_tid: object) -> TurnId:
            nonlocal turn_num, current_turn_id
            s = str(raw_tid or "").strip()
            if not s:
                return current_turn_id
            if s not in raw_turn_to_canonical:
                if len(raw_turn_to_canonical) == 0 and not turn_has_response:
                    raw_turn_to_canonical[s] = TurnId("turn-1")
                    turn_num = 1
                else:
                    turn_num = max(turn_num, len(raw_turn_to_canonical)) + 1
                    raw_turn_to_canonical[s] = TurnId(f"turn-{turn_num}")
            current_turn_id = raw_turn_to_canonical[s]
            return current_turn_id

        for row in raw_lines:
            raw_str = row.raw_json.strip()
            if not raw_str:
                continue
            obj = json.loads(raw_str)
            rtype = obj.get("type", "")
            ts_iso = str(obj.get("timestamp") or "2026-01-01T00:00:00Z")
            pl = obj.get("payload")
            if not isinstance(pl, dict):
                continue

            if rtype == "session_meta":
                sid = str(pl.get("id") or pl.get("session_id") or "")
                ext = HostExtensionEnvelope(
                    host=HostKind.CODEX,
                    extension_type="session_meta",
                    raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                )
                anchor = EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=current_turn_id,
                    timestamp_iso=ts_iso,
                    codex_line_no=row.line_no,
                    host_extensions=(ext,),
                )
                events.append(
                    SystemEvent(
                        kind="system_message",
                        anchor=anchor,
                        event_type="session_meta",
                        message_text=f"codex_session:{sid}",
                    )
                )

            elif rtype == "world_state":
                ext = HostExtensionEnvelope(
                    host=HostKind.CODEX,
                    extension_type="world_state",
                    raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                )
                anchor = EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=current_turn_id,
                    timestamp_iso=ts_iso,
                    codex_line_no=row.line_no,
                    host_extensions=(ext,),
                )
                events.append(
                    SystemEvent(
                        kind="system_message",
                        anchor=anchor,
                        event_type="world_state",
                        message_text="codex_world_state",
                    )
                )

            elif rtype == "turn_context":
                tc_turn = pl.get("turn_id")
                if tc_turn:
                    _canonical_turn_for_raw(tc_turn)
                checkpoint_counter += 1
                ext = HostExtensionEnvelope(
                    host=HostKind.CODEX,
                    extension_type="turn_context",
                    raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                )
                anchor = EventOriginAnchor(
                    origin_host=HostKind.CODEX,
                    turn_id=current_turn_id,
                    timestamp_iso=ts_iso,
                    codex_line_no=row.line_no,
                    host_extensions=(ext,),
                )
                summary_str = (
                    str(pl["summary"])
                    if "summary" in pl and pl["summary"] is not None
                    else str(pl.get("model") or "turn_context")
                )
                conv_title = pl.get("conversation_title") or pl.get("title")
                cp_idx_val = (
                    int(pl["checkpoint_index"])
                    if "checkpoint_index" in pl and pl["checkpoint_index"] is not None
                    else checkpoint_counter
                )
                events.append(
                    CheckpointEvent(
                        kind="checkpoint",
                        anchor=anchor,
                        checkpoint_index=cp_idx_val,
                        user_intent=str(pl.get("user_intent") or ""),
                        session_summary=summary_str,
                        code_change_summary=str(pl.get("code_change_summary") or ""),
                        conversation_title=str(conv_title) if conv_title else None,
                    )
                )

            elif rtype == "event_msg":
                ev_sub = str(pl.get("type") or "")
                ev_turn = pl.get("turn_id")
                if ev_sub == "task_started":
                    if ev_turn:
                        _canonical_turn_for_raw(ev_turn)
                    elif turn_has_response:
                        turn_num += 1
                        current_turn_id = TurnId(f"turn-{turn_num}")
                    turn_has_response = False
                    anchor = EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=current_turn_id,
                        timestamp_iso=ts_iso,
                        codex_line_no=row.line_no,
                    )
                    b_kind_start: Literal["task_boundary", "notify_user"] = (
                        "notify_user" if pl.get("boundary_kind") == "notify_user" else "task_boundary"
                    )
                    events.append(
                        TaskBoundaryEvent(
                            kind="task_boundary",
                            anchor=anchor,
                            boundary_kind=b_kind_start,
                            task_name=str(pl.get("task_name") or ""),
                            task_status=str(pl.get("task_status") or "task_started"),
                            task_summary=str(pl.get("task_summary") or ""),
                        )
                    )
                elif ev_sub == "task_complete":
                    if ev_turn:
                        _canonical_turn_for_raw(ev_turn)
                    turn_has_response = True
                    anchor = EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=current_turn_id,
                        timestamp_iso=ts_iso,
                        codex_line_no=row.line_no,
                    )
                    b_kind_done: Literal["task_boundary", "notify_user"] = (
                        "notify_user" if pl.get("boundary_kind") == "notify_user" else "task_boundary"
                    )
                    t_summary_done = (
                        str(pl["task_summary"])
                        if "task_summary" in pl and pl["task_summary"] is not None
                        else str(pl.get("last_agent_message") or "Turn complete")
                    )
                    events.append(
                        TaskBoundaryEvent(
                            kind="task_boundary",
                            anchor=anchor,
                            boundary_kind=b_kind_done,
                            task_name=str(pl.get("task_name") or ""),
                            task_status=str(pl.get("task_status") or "task_complete"),
                            task_summary=t_summary_done,
                        )
                    )
                elif ev_sub == "user_message":
                    if ev_turn:
                        _canonical_turn_for_raw(ev_turn)
                    elif turn_has_response:
                        turn_num += 1
                        current_turn_id = TurnId(f"turn-{turn_num}")
                        turn_has_response = False
                    anchor = EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=current_turn_id,
                        timestamp_iso=ts_iso,
                        codex_line_no=row.line_no,
                    )
                    events.append(
                        UserInputEvent(
                            kind="user_input",
                            anchor=anchor,
                            query_text=str(pl.get("message") or pl.get("text") or ""),
                        )
                    )
                elif ev_sub == "agent_message":
                    turn_has_response = True
                    anchor = EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=current_turn_id,
                        timestamp_iso=ts_iso,
                        codex_line_no=row.line_no,
                    )
                    events.append(
                        AssistantTextEvent(
                            kind="assistant_text",
                            anchor=anchor,
                            text=str(pl.get("message") or pl.get("text") or ""),
                        )
                    )
                elif ev_sub == "agent_reasoning":
                    turn_has_response = True
                    anchor = EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=current_turn_id,
                        timestamp_iso=ts_iso,
                        codex_line_no=row.line_no,
                    )
                    events.append(
                        ThinkingEvent(
                            kind="thinking",
                            anchor=anchor,
                            summary_text=str(pl.get("text") or pl.get("summary") or ""),
                        )
                    )

            elif rtype == "response_item":
                ptype = str(pl.get("type") or "")
                passthrough = pl.get("internal_chat_message_metadata_passthrough")
                if isinstance(passthrough, dict) and passthrough.get("turn_id"):
                    _canonical_turn_for_raw(passthrough["turn_id"])

                if ptype == "message":
                    role = str(pl.get("role") or "user")
                    content_blocks = pl.get("content") or []
                    texts: list[str] = []
                    media_items: list[MediaAttachment] = []
                    if isinstance(content_blocks, list):
                        for blk in content_blocks:
                            if isinstance(blk, dict):
                                if "text" in blk and blk["text"] is not None:
                                    texts.append(str(blk["text"]))
                                elif blk.get("type") == "input_image":
                                    media_items.append(
                                        MediaAttachment(
                                            mime_type=str(blk.get("mime_type") or "image/png"),
                                            uri=str(blk.get("image_url") or blk.get("uri") or ""),
                                        )
                                    )
                    full_text = "\n".join(texts)
                    msg_id = str(pl.get("id")) if pl.get("id") else None

                    if role == "user":
                        if turn_has_response and not (isinstance(passthrough, dict) and passthrough.get("turn_id")):
                            turn_num += 1
                            current_turn_id = TurnId(f"turn-{turn_num}")
                            turn_has_response = False
                        scope_items_out: Tuple[str, ...] = ()
                        if isinstance(passthrough, dict) and isinstance(passthrough.get("scope_items"), list):
                            scope_items_out = tuple(str(s) for s in passthrough["scope_items"])
                        anchor = EventOriginAnchor(
                            origin_host=HostKind.CODEX,
                            turn_id=current_turn_id,
                            timestamp_iso=ts_iso,
                            codex_line_no=row.line_no,
                            client_message_id=msg_id,
                        )
                        events.append(
                            UserInputEvent(
                                kind="user_input",
                                anchor=anchor,
                                query_text=full_text,
                                scope_items=scope_items_out,
                                media=tuple(media_items),
                            )
                        )
                    elif role == "assistant":
                        turn_has_response = True
                        anchor = EventOriginAnchor(
                            origin_host=HostKind.CODEX,
                            turn_id=current_turn_id,
                            timestamp_iso=ts_iso,
                            codex_line_no=row.line_no,
                        )
                        events.append(
                            AssistantTextEvent(
                                kind="assistant_text",
                                anchor=anchor,
                                text=full_text,
                                message_id=msg_id,
                            )
                        )
                    else:
                        ext = HostExtensionEnvelope(
                            host=HostKind.CODEX,
                            extension_type=f"message_{role}",
                            raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                        )
                        anchor = EventOriginAnchor(
                            origin_host=HostKind.CODEX,
                            turn_id=current_turn_id,
                            timestamp_iso=ts_iso,
                            codex_line_no=row.line_no,
                            host_extensions=(ext,),
                        )
                        events.append(
                            SystemEvent(
                                kind="system_message",
                                anchor=anchor,
                                event_type=f"codex_{role}_message",
                                message_text=full_text,
                            )
                        )

                elif ptype in ("reasoning", "magpie_reasoning"):
                    turn_has_response = True
                    summary_list = pl.get("summary") or []
                    summary_parts: list[str] = []
                    if isinstance(summary_list, list):
                        for item in summary_list:
                            if isinstance(item, dict) and "text" in item:
                                summary_parts.append(str(item["text"]))
                            elif isinstance(item, str):
                                summary_parts.append(item)
                    summary_text = "\n".join(summary_parts)
                    raw_thinking_str: str | None = None
                    if isinstance(pl.get("content"), str):
                        raw_thinking_str = pl["content"]

                    exts: list[HostExtensionEnvelope] = []
                    enc_content = pl.get("encrypted_content")
                    if enc_content:
                        exts.append(
                            HostExtensionEnvelope(
                                host=HostKind.CODEX,
                                extension_type="encrypted_content",
                                raw_payload=str(enc_content).encode("utf-8"),
                                metadata=(("reasoning_type", ptype),),
                            )
                        )
                    else:
                        exts.append(
                            HostExtensionEnvelope(
                                host=HostKind.CODEX,
                                extension_type="reasoning_meta",
                                raw_payload=b"",
                                metadata=(("reasoning_type", ptype),),
                            )
                        )

                    anchor = EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=current_turn_id,
                        timestamp_iso=ts_iso,
                        codex_line_no=row.line_no,
                        host_extensions=tuple(exts),
                    )
                    events.append(
                        ThinkingEvent(
                            kind="thinking",
                            anchor=anchor,
                            summary_text=summary_text,
                            raw_thinking=raw_thinking_str,
                        )
                    )

                elif ptype in ("function_call", "custom_tool_call", "tool_search_call"):
                    turn_has_response = True
                    cid = ToolCallId(str(pl.get("call_id") or pl.get("id") or f"call-line-{int(row.line_no)}"))
                    tname = str(pl.get("name") or ("tool_search" if ptype == "tool_search_call" else "unknown_tool"))
                    raw_args = (
                        pl.get("arguments")
                        if pl.get("arguments") not in (None, "")
                        else pl.get("input", "{}")
                    )
                    if isinstance(raw_args, (dict, list)):
                        args_json = json.dumps(raw_args, ensure_ascii=False, sort_keys=True)
                    elif isinstance(raw_args, str):
                        if ptype == "custom_tool_call" and tname == "apply_patch" and not raw_args.strip().startswith("{"):
                            path_match = re.search(
                                r"^\*\*\* (Update|Add|Delete) File:\s*(.+)$",
                                raw_args,
                                flags=re.MULTILINE,
                            )
                            inferred_op = "MODIFY"
                            if path_match:
                                verb = path_match.group(1)
                                if verb == "Add":
                                    inferred_op = "CREATE"
                                elif verb == "Delete":
                                    inferred_op = "DELETE"
                                inferred_path = normalize_tool_file_path(path_match.group(2))
                            else:
                                inferred_path = "patch_target"
                            normalized_patch = re.sub(
                                r"^(\*\*\* (?:Update|Add|Delete) File:\s*)(.+)$",
                                lambda m: m.group(1) + normalize_tool_file_path(m.group(2)),
                                raw_args,
                                flags=re.MULTILINE,
                            )
                            args_json = json.dumps(
                                {
                                    "path": inferred_path,
                                    "edit_op": inferred_op,
                                    "unified_diff": normalized_patch,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        else:
                            args_json = raw_args
                    else:
                        args_json = "{}"

                    parsed_call_args = _safe_json_dict(args_json)
                    if "exit_code" in parsed_call_args:
                        call_has_explicit_exit_code.add(cid)

                    payload = TrajectoryWireCodec.decode_tool_payload(tname, args_json)
                    exts = []
                    if pl.get("namespace"):
                        exts.append(
                            HostExtensionEnvelope(
                                host=HostKind.CODEX,
                                extension_type="tool_namespace",
                                raw_payload=str(pl["namespace"]).encode("utf-8"),
                                metadata=(("item_type", ptype),),
                            )
                        )
                    anchor = EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=current_turn_id,
                        timestamp_iso=ts_iso,
                        codex_line_no=row.line_no,
                        host_extensions=tuple(exts),
                    )
                    call_index_by_id[cid] = len(events)
                    events.append(
                        ToolCallEvent(
                            kind="tool_call",
                            anchor=anchor,
                            call_id=cid,
                            tool_name=tname,
                            tool_summary=tname,
                            payload=payload,
                        )
                    )

                elif ptype in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
                    turn_has_response = True
                    cid = ToolCallId(str(pl.get("call_id") or pl.get("id") or f"call-out-{int(row.line_no)}"))
                    raw_out = pl.get("output") if "output" in pl else pl.get("tools", "")
                    if isinstance(raw_out, list):
                        parts: list[str] = []
                        for item in raw_out:
                            if isinstance(item, dict) and "text" in item:
                                parts.append(str(item["text"]))
                            else:
                                parts.append(json.dumps(item, ensure_ascii=False))
                        out_text = "\n".join(parts)
                    elif isinstance(raw_out, dict):
                        out_text = json.dumps(raw_out, ensure_ascii=False)
                    else:
                        out_text = str(raw_out or "")

                    prior_tname = "unknown_tool"
                    prior_payload: CanonicalToolPayload = OpaqueToolPayload(
                        kind="opaque",
                        tool_name=prior_tname,
                        raw_arguments_json="{}",
                    )
                    if cid in call_index_by_id:
                        call_pos = call_index_by_id[cid]
                        prior_ev = events[call_pos]
                        if isinstance(prior_ev, ToolCallEvent):
                            prior_tname = prior_ev.tool_name
                            prior_payload = prior_ev.payload
                            if (
                                isinstance(prior_payload, RunCommandPayload)
                                and prior_payload.exit_code is None
                                and cid not in call_has_explicit_exit_code
                            ):
                                m_exit = re.search(r"Process exited with code (-?\d+)", out_text)
                                exit_val = int(m_exit.group(1)) if m_exit else 0
                                prior_payload = dataclasses.replace(prior_payload, exit_code=exit_val)
                                events[call_pos] = dataclasses.replace(prior_ev, payload=prior_payload)

                    is_err = bool(pl.get("is_error", False)) or str(pl.get("status") or "").lower() in (
                        "failed",
                        "error",
                    )
                    anchor = EventOriginAnchor(
                        origin_host=HostKind.CODEX,
                        turn_id=current_turn_id,
                        timestamp_iso=ts_iso,
                        codex_line_no=row.line_no,
                    )
                    events.append(
                        ToolResultEvent(
                            kind="tool_result",
                            anchor=anchor,
                            call_id=cid,
                            tool_name=prior_tname,
                            output_text=out_text,
                            is_error=is_err,
                            payload=prior_payload,
                        )
                    )

        return TrajectoryLedger.from_events(
            workspace_path=workspace_path,
            workspace_uri=workspace_uri,
            events=events,
            explicit_title_override=explicit_title_override,
        )

    @staticmethod
    def encode_events_to_codex_rollout(
        events: Sequence[CanonicalEvent],
        starting_line_no: RolloutLineNo = RolloutLineNo(1),
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> Tuple[CodexRawRolloutLine, ...]:
        """Serialize a slice of `CanonicalEvent` nodes into Codex `rollout-*.jsonl` lines."""
        lines: list[CodexRawRolloutLine] = []
        next_line = int(starting_line_no)

        def _append_record(ts: str, rtype: str, payload: Mapping[str, Any]) -> None:
            nonlocal next_line
            record = {
                "timestamp": ts or "2026-01-01T00:00:00Z",
                "ordinal": next_line - 1,
                "type": rtype,
                "payload": payload,
            }
            lines.append(
                CodexRawRolloutLine(
                    line_no=RolloutLineNo(next_line),
                    raw_json=json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                )
            )
            next_line += 1

        has_session_meta = any(
            isinstance(e, SystemEvent) and e.event_type == "session_meta" for e in events
        )
        if not has_session_meta and events and int(starting_line_no) == 1:
            first_ts = events[0].anchor.timestamp_iso or "2026-01-01T00:00:00Z"
            effective_sid = session_id or "codex-synced-session"
            meta_payload: dict[str, Any] = {
                "id": effective_sid,
                "session_id": effective_sid,
                "timestamp": first_ts,
                "originator": "codex_agy_sync",
                "source": "desktop",
                "model_provider": "openai",
            }
            if cwd is not None:
                meta_payload["cwd"] = cwd
            _append_record(first_ts, "session_meta", meta_payload)

        active_turn: TurnId | None = None
        active_turn_closed = True
        last_ts = "2026-01-01T00:00:00Z"

        for ev in events:
            ts = ev.anchor.timestamp_iso or last_ts
            last_ts = ts

            if isinstance(ev, SystemEvent) and ev.event_type in ("session_meta", "world_state"):
                raw_dict: dict[str, Any] = {}
                for ext in ev.anchor.host_extensions:
                    if ext.extension_type == ev.event_type and ext.raw_payload:
                        raw_dict = _safe_json_dict(ext.raw_payload.decode("utf-8", errors="replace"))
                if not raw_dict:
                    raw_dict = {"message": ev.message_text, "event_type": ev.event_type}
                _append_record(ts, ev.event_type, raw_dict)
                continue

            if ev.anchor.turn_id != active_turn:
                if active_turn is not None and not active_turn_closed:
                    _append_record(
                        ts,
                        "event_msg",
                        {
                            "type": "task_complete",
                            "turn_id": str(active_turn),
                            "root_turn_id": str(active_turn),
                            "last_agent_message": "Turn complete",
                        },
                    )
                active_turn = ev.anchor.turn_id
                active_turn_closed = False
                if not (isinstance(ev, TaskBoundaryEvent) and ev.task_status == "task_started"):
                    _append_record(
                        ts,
                        "event_msg",
                        {
                            "type": "task_started",
                            "turn_id": str(active_turn),
                            "root_turn_id": str(active_turn),
                        },
                    )

            if isinstance(ev, UserInputEvent):
                content_list: list[dict[str, Any]] = [{"type": "input_text", "text": ev.query_text}]
                for m in ev.media:
                    content_list.append({"type": "input_image", "mime_type": m.mime_type, "uri": m.uri})
                user_passthrough: dict[str, Any] = {"turn_id": str(ev.anchor.turn_id)}
                if ev.scope_items:
                    user_passthrough["scope_items"] = list(ev.scope_items)
                _append_record(
                    ts,
                    "response_item",
                    {
                        "type": "message",
                        "id": ev.anchor.client_message_id or f"msg_user_{next_line}",
                        "role": "user",
                        "content": content_list,
                        "internal_chat_message_metadata_passthrough": user_passthrough,
                    },
                )

            elif isinstance(ev, ThinkingEvent):
                r_type = "reasoning"
                enc_val: str | None = None
                for ext in ev.anchor.host_extensions:
                    for mk, mv in ext.metadata:
                        if mk == "reasoning_type" and mv in ("reasoning", "magpie_reasoning"):
                            r_type = mv
                    if ext.extension_type == "encrypted_content" and ext.raw_payload:
                        enc_val = ext.raw_payload.decode("utf-8", errors="replace")
                pl_dict: dict[str, Any] = {
                    "type": r_type,
                    "summary": [{"type": "summary_text", "text": ev.summary_text}],
                    "content": ev.raw_thinking,
                    "internal_chat_message_metadata_passthrough": {"turn_id": str(ev.anchor.turn_id)},
                }
                if enc_val is not None:
                    pl_dict["encrypted_content"] = enc_val
                _append_record(ts, "response_item", pl_dict)

            elif isinstance(ev, AssistantTextEvent):
                _append_record(
                    ts,
                    "response_item",
                    {
                        "type": "message",
                        "status": "completed",
                        "id": ev.message_id or f"msg_asst_{next_line}",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": ev.text}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": str(ev.anchor.turn_id)},
                    },
                )

            elif isinstance(ev, ToolCallEvent):
                spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(ev.payload.kind)
                if spec is not None:
                    c_kind = spec.codex_call_kind
                    c_name = spec.codex_tool_name
                else:
                    c_kind = "tool_search_call" if ev.tool_name == "tool_search" else "function_call"
                    c_name = ev.tool_name

                if isinstance(ev.payload, RunCommandPayload):
                    norm_cwd = normalize_tool_file_path(ev.payload.cwd)
                    cmd_args = _safe_json_dict(serialize_tool_payload_arguments_json(ev.payload))
                    cmd_args["cmd"] = ev.payload.command_line
                    cmd_args["workdir"] = norm_cwd
                    args_json = json.dumps(cmd_args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                else:
                    args_json = serialize_tool_payload_arguments_json(ev.payload)

                call_pl: dict[str, Any] = {
                    "type": c_kind,
                    "status": "completed",
                    "call_id": str(ev.call_id),
                    "name": c_name,
                    "arguments": args_json,
                    "internal_chat_message_metadata_passthrough": {"turn_id": str(ev.anchor.turn_id)},
                }
                if c_kind == "custom_tool_call" and isinstance(ev.payload, FileEditPayload):
                    call_pl["input"] = _synthesize_codex_patch_input(ev.payload)
                for ext in ev.anchor.host_extensions:
                    if ext.extension_type == "tool_namespace" and ext.raw_payload:
                        call_pl["namespace"] = ext.raw_payload.decode("utf-8", errors="replace")
                _append_record(ts, "response_item", call_pl)

            elif isinstance(ev, ToolResultEvent):
                spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(ev.payload.kind)
                if spec is not None and spec.codex_call_kind == "custom_tool_call":
                    out_kind = "custom_tool_call_output"
                elif ev.tool_name == "tool_search":
                    out_kind = "tool_search_output"
                else:
                    out_kind = "function_call_output"

                _append_record(
                    ts,
                    "response_item",
                    {
                        "type": out_kind,
                        "call_id": str(ev.call_id),
                        "output": ev.output_text,
                        "is_error": ev.is_error,
                        "status": "failed" if ev.is_error else "completed",
                        "internal_chat_message_metadata_passthrough": {"turn_id": str(ev.anchor.turn_id)},
                    },
                )

            elif isinstance(ev, CheckpointEvent):
                cp_pl: dict[str, Any] = {
                    "turn_id": str(ev.anchor.turn_id),
                    "root_turn_id": str(ev.anchor.turn_id),
                    "checkpoint_index": ev.checkpoint_index,
                    "user_intent": ev.user_intent,
                    "summary": ev.session_summary,
                    "code_change_summary": ev.code_change_summary,
                }
                if ev.conversation_title:
                    cp_pl["conversation_title"] = ev.conversation_title
                for ext in ev.anchor.host_extensions:
                    if ext.extension_type == "turn_context" and ext.raw_payload:
                        orig_tc = _safe_json_dict(ext.raw_payload.decode("utf-8", errors="replace"))
                        orig_tc.update(cp_pl)
                        cp_pl = orig_tc
                _append_record(ts, "turn_context", cp_pl)

            elif isinstance(ev, TaskBoundaryEvent):
                if ev.task_status == "task_complete":
                    active_turn_closed = True
                _append_record(
                    ts,
                    "event_msg",
                    {
                        "type": ev.task_status if ev.task_status in ("task_started", "task_complete") else "task_complete",
                        "turn_id": str(ev.anchor.turn_id),
                        "root_turn_id": str(ev.anchor.turn_id),
                        "task_name": ev.task_name,
                        "task_summary": ev.task_summary,
                        "last_agent_message": ev.task_summary,
                        "boundary_kind": ev.boundary_kind,
                        "task_status": ev.task_status,
                    },
                )

            elif isinstance(ev, SystemEvent):
                if ev.event_type.startswith("codex_") and ev.event_type.endswith("_message"):
                    role = ev.event_type.removeprefix("codex_").removesuffix("_message")
                    _append_record(
                        ts,
                        "response_item",
                        {
                            "type": "message",
                            "role": role,
                            "content": [{"type": "input_text", "text": ev.message_text}],
                            "internal_chat_message_metadata_passthrough": {"turn_id": str(ev.anchor.turn_id)},
                        },
                    )
                else:
                    _append_record(
                        ts,
                        "event_msg",
                        {
                            "type": "system_message",
                            "turn_id": str(ev.anchor.turn_id),
                            "event_type": ev.event_type,
                            "message": ev.message_text,
                        },
                    )

        if active_turn is not None and not active_turn_closed:
            _append_record(
                last_ts,
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": str(active_turn),
                    "root_turn_id": str(active_turn),
                    "last_agent_message": "Turn complete",
                },
            )

        return tuple(lines)

    @staticmethod
    def encode_ledger_to_codex_rollout(
        ledger_or_events: TrajectoryLedger | Sequence[CanonicalEvent],
        starting_line_no: RolloutLineNo = RolloutLineNo(1),
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> Tuple[CodexRawRolloutLine, ...]:
        """Encode a `TrajectoryLedger` or event sequence into Codex `rollout-*.jsonl` lines."""
        if isinstance(ledger_or_events, TrajectoryLedger):
            events: Sequence[CanonicalEvent] = ledger_or_events.events
            effective_cwd = cwd if cwd is not None else str(ledger_or_events.workspace_path)
        else:
            events = ledger_or_events
            effective_cwd = cwd
        return TrajectoryWireCodec.encode_events_to_codex_rollout(
            events,
            starting_line_no,
            session_id=session_id,
            cwd=effective_cwd,
        )


_GUI_SOURCE_BY_ENUM: Mapping[int, AgyGuiSafeSource] = {
    src.proto_enum_value: src for src in AgyGuiSafeSource
}
_PBTXT_TITLE_RE = re.compile(r'title:\s*"((?:[^"\\]|\\.)*)"')


def serialize_tool_payload_arguments_json(payload: CanonicalToolPayload) -> str:
    """Serialize a `CanonicalToolPayload` into deterministic JSON arguments."""
    if isinstance(payload, OpaqueToolPayload):
        return payload.raw_arguments_json or "{}"
    if isinstance(payload, FinishPayload):
        return payload.arguments_json or "{}"
    data = dataclasses.asdict(payload)
    data.pop("kind", None)
    if isinstance(payload, (ReadFilePayload, FileEditPayload)):
        data["path"] = normalize_tool_file_path(payload.path)
    elif isinstance(payload, RunCommandPayload):
        data["cwd"] = normalize_tool_file_path(payload.cwd)
    elif isinstance(payload, DefineSubagentPayload) and payload.agent_path is not None:
        data["agent_path"] = normalize_tool_file_path(payload.agent_path)
    elif isinstance(payload, GenerateImagePayload):
        data["image_paths"] = [normalize_tool_file_path(p) for p in payload.image_paths]
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_transcript_row_jsonl(row: TranscriptRowProjection) -> str:
    """Render one compact JSONL line matching Antigravity `transcript.jsonl` format."""
    obj: dict[str, object] = {
        "step_index": int(row.step_index),
        "source": row.source,
        "type": row.step_type_name,
        "status": row.status_name,
        "created_at": row.created_at,
        "content": row.content,
    }
    if row.thinking is not None:
        obj["thinking"] = row.thinking
    if row.tool_calls_json:
        obj["tool_calls"] = list(row.tool_calls_json)
    if row.media_uris:
        obj["media"] = list(row.media_uris)
    if row.input_tokens or row.output_tokens or row.cache_read_tokens:
        obj["input_tokens"] = row.input_tokens
        obj["cache_read_tokens"] = row.cache_read_tokens
        obj["output_tokens"] = row.output_tokens
    if row.truncated_fields:
        obj["truncated_fields"] = list(row.truncated_fields)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def render_annotation_pbtxt(proj: AnnotationPbtxtProjection) -> str:
    """Serialize `exa.jetski_cortex_pb.ConversationAnnotations` textproto."""
    single_line_title = " ".join(proj.title.split())
    escaped_title = single_line_title.replace("\\", "\\\\").replace('"', '\\"')
    seconds, nanos = parse_iso_to_seconds_nanos(proj.last_user_view_time_iso)
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


def parse_annotation_pbtxt_title(raw_text: str) -> str | None:
    """Extract unescaped `title` string from `ConversationAnnotations` textproto."""
    m = _PBTXT_TITLE_RE.search(raw_text or "")
    if not m or not m.group(1).strip():
        return None
    return (
        m.group(1)
        .replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\\\", "\\")
        .strip()
    ) or None


def encode_workspace_info_proto(uri: WorkspaceRootUri) -> bytes:
    """Encode `exa.cortex_pb.CortexWorkspaceMetadata` wire bytes."""
    text = str(uri)
    return encode_string_field(1, text) + encode_string_field(2, text)


def encode_annotations_proto(proj: AnnotationPbtxtProjection) -> bytes:
    """Encode `exa.jetski_cortex_pb.ConversationAnnotations` wire bytes."""
    buf = bytearray()
    if proj.title:
        buf.extend(encode_string_field(1, proj.title))
    ts_bytes = encode_timestamp_proto(proj.last_user_view_time_iso)
    if ts_bytes:
        buf.extend(encode_bytes_field(7, ts_bytes))
    if proj.unknown_wire_fields:
        buf.extend(encode_unknown_fields(proj.unknown_wire_fields))
    return bytes(buf)


def _last_str(field_map: Mapping[int, Sequence[tuple[int, int | bytes]]], tag: int, strip: bool = False) -> str:
    val = ""
    for wt, raw in field_map.get(tag, ()):
        if wt == 2 and isinstance(raw, bytes):
            val = raw.decode("utf-8", errors="replace")
    return val.strip() if strip else val


def _last_varint(field_map: Mapping[int, Sequence[tuple[int, int | bytes]]], tag: int) -> int | None:
    out: int | None = None
    for wt, raw in field_map.get(tag, ()):
        if wt == 0 and isinstance(raw, int):
            out = int(raw)
    return out


def _last_timestamp(field_map: Mapping[int, Sequence[tuple[int, int | bytes]]], tag: int, default_iso: str = "") -> str:
    out = default_iso
    for wt, raw in field_map.get(tag, ()):
        if wt == 2 and isinstance(raw, bytes):
            out = decode_timestamp_proto(raw, default_iso=default_iso)
    return out


def decode_annotations_proto(
    raw_bytes: bytes,
    cascade_id: CascadeId,
    default_view_iso: str = "2026-01-01T00:00:00Z",
) -> AnnotationPbtxtProjection:
    """Decode `exa.jetski_cortex_pb.ConversationAnnotations` wire bytes preserving unknown fields."""
    field_map, unknowns = parse_proto_fields(raw_bytes, known_tags={1, 7})
    return AnnotationPbtxtProjection(
        cascade_id=cascade_id,
        title=_last_str(field_map, 1),
        last_user_view_time_iso=_last_timestamp(field_map, 7, default_iso=default_view_iso),
        unknown_wire_fields=tuple(unknowns),
    )


def encode_trajectory_metadata_proto(
    project_id: AgyProjectId,
    workspace_uris: Sequence[WorkspaceRootUri],
    created_at_iso: str,
    nesting_depth: int = 0,
    parent_conversation_id: str = "",
    root_conversation_id: str = "",
    agent_type_name: str = "",
    agent_role: str = "",
    unknown_wire_fields: Sequence[UnknownWireField] = (),
) -> bytes:
    """Encode `exa.cortex_pb.CortexTrajectoryMetadata` wire bytes."""
    buf = bytearray()
    for uri in workspace_uris:
        buf.extend(encode_bytes_field(1, encode_workspace_info_proto(uri)))
    if ts_bytes := encode_timestamp_proto(created_at_iso):
        buf.extend(encode_bytes_field(2, ts_bytes))
    if parent_conversation_id:
        buf.extend(encode_string_field(5, parent_conversation_id))
    if root_conversation_id:
        buf.extend(encode_string_field(6, root_conversation_id))
    for uri in workspace_uris:
        buf.extend(encode_string_field(7, str(uri)))
    if agent_type_name or agent_role:
        buf.extend(encode_bytes_field(8, encode_string_field(1, agent_type_name) + encode_string_field(2, agent_role)))
    if nesting_depth != 0:
        buf.extend(encode_varint_field(17, nesting_depth))
    if str(project_id):
        buf.extend(encode_string_field(18, str(project_id)))
    if unknown_wire_fields:
        buf.extend(encode_unknown_fields(unknown_wire_fields))
    return bytes(buf)


def decode_trajectory_metadata_proto(raw_bytes: bytes) -> dict[str, Any]:
    """Decode `exa.cortex_pb.CortexTrajectoryMetadata` wire bytes preserving unknown fields."""
    field_map, unknowns = parse_proto_fields(raw_bytes, known_tags={1, 2, 5, 6, 7, 8, 17, 18})
    workspace_uris: list[str] = []
    for wt, val in field_map.get(1, ()):
        if wt == 2 and isinstance(val, bytes):
            for sfn, swt, _, sraw in iter_wire_fields(val):
                if sfn == 1 and swt == 2 and (u := sraw.decode("utf-8", errors="replace").strip()) and u not in workspace_uris:
                    workspace_uris.append(u)
    for wt, val in field_map.get(7, ()):
        if wt == 2 and isinstance(val, bytes) and (u := val.decode("utf-8", errors="replace").strip()) and u not in workspace_uris:
            workspace_uris.append(u)
    agent_type_name = agent_role = ""
    for wt, val in field_map.get(8, ()):
        if wt == 2 and isinstance(val, bytes):
            for sfn, swt, _, sraw in iter_wire_fields(val):
                if sfn == 1 and swt == 2:
                    agent_type_name = sraw.decode("utf-8", errors="replace").strip()
                elif sfn == 2 and swt == 2:
                    agent_role = sraw.decode("utf-8", errors="replace").strip()
    raw_depth = _last_varint(field_map, 17)
    return {
        "project_id": _last_str(field_map, 18, strip=True),
        "workspace_uris": workspace_uris,
        "created_at_iso": _last_timestamp(field_map, 2),
        "parent_conversation_id": _last_str(field_map, 5, strip=True),
        "root_conversation_id": _last_str(field_map, 6, strip=True),
        "agent_type_name": agent_type_name,
        "agent_role": agent_role,
        "nesting_depth": max(0, signed_int32(raw_depth)) if raw_depth is not None else 0,
        "unknown_wire_fields": tuple(unknowns),
    }


def encode_cascade_summary_proto(proj: CascadeSummaryProjection) -> bytes:
    """Encode `exa.jetski_cortex_pb.CascadeTrajectorySummary` wire bytes."""
    buf = bytearray()
    if proj.title:
        buf.extend(encode_string_field(1, proj.title))
    if proj.step_count > 0:
        buf.extend(encode_varint_field(2, proj.step_count))
    if mod_ts := encode_timestamp_proto(proj.updated_at_iso):
        buf.extend(encode_bytes_field(3, mod_ts))
    if traj_id_str := (str(proj.trajectory_id) if proj.trajectory_id is not None else str(proj.cascade_id)):
        buf.extend(encode_string_field(4, traj_id_str))
    if proj.status > 0:
        buf.extend(encode_varint_field(5, proj.status))
    if created_ts := encode_timestamp_proto(proj.created_at_iso):
        buf.extend(encode_bytes_field(7, created_ts))
    for uri in proj.workspace_uris:
        buf.extend(encode_bytes_field(9, encode_workspace_info_proto(uri)))
    if user_ts := encode_timestamp_proto(proj.last_user_input_time_iso or proj.updated_at_iso):
        buf.extend(encode_bytes_field(10, user_ts))
    if annot_bytes := encode_annotations_proto(proj.annotations):
        buf.extend(encode_bytes_field(15, annot_bytes))
    if proj.last_user_input_step_index is not None and proj.last_user_input_step_index >= 0:
        buf.extend(encode_varint_field(16, proj.last_user_input_step_index))
    traj_meta_bytes = encode_trajectory_metadata_proto(
        project_id=proj.project_id, workspace_uris=proj.workspace_uris, created_at_iso=proj.created_at_iso,
        nesting_depth=proj.nesting_depth, parent_conversation_id=str(proj.parent_cascade_id or ""),
        root_conversation_id=str(proj.root_cascade_id or ""), agent_type_name=proj.agent_type_name,
        agent_role=proj.agent_role, unknown_wire_fields=proj.trajectory_metadata_unknown_fields,
    )
    if traj_meta_bytes:
        buf.extend(encode_bytes_field(17, traj_meta_bytes))
    if proj.source.proto_enum_value != 0:
        buf.extend(encode_varint_field(20, proj.source.proto_enum_value))
    if proj.trajectory_type > 0:
        buf.extend(encode_varint_field(22, proj.trajectory_type))
    if proj.unknown_wire_fields:
        buf.extend(encode_unknown_fields(proj.unknown_wire_fields))
    return bytes(buf)


def decode_cascade_summary_proto(
    raw_bytes: bytes,
    fallback_cascade_id: CascadeId | None = None,
) -> CascadeSummaryProjection:
    """Decode `exa.jetski_cortex_pb.CascadeTrajectorySummary` wire bytes faithfully."""
    field_map, unknowns = parse_proto_fields(raw_bytes, known_tags={1, 2, 3, 4, 5, 7, 9, 10, 15, 16, 17, 20, 22})
    summary_str = _last_str(field_map, 1)
    traj_id_str = _last_str(field_map, 4, strip=True)
    updated_iso, created_iso, last_user_iso = _last_timestamp(field_map, 3), _last_timestamp(field_map, 7), _last_timestamp(field_map, 10)
    effective_cid = fallback_cascade_id or CascadeId(traj_id_str or "unknown-cascade")

    ws_uris: list[WorkspaceRootUri] = []
    for wt, val in field_map.get(9, ()):
        if wt == 2 and isinstance(val, bytes):
            for sfn, swt, _, sraw in iter_wire_fields(val):
                if sfn == 1 and swt == 2 and (u_str := sraw.decode("utf-8", errors="replace").strip()) and WorkspaceRootUri(u_str) not in ws_uris:
                    ws_uris.append(WorkspaceRootUri(u_str))

    tmeta = decode_trajectory_metadata_proto(b"")
    for wt, val in field_map.get(17, ()):
        if wt == 2 and isinstance(val, bytes):
            tmeta = decode_trajectory_metadata_proto(val)
            for u_str in tmeta["workspace_uris"]:
                if u_str and WorkspaceRootUri(u_str) not in ws_uris:
                    ws_uris.append(WorkspaceRootUri(u_str))

    eff_created = created_iso or str(tmeta["created_at_iso"] or "")
    eff_updated = updated_iso or last_user_iso or eff_created
    annot = AnnotationPbtxtProjection(cascade_id=effective_cid, title="", last_user_view_time_iso=eff_updated)
    for wt, val in field_map.get(15, ()):
        if wt == 2 and isinstance(val, bytes):
            annot = decode_annotations_proto(val, effective_cid, default_view_iso=eff_updated)

    source = AgyGuiSafeSource.UNSPECIFIED
    if (raw_source := _last_varint(field_map, 20)) is not None:
        if (mapped := _GUI_SOURCE_BY_ENUM.get(raw_source)) is not None:
            source = mapped
        else:
            unknowns.append(UnknownWireField(field_tag=20, wire_type=0, raw_bytes=encode_varint(raw_source)))

    resolved_title = annot.title or summary_str
    agent_type, agent_role = str(tmeta["agent_type_name"] or ""), str(tmeta["agent_role"] or "")
    fields_dict: dict[str, Any] = {
        "cascade_id": effective_cid, "project_id": AgyProjectId(str(tmeta["project_id"] or "")),
        "workspace_uris": tuple(ws_uris), "source": source,
        "title": resolved_title, "preview": summary_str or resolved_title,
        "step_count": _last_varint(field_map, 2) or 0,
        "created_at_iso": eff_created, "updated_at_iso": eff_updated, "annotations": annot,
        "nesting_depth": int(tmeta["nesting_depth"]),
        "parent_cascade_id": CascadeId(tmeta["parent_conversation_id"]) if tmeta["parent_conversation_id"] else None,
        "root_cascade_id": CascadeId(tmeta["root_conversation_id"]) if tmeta["root_conversation_id"] else None,
        "agent_name": agent_role or agent_type, "agent_type_name": agent_type, "agent_role": agent_role,
        "status": _last_varint(field_map, 5) or 0,
        "trajectory_id": TrajectoryId(traj_id_str) if traj_id_str else None,
        "trajectory_type": _last_varint(field_map, 22) or 0,
        "last_user_input_time_iso": last_user_iso, "last_user_input_step_index": _last_varint(field_map, 16),
        "trajectory_metadata_unknown_fields": tuple(tmeta["unknown_wire_fields"]),
        "unknown_wire_fields": tuple(unknowns),
    }
    try:
        return CascadeSummaryProjection(**fields_dict)
    except ValueError:
        return CascadeSummaryProjection._from_wire_fields(**fields_dict)


def encode_summaries_state_proto(state: SummariesStateProjection) -> bytes:
    """Encode `jetbox_summaries_pb.SummariesState` wire bytes."""
    buf = bytearray()
    for cid in sorted(state.summaries.keys()):
        summary_proj = state.summaries[cid]
        entry = encode_string_field(1, str(cid)) + encode_bytes_field(2, summary_proj.summary_proto_bytes)
        buf.extend(encode_bytes_field(1, entry))
    for path_key in sorted(state.file_mod_times.keys()):
        mtime_val = state.file_mod_times[path_key]
        entry = encode_string_field(1, path_key) + encode_varint_field(2, int(mtime_val))
        buf.extend(encode_bytes_field(2, entry))
    if state.unknown_wire_fields:
        buf.extend(encode_unknown_fields(state.unknown_wire_fields))
    return bytes(buf)


def decode_summaries_state_proto(raw_bytes: bytes) -> SummariesStateProjection:
    """Decode `jetbox_summaries_pb.SummariesState` wire bytes faithfully."""
    field_map, unknowns = parse_proto_fields(raw_bytes, known_tags={1, 2})
    summaries: dict[CascadeId, CascadeSummaryProjection] = {}
    file_mod_times: dict[str, int] = {}

    for wt, payload in field_map.get(1, ()):
        if wt == 2 and isinstance(payload, bytes):
            cid_str = ""
            sum_bytes = b""
            for efn, ewt, _, epayload in iter_wire_fields(payload):
                if efn == 1 and ewt == 2:
                    cid_str = epayload.decode("utf-8", errors="replace")
                elif efn == 2 and ewt == 2:
                    sum_bytes = bytes(epayload)
            if cid_str:
                cid = CascadeId(cid_str)
                summaries[cid] = decode_cascade_summary_proto(sum_bytes, fallback_cascade_id=cid)

    for wt, payload in field_map.get(2, ()):
        if wt == 2 and isinstance(payload, bytes):
            pkey = ""
            mval = 0
            for efn, ewt, eval_int, epayload in iter_wire_fields(payload):
                if efn == 1 and ewt == 2:
                    pkey = epayload.decode("utf-8", errors="replace")
                elif efn == 2 and ewt == 0:
                    mval = signed_int64(eval_int)
            if pkey:
                file_mod_times[pkey] = mval

    return SummariesStateProjection(
        summaries=summaries,
        file_mod_times=file_mod_times,
        unknown_wire_fields=tuple(unknowns),
    )
