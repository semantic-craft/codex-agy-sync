"""Antigravity `gemini_coder.Step` trajectory decoder and encoder driven by `STEP_WIRE_TABLE`."""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Mapping, Sequence, Tuple

from .._wire import (
    UnknownWireField,
    decode_timestamp_proto,
    encode_bytes_field,
    encode_string_field,
    encode_timestamp_proto,
    encode_unknown_fields,
    encode_varint_field,
    iter_wire_fields,
    parse_proto_fields,
    signed_int32,
    signed_int64,
)
from ..ledger import (
    INTERRUPTED_TOOL_OUTPUT, AssistantTextEvent, CanonicalEvent, CheckpointEvent,
    EventOriginAnchor, HostExtensionEnvelope, HostKind, MediaAttachment, StepIndex,
    SystemEvent, TaskBoundaryEvent, ThinkingEvent, TokenUsageAccounting, ToolCallEvent,
    ToolCallId, ToolResultEvent, TrajectoryLedger, TurnId, UserInputEvent,
    WorkspacePath, WorkspaceRootUri,
)
from .schema_tags import (
    ACTIVE_STEP_STATUS_CODES, STEP_TAG_TO_SPEC, STEP_WIRE_TABLE, AgyRawStepRow,
    ChatToolCallTag, CheckpointTag, ContextScopeItemTag, CortexStepTypeCode,
    DurationProtoTag, ErrorDetailsTag, ErrorMessageStepTag, GeminiStepWireTag,
    GenericResultTag, GenericStepTag, MapEntryTag, MediaTag, ModelUsageTag,
    NotifyUserTag, PathScopeItemTag, PlannerResponseTag, StepFieldTag, StepMetaTag,
    StepSourceCode, StepStatusCode, StepWireSpec, SystemMessageTag, TaskBoundaryTag,
    TextOrScopeItemTag, UserInputTag,
)
from .tool_payloads import decode_any_envelope
from .tools import (
    TOOL_SPEC_BY_PAYLOAD_KIND, decode_tool_payload, encode_tool_payload_to_any,
    safe_json_dict, serialize_agy_chat_tool_arguments,
)

_KNOWN_STEP_TAGS = frozenset({1, 4, 5, 6, 31, 56, 69, 133, 147, 148, 155, 158, 159} | {int(t) for t in GeminiStepWireTag})


def _decode_duration_ms(raw: bytes) -> int:
    seconds = nanos = 0
    for fn, wt, ival, _ in iter_wire_fields(raw):
        if fn == DurationProtoTag.SECONDS and wt == 0:
            seconds = signed_int64(ival)
        elif fn == DurationProtoTag.NANOS and wt == 0:
            nanos = signed_int32(ival)
    return int(seconds * 1000 + (nanos // 1_000_000))


def _encode_duration_proto(duration_ms: int) -> bytes:
    if duration_ms <= 0:
        return b""
    seconds, nanos = duration_ms // 1000, (duration_ms % 1000) * 1_000_000
    return (encode_varint_field(DurationProtoTag.SECONDS, seconds) if seconds else b"") + (
        encode_varint_field(DurationProtoTag.NANOS, nanos) if nanos else b""
    )


def _decode_chat_tool_call(buf: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {"id": "", "name": "", "arguments_json": "{}", "thinking_signature": None}
    for fn, wt, _, raw in iter_wire_fields(buf):
        if fn == ChatToolCallTag.ID and wt == 2:
            out["id"] = raw.decode("utf-8", errors="replace")
        elif fn == ChatToolCallTag.NAME and wt == 2:
            out["name"] = raw.decode("utf-8", errors="replace")
        elif fn == ChatToolCallTag.ARGUMENTS_JSON and wt == 2:
            out["arguments_json"] = raw.decode("utf-8", errors="replace")
        elif fn == ChatToolCallTag.THINKING_SIGNATURE and wt == 2:
            out["thinking_signature"] = bytes(raw)
    return out


def _encode_chat_tool_call(call_id: str, name: str, arguments_json: str, thinking_signature: bytes | None = None) -> bytes:
    return (
        encode_string_field(ChatToolCallTag.ID, call_id)
        + encode_string_field(ChatToolCallTag.NAME, name)
        + encode_string_field(ChatToolCallTag.ARGUMENTS_JSON, arguments_json)
        + (encode_bytes_field(ChatToolCallTag.THINKING_SIGNATURE, thinking_signature) if thinking_signature else b"")
    )


def _decode_step_metadata(buf: bytes) -> tuple[dict[str, Any], bytes]:
    known_tags = set(range(1, int(StepMetaTag.MAX_KNOWN_TAG) + 1))
    meta: dict[str, Any] = {
        "created_at_iso": "2026-01-01T00:00:00Z", "source": 0, "tool_call": None, "tool_summary": "",
        "generator_model": None, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "preserved_fields": [],
    }
    extra_fields: list[UnknownWireField] = []
    for fn, wt, ival, raw in iter_wire_fields(buf):
        if fn not in known_tags:
            extra_fields.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
        elif fn == StepMetaTag.CREATED_AT and wt == 2:
            meta["created_at_iso"] = decode_timestamp_proto(raw)
        elif fn == StepMetaTag.SOURCE and wt == 0:
            meta["source"] = ival
        elif fn == StepMetaTag.TOOL_CALL and wt == 2:
            meta["tool_call"] = _decode_chat_tool_call(raw)
        elif fn == StepMetaTag.MODEL_USAGE and wt == 2:
            for ufn, uwt, uival, _ in iter_wire_fields(raw):
                if ufn == ModelUsageTag.INPUT_TOKENS and uwt == 0:
                    meta["input_tokens"] = uival
                elif ufn == ModelUsageTag.OUTPUT_TOKENS and uwt == 0:
                    meta["output_tokens"] = uival
                elif ufn == ModelUsageTag.CACHE_READ_TOKENS and uwt == 0:
                    meta["cache_read_tokens"] = uival
        elif fn == StepMetaTag.GENERATOR_MODEL and wt == 0:
            meta["generator_model"] = str(ival)
        elif fn == StepMetaTag.TOOL_SUMMARY and wt == 2:
            meta["tool_summary"] = raw.decode("utf-8", errors="replace")
        else:
            uf = UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw)
            meta["preserved_fields"].append(uf)
            extra_fields.append(uf)
    return meta, encode_unknown_fields(extra_fields)


def _encode_step_metadata(
    created_at_iso: str, source_enum: int, *, tool_call: Mapping[str, Any] | None = None,
    tool_summary: str = "", token_usage: TokenUsageAccounting | None = None,
    generator_model: str | None = None, extra_wire_bytes: bytes = b"",
) -> bytes:
    buf = bytearray()
    if ts_bytes := encode_timestamp_proto(created_at_iso):
        buf.extend(encode_bytes_field(StepMetaTag.CREATED_AT, ts_bytes))
    if source_enum:
        buf.extend(encode_varint_field(StepMetaTag.SOURCE, source_enum))
    if tool_call is not None:
        buf.extend(encode_bytes_field(StepMetaTag.TOOL_CALL, _encode_chat_tool_call(str(tool_call.get("id", "")), str(tool_call.get("name", "")), str(tool_call.get("arguments_json", "{}")), tool_call.get("thinking_signature"))))
    if token_usage is not None and (token_usage.input_tokens or token_usage.output_tokens or token_usage.cache_read_tokens):
        usage_buf = (
            (encode_varint_field(ModelUsageTag.INPUT_TOKENS, token_usage.input_tokens) if token_usage.input_tokens else b"")
            + (encode_varint_field(ModelUsageTag.OUTPUT_TOKENS, token_usage.output_tokens) if token_usage.output_tokens else b"")
            + (encode_varint_field(ModelUsageTag.CACHE_READ_TOKENS, token_usage.cache_read_tokens) if token_usage.cache_read_tokens else b"")
        )
        buf.extend(encode_bytes_field(StepMetaTag.MODEL_USAGE, usage_buf))
    if generator_model and generator_model.isdigit():
        buf.extend(encode_varint_field(StepMetaTag.GENERATOR_MODEL, int(generator_model)))
    if tool_summary:
        buf.extend(encode_string_field(StepMetaTag.TOOL_SUMMARY, tool_summary))
    if extra_wire_bytes:
        buf.extend(extra_wire_bytes)
    return bytes(buf)


def _decode_scope_item_bytes(ctx_bytes: bytes) -> str:
    fields = iter_wire_fields(ctx_bytes)
    if len(fields) == 1 and fields[0][0] == ContextScopeItemTag.FILE and fields[0][1] == 2:
        pfields = iter_wire_fields(fields[0][3])
        if len(pfields) == 1 and pfields[0][0] == PathScopeItemTag.ABSOLUTE_URI and pfields[0][1] == 2:
            uri = pfields[0][3].decode("utf-8", errors="replace")
            if uri and not uri.startswith("ctxpb:") and encode_bytes_field(ContextScopeItemTag.FILE, encode_string_field(PathScopeItemTag.ABSOLUTE_URI, uri)) == ctx_bytes:
                return uri
    return f"ctxpb:{ctx_bytes.hex()}"


def _encode_scope_item_bytes(scope_str: str) -> bytes:
    if scope_str.startswith("ctxpb:"):
        try:
            return bytes.fromhex(scope_str[6:])
        except ValueError:
            pass
    return encode_bytes_field(ContextScopeItemTag.FILE, encode_string_field(PathScopeItemTag.ABSOLUTE_URI, scope_str))


def _make_agy_exts(sub_unknowns: Sequence[UnknownWireField], meta_extra: bytes, extra: Sequence[HostExtensionEnvelope] = ()) -> Tuple[HostExtensionEnvelope, ...]:
    out: list[HostExtensionEnvelope] = []
    if sub_unknowns:
        out.append(HostExtensionEnvelope(host=HostKind.ANTIGRAVITY, extension_type="agy_submessage_unknowns", raw_payload=encode_unknown_fields(sub_unknowns)))
    if meta_extra:
        out.append(HostExtensionEnvelope(host=HostKind.ANTIGRAVITY, extension_type="agy_metadata_extra", raw_payload=meta_extra))
    out.extend(extra)
    return tuple(out)


def _ext_payload(anchor: EventOriginAnchor, ext_type: str) -> bytes:
    for ext in anchor.host_extensions:
        if ext.extension_type == ext_type and ext.raw_payload:
            return bytes(ext.raw_payload)
    return b""


def _fm_last_str(fm: Mapping[int, Sequence[tuple[int, int | bytes]]], tag: int) -> str:
    out = ""
    for wt, val in fm.get(int(tag), ()):
        if wt == 2 and isinstance(val, bytes):
            out = val.decode("utf-8", errors="replace")
    return out


def _decode_user_input(row: AgyRawStepRow, oneof_bytes: bytes, turn_id: TurnId, ts_iso: str, step_unknowns: Sequence[UnknownWireField], meta_extra: bytes) -> UserInputEvent:
    fm, ui_unknowns = parse_proto_fields(oneof_bytes, known_tags=range(1, int(UserInputTag.CLIENT_MESSAGE_ID) + 1))
    item_texts: list[str] = []
    scope_uris: list[str] = []
    for wt, raw in fm.get(int(UserInputTag.ITEMS), ()):
        if wt == 2 and isinstance(raw, bytes):
            for ifn, iwt, _, iraw in iter_wire_fields(raw):
                if ifn == TextOrScopeItemTag.TEXT and iwt == 2:
                    item_texts.append(iraw.decode("utf-8", errors="replace"))
                elif ifn == TextOrScopeItemTag.ITEM and iwt == 2:
                    scope_uris.append(_decode_scope_item_bytes(iraw))
    media_list: list[MediaAttachment] = []
    for wt, raw in fm.get(int(UserInputTag.MEDIA), ()):
        if wt == 2 and isinstance(raw, bytes):
            mfm, _ = parse_proto_fields(raw)
            media_list.append(MediaAttachment(mime_type=_fm_last_str(mfm, MediaTag.MIME_TYPE), uri=_fm_last_str(mfm, MediaTag.URI)))
    client_msg_id = _fm_last_str(fm, UserInputTag.CLIENT_MESSAGE_ID) or None
    final_query = _fm_last_str(fm, UserInputTag.USER_RESPONSE) or "\n".join(t for t in item_texts if t) or _fm_last_str(fm, UserInputTag.QUERY)
    anchor = EventOriginAnchor(
        origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso=ts_iso, agy_step_index=row.idx,
        client_message_id=client_msg_id, unknown_wire_fields=tuple(step_unknowns), host_extensions=_make_agy_exts(ui_unknowns, meta_extra),
    )
    return UserInputEvent(kind="user_input", anchor=anchor, query_text=final_query, scope_items=tuple(scope_uris), media=tuple(media_list))


def _decode_planner_response(
    row: AgyRawStepRow, oneof_bytes: bytes, turn_id: TurnId, ts_iso: str, meta_dict: Mapping[str, Any],
    token_usage: TokenUsageAccounting, step_unknowns: Sequence[UnknownWireField], meta_extra: bytes,
    events: list[CanonicalEvent], call_index_by_id: dict[ToolCallId, int], unmatched_synthetic: list[ToolCallId],
) -> None:
    fm, pr_unknowns = parse_proto_fields(oneof_bytes, known_tags=range(1, int(PlannerResponseTag.MAX_KNOWN_TAG) + 1))
    resp_text = _fm_last_str(fm, PlannerResponseTag.RESPONSE) or _fm_last_str(fm, PlannerResponseTag.MODIFIED_RESPONSE)
    thinking_text = _fm_last_str(fm, PlannerResponseTag.THINKING)
    raw_thinking = _fm_last_str(fm, PlannerResponseTag.RAW_THINKING) if PlannerResponseTag.RAW_THINKING in fm else None
    sig_str = _fm_last_str(fm, PlannerResponseTag.SIGNATURE)
    thinking_sig = next((val for wt, val in reversed(fm.get(int(PlannerResponseTag.THINKING_SIGNATURE), ())) if wt == 2 and isinstance(val, bytes)), None)
    if thinking_sig is None and sig_str:
        thinking_sig = sig_str.encode("utf-8")
    dur_raw = next((val for wt, val in reversed(fm.get(int(PlannerResponseTag.THINKING_DURATION), ())) if wt == 2 and isinstance(val, bytes)), None)
    stop_val = next((str(val) for wt, val in reversed(fm.get(int(PlannerResponseTag.STOP_REASON), ())) if wt == 0 and isinstance(val, int)), None)
    tool_calls_raw = [_decode_chat_tool_call(val) for wt, val in fm.get(int(PlannerResponseTag.TOOL_CALLS), ()) if wt == 2 and isinstance(val, bytes)]

    usage_with_dur = dataclasses.replace(token_usage, thinking_duration_ms=_decode_duration_ms(dur_raw) if dur_raw is not None else None)
    base_anchor = EventOriginAnchor(
        origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso=ts_iso, agy_step_index=row.idx,
        unknown_wire_fields=tuple(step_unknowns), host_extensions=_make_agy_exts(pr_unknowns, meta_extra),
    )
    emitted = False
    if thinking_text or raw_thinking or thinking_sig:
        events.append(ThinkingEvent(kind="thinking", anchor=base_anchor, summary_text=thinking_text or (raw_thinking or ""), raw_thinking=raw_thinking, thinking_signature=thinking_sig, token_usage=usage_with_dur))
        emitted = True
    if resp_text or (not emitted and not tool_calls_raw):
        msg_id = _fm_last_str(fm, PlannerResponseTag.MESSAGE_ID) or None
        events.append(AssistantTextEvent(kind="assistant_text", anchor=base_anchor, text=resp_text, message_id=msg_id, stop_reason=stop_val, generator_model=meta_dict["generator_model"], token_usage=usage_with_dur))

    for tc in tool_calls_raw:
        raw_cid = str(tc.get("id") or "").strip()
        cid = ToolCallId(raw_cid) if raw_cid else ToolCallId(f"call-step-{int(row.idx)}-{len(events)}")
        if not raw_cid:
            unmatched_synthetic.append(cid)
        tname, args_json = tc.get("name") or "unknown_tool", tc.get("arguments_json") or "{}"
        args_obj = safe_json_dict(args_json)
        summary_lbl = str(args_obj.get("toolSummary") or args_obj.get("tool_summary") or meta_dict["tool_summary"] or tname)
        call_index_by_id[cid] = len(events)
        events.append(ToolCallEvent(kind="tool_call", anchor=base_anchor, call_id=cid, tool_name=tname, tool_summary=summary_lbl, payload=decode_tool_payload(tname, args_json), thinking_signature=tc.get("thinking_signature")))


def _decode_generic_step(
    row: AgyRawStepRow, oneof_bytes: bytes, turn_id: TurnId, ts_iso: str, meta_dict: Mapping[str, Any],
    step_unknowns: Sequence[UnknownWireField], meta_extra: bytes,
    events: list[CanonicalEvent], call_index_by_id: dict[ToolCallId, int], unmatched_synthetic: list[ToolCallId],
) -> None:
    tc_meta = meta_dict["tool_call"] or {}
    raw_cid = str(tc_meta.get("id") or "").strip()
    cid = ToolCallId(raw_cid) if raw_cid else (unmatched_synthetic.pop(0) if unmatched_synthetic else ToolCallId(f"call-generic-{int(row.idx)}"))
    tname, args_json = tc_meta.get("name") or "unknown_tool", tc_meta.get("arguments_json") or "{}"
    tool_summary = str(meta_dict["tool_summary"] or tname)

    fm, gen_unknowns = parse_proto_fields(oneof_bytes, known_tags=(int(GenericStepTag.ARGS), int(GenericStepTag.RESULT)))
    args_map: dict[str, str] = {}
    for wt, raw in fm.get(int(GenericStepTag.ARGS), ()):
        if wt == 2 and isinstance(raw, bytes):
            efm, _ = parse_proto_fields(raw)
            if k_str := _fm_last_str(efm, MapEntryTag.KEY):
                args_map[k_str] = _fm_last_str(efm, MapEntryTag.VALUE)
    result_text, any_url, any_val, full_uri = "", None, None, None
    for wt, raw in fm.get(int(GenericStepTag.RESULT), ()):
        if wt == 2 and isinstance(raw, bytes):
            rfm, _ = parse_proto_fields(raw)
            result_text = _fm_last_str(rfm, GenericResultTag.RESULT) or result_text
            full_uri = _fm_last_str(rfm, GenericResultTag.FULL_OUTPUT_URI) or full_uri
            for pwt, praw in rfm.get(int(GenericResultTag.PAYLOAD), ()):
                if pwt == 2 and isinstance(praw, bytes):
                    any_url, any_val = decode_any_envelope(praw)

    if (not args_json or args_json == "{}") and args_map:
        args_json = json.dumps(args_map, ensure_ascii=False, sort_keys=True)
    rich_payload = decode_tool_payload(tname, args_json, any_url, any_val)
    if cid in call_index_by_id and isinstance(events[call_pos := call_index_by_id[cid]], ToolCallEvent):
        prior_call = events[call_pos]
        assert isinstance(prior_call, ToolCallEvent)
        upgraded_summary = tool_summary if (tool_summary and tool_summary != tname) else prior_call.tool_summary
        events[call_pos] = dataclasses.replace(prior_call, tool_summary=upgraded_summary, payload=rich_payload, thinking_signature=prior_call.thinking_signature or tc_meta.get("thinking_signature"))

    anchor = EventOriginAnchor(
        origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso=ts_iso, agy_step_index=row.idx,
        unknown_wire_fields=tuple(step_unknowns), host_extensions=_make_agy_exts(gen_unknowns, meta_extra),
    )
    is_err = row.status in (int(StepStatusCode.ERROR), int(StepStatusCode.CANCELED), int(StepStatusCode.INTERRUPTED))
    events.append(ToolResultEvent(kind="tool_result", anchor=anchor, call_id=cid, tool_name=tname, output_text=result_text, is_error=is_err, payload=rich_payload, full_output_uri=full_uri))


def _decode_error_message_step(row: AgyRawStepRow, oneof_bytes: bytes, turn_id: TurnId, ts_iso: str, step_unknowns: Sequence[UnknownWireField], meta_extra: bytes) -> SystemEvent:
    fm, _ = parse_proto_fields(oneof_bytes)
    msg_text = ""
    for wt, raw in fm.get(int(ErrorMessageStepTag.ERROR), ()):
        if wt == 2 and isinstance(raw, bytes):
            efm, _ = parse_proto_fields(raw)
            msg_text = _fm_last_str(efm, ErrorDetailsTag.USER_ERROR_MESSAGE) or _fm_last_str(efm, ErrorDetailsTag.SHORT_ERROR) or _fm_last_str(efm, ErrorDetailsTag.FULL_ERROR) or msg_text
    err_ext = HostExtensionEnvelope(host=HostKind.ANTIGRAVITY, extension_type="agy_error_message_step", raw_payload=oneof_bytes, metadata=(("status", str(row.status)),))
    anchor = EventOriginAnchor(
        origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso=ts_iso, agy_step_index=row.idx,
        unknown_wire_fields=tuple(step_unknowns), host_extensions=_make_agy_exts((), meta_extra, (err_ext,)),
    )
    return SystemEvent(kind="system_message", anchor=anchor, event_type="error_message", message_text=msg_text or "error_message")


def _decode_framing_step(
    spec: StepWireSpec, row: AgyRawStepRow, oneof_bytes: bytes, turn_id: TurnId, ts_iso: str,
    step_unknowns: Sequence[UnknownWireField], meta_extra: bytes,
) -> tuple[CanonicalEvent, str | None]:
    if spec.step_type == CortexStepTypeCode.CHECKPOINT:
        fm, unknowns = parse_proto_fields(oneof_bytes, known_tags=range(1, int(CheckpointTag.MAX_KNOWN_TAG) + 1))
        cp_idx = next((int(v) for wt, v in reversed(fm.get(int(CheckpointTag.CHECKPOINT_INDEX), ())) if wt == 0 and isinstance(v, int)), 0)
        conv_title = _fm_last_str(fm, CheckpointTag.CONVERSATION_TITLE) or None
        anchor = EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso=ts_iso, agy_step_index=row.idx, unknown_wire_fields=tuple(step_unknowns), host_extensions=_make_agy_exts(unknowns, meta_extra))
        return CheckpointEvent(kind="checkpoint", anchor=anchor, checkpoint_index=cp_idx, user_intent=_fm_last_str(fm, CheckpointTag.USER_INTENT), session_summary=_fm_last_str(fm, CheckpointTag.SESSION_SUMMARY), code_change_summary=_fm_last_str(fm, CheckpointTag.CODE_CHANGE_SUMMARY), conversation_title=conv_title), None

    if spec.step_type == CortexStepTypeCode.TASK_BOUNDARY:
        fm, unknowns = parse_proto_fields(oneof_bytes, known_tags=range(1, int(TaskBoundaryTag.MAX_KNOWN_TAG) + 1))
        t_status = _fm_last_str(fm, TaskBoundaryTag.TASK_STATUS)
        t_mode = next((str(v) for wt, v in reversed(fm.get(int(TaskBoundaryTag.MODE), ())) if wt == 0 and isinstance(v, int)), None)
        anchor = EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso=ts_iso, agy_step_index=row.idx, unknown_wire_fields=tuple(step_unknowns), host_extensions=_make_agy_exts(unknowns, meta_extra))
        return TaskBoundaryEvent(kind="task_boundary", anchor=anchor, boundary_kind="task_boundary", task_name=_fm_last_str(fm, TaskBoundaryTag.TASK_NAME), task_status=t_status or "DONE", task_summary=_fm_last_str(fm, TaskBoundaryTag.TASK_SUMMARY), mode=t_mode), t_status

    if spec.step_type == CortexStepTypeCode.NOTIFY_USER:
        fm, unknowns = parse_proto_fields(oneof_bytes, known_tags=range(1, int(NotifyUserTag.MAX_KNOWN_TAG) + 1))
        r_uris = tuple(v.decode("utf-8", errors="replace") for wt, v in fm.get(int(NotifyUserTag.REVIEW_ABSOLUTE_URIS), ()) if wt == 2 and isinstance(v, bytes))
        blocking = any(wt == 0 and bool(v) for wt, v in fm.get(int(NotifyUserTag.IS_BLOCKING), ()))
        anchor = EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso=ts_iso, agy_step_index=row.idx, unknown_wire_fields=tuple(step_unknowns), host_extensions=_make_agy_exts(unknowns, meta_extra))
        return TaskBoundaryEvent(kind="task_boundary", anchor=anchor, boundary_kind="notify_user", task_name="notify_user", task_status="DONE", task_summary=_fm_last_str(fm, NotifyUserTag.NOTIFICATION_CONTENT), blocked_on_user=blocking, review_uris=r_uris), "DONE"

    fm, unknowns = parse_proto_fields(oneof_bytes, known_tags=range(1, int(SystemMessageTag.MAX_KNOWN_TAG) + 1))
    anchor = EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=turn_id, timestamp_iso=ts_iso, agy_step_index=row.idx, unknown_wire_fields=tuple(step_unknowns), host_extensions=_make_agy_exts(unknowns, meta_extra))
    return SystemEvent(kind="system_message", anchor=anchor, event_type=_fm_last_str(fm, SystemMessageTag.EVENT_TYPE) or "system_message", message_text=_fm_last_str(fm, SystemMessageTag.MESSAGE)), None


def _classify_raw_step_row(row: AgyRawStepRow) -> tuple[dict[str, Any], bytes, StepWireSpec | None, bool, bytes, list[UnknownWireField]]:
    step_fields = iter_wire_fields(row.step_payload_blob) if row.step_payload_blob else []
    raw_meta = row.metadata_blob or next((raw for fn, wt, _, raw in step_fields if fn == StepFieldTag.METADATA and wt == 2), b"")
    meta_dict, meta_extra = _decode_step_metadata(raw_meta) if raw_meta else (_decode_step_metadata(b"")[0], b"")
    spec = STEP_WIRE_TABLE.get(CortexStepTypeCode(row.step_type)) if row.step_type in CortexStepTypeCode._value2member_map_ else None
    is_err_msg = row.step_type == int(CortexStepTypeCode.ERROR_MESSAGE)
    oneof_bytes = b""
    step_unknowns: list[UnknownWireField] = []
    for fn, wt, _, raw in step_fields:
        if fn not in _KNOWN_STEP_TAGS:
            step_unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
        elif is_err_msg and fn == int(GeminiStepWireTag.ERROR_MESSAGE_TAG) and wt == 2:
            oneof_bytes = raw
        elif spec is not None and fn == int(spec.step_oneof_tag) and wt == 2:
            oneof_bytes = raw
        elif spec is None and not is_err_msg and fn == int(GeminiStepWireTag.ERROR_MESSAGE_TAG) and wt == 2:
            is_err_msg, oneof_bytes = True, raw
        elif spec is None and not is_err_msg and fn in STEP_TAG_TO_SPEC and wt == 2:
            spec, oneof_bytes = STEP_TAG_TO_SPEC[GeminiStepWireTag(fn)], raw
    return meta_dict, meta_extra, spec, is_err_msg, oneof_bytes, step_unknowns


def decode_agy_trajectory(
    workspace_path: WorkspacePath,
    workspace_uri: WorkspaceRootUri,
    raw_steps: Sequence[AgyRawStepRow],
    *,
    explicit_title_override: str | None = None,
) -> TrajectoryLedger:
    """Parse raw Antigravity `steps` rows into an indexed `TrajectoryLedger`."""
    events: list[CanonicalEvent] = []
    call_index_by_id: dict[ToolCallId, int] = {}
    unmatched_synthetic: list[ToolCallId] = []
    turn_num, current_turn_id, turn_has_response = 1, TurnId("turn-1"), False

    for row in raw_steps:
        start_ev_len = len(events)
        meta_dict, meta_extra, spec, is_err_msg, oneof_bytes, step_unknowns = _classify_raw_step_row(row)
        ts_iso = str(meta_dict["created_at_iso"])
        if is_err_msg:
            turn_has_response = True
            events.append(_decode_error_message_step(row, oneof_bytes, current_turn_id, ts_iso, step_unknowns, meta_extra))
        elif spec is None:
            turn_has_response = True
            op_ext = HostExtensionEnvelope(
                host=HostKind.ANTIGRAVITY, extension_type="agy_opaque_step", raw_payload=row.step_payload_blob,
                metadata=(("step_type", str(row.step_type)), ("status", str(row.status)), ("has_subtrajectory", "1" if row.has_subtrajectory else "0"), ("step_format", str(row.step_format)), ("metadata_hex", row.metadata_blob.hex())),
            )
            anchor = EventOriginAnchor(origin_host=HostKind.ANTIGRAVITY, turn_id=current_turn_id, timestamp_iso=ts_iso, agy_step_index=row.idx, host_extensions=(op_ext,))
            events.append(SystemEvent(kind="system_message", anchor=anchor, event_type=f"opaque_agy_step:{row.step_type}", message_text=f"agy_step_type:{row.step_type}"))
        else:
            token_usage = TokenUsageAccounting(input_tokens=int(meta_dict["input_tokens"]), output_tokens=int(meta_dict["output_tokens"]), cache_read_tokens=int(meta_dict["cache_read_tokens"]))
            if spec.step_type == CortexStepTypeCode.USER_INPUT:
                if turn_has_response:
                    turn_num += 1
                    current_turn_id, turn_has_response = TurnId(f"turn-{turn_num}"), False
                events.append(_decode_user_input(row, oneof_bytes, current_turn_id, ts_iso, step_unknowns, meta_extra))
            elif spec.step_type == CortexStepTypeCode.PLANNER_RESPONSE:
                turn_has_response = True
                _decode_planner_response(row, oneof_bytes, current_turn_id, ts_iso, meta_dict, token_usage, step_unknowns, meta_extra, events, call_index_by_id, unmatched_synthetic)
            elif spec.step_type == CortexStepTypeCode.GENERIC:
                turn_has_response = True
                _decode_generic_step(row, oneof_bytes, current_turn_id, ts_iso, meta_dict, step_unknowns, meta_extra, events, call_index_by_id, unmatched_synthetic)
            elif spec.step_type == CortexStepTypeCode.TASK_BOUNDARY:
                probe_ev, raw_status = _decode_framing_step(spec, row, oneof_bytes, current_turn_id, ts_iso, step_unknowns, meta_extra)
                if (raw_status or "").lower() in ("task_started", "running", "in_progress", "starting"):
                    if turn_has_response:
                        turn_num += 1
                        current_turn_id, turn_has_response = TurnId(f"turn-{turn_num}"), False
                        probe_ev, _ = _decode_framing_step(spec, row, oneof_bytes, current_turn_id, ts_iso, step_unknowns, meta_extra)
                else:
                    turn_has_response = True
                events.append(probe_ev)
            else:
                if spec.step_type == CortexStepTypeCode.NOTIFY_USER:
                    turn_has_response = True
                ev_out, _ = _decode_framing_step(spec, row, oneof_bytes, current_turn_id, ts_iso, step_unknowns, meta_extra)
                events.append(ev_out)
        if row.status in ACTIVE_STEP_STATUS_CODES:
            st_str = str(row.status)
            st_ext = HostExtensionEnvelope(host=HostKind.ANTIGRAVITY, extension_type="agy_step_status", raw_payload=b"", metadata=(("status", st_str), ("agy_active_status", st_str)))
            for ev_idx in range(start_ev_len, len(events)):
                cur = events[ev_idx]
                events[ev_idx] = dataclasses.replace(cur, anchor=dataclasses.replace(cur.anchor, host_extensions=(*cur.anchor.host_extensions, st_ext)))

    return TrajectoryLedger.from_events(workspace_path=workspace_path, workspace_uri=workspace_uri, events=events, explicit_title_override=explicit_title_override)


def bind_agy_step(row: AgyRawStepRow, trajectory_id: str, cascade_id: str) -> AgyRawStepRow:
    reference = encode_string_field(1, trajectory_id) + encode_varint_field(2, int(row.idx)) + encode_string_field(4, cascade_id)
    metadata = encode_unknown_fields(
        UnknownWireField(n, w, raw) for n, w, _, raw in iter_wire_fields(row.metadata_blob) if n != 20
    ) + encode_bytes_field(20, reference)
    payload = encode_unknown_fields(
        UnknownWireField(n, w, raw) for n, w, _, raw in iter_wire_fields(row.step_payload_blob) if n != int(StepFieldTag.METADATA)
    ) + encode_bytes_field(StepFieldTag.METADATA, metadata)
    return dataclasses.replace(row, metadata_blob=metadata, step_payload_blob=payload)


def _wrap_step_row(idx: int, step_type: int, oneof_tag: int, oneof_payload: bytes, meta_blob: bytes, unknowns: Sequence[UnknownWireField], status: int = int(StepStatusCode.DONE)) -> AgyRawStepRow:
    step_buf = (
        encode_varint_field(StepFieldTag.TYPE, step_type)
        + encode_varint_field(StepFieldTag.STATUS, status)
        + encode_bytes_field(StepFieldTag.METADATA, meta_blob)
        + encode_bytes_field(oneof_tag, oneof_payload)
        + encode_unknown_fields(unknowns)
    )
    return AgyRawStepRow(idx=StepIndex(idx), step_type=step_type, status=status, has_subtrajectory=False, metadata_blob=meta_blob, step_payload_blob=step_buf, step_format=0)


def _encode_planner_group(
    events: Sequence[CanonicalEvent], start_pos: int, next_idx: int, resolved_call_ids: frozenset[ToolCallId],
) -> tuple[AgyRawStepRow, int]:
    first = events[start_pos]
    group_turn = first.anchor.turn_id
    group_step_idx = first.anchor.agy_step_index if first.anchor.origin_host == HostKind.ANTIGRAVITY else None
    thinking_ev: ThinkingEvent | None = None
    text_ev: AssistantTextEvent | None = None
    tool_evs: list[ToolCallEvent] = []
    unknowns: list[UnknownWireField] = []
    seen_ukeys: set[tuple[int, int, bytes]] = set()
    pos = start_pos

    while pos < len(events):
        cand = events[pos]
        if not isinstance(cand, (ThinkingEvent, AssistantTextEvent, ToolCallEvent)) or cand.anchor.turn_id != group_turn:
            break
        if group_step_idx is not None and cand.anchor.origin_host == HostKind.ANTIGRAVITY:
            if cand.anchor.agy_step_index != group_step_idx:
                break
        elif (isinstance(cand, ThinkingEvent) and (thinking_ev is not None or text_ev is not None or tool_evs)) or (
            isinstance(cand, AssistantTextEvent) and (text_ev is not None or tool_evs)
        ):
            break
        if isinstance(cand, ThinkingEvent):
            thinking_ev = cand
        elif isinstance(cand, AssistantTextEvent):
            text_ev = cand
        else:
            tool_evs.append(cand)
        for uf in cand.anchor.unknown_wire_fields:
            if (uk := (uf.field_tag, uf.wire_type, uf.raw_bytes)) not in seen_ukeys:
                seen_ukeys.add(uk)
                unknowns.append(uf)
        pos += 1

    lead = thinking_ev or text_ev or tool_evs[0]
    usage = text_ev.token_usage if text_ev is not None else (thinking_ev.token_usage if thinking_ev is not None else TokenUsageAccounting())
    pr_buf = bytearray()
    if text_ev is not None and text_ev.text:
        pr_buf.extend(encode_string_field(PlannerResponseTag.RESPONSE, text_ev.text))
    if thinking_ev is not None and thinking_ev.summary_text:
        pr_buf.extend(encode_string_field(PlannerResponseTag.THINKING, thinking_ev.summary_text))
    if text_ev is not None and text_ev.message_id:
        pr_buf.extend(encode_string_field(PlannerResponseTag.MESSAGE_ID, text_ev.message_id))
    for tc_ev in tool_evs:
        spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(tc_ev.payload.kind)
        agy_name = spec.agy_tool_name if spec else tc_ev.tool_name
        args_json = serialize_agy_chat_tool_arguments(tc_ev.payload, has_result_step=(tc_ev.call_id in resolved_call_ids))
        pr_buf.extend(encode_bytes_field(PlannerResponseTag.TOOL_CALLS, _encode_chat_tool_call(str(tc_ev.call_id), agy_name, args_json, tc_ev.thinking_signature)))
    if usage.thinking_duration_ms and (dur_b := _encode_duration_proto(usage.thinking_duration_ms)):
        pr_buf.extend(encode_bytes_field(PlannerResponseTag.THINKING_DURATION, dur_b))
    if text_ev is not None and text_ev.stop_reason and text_ev.stop_reason.isdigit():
        pr_buf.extend(encode_varint_field(PlannerResponseTag.STOP_REASON, int(text_ev.stop_reason)))
    if thinking_ev is not None and thinking_ev.thinking_signature:
        pr_buf.extend(encode_bytes_field(PlannerResponseTag.THINKING_SIGNATURE, thinking_ev.thinking_signature))
    if thinking_ev is not None and thinking_ev.raw_thinking:
        pr_buf.extend(encode_string_field(PlannerResponseTag.RAW_THINKING, thinking_ev.raw_thinking))
    pr_buf.extend(_ext_payload(lead.anchor, "agy_submessage_unknowns"))

    meta_blob = _encode_step_metadata(
        lead.anchor.timestamp_iso, int(StepSourceCode.MODEL), token_usage=usage,
        generator_model=text_ev.generator_model if text_ev is not None else None,
        extra_wire_bytes=_ext_payload(lead.anchor, "agy_metadata_extra"),
    )
    return _wrap_step_row(next_idx, int(CortexStepTypeCode.PLANNER_RESPONSE), int(GeminiStepWireTag.PLANNER_RESPONSE_TAG), bytes(pr_buf), meta_blob, unknowns), pos


def _encode_system_event_step(ev: SystemEvent, next_idx: int) -> AgyRawStepRow:
    for ext in ev.anchor.host_extensions:
        if ext.extension_type == "agy_opaque_step" and ext.raw_payload:
            md = dict(ext.metadata)
            meta_blob = bytes.fromhex(md["metadata_hex"]) if md.get("metadata_hex") else _encode_step_metadata(ev.anchor.timestamp_iso, int(StepSourceCode.SYSTEM))
            return AgyRawStepRow(
                idx=StepIndex(next_idx), step_type=int(md.get("step_type", "101")), status=int(md.get("status", "3")),
                has_subtrajectory=(md.get("has_subtrajectory") == "1"), metadata_blob=meta_blob,
                step_payload_blob=bytes(ext.raw_payload), step_format=int(md.get("step_format", "1")),
            )
    meta_blob = _encode_step_metadata(ev.anchor.timestamp_iso, int(StepSourceCode.SYSTEM), extra_wire_bytes=_ext_payload(ev.anchor, "agy_metadata_extra"))
    if ev.event_type == "error_message":
        status_code, err_oneof = int(StepStatusCode.DONE), b""
        for ext in ev.anchor.host_extensions:
            if ext.extension_type == "agy_error_message_step" and ext.raw_payload:
                err_oneof = bytes(ext.raw_payload)
                status_code = int(dict(ext.metadata).get("status", str(int(StepStatusCode.DONE))))
        if not err_oneof:
            details = encode_string_field(ErrorDetailsTag.USER_ERROR_MESSAGE, ev.message_text) + encode_string_field(ErrorDetailsTag.SHORT_ERROR, ev.message_text)
            err_oneof = encode_bytes_field(ErrorMessageStepTag.ERROR, details) + encode_varint_field(ErrorMessageStepTag.SHOULD_SHOW_USER, 1)
        return _wrap_step_row(next_idx, int(CortexStepTypeCode.ERROR_MESSAGE), int(GeminiStepWireTag.ERROR_MESSAGE_TAG), err_oneof, meta_blob, ev.anchor.unknown_wire_fields, status=status_code)
    sm_buf = encode_string_field(SystemMessageTag.MESSAGE, ev.message_text) + encode_string_field(SystemMessageTag.EVENT_TYPE, ev.event_type) + _ext_payload(ev.anchor, "agy_submessage_unknowns")
    return _wrap_step_row(next_idx, int(CortexStepTypeCode.SYSTEM_MESSAGE), int(GeminiStepWireTag.SYSTEM_MESSAGE_TAG), sm_buf, meta_blob, ev.anchor.unknown_wire_fields)


def _encode_single_agy_event(ev: CanonicalEvent, next_idx: int, call_by_id: Mapping[ToolCallId, ToolCallEvent]) -> AgyRawStepRow | None:
    if isinstance(ev, UserInputEvent):
        ui_buf = bytearray(encode_string_field(UserInputTag.QUERY, ev.query_text) + encode_string_field(UserInputTag.USER_RESPONSE, ev.query_text))
        ui_buf.extend(encode_bytes_field(UserInputTag.ITEMS, encode_string_field(TextOrScopeItemTag.TEXT, ev.query_text)))
        for s_item in ev.scope_items:
            ui_buf.extend(encode_bytes_field(UserInputTag.ITEMS, encode_bytes_field(TextOrScopeItemTag.ITEM, _encode_scope_item_bytes(s_item))))
        for m in ev.media:
            ui_buf.extend(encode_bytes_field(UserInputTag.MEDIA, encode_string_field(MediaTag.MIME_TYPE, m.mime_type) + encode_string_field(MediaTag.URI, m.uri)))
        if ev.anchor.client_message_id:
            ui_buf.extend(encode_string_field(UserInputTag.CLIENT_MESSAGE_ID, ev.anchor.client_message_id))
        ui_buf.extend(_ext_payload(ev.anchor, "agy_submessage_unknowns"))
        meta_blob = _encode_step_metadata(ev.anchor.timestamp_iso, int(StepSourceCode.USER_EXPLICIT), extra_wire_bytes=_ext_payload(ev.anchor, "agy_metadata_extra"))
        return _wrap_step_row(next_idx, int(CortexStepTypeCode.USER_INPUT), int(GeminiStepWireTag.USER_INPUT_TAG), bytes(ui_buf), meta_blob, ev.anchor.unknown_wire_fields)

    if isinstance(ev, ToolResultEvent):
        spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(ev.payload.kind)
        agy_name = spec.agy_tool_name if spec else ev.tool_name
        args_json = serialize_agy_chat_tool_arguments(ev.payload, has_result_step=True)
        args_dict = safe_json_dict(args_json)
        gen_buf = bytearray()
        for k in sorted(args_dict):
            v_str = args_dict[k] if isinstance(args_dict[k], str) else json.dumps(args_dict[k], ensure_ascii=False)
            gen_buf.extend(encode_bytes_field(GenericStepTag.ARGS, encode_string_field(MapEntryTag.KEY, str(k)) + encode_string_field(MapEntryTag.VALUE, str(v_str))))
        res_buf = (
            encode_string_field(GenericResultTag.RESULT, ev.output_text)
            + (encode_bytes_field(GenericResultTag.PAYLOAD, any_b) if (any_b := encode_tool_payload_to_any(ev.payload)) else b"")
            + (encode_string_field(GenericResultTag.FULL_OUTPUT_URI, ev.full_output_uri) if ev.full_output_uri else b"")
        )
        gen_buf.extend(encode_bytes_field(GenericStepTag.RESULT, res_buf) + _ext_payload(ev.anchor, "agy_submessage_unknowns"))
        paired_call = call_by_id.get(ev.call_id)
        summary_lbl = paired_call.tool_summary if (paired_call and paired_call.tool_summary) else agy_name
        meta_blob = _encode_step_metadata(
            ev.anchor.timestamp_iso, int(StepSourceCode.MODEL),
            tool_call={"id": str(ev.call_id), "name": agy_name, "arguments_json": args_json, "thinking_signature": paired_call.thinking_signature if paired_call else None},
            tool_summary=summary_lbl, extra_wire_bytes=_ext_payload(ev.anchor, "agy_metadata_extra"),
        )
        status_code = (
            (int(StepStatusCode.CANCELED) if ev.output_text == INTERRUPTED_TOOL_OUTPUT else int(StepStatusCode.ERROR))
            if ev.is_error
            else int(StepStatusCode.DONE)
        )
        return _wrap_step_row(next_idx, int(CortexStepTypeCode.GENERIC), int(GeminiStepWireTag.GENERIC_TAG), bytes(gen_buf), meta_blob, ev.anchor.unknown_wire_fields, status=status_code)

    if isinstance(ev, CheckpointEvent):
        cp_buf = (
            (encode_varint_field(CheckpointTag.CHECKPOINT_INDEX, ev.checkpoint_index) if ev.checkpoint_index else b"")
            + encode_string_field(CheckpointTag.USER_INTENT, ev.user_intent)
            + encode_string_field(CheckpointTag.SESSION_SUMMARY, ev.session_summary)
            + encode_string_field(CheckpointTag.CODE_CHANGE_SUMMARY, ev.code_change_summary)
            + (encode_string_field(CheckpointTag.CONVERSATION_TITLE, ev.conversation_title) if ev.conversation_title else b"")
            + _ext_payload(ev.anchor, "agy_submessage_unknowns")
        )
        meta_blob = _encode_step_metadata(ev.anchor.timestamp_iso, int(StepSourceCode.SYSTEM), extra_wire_bytes=_ext_payload(ev.anchor, "agy_metadata_extra"))
        return _wrap_step_row(next_idx, int(CortexStepTypeCode.CHECKPOINT), int(GeminiStepWireTag.CHECKPOINT_TAG), cp_buf, meta_blob, ev.anchor.unknown_wire_fields)

    if isinstance(ev, TaskBoundaryEvent):
        meta_blob = _encode_step_metadata(ev.anchor.timestamp_iso, int(StepSourceCode.MODEL), extra_wire_bytes=_ext_payload(ev.anchor, "agy_metadata_extra"))
        if ev.boundary_kind == "notify_user":
            nu_buf = (
                b"".join(encode_string_field(NotifyUserTag.REVIEW_ABSOLUTE_URIS, u) for u in ev.review_uris)
                + encode_string_field(NotifyUserTag.NOTIFICATION_CONTENT, ev.task_summary)
                + (encode_varint_field(NotifyUserTag.IS_BLOCKING, 1) if ev.blocked_on_user else b"")
                + _ext_payload(ev.anchor, "agy_submessage_unknowns")
            )
            return _wrap_step_row(next_idx, int(CortexStepTypeCode.NOTIFY_USER), int(GeminiStepWireTag.NOTIFY_USER_TAG), nu_buf, meta_blob, ev.anchor.unknown_wire_fields)
        tb_buf = (
            encode_string_field(TaskBoundaryTag.TASK_NAME, ev.task_name)
            + encode_string_field(TaskBoundaryTag.TASK_STATUS, ev.task_status)
            + encode_string_field(TaskBoundaryTag.TASK_SUMMARY, ev.task_summary)
            + (encode_varint_field(TaskBoundaryTag.MODE, int(ev.mode)) if (ev.mode and ev.mode.isdigit()) else b"")
            + _ext_payload(ev.anchor, "agy_submessage_unknowns")
        )
        return _wrap_step_row(next_idx, int(CortexStepTypeCode.TASK_BOUNDARY), int(GeminiStepWireTag.TASK_BOUNDARY_TAG), tb_buf, meta_blob, ev.anchor.unknown_wire_fields)

    if isinstance(ev, SystemEvent):
        return _encode_system_event_step(ev, next_idx)
    return None


def encode_agy_trajectory(
    events: Sequence[CanonicalEvent],
    starting_step_idx: StepIndex = StepIndex(0),
) -> Tuple[AgyRawStepRow, ...]:
    """Serialize a slice of `CanonicalEvent` nodes into Antigravity `steps` rows."""
    rows: list[AgyRawStepRow] = []
    next_idx = int(starting_step_idx)
    resolved_ids = frozenset(ev.call_id for ev in events if isinstance(ev, ToolResultEvent))
    call_by_id: dict[ToolCallId, ToolCallEvent] = {ev.call_id: ev for ev in events if isinstance(ev, ToolCallEvent)}
    pos = 0
    while pos < len(events):
        ev = events[pos]
        if isinstance(ev, (ThinkingEvent, AssistantTextEvent, ToolCallEvent)):
            row, pos = _encode_planner_group(events, pos, next_idx, resolved_ids)
            rows.append(row)
            next_idx += 1
        else:
            if (single_row := _encode_single_agy_event(ev, next_idx, call_by_id)) is not None:
                rows.append(single_row)
                next_idx += 1
            pos += 1
    return tuple(rows)


decode_agy_steps_to_ledger = decode_agy_trajectory
encode_events_to_agy_steps = encode_agy_trajectory


def encode_ledger_to_agy_steps(
    ledger_or_events: TrajectoryLedger | Sequence[CanonicalEvent],
    starting_step_idx: StepIndex = StepIndex(0),
) -> Tuple[AgyRawStepRow, ...]:
    """Encode a `TrajectoryLedger` or event sequence into Antigravity `steps` rows."""
    events = ledger_or_events.events if isinstance(ledger_or_events, TrajectoryLedger) else ledger_or_events
    return encode_agy_trajectory(events, starting_step_idx)
