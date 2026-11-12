"""Bidirectional trajectory and companion projection wire codecs for Antigravity and Codex."""

from __future__ import annotations

from typing import Sequence, Tuple

from ..ledger import (
    CanonicalEvent,
    CanonicalToolPayload,
    RolloutLineNo,
    StepIndex,
    TrajectoryLedger,
    WorkspacePath,
    WorkspaceRootUri,
)
from .projections import (
    decode_annotations_proto,
    decode_cascade_summary_proto,
    decode_summaries_state_proto,
    decode_trajectory_metadata_proto,
    encode_annotations_proto,
    encode_cascade_summary_proto,
    encode_summaries_state_proto,
    encode_trajectory_metadata_proto,
    encode_workspace_info_proto,
    parse_annotation_pbtxt_title,
    render_annotation_pbtxt,
    render_transcript_row_jsonl,
)
from .rollout import (
    decompress_zstd_rollout_bytes,
    decode_codex_rollout,
    decode_codex_rollout_to_ledger,
    decode_codex_rpc_thread_to_ledger,
    encode_codex_rollout,
    encode_events_to_codex_rollout,
    encode_ledger_to_codex_rollout,
    read_codex_rollout_lines,
)
from .schema_tags import (
    STEP_TAG_TO_SPEC,
    STEP_WIRE_TABLE,
    AgyRawStepRow,
    CodexRawRolloutLine,
    CortexStepTypeCode,
    GeminiStepWireTag,
    StepWireSpec,
    ToolCodecSpec,
)
from .steps import (
    decode_agy_steps_to_ledger,
    decode_agy_trajectory,
    encode_agy_trajectory,
    encode_events_to_agy_steps,
    encode_ledger_to_agy_steps,
)
from .tools import (
    TOOL_CODEC_TABLE,
    TOOL_NAME_LOOKUP,
    TOOL_SPEC_BY_PAYLOAD_KIND,
    TOOL_SPEC_BY_TYPE_URL,
    decode_tool_payload,
    encode_tool_for_codex,
    encode_tool_payload_to_any,
    serialize_agy_chat_tool_arguments,
    serialize_tool_payload_arguments_json,
)


class TrajectoryWireCodec:
    """Stateless, table-driven bidirectional translator between host wire formats and `TrajectoryLedger`."""

    @staticmethod
    def decode_tool_payload(
        tool_name: str,
        arguments_json: str,
        any_type_url: str | None = None,
        any_value_bytes: bytes | None = None,
    ) -> CanonicalToolPayload:
        return decode_tool_payload(tool_name, arguments_json, any_type_url, any_value_bytes)

    @staticmethod
    def decode_agy_steps_to_ledger(
        workspace_path: WorkspacePath,
        workspace_uri: WorkspaceRootUri,
        raw_steps: Sequence[AgyRawStepRow],
        *,
        explicit_title_override: str | None = None,
    ) -> TrajectoryLedger:
        return decode_agy_trajectory(workspace_path, workspace_uri, raw_steps, explicit_title_override=explicit_title_override)

    @staticmethod
    def encode_events_to_agy_steps(
        events: Sequence[CanonicalEvent],
        starting_step_idx: StepIndex = StepIndex(0),
    ) -> Tuple[AgyRawStepRow, ...]:
        return encode_agy_trajectory(events, starting_step_idx)

    @staticmethod
    def encode_ledger_to_agy_steps(
        ledger_or_events: TrajectoryLedger | Sequence[CanonicalEvent],
        starting_step_idx: StepIndex = StepIndex(0),
    ) -> Tuple[AgyRawStepRow, ...]:
        return encode_ledger_to_agy_steps(ledger_or_events, starting_step_idx)

    @staticmethod
    def decode_codex_rollout_to_ledger(
        workspace_path: WorkspacePath,
        workspace_uri: WorkspaceRootUri,
        raw_lines: Sequence[CodexRawRolloutLine],
        *,
        explicit_title_override: str | None = None,
    ) -> TrajectoryLedger:
        return decode_codex_rollout(workspace_path, workspace_uri, raw_lines, explicit_title_override=explicit_title_override)

    @staticmethod
    def encode_events_to_codex_rollout(
        events: Sequence[CanonicalEvent],
        starting_line_no: RolloutLineNo = RolloutLineNo(1),
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> Tuple[CodexRawRolloutLine, ...]:
        return encode_codex_rollout(events, starting_line_no, session_id=session_id, cwd=cwd)

    @staticmethod
    def encode_ledger_to_codex_rollout(
        ledger_or_events: TrajectoryLedger | Sequence[CanonicalEvent],
        starting_line_no: RolloutLineNo = RolloutLineNo(1),
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> Tuple[CodexRawRolloutLine, ...]:
        return encode_ledger_to_codex_rollout(ledger_or_events, starting_line_no, session_id=session_id, cwd=cwd)


__all__ = [
    "AgyRawStepRow",
    "CodexRawRolloutLine",
    "CortexStepTypeCode",
    "GeminiStepWireTag",
    "STEP_TAG_TO_SPEC",
    "STEP_WIRE_TABLE",
    "TOOL_CODEC_TABLE",
    "TOOL_NAME_LOOKUP",
    "TOOL_SPEC_BY_PAYLOAD_KIND",
    "TOOL_SPEC_BY_TYPE_URL",
    "ToolCodecSpec",
    "StepWireSpec",
    "TrajectoryWireCodec",
    "decode_agy_steps_to_ledger",
    "decode_agy_trajectory",
    "decode_annotations_proto",
    "decode_cascade_summary_proto",
    "decode_codex_rollout",
    "decode_codex_rollout_to_ledger",
    "decode_codex_rpc_thread_to_ledger",
    "decode_summaries_state_proto",
    "decode_tool_payload",
    "decode_trajectory_metadata_proto",
    "decompress_zstd_rollout_bytes",
    "encode_agy_trajectory",
    "encode_annotations_proto",
    "encode_cascade_summary_proto",
    "encode_codex_rollout",
    "encode_events_to_agy_steps",
    "encode_events_to_codex_rollout",
    "encode_ledger_to_agy_steps",
    "encode_ledger_to_codex_rollout",
    "encode_summaries_state_proto",
    "encode_tool_for_codex",
    "encode_tool_payload_to_any",
    "encode_trajectory_metadata_proto",
    "encode_workspace_info_proto",
    "parse_annotation_pbtxt_title",
    "read_codex_rollout_lines",
    "render_annotation_pbtxt",
    "render_transcript_row_jsonl",
    "serialize_agy_chat_tool_arguments",
    "serialize_tool_payload_arguments_json",
]
