"""Companion summary, trajectory metadata, annotation pbtxt, and transcript wire codecs."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .._wire import (
    UnknownWireField,
    decode_timestamp_proto,
    encode_bytes_field,
    encode_string_field,
    encode_timestamp_proto,
    encode_unknown_fields,
    encode_varint,
    encode_varint_field,
    iter_wire_fields,
    parse_iso_to_seconds_nanos,
    parse_proto_fields,
    signed_int32,
    signed_int64,
)
from ..ledger import (
    AgyGuiSafeSource,
    AgyProjectId,
    AnnotationPbtxtProjection,
    CascadeId,
    CascadeSummaryProjection,
    SummariesStateProjection,
    TrajectoryId,
    TranscriptRowProjection,
    WorkspaceRootUri,
)
from .schema_tags import (
    AnnotationsFieldTag,
    MapEntryTag,
    SubagentMetaSpecTag,
    SummariesStateFieldTag,
    SummaryFieldTag,
    TrajectoryMetaFieldTag,
    WorkspaceInfoFieldTag,
)

_GUI_SOURCE_BY_ENUM: Mapping[int, AgyGuiSafeSource] = {
    src.proto_enum_value: src for src in AgyGuiSafeSource
}
_PBTXT_TITLE_RE = re.compile(r'title:\s*"((?:[^"\\]|\\.)*)"')


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
            f"last_user_view_time {{\n  seconds: {seconds}\n  nanos: {nanos}\n}}\n"
        )
    return f'title: "{escaped_title}"\nlast_user_view_time {{\n  seconds: {seconds}\n}}\n'


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
    """Encode `exa.cortex_pb.WorkspaceInfo` wire bytes."""
    text = str(uri)
    return (
        encode_string_field(WorkspaceInfoFieldTag.WORKSPACE_FOLDER_ABSOLUTE_URI, text)
        + encode_string_field(WorkspaceInfoFieldTag.GIT_ROOT_ABSOLUTE_URI, text)
    )


def encode_annotations_proto(proj: AnnotationPbtxtProjection) -> bytes:
    """Encode `exa.jetski_cortex_pb.ConversationAnnotations` wire bytes."""
    buf = bytearray()
    if proj.title:
        buf.extend(encode_string_field(AnnotationsFieldTag.TITLE, proj.title))
    if ts_bytes := encode_timestamp_proto(proj.last_user_view_time_iso):
        buf.extend(encode_bytes_field(AnnotationsFieldTag.LAST_USER_VIEW_TIME, ts_bytes))
    if proj.unknown_wire_fields:
        buf.extend(encode_unknown_fields(proj.unknown_wire_fields))
    return bytes(buf)


def decode_annotations_proto(
    raw_bytes: bytes,
    cascade_id: CascadeId,
    default_view_iso: str = "2026-01-01T00:00:00Z",
) -> AnnotationPbtxtProjection:
    """Decode `exa.jetski_cortex_pb.ConversationAnnotations` wire bytes preserving unknown fields."""
    known = {int(AnnotationsFieldTag.TITLE), int(AnnotationsFieldTag.LAST_USER_VIEW_TIME)}
    field_map, unknowns = parse_proto_fields(raw_bytes, known_tags=known)
    return AnnotationPbtxtProjection(
        cascade_id=cascade_id,
        title=_last_str(field_map, AnnotationsFieldTag.TITLE),
        last_user_view_time_iso=_last_timestamp(
            field_map, AnnotationsFieldTag.LAST_USER_VIEW_TIME, default_iso=default_view_iso
        ),
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
        buf.extend(encode_bytes_field(TrajectoryMetaFieldTag.WORKSPACES, encode_workspace_info_proto(uri)))
    if ts_bytes := encode_timestamp_proto(created_at_iso):
        buf.extend(encode_bytes_field(TrajectoryMetaFieldTag.CREATED_AT, ts_bytes))
    if parent_conversation_id:
        buf.extend(encode_string_field(TrajectoryMetaFieldTag.PARENT_CONVERSATION_ID, parent_conversation_id))
    if root_conversation_id:
        buf.extend(encode_string_field(TrajectoryMetaFieldTag.ROOT_CONVERSATION_ID, root_conversation_id))
    for uri in workspace_uris:
        buf.extend(encode_string_field(TrajectoryMetaFieldTag.WORKSPACE_URIS, str(uri)))
    if agent_type_name or agent_role:
        spec_bytes = (
            encode_string_field(SubagentMetaSpecTag.TYPE_NAME, agent_type_name)
            + encode_string_field(SubagentMetaSpecTag.ROLE, agent_role)
        )
        buf.extend(encode_bytes_field(TrajectoryMetaFieldTag.SUBAGENT_SPEC, spec_bytes))
    if nesting_depth != 0:
        buf.extend(encode_varint_field(TrajectoryMetaFieldTag.NESTING_DEPTH, nesting_depth))
    if str(project_id):
        buf.extend(encode_string_field(TrajectoryMetaFieldTag.PROJECT_ID, str(project_id)))
    if unknown_wire_fields:
        buf.extend(encode_unknown_fields(unknown_wire_fields))
    return bytes(buf)


def decode_trajectory_metadata_proto(raw_bytes: bytes) -> dict[str, Any]:
    """Decode `exa.cortex_pb.CortexTrajectoryMetadata` wire bytes preserving unknown fields."""
    known = {int(t) for t in TrajectoryMetaFieldTag}
    field_map, unknowns = parse_proto_fields(raw_bytes, known_tags=known)
    workspace_uris: list[str] = []
    for wt, val in field_map.get(TrajectoryMetaFieldTag.WORKSPACES, ()):
        if wt == 2 and isinstance(val, bytes):
            for sfn, swt, _, sraw in iter_wire_fields(val):
                if sfn == WorkspaceInfoFieldTag.WORKSPACE_FOLDER_ABSOLUTE_URI and swt == 2:
                    if (u := sraw.decode("utf-8", errors="replace").strip()) and u not in workspace_uris:
                        workspace_uris.append(u)
    for wt, val in field_map.get(TrajectoryMetaFieldTag.WORKSPACE_URIS, ()):
        if wt == 2 and isinstance(val, bytes):
            if (u := val.decode("utf-8", errors="replace").strip()) and u not in workspace_uris:
                workspace_uris.append(u)
    agent_type_name = agent_role = ""
    for wt, val in field_map.get(TrajectoryMetaFieldTag.SUBAGENT_SPEC, ()):
        if wt == 2 and isinstance(val, bytes):
            for sfn, swt, _, sraw in iter_wire_fields(val):
                if sfn == SubagentMetaSpecTag.TYPE_NAME and swt == 2:
                    agent_type_name = sraw.decode("utf-8", errors="replace").strip()
                elif sfn == SubagentMetaSpecTag.ROLE and swt == 2:
                    agent_role = sraw.decode("utf-8", errors="replace").strip()
    raw_depth = _last_varint(field_map, TrajectoryMetaFieldTag.NESTING_DEPTH)
    return {
        "project_id": _last_str(field_map, TrajectoryMetaFieldTag.PROJECT_ID, strip=True),
        "workspace_uris": workspace_uris,
        "created_at_iso": _last_timestamp(field_map, TrajectoryMetaFieldTag.CREATED_AT),
        "parent_conversation_id": _last_str(field_map, TrajectoryMetaFieldTag.PARENT_CONVERSATION_ID, strip=True),
        "root_conversation_id": _last_str(field_map, TrajectoryMetaFieldTag.ROOT_CONVERSATION_ID, strip=True),
        "agent_type_name": agent_type_name,
        "agent_role": agent_role,
        "nesting_depth": max(0, signed_int32(raw_depth)) if raw_depth is not None else 0,
        "unknown_wire_fields": tuple(unknowns),
    }


def encode_cascade_summary_proto(proj: CascadeSummaryProjection) -> bytes:
    """Encode `exa.jetski_cortex_pb.CascadeTrajectorySummary` wire bytes."""
    buf = bytearray()
    if proj.title:
        buf.extend(encode_string_field(SummaryFieldTag.SUMMARY, proj.title))
    if proj.step_count > 0:
        buf.extend(encode_varint_field(SummaryFieldTag.STEP_COUNT, proj.step_count))
    if mod_ts := encode_timestamp_proto(proj.updated_at_iso):
        buf.extend(encode_bytes_field(SummaryFieldTag.LAST_MODIFIED_TIME, mod_ts))
    if traj_id_str := (str(proj.trajectory_id) if proj.trajectory_id is not None else str(proj.cascade_id)):
        buf.extend(encode_string_field(SummaryFieldTag.TRAJECTORY_ID, traj_id_str))
    if proj.status > 0:
        buf.extend(encode_varint_field(SummaryFieldTag.STATUS, proj.status))
    if created_ts := encode_timestamp_proto(proj.created_at_iso):
        buf.extend(encode_bytes_field(SummaryFieldTag.CREATED_TIME, created_ts))
    for uri in proj.workspace_uris:
        buf.extend(encode_bytes_field(SummaryFieldTag.WORKSPACES, encode_workspace_info_proto(uri)))
    if user_ts := encode_timestamp_proto(proj.last_user_input_time_iso or proj.updated_at_iso):
        buf.extend(encode_bytes_field(SummaryFieldTag.LAST_USER_INPUT_TIME, user_ts))
    if annot_bytes := encode_annotations_proto(proj.annotations):
        buf.extend(encode_bytes_field(SummaryFieldTag.ANNOTATIONS, annot_bytes))
    if proj.last_user_input_step_index is not None and proj.last_user_input_step_index >= 0:
        buf.extend(encode_varint_field(SummaryFieldTag.LAST_USER_INPUT_STEP_INDEX, proj.last_user_input_step_index))
    traj_meta_bytes = encode_trajectory_metadata_proto(
        project_id=proj.project_id, workspace_uris=proj.workspace_uris, created_at_iso=proj.created_at_iso,
        nesting_depth=proj.nesting_depth, parent_conversation_id=str(proj.parent_cascade_id or ""),
        root_conversation_id=str(proj.root_cascade_id or ""), agent_type_name=proj.agent_type_name,
        agent_role=proj.agent_role, unknown_wire_fields=proj.trajectory_metadata_unknown_fields,
    )
    if traj_meta_bytes:
        buf.extend(encode_bytes_field(SummaryFieldTag.TRAJECTORY_METADATA, traj_meta_bytes))
    if proj.source.proto_enum_value != 0:
        buf.extend(encode_varint_field(SummaryFieldTag.TRAJECTORY_SOURCE, proj.source.proto_enum_value))
    if proj.trajectory_type > 0:
        buf.extend(encode_varint_field(SummaryFieldTag.TRAJECTORY_TYPE, proj.trajectory_type))
    if proj.unknown_wire_fields:
        buf.extend(encode_unknown_fields(proj.unknown_wire_fields))
    return bytes(buf)


def decode_cascade_summary_proto(
    raw_bytes: bytes,
    fallback_cascade_id: CascadeId | None = None,
) -> CascadeSummaryProjection:
    """Decode `exa.jetski_cortex_pb.CascadeTrajectorySummary` wire bytes faithfully."""
    known = {int(t) for t in SummaryFieldTag}
    field_map, unknowns = parse_proto_fields(raw_bytes, known_tags=known)
    summary_str = _last_str(field_map, SummaryFieldTag.SUMMARY)
    traj_id_str = _last_str(field_map, SummaryFieldTag.TRAJECTORY_ID, strip=True)
    updated_iso = _last_timestamp(field_map, SummaryFieldTag.LAST_MODIFIED_TIME)
    created_iso = _last_timestamp(field_map, SummaryFieldTag.CREATED_TIME)
    last_user_iso = _last_timestamp(field_map, SummaryFieldTag.LAST_USER_INPUT_TIME)
    effective_cid = fallback_cascade_id or CascadeId(traj_id_str or "unknown-cascade")

    ws_uris: list[WorkspaceRootUri] = []
    for wt, val in field_map.get(SummaryFieldTag.WORKSPACES, ()):
        if wt == 2 and isinstance(val, bytes):
            for sfn, swt, _, sraw in iter_wire_fields(val):
                if sfn == WorkspaceInfoFieldTag.WORKSPACE_FOLDER_ABSOLUTE_URI and swt == 2:
                    if (u_str := sraw.decode("utf-8", errors="replace").strip()) and WorkspaceRootUri(u_str) not in ws_uris:
                        ws_uris.append(WorkspaceRootUri(u_str))

    tmeta = decode_trajectory_metadata_proto(b"")
    for wt, val in field_map.get(SummaryFieldTag.TRAJECTORY_METADATA, ()):
        if wt == 2 and isinstance(val, bytes):
            tmeta = decode_trajectory_metadata_proto(val)
            for u_str in tmeta["workspace_uris"]:
                if u_str and WorkspaceRootUri(u_str) not in ws_uris:
                    ws_uris.append(WorkspaceRootUri(u_str))

    eff_created = created_iso or str(tmeta["created_at_iso"] or "")
    eff_updated = updated_iso or last_user_iso or eff_created
    annot = AnnotationPbtxtProjection(cascade_id=effective_cid, title="", last_user_view_time_iso=eff_updated)
    for wt, val in field_map.get(SummaryFieldTag.ANNOTATIONS, ()):
        if wt == 2 and isinstance(val, bytes):
            annot = decode_annotations_proto(val, effective_cid, default_view_iso=eff_updated)

    source = AgyGuiSafeSource.UNSPECIFIED
    if (raw_source := _last_varint(field_map, SummaryFieldTag.TRAJECTORY_SOURCE)) is not None:
        if (mapped := _GUI_SOURCE_BY_ENUM.get(raw_source)) is not None:
            source = mapped
        else:
            unknowns.append(
                UnknownWireField(field_tag=int(SummaryFieldTag.TRAJECTORY_SOURCE), wire_type=0, raw_bytes=encode_varint(raw_source))
            )

    resolved_title = annot.title or summary_str
    agent_type, agent_role = str(tmeta["agent_type_name"] or ""), str(tmeta["agent_role"] or "")
    fields_dict: dict[str, Any] = {
        "cascade_id": effective_cid, "project_id": AgyProjectId(str(tmeta["project_id"] or "")),
        "workspace_uris": tuple(ws_uris), "source": source,
        "title": resolved_title, "preview": summary_str or resolved_title,
        "step_count": _last_varint(field_map, SummaryFieldTag.STEP_COUNT) or 0,
        "created_at_iso": eff_created, "updated_at_iso": eff_updated, "annotations": annot,
        "nesting_depth": int(tmeta["nesting_depth"]),
        "parent_cascade_id": CascadeId(tmeta["parent_conversation_id"]) if tmeta["parent_conversation_id"] else None,
        "root_cascade_id": CascadeId(tmeta["root_conversation_id"]) if tmeta["root_conversation_id"] else None,
        "agent_name": agent_role or agent_type, "agent_type_name": agent_type, "agent_role": agent_role,
        "status": _last_varint(field_map, SummaryFieldTag.STATUS) or 0,
        "trajectory_id": TrajectoryId(traj_id_str) if traj_id_str else None,
        "trajectory_type": _last_varint(field_map, SummaryFieldTag.TRAJECTORY_TYPE) or 0,
        "last_user_input_time_iso": last_user_iso,
        "last_user_input_step_index": _last_varint(field_map, SummaryFieldTag.LAST_USER_INPUT_STEP_INDEX),
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
        entry = (
            encode_string_field(MapEntryTag.KEY, str(cid))
            + encode_bytes_field(MapEntryTag.VALUE, summary_proj.summary_proto_bytes)
        )
        buf.extend(encode_bytes_field(SummariesStateFieldTag.SUMMARIES, entry))
    for path_key in sorted(state.file_mod_times.keys()):
        mtime_val = state.file_mod_times[path_key]
        entry = (
            encode_string_field(MapEntryTag.KEY, path_key)
            + encode_varint_field(MapEntryTag.VALUE, int(mtime_val))
        )
        buf.extend(encode_bytes_field(SummariesStateFieldTag.FILE_MOD_TIMES, entry))
    if state.unknown_wire_fields:
        buf.extend(encode_unknown_fields(state.unknown_wire_fields))
    return bytes(buf)


def decode_summaries_state_proto(raw_bytes: bytes) -> SummariesStateProjection:
    """Decode `jetbox_summaries_pb.SummariesState` wire bytes faithfully."""
    known = {int(SummariesStateFieldTag.SUMMARIES), int(SummariesStateFieldTag.FILE_MOD_TIMES)}
    field_map, unknowns = parse_proto_fields(raw_bytes, known_tags=known)
    summaries: dict[CascadeId, CascadeSummaryProjection] = {}
    file_mod_times: dict[str, int] = {}

    for wt, payload in field_map.get(SummariesStateFieldTag.SUMMARIES, ()):
        if wt == 2 and isinstance(payload, bytes):
            cid_str, sum_bytes = "", b""
            for efn, ewt, _, epayload in iter_wire_fields(payload):
                if efn == MapEntryTag.KEY and ewt == 2:
                    cid_str = epayload.decode("utf-8", errors="replace")
                elif efn == MapEntryTag.VALUE and ewt == 2:
                    sum_bytes = bytes(epayload)
            if cid_str:
                cid = CascadeId(cid_str)
                summaries[cid] = decode_cascade_summary_proto(sum_bytes, fallback_cascade_id=cid)

    for wt, payload in field_map.get(SummariesStateFieldTag.FILE_MOD_TIMES, ()):
        if wt == 2 and isinstance(payload, bytes):
            pkey, mval = "", 0
            for efn, ewt, eval_int, epayload in iter_wire_fields(payload):
                if efn == MapEntryTag.KEY and ewt == 2:
                    pkey = epayload.decode("utf-8", errors="replace")
                elif efn == MapEntryTag.VALUE and ewt == 0:
                    mval = signed_int64(eval_int)
            if pkey:
                file_mod_times[pkey] = mval

    return SummariesStateProjection(
        summaries=summaries,
        file_mod_times=file_mod_times,
        unknown_wire_fields=tuple(unknowns),
    )
