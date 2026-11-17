//! Codex JSONL / `.jsonl.zst` streaming rollout and trajectory codecs.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::Path;

use cas_core::{
    CanonicalEvent, CanonicalToolPayload, EventOriginAnchor, HostExtensionEnvelope, HostKind,
    INTERRUPTED_TOOL_OUTPUT, MediaAttachment, PACKAGE_VERSION, RolloutLineNo, SearchWebDocument,
    TaskBoundaryKind, TokenUsageAccounting, ToolCallId, TrajectoryLedger, TurnId, WorkspacePath,
    WorkspaceRootUri, is_nonterminal_work_or_abort_event, is_terminal_completion_event,
    normalize_tool_file_path, safe_json_dict, sort_json_value,
};
use serde_json::{Map, Value, json};

use crate::error::CodecError;
use crate::schema_tags::CodexRawRolloutLine;
use crate::tool_codecs::{
    apply_tool_result_meta, decode_tool_payload, encode_tool_for_codex, extract_tool_result_meta,
    find_tool_spec_by_name, rewrite_patch_headers,
};

const ZSTD_MAGIC: [u8; 4] = [0x28, 0xB5, 0x2F, 0xFD];

fn extract_exit_code(out_text: &str) -> Option<i64> {
    const NEEDLE: &str = "Process exited with code ";
    let idx = out_text.find(NEEDLE)?;
    let rest = &out_text[idx + NEEDLE.len()..];
    let end = rest
        .char_indices()
        .take_while(|(i, c)| (*i == 0 && *c == '-') || c.is_ascii_digit())
        .last()
        .map(|(i, c)| i + c.len_utf8())?;
    rest[..end].parse::<i64>().ok()
}

fn is_ignored_record_type(rtype: &str) -> bool {
    matches!(
        rtype,
        "token_usage_record" | "inter_agent_communication_metadata"
    )
}

fn is_ignored_event_msg_subtype(sub: &str) -> bool {
    sub.ends_with("_delta")
        || matches!(
            sub,
            "token_count"
                | "item_started"
                | "item_completed"
                | "exec_command_begin"
                | "exec_command_end"
                | "exec_command_output_delta"
                | "mcp_tool_call_begin"
                | "mcp_tool_call_end"
                | "patch_apply_begin"
                | "patch_apply_end"
                | "web_search_begin"
                | "web_search_end"
                | "turn_diff"
                | "background_event"
        )
}

struct RolloutTurnTracker {
    turn_num: u32,
    current_turn_id: TurnId,
    turn_has_response: bool,
    cp_counter: i64,
    raw_turn_to_canonical: HashMap<String, TurnId>,
    user_sources: Vec<(usize, String, &'static str)>,
    asst_sources: Vec<(usize, String, &'static str)>,
}

impl RolloutTurnTracker {
    fn new() -> Result<Self, CodecError> {
        Ok(Self {
            turn_num: 1,
            current_turn_id: TurnId::parse("turn-1")?,
            turn_has_response: false,
            cp_counter: 0,
            raw_turn_to_canonical: HashMap::new(),
            user_sources: Vec::new(),
            asst_sources: Vec::new(),
        })
    }

    fn has_user_counterpart(&self, text: &str, source: &str) -> bool {
        let trimmed = text.trim();
        self.user_sources
            .iter()
            .any(|(_, txt, src)| txt == trimmed && *src == source)
    }

    fn switch_turn(&mut self, new_tid: TurnId) {
        if new_tid != self.current_turn_id {
            self.user_sources.clear();
            self.asst_sources.clear();
            self.current_turn_id = new_tid;
        }
    }

    fn advance_sequential(&mut self) -> Result<(), CodecError> {
        self.turn_num += 1;
        let tid = TurnId::parse(&format!("turn-{}", self.turn_num))?;
        self.switch_turn(tid);
        self.turn_has_response = false;
        Ok(())
    }

    fn canonical_for_raw(&mut self, raw_tid: &str) -> Result<TurnId, CodecError> {
        let s = raw_tid.trim();
        if s.is_empty() {
            return Ok(self.current_turn_id.clone());
        }
        if !self.raw_turn_to_canonical.contains_key(s) {
            if self.raw_turn_to_canonical.is_empty() && !self.turn_has_response {
                self.turn_num = 1;
                self.raw_turn_to_canonical
                    .insert(s.to_owned(), TurnId::parse("turn-1")?);
            } else {
                self.turn_num = self.turn_num.max(self.raw_turn_to_canonical.len() as u32) + 1;
                self.raw_turn_to_canonical.insert(
                    s.to_owned(),
                    TurnId::parse(&format!("turn-{}", self.turn_num))?,
                );
            }
        }
        let mapped = self
            .raw_turn_to_canonical
            .get(s)
            .cloned()
            .unwrap_or_else(|| self.current_turn_id.clone());
        self.switch_turn(mapped);
        Ok(self.current_turn_id.clone())
    }
}

/// Decompress zstd-compressed Codex rollout bytes into UTF-8 text.
///
/// # Errors
/// Returns [`CodecError::ZstdStream`] on truncated or invalid zstd streams.
pub fn decompress_zstd_rollout_bytes(raw_bytes: &[u8]) -> Result<String, CodecError> {
    let mut decoder = zstd::stream::read::Decoder::new(raw_bytes).map_err(|e| {
        CodecError::ZstdStream(format!("Invalid or truncated zstd rollout stream: {e}"))
    })?;
    let mut out = Vec::new();
    decoder.read_to_end(&mut out).map_err(|e| {
        CodecError::ZstdStream(format!("Invalid or truncated zstd rollout stream: {e}"))
    })?;
    Ok(String::from_utf8_lossy(&out).into_owned())
}

/// Compress UTF-8 Codex rollout text into zstd frame bytes.
///
/// # Errors
/// Returns [`CodecError::ZstdStream`] if compression fails.
pub fn compress_zstd_rollout_bytes(text: &str) -> Result<Vec<u8>, CodecError> {
    zstd::stream::encode_all(text.as_bytes(), 3)
        .map_err(|e| CodecError::ZstdStream(format!("Failed to compress zstd rollout: {e}")))
}

fn read_binary_lf_lines<R: BufRead>(mut reader: R) -> Result<Vec<CodexRawRolloutLine>, CodecError> {
    let mut lines = Vec::new();
    let mut buf = Vec::with_capacity(4096);
    let mut line_idx = 0u64;
    loop {
        buf.clear();
        let n = reader
            .read_until(b'\n', &mut buf)
            .map_err(|e| CodecError::ZstdStream(format!("Rollout stream read error: {e}")))?;
        if n == 0 {
            break;
        }
        line_idx += 1;
        while buf.last() == Some(&b'\n') {
            buf.pop();
        }
        let s = String::from_utf8_lossy(&buf);
        if !s.trim().is_empty() {
            lines.push(CodexRawRolloutLine {
                line_no: RolloutLineNo(line_idx),
                raw_json: s.into_owned(),
            });
        }
    }
    Ok(lines)
}

/// Read `.jsonl` or `.jsonl.zst` Codex rollout lines splitting strictly on LF with bounded buffer reuse.
///
/// # Errors
/// Returns [`CodecError::Io`] or [`CodecError::ZstdStream`] on I/O or decompression failure.
pub fn read_codex_rollout_lines(path: &Path) -> Result<Vec<CodexRawRolloutLine>, CodecError> {
    let mut magic = [0u8; 4];
    let has_zst_magic = {
        let mut fh = File::open(path)?;
        let n = fh.read(&mut magic)?;
        n == 4 && magic == ZSTD_MAGIC
    };
    let is_zst = path
        .file_name()
        .and_then(|n| n.to_str())
        .is_some_and(|n| n.ends_with(".zst"))
        || has_zst_magic;

    let fh = File::open(path)?;
    if !is_zst {
        return read_binary_lf_lines(BufReader::new(fh));
    }
    let decoder = zstd::stream::read::Decoder::new(BufReader::new(fh)).map_err(|e| {
        CodecError::ZstdStream(format!("Invalid or truncated zstd rollout stream: {e}"))
    })?;
    read_binary_lf_lines(BufReader::new(decoder))
}

fn val_str(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => b.to_string(),
        _ => String::new(),
    }
}

fn val_opt_str(v: Option<&Value>) -> Option<String> {
    let s = val_str(v);
    if s.is_empty() { None } else { Some(s) }
}

fn sorted_json_string(v: &Value) -> String {
    sort_json_value(v).to_string()
}

fn extract_content_blocks(content: Option<&Value>) -> (String, Vec<MediaAttachment>) {
    let Some(val) = content else {
        return (String::new(), Vec::new());
    };
    if let Some(s) = val.as_str() {
        return (s.to_owned(), Vec::new());
    }
    let mut texts = Vec::new();
    let mut media = Vec::new();
    if let Some(arr) = val.as_array() {
        for blk in arr {
            let Some(obj) = blk.as_object() else {
                continue;
            };
            if let Some(t) = obj.get("text").filter(|v| !v.is_null()) {
                texts.push(val_str(Some(t)));
            } else if matches!(
                obj.get("type").and_then(Value::as_str),
                Some("input_image" | "image")
            ) {
                media.push(MediaAttachment {
                    mime_type: val_opt_str(obj.get("mime_type"))
                        .unwrap_or_else(|| "image/png".to_owned()),
                    uri: val_opt_str(obj.get("image_url").or_else(|| obj.get("uri")))
                        .unwrap_or_default(),
                    byte_size: None,
                    caption: None,
                });
            }
        }
    }
    (texts.join("\n"), media)
}

fn stringify_tool_output(raw_out: Option<&Value>) -> String {
    let Some(v) = raw_out else {
        return String::new();
    };
    match v {
        Value::Array(arr) => arr
            .iter()
            .map(|it| {
                if let Some(obj) = it.as_object()
                    && let Some(t) = obj.get("text")
                {
                    return val_str(Some(t));
                }
                it.to_string()
            })
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Object(_) => v.to_string(),
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn normalize_patch_call_args(raw_args: &str) -> String {
    let (first_hdr, norm_patch) = rewrite_patch_headers(raw_args);
    let (verb, path_raw) = first_hdr.unwrap_or(("Update", "patch_target".to_owned()));
    let inferred_op = match verb {
        "Add" => "CREATE",
        "Delete" => "DELETE",
        _ => "MODIFY",
    };
    let inferred_path = if path_raw == "patch_target" {
        "patch_target".to_owned()
    } else {
        normalize_tool_file_path(&path_raw).into_owned()
    };
    json!({
        "edit_op": inferred_op,
        "path": inferred_path,
        "unified_diff": norm_patch
    })
    .to_string()
}

struct RolloutDecodeState {
    events: Vec<CanonicalEvent>,
    call_index_by_id: HashMap<ToolCallId, usize>,
    call_has_explicit_exit: HashSet<ToolCallId>,
    ws_result_index: HashMap<ToolCallId, usize>,
}

fn decode_web_search_call(
    pl: &Map<String, Value>,
    anchor: &EventOriginAnchor,
    line_no: RolloutLineNo,
    st: &mut RolloutDecodeState,
) -> Result<(), CodecError> {
    let raw_cid = val_opt_str(pl.get("call_id").or_else(|| pl.get("id")))
        .unwrap_or_else(|| format!("ws-line-{}", line_no.0));
    let cid = ToolCallId::parse(&raw_cid)?;
    let empty_action = Map::new();
    let action = pl
        .get("action")
        .and_then(Value::as_object)
        .unwrap_or(&empty_action);
    let first_q = action
        .get("queries")
        .and_then(Value::as_array)
        .and_then(|arr| arr.first())
        .map(|v| val_str(Some(v)));
    let query = val_opt_str(action.get("query"))
        .or(first_q)
        .or_else(|| val_opt_str(pl.get("query")))
        .unwrap_or_default();
    let domain = val_opt_str(action.get("domain").or_else(|| pl.get("domain")));
    let raw_results = action
        .get("results")
        .or_else(|| pl.get("results"))
        .and_then(Value::as_array);
    let docs: Vec<SearchWebDocument> = raw_results
        .map(|arr| {
            arr.iter()
                .filter_map(Value::as_object)
                .map(|d| SearchWebDocument {
                    title: val_str(d.get("title")),
                    url: val_str(d.get("url")),
                    snippet: val_str(d.get("snippet")),
                })
                .collect()
        })
        .unwrap_or_default();
    let payload = CanonicalToolPayload::SearchWeb {
        query: query.clone(),
        domain_filter: domain,
        documents: docs,
        summary: None,
    };
    let raw_ext = HostExtensionEnvelope {
        host: HostKind::Codex,
        extension_type: "codex_web_search_call".to_owned(),
        raw_payload: sorted_json_string(&Value::Object(pl.clone())).into_bytes(),
        metadata: Vec::new(),
    };
    let mut call_anchor = anchor.clone();
    call_anchor.host_extensions = vec![raw_ext];
    st.call_index_by_id.insert(cid.clone(), st.events.len());
    st.events.push(CanonicalEvent::ToolCall {
        anchor: call_anchor,
        call_id: cid.clone(),
        tool_name: "web_search".to_owned(),
        tool_summary: "web_search".to_owned(),
        payload: payload.clone(),
        thinking_signature: None,
    });
    let status_str = val_opt_str(pl.get("status"))
        .unwrap_or_else(|| "completed".to_owned())
        .to_ascii_lowercase();
    if status_str != "in_progress" {
        let out_text = val_opt_str(pl.get("output")).unwrap_or_else(|| {
            if query.is_empty() {
                "Web search completed".to_owned()
            } else {
                format!("Web search: {query}")
            }
        });
        st.ws_result_index.insert(cid.clone(), st.events.len());
        st.events.push(CanonicalEvent::ToolResult {
            anchor: anchor.clone(),
            call_id: cid,
            tool_name: "web_search".to_owned(),
            output_text: out_text,
            is_error: matches!(status_str.as_str(), "failed" | "error"),
            payload,
            full_output_uri: None,
        });
    }
    Ok(())
}

fn decode_event_msg_record(
    pl: &Map<String, Value>,
    ts_iso: &str,
    line_no: RolloutLineNo,
    tracker: &mut RolloutTurnTracker,
    events: &mut Vec<CanonicalEvent>,
) -> (bool, bool) {
    let ev_sub = val_str(pl.get("type"));
    if ev_sub.is_empty() || is_ignored_event_msg_subtype(&ev_sub) {
        return (false, false);
    }
    let mut anchor =
        EventOriginAnchor::new(HostKind::Codex, tracker.current_turn_id.clone(), ts_iso);
    anchor.codex_line_no = Some(line_no);

    match ev_sub.as_str() {
        "task_started" => {
            let b_kind = if val_str(pl.get("boundary_kind")) == "notify_user" {
                TaskBoundaryKind::NotifyUser
            } else {
                TaskBoundaryKind::TaskBoundary
            };
            events.push(CanonicalEvent::TaskBoundary {
                anchor,
                boundary_kind: b_kind,
                task_name: val_str(pl.get("task_name")),
                task_status: val_opt_str(pl.get("task_status"))
                    .unwrap_or_else(|| "task_started".to_owned()),
                task_summary: val_str(pl.get("task_summary")),
                mode: None,
                blocked_on_user: false,
                review_uris: Vec::new(),
            });
            (false, true)
        }
        "task_complete" => {
            let b_kind = if val_str(pl.get("boundary_kind")) == "notify_user" {
                TaskBoundaryKind::NotifyUser
            } else {
                TaskBoundaryKind::TaskBoundary
            };
            let t_sum = if let Some(v) = pl.get("task_summary").filter(|v| !v.is_null()) {
                val_str(Some(v))
            } else {
                val_opt_str(pl.get("last_agent_message"))
                    .unwrap_or_else(|| "Turn complete".to_owned())
            };
            events.push(CanonicalEvent::TaskBoundary {
                anchor,
                boundary_kind: b_kind,
                task_name: val_str(pl.get("task_name")),
                task_status: val_opt_str(pl.get("task_status"))
                    .unwrap_or_else(|| "task_complete".to_owned()),
                task_summary: t_sum,
                mode: None,
                blocked_on_user: false,
                review_uris: Vec::new(),
            });
            (true, false)
        }
        "user_message" => {
            let q_text = val_str(pl.get("message").or_else(|| pl.get("text")));
            let trimmed = q_text.trim().to_owned();
            for entry in &mut tracker.user_sources {
                if entry.1 == trimmed && entry.2 == "response_item" {
                    entry.2 = "merged";
                    return (false, false);
                }
            }
            tracker
                .user_sources
                .push((events.len(), trimmed, "event_msg"));
            events.push(CanonicalEvent::UserInput {
                anchor,
                query_text: q_text,
                scope_items: Vec::new(),
                media: Vec::new(),
            });
            (false, false)
        }
        "agent_message" => {
            let a_text = val_str(pl.get("message").or_else(|| pl.get("text")));
            let trimmed = a_text.trim().to_owned();
            for entry in &mut tracker.asst_sources {
                if entry.1 == trimmed && entry.2 == "response_item" {
                    entry.2 = "merged";
                    return (true, false);
                }
            }
            tracker
                .asst_sources
                .push((events.len(), trimmed, "event_msg"));
            events.push(CanonicalEvent::AssistantText {
                anchor,
                text: a_text,
                message_id: None,
                stop_reason: None,
                generator_model: None,
                token_usage: TokenUsageAccounting::default(),
            });
            (true, false)
        }
        "agent_reasoning" => {
            events.push(CanonicalEvent::Thinking {
                anchor,
                summary_text: val_str(pl.get("text").or_else(|| pl.get("summary"))),
                raw_thinking: None,
                thinking_signature: None,
                token_usage: TokenUsageAccounting::default(),
            });
            (true, false)
        }
        "system_message" => {
            let ev_type =
                val_opt_str(pl.get("event_type")).unwrap_or_else(|| "system_message".to_owned());
            if let Some(pt) = pl
                .get("internal_chat_message_metadata_passthrough")
                .and_then(Value::as_object)
                && let Some(ae) = pt.get("agy_ext").and_then(Value::as_object)
                && let (Some(ae_type), Some(payload_hex)) = (
                    ae.get("type")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty()),
                    ae.get("payload_hex").and_then(Value::as_str),
                )
                && let Ok(raw_payload) = hex::decode(payload_hex)
            {
                let metadata = ae
                    .get("metadata")
                    .and_then(Value::as_object)
                    .map(|md| {
                        md.iter()
                            .map(|(k, v)| (k.clone(), val_str(Some(v))))
                            .collect()
                    })
                    .unwrap_or_default();
                anchor.host_extensions.push(HostExtensionEnvelope {
                    host: HostKind::Antigravity,
                    extension_type: ae_type.to_owned(),
                    raw_payload,
                    metadata,
                });
            }
            let is_resp = ev_type == "error_message" || ev_type.starts_with("opaque_agy_step:");
            events.push(CanonicalEvent::SystemMessage {
                anchor,
                event_type: ev_type,
                message_text: val_str(pl.get("message")),
            });
            (is_resp, false)
        }
        _ => {
            anchor.host_extensions.push(HostExtensionEnvelope {
                host: HostKind::Codex,
                extension_type: "codex_raw_record".to_owned(),
                raw_payload: sorted_json_string(&Value::Object(pl.clone())).into_bytes(),
                metadata: vec![("record_type".to_owned(), "event_msg".to_owned())],
            });
            let msg = val_opt_str(pl.get("message"))
                .or_else(|| val_opt_str(pl.get("reason")))
                .unwrap_or_else(|| ev_sub.clone());
            events.push(CanonicalEvent::SystemMessage {
                anchor,
                event_type: format!("codex_event_msg:{ev_sub}"),
                message_text: msg,
            });
            (matches!(ev_sub.as_str(), "turn_aborted" | "error"), false)
        }
    }
}

fn decode_tool_response_item(
    ptype: &str,
    pl: &Map<String, Value>,
    pt_dict: &Map<String, Value>,
    base_anchor: &EventOriginAnchor,
    line_no: RolloutLineNo,
    st: &mut RolloutDecodeState,
) -> Result<(), CodecError> {
    if ptype == "web_search_call" {
        return decode_web_search_call(pl, base_anchor, line_no, st);
    }
    if matches!(
        ptype,
        "function_call" | "custom_tool_call" | "tool_search_call"
    ) {
        let raw_cid = val_opt_str(pl.get("call_id").or_else(|| pl.get("id")))
            .unwrap_or_else(|| format!("call-line-{}", line_no.0));
        let cid = ToolCallId::parse(&raw_cid)?;
        let tname = val_opt_str(pl.get("name")).unwrap_or_else(|| {
            if ptype == "tool_search_call" {
                "tool_search".to_owned()
            } else {
                "unknown_tool".to_owned()
            }
        });
        let raw_args_val = match pl.get("arguments") {
            Some(v) if !v.is_null() && v.as_str() != Some("") => Some(v),
            _ => pl.get("input"),
        };
        let args_json = match raw_args_val {
            Some(v @ (Value::Object(_) | Value::Array(_))) => sorted_json_string(v),
            Some(Value::String(s)) => {
                if ptype == "custom_tool_call"
                    && tname == "apply_patch"
                    && !s.trim_start().starts_with('{')
                {
                    normalize_patch_call_args(s)
                } else {
                    s.clone()
                }
            }
            _ => "{}".to_owned(),
        };
        if safe_json_dict(&args_json).contains_key("exit_code") {
            st.call_has_explicit_exit.insert(cid.clone());
        }
        let mut payload = decode_tool_payload(&tname, &args_json, None, None, true);
        if ptype == "custom_tool_call"
            && tname == "exec"
            && serde_json::from_str::<Value>(&args_json).is_err()
        {
            payload = CanonicalToolPayload::Opaque {
                tool_name: tname.clone(),
                raw_arguments_json: args_json,
                type_url: None,
                raw_any_bytes: None,
            };
        }
        let tsig = pt_dict
            .get("thinking_signature_hex")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .and_then(|h| hex::decode(h).ok());
        let mut anchor = base_anchor.clone();
        if let Some(ns) = val_opt_str(pl.get("namespace")) {
            anchor.host_extensions.push(HostExtensionEnvelope {
                host: HostKind::Codex,
                extension_type: "tool_namespace".to_owned(),
                raw_payload: ns.into_bytes(),
                metadata: vec![("item_type".to_owned(), ptype.to_owned())],
            });
        }
        st.call_index_by_id.insert(cid.clone(), st.events.len());
        st.events.push(CanonicalEvent::ToolCall {
            anchor,
            call_id: cid,
            tool_name: tname.clone(),
            tool_summary: tname,
            payload,
            thinking_signature: tsig,
        });
        return Ok(());
    }

    let raw_cid = val_opt_str(pl.get("call_id").or_else(|| pl.get("id")))
        .unwrap_or_else(|| format!("call-out-{}", line_no.0));
    let cid = ToolCallId::parse(&raw_cid)?;
    let out_text = stringify_tool_output(if pl.contains_key("output") {
        pl.get("output")
    } else {
        pl.get("tools")
    });
    let mut prior_tname = "unknown_tool".to_owned();
    let mut prior_payload = CanonicalToolPayload::Opaque {
        tool_name: prior_tname.clone(),
        raw_arguments_json: "{}".to_owned(),
        type_url: None,
        raw_any_bytes: None,
    };
    let rmeta = pt_dict.get("tool_result_meta").and_then(Value::as_object);
    if let Some(&call_pos) = st.call_index_by_id.get(&cid)
        && let Some(CanonicalEvent::ToolCall {
            tool_name: ev_tname,
            payload: ev_payload,
            ..
        }) = st.events.get_mut(call_pos)
    {
        prior_tname = ev_tname.clone();
        if let Some(rm) = rmeta
            && !rm.is_empty()
        {
            *ev_payload = apply_tool_result_meta(&prior_tname, ev_payload, rm);
        } else if let CanonicalToolPayload::RunCommand { exit_code: ec, .. } = ev_payload
            && ec.is_none()
            && !st.call_has_explicit_exit.contains(&cid)
        {
            let parsed = extract_exit_code(&out_text).unwrap_or(0);
            *ec = Some(parsed);
        }
        prior_payload = ev_payload.clone();
    }
    let is_err = pl.get("is_error").and_then(Value::as_bool).unwrap_or(false)
        || matches!(
            val_str(pl.get("status")).to_ascii_lowercase().as_str(),
            "failed" | "error"
        );
    let mut anchor = base_anchor.clone();
    if pt_dict
        .get("synthesized_dangling_output")
        .and_then(Value::as_bool)
        == Some(true)
    {
        anchor.host_extensions.push(HostExtensionEnvelope {
            host: HostKind::Codex,
            extension_type: "synthesized_dangling_output".to_owned(),
            raw_payload: b"1".to_vec(),
            metadata: Vec::new(),
        });
    }
    if val_opt_str(pl.get("call_id")).is_none() {
        anchor.host_extensions.push(HostExtensionEnvelope {
            host: HostKind::Codex,
            extension_type: "codex_unpaired_tool_output".to_owned(),
            raw_payload: sorted_json_string(&Value::Object(pl.clone())).into_bytes(),
            metadata: Vec::new(),
        });
    }
    let res_ev = CanonicalEvent::ToolResult {
        anchor,
        call_id: cid.clone(),
        tool_name: prior_tname,
        output_text: out_text,
        is_error: is_err,
        payload: prior_payload,
        full_output_uri: None,
    };
    if let Some(&existing_pos) = st.ws_result_index.get(&cid) {
        st.events[existing_pos] = res_ev;
    } else {
        st.events.push(res_ev);
    }
    Ok(())
}

fn decode_response_item_record(
    pl: &Map<String, Value>,
    ts_iso: &str,
    line_no: RolloutLineNo,
    tracker: &mut RolloutTurnTracker,
    st: &mut RolloutDecodeState,
) -> Result<bool, CodecError> {
    let ptype = val_str(pl.get("type"));
    let empty_map = Map::new();
    let pt_dict = pl
        .get("internal_chat_message_metadata_passthrough")
        .and_then(Value::as_object)
        .unwrap_or(&empty_map);
    let mut base_anchor =
        EventOriginAnchor::new(HostKind::Codex, tracker.current_turn_id.clone(), ts_iso);
    base_anchor.codex_line_no = Some(line_no);

    if ptype == "message" {
        let role = val_opt_str(pl.get("role")).unwrap_or_else(|| "user".to_owned());
        let (full_text, media) = extract_content_blocks(pl.get("content"));
        let msg_id = val_opt_str(pl.get("id"));
        if role == "user" {
            let scope_items = pt_dict
                .get("scope_items")
                .and_then(Value::as_array)
                .map(|arr| arr.iter().map(|v| val_str(Some(v))).collect())
                .unwrap_or_default();
            let mut prov = Map::new();
            for key in ["content_item_kinds", "content_item_metadata"] {
                if let Some(val) = pt_dict.get(key) {
                    prov.insert(key.to_owned(), val.clone());
                }
            }
            if !prov.is_empty() {
                base_anchor.host_extensions.push(HostExtensionEnvelope {
                    host: HostKind::Codex,
                    extension_type: "codex_message_provenance".to_owned(),
                    raw_payload: sorted_json_string(&Value::Object(prov)).into_bytes(),
                    metadata: Vec::new(),
                });
            }
            base_anchor.client_message_id = msg_id;
            let u_ev = CanonicalEvent::UserInput {
                anchor: base_anchor,
                query_text: full_text.clone(),
                scope_items,
                media,
            };
            let trimmed = full_text.trim().to_owned();
            for entry in &mut tracker.user_sources {
                if entry.2 == "event_msg" && entry.1 == trimmed {
                    st.events[entry.0] = u_ev;
                    entry.2 = "merged";
                    return Ok(false);
                }
            }
            tracker
                .user_sources
                .push((st.events.len(), trimmed, "response_item"));
            st.events.push(u_ev);
            return Ok(false);
        }
        if role == "assistant" {
            let a_ev = CanonicalEvent::AssistantText {
                anchor: base_anchor,
                text: full_text.clone(),
                message_id: msg_id,
                stop_reason: None,
                generator_model: None,
                token_usage: TokenUsageAccounting::default(),
            };
            let trimmed = full_text.trim().to_owned();
            for entry in &mut tracker.asst_sources {
                if entry.2 == "event_msg" && entry.1 == trimmed {
                    st.events[entry.0] = a_ev;
                    entry.2 = "merged";
                    return Ok(true);
                }
            }
            tracker
                .asst_sources
                .push((st.events.len(), trimmed, "response_item"));
            st.events.push(a_ev);
            return Ok(true);
        }
        base_anchor.host_extensions.push(HostExtensionEnvelope {
            host: HostKind::Codex,
            extension_type: format!("message_{role}"),
            raw_payload: sorted_json_string(&Value::Object(pl.clone())).into_bytes(),
            metadata: Vec::new(),
        });
        st.events.push(CanonicalEvent::SystemMessage {
            anchor: base_anchor,
            event_type: format!("codex_{role}_message"),
            message_text: full_text,
        });
        return Ok(false);
    }

    if ptype == "agent_message" {
        let (full_text, _) = extract_content_blocks(pl.get("content"));
        let text = if full_text.is_empty() {
            val_str(pl.get("text").or_else(|| pl.get("message")))
        } else {
            full_text
        };
        base_anchor.host_extensions.push(HostExtensionEnvelope {
            host: HostKind::Codex,
            extension_type: "codex_agent_message".to_owned(),
            raw_payload: sorted_json_string(&Value::Object(pl.clone())).into_bytes(),
            metadata: Vec::new(),
        });
        st.events.push(CanonicalEvent::AssistantText {
            anchor: base_anchor,
            text,
            message_id: val_opt_str(pl.get("id")),
            stop_reason: None,
            generator_model: None,
            token_usage: TokenUsageAccounting::default(),
        });
        return Ok(true);
    }

    if matches!(ptype.as_str(), "reasoning" | "magpie_reasoning") {
        let s_parts: Vec<String> = pl
            .get("summary")
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter_map(|it| match it {
                        Value::Object(o) => o.get("text").map(|t| val_str(Some(t))),
                        Value::String(s) => Some(s.clone()),
                        _ => None,
                    })
                    .collect()
            })
            .unwrap_or_default();
        if let Some(enc) = val_opt_str(pl.get("encrypted_content")) {
            base_anchor.host_extensions.push(HostExtensionEnvelope {
                host: HostKind::Codex,
                extension_type: "encrypted_content".to_owned(),
                raw_payload: enc.into_bytes(),
                metadata: vec![("reasoning_type".to_owned(), ptype)],
            });
        } else {
            base_anchor.host_extensions.push(HostExtensionEnvelope {
                host: HostKind::Codex,
                extension_type: "reasoning_meta".to_owned(),
                raw_payload: Vec::new(),
                metadata: vec![("reasoning_type".to_owned(), ptype)],
            });
        }
        let raw_thinking = pl.get("content").and_then(Value::as_str).map(str::to_owned);
        st.events.push(CanonicalEvent::Thinking {
            anchor: base_anchor,
            summary_text: s_parts.join("\n"),
            raw_thinking,
            thinking_signature: None,
            token_usage: TokenUsageAccounting::default(),
        });
        return Ok(true);
    }

    if matches!(
        ptype.as_str(),
        "web_search_call"
            | "function_call"
            | "custom_tool_call"
            | "tool_search_call"
            | "function_call_output"
            | "custom_tool_call_output"
            | "tool_search_output"
            | "web_search_call_output"
    ) {
        decode_tool_response_item(&ptype, pl, pt_dict, &base_anchor, line_no, st)?;
        return Ok(true);
    }

    base_anchor.host_extensions.push(HostExtensionEnvelope {
        host: HostKind::Codex,
        extension_type: "codex_raw_record".to_owned(),
        raw_payload: sorted_json_string(&Value::Object(pl.clone())).into_bytes(),
        metadata: vec![("record_type".to_owned(), "response_item".to_owned())],
    });
    st.events.push(CanonicalEvent::SystemMessage {
        anchor: base_anchor,
        event_type: format!(
            "codex_response_item:{}",
            if ptype.is_empty() { "unknown" } else { &ptype }
        ),
        message_text: if ptype.is_empty() {
            "response_item".to_owned()
        } else {
            ptype
        },
    });
    Ok(false)
}

/// Parse Codex `rollout-*.jsonl` lines into an indexed [`TrajectoryLedger`].
///
/// # Errors
/// Returns [`CodecError::Json`] or [`CodecError::Core`] if JSON lines or IDs are invalid.
pub fn decode_codex_rollout(
    workspace_path: WorkspacePath,
    workspace_uri: WorkspaceRootUri,
    raw_lines: &[CodexRawRolloutLine],
    explicit_title_override: Option<&str>,
) -> Result<TrajectoryLedger, CodecError> {
    let mut st = RolloutDecodeState {
        events: Vec::new(),
        call_index_by_id: HashMap::new(),
        call_has_explicit_exit: HashSet::new(),
        ws_result_index: HashMap::new(),
    };
    let mut tracker = RolloutTurnTracker::new()?;

    for row in raw_lines {
        let raw_str = row.raw_json.trim();
        if raw_str.is_empty() {
            continue;
        }
        let obj: Value = serde_json::from_str(raw_str)?;
        let Some(obj_map) = obj.as_object() else {
            continue;
        };
        let rtype = val_str(obj_map.get("type"));
        let Some(pl) = obj_map.get("payload").and_then(Value::as_object) else {
            continue;
        };
        if is_ignored_record_type(&rtype) {
            continue;
        }
        let ts_iso = val_opt_str(obj_map.get("timestamp"))
            .unwrap_or_else(|| "2026-01-01T00:00:00Z".to_owned());

        match rtype.as_str() {
            "session_meta" | "world_state" => {
                let mut anchor = EventOriginAnchor::new(
                    HostKind::Codex,
                    tracker.current_turn_id.clone(),
                    &ts_iso,
                );
                anchor.codex_line_no = Some(row.line_no);
                anchor.host_extensions.push(HostExtensionEnvelope {
                    host: HostKind::Codex,
                    extension_type: rtype.clone(),
                    raw_payload: sorted_json_string(&Value::Object(pl.clone())).into_bytes(),
                    metadata: Vec::new(),
                });
                let msg = if rtype == "session_meta" {
                    format!(
                        "codex_session:{}",
                        val_str(pl.get("id").or_else(|| pl.get("session_id")))
                    )
                } else {
                    "codex_world_state".to_owned()
                };
                st.events.push(CanonicalEvent::SystemMessage {
                    anchor,
                    event_type: rtype,
                    message_text: msg,
                });
            }
            "turn_context" => {
                if let Some(rtid) = val_opt_str(pl.get("turn_id")) {
                    tracker.canonical_for_raw(&rtid)?;
                }
                tracker.cp_counter += 1;
                let mut anchor = EventOriginAnchor::new(
                    HostKind::Codex,
                    tracker.current_turn_id.clone(),
                    &ts_iso,
                );
                anchor.codex_line_no = Some(row.line_no);
                anchor.host_extensions.push(HostExtensionEnvelope {
                    host: HostKind::Codex,
                    extension_type: "turn_context".to_owned(),
                    raw_payload: sorted_json_string(&Value::Object(pl.clone())).into_bytes(),
                    metadata: Vec::new(),
                });
                let sum_str = if let Some(v) = pl.get("summary").filter(|v| !v.is_null()) {
                    val_str(Some(v))
                } else {
                    val_opt_str(pl.get("model")).unwrap_or_else(|| "turn_context".to_owned())
                };
                let conv_title =
                    val_opt_str(pl.get("conversation_title").or_else(|| pl.get("title")));
                let cp_idx = pl
                    .get("checkpoint_index")
                    .and_then(Value::as_i64)
                    .unwrap_or(tracker.cp_counter);
                st.events.push(CanonicalEvent::Checkpoint {
                    anchor,
                    checkpoint_index: cp_idx,
                    user_intent: val_str(pl.get("user_intent")),
                    session_summary: sum_str,
                    code_change_summary: val_str(pl.get("code_change_summary")),
                    conversation_title: conv_title,
                });
            }
            "event_msg" => {
                let ev_sub = val_str(pl.get("type"));
                let ev_turn = val_opt_str(pl.get("turn_id"));
                if matches!(ev_sub.as_str(), "task_started" | "user_message") {
                    if let Some(et) = ev_turn.as_deref() {
                        tracker.canonical_for_raw(et)?;
                    } else if tracker.turn_has_response
                        && !(ev_sub == "user_message"
                            && tracker.has_user_counterpart(
                                &val_str(pl.get("message").or_else(|| pl.get("text"))),
                                "response_item",
                            ))
                    {
                        tracker.advance_sequential()?;
                    }
                    if ev_sub == "task_started" {
                        tracker.turn_has_response = false;
                    }
                } else if let Some(et) = ev_turn.as_deref()
                    && !is_ignored_event_msg_subtype(&ev_sub)
                {
                    tracker.canonical_for_raw(et)?;
                }
                let (resp_flag, reset_flag) =
                    decode_event_msg_record(pl, &ts_iso, row.line_no, &mut tracker, &mut st.events);
                if reset_flag {
                    tracker.turn_has_response = false;
                } else if resp_flag {
                    tracker.turn_has_response = true;
                }
            }
            "response_item" => {
                if let Some(pt) = pl
                    .get("internal_chat_message_metadata_passthrough")
                    .and_then(Value::as_object)
                    && let Some(tid) = val_opt_str(pt.get("turn_id"))
                {
                    tracker.canonical_for_raw(&tid)?;
                } else if val_str(pl.get("type")) == "message"
                    && val_opt_str(pl.get("role")).as_deref().unwrap_or("user") == "user"
                    && tracker.turn_has_response
                    && !tracker.has_user_counterpart(
                        &extract_content_blocks(pl.get("content")).0,
                        "event_msg",
                    )
                {
                    tracker.advance_sequential()?;
                }
                if decode_response_item_record(pl, &ts_iso, row.line_no, &mut tracker, &mut st)? {
                    tracker.turn_has_response = true;
                }
            }
            _ => {
                let mut anchor = EventOriginAnchor::new(
                    HostKind::Codex,
                    tracker.current_turn_id.clone(),
                    &ts_iso,
                );
                anchor.codex_line_no = Some(row.line_no);
                anchor.host_extensions.push(HostExtensionEnvelope {
                    host: HostKind::Codex,
                    extension_type: "codex_raw_record".to_owned(),
                    raw_payload: sorted_json_string(&Value::Object(pl.clone())).into_bytes(),
                    metadata: vec![(
                        "record_type".to_owned(),
                        if rtype.is_empty() {
                            "unknown".to_owned()
                        } else {
                            rtype.clone()
                        },
                    )],
                });
                st.events.push(CanonicalEvent::SystemMessage {
                    anchor,
                    event_type: format!("codex_record:{rtype}"),
                    message_text: val_opt_str(pl.get("message")).unwrap_or(rtype),
                });
            }
        }
    }

    Ok(TrajectoryLedger::from_events(
        workspace_path,
        workspace_uri,
        &st.events,
        explicit_title_override,
        false,
    ))
}

struct RolloutLineEmitter {
    lines: Vec<CodexRawRolloutLine>,
    next_line: u64,
}

impl RolloutLineEmitter {
    fn new(starting_line_no: RolloutLineNo) -> Self {
        Self {
            lines: Vec::new(),
            next_line: starting_line_no.0,
        }
    }

    fn append(&mut self, ts: &str, rtype: &str, mut payload: Map<String, Value>) {
        if rtype == "session_meta" {
            let mut with_ver = Map::new();
            with_ver.insert("cli_version".into(), json!(PACKAGE_VERSION));
            for (k, v) in payload {
                with_ver.insert(k, v);
            }
            payload = with_ver;
        }
        let mut record = Map::new();
        record.insert(
            "timestamp".into(),
            json!(if ts.is_empty() {
                "2026-01-01T00:00:00Z"
            } else {
                ts
            }),
        );
        record.insert("ordinal".into(), json!(self.next_line.saturating_sub(1)));
        record.insert("type".into(), json!(rtype));
        record.insert("payload".into(), Value::Object(payload));
        self.lines.push(CodexRawRolloutLine {
            line_no: RolloutLineNo(self.next_line),
            raw_json: Value::Object(record).to_string(),
        });
        self.next_line += 1;
    }
}

fn btree_to_map(b: BTreeMap<String, Value>) -> Map<String, Value> {
    b.into_iter().collect()
}

fn encode_codex_system_event(
    anchor: &EventOriginAnchor,
    event_type: &str,
    message_text: &str,
    ts: &str,
    emitter: &mut RolloutLineEmitter,
) {
    for ext in &anchor.host_extensions {
        if ext.extension_type == "codex_raw_record" && !ext.raw_payload.is_empty() {
            let mut raw_d =
                btree_to_map(safe_json_dict(&String::from_utf8_lossy(&ext.raw_payload)));
            if raw_d.contains_key("turn_id") {
                raw_d.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
            }
            if raw_d.contains_key("root_turn_id") {
                raw_d.insert("root_turn_id".into(), json!(anchor.turn_id.as_str()));
            }
            let rtype = ext
                .metadata
                .iter()
                .find(|(k, _)| k == "record_type")
                .map_or("event_msg", |(_, v)| v.as_str());
            emitter.append(ts, rtype, raw_d);
            return;
        }
    }
    if let Some(role) = event_type
        .strip_prefix("codex_")
        .and_then(|s| s.strip_suffix("_message"))
    {
        let ext_name = format!("message_{role}");
        for ext in &anchor.host_extensions {
            if ext.extension_type == ext_name && !ext.raw_payload.is_empty() {
                let mut raw_pl =
                    btree_to_map(safe_json_dict(&String::from_utf8_lossy(&ext.raw_payload)));
                if !raw_pl.is_empty() {
                    if let Some(Value::Object(pt)) =
                        raw_pl.get_mut("internal_chat_message_metadata_passthrough")
                    {
                        pt.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
                    }
                    emitter.append(ts, "response_item", raw_pl);
                    return;
                }
            }
        }
        let mut pl = Map::new();
        pl.insert("type".into(), json!("message"));
        pl.insert("role".into(), json!(role));
        pl.insert(
            "content".into(),
            json!([{"type": "input_text", "text": message_text}]),
        );
        pl.insert(
            "internal_chat_message_metadata_passthrough".into(),
            json!({"turn_id": anchor.turn_id.as_str()}),
        );
        emitter.append(ts, "response_item", pl);
        return;
    }

    let mut pl_out = Map::new();
    pl_out.insert("type".into(), json!("system_message"));
    pl_out.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
    pl_out.insert("event_type".into(), json!(event_type));
    pl_out.insert("message".into(), json!(message_text));
    for ext in &anchor.host_extensions {
        if matches!(
            ext.extension_type.as_str(),
            "agy_error_message_step" | "agy_opaque_step"
        ) {
            let md: Map<String, Value> = ext
                .metadata
                .iter()
                .map(|(k, v)| (k.clone(), json!(v)))
                .collect();
            pl_out.insert(
                "internal_chat_message_metadata_passthrough".into(),
                json!({
                    "turn_id": anchor.turn_id.as_str(),
                    "agy_ext": {
                        "type": ext.extension_type,
                        "payload_hex": hex::encode(&ext.raw_payload),
                        "metadata": Value::Object(md),
                    }
                }),
            );
            break;
        }
    }
    emitter.append(ts, "event_msg", pl_out);
}

fn encode_single_codex_event(
    ev: &CanonicalEvent,
    ts: &str,
    ws_native_calls: &mut HashSet<ToolCallId>,
    emitter: &mut RolloutLineEmitter,
) -> bool {
    let hint = emitter.next_line;
    match ev {
        CanonicalEvent::UserInput {
            anchor,
            query_text,
            scope_items,
            media,
        } => {
            let mut content_list = vec![json!({"type": "input_text", "text": query_text})];
            for m in media {
                content_list.push(json!({
                    "type": "input_image",
                    "mime_type": m.mime_type,
                    "image_url": m.uri,
                }));
            }
            let mut u_pt = Map::new();
            u_pt.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
            if !scope_items.is_empty() {
                u_pt.insert("scope_items".into(), json!(scope_items));
            }
            for ext in &anchor.host_extensions {
                if ext.extension_type == "codex_message_provenance" {
                    let prov = safe_json_dict(&String::from_utf8_lossy(&ext.raw_payload));
                    for (k, v) in prov {
                        u_pt.insert(k, v);
                    }
                }
            }
            let msg_id = anchor
                .client_message_id
                .clone()
                .unwrap_or_else(|| format!("msg_user_{hint}"));
            let mut resp_pl = Map::new();
            resp_pl.insert("type".into(), json!("message"));
            resp_pl.insert("id".into(), json!(msg_id));
            resp_pl.insert("role".into(), json!("user"));
            resp_pl.insert("content".into(), Value::Array(content_list));
            resp_pl.insert(
                "internal_chat_message_metadata_passthrough".into(),
                Value::Object(u_pt),
            );
            emitter.append(ts, "response_item", resp_pl);

            let mut ev_pl = Map::new();
            ev_pl.insert("type".into(), json!("user_message"));
            ev_pl.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
            ev_pl.insert("message".into(), json!(query_text));
            ev_pl.insert("images".into(), json!([]));
            ev_pl.insert("local_images".into(), json!([]));
            ev_pl.insert("text_elements".into(), json!([]));
            emitter.append(ts, "event_msg", ev_pl);
            false
        }
        CanonicalEvent::Thinking {
            anchor,
            summary_text,
            raw_thinking,
            ..
        } => {
            let mut r_type = "reasoning".to_owned();
            let mut enc_val: Option<String> = None;
            for ext in &anchor.host_extensions {
                for (mk, mv) in &ext.metadata {
                    if mk == "reasoning_type"
                        && matches!(mv.as_str(), "reasoning" | "magpie_reasoning")
                    {
                        r_type = mv.clone();
                    }
                }
                if ext.extension_type == "encrypted_content" && !ext.raw_payload.is_empty() {
                    enc_val = Some(String::from_utf8_lossy(&ext.raw_payload).into_owned());
                }
            }
            let mut pl_d = Map::new();
            pl_d.insert("type".into(), json!(r_type));
            pl_d.insert(
                "summary".into(),
                json!([{"type": "summary_text", "text": summary_text}]),
            );
            pl_d.insert("content".into(), json!(raw_thinking));
            pl_d.insert(
                "internal_chat_message_metadata_passthrough".into(),
                json!({"turn_id": anchor.turn_id.as_str()}),
            );
            if let Some(enc) = enc_val {
                pl_d.insert("encrypted_content".into(), json!(enc));
            }
            emitter.append(ts, "response_item", pl_d);
            false
        }
        CanonicalEvent::AssistantText {
            anchor,
            text,
            message_id,
            ..
        } => {
            let mut raw_am: Option<Map<String, Value>> = anchor
                .host_extensions
                .iter()
                .find(|ext| {
                    ext.extension_type == "codex_agent_message" && !ext.raw_payload.is_empty()
                })
                .map(|ext| btree_to_map(safe_json_dict(&String::from_utf8_lossy(&ext.raw_payload))))
                .filter(|m| !m.is_empty());
            if let Some(ref mut am) = raw_am {
                if let Some(Value::Object(pt)) =
                    am.get_mut("internal_chat_message_metadata_passthrough")
                {
                    pt.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
                }
                emitter.append(ts, "response_item", am.clone());
            } else {
                let mut pl = Map::new();
                pl.insert("type".into(), json!("message"));
                pl.insert("status".into(), json!("completed"));
                pl.insert(
                    "id".into(),
                    json!(
                        message_id
                            .clone()
                            .unwrap_or_else(|| format!("msg_asst_{hint}"))
                    ),
                );
                pl.insert("role".into(), json!("assistant"));
                pl.insert(
                    "content".into(),
                    json!([{"type": "output_text", "text": text}]),
                );
                pl.insert(
                    "internal_chat_message_metadata_passthrough".into(),
                    json!({"turn_id": anchor.turn_id.as_str()}),
                );
                emitter.append(ts, "response_item", pl);
            }
            let mut ev_pl = Map::new();
            ev_pl.insert("type".into(), json!("agent_message"));
            ev_pl.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
            ev_pl.insert("message".into(), json!(text));
            ev_pl.insert("phase".into(), json!("final_answer"));
            emitter.append(ts, "event_msg", ev_pl);
            false
        }
        CanonicalEvent::ToolCall {
            anchor,
            call_id,
            tool_name,
            payload,
            thinking_signature,
            ..
        } => {
            if let Some(ext) = anchor
                .host_extensions
                .iter()
                .find(|e| e.extension_type == "codex_web_search_call" && !e.raw_payload.is_empty())
            {
                let raw_ws =
                    btree_to_map(safe_json_dict(&String::from_utf8_lossy(&ext.raw_payload)));
                if !raw_ws.is_empty() {
                    ws_native_calls.insert(call_id.clone());
                    emitter.append(ts, "response_item", raw_ws);
                    return false;
                }
            }
            let (c_kind, c_name, args_json, custom_input) =
                encode_tool_for_codex(tool_name, payload);
            let mut call_pt = Map::new();
            call_pt.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
            if let Some(sig) = thinking_signature.as_deref().filter(|s| !s.is_empty()) {
                call_pt.insert("thinking_signature_hex".into(), json!(hex::encode(sig)));
            }
            let mut call_pl = Map::new();
            call_pl.insert("type".into(), json!(c_kind));
            call_pl.insert("status".into(), json!("completed"));
            call_pl.insert("call_id".into(), json!(call_id.as_str()));
            call_pl.insert("name".into(), json!(c_name));
            call_pl.insert("arguments".into(), json!(args_json));
            call_pl.insert(
                "internal_chat_message_metadata_passthrough".into(),
                Value::Object(call_pt),
            );
            if let Some(ci) = custom_input {
                call_pl.insert("input".into(), json!(ci));
            }
            if let Some(ns_ext) = anchor
                .host_extensions
                .iter()
                .find(|e| e.extension_type == "tool_namespace" && !e.raw_payload.is_empty())
            {
                call_pl.insert(
                    "namespace".into(),
                    json!(String::from_utf8_lossy(&ns_ext.raw_payload).into_owned()),
                );
            }
            emitter.append(ts, "response_item", call_pl);
            false
        }
        CanonicalEvent::ToolResult {
            anchor,
            call_id,
            tool_name,
            output_text,
            is_error,
            payload,
            ..
        } => {
            if let Some(ext) = anchor
                .host_extensions
                .iter()
                .find(|e| e.extension_type == "codex_unpaired_tool_output")
            {
                let mut raw_output =
                    btree_to_map(safe_json_dict(&String::from_utf8_lossy(&ext.raw_payload)));
                if !raw_output.is_empty() {
                    if let Some(Value::Object(pt)) =
                        raw_output.get_mut("internal_chat_message_metadata_passthrough")
                    {
                        pt.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
                    }
                    emitter.append(ts, "response_item", raw_output);
                    return false;
                }
            }
            if !ws_native_calls.contains(call_id) {
                let spec = find_tool_spec_by_name(payload.kind_str());
                let out_kind = if spec.is_some_and(|s| s.codex_call_kind == "custom_tool_call") {
                    "custom_tool_call_output"
                } else if tool_name == "tool_search" {
                    "tool_search_output"
                } else {
                    "function_call_output"
                };
                let mut res_pt = Map::new();
                res_pt.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
                let rmeta = extract_tool_result_meta(payload);
                if !rmeta.is_empty() {
                    res_pt.insert(
                        "tool_result_meta".into(),
                        Value::Object(btree_to_map(rmeta)),
                    );
                }
                if anchor
                    .host_extensions
                    .iter()
                    .any(|e| e.extension_type == "synthesized_dangling_output")
                {
                    res_pt.insert("synthesized_dangling_output".into(), json!(true));
                }
                let mut pl = Map::new();
                pl.insert("type".into(), json!(out_kind));
                pl.insert("call_id".into(), json!(call_id.as_str()));
                pl.insert("output".into(), json!(output_text));
                pl.insert("is_error".into(), json!(*is_error));
                pl.insert(
                    "status".into(),
                    json!(if *is_error { "failed" } else { "completed" }),
                );
                pl.insert(
                    "internal_chat_message_metadata_passthrough".into(),
                    Value::Object(res_pt),
                );
                emitter.append(ts, "response_item", pl);
            }
            false
        }
        CanonicalEvent::Checkpoint {
            anchor,
            checkpoint_index,
            user_intent,
            session_summary,
            code_change_summary,
            conversation_title,
        } => {
            let mut cp_pl = Map::new();
            if let Some(ext) = anchor
                .host_extensions
                .iter()
                .find(|e| e.extension_type == "turn_context" && !e.raw_payload.is_empty())
            {
                cp_pl = btree_to_map(safe_json_dict(&String::from_utf8_lossy(&ext.raw_payload)));
            }
            cp_pl.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
            cp_pl.insert("root_turn_id".into(), json!(anchor.turn_id.as_str()));
            cp_pl.insert("checkpoint_index".into(), json!(*checkpoint_index));
            cp_pl.insert("user_intent".into(), json!(user_intent));
            cp_pl.insert("summary".into(), json!(session_summary));
            cp_pl.insert("code_change_summary".into(), json!(code_change_summary));
            if let Some(t) = conversation_title.as_deref().filter(|s| !s.is_empty()) {
                cp_pl.insert("conversation_title".into(), json!(t));
            }
            emitter.append(ts, "turn_context", cp_pl);
            false
        }
        CanonicalEvent::TaskBoundary {
            anchor,
            boundary_kind,
            task_name,
            task_status,
            task_summary,
            ..
        } => {
            let ev_type = if matches!(task_status.as_str(), "task_started" | "task_complete") {
                task_status.as_str()
            } else {
                "task_complete"
            };
            let mut pl = Map::new();
            pl.insert("type".into(), json!(ev_type));
            pl.insert("turn_id".into(), json!(anchor.turn_id.as_str()));
            pl.insert("root_turn_id".into(), json!(anchor.turn_id.as_str()));
            pl.insert("task_name".into(), json!(task_name));
            pl.insert("task_summary".into(), json!(task_summary));
            pl.insert("last_agent_message".into(), json!(task_summary));
            pl.insert("boundary_kind".into(), json!(boundary_kind.as_str()));
            pl.insert("task_status".into(), json!(task_status));
            emitter.append(ts, "event_msg", pl);
            task_status == "task_complete"
        }
        CanonicalEvent::SystemMessage {
            anchor,
            event_type,
            message_text,
        } => {
            encode_codex_system_event(anchor, event_type, message_text, ts, emitter);
            false
        }
    }
}

/// Serialize a slice of [`CanonicalEvent`] nodes into Codex `rollout-*.jsonl` lines.
#[must_use]
pub fn encode_codex_rollout(
    events: &[CanonicalEvent],
    starting_line_no: RolloutLineNo,
    session_id: Option<&str>,
    cwd: Option<&str>,
) -> Vec<CodexRawRolloutLine> {
    let mut emitter = RolloutLineEmitter::new(starting_line_no);

    let has_session_meta = events.iter().any(|e| {
        matches!(
            e,
            CanonicalEvent::SystemMessage { event_type, .. } if event_type == "session_meta"
        )
    });
    if !events.is_empty() && starting_line_no.0 == 1 && !has_session_meta {
        let first_ts = if events[0].anchor().timestamp_iso.is_empty() {
            "2026-01-01T00:00:00Z"
        } else {
            events[0].anchor().timestamp_iso.as_str()
        };
        let eff_sid = session_id.unwrap_or("codex-synced-session");
        let mut meta_pl = Map::new();
        meta_pl.insert("id".into(), json!(eff_sid));
        meta_pl.insert("session_id".into(), json!(eff_sid));
        meta_pl.insert("timestamp".into(), json!(first_ts));
        meta_pl.insert("originator".into(), json!("codex_agy_sync"));
        meta_pl.insert("source".into(), json!("desktop"));
        meta_pl.insert("model_provider".into(), json!("openai"));
        if let Some(c) = cwd {
            meta_pl.insert("cwd".into(), json!(c));
        }
        emitter.append(first_ts, "session_meta", meta_pl);
    }

    let resolved_ids: HashSet<ToolCallId> = events
        .iter()
        .filter_map(|e| match e {
            CanonicalEvent::ToolResult { call_id, .. } => Some(call_id.clone()),
            _ => None,
        })
        .collect();
    let synth_ext = HostExtensionEnvelope {
        host: HostKind::Codex,
        extension_type: "synthesized_dangling_output".to_owned(),
        raw_payload: b"1".to_vec(),
        metadata: Vec::new(),
    };
    let mut dangling_turn_calls: Vec<(ToolCallId, String, CanonicalToolPayload, HostKind)> =
        Vec::new();
    let mut active_turn: Option<TurnId> = None;
    let mut active_turn_closed = true;
    let mut turn_events_buf: Vec<&CanonicalEvent> = Vec::new();
    let mut last_ts = "2026-01-01T00:00:00Z".to_owned();
    let mut ws_native_calls: HashSet<ToolCallId> = HashSet::new();

    for ev in events {
        if !ev.anchor().timestamp_iso.is_empty() {
            last_ts.clone_from(&ev.anchor().timestamp_iso);
        }
        let ts = last_ts.clone();
        if let CanonicalEvent::SystemMessage {
            anchor,
            event_type,
            message_text,
        } = ev
            && matches!(event_type.as_str(), "session_meta" | "world_state")
        {
            let raw_dict = anchor
                .host_extensions
                .iter()
                .find(|ext| ext.extension_type == *event_type && !ext.raw_payload.is_empty())
                .map(|ext| btree_to_map(safe_json_dict(&String::from_utf8_lossy(&ext.raw_payload))))
                .filter(|m| !m.is_empty())
                .unwrap_or_else(|| {
                    let mut m = Map::new();
                    m.insert("message".into(), json!(message_text));
                    m.insert("event_type".into(), json!(event_type));
                    m
                });
            emitter.append(&ts, event_type, raw_dict);
            continue;
        }

        if active_turn.as_ref() != Some(&ev.anchor().turn_id) {
            if let Some(prev_turn) = active_turn.take() {
                for (cid, tname, payload, origin_host) in dangling_turn_calls.drain(..) {
                    if !ws_native_calls.contains(&cid) {
                        let mut anchor =
                            EventOriginAnchor::new(origin_host, prev_turn.clone(), &ts);
                        anchor.host_extensions = vec![synth_ext.clone()];
                        let s_res = CanonicalEvent::ToolResult {
                            anchor,
                            call_id: cid,
                            tool_name: tname,
                            output_text: INTERRUPTED_TOOL_OUTPUT.to_owned(),
                            is_error: true,
                            payload,
                            full_output_uri: None,
                        };
                        encode_single_codex_event(&s_res, &ts, &mut ws_native_calls, &mut emitter);
                    }
                }
                if !active_turn_closed {
                    let mut pl = Map::new();
                    pl.insert("type".into(), json!("task_complete"));
                    pl.insert("turn_id".into(), json!(prev_turn.as_str()));
                    pl.insert("root_turn_id".into(), json!(prev_turn.as_str()));
                    pl.insert("last_agent_message".into(), json!("Turn complete"));
                    emitter.append(&ts, "event_msg", pl);
                }
            }
            let new_turn = ev.anchor().turn_id.clone();
            active_turn = Some(new_turn.clone());
            active_turn_closed = false;
            turn_events_buf.clear();
            let is_task_started = matches!(
                ev,
                CanonicalEvent::TaskBoundary { task_status, .. } if task_status == "task_started"
            );
            if !is_task_started {
                let mut pl = Map::new();
                pl.insert("type".into(), json!("task_started"));
                pl.insert("turn_id".into(), json!(new_turn.as_str()));
                pl.insert("root_turn_id".into(), json!(new_turn.as_str()));
                emitter.append(&ts, "event_msg", pl);
            }
        }
        turn_events_buf.push(ev);
        if encode_single_codex_event(ev, &ts, &mut ws_native_calls, &mut emitter) {
            active_turn_closed = true;
        }
        if let CanonicalEvent::ToolCall {
            anchor,
            call_id,
            tool_name,
            payload,
            ..
        } = ev
            && !resolved_ids.contains(call_id)
            && !ws_native_calls.contains(call_id)
        {
            dangling_turn_calls.push((
                call_id.clone(),
                tool_name.clone(),
                payload.clone(),
                anchor.origin_host,
            ));
        }
    }

    if let Some(at) = active_turn
        && !active_turn_closed
        && dangling_turn_calls.is_empty()
    {
        let lw = turn_events_buf
            .iter()
            .enumerate()
            .filter(|(_, e)| is_nonterminal_work_or_abort_event(e))
            .map(|(i, _)| i as isize)
            .max()
            .unwrap_or(-1);
        let lc = turn_events_buf
            .iter()
            .enumerate()
            .filter(|(_, e)| is_terminal_completion_event(e))
            .map(|(i, _)| i as isize)
            .max()
            .unwrap_or(-1);
        let has_active_agy = turn_events_buf.iter().any(|e| {
            e.anchor()
                .host_extensions
                .iter()
                .any(|ext| ext.extension_type == "agy_step_status")
        });
        if lw <= lc && !has_active_agy {
            let mut pl = Map::new();
            pl.insert("type".into(), json!("task_complete"));
            pl.insert("turn_id".into(), json!(at.as_str()));
            pl.insert("root_turn_id".into(), json!(at.as_str()));
            pl.insert("last_agent_message".into(), json!("Turn complete"));
            emitter.append(&last_ts, "event_msg", pl);
        }
    }

    emitter.lines
}
