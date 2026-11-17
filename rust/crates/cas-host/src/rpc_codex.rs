//! `AF_UNIX` RFC 6455 WebSocket JSON-RPC v2 client for `codex app-server`.

use std::collections::HashSet;
use std::fs;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::{Duration, Instant};

use cas_codecs::read_codex_rollout_lines;
use cas_core::{CodexDaemonEndpoint, CodexThreadId, PACKAGE_VERSION, validate_session_id};
use serde_json::{Map, Value, json};
use tungstenite::{Message, WebSocket};
use uuid::Uuid;

use crate::error::HostError;
use crate::locks::{PresenceWalGuard, has_live_unix_socket, unix_socket_connect_path};
use crate::paths::{HostEnvironmentPaths, parse_ws_ident};

const ACTIVE_TURN_STATUSES: &[&str] = &["active", "running", "inprogress", "in_progress"];
const NOT_FOUND_MARKERS: &[&str] = &[
    "not found",
    "no rollout",
    "unknown thread",
    "does not exist",
];

/// Decoded `codex app-server` `thread/read` JSON-RPC v2 response.
#[derive(Debug, Clone)]
pub struct CodexRpcReadResult {
    pub thread: Map<String, Value>,
    pub is_active: bool,
}

/// Active WebSocket client over an `AF_UNIX` stream connected to `codex app-server`.
pub struct UnixWebSocketRpc {
    ws: WebSocket<UnixStream>,
}

impl UnixWebSocketRpc {
    fn set_timeout(&mut self, timeout: Duration) -> Result<(), HostError> {
        self.ws
            .get_mut()
            .set_read_timeout(Some(timeout))
            .map_err(|e| {
                HostError::OnlineRpcUnavailable(format!("Unix socket timeout error: {e}"))
            })?;
        self.ws
            .get_mut()
            .set_write_timeout(Some(timeout))
            .map_err(|e| {
                HostError::OnlineRpcUnavailable(format!("Unix socket timeout error: {e}"))
            })?;
        Ok(())
    }

    pub fn send_json(&mut self, obj: &Value) -> Result<(), HostError> {
        let text = obj.to_string();
        self.ws.send(Message::Text(text.into())).map_err(|e| {
            HostError::OnlineRpcUnavailable(format!("Codex WebSocket send failed: {e}"))
        })
    }

    pub fn recv_json(&mut self) -> Result<Map<String, Value>, HostError> {
        loop {
            let msg = self.ws.read().map_err(|e| {
                HostError::OnlineRpcUnavailable(format!("Codex WebSocket read failed: {e}"))
            })?;
            match msg {
                Message::Text(txt) => {
                    let parsed: Value = serde_json::from_str(&txt).map_err(|_| {
                        HostError::OnlineRpcUnavailable(
                            "Malformed JSON frame from Codex app-server.".to_owned(),
                        )
                    })?;
                    return parsed.as_object().cloned().ok_or_else(|| {
                        HostError::OnlineRpcUnavailable(
                            "Non-object JSON-RPC message from Codex app-server.".to_owned(),
                        )
                    });
                }
                Message::Binary(bytes) => {
                    let parsed: Value = serde_json::from_slice(&bytes).map_err(|_| {
                        HostError::OnlineRpcUnavailable(
                            "Malformed JSON frame from Codex app-server.".to_owned(),
                        )
                    })?;
                    return parsed.as_object().cloned().ok_or_else(|| {
                        HostError::OnlineRpcUnavailable(
                            "Non-object JSON-RPC message from Codex app-server.".to_owned(),
                        )
                    });
                }
                Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => continue,
                Message::Close(_) => {
                    return Err(HostError::OnlineRpcUnavailable(
                        "Codex WebSocket closed by server.".to_owned(),
                    ));
                }
            }
        }
    }

    pub fn wait_for_id(
        &mut self,
        target_id: i64,
        max_messages: usize,
    ) -> Result<Map<String, Value>, HostError> {
        for _ in 0..max_messages {
            let msg = self.recv_json()?;
            if msg.get("id").and_then(Value::as_i64) == Some(target_id) {
                return Ok(msg);
            }
        }
        Err(HostError::OnlineRpcUnavailable(format!(
            "Timed out awaiting Codex RPC response id={target_id}."
        )))
    }

    pub fn close(mut self) {
        let _ = self.ws.close(None);
    }
}

/// Connect and run `initialize`/`initialized` handshake with `codex app-server` over `AF_UNIX`.
///
/// # Errors
/// Returns [`HostError::OnlineRpcUnavailable`] if socket is missing or handshake fails.
pub fn open_codex_ws_rpc(
    endpoint: Option<&CodexDaemonEndpoint>,
    timeout_s: f64,
) -> Result<UnixWebSocketRpc, HostError> {
    let Some(ep) = endpoint.filter(|e| has_live_unix_socket(Some(&e.socket_path))) else {
        return Err(HostError::OnlineRpcUnavailable(
            "No live Codex app-server Unix socket available.".to_owned(),
        ));
    };
    let conn_path = unix_socket_connect_path(&ep.socket_path);
    let stream = UnixStream::connect(&conn_path).map_err(|e| {
        HostError::OnlineRpcUnavailable(format!(
            "Codex app-server Unix socket connection failed: {e}"
        ))
    })?;
    let timeout = Duration::from_secs_f64(timeout_s.max(0.1));
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));

    let (ws, _resp) = tungstenite::client("ws://localhost/", stream).map_err(|e| {
        HostError::OnlineRpcUnavailable(format!("Codex WebSocket handshake failed: {e}"))
    })?;
    let mut client = UnixWebSocketRpc { ws };
    client.send_json(&json!({
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "codex-agy-sync", "version": PACKAGE_VERSION},
            "capabilities": {"experimentalApi": true},
        }
    }))?;
    let init_resp = client.wait_for_id(1, 128)?;
    if init_resp.contains_key("error") {
        return Err(HostError::OnlineRpcUnavailable(
            "Codex app-server initialize RPC failed.".to_owned(),
        ));
    }
    client.send_json(&json!({"method": "initialized", "params": {}}))?;
    Ok(client)
}

fn status_str_lower(val: Option<&Value>) -> String {
    match val {
        Some(Value::Object(m)) => m
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_ascii_lowercase(),
        Some(Value::String(s)) => s.to_ascii_lowercase(),
        _ => String::new(),
    }
}

/// Extract thread map and active status from a `thread/read` JSON-RPC response.
///
/// # Errors
/// Returns [`HostError::FileNotFound`] or [`HostError::OnlineRpcUnavailable`].
pub fn extract_codex_rpc_thread(
    resp_msg: &Map<String, Value>,
    thread_id: &CodexThreadId,
) -> Result<CodexRpcReadResult, HostError> {
    if let Some(err_obj) = resp_msg.get("error") {
        let err_msg = match err_obj {
            Value::Object(m) => m
                .get("message")
                .and_then(Value::as_str)
                .map_or_else(|| err_obj.to_string(), str::to_owned),
            other => other.to_string(),
        };
        let low = err_msg.to_ascii_lowercase();
        if NOT_FOUND_MARKERS.iter().any(|m| low.contains(m)) {
            return Err(HostError::FileNotFound(format!(
                "Codex thread {} not found via app-server RPC.",
                thread_id.as_str()
            )));
        }
        return Err(HostError::OnlineRpcUnavailable(format!(
            "Codex thread/read RPC error: {err_msg}"
        )));
    }

    let Some(thread) = resp_msg
        .get("result")
        .and_then(Value::as_object)
        .and_then(|r| r.get("thread"))
        .and_then(Value::as_object)
    else {
        return Err(HostError::FileNotFound(format!(
            "Codex thread {} not found via app-server RPC.",
            thread_id.as_str()
        )));
    };

    let status_type = status_str_lower(thread.get("status"));
    let has_active_turn = thread
        .get("turns")
        .and_then(Value::as_array)
        .is_some_and(|turns| {
            turns.iter().any(|t| {
                t.as_object().is_some_and(|to| {
                    let st = status_str_lower(to.get("status"));
                    ACTIVE_TURN_STATUSES.contains(&st.as_str())
                })
            })
        });

    Ok(CodexRpcReadResult {
        thread: thread.clone(),
        is_active: ACTIVE_TURN_STATUSES.contains(&status_type.as_str()) || has_active_turn,
    })
}

/// Read a Codex thread lock-free via `codex app-server` Unix-socket WebSocket (`thread/read`).
///
/// # Errors
/// Returns [`HostError::FileNotFound`] or [`HostError::OnlineRpcUnavailable`].
pub fn fetch_codex_thread_via_rpc(
    endpoint: Option<&CodexDaemonEndpoint>,
    thread_id: &CodexThreadId,
    timeout_s: f64,
) -> Result<CodexRpcReadResult, HostError> {
    validate_session_id(thread_id.as_str(), "codex_thread_id")?;
    let mut rpc = open_codex_ws_rpc(endpoint, timeout_s)?;
    rpc.send_json(&json!({
        "id": 2,
        "method": "thread/read",
        "params": {"threadId": thread_id.as_str(), "includeTurns": true},
    }))?;
    let resp = rpc.wait_for_id(2, 128)?;
    rpc.close();
    extract_codex_rpc_thread(&resp, thread_id)
}

fn codex_project_matches_workspace(
    project: &Value,
    target_path: &cas_core::WorkspacePath,
) -> Option<String> {
    let obj = project.as_object()?;
    let proj_id = obj.get("id").and_then(Value::as_str)?.trim();
    if proj_id.is_empty() {
        return None;
    }
    let roots = obj.get("roots").and_then(Value::as_array)?;
    for root in roots {
        let raw = match root {
            Value::Object(m) => m.get("path").and_then(Value::as_str),
            Value::String(s) => Some(s.as_str()),
            _ => None,
        };
        if let Some(r) = raw.filter(|s| !s.is_empty())
            && let Ok((norm_path, _)) = parse_ws_ident(r)
            && &norm_path == target_path
        {
            return Some(proj_id.to_owned());
        }
    }
    None
}

/// Register `workspace` in live `codex app-server` via `project/list` and `project/create`.
///
/// # Errors
/// Returns [`HostError::OnlineRpcUnavailable`] if daemon is unavailable or RPC fails.
pub fn register_codex_project_via_rpc(
    paths: &HostEnvironmentPaths,
    workspace: &str,
    timeout_s: f64,
) -> Result<String, HostError> {
    let (ws_path, _) = parse_ws_ident(workspace)?;
    let endpoint = PresenceWalGuard::new(paths.clone()).discover_codex_daemon();
    let mut rpc = open_codex_ws_rpc(endpoint.as_ref(), timeout_s)?;

    let mut req_id = 2i64;
    let mut cursor: Option<String> = None;
    let mut existing_id: Option<String> = None;

    for _ in 0..64 {
        rpc.send_json(&json!({
            "id": req_id,
            "method": "project/list",
            "params": {"cursor": cursor, "limit": 100},
        }))?;
        let resp = rpc.wait_for_id(req_id, 128)?;
        req_id += 1;
        if resp.contains_key("error") {
            return Err(HostError::OnlineRpcUnavailable(
                "Codex project/list RPC failed.".to_owned(),
            ));
        }
        let Some(result) = resp.get("result").and_then(Value::as_object) else {
            return Err(HostError::OnlineRpcUnavailable(
                "Invalid Codex project/list RPC result.".to_owned(),
            ));
        };
        let items = result
            .get("data")
            .or_else(|| result.get("projects"))
            .or_else(|| result.get("items"))
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        for item in items {
            if let Some(matched) = codex_project_matches_workspace(item, &ws_path) {
                existing_id = Some(matched);
                break;
            }
        }
        if existing_id.is_some() {
            break;
        }
        let next_cursor = result
            .get("nextCursor")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        if next_cursor.is_none() || next_cursor == cursor.as_deref() {
            break;
        }
        cursor = next_cursor.map(str::to_owned);
    }

    if let Some(id) = existing_id {
        rpc.close();
        return Ok(id);
    }

    let ws_name = ws_path
        .as_path()
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|s| !s.is_empty())
        .map_or_else(|| ws_path.to_string(), str::to_owned);

    rpc.send_json(&json!({
        "id": req_id,
        "method": "project/create",
        "params": {
            "idempotencyKey": Uuid::new_v4().to_string(),
            "name": ws_name,
            "roots": [{"path": ws_path.to_string()}],
            "metadata": Value::Null,
        }
    }))?;
    let create_resp = rpc.wait_for_id(req_id, 128)?;
    rpc.close();
    if create_resp.contains_key("error") {
        return Err(HostError::OnlineRpcUnavailable(
            "Codex project/create RPC failed.".to_owned(),
        ));
    }
    let proj_id = create_resp
        .get("result")
        .and_then(Value::as_object)
        .and_then(|r| r.get("project"))
        .and_then(Value::as_object)
        .and_then(|p| p.get("id"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            HostError::OnlineRpcUnavailable(
                "Codex project/create RPC did not return project id.".to_owned(),
            )
        })?;
    Ok(proj_id.to_owned())
}

struct NativeCodexRpcSession {
    rpc: UnixWebSocketRpc,
    deadline: Instant,
    request_id: i64,
    closed_threads: HashSet<String>,
}

impl NativeCodexRpcSession {
    fn receive(&mut self) -> Result<Map<String, Value>, HostError> {
        let now = Instant::now();
        if now >= self.deadline {
            return Err(HostError::OnlineRpcUnavailable(
                "Codex native import deadline expired.".to_owned(),
            ));
        }
        self.rpc.set_timeout(self.deadline - now)?;
        let message = self.rpc.recv_json()?;
        let method = message.get("method").and_then(Value::as_str).unwrap_or("");
        let params = message.get("params").and_then(Value::as_object);
        let status_type = params
            .and_then(|p| p.get("status"))
            .and_then(Value::as_object)
            .and_then(|s| s.get("type"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if (method == "thread/closed"
            || (method == "thread/status/changed" && status_type == "notLoaded"))
            && let Some(tid) = params
                .and_then(|p| p.get("threadId"))
                .and_then(Value::as_str)
        {
            self.closed_threads.insert(tid.to_owned());
        }
        Ok(message)
    }

    fn call(&mut self, method: &str, params: Value) -> Result<Map<String, Value>, HostError> {
        self.request_id += 1;
        let id = self.request_id;
        self.rpc
            .send_json(&json!({"id": id, "method": method, "params": params}))?;
        loop {
            let response = self.receive()?;
            if response.get("id").and_then(Value::as_i64) != Some(id) {
                continue;
            }
            if let Some(err) = response.get("error") {
                let code = err
                    .as_object()
                    .and_then(|o| o.get("code"))
                    .and_then(Value::as_i64);
                let code_str = code.map_or_else(|| "unknown".to_owned(), |c| c.to_string());
                return Err(HostError::OnlineRpcUnavailable(format!(
                    "Codex {method} RPC failed with code {code_str}."
                )));
            }
            return response
                .get("result")
                .and_then(Value::as_object)
                .cloned()
                .ok_or_else(|| {
                    HostError::OnlineRpcUnavailable(format!(
                        "Codex {method} returned a non-object result."
                    ))
                });
        }
    }

    fn wait_until_closed(
        &mut self,
        thread_id: &CodexThreadId,
        unsubscribe: &Map<String, Value>,
    ) -> Result<(), HostError> {
        if unsubscribe.get("status").and_then(Value::as_str) == Some("notLoaded") {
            return Ok(());
        }
        while !self.closed_threads.contains(thread_id.as_str()) {
            let _ = self.receive()?;
        }
        Ok(())
    }
}

/// Import a staged Codex rollout via `codex app-server` native RPC (`thread/fork`).
///
/// # Errors
/// Returns [`HostError::NativeCodexImportPending`] if failure occurs after `thread/fork` was sent,
/// or [`HostError::OnlineRpcUnavailable`] / [`HostError::InvalidArgument`] before fork.
pub fn import_codex_rollout_via_rpc(
    endpoint: &CodexDaemonEndpoint,
    rollout_path: &Path,
    workspace_path: &Path,
    project_id: &str,
    title: &str,
    timeout_s: f64,
) -> Result<CodexThreadId, HostError> {
    let raw_lines = read_codex_rollout_lines(rollout_path)?;
    let mut rows: Vec<Value> = Vec::with_capacity(raw_lines.len());
    for line in &raw_lines {
        rows.push(serde_json::from_str(&line.raw_json)?);
    }
    let first_type = rows
        .first()
        .and_then(Value::as_object)
        .and_then(|o| o.get("type"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if first_type != "session_meta" {
        return Err(HostError::InvalidArgument(
            "Codex import requires a rollout beginning with session_meta.".to_owned(),
        ));
    }
    let source_id_str = rows[0]
        .as_object()
        .and_then(|o| o.get("payload"))
        .and_then(Value::as_object)
        .and_then(|p| p.get("id"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let source_id = validate_session_id(source_id_str, "source_codex_thread_id")?.to_owned();
    let expect_items = rows.iter().any(|r| {
        r.as_object()
            .and_then(|o| o.get("type"))
            .and_then(Value::as_str)
            == Some("response_item")
    });

    if !has_live_unix_socket(Some(&endpoint.socket_path)) {
        return Err(HostError::OnlineRpcUnavailable(
            "No live Codex app-server Unix socket available.".to_owned(),
        ));
    }

    let deadline = Instant::now() + Duration::from_secs_f64(timeout_s.max(1.0));
    let conn_path = unix_socket_connect_path(&endpoint.socket_path);
    let stream = UnixStream::connect(&conn_path).map_err(|e| {
        HostError::OnlineRpcUnavailable(format!("Codex native import connection failed: {e}"))
    })?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs_f64(timeout_s.max(1.0))));
    let _ = stream.set_write_timeout(Some(Duration::from_secs_f64(timeout_s.max(1.0))));
    let (ws, _) = tungstenite::client("ws://localhost/", stream).map_err(|e| {
        HostError::OnlineRpcUnavailable(format!("Codex native import connection failed: {e}"))
    })?;

    let mut session = NativeCodexRpcSession {
        rpc: UnixWebSocketRpc { ws },
        deadline,
        request_id: 0,
        closed_threads: HashSet::new(),
    };
    let mut fork_sent = false;
    let mut thread_id: Option<CodexThreadId> = None;

    let res: Result<CodexThreadId, HostError> = (|| {
        session.call(
            "initialize",
            json!({
                "clientInfo": {"name": "codex-agy-sync", "version": PACKAGE_VERSION},
                "capabilities": {"experimentalApi": true},
            }),
        )?;
        session
            .rpc
            .send_json(&json!({"method": "initialized", "params": {}}))?;
        fork_sent = true;

        let resolved_rollout =
            fs::canonicalize(rollout_path).unwrap_or_else(|_| rollout_path.to_path_buf());
        let resolved_ws =
            fs::canonicalize(workspace_path).unwrap_or_else(|_| workspace_path.to_path_buf());

        let fork = session.call(
            "thread/fork",
            json!({
                "threadId": source_id,
                "path": resolved_rollout.display().to_string(),
                "cwd": resolved_ws.display().to_string(),
                "ephemeral": false,
            }),
        )?;
        let tid_str = fork
            .get("thread")
            .and_then(Value::as_object)
            .and_then(|t| t.get("id"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let tid = CodexThreadId::parse(validate_session_id(tid_str, "codex_thread_id")?)?;
        thread_id = Some(tid.clone());

        let cfg_res: Result<(), HostError> = (|| {
            session.call(
                "thread/metadata/update",
                json!({"threadId": tid.as_str(), "projectId": project_id}),
            )?;
            session.call(
                "thread/name/set",
                json!({"threadId": tid.as_str(), "name": title}),
            )?;
            let read_res = session.call(
                "thread/read",
                json!({"threadId": tid.as_str(), "includeTurns": true}),
            )?;
            let mut wrapper = Map::new();
            wrapper.insert("result".to_owned(), Value::Object(read_res));
            let parsed = extract_codex_rpc_thread(&wrapper, &tid)?;
            if parsed.thread.get("id").and_then(Value::as_str) != Some(tid.as_str())
                || parsed.thread.get("projectId").and_then(Value::as_str) != Some(project_id)
            {
                return Err(HostError::OnlineRpcUnavailable(
                    "Codex imported thread identity or project did not match.".to_owned(),
                ));
            }
            if expect_items {
                let has_any_item = parsed
                    .thread
                    .get("turns")
                    .and_then(Value::as_array)
                    .is_some_and(|turns| {
                        turns.iter().any(|t| {
                            t.as_object()
                                .and_then(|o| o.get("items"))
                                .and_then(Value::as_array)
                                .is_some_and(|arr| !arr.is_empty())
                        })
                    });
                if !has_any_item {
                    return Err(HostError::OnlineRpcUnavailable(
                        "Codex imported nonempty history without Desktop items.".to_owned(),
                    ));
                }
            }
            Ok(())
        })();
        if let Err(err) = cfg_res {
            let _ = session.call("thread/unsubscribe", json!({"threadId": tid.as_str()}));
            return Err(err);
        }
        let unsub = session.call("thread/unsubscribe", json!({"threadId": tid.as_str()}))?;
        session.wait_until_closed(&tid, &unsub)?;
        Ok(tid)
    })();

    session.rpc.close();
    match res {
        Ok(tid) => Ok(tid),
        Err(err) if fork_sent => Err(HostError::NativeCodexImportPending {
            message: err.to_string(),
            thread_id: thread_id.map(|t| t.as_str().to_owned()),
        }),
        Err(err) => Err(HostError::OnlineRpcUnavailable(format!(
            "Codex native import connection failed: {err}"
        ))),
    }
}
