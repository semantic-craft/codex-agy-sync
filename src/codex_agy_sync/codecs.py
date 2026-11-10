"""Table-driven wire codecs for Antigravity `gemini_coder.Step` and Codex `rollout-*.jsonl`.

Invariants encoded in this module:
1. `STEP_WIRE_TABLE` is the single authoritative mapping between `steps.step_type`
   (`exa.cortex_pb.CortexStepType` enum values 14, 15, 23, 81, 82, 101, 132) and
   `gemini_coder.Step` protobuf oneof field tags (19, 20, 30, 93, 94, 114, 140),
   eliminating scattered `if/elif` chains across encoders and decoders
   (per `principle-model-the-domain`).
2. `TOOL_CODEC_TABLE` is the single authoritative registry mapping all 10
   `exa.cortex.core.tools.*` `google.protobuf.Any` type URLs to their canonical
   `CanonicalToolPayload` domain types and corresponding Codex `function_call` /
   `custom_tool_call` schemas.
3. All wire parsing and serialization happens strictly at the codec boundary
   (per `principle-boundary-discipline`); no protobuf tags or raw JSONL dicts leak
   into `TrajectoryLedger`, while unknown protobuf tags and host extensions are
   captured into `UnknownWireField` and `HostExtensionEnvelope`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Literal, Mapping, Sequence, Tuple

from .ledger import (
    CanonicalEvent,
    CanonicalToolPayload,
    RolloutLineNo,
    StepIndex,
    TrajectoryLedger,
    WorkspacePath,
    WorkspaceRootUri,
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
    """Protobuf field numbers inside `gemini_coder.Step` (`steps.step_payload`).

    Crucial invariant from Phase A grounding: `steps.step_type` is an enum integer
    (e.g. `14` for `USER_INPUT`), whereas `gemini_coder.Step` uses a distinct
    field tag in its `step` oneof (e.g. tag `19` for `Step.user_input`).
    """

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
        codex_fallback_tool_names=("custom_tool_call:exec", "shell"),
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


# ---------------------------------------------------------------------------
# Boundary WireEnvelopes (Private to Codec/Transport Layer)
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


# ---------------------------------------------------------------------------
# Pure Table-Driven Trajectory Codecs
# ---------------------------------------------------------------------------


class TrajectoryWireCodec:
    """Stateless, table-driven bidirectional translator between host wire formats and `TrajectoryLedger`.

    Invariants:
    - Uses `STEP_WIRE_TABLE` and `TOOL_CODEC_TABLE` for dispatch rather than ad-hoc branching.
    - Resolves the 1-to-N fanout between one `CortexStepPlannerResponse` (`step_type = 15`,
      wire tag `20`) and N `CortexStepGeneric` (`step_type = 132`, wire tag `140`) steps:
      - Decoding AGY -> Ledger splits `step_type = 15` into `ThinkingEvent` (if `thinking`
        field 3 or `raw_thinking` field 16 is present), `AssistantTextEvent` (if `response`
        field 1 is present), and `N` `ToolCallEvent` items (from `tool_calls` field 7), all
        sharing the parent `StepIndex`. Each following `step_type = 132` decodes into a
        `ToolResultEvent` correlated via `CortexStepMetadata.tool_call.id` (field 4).
      - Encoding Ledger -> AGY coalesces a consecutive `(ThinkingEvent?, AssistantTextEvent?,
        ToolCallEvent*)` sequence belonging to the same turn slice back into one
        `CortexStepPlannerResponse` (`step_type = 15`, tag `20`) followed by `N`
        `CortexStepGeneric` (`step_type = 132`, tag `140`) rows.
    """

    @staticmethod
    def decode_agy_steps_to_ledger(
        workspace_path: WorkspacePath,
        workspace_uri: WorkspaceRootUri,
        raw_steps: Sequence[AgyRawStepRow],
        *,
        explicit_title_override: str | None = None,
    ) -> TrajectoryLedger:
        """Parse raw Antigravity `steps` rows into an indexed `TrajectoryLedger`.

        Invariants:
        - Looks up each `row.step_type` in `STEP_WIRE_TABLE` and verifies that the
          inner `gemini_coder.Step` protobuf contains `spec.step_oneof_tag`.
        - Unpacks `CortexStepGeneric.payload` (`field 6`, `google.protobuf.Any`) via
          `TOOL_SPEC_BY_TYPE_URL`, falling back to `OpaqueToolPayload` if unknown.
        - Preserves any unrecognized protobuf field tags in `EventOriginAnchor.unknown_wire_fields`.
        """
        # TODO:
        # 1. Track current `TurnId` (incremented on each `CortexStepTypeCode.USER_INPUT`).
        # 2. For each `row` in `raw_steps`, look up `spec = STEP_WIRE_TABLE.get(row.step_type)`.
        # 3. Parse `metadata_blob` (`CortexStepMetadata`: timestamps, token counts, `tool_call` field 4,
        #    `tool_summary` field 30) and `step_payload_blob` (`gemini_coder.Step` tag `spec.step_oneof_tag`).
        # 4. Emit corresponding `CanonicalEvent` node(s) and return `TrajectoryLedger.from_events(...)`.
        raise NotImplementedError("TrajectoryWireCodec.decode_agy_steps_to_ledger stub")

    @staticmethod
    def encode_events_to_agy_steps(
        events: Sequence[CanonicalEvent],
        starting_step_idx: StepIndex,
    ) -> Tuple[AgyRawStepRow, ...]:
        """Serialize a slice of `CanonicalEvent` nodes into Antigravity `steps` rows.

        Invariants:
        - Coalesces adjacent `ThinkingEvent`, `AssistantTextEvent`, and `ToolCallEvent` items
          from the same planner turn-step into a single `AgyRawStepRow` with
          `step_type = 15` (`PLANNER_RESPONSE`) and wire tag `20` (`Step.planner_response`).
        - Emits one `AgyRawStepRow` with `step_type = 132` (`GENERIC`) and wire tag `140`
          (`Step.generic`) per `ToolResultEvent`, packing the typed tool payload into
          `google.protobuf.Any` (`field 6`) using `TOOL_CODEC_TABLE` and storing tool call
          identity in `CortexStepMetadata.tool_call` (`field 4`).
        - Re-emits any preserved `UnknownWireField` bytes on round-trip steps.
        - Assigns strictly contiguous `StepIndex` values starting at `starting_step_idx`.
        """
        # TODO:
        # 1. Group contiguous planner events (`thinking`, `assistant_text`, `tool_call`) before
        #    any `tool_result` or boundary event into one `CortexStepPlannerResponse` (tag 20).
        # 2. Encode each `ToolResultEvent` as `CortexStepGeneric` (tag 140) with `status = 3` (DONE).
        # 3. Encode `UserInputEvent` (type 14, tag 19), `CheckpointEvent` (type 23, tag 30),
        #    `TaskBoundaryEvent` (type 81/82, tag 93/94), `SystemEvent` (type 101, tag 114).
        raise NotImplementedError("TrajectoryWireCodec.encode_events_to_agy_steps stub")

    @staticmethod
    def decode_codex_rollout_to_ledger(
        workspace_path: WorkspacePath,
        workspace_uri: WorkspaceRootUri,
        raw_lines: Sequence[CodexRawRolloutLine],
        *,
        explicit_title_override: str | None = None,
    ) -> TrajectoryLedger:
        """Parse Codex `rollout-*.jsonl` lines into an indexed `TrajectoryLedger`.

        Invariants:
        - Maps `event_msg` (`task_started` / `task_complete`) to `TaskBoundaryEvent` and turn
          completion state (`has_inflight_turn = False` only when the trailing `task_started`
          is closed by `task_complete` or all tool calls have resolved with an assistant reply).
        - Maps `response_item` (`reasoning`/`magpie_reasoning`, `message`,
          `function_call`/`custom_tool_call`, `function_call_output`/`custom_tool_call_output`)
          to `ThinkingEvent` (storing `encrypted_content` in `HostExtensionEnvelope`),
          `UserInputEvent`/`AssistantTextEvent`, `ToolCallEvent`, and `ToolResultEvent`
          using `TOOL_SPEC_BY_CODEX_NAME`.
        """
        # TODO:
        # 1. Iterate `raw_lines`, parsing JSON at the boundary.
        # 2. Dispatch on record type (`session_meta`, `turn_context`, `world_state`,
        #    `event_msg`, `response_item`) and map tool calls via `TOOL_SPEC_BY_CODEX_NAME`.
        # 3. Construct and return `TrajectoryLedger.from_events(...)`.
        raise NotImplementedError("TrajectoryWireCodec.decode_codex_rollout_to_ledger stub")

    @staticmethod
    def encode_events_to_codex_rollout(
        events: Sequence[CanonicalEvent],
        starting_line_no: RolloutLineNo,
    ) -> Tuple[CodexRawRolloutLine, ...]:
        """Serialize a slice of `CanonicalEvent` nodes into Codex `rollout-*.jsonl` lines.

        Invariants:
        - Wraps each logical turn with `event_msg` (`task_started` / `task_complete`) and
          emits `response_item` lines for `ThinkingEvent` (`reasoning`), `ToolCallEvent`
          (`function_call` or `custom_tool_call` per `TOOL_CODEC_TABLE`), `ToolResultEvent`
          (`function_call_output` or `custom_tool_call_output` correlated by `call_id`),
          and `AssistantTextEvent` (`message`).
        """
        # TODO:
        # 1. Iterate `events` in ledger order, emitting `turn_context` / `event_msg` headers
        #    on turn boundaries.
        # 2. Look up `ToolCodecSpec` for each `ToolCallEvent` / `ToolResultEvent` and format
        #    the JSONL line with monotonically increasing `RolloutLineNo`.
        raise NotImplementedError("TrajectoryWireCodec.encode_events_to_codex_rollout stub")

    @staticmethod
    def decode_tool_payload(
        tool_name: str,
        arguments_json: str,
        any_type_url: str | None = None,
        any_value_bytes: bytes | None = None,
    ) -> CanonicalToolPayload:
        """Decode a tool call/result into a typed `CanonicalToolPayload` via `TOOL_CODEC_TABLE`."""
        # TODO:
        # Lookup `TOOL_SPEC_BY_TYPE_URL` or `TOOL_SPEC_BY_AGY_NAME` / `TOOL_SPEC_BY_CODEX_NAME`;
        # parse into the matching dataclass or return `OpaqueToolPayload`.
        raise NotImplementedError("TrajectoryWireCodec.decode_tool_payload stub")
