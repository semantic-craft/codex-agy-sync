//! Codex `app-server` RPC `thread/read` decoder and rollout prefix splicer.

use std::collections::HashSet;

use cas_core::{
    CanonicalEvent, EventOriginAnchor, FileEditOp, HostKind, MediaAttachment, RolloutLineNo,
    TaskBoundaryKind, TokenUsageAccounting, ToolCallId, TrajectoryLedger, TurnId, WorkspacePath,
    WorkspaceRootUri, normalize_tool_file_path,
};
use serde_json::{Map, Value, json};

use crate::error::CodecError;

fn is_completed_status(st: &str) -> bool {
    matches!(
        st,
        "completed" | "failed" | "declined" | "aborted" | "cancelled" | "canceled"
    )
}

fn is_error_status(st: &str) -> bool {
    matches!(
        st,
        "failed" | "declined" | "aborted" | "cancelled" | "canceled" | "error"
    )
}

fn val_str(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => b.to_string(),
        _ => String::new(),
    }
}

fn extract_rpc_content_blocks(val: Option<&Value>) -> (String, Vec<MediaAttachment>) {
    let Some(v) = val else {
        return (String::new(), Vec::new());
    };
    if let Some(s) = v.as_str() {
        return (s.to_owned(), Vec::new());
    }
    let mut texts = Vec::new();
    let mut media = Vec::new();
    if let Some(arr) = v.as_array() {
        for blk in arr {
            let Some(obj) = blk.as_object() else {
                continue;
            };
            if let Some(txt) = obj.get("text").filter(|t| !t.is_null()) {
                texts.push(val_str(Some(txt)));
            } else if matches!(
                obj.get("type").and_then(Value::as_str),
                Some("input_image" | "image")
            ) {
                let mime = obj
                    .get("mime_type")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or("image/png")
                    .to_owned();
                let uri = obj
                    .get("image_url")
                    .or_else(|| obj.get("uri"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                media.push(MediaAttachment {
                    mime_type: mime,
                    uri,
                    byte_size: None,
                    caption: None,
                });
            }
        }
    }
    (texts.join("\n"), media)
}

fn stringify_rpc_output(val: Option<&Value>) -> String {
    let Some(v) = val else {
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
        Value::Object(obj) => {
            let btree: std::collections::BTreeMap<&String, &Value> = obj.iter().collect();
            json!(btree).to_string()
        }
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn status_type_lower(val: Option<&Value>, default_status: &str) -> String {
    match val {
        Some(Value::Object(m)) => m
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or(default_status)
            .trim()
            .to_ascii_lowercase(),
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() {
                default_status.to_owned()
            } else {
                t.to_ascii_lowercase()
            }
        }
        _ => default_status.to_owned(),
    }
}

fn map_file_change_op(raw_kind: Option<&Value>) -> FileEditOp {
    let lc = status_type_lower(raw_kind, "");
    match lc.as_str() {
        "add" | "create" => FileEditOp::Add,
        "delete" => FileEditOp::Delete,
        _ => FileEditOp::Modify,
    }
}

fn decode_rpc_turn_item(
    item: &Map<String, Value>,
    anchor: &EventOriginAnchor,
    t_idx: usize,
    workspace_path: &WorkspacePath,
    events: &mut Vec<CanonicalEvent>,
) -> Result<(), CodecError> {
    let itype = item.get("type").and_then(Value::as_str).unwrap_or("");
    let st_lc = status_type_lower(item.get("status"), "completed");
    let is_completed = is_completed_status(&st_lc);
    let is_error = is_error_status(&st_lc);

    match itype {
        "userMessage" => {
            let raw_c = item.get("content").or_else(|| item.get("text"));
            let (query_text, media) = extract_rpc_content_blocks(raw_c);
            events.push(CanonicalEvent::UserInput {
                anchor: anchor.clone(),
                query_text,
                scope_items: Vec::new(),
                media,
            });
        }
        "agentMessage" => {
            let text = if let Some(t) = item.get("text").filter(|v| !v.is_null()) {
                val_str(Some(t))
            } else {
                extract_rpc_content_blocks(item.get("content")).0
            };
            let message_id = item
                .get("id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned);
            events.push(CanonicalEvent::AssistantText {
                anchor: anchor.clone(),
                text,
                message_id,
                stop_reason: None,
                generator_model: None,
                token_usage: TokenUsageAccounting::default(),
            });
        }
        "reasoning" => {
            let summary_text = match item.get("summary") {
                Some(Value::Array(arr)) => arr
                    .iter()
                    .map(|x| val_str(Some(x)))
                    .collect::<Vec<_>>()
                    .join("\n"),
                other => val_str(other),
            };
            events.push(CanonicalEvent::Thinking {
                anchor: anchor.clone(),
                summary_text,
                raw_thinking: None,
                thinking_signature: None,
                token_usage: TokenUsageAccounting::default(),
            });
        }
        "commandExecution" => {
            let raw_cid = item
                .get("id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("rpc-cmd-{t_idx}-{}", events.len()));
            let cid = ToolCallId::parse(&raw_cid)?;
            let exit_code =
                item.get("exitCode")
                    .and_then(Value::as_i64)
                    .or(if is_completed && !is_error {
                        Some(0)
                    } else {
                        None
                    });
            let raw_cwd = item
                .get("cwd")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| workspace_path.as_path().display().to_string());
            let pl = cas_core::CanonicalToolPayload::RunCommand {
                command_line: val_str(item.get("command")),
                cwd: normalize_tool_file_path(&raw_cwd).into_owned(),
                exit_code,
                timed_out: false,
                is_daemon: false,
                shell_name: None,
                ran_in_sandbox: false,
                sandbox_policy: None,
            };
            events.push(CanonicalEvent::ToolCall {
                anchor: anchor.clone(),
                call_id: cid.clone(),
                tool_name: "exec_command".to_owned(),
                tool_summary: "exec_command".to_owned(),
                payload: pl.clone(),
                thinking_signature: None,
            });
            if is_completed {
                events.push(CanonicalEvent::ToolResult {
                    anchor: anchor.clone(),
                    call_id: cid,
                    tool_name: "exec_command".to_owned(),
                    output_text: val_str(item.get("aggregatedOutput")),
                    is_error,
                    payload: pl,
                    full_output_uri: None,
                });
            }
        }
        "fileChange" => {
            let base_cid = item
                .get("id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("rpc-patch-{t_idx}-{}", events.len()));
            let raw_changes: Vec<&Map<String, Value>> = item
                .get("changes")
                .and_then(Value::as_array)
                .map(|arr| arr.iter().filter_map(Value::as_object).collect())
                .unwrap_or_default();
            let empty_map = Map::new();
            let changes: Vec<&Map<String, Value>> = if raw_changes.is_empty() {
                vec![&empty_map]
            } else {
                raw_changes
            };
            for (idx, ch) in changes.into_iter().enumerate() {
                let cid_str = if idx == 0 {
                    base_cid.clone()
                } else {
                    format!("{base_cid}:{idx}")
                };
                let cid = ToolCallId::parse(&cid_str)?;
                let f_pl = cas_core::CanonicalToolPayload::FileEdit {
                    path: normalize_tool_file_path(&val_str(ch.get("path"))).into_owned(),
                    edit_op: map_file_change_op(ch.get("kind")),
                    unified_diff: val_str(ch.get("diff")),
                    diff_stats: (0, 0),
                    is_artifact: false,
                    artifact_summary: None,
                };
                events.push(CanonicalEvent::ToolCall {
                    anchor: anchor.clone(),
                    call_id: cid.clone(),
                    tool_name: "apply_patch".to_owned(),
                    tool_summary: "apply_patch".to_owned(),
                    payload: f_pl.clone(),
                    thinking_signature: None,
                });
                if is_completed {
                    events.push(CanonicalEvent::ToolResult {
                        anchor: anchor.clone(),
                        call_id: cid,
                        tool_name: "apply_patch".to_owned(),
                        output_text: "Applied patch".to_owned(),
                        is_error,
                        payload: f_pl,
                        full_output_uri: None,
                    });
                }
            }
        }
        "webSearch" => {
            let raw_cid = item
                .get("id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("rpc-ws-{t_idx}-{}", events.len()));
            let cid = ToolCallId::parse(&raw_cid)?;
            let query = val_str(item.get("query"));
            let w_pl = cas_core::CanonicalToolPayload::SearchWeb {
                query: query.clone(),
                domain_filter: None,
                documents: Vec::new(),
                summary: None,
            };
            events.push(CanonicalEvent::ToolCall {
                anchor: anchor.clone(),
                call_id: cid.clone(),
                tool_name: "web_search".to_owned(),
                tool_summary: "web_search".to_owned(),
                payload: w_pl.clone(),
                thinking_signature: None,
            });
            if is_completed {
                events.push(CanonicalEvent::ToolResult {
                    anchor: anchor.clone(),
                    call_id: cid,
                    tool_name: "web_search".to_owned(),
                    output_text: format!("Web search: {query}"),
                    is_error,
                    payload: w_pl,
                    full_output_uri: None,
                });
            }
        }
        _ if matches!(itype, "mcpToolCall" | "dynamicToolCall" | "collabToolCall")
            || itype.ends_with("ToolCall") =>
        {
            let raw_cid = item
                .get("id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("rpc-tool-{t_idx}-{}", events.len()));
            let cid = ToolCallId::parse(&raw_cid)?;
            let tname = item
                .get("tool")
                .or_else(|| item.get("name"))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or(itype)
                .to_owned();
            let raw_args = item.get("arguments");
            let args_json = match raw_args {
                Some(Value::String(s)) => s.clone(),
                Some(other) => {
                    let btree = cas_core::sort_json_value(other);
                    btree.to_string()
                }
                None => cas_core::sort_json_value(&Value::Object(item.clone())).to_string(),
            };
            let o_pl = cas_core::CanonicalToolPayload::Opaque {
                tool_name: tname.clone(),
                raw_arguments_json: args_json,
                type_url: None,
                raw_any_bytes: None,
            };
            events.push(CanonicalEvent::ToolCall {
                anchor: anchor.clone(),
                call_id: cid.clone(),
                tool_name: tname.clone(),
                tool_summary: tname.clone(),
                payload: o_pl.clone(),
                thinking_signature: None,
            });
            if is_completed {
                let raw_res = item
                    .get("result")
                    .or_else(|| if is_error { item.get("error") } else { None });
                let has_err = is_error || item.get("error").is_some_and(|e| !e.is_null());
                events.push(CanonicalEvent::ToolResult {
                    anchor: anchor.clone(),
                    call_id: cid,
                    tool_name: tname,
                    output_text: stringify_rpc_output(raw_res),
                    is_error: has_err,
                    payload: o_pl,
                    full_output_uri: None,
                });
            }
        }
        _ => {}
    }
    Ok(())
}

/// Translate a Codex `app-server` `thread/read` payload (`turns[*].items[*]`) into [`TrajectoryLedger`].
///
/// # Errors
/// Returns [`CodecError::Core`] if turn/tool identifiers fail validation.
pub fn decode_codex_rpc_thread_to_ledger(
    workspace_path: WorkspacePath,
    workspace_uri: WorkspaceRootUri,
    thread_obj: &Map<String, Value>,
    explicit_title_override: Option<&str>,
) -> Result<TrajectoryLedger, CodecError> {
    let mut events = Vec::new();
    let ts_default = thread_obj
        .get("updatedAt")
        .or_else(|| thread_obj.get("createdAt"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("2026-01-01T00:00:00Z");

    if let Some(turns) = thread_obj.get("turns").and_then(Value::as_array) {
        for (idx, turn_val) in turns.iter().enumerate() {
            let t_idx = idx + 1;
            let Some(turn) = turn_val.as_object() else {
                continue;
            };
            let anchor = EventOriginAnchor::new(
                HostKind::Codex,
                TurnId::parse(&format!("turn-{t_idx}"))?,
                ts_default,
            );
            if let Some(items) = turn.get("items").and_then(Value::as_array) {
                for item_val in items {
                    if let Some(item) = item_val.as_object() {
                        decode_rpc_turn_item(item, &anchor, t_idx, &workspace_path, &mut events)?;
                    }
                }
            }
            let t_status = status_type_lower(turn.get("status"), "completed");
            if t_status == "completed" {
                events.push(CanonicalEvent::TaskBoundary {
                    anchor,
                    boundary_kind: TaskBoundaryKind::TaskBoundary,
                    task_name: String::new(),
                    task_status: "task_complete".to_owned(),
                    task_summary: "Turn complete".to_owned(),
                    mode: None,
                    blocked_on_user: false,
                    review_uris: Vec::new(),
                });
            } else if matches!(t_status.as_str(), "inprogress" | "in_progress") {
                events.push(CanonicalEvent::TaskBoundary {
                    anchor,
                    boundary_kind: TaskBoundaryKind::TaskBoundary,
                    task_name: String::new(),
                    task_status: "in_progress".to_owned(),
                    task_summary: String::new(),
                    mode: None,
                    blocked_on_user: false,
                    review_uris: Vec::new(),
                });
            } else if matches!(t_status.as_str(), "interrupted" | "failed") {
                let err_msg = match turn.get("error") {
                    Some(Value::Object(m)) => val_str(m.get("message")),
                    Some(other) => val_str(Some(other)),
                    None => val_str(turn.get("status")),
                };
                let final_msg = if err_msg.is_empty() {
                    t_status
                } else {
                    err_msg
                };
                events.push(CanonicalEvent::SystemMessage {
                    anchor,
                    event_type: "codex_event_msg:turn_aborted".to_owned(),
                    message_text: final_msg,
                });
            }
        }
    }

    let title_override = thread_obj
        .get("name")
        .or_else(|| thread_obj.get("title"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or(explicit_title_override);

    Ok(TrajectoryLedger::from_events(
        workspace_path,
        workspace_uri,
        &events,
        title_override,
        false,
    ))
}

/// Splice flushed completed rollout turns ahead of newer RPC turns to preserve exact rollout digests.
#[must_use]
pub fn splice_rollout_completed_prefix(
    disk_ledger: &TrajectoryLedger,
    rpc_ledger: &TrajectoryLedger,
    rpc_is_active: bool,
    explicit_title_override: Option<&str>,
) -> TrajectoryLedger {
    let Some(disk_wm) = disk_ledger.latest_watermark() else {
        let mut out = rpc_ledger.clone();
        if rpc_is_active && !out.has_inflight_turn {
            out.has_inflight_turn = true;
        }
        return out;
    };
    let disk_completed_turns: HashSet<&TurnId> = disk_ledger
        .completed_turn_watermarks
        .iter()
        .map(|wm| &wm.turn_id)
        .collect();
    let rpc_tail: Vec<CanonicalEvent> = rpc_ledger
        .events
        .iter()
        .filter(|ev| !disk_completed_turns.contains(&ev.anchor().turn_id))
        .cloned()
        .collect();
    if rpc_tail.is_empty() {
        let mut out = disk_ledger.clone();
        if !rpc_is_active
            && disk_ledger.completed_turn_watermarks.len()
                >= rpc_ledger.completed_turn_watermarks.len()
            && disk_ledger.has_inflight_turn == rpc_ledger.has_inflight_turn
        {
            return out;
        }
        if rpc_is_active && !out.has_inflight_turn {
            out.has_inflight_turn = true;
        }
        return out;
    }

    let prefix_end = (disk_wm.last_ledger_seq.0 + 1).min(disk_ledger.events.len());
    let mut combined = disk_ledger.events[..prefix_end].to_vec();
    for mut ev in rpc_tail {
        let a = ev.anchor_mut();
        a.agy_step_index = None;
        a.codex_line_no = None;
        combined.push(ev);
    }
    let title = explicit_title_override.or(Some(rpc_ledger.title.as_str()));
    let mut merged = TrajectoryLedger::from_events(
        rpc_ledger.workspace_path.clone(),
        rpc_ledger.workspace_uri.clone(),
        &combined,
        title,
        false,
    );
    if rpc_is_active && !merged.has_inflight_turn {
        merged.has_inflight_turn = true;
    }
    let _ = RolloutLineNo(0);
    merged
}
