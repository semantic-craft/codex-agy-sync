//! Pure-Rust protobuf wire/step/summary codecs and streaming Codex rollout `.jsonl`/`.jsonl.zst` codecs.

pub mod error;
pub mod projections;
pub mod rollout;
pub mod rpc_thread;
pub mod schema_tags;
pub mod steps;
pub mod tool_codecs;
pub mod wire;

pub use error::CodecError;
pub use projections::{
    DecodedTrajectoryMetadata, TrajectoryMetadataEncodeParams, decode_annotations_proto,
    decode_cascade_summary_proto, decode_summaries_state_proto, decode_trajectory_metadata_proto,
    encode_annotations_proto, encode_cascade_summary_proto, encode_summaries_state_proto,
    encode_trajectory_metadata_proto, encode_workspace_info_proto, parse_annotation_pbtxt_title,
    render_annotation_pbtxt, render_transcript_row_jsonl,
};
pub use rollout::{
    compress_zstd_rollout_bytes, decode_codex_rollout, decompress_zstd_rollout_bytes,
    encode_codex_rollout, read_codex_rollout_lines,
};
pub use rpc_thread::{decode_codex_rpc_thread_to_ledger, splice_rollout_completed_prefix};
pub use schema_tags::{
    AgyRawStepRow, CodexRawRolloutLine, CortexStepTypeCode, GeminiStepWireTag, STEP_WIRE_TABLE,
    StepWireSpec, find_step_spec_by_tag, find_step_spec_by_type, step_status,
};
pub use steps::{bind_agy_step, decode_agy_trajectory, encode_agy_trajectory};
pub use tool_codecs::{
    CodexCallKind, TOOL_CODEC_TABLE, ToolCodecSpec, apply_tool_result_meta, decode_any_envelope,
    decode_tool_payload, decode_unified_diff_block, encode_any_envelope, encode_tool_for_codex,
    encode_tool_payload_to_any, encode_unified_diff_block, extract_tool_result_meta,
    find_tool_spec_by_name, serialize_agy_chat_tool_arguments,
};
pub use wire::{
    FieldMap, UnknownWireField, WireField, WireValue, decode_timestamp_proto, encode_bytes_field,
    encode_string_field, encode_timestamp_proto, encode_unknown_fields, encode_uvarint_field,
    encode_varint, encode_varint_field, fm_bytes, fm_last_str, fm_last_str_fallback,
    fm_last_varint, fm_opt_str, fm_strs, iter_wire_fields, parse_proto_fields, read_varint,
    signed_int32, signed_int64,
};
