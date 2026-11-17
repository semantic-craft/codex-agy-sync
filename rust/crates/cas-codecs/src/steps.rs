//! Antigravity `gemini_coder.Step` trajectory decoder and encoder driven by `STEP_WIRE_TABLE`.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};

use cas_core::{
    CanonicalEvent, EventOriginAnchor, HostExtensionEnvelope, HostKind, INTERRUPTED_TOOL_OUTPUT,
    MediaAttachment, StepIndex, TaskBoundaryKind, TokenUsageAccounting, ToolCallId,
    TrajectoryLedger, TurnId, WorkspacePath, WorkspaceRootUri, safe_json_dict,
};
use serde_json::{Value, json};

use crate::error::CodecError;
use crate::schema_tags::{
    AgyRawStepRow, CortexStepTypeCode, GeminiStepWireTag, StepWireSpec, chat_tool_call,
    checkpoint_tag, context_scope, duration_proto, error_details, error_message_step,
    find_step_spec_by_tag, find_step_spec_by_type, generic_result, generic_step, map_entry,
    media_tag, model_usage, notify_user_tag, path_scope, planner_response, step_field, step_meta,
    step_source, step_status, system_message_tag, task_boundary_tag, text_or_scope, user_input,
};
use crate::tool_codecs::{
    decode_any_envelope, decode_tool_payload, encode_tool_payload_to_any, find_tool_spec_by_name,
    serialize_agy_chat_tool_arguments,
};
use crate::wire::{
    FieldMap, UnknownWireField, decode_timestamp_proto, encode_bytes_field, encode_string_field,
    encode_timestamp_proto, encode_unknown_fields, encode_varint_field, fm_bytes,
    fm_last_str_fallback as fm_last_str, fm_last_varint, iter_wire_fields, parse_proto_fields,
    signed_int32, signed_int64,
};

fn parse_proto_fields_known(
    buf: &[u8],
    known_tags: &[u32],
) -> Result<(FieldMap, Vec<UnknownWireField>), CodecError> {
    parse_proto_fields(buf, Some(known_tags))
}

fn is_known_step_tag(tag: u32) -> bool {
    matches!(
        tag,
        1 | 4 | 5 | 6 | 31 | 56 | 69 | 133 | 147 | 148 | 155 | 158 | 159
    ) || GeminiStepWireTag::from_u32(tag).is_some()
}

#[derive(Debug, Clone, Default)]
struct DecodedChatToolCall {
    id: String,
    name: String,
    arguments_json: String,
    thinking_signature: Option<Vec<u8>>,
}

#[derive(Debug, Clone)]
struct DecodedStepMeta {
    created_at_iso: String,
    source: i64,
    tool_call: Option<DecodedChatToolCall>,
    tool_summary: String,
    generator_model: Option<String>,
    input_tokens: i64,
    output_tokens: i64,
    cache_read_tokens: i64,
}

impl Default for DecodedStepMeta {
    fn default() -> Self {
        Self {
            created_at_iso: "2026-01-01T00:00:00Z".to_owned(),
            source: 0,
            tool_call: None,
            tool_summary: String::new(),
            generator_model: None,
            input_tokens: 0,
            output_tokens: 0,
            cache_read_tokens: 0,
        }
    }
}

fn decode_duration_ms(raw: &[u8]) -> Result<i64, CodecError> {
    let mut seconds = 0i64;
    let mut nanos = 0i64;
    for (fn_tag, wt, ival, _) in iter_wire_fields(raw)? {
        if fn_tag == duration_proto::SECONDS && wt == 0 {
            seconds = signed_int64(ival);
        } else if fn_tag == duration_proto::NANOS && wt == 0 {
            nanos = i64::from(signed_int32(ival));
        }
    }
    Ok(seconds * 1000 + (nanos / 1_000_000))
}

fn encode_duration_proto(duration_ms: i64) -> Vec<u8> {
    if duration_ms <= 0 {
        return Vec::new();
    }
    let seconds = duration_ms / 1000;
    let nanos = (duration_ms % 1000) * 1_000_000;
    let mut out = Vec::new();
    if seconds != 0 {
        out.extend_from_slice(&encode_varint_field(duration_proto::SECONDS, seconds));
    }
    if nanos != 0 {
        out.extend_from_slice(&encode_varint_field(duration_proto::NANOS, nanos));
    }
    out
}

fn decode_chat_tool_call(buf: &[u8]) -> Result<DecodedChatToolCall, CodecError> {
    let mut out = DecodedChatToolCall {
        id: String::new(),
        name: String::new(),
        arguments_json: "{}".to_owned(),
        thinking_signature: None,
    };
    for (fn_tag, wt, _, raw) in iter_wire_fields(buf)? {
        if wt != 2 {
            continue;
        }
        match fn_tag {
            chat_tool_call::ID => out.id = String::from_utf8_lossy(&raw).into_owned(),
            chat_tool_call::NAME => out.name = String::from_utf8_lossy(&raw).into_owned(),
            chat_tool_call::ARGUMENTS_JSON => {
                out.arguments_json = String::from_utf8_lossy(&raw).into_owned();
            }
            chat_tool_call::THINKING_SIGNATURE => out.thinking_signature = Some(raw),
            _ => {}
        }
    }
    Ok(out)
}

fn encode_chat_tool_call(
    call_id: &str,
    name: &str,
    arguments_json: &str,
    thinking_signature: Option<&[u8]>,
) -> Vec<u8> {
    let mut out = encode_string_field(chat_tool_call::ID, call_id);
    out.extend_from_slice(&encode_string_field(chat_tool_call::NAME, name));
    out.extend_from_slice(&encode_string_field(
        chat_tool_call::ARGUMENTS_JSON,
        arguments_json,
    ));
    if let Some(sig) = thinking_signature.filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_bytes_field(chat_tool_call::THINKING_SIGNATURE, sig));
    }
    out
}

fn decode_step_metadata(buf: &[u8]) -> Result<(DecodedStepMeta, Vec<u8>), CodecError> {
    let mut meta = DecodedStepMeta::default();
    let mut extra_fields = Vec::new();
    for (fn_tag, wt, ival, raw) in iter_wire_fields(buf)? {
        if !(1..=step_meta::MAX_KNOWN_TAG).contains(&fn_tag) {
            extra_fields.push(UnknownWireField {
                field_tag: fn_tag,
                wire_type: wt,
                raw_bytes: raw,
            });
        } else if fn_tag == step_meta::CREATED_AT && wt == 2 {
            meta.created_at_iso = decode_timestamp_proto(&raw, "2026-01-01T00:00:00Z")?;
        } else if fn_tag == step_meta::SOURCE && wt == 0 {
            meta.source = ival as i64;
        } else if fn_tag == step_meta::TOOL_CALL && wt == 2 {
            meta.tool_call = Some(decode_chat_tool_call(&raw)?);
        } else if fn_tag == step_meta::MODEL_USAGE && wt == 2 {
            for (ufn, uwt, uival, _) in iter_wire_fields(&raw)? {
                if uwt == 0 {
                    match ufn {
                        model_usage::INPUT_TOKENS => meta.input_tokens = uival as i64,
                        model_usage::OUTPUT_TOKENS => meta.output_tokens = uival as i64,
                        model_usage::CACHE_READ_TOKENS => meta.cache_read_tokens = uival as i64,
                        _ => {}
                    }
                }
            }
        } else if fn_tag == step_meta::GENERATOR_MODEL && wt == 0 {
            meta.generator_model = Some(ival.to_string());
        } else if fn_tag == step_meta::TOOL_SUMMARY && wt == 2 {
            meta.tool_summary = String::from_utf8_lossy(&raw).into_owned();
        } else {
            extra_fields.push(UnknownWireField {
                field_tag: fn_tag,
                wire_type: wt,
                raw_bytes: raw,
            });
        }
    }
    Ok((meta, encode_unknown_fields(&extra_fields)))
}

fn encode_step_metadata(
    created_at_iso: &str,
    source_enum: i64,
    tool_call: Option<&DecodedChatToolCall>,
    tool_summary: &str,
    token_usage: Option<TokenUsageAccounting>,
    generator_model: Option<&str>,
    extra_wire_bytes: &[u8],
) -> Vec<u8> {
    let mut buf = Vec::new();
    let ts_bytes = encode_timestamp_proto(created_at_iso);
    if !ts_bytes.is_empty() {
        buf.extend_from_slice(&encode_bytes_field(step_meta::CREATED_AT, &ts_bytes));
    }
    if source_enum != 0 {
        buf.extend_from_slice(&encode_varint_field(step_meta::SOURCE, source_enum));
    }
    if let Some(tc) = tool_call {
        let encoded_tc = encode_chat_tool_call(
            &tc.id,
            &tc.name,
            &tc.arguments_json,
            tc.thinking_signature.as_deref(),
        );
        buf.extend_from_slice(&encode_bytes_field(step_meta::TOOL_CALL, &encoded_tc));
    }
    if let Some(tu) = token_usage
        && (tu.input_tokens != 0 || tu.output_tokens != 0 || tu.cache_read_tokens != 0)
    {
        let mut usage_buf = Vec::new();
        if tu.input_tokens != 0 {
            usage_buf.extend_from_slice(&encode_varint_field(
                model_usage::INPUT_TOKENS,
                tu.input_tokens,
            ));
        }
        if tu.output_tokens != 0 {
            usage_buf.extend_from_slice(&encode_varint_field(
                model_usage::OUTPUT_TOKENS,
                tu.output_tokens,
            ));
        }
        if tu.cache_read_tokens != 0 {
            usage_buf.extend_from_slice(&encode_varint_field(
                model_usage::CACHE_READ_TOKENS,
                tu.cache_read_tokens,
            ));
        }
        buf.extend_from_slice(&encode_bytes_field(step_meta::MODEL_USAGE, &usage_buf));
    }
    if let Some(gm) = generator_model
        && let Ok(gm_int) = gm.parse::<i64>()
    {
        buf.extend_from_slice(&encode_varint_field(step_meta::GENERATOR_MODEL, gm_int));
    }
    if !tool_summary.is_empty() {
        buf.extend_from_slice(&encode_string_field(step_meta::TOOL_SUMMARY, tool_summary));
    }
    if !extra_wire_bytes.is_empty() {
        buf.extend_from_slice(extra_wire_bytes);
    }
    buf
}

fn decode_scope_item_bytes(ctx_bytes: &[u8]) -> Result<String, CodecError> {
    let fields = iter_wire_fields(ctx_bytes)?;
    if fields.len() == 1 && fields[0].0 == context_scope::FILE && fields[0].1 == 2 {
        let pfields = iter_wire_fields(&fields[0].3)?;
        if pfields.len() == 1 && pfields[0].0 == path_scope::ABSOLUTE_URI && pfields[0].1 == 2 {
            let uri = String::from_utf8_lossy(&pfields[0].3).into_owned();
            if !uri.is_empty() && !uri.starts_with("ctxpb:") {
                let reencoded = encode_bytes_field(
                    context_scope::FILE,
                    &encode_string_field(path_scope::ABSOLUTE_URI, &uri),
                );
                if reencoded == ctx_bytes {
                    return Ok(uri);
                }
            }
        }
    }
    Ok(format!("ctxpb:{}", hex::encode(ctx_bytes)))
}

fn encode_scope_item_bytes(scope_str: &str) -> Vec<u8> {
    if let Some(hex_part) = scope_str.strip_prefix("ctxpb:")
        && let Ok(raw) = hex::decode(hex_part)
    {
        return raw;
    }
    encode_bytes_field(
        context_scope::FILE,
        &encode_string_field(path_scope::ABSOLUTE_URI, scope_str),
    )
}

fn make_agy_exts(
    sub_unknowns: &[UnknownWireField],
    meta_extra: &[u8],
    extra: &[HostExtensionEnvelope],
) -> Vec<HostExtensionEnvelope> {
    let mut out = Vec::new();
    if !sub_unknowns.is_empty() {
        out.push(HostExtensionEnvelope {
            host: HostKind::Antigravity,
            extension_type: "agy_submessage_unknowns".to_owned(),
            raw_payload: encode_unknown_fields(sub_unknowns),
            metadata: Vec::new(),
        });
    }
    if !meta_extra.is_empty() {
        out.push(HostExtensionEnvelope {
            host: HostKind::Antigravity,
            extension_type: "agy_metadata_extra".to_owned(),
            raw_payload: meta_extra.to_vec(),
            metadata: Vec::new(),
        });
    }
    out.extend_from_slice(extra);
    out
}

fn ext_payload<'a>(anchor: &'a EventOriginAnchor, ext_type: &str) -> &'a [u8] {
    for ext in &anchor.host_extensions {
        if ext.extension_type == ext_type && !ext.raw_payload.is_empty() {
            return &ext.raw_payload;
        }
    }
    &[]
}

fn known_range(max_tag: u32) -> Vec<u32> {
    (1..=max_tag).collect()
}

fn decode_user_input(
    row: &AgyRawStepRow,
    oneof_bytes: &[u8],
    turn_id: &TurnId,
    ts_iso: &str,
    step_unknowns: &[UnknownWireField],
    meta_extra: &[u8],
) -> Result<CanonicalEvent, CodecError> {
    let known = known_range(user_input::CLIENT_MESSAGE_ID);
    let (fm, ui_unknowns) = parse_proto_fields_known(oneof_bytes, &known)?;
    let mut item_texts = Vec::new();
    let mut scope_uris = Vec::new();
    for raw in fm_bytes(&fm, user_input::ITEMS) {
        for (ifn, iwt, _, iraw) in iter_wire_fields(raw)? {
            if iwt == 2 {
                if ifn == text_or_scope::TEXT {
                    item_texts.push(String::from_utf8_lossy(&iraw).into_owned());
                } else if ifn == text_or_scope::ITEM {
                    scope_uris.push(decode_scope_item_bytes(&iraw)?);
                }
            }
        }
    }
    let mut media = Vec::new();
    for raw in fm_bytes(&fm, user_input::MEDIA) {
        let (mfm, _) = parse_proto_fields(raw, None)?;
        media.push(MediaAttachment {
            mime_type: fm_last_str(&mfm, media_tag::MIME_TYPE, ""),
            uri: fm_last_str(&mfm, media_tag::URI, ""),
            byte_size: None,
            caption: None,
        });
    }
    let client_msg_id = {
        let s = fm_last_str(&fm, user_input::CLIENT_MESSAGE_ID, "");
        if s.is_empty() { None } else { Some(s) }
    };
    let user_resp = fm_last_str(&fm, user_input::USER_RESPONSE, "");
    let joined_items: String = item_texts
        .into_iter()
        .filter(|t| !t.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    let final_query = if !user_resp.is_empty() {
        user_resp
    } else if !joined_items.is_empty() {
        joined_items
    } else {
        fm_last_str(&fm, user_input::QUERY, "")
    };
    let anchor = EventOriginAnchor {
        origin_host: HostKind::Antigravity,
        turn_id: turn_id.clone(),
        timestamp_iso: ts_iso.to_owned(),
        agy_step_index: Some(row.idx),
        codex_line_no: None,
        client_message_id: client_msg_id,
        unknown_wire_fields: step_unknowns.to_vec(),
        host_extensions: make_agy_exts(&ui_unknowns, meta_extra, &[]),
    };
    Ok(CanonicalEvent::UserInput {
        anchor,
        query_text: final_query,
        scope_items: scope_uris,
        media,
    })
}

struct StepDecodeCtx<'a> {
    events: &'a mut Vec<CanonicalEvent>,
    call_index_by_id: &'a mut HashMap<ToolCallId, usize>,
    unmatched_synthetic: &'a mut VecDeque<ToolCallId>,
}

fn decode_planner_response(
    row: &AgyRawStepRow,
    oneof_bytes: &[u8],
    turn_id: &TurnId,
    meta: &DecodedStepMeta,
    step_unknowns: &[UnknownWireField],
    meta_extra: &[u8],
    ctx: &mut StepDecodeCtx<'_>,
) -> Result<(), CodecError> {
    let known = known_range(planner_response::MAX_KNOWN_TAG);
    let (fm, pr_unknowns) = parse_proto_fields_known(oneof_bytes, &known)?;
    let resp_text = {
        let r = fm_last_str(&fm, planner_response::RESPONSE, "");
        if r.is_empty() {
            fm_last_str(&fm, planner_response::MODIFIED_RESPONSE, "")
        } else {
            r
        }
    };
    let thinking_text = fm_last_str(&fm, planner_response::THINKING, "");
    let raw_thinking = if fm.contains_key(&planner_response::RAW_THINKING) {
        Some(fm_last_str(&fm, planner_response::RAW_THINKING, ""))
    } else {
        None
    };
    let sig_str = fm_last_str(&fm, planner_response::SIGNATURE, "");
    let mut thinking_sig = fm_bytes(&fm, planner_response::THINKING_SIGNATURE)
        .into_iter()
        .last()
        .map(<[u8]>::to_vec);
    if thinking_sig.is_none() && !sig_str.is_empty() {
        thinking_sig = Some(sig_str.into_bytes());
    }
    let dur_ms = fm_bytes(&fm, planner_response::THINKING_DURATION)
        .into_iter()
        .last()
        .map(decode_duration_ms)
        .transpose()?;
    let stop_reason = fm_last_varint(&fm, planner_response::STOP_REASON).map(|v| v.to_string());
    let mut tool_calls_raw = Vec::new();
    for raw in fm_bytes(&fm, planner_response::TOOL_CALLS) {
        tool_calls_raw.push(decode_chat_tool_call(raw)?);
    }

    let usage_with_dur = TokenUsageAccounting {
        input_tokens: meta.input_tokens,
        output_tokens: meta.output_tokens,
        cache_read_tokens: meta.cache_read_tokens,
        thinking_duration_ms: dur_ms,
    };
    let base_anchor = EventOriginAnchor {
        origin_host: HostKind::Antigravity,
        turn_id: turn_id.clone(),
        timestamp_iso: meta.created_at_iso.clone(),
        agy_step_index: Some(row.idx),
        codex_line_no: None,
        client_message_id: None,
        unknown_wire_fields: step_unknowns.to_vec(),
        host_extensions: make_agy_exts(&pr_unknowns, meta_extra, &[]),
    };
    let mut emitted = false;
    if !thinking_text.is_empty() || raw_thinking.is_some() || thinking_sig.is_some() {
        let summary_text = if thinking_text.is_empty() {
            raw_thinking.clone().unwrap_or_default()
        } else {
            thinking_text
        };
        ctx.events.push(CanonicalEvent::Thinking {
            anchor: base_anchor.clone(),
            summary_text,
            raw_thinking,
            thinking_signature: thinking_sig,
            token_usage: usage_with_dur,
        });
        emitted = true;
    }
    if !resp_text.is_empty() || (!emitted && tool_calls_raw.is_empty()) {
        let message_id = {
            let m = fm_last_str(&fm, planner_response::MESSAGE_ID, "");
            if m.is_empty() { None } else { Some(m) }
        };
        ctx.events.push(CanonicalEvent::AssistantText {
            anchor: base_anchor.clone(),
            text: resp_text,
            message_id,
            stop_reason,
            generator_model: meta.generator_model.clone(),
            token_usage: usage_with_dur,
        });
    }
    for tc in tool_calls_raw {
        let trimmed_cid = tc.id.trim();
        let cid = if trimmed_cid.is_empty() {
            let synth =
                ToolCallId::parse(&format!("call-step-{}-{}", row.idx.0, ctx.events.len()))?;
            ctx.unmatched_synthetic.push_back(synth.clone());
            synth
        } else {
            ToolCallId::parse(trimmed_cid)?
        };
        let tname = if tc.name.is_empty() {
            "unknown_tool".to_owned()
        } else {
            tc.name
        };
        let args_json = if tc.arguments_json.is_empty() {
            "{}".to_owned()
        } else {
            tc.arguments_json
        };
        let args_obj = safe_json_dict(&args_json);
        let summary_lbl = args_obj
            .get("toolSummary")
            .or_else(|| args_obj.get("tool_summary"))
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .or_else(|| {
                if meta.tool_summary.is_empty() {
                    None
                } else {
                    Some(meta.tool_summary.clone())
                }
            })
            .unwrap_or_else(|| tname.clone());
        let payload = decode_tool_payload(&tname, &args_json, None, None, false);
        ctx.call_index_by_id.insert(cid.clone(), ctx.events.len());
        ctx.events.push(CanonicalEvent::ToolCall {
            anchor: base_anchor.clone(),
            call_id: cid,
            tool_name: tname,
            tool_summary: summary_lbl,
            payload,
            thinking_signature: tc.thinking_signature,
        });
    }
    Ok(())
}

fn decode_generic_step(
    row: &AgyRawStepRow,
    oneof_bytes: &[u8],
    turn_id: &TurnId,
    meta: &DecodedStepMeta,
    step_unknowns: &[UnknownWireField],
    meta_extra: &[u8],
    ctx: &mut StepDecodeCtx<'_>,
) -> Result<(), CodecError> {
    let tc_meta = meta.tool_call.as_ref();
    let raw_cid = tc_meta.map_or("", |t| t.id.trim());
    let cid = if !raw_cid.is_empty() {
        ToolCallId::parse(raw_cid)?
    } else if let Some(prev) = ctx.unmatched_synthetic.pop_front() {
        prev
    } else {
        ToolCallId::parse(&format!("call-generic-{}", row.idx.0))?
    };
    let tname = tc_meta
        .map(|t| t.name.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("unknown_tool")
        .to_owned();
    let mut args_json = tc_meta
        .map(|t| t.arguments_json.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("{}")
        .to_owned();
    let tool_summary = if meta.tool_summary.is_empty() {
        tname.clone()
    } else {
        meta.tool_summary.clone()
    };

    let (fm, gen_unknowns) =
        parse_proto_fields_known(oneof_bytes, &[generic_step::ARGS, generic_step::RESULT])?;
    let mut args_map: BTreeMap<String, Value> = BTreeMap::new();
    for raw in fm_bytes(&fm, generic_step::ARGS) {
        let (efm, _) = parse_proto_fields(raw, None)?;
        let k_str = fm_last_str(&efm, map_entry::KEY, "");
        if !k_str.is_empty() {
            args_map.insert(
                k_str,
                Value::String(fm_last_str(&efm, map_entry::VALUE, "")),
            );
        }
    }
    let mut result_text = String::new();
    let mut any_url: Option<String> = None;
    let mut any_val: Option<Vec<u8>> = None;
    let mut full_uri: Option<String> = None;
    for raw in fm_bytes(&fm, generic_step::RESULT) {
        let (rfm, _) = parse_proto_fields(raw, None)?;
        let r = fm_last_str(&rfm, generic_result::RESULT, "");
        if !r.is_empty() {
            result_text = r;
        }
        let u = fm_last_str(&rfm, generic_result::FULL_OUTPUT_URI, "");
        if !u.is_empty() {
            full_uri = Some(u);
        }
        for praw in fm_bytes(&rfm, generic_result::PAYLOAD) {
            let (tu, vb) = decode_any_envelope(praw);
            any_url = Some(tu);
            any_val = Some(vb);
        }
    }
    if (args_json.is_empty() || args_json == "{}") && !args_map.is_empty() {
        args_json = cas_core::dump_canonical_json(&args_map);
    }
    let rich_payload = decode_tool_payload(
        &tname,
        &args_json,
        any_url.as_deref(),
        any_val.as_deref(),
        false,
    );
    if let Some(&call_pos) = ctx.call_index_by_id.get(&cid)
        && let Some(CanonicalEvent::ToolCall {
            tool_summary: prior_summary,
            payload: prior_payload,
            thinking_signature: prior_sig,
            ..
        }) = ctx.events.get_mut(call_pos)
    {
        if !tool_summary.is_empty() && tool_summary != tname {
            prior_summary.clone_from(&tool_summary);
        }
        *prior_payload = rich_payload.clone();
        if prior_sig.is_none() {
            *prior_sig = tc_meta.and_then(|t| t.thinking_signature.clone());
        }
    }
    let anchor = EventOriginAnchor {
        origin_host: HostKind::Antigravity,
        turn_id: turn_id.clone(),
        timestamp_iso: meta.created_at_iso.clone(),
        agy_step_index: Some(row.idx),
        codex_line_no: None,
        client_message_id: None,
        unknown_wire_fields: step_unknowns.to_vec(),
        host_extensions: make_agy_exts(&gen_unknowns, meta_extra, &[]),
    };
    let is_err = matches!(
        row.status,
        step_status::ERROR | step_status::CANCELED | step_status::INTERRUPTED
    );
    ctx.events.push(CanonicalEvent::ToolResult {
        anchor,
        call_id: cid,
        tool_name: tname,
        output_text: result_text,
        is_error: is_err,
        payload: rich_payload,
        full_output_uri: full_uri,
    });
    Ok(())
}

fn decode_error_message_step(
    row: &AgyRawStepRow,
    oneof_bytes: &[u8],
    turn_id: &TurnId,
    ts_iso: &str,
    step_unknowns: &[UnknownWireField],
    meta_extra: &[u8],
) -> Result<CanonicalEvent, CodecError> {
    let (fm, _) = parse_proto_fields(oneof_bytes, None)?;
    let mut msg_text = String::new();
    for raw in fm_bytes(&fm, error_message_step::ERROR) {
        let (efm, _) = parse_proto_fields(raw, None)?;
        for tag in [
            error_details::USER_ERROR_MESSAGE,
            error_details::SHORT_ERROR,
            error_details::FULL_ERROR,
        ] {
            let s = fm_last_str(&efm, tag, "");
            if !s.is_empty() {
                msg_text = s;
                break;
            }
        }
    }
    let err_ext = HostExtensionEnvelope {
        host: HostKind::Antigravity,
        extension_type: "agy_error_message_step".to_owned(),
        raw_payload: oneof_bytes.to_vec(),
        metadata: vec![("status".to_owned(), row.status.to_string())],
    };
    let anchor = EventOriginAnchor {
        origin_host: HostKind::Antigravity,
        turn_id: turn_id.clone(),
        timestamp_iso: ts_iso.to_owned(),
        agy_step_index: Some(row.idx),
        codex_line_no: None,
        client_message_id: None,
        unknown_wire_fields: step_unknowns.to_vec(),
        host_extensions: make_agy_exts(&[], meta_extra, &[err_ext]),
    };
    Ok(CanonicalEvent::SystemMessage {
        anchor,
        event_type: "error_message".to_owned(),
        message_text: if msg_text.is_empty() {
            "error_message".to_owned()
        } else {
            msg_text
        },
    })
}

fn decode_framing_step(
    spec: &StepWireSpec,
    row: &AgyRawStepRow,
    oneof_bytes: &[u8],
    turn_id: &TurnId,
    ts_iso: &str,
    step_unknowns: &[UnknownWireField],
    meta_extra: &[u8],
) -> Result<(CanonicalEvent, Option<String>), CodecError> {
    match spec.step_type {
        CortexStepTypeCode::Checkpoint => {
            let known = known_range(checkpoint_tag::MAX_KNOWN_TAG);
            let (fm, unknowns) = parse_proto_fields_known(oneof_bytes, &known)?;
            let cp_idx = fm_last_varint(&fm, checkpoint_tag::CHECKPOINT_INDEX).unwrap_or(0) as i64;
            let conv_title = {
                let s = fm_last_str(&fm, checkpoint_tag::CONVERSATION_TITLE, "");
                if s.is_empty() { None } else { Some(s) }
            };
            let anchor = EventOriginAnchor {
                origin_host: HostKind::Antigravity,
                turn_id: turn_id.clone(),
                timestamp_iso: ts_iso.to_owned(),
                agy_step_index: Some(row.idx),
                codex_line_no: None,
                client_message_id: None,
                unknown_wire_fields: step_unknowns.to_vec(),
                host_extensions: make_agy_exts(&unknowns, meta_extra, &[]),
            };
            Ok((
                CanonicalEvent::Checkpoint {
                    anchor,
                    checkpoint_index: cp_idx,
                    user_intent: fm_last_str(&fm, checkpoint_tag::USER_INTENT, ""),
                    session_summary: fm_last_str(&fm, checkpoint_tag::SESSION_SUMMARY, ""),
                    code_change_summary: fm_last_str(&fm, checkpoint_tag::CODE_CHANGE_SUMMARY, ""),
                    conversation_title: conv_title,
                },
                None,
            ))
        }
        CortexStepTypeCode::TaskBoundary => {
            let known = known_range(task_boundary_tag::MAX_KNOWN_TAG);
            let (fm, unknowns) = parse_proto_fields_known(oneof_bytes, &known)?;
            let t_status = fm_last_str(&fm, task_boundary_tag::TASK_STATUS, "");
            let t_mode = fm_last_varint(&fm, task_boundary_tag::MODE).map(|v| v.to_string());
            let anchor = EventOriginAnchor {
                origin_host: HostKind::Antigravity,
                turn_id: turn_id.clone(),
                timestamp_iso: ts_iso.to_owned(),
                agy_step_index: Some(row.idx),
                codex_line_no: None,
                client_message_id: None,
                unknown_wire_fields: step_unknowns.to_vec(),
                host_extensions: make_agy_exts(&unknowns, meta_extra, &[]),
            };
            let status_out = Some(t_status.clone());
            Ok((
                CanonicalEvent::TaskBoundary {
                    anchor,
                    boundary_kind: TaskBoundaryKind::TaskBoundary,
                    task_name: fm_last_str(&fm, task_boundary_tag::TASK_NAME, ""),
                    task_status: if t_status.is_empty() {
                        "DONE".to_owned()
                    } else {
                        t_status
                    },
                    task_summary: fm_last_str(&fm, task_boundary_tag::TASK_SUMMARY, ""),
                    mode: t_mode,
                    blocked_on_user: false,
                    review_uris: Vec::new(),
                },
                status_out,
            ))
        }
        CortexStepTypeCode::NotifyUser => {
            let known = known_range(notify_user_tag::MAX_KNOWN_TAG);
            let (fm, unknowns) = parse_proto_fields_known(oneof_bytes, &known)?;
            let r_uris: Vec<String> = fm_bytes(&fm, notify_user_tag::REVIEW_ABSOLUTE_URIS)
                .into_iter()
                .map(|b| String::from_utf8_lossy(b).into_owned())
                .collect();
            let blocking =
                fm_last_varint(&fm, notify_user_tag::IS_BLOCKING).is_some_and(|v| v != 0);
            let anchor = EventOriginAnchor {
                origin_host: HostKind::Antigravity,
                turn_id: turn_id.clone(),
                timestamp_iso: ts_iso.to_owned(),
                agy_step_index: Some(row.idx),
                codex_line_no: None,
                client_message_id: None,
                unknown_wire_fields: step_unknowns.to_vec(),
                host_extensions: make_agy_exts(&unknowns, meta_extra, &[]),
            };
            Ok((
                CanonicalEvent::TaskBoundary {
                    anchor,
                    boundary_kind: TaskBoundaryKind::NotifyUser,
                    task_name: "notify_user".to_owned(),
                    task_status: "DONE".to_owned(),
                    task_summary: fm_last_str(&fm, notify_user_tag::NOTIFICATION_CONTENT, ""),
                    mode: None,
                    blocked_on_user: blocking,
                    review_uris: r_uris,
                },
                Some("DONE".to_owned()),
            ))
        }
        _ => {
            let known = known_range(system_message_tag::MAX_KNOWN_TAG);
            let (fm, unknowns) = parse_proto_fields_known(oneof_bytes, &known)?;
            let ev_type = fm_last_str(&fm, system_message_tag::EVENT_TYPE, "");
            let anchor = EventOriginAnchor {
                origin_host: HostKind::Antigravity,
                turn_id: turn_id.clone(),
                timestamp_iso: ts_iso.to_owned(),
                agy_step_index: Some(row.idx),
                codex_line_no: None,
                client_message_id: None,
                unknown_wire_fields: step_unknowns.to_vec(),
                host_extensions: make_agy_exts(&unknowns, meta_extra, &[]),
            };
            Ok((
                CanonicalEvent::SystemMessage {
                    anchor,
                    event_type: if ev_type.is_empty() {
                        "system_message".to_owned()
                    } else {
                        ev_type
                    },
                    message_text: fm_last_str(&fm, system_message_tag::MESSAGE, ""),
                },
                None,
            ))
        }
    }
}

struct ClassifiedStepRow {
    meta: DecodedStepMeta,
    meta_extra: Vec<u8>,
    spec: Option<&'static StepWireSpec>,
    is_err_msg: bool,
    oneof_bytes: Vec<u8>,
    step_unknowns: Vec<UnknownWireField>,
}

fn classify_raw_step_row(row: &AgyRawStepRow) -> Result<ClassifiedStepRow, CodecError> {
    let step_fields = if row.step_payload_blob.is_empty() {
        Vec::new()
    } else {
        iter_wire_fields(&row.step_payload_blob)?
    };
    let raw_meta = if !row.metadata_blob.is_empty() {
        row.metadata_blob.as_slice()
    } else {
        step_fields
            .iter()
            .find(|(fn_tag, wt, _, _)| *fn_tag == step_field::METADATA && *wt == 2)
            .map_or(&[][..], |(_, _, _, raw)| raw.as_slice())
    };
    let (meta, meta_extra) = if raw_meta.is_empty() {
        (DecodedStepMeta::default(), Vec::new())
    } else {
        decode_step_metadata(raw_meta)?
    };
    let mut spec = find_step_spec_by_type(row.step_type);
    let mut is_err_msg = row.step_type == CortexStepTypeCode::ErrorMessage as i32;
    let mut oneof_bytes = Vec::new();
    let mut step_unknowns = Vec::new();

    for (fn_tag, wt, _, raw) in step_fields {
        if !is_known_step_tag(fn_tag) {
            step_unknowns.push(UnknownWireField {
                field_tag: fn_tag,
                wire_type: wt,
                raw_bytes: raw,
            });
        } else if wt == 2
            && ((is_err_msg && fn_tag == GeminiStepWireTag::ErrorMessage as u32)
                || spec.is_some_and(|s| fn_tag == s.step_oneof_tag as u32))
        {
            oneof_bytes = raw;
        } else if spec.is_none()
            && !is_err_msg
            && fn_tag == GeminiStepWireTag::ErrorMessage as u32
            && wt == 2
        {
            is_err_msg = true;
            oneof_bytes = raw;
        } else if spec.is_none()
            && !is_err_msg
            && wt == 2
            && let Some(found) = find_step_spec_by_tag(fn_tag)
        {
            spec = Some(found);
            oneof_bytes = raw;
        }
    }
    Ok(ClassifiedStepRow {
        meta,
        meta_extra,
        spec,
        is_err_msg,
        oneof_bytes,
        step_unknowns,
    })
}

/// Parse raw Antigravity `steps` rows into an indexed [`TrajectoryLedger`].
///
/// # Errors
/// Returns [`CodecError::Wire`] or [`CodecError::Core`] if step wire bytes or identifiers are invalid.
pub fn decode_agy_trajectory(
    workspace_path: WorkspacePath,
    workspace_uri: WorkspaceRootUri,
    raw_steps: &[AgyRawStepRow],
    explicit_title_override: Option<&str>,
) -> Result<TrajectoryLedger, CodecError> {
    let mut events = Vec::new();
    let mut call_index_by_id = HashMap::new();
    let mut unmatched_synthetic = VecDeque::new();
    let mut turn_num: u32 = 1;
    let mut current_turn_id = TurnId::parse("turn-1")?;
    let mut turn_has_response = false;

    for row in raw_steps {
        let start_ev_len = events.len();
        let classified = classify_raw_step_row(row)?;
        let ts_iso = classified.meta.created_at_iso.as_str();

        if classified.is_err_msg {
            turn_has_response = true;
            events.push(decode_error_message_step(
                row,
                &classified.oneof_bytes,
                &current_turn_id,
                ts_iso,
                &classified.step_unknowns,
                &classified.meta_extra,
            )?);
        } else if let Some(spec) = classified.spec {
            match spec.step_type {
                CortexStepTypeCode::UserInput => {
                    if turn_has_response {
                        turn_num += 1;
                        current_turn_id = TurnId::parse(&format!("turn-{turn_num}"))?;
                        turn_has_response = false;
                    }
                    events.push(decode_user_input(
                        row,
                        &classified.oneof_bytes,
                        &current_turn_id,
                        ts_iso,
                        &classified.step_unknowns,
                        &classified.meta_extra,
                    )?);
                }
                CortexStepTypeCode::PlannerResponse => {
                    turn_has_response = true;
                    let mut ctx = StepDecodeCtx {
                        events: &mut events,
                        call_index_by_id: &mut call_index_by_id,
                        unmatched_synthetic: &mut unmatched_synthetic,
                    };
                    decode_planner_response(
                        row,
                        &classified.oneof_bytes,
                        &current_turn_id,
                        &classified.meta,
                        &classified.step_unknowns,
                        &classified.meta_extra,
                        &mut ctx,
                    )?;
                }
                CortexStepTypeCode::Generic => {
                    turn_has_response = true;
                    let mut ctx = StepDecodeCtx {
                        events: &mut events,
                        call_index_by_id: &mut call_index_by_id,
                        unmatched_synthetic: &mut unmatched_synthetic,
                    };
                    decode_generic_step(
                        row,
                        &classified.oneof_bytes,
                        &current_turn_id,
                        &classified.meta,
                        &classified.step_unknowns,
                        &classified.meta_extra,
                        &mut ctx,
                    )?;
                }
                CortexStepTypeCode::TaskBoundary => {
                    let (mut probe_ev, raw_status) = decode_framing_step(
                        spec,
                        row,
                        &classified.oneof_bytes,
                        &current_turn_id,
                        ts_iso,
                        &classified.step_unknowns,
                        &classified.meta_extra,
                    )?;
                    let st_low = raw_status.unwrap_or_default().to_ascii_lowercase();
                    if matches!(
                        st_low.as_str(),
                        "task_started" | "running" | "in_progress" | "starting"
                    ) {
                        if turn_has_response {
                            turn_num += 1;
                            current_turn_id = TurnId::parse(&format!("turn-{turn_num}"))?;
                            turn_has_response = false;
                            (probe_ev, _) = decode_framing_step(
                                spec,
                                row,
                                &classified.oneof_bytes,
                                &current_turn_id,
                                ts_iso,
                                &classified.step_unknowns,
                                &classified.meta_extra,
                            )?;
                        }
                    } else {
                        turn_has_response = true;
                    }
                    events.push(probe_ev);
                }
                _ => {
                    if spec.step_type == CortexStepTypeCode::NotifyUser {
                        turn_has_response = true;
                    }
                    let (ev_out, _) = decode_framing_step(
                        spec,
                        row,
                        &classified.oneof_bytes,
                        &current_turn_id,
                        ts_iso,
                        &classified.step_unknowns,
                        &classified.meta_extra,
                    )?;
                    events.push(ev_out);
                }
            }
        } else {
            turn_has_response = true;
            let op_ext = HostExtensionEnvelope {
                host: HostKind::Antigravity,
                extension_type: "agy_opaque_step".to_owned(),
                raw_payload: row.step_payload_blob.clone(),
                metadata: vec![
                    ("step_type".into(), row.step_type.to_string()),
                    ("status".into(), row.status.to_string()),
                    (
                        "has_subtrajectory".into(),
                        if row.has_subtrajectory { "1" } else { "0" }.to_owned(),
                    ),
                    ("step_format".into(), row.step_format.to_string()),
                    ("metadata_hex".into(), hex::encode(&row.metadata_blob)),
                ],
            };
            let anchor = EventOriginAnchor {
                origin_host: HostKind::Antigravity,
                turn_id: current_turn_id.clone(),
                timestamp_iso: ts_iso.to_owned(),
                agy_step_index: Some(row.idx),
                codex_line_no: None,
                client_message_id: None,
                unknown_wire_fields: Vec::new(),
                host_extensions: vec![op_ext],
            };
            events.push(CanonicalEvent::SystemMessage {
                anchor,
                event_type: format!("opaque_agy_step:{}", row.step_type),
                message_text: format!("agy_step_type:{}", row.step_type),
            });
        }

        if step_status::is_active(row.status) {
            let st_str = row.status.to_string();
            let st_ext = HostExtensionEnvelope {
                host: HostKind::Antigravity,
                extension_type: "agy_step_status".to_owned(),
                raw_payload: Vec::new(),
                metadata: vec![
                    ("status".into(), st_str.clone()),
                    ("agy_active_status".into(), st_str),
                ],
            };
            for ev in &mut events[start_ev_len..] {
                ev.anchor_mut().host_extensions.push(st_ext.clone());
            }
        }
    }

    Ok(TrajectoryLedger::from_events(
        workspace_path,
        workspace_uri,
        &events,
        explicit_title_override,
        false,
    ))
}

/// Bind `metadata_blob` and `step_payload_blob` of an Antigravity step row to `(trajectory_id, cascade_id)`.
///
/// # Errors
/// Returns [`CodecError::Wire`] if existing wire bytes cannot be parsed.
pub fn bind_agy_step(
    row: &AgyRawStepRow,
    trajectory_id: &str,
    cascade_id: &str,
) -> Result<AgyRawStepRow, CodecError> {
    let mut reference = encode_string_field(1, trajectory_id);
    reference.extend_from_slice(&encode_varint_field(2, i64::from(row.idx.0)));
    reference.extend_from_slice(&encode_string_field(4, cascade_id));

    let meta_kept: Vec<UnknownWireField> = iter_wire_fields(&row.metadata_blob)?
        .into_iter()
        .filter(|(n, _, _, _)| *n != 20)
        .map(|(field_tag, wire_type, _, raw_bytes)| UnknownWireField {
            field_tag,
            wire_type,
            raw_bytes,
        })
        .collect();
    let mut metadata = encode_unknown_fields(&meta_kept);
    metadata.extend_from_slice(&encode_bytes_field(20, &reference));

    let payload_kept: Vec<UnknownWireField> = iter_wire_fields(&row.step_payload_blob)?
        .into_iter()
        .filter(|(n, _, _, _)| *n != step_field::METADATA)
        .map(|(field_tag, wire_type, _, raw_bytes)| UnknownWireField {
            field_tag,
            wire_type,
            raw_bytes,
        })
        .collect();
    let mut payload = encode_unknown_fields(&payload_kept);
    payload.extend_from_slice(&encode_bytes_field(step_field::METADATA, &metadata));

    let mut out = row.clone();
    out.metadata_blob = metadata;
    out.step_payload_blob = payload;
    Ok(out)
}

fn wrap_step_row(
    idx: u32,
    step_type: i32,
    oneof_tag: u32,
    oneof_payload: &[u8],
    meta_blob: &[u8],
    unknowns: &[UnknownWireField],
    status: i32,
) -> AgyRawStepRow {
    let mut step_buf = encode_varint_field(step_field::TYPE, i64::from(step_type));
    step_buf.extend_from_slice(&encode_varint_field(step_field::STATUS, i64::from(status)));
    step_buf.extend_from_slice(&encode_bytes_field(step_field::METADATA, meta_blob));
    step_buf.extend_from_slice(&encode_bytes_field(oneof_tag, oneof_payload));
    step_buf.extend_from_slice(&encode_unknown_fields(unknowns));
    AgyRawStepRow {
        idx: StepIndex(idx),
        step_type,
        status,
        has_subtrajectory: false,
        metadata_blob: meta_blob.to_vec(),
        step_payload_blob: step_buf,
        step_format: 0,
    }
}

fn encode_planner_group(
    events: &[CanonicalEvent],
    start_pos: usize,
    next_idx: u32,
    resolved_call_ids: &HashSet<ToolCallId>,
) -> (AgyRawStepRow, usize) {
    let first = &events[start_pos];
    let group_turn = &first.anchor().turn_id;
    let group_step_idx = if first.anchor().origin_host == HostKind::Antigravity {
        first.anchor().agy_step_index
    } else {
        None
    };

    let mut thinking_ev: Option<&CanonicalEvent> = None;
    let mut text_ev: Option<&CanonicalEvent> = None;
    let mut tool_evs: Vec<&CanonicalEvent> = Vec::new();
    let mut unknowns: Vec<UnknownWireField> = Vec::new();
    let mut seen_ukeys: HashSet<(u32, u8, Vec<u8>)> = HashSet::new();
    let mut pos = start_pos;

    while pos < events.len() {
        let cand = &events[pos];
        let is_planner_member = matches!(
            cand,
            CanonicalEvent::Thinking { .. }
                | CanonicalEvent::AssistantText { .. }
                | CanonicalEvent::ToolCall { .. }
        );
        if !is_planner_member || &cand.anchor().turn_id != group_turn {
            break;
        }
        if let Some(gs) = group_step_idx
            && cand.anchor().origin_host == HostKind::Antigravity
        {
            if cand.anchor().agy_step_index != Some(gs) {
                break;
            }
        } else if (matches!(cand, CanonicalEvent::Thinking { .. })
            && (thinking_ev.is_some() || text_ev.is_some() || !tool_evs.is_empty()))
            || (matches!(cand, CanonicalEvent::AssistantText { .. })
                && (text_ev.is_some() || !tool_evs.is_empty()))
        {
            break;
        }

        match cand {
            CanonicalEvent::Thinking { .. } => thinking_ev = Some(cand),
            CanonicalEvent::AssistantText { .. } => text_ev = Some(cand),
            CanonicalEvent::ToolCall { .. } => tool_evs.push(cand),
            _ => {}
        }
        for uf in &cand.anchor().unknown_wire_fields {
            let key = (uf.field_tag, uf.wire_type, uf.raw_bytes.clone());
            if seen_ukeys.insert(key) {
                unknowns.push(uf.clone());
            }
        }
        pos += 1;
    }

    let lead = thinking_ev
        .or(text_ev)
        .or_else(|| tool_evs.first().copied())
        .unwrap_or(first);
    let usage = if let Some(CanonicalEvent::AssistantText { token_usage, .. }) = text_ev {
        *token_usage
    } else if let Some(CanonicalEvent::Thinking { token_usage, .. }) = thinking_ev {
        *token_usage
    } else {
        TokenUsageAccounting::default()
    };

    let mut pr_buf = Vec::new();
    if let Some(CanonicalEvent::AssistantText { text, .. }) = text_ev
        && !text.is_empty()
    {
        pr_buf.extend_from_slice(&encode_string_field(planner_response::RESPONSE, text));
    }
    if let Some(CanonicalEvent::Thinking { summary_text, .. }) = thinking_ev
        && !summary_text.is_empty()
    {
        pr_buf.extend_from_slice(&encode_string_field(
            planner_response::THINKING,
            summary_text,
        ));
    }
    if let Some(CanonicalEvent::AssistantText {
        message_id: Some(mid),
        ..
    }) = text_ev
        && !mid.is_empty()
    {
        pr_buf.extend_from_slice(&encode_string_field(planner_response::MESSAGE_ID, mid));
    }
    for ev in &tool_evs {
        if let CanonicalEvent::ToolCall {
            call_id,
            tool_name,
            payload,
            thinking_signature,
            ..
        } = ev
        {
            let agy_name = find_tool_spec_by_name(payload.kind_str())
                .map_or(tool_name.as_str(), |s| s.agy_tool_name);
            let args_json =
                serialize_agy_chat_tool_arguments(payload, resolved_call_ids.contains(call_id));
            let ctc = encode_chat_tool_call(
                call_id.as_str(),
                agy_name,
                &args_json,
                thinking_signature.as_deref(),
            );
            pr_buf.extend_from_slice(&encode_bytes_field(planner_response::TOOL_CALLS, &ctc));
        }
    }
    if let Some(dur_ms) = usage.thinking_duration_ms {
        let dur_b = encode_duration_proto(dur_ms);
        if !dur_b.is_empty() {
            pr_buf.extend_from_slice(&encode_bytes_field(
                planner_response::THINKING_DURATION,
                &dur_b,
            ));
        }
    }
    if let Some(CanonicalEvent::AssistantText {
        stop_reason: Some(sr),
        ..
    }) = text_ev
        && let Ok(sr_int) = sr.parse::<i64>()
    {
        pr_buf.extend_from_slice(&encode_varint_field(planner_response::STOP_REASON, sr_int));
    }
    if let Some(CanonicalEvent::Thinking {
        thinking_signature: Some(sig),
        ..
    }) = thinking_ev
        && !sig.is_empty()
    {
        pr_buf.extend_from_slice(&encode_bytes_field(
            planner_response::THINKING_SIGNATURE,
            sig,
        ));
    }
    if let Some(CanonicalEvent::Thinking {
        raw_thinking: Some(rt),
        ..
    }) = thinking_ev
        && !rt.is_empty()
    {
        pr_buf.extend_from_slice(&encode_string_field(planner_response::RAW_THINKING, rt));
    }
    pr_buf.extend_from_slice(ext_payload(lead.anchor(), "agy_submessage_unknowns"));

    let gen_model = if let Some(CanonicalEvent::AssistantText {
        generator_model, ..
    }) = text_ev
    {
        generator_model.as_deref()
    } else {
        None
    };
    let meta_blob = encode_step_metadata(
        &lead.anchor().timestamp_iso,
        step_source::MODEL,
        None,
        "",
        Some(usage),
        gen_model,
        ext_payload(lead.anchor(), "agy_metadata_extra"),
    );
    let row = wrap_step_row(
        next_idx,
        CortexStepTypeCode::PlannerResponse as i32,
        GeminiStepWireTag::PlannerResponse as u32,
        &pr_buf,
        &meta_blob,
        &unknowns,
        step_status::DONE,
    );
    (row, pos)
}

fn encode_system_event_step(ev: &CanonicalEvent, next_idx: u32) -> AgyRawStepRow {
    let CanonicalEvent::SystemMessage {
        anchor,
        event_type,
        message_text,
    } = ev
    else {
        return AgyRawStepRow {
            idx: StepIndex(next_idx),
            step_type: CortexStepTypeCode::SystemMessage as i32,
            status: step_status::DONE,
            has_subtrajectory: false,
            metadata_blob: Vec::new(),
            step_payload_blob: Vec::new(),
            step_format: 0,
        };
    };

    for ext in &anchor.host_extensions {
        if ext.extension_type == "agy_opaque_step" && !ext.raw_payload.is_empty() {
            let md: HashMap<&str, &str> = ext
                .metadata
                .iter()
                .map(|(k, v)| (k.as_str(), v.as_str()))
                .collect();
            let meta_blob = md
                .get("metadata_hex")
                .filter(|s| !s.is_empty())
                .and_then(|h| hex::decode(h).ok())
                .unwrap_or_else(|| {
                    encode_step_metadata(
                        &anchor.timestamp_iso,
                        step_source::SYSTEM,
                        None,
                        "",
                        None,
                        None,
                        &[],
                    )
                });
            return AgyRawStepRow {
                idx: StepIndex(next_idx),
                step_type: md
                    .get("step_type")
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(101),
                status: md
                    .get("status")
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(step_status::DONE),
                has_subtrajectory: md.get("has_subtrajectory").is_some_and(|v| *v == "1"),
                metadata_blob: meta_blob,
                step_payload_blob: ext.raw_payload.clone(),
                step_format: md
                    .get("step_format")
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(1),
            };
        }
    }

    let meta_blob = encode_step_metadata(
        &anchor.timestamp_iso,
        step_source::SYSTEM,
        None,
        "",
        None,
        None,
        ext_payload(anchor, "agy_metadata_extra"),
    );

    if event_type == "error_message" {
        let mut status_code = step_status::DONE;
        let mut err_oneof = Vec::new();
        for ext in &anchor.host_extensions {
            if ext.extension_type == "agy_error_message_step" && !ext.raw_payload.is_empty() {
                err_oneof = ext.raw_payload.clone();
                for (k, v) in &ext.metadata {
                    if k == "status"
                        && let Ok(parsed) = v.parse::<i32>()
                    {
                        status_code = parsed;
                    }
                }
            }
        }
        if err_oneof.is_empty() {
            let mut details = encode_string_field(error_details::USER_ERROR_MESSAGE, message_text);
            details.extend_from_slice(&encode_string_field(
                error_details::SHORT_ERROR,
                message_text,
            ));
            err_oneof = encode_bytes_field(error_message_step::ERROR, &details);
            err_oneof.extend_from_slice(&encode_varint_field(
                error_message_step::SHOULD_SHOW_USER,
                1,
            ));
        }
        return wrap_step_row(
            next_idx,
            CortexStepTypeCode::ErrorMessage as i32,
            GeminiStepWireTag::ErrorMessage as u32,
            &err_oneof,
            &meta_blob,
            &anchor.unknown_wire_fields,
            status_code,
        );
    }

    let mut sm_buf = encode_string_field(system_message_tag::MESSAGE, message_text);
    sm_buf.extend_from_slice(&encode_string_field(
        system_message_tag::EVENT_TYPE,
        event_type,
    ));
    sm_buf.extend_from_slice(ext_payload(anchor, "agy_submessage_unknowns"));
    wrap_step_row(
        next_idx,
        CortexStepTypeCode::SystemMessage as i32,
        GeminiStepWireTag::SystemMessage as u32,
        &sm_buf,
        &meta_blob,
        &anchor.unknown_wire_fields,
        step_status::DONE,
    )
}

fn encode_single_agy_event(
    ev: &CanonicalEvent,
    next_idx: u32,
    call_by_id: &HashMap<ToolCallId, (&str, Option<&[u8]>)>,
) -> Option<AgyRawStepRow> {
    match ev {
        CanonicalEvent::UserInput {
            anchor,
            query_text,
            scope_items,
            media,
        } => {
            let mut ui_buf = encode_string_field(user_input::QUERY, query_text);
            ui_buf.extend_from_slice(&encode_string_field(user_input::USER_RESPONSE, query_text));
            ui_buf.extend_from_slice(&encode_bytes_field(
                user_input::ITEMS,
                &encode_string_field(text_or_scope::TEXT, query_text),
            ));
            for s_item in scope_items {
                ui_buf.extend_from_slice(&encode_bytes_field(
                    user_input::ITEMS,
                    &encode_bytes_field(text_or_scope::ITEM, &encode_scope_item_bytes(s_item)),
                ));
            }
            for m in media {
                let mut mm = encode_string_field(media_tag::MIME_TYPE, &m.mime_type);
                mm.extend_from_slice(&encode_string_field(media_tag::URI, &m.uri));
                ui_buf.extend_from_slice(&encode_bytes_field(user_input::MEDIA, &mm));
            }
            if let Some(cid) = anchor
                .client_message_id
                .as_deref()
                .filter(|s| !s.is_empty())
            {
                ui_buf.extend_from_slice(&encode_string_field(user_input::CLIENT_MESSAGE_ID, cid));
            }
            ui_buf.extend_from_slice(ext_payload(anchor, "agy_submessage_unknowns"));
            let meta_blob = encode_step_metadata(
                &anchor.timestamp_iso,
                step_source::USER_EXPLICIT,
                None,
                "",
                None,
                None,
                ext_payload(anchor, "agy_metadata_extra"),
            );
            Some(wrap_step_row(
                next_idx,
                CortexStepTypeCode::UserInput as i32,
                GeminiStepWireTag::UserInput as u32,
                &ui_buf,
                &meta_blob,
                &anchor.unknown_wire_fields,
                step_status::DONE,
            ))
        }
        CanonicalEvent::ToolResult {
            anchor,
            call_id,
            tool_name,
            output_text,
            is_error,
            payload,
            full_output_uri,
        } => {
            let agy_name = find_tool_spec_by_name(payload.kind_str())
                .map_or(tool_name.as_str(), |s| s.agy_tool_name);
            let args_json = serialize_agy_chat_tool_arguments(payload, true);
            let args_dict = safe_json_dict(&args_json);
            let mut gen_buf = Vec::new();
            for (k, v) in &args_dict {
                let v_str = match v {
                    Value::String(s) => s.clone(),
                    other => json!(other).to_string(),
                };
                let mut entry = encode_string_field(map_entry::KEY, k);
                entry.extend_from_slice(&encode_string_field(map_entry::VALUE, &v_str));
                gen_buf.extend_from_slice(&encode_bytes_field(generic_step::ARGS, &entry));
            }
            let mut res_buf = encode_string_field(generic_result::RESULT, output_text);
            let any_b = encode_tool_payload_to_any(payload);
            if !any_b.is_empty() {
                res_buf.extend_from_slice(&encode_bytes_field(generic_result::PAYLOAD, &any_b));
            }
            if let Some(fu) = full_output_uri.as_deref().filter(|s| !s.is_empty()) {
                res_buf
                    .extend_from_slice(&encode_string_field(generic_result::FULL_OUTPUT_URI, fu));
            }
            gen_buf.extend_from_slice(&encode_bytes_field(generic_step::RESULT, &res_buf));
            gen_buf.extend_from_slice(ext_payload(anchor, "agy_submessage_unknowns"));

            let paired = call_by_id.get(call_id).copied();
            let summary_lbl = paired
                .map(|(s, _)| s)
                .filter(|s| !s.is_empty())
                .unwrap_or(agy_name);
            let tc = DecodedChatToolCall {
                id: call_id.as_str().to_owned(),
                name: agy_name.to_owned(),
                arguments_json: args_json,
                thinking_signature: paired.and_then(|(_, sig)| sig.map(<[u8]>::to_vec)),
            };
            let meta_blob = encode_step_metadata(
                &anchor.timestamp_iso,
                step_source::MODEL,
                Some(&tc),
                summary_lbl,
                None,
                None,
                ext_payload(anchor, "agy_metadata_extra"),
            );
            let status_code = if *is_error {
                if output_text == INTERRUPTED_TOOL_OUTPUT {
                    step_status::CANCELED
                } else {
                    step_status::ERROR
                }
            } else {
                step_status::DONE
            };
            Some(wrap_step_row(
                next_idx,
                CortexStepTypeCode::Generic as i32,
                GeminiStepWireTag::Generic as u32,
                &gen_buf,
                &meta_blob,
                &anchor.unknown_wire_fields,
                status_code,
            ))
        }
        CanonicalEvent::Checkpoint {
            anchor,
            checkpoint_index,
            user_intent,
            session_summary,
            code_change_summary,
            conversation_title,
        } => {
            let mut cp_buf = Vec::new();
            if *checkpoint_index != 0 {
                cp_buf.extend_from_slice(&encode_varint_field(
                    checkpoint_tag::CHECKPOINT_INDEX,
                    *checkpoint_index,
                ));
            }
            cp_buf.extend_from_slice(&encode_string_field(
                checkpoint_tag::USER_INTENT,
                user_intent,
            ));
            cp_buf.extend_from_slice(&encode_string_field(
                checkpoint_tag::SESSION_SUMMARY,
                session_summary,
            ));
            cp_buf.extend_from_slice(&encode_string_field(
                checkpoint_tag::CODE_CHANGE_SUMMARY,
                code_change_summary,
            ));
            if let Some(title) = conversation_title.as_deref().filter(|s| !s.is_empty()) {
                cp_buf.extend_from_slice(&encode_string_field(
                    checkpoint_tag::CONVERSATION_TITLE,
                    title,
                ));
            }
            cp_buf.extend_from_slice(ext_payload(anchor, "agy_submessage_unknowns"));
            let meta_blob = encode_step_metadata(
                &anchor.timestamp_iso,
                step_source::SYSTEM,
                None,
                "",
                None,
                None,
                ext_payload(anchor, "agy_metadata_extra"),
            );
            Some(wrap_step_row(
                next_idx,
                CortexStepTypeCode::Checkpoint as i32,
                GeminiStepWireTag::Checkpoint as u32,
                &cp_buf,
                &meta_blob,
                &anchor.unknown_wire_fields,
                step_status::DONE,
            ))
        }
        CanonicalEvent::TaskBoundary {
            anchor,
            boundary_kind,
            task_name,
            task_status,
            task_summary,
            mode,
            blocked_on_user,
            review_uris,
        } => {
            let meta_blob = encode_step_metadata(
                &anchor.timestamp_iso,
                step_source::MODEL,
                None,
                "",
                None,
                None,
                ext_payload(anchor, "agy_metadata_extra"),
            );
            if *boundary_kind == TaskBoundaryKind::NotifyUser {
                let mut nu_buf = Vec::new();
                for u in review_uris {
                    nu_buf.extend_from_slice(&encode_string_field(
                        notify_user_tag::REVIEW_ABSOLUTE_URIS,
                        u,
                    ));
                }
                nu_buf.extend_from_slice(&encode_string_field(
                    notify_user_tag::NOTIFICATION_CONTENT,
                    task_summary,
                ));
                if *blocked_on_user {
                    nu_buf.extend_from_slice(&encode_varint_field(notify_user_tag::IS_BLOCKING, 1));
                }
                nu_buf.extend_from_slice(ext_payload(anchor, "agy_submessage_unknowns"));
                Some(wrap_step_row(
                    next_idx,
                    CortexStepTypeCode::NotifyUser as i32,
                    GeminiStepWireTag::NotifyUser as u32,
                    &nu_buf,
                    &meta_blob,
                    &anchor.unknown_wire_fields,
                    step_status::DONE,
                ))
            } else {
                let mut tb_buf = encode_string_field(task_boundary_tag::TASK_NAME, task_name);
                tb_buf.extend_from_slice(&encode_string_field(
                    task_boundary_tag::TASK_STATUS,
                    task_status,
                ));
                tb_buf.extend_from_slice(&encode_string_field(
                    task_boundary_tag::TASK_SUMMARY,
                    task_summary,
                ));
                if let Some(m) = mode.as_deref()
                    && let Ok(m_int) = m.parse::<i64>()
                {
                    tb_buf.extend_from_slice(&encode_varint_field(task_boundary_tag::MODE, m_int));
                }
                tb_buf.extend_from_slice(ext_payload(anchor, "agy_submessage_unknowns"));
                Some(wrap_step_row(
                    next_idx,
                    CortexStepTypeCode::TaskBoundary as i32,
                    GeminiStepWireTag::TaskBoundary as u32,
                    &tb_buf,
                    &meta_blob,
                    &anchor.unknown_wire_fields,
                    step_status::DONE,
                ))
            }
        }
        CanonicalEvent::SystemMessage { .. } => Some(encode_system_event_step(ev, next_idx)),
        CanonicalEvent::Thinking { .. }
        | CanonicalEvent::AssistantText { .. }
        | CanonicalEvent::ToolCall { .. } => None,
    }
}

/// Serialize a slice of [`CanonicalEvent`] nodes into Antigravity `steps` rows.
#[must_use]
pub fn encode_agy_trajectory(
    events: &[CanonicalEvent],
    starting_step_idx: StepIndex,
) -> Vec<AgyRawStepRow> {
    let mut rows = Vec::new();
    let mut next_idx = starting_step_idx.0;
    let resolved_ids: HashSet<ToolCallId> = events
        .iter()
        .filter_map(|ev| match ev {
            CanonicalEvent::ToolResult { call_id, .. } => Some(call_id.clone()),
            _ => None,
        })
        .collect();
    let call_by_id: HashMap<ToolCallId, (&str, Option<&[u8]>)> = events
        .iter()
        .filter_map(|ev| match ev {
            CanonicalEvent::ToolCall {
                call_id,
                tool_summary,
                thinking_signature,
                ..
            } => Some((
                call_id.clone(),
                (tool_summary.as_str(), thinking_signature.as_deref()),
            )),
            _ => None,
        })
        .collect();

    let mut pos = 0usize;
    while pos < events.len() {
        let ev = &events[pos];
        if matches!(
            ev,
            CanonicalEvent::Thinking { .. }
                | CanonicalEvent::AssistantText { .. }
                | CanonicalEvent::ToolCall { .. }
        ) {
            let (row, next_pos) = encode_planner_group(events, pos, next_idx, &resolved_ids);
            rows.push(row);
            next_idx += 1;
            pos = next_pos;
        } else {
            if let Some(single) = encode_single_agy_event(ev, next_idx, &call_by_id) {
                rows.push(single);
                next_idx += 1;
            }
            pos += 1;
        }
    }
    rows
}
