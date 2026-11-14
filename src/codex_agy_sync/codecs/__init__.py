"""Bidirectional trajectory and companion projection wire codecs for Antigravity and Codex."""

from __future__ import annotations

from ..payloads import serialize_tool_payload_arguments_json
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
    compress_zstd_rollout_bytes,
    decode_codex_rollout,
    decompress_zstd_rollout_bytes,
    encode_codex_rollout,
    iter_codex_rollout_lines,
    read_codex_rollout_lines,
)
from .rpc_thread import (
    decode_codex_rpc_thread_to_ledger,
    splice_rollout_completed_prefix,
)
from .schema_tags import (
    ACTIVE_STEP_STATUS_CODES,
    STEP_TAG_TO_SPEC,
    STEP_WIRE_TABLE,
    AgyRawStepRow,
    CodexRawRolloutLine,
    CortexStepTypeCode,
    GeminiStepWireTag,
    StepStatusCode,
    StepWireSpec,
    ToolCodecSpec,
)
from .steps import (
    KNOWN_STEP_TYPE_CODES,
    decode_agy_trajectory,
    encode_agy_trajectory,
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
)

__all__ = [
    "ACTIVE_STEP_STATUS_CODES",
    "AgyRawStepRow",
    "CodexRawRolloutLine",
    "CortexStepTypeCode",
    "GeminiStepWireTag",
    "KNOWN_STEP_TYPE_CODES",
    "STEP_TAG_TO_SPEC",
    "STEP_WIRE_TABLE",
    "StepStatusCode",
    "StepWireSpec",
    "TOOL_CODEC_TABLE",
    "TOOL_NAME_LOOKUP",
    "TOOL_SPEC_BY_PAYLOAD_KIND",
    "TOOL_SPEC_BY_TYPE_URL",
    "ToolCodecSpec",
    "compress_zstd_rollout_bytes",
    "decode_agy_trajectory",
    "decode_annotations_proto",
    "decode_cascade_summary_proto",
    "decode_codex_rollout",
    "decode_codex_rpc_thread_to_ledger",
    "decode_summaries_state_proto",
    "decode_tool_payload",
    "decode_trajectory_metadata_proto",
    "decompress_zstd_rollout_bytes",
    "encode_agy_trajectory",
    "encode_annotations_proto",
    "encode_cascade_summary_proto",
    "encode_codex_rollout",
    "encode_summaries_state_proto",
    "encode_tool_for_codex",
    "encode_tool_payload_to_any",
    "encode_trajectory_metadata_proto",
    "encode_workspace_info_proto",
    "iter_codex_rollout_lines",
    "parse_annotation_pbtxt_title",
    "read_codex_rollout_lines",
    "render_annotation_pbtxt",
    "render_transcript_row_jsonl",
    "serialize_agy_chat_tool_arguments",
    "serialize_tool_payload_arguments_json",
    "splice_rollout_completed_prefix",
]
