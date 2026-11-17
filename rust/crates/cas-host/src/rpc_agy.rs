//! Loopback HTTP/1.1 Connect-RPC client for Antigravity `LanguageServerService`.

use std::collections::HashSet;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use cas_codecs::{
    AgyRawStepRow, encode_bytes_field, encode_string_field, encode_varint_field, iter_wire_fields,
    read_varint,
};
use cas_core::{
    AgyDaemonEndpoint, AgyProjectId, CascadeId, PACKAGE_VERSION, StepIndex, WorkspacePath,
    WorkspaceRootUri, validate_session_id,
};
use serde_json::Value;
use uuid::Uuid;

use crate::error::HostError;
use crate::locks::PresenceWalGuard;
use crate::paths::{HostEnvironmentPaths, parse_ws_ident};

/// Decoded `LanguageServerService/GetCascadeTrajectory` binary protobuf payload.
#[derive(Debug, Clone)]
pub struct AgyRpcReadResult {
    pub raw_steps: Vec<AgyRawStepRow>,
    pub cascade_run_status: i32,
    pub trajectory_metadata_blob: Vec<u8>,
    pub source_enum: i32,
}

impl AgyRpcReadResult {
    #[must_use]
    pub const fn is_active(&self) -> bool {
        matches!(self.cascade_run_status, 2..=4)
    }
}

/// Reusable HTTP/1.1 keep-alive connection to loopback Antigravity LanguageServer.
pub struct AgyHttpConn {
    port: u16,
    stream: Option<TcpStream>,
}

impl AgyHttpConn {
    #[must_use]
    pub fn new(port: u16) -> Self {
        Self { port, stream: None }
    }

    fn connect(&mut self, timeout: Duration) -> std::io::Result<()> {
        let addr = SocketAddr::from(([127, 0, 0, 1], self.port));
        let s = TcpStream::connect_timeout(&addr, timeout)?;
        s.set_read_timeout(Some(timeout))?;
        s.set_write_timeout(Some(timeout))?;
        self.stream = Some(s);
        Ok(())
    }

    pub fn close(&mut self) {
        self.stream = None;
    }
}

fn read_http_response(stream: &mut TcpStream) -> std::io::Result<(u16, Vec<u8>, bool)> {
    let mut reader = BufReader::new(stream);
    let mut status_line = String::new();
    let n = reader.read_line(&mut status_line)?;
    if n == 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::UnexpectedEof,
            "EOF reading HTTP status line",
        ));
    }
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "Invalid HTTP status")
        })?;

    let mut content_length: Option<usize> = None;
    let mut is_chunked = false;
    let mut will_close = false;
    loop {
        let mut hline = String::new();
        let hn = reader.read_line(&mut hline)?;
        if hn == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "EOF in HTTP headers",
            ));
        }
        let trimmed = hline.trim_end_matches(['\r', '\n']);
        if trimmed.is_empty() {
            break;
        }
        if let Some((k, v)) = trimmed.split_once(':') {
            let kl = k.trim().to_ascii_lowercase();
            let vl = v.trim();
            if kl == "content-length" {
                content_length = vl.parse::<usize>().ok();
            } else if kl == "transfer-encoding" && vl.to_ascii_lowercase().contains("chunked") {
                is_chunked = true;
            } else if kl == "connection" && vl.eq_ignore_ascii_case("close") {
                will_close = true;
            }
        }
    }

    let mut body = Vec::new();
    if is_chunked {
        loop {
            let mut size_line = String::new();
            let sn = reader.read_line(&mut size_line)?;
            if sn == 0 {
                break;
            }
            let hex_part = size_line.split(';').next().unwrap_or("").trim();
            let chunk_len = usize::from_str_radix(hex_part, 16).map_err(|_| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, "Invalid chunk size")
            })?;
            if chunk_len == 0 {
                loop {
                    let mut tr = String::new();
                    if reader.read_line(&mut tr)? == 0 || tr.trim().is_empty() {
                        break;
                    }
                }
                break;
            }
            let start = body.len();
            body.resize(start + chunk_len, 0);
            reader.read_exact(&mut body[start..])?;
            let mut crlf = [0u8; 2];
            reader.read_exact(&mut crlf)?;
        }
    } else if let Some(len) = content_length {
        body.resize(len, 0);
        reader.read_exact(&mut body)?;
    } else {
        reader.read_to_end(&mut body)?;
        will_close = true;
    }

    Ok((status, body, will_close))
}

fn send_agy_proto_once(
    stream: &mut TcpStream,
    port: u16,
    path: &str,
    body: &[u8],
    csrf_header: (&str, &str),
    keep_alive: bool,
    timeout: Duration,
) -> std::io::Result<(u16, Vec<u8>, bool)> {
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    let conn_val = if keep_alive { "keep-alive" } else { "close" };
    let req_head = format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/proto\r\nConnect-Protocol-Version: 1\r\nConnection: {conn_val}\r\n{}: {}\r\nContent-Length: {}\r\nUser-Agent: codex-agy-sync/{PACKAGE_VERSION}\r\n\r\n",
        csrf_header.0,
        csrf_header.1,
        body.len()
    );
    stream.write_all(req_head.as_bytes())?;
    if !body.is_empty() {
        stream.write_all(body)?;
    }
    stream.flush()?;
    read_http_response(stream)
}

fn post_agy_proto(
    endpoint: &AgyDaemonEndpoint,
    path: &str,
    body: &[u8],
    timeout_s: f64,
    conn: Option<&mut AgyHttpConn>,
) -> Result<(u16, Vec<u8>), HostError> {
    let timeout = Duration::from_secs_f64(timeout_s.max(0.1));
    let csrf = endpoint.csrf_secret.as_loopback_header();
    let Some(pooled) = conn else {
        let addr = SocketAddr::from(([127, 0, 0, 1], endpoint.http_port));
        let mut s = TcpStream::connect_timeout(&addr, timeout).map_err(|e| {
            HostError::OnlineRpcUnavailable(format!(
                "Antigravity {path} RPC connection failed: {e}"
            ))
        })?;
        let (st, data, _) =
            send_agy_proto_once(&mut s, endpoint.http_port, path, body, csrf, false, timeout)
                .map_err(|e| {
                    HostError::OnlineRpcUnavailable(format!(
                        "Antigravity {path} RPC connection failed: {e}"
                    ))
                })?;
        return Ok((st, data));
    };

    let was_reused = pooled.stream.is_some();
    if pooled.stream.is_none() {
        pooled.connect(timeout).map_err(|e| {
            HostError::OnlineRpcUnavailable(format!(
                "Antigravity {path} RPC connection failed: {e}"
            ))
        })?;
    }
    if let Some(ref mut s) = pooled.stream {
        match send_agy_proto_once(s, endpoint.http_port, path, body, csrf, true, timeout) {
            Ok((st, data, will_close)) => {
                if will_close {
                    pooled.close();
                }
                return Ok((st, data));
            }
            Err(err) => {
                pooled.close();
                if !was_reused
                    || matches!(
                        err.kind(),
                        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                    )
                {
                    return Err(HostError::OnlineRpcUnavailable(format!(
                        "Antigravity {path} RPC connection failed: {err}"
                    )));
                }
            }
        }
    }
    pooled.connect(timeout).map_err(|e| {
        HostError::OnlineRpcUnavailable(format!("Antigravity {path} RPC connection failed: {e}"))
    })?;
    let Some(ref mut s) = pooled.stream else {
        return Err(HostError::OnlineRpcUnavailable(
            "Antigravity RPC reconnect failed.".to_owned(),
        ));
    };
    let (st, data, will_close) =
        send_agy_proto_once(s, endpoint.http_port, path, body, csrf, true, timeout).map_err(
            |e| {
                pooled.close();
                HostError::OnlineRpcUnavailable(format!(
                    "Antigravity {path} RPC connection failed: {e}"
                ))
            },
        )?;
    if will_close {
        pooled.close();
    }
    Ok((st, data))
}

fn decode_agy_rpc_step(idx: u32, step_blob: Vec<u8>) -> Result<AgyRawStepRow, HostError> {
    let mut step_type = 0i32;
    let mut status = 0i32;
    let mut has_sub = false;
    let mut meta_blob = Vec::new();
    for (fn_tag, wt, ival, raw) in iter_wire_fields(&step_blob)? {
        if fn_tag == 1 && wt == 0 {
            step_type = ival as i32;
        } else if fn_tag == 4 && wt == 0 {
            status = ival as i32;
        } else if fn_tag == 5 && wt == 2 {
            meta_blob = raw;
        } else if fn_tag == 6 && wt == 2 {
            has_sub = true;
        }
    }
    Ok(AgyRawStepRow {
        idx: StepIndex(idx),
        step_type,
        status,
        has_subtrajectory: has_sub,
        metadata_blob: meta_blob,
        step_payload_blob: step_blob,
        step_format: 1,
    })
}

fn has_agy_trajectory_content(traj_blob: &[u8]) -> Result<bool, HostError> {
    let mut pos = 0usize;
    let n = traj_blob.len();
    while pos < n {
        let tag_start = pos;
        let (tag, next_pos) = read_varint(traj_blob, pos)?;
        pos = next_pos;
        let fn_tag = (tag >> 3) as u32;
        let wt = (tag & 0x07) as u8;
        if fn_tag == 0 {
            return Err(HostError::OnlineRpcUnavailable(format!(
                "Invalid protobuf field number 0 at offset {tag_start}."
            )));
        }
        match wt {
            0 => {
                let (_, after) = read_varint(traj_blob, pos)?;
                pos = after;
            }
            1 | 5 => {
                let w = if wt == 1 { 8 } else { 4 };
                if pos + w > n {
                    return Err(HostError::OnlineRpcUnavailable(
                        "Truncated fixed protobuf field.".to_owned(),
                    ));
                }
                pos += w;
            }
            2 => {
                let (ln_u64, after) = read_varint(traj_blob, pos)?;
                pos = after;
                let ln = usize::try_from(ln_u64).map_err(|_| {
                    HostError::OnlineRpcUnavailable("Length-delimited overflow.".to_owned())
                })?;
                if pos.saturating_add(ln) > n {
                    return Err(HostError::OnlineRpcUnavailable(
                        "Truncated length-delimited protobuf field.".to_owned(),
                    ));
                }
                if fn_tag == 2 || (fn_tag == 1 && ln > 0) {
                    return Ok(true);
                }
                pos += ln;
            }
            other => {
                return Err(HostError::OnlineRpcUnavailable(format!(
                    "Unsupported protobuf wire type {other}."
                )));
            }
        }
    }
    Ok(false)
}

fn decode_agy_trajectory_response(
    resp_bytes: &[u8],
    cascade_id: &CascadeId,
    include_steps: bool,
) -> Result<AgyRpcReadResult, HostError> {
    let mut trajectory_blob = Vec::new();
    let mut run_status = 1i32;
    for (fn_tag, wt, ival, raw) in iter_wire_fields(resp_bytes)? {
        if fn_tag == 1 && wt == 2 {
            trajectory_blob = raw;
        } else if fn_tag == 2 && wt == 0 {
            run_status = ival as i32;
        }
    }
    if trajectory_blob.is_empty() {
        return Err(HostError::FileNotFound(format!(
            "Antigravity cascade {} returned empty trajectory.",
            cascade_id.as_str()
        )));
    }
    if !include_steps {
        if !has_agy_trajectory_content(&trajectory_blob)? {
            return Err(HostError::FileNotFound(format!(
                "Antigravity cascade {} returned empty trajectory.",
                cascade_id.as_str()
            )));
        }
        return Ok(AgyRpcReadResult {
            raw_steps: Vec::new(),
            cascade_run_status: run_status,
            trajectory_metadata_blob: Vec::new(),
            source_enum: 0,
        });
    }

    let mut traj_id_bytes = Vec::new();
    let mut tmeta_blob = Vec::new();
    let mut source_enum = 0i32;
    let mut raw_steps = Vec::new();
    for (fn_tag, wt, ival, raw) in iter_wire_fields(&trajectory_blob)? {
        if fn_tag == 1 && wt == 2 {
            traj_id_bytes = raw;
        } else if fn_tag == 2 && wt == 2 {
            let idx = raw_steps.len() as u32;
            raw_steps.push(decode_agy_rpc_step(idx, raw)?);
        } else if fn_tag == 7 && wt == 2 {
            tmeta_blob = raw;
        } else if fn_tag == 8 && wt == 0 {
            source_enum = ival as i32;
        }
    }
    if raw_steps.is_empty() && traj_id_bytes.is_empty() {
        return Err(HostError::FileNotFound(format!(
            "Antigravity cascade {} returned empty trajectory.",
            cascade_id.as_str()
        )));
    }
    Ok(AgyRpcReadResult {
        raw_steps,
        cascade_run_status: run_status,
        trajectory_metadata_blob: tmeta_blob,
        source_enum,
    })
}

/// Read an Antigravity trajectory lock-free via loopback Connect RPC (`GetCascadeTrajectory`).
///
/// # Errors
/// Returns [`HostError::FileNotFound`] on 404/empty trajectory or [`HostError::OnlineRpcUnavailable`] on transport/decode errors.
pub fn fetch_agy_trajectory_via_rpc(
    endpoint: Option<&AgyDaemonEndpoint>,
    cascade_id: &CascadeId,
    timeout_s: f64,
    include_steps: bool,
    conn: Option<&mut AgyHttpConn>,
) -> Result<AgyRpcReadResult, HostError> {
    validate_session_id(cascade_id.as_str(), "cascade_id")?;
    let Some(ep) = endpoint.filter(|e| e.http_port > 0) else {
        return Err(HostError::OnlineRpcUnavailable(
            "No live Antigravity LanguageServer HTTP endpoint available.".to_owned(),
        ));
    };
    let path = "/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory";
    let body = encode_string_field(1, cascade_id.as_str());
    let (status, resp_bytes) = post_agy_proto(ep, path, &body, timeout_s, conn)?;
    if status != 200 {
        let preview_end = resp_bytes.len().min(4096);
        let err_body = String::from_utf8_lossy(&resp_bytes[..preview_end]).to_ascii_lowercase();
        if status == 404 || err_body.contains("not_found") || err_body.contains("not found") {
            return Err(HostError::FileNotFound(format!(
                "Antigravity cascade {} not found via LanguageServer RPC.",
                cascade_id.as_str()
            )));
        }
        return Err(HostError::OnlineRpcUnavailable(format!(
            "Antigravity GetCascadeTrajectory RPC failed with HTTP {status}."
        )));
    }
    decode_agy_trajectory_response(&resp_bytes, cascade_id, include_steps).map_err(|e| match e {
        HostError::FileNotFound(_) => e,
        _ => HostError::OnlineRpcUnavailable(
            "Invalid protobuf payload from Antigravity GetCascadeTrajectory RPC.".to_owned(),
        ),
    })
}

/// Fetch all cascade run statuses lock-free via loopback Connect RPC (`GetAllCascadeTrajectories`).
///
/// # Errors
/// Returns [`HostError::OnlineRpcUnavailable`] if the daemon is unavailable or returns invalid wire data.
pub fn fetch_agy_run_statuses_via_rpc(
    endpoint: Option<&AgyDaemonEndpoint>,
    timeout_s: f64,
    conn: Option<&mut AgyHttpConn>,
) -> Result<std::collections::HashMap<String, i32>, HostError> {
    let Some(ep) = endpoint.filter(|e| e.http_port > 0) else {
        return Err(HostError::OnlineRpcUnavailable(
            "No live Antigravity LanguageServer HTTP endpoint available.".to_owned(),
        ));
    };
    let path = "/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories";
    let (status, resp_bytes) = post_agy_proto(ep, path, &[], timeout_s, conn)?;
    if status != 200 {
        return Err(HostError::OnlineRpcUnavailable(format!(
            "Antigravity GetAllCascadeTrajectories RPC failed with HTTP {status}."
        )));
    }
    let mut out = std::collections::HashMap::new();
    let fields = iter_wire_fields(&resp_bytes).map_err(|_| {
        HostError::OnlineRpcUnavailable(
            "Invalid protobuf payload from Antigravity GetAllCascadeTrajectories RPC.".to_owned(),
        )
    })?;
    for (fn_tag, wt, _, raw) in fields {
        if fn_tag != 1 || wt != 2 {
            continue;
        }
        let mut cid = String::new();
        let mut summary_raw = Vec::new();
        for (efn, ewt, _, eraw) in iter_wire_fields(&raw).map_err(|_| {
            HostError::OnlineRpcUnavailable(
                "Invalid protobuf payload from Antigravity GetAllCascadeTrajectories RPC."
                    .to_owned(),
            )
        })? {
            if efn == 1 && ewt == 2 {
                cid = String::from_utf8_lossy(&eraw).into_owned();
            } else if efn == 2 && ewt == 2 {
                summary_raw = eraw;
            }
        }
        if !cid.is_empty() {
            let mut st = 0i32;
            for (sfn, swt, sival, _) in iter_wire_fields(&summary_raw).unwrap_or_default() {
                if sfn == 5 && swt == 0 {
                    st = sival as i32;
                }
            }
            out.insert(cid, st);
        }
    }
    Ok(out)
}

fn detect_git_default_branch(ws_path: &Path) -> Option<String> {
    let git_entry = ws_path.join(".git");
    if !git_entry.exists() {
        return None;
    }
    let mut git_dir = if git_entry.is_dir() {
        Some(git_entry.clone())
    } else {
        None
    };
    if git_dir.is_none()
        && git_entry.is_file()
        && let Ok(text) = fs::read_to_string(&git_entry)
        && let Some(first_line) = text.lines().next()
    {
        let trimmed = first_line.trim();
        if trimmed.len() >= 7 && trimmed[..7].eq_ignore_ascii_case("gitdir:") {
            let raw_dir = trimmed[7..].trim();
            let cand = PathBuf::from(raw_dir);
            git_dir = Some(if cand.is_absolute() {
                cand
            } else {
                cas_core::resolve_strict_false(&ws_path.join(cand))
            });
        }
    }
    if let Some(gd) = git_dir {
        let head_file = gd.join("HEAD");
        if head_file.is_file()
            && let Ok(head_text) = fs::read_to_string(&head_file)
            && let Some(branch) = head_text.trim().strip_prefix("ref: refs/heads/")
        {
            let b = branch.trim();
            if !b.is_empty() {
                return Some(b.to_owned());
            }
        }
    }
    Some("main".to_owned())
}

/// Encode an `exa.language_server_pb.CreateProjectRequest` wire payload.
///
/// # Errors
/// Returns [`HostError`] if workspace path normalization fails.
pub fn encode_agy_create_project_request(
    workspace: &str,
    project_id: Option<&str>,
    name: Option<&str>,
    backend: i64,
    is_workspace_only: bool,
) -> Result<Vec<u8>, HostError> {
    let (ws_path, ws_uri) = parse_ws_ident(workspace)?;
    let pid = project_id
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map_or_else(|| Uuid::new_v4().to_string(), str::to_owned);
    let proj_name = name.map(str::to_owned).unwrap_or_else(|| {
        ws_path
            .as_path()
            .file_name()
            .and_then(|n| n.to_str())
            .filter(|s| !s.is_empty())
            .map_or_else(|| ws_path.to_string(), str::to_owned)
    });

    let resource_bytes = if let Some(branch) = detect_git_default_branch(ws_path.as_path()) {
        let mut gf = encode_string_field(1, ws_uri.as_str());
        gf.extend_from_slice(&encode_string_field(2, &branch));
        gf.extend_from_slice(&encode_varint_field(3, 1));
        encode_bytes_field(3, &gf)
    } else {
        encode_bytes_field(1, ws_uri.as_str().as_bytes())
    };
    let resources_bytes = encode_bytes_field(1, &resource_bytes);
    let perm_grants_bytes = encode_varint_field(3, 1);

    let mut project_bytes = encode_string_field(1, &pid);
    project_bytes.extend_from_slice(&encode_string_field(2, &proj_name));
    project_bytes.extend_from_slice(&encode_bytes_field(6, &resources_bytes));
    project_bytes.extend_from_slice(&encode_bytes_field(9, &perm_grants_bytes));
    project_bytes.extend_from_slice(&encode_varint_field(
        12,
        if is_workspace_only { 1 } else { 0 },
    ));
    project_bytes.extend_from_slice(&encode_varint_field(14, backend));

    let mut req = encode_bytes_field(1, &project_bytes);
    req.extend_from_slice(&encode_varint_field(2, backend));
    Ok(req)
}

fn existing_agy_project_names(paths: &HostEnvironmentPaths) -> HashSet<String> {
    let projects_dir = paths.gemini_config_dir.join("projects");
    let mut names = HashSet::new();
    let Ok(rd) = fs::read_dir(&projects_dir) else {
        return names;
    };
    for entry in rd.flatten() {
        let p = entry.path();
        if p.is_file()
            && p.extension().and_then(|e| e.to_str()) == Some("json")
            && let Ok(text) = fs::read_to_string(&p)
            && let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text)
            && let Some(name) = obj.get("name").and_then(Value::as_str).map(str::trim)
            && !name.is_empty()
        {
            names.insert(name.to_owned());
        }
    }
    names
}

fn agy_project_name_candidates(
    ws_path: &WorkspacePath,
    project_id: &str,
    existing_names: &HashSet<String>,
) -> Vec<String> {
    let base_name = ws_path
        .as_path()
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|s| !s.is_empty())
        .map_or_else(|| ws_path.to_string(), str::to_owned);
    let parent_name = ws_path
        .as_path()
        .parent()
        .and_then(|p| p.file_name())
        .and_then(|n| n.to_str())
        .map(str::trim)
        .unwrap_or("");
    let prefix = &project_id[..project_id.len().min(8)];
    let fallback = format!("{base_name} ({prefix})");

    let mut raw = vec![base_name.clone()];
    if !parent_name.is_empty() {
        raw.push(format!("{base_name} ({parent_name})"));
    }
    raw.push(fallback.clone());

    let mut deduped = Vec::new();
    for item in raw {
        if !deduped.contains(&item) {
            deduped.push(item);
        }
    }
    deduped
        .into_iter()
        .filter(|cand| cand == &fallback || !existing_names.contains(cand))
        .collect()
}

/// Register `workspace` in live Antigravity LanguageServer via `CreateProject` RPC.
///
/// # Errors
/// Returns [`HostError::OnlineRpcUnavailable`] if no live daemon is reachable or registration fails.
pub fn register_agy_project_via_rpc<F>(
    paths: &HostEnvironmentPaths,
    workspace: &str,
    lookup_fn: F,
    timeout_s: f64,
) -> Result<String, HostError>
where
    F: Fn(&WorkspaceRootUri) -> Option<AgyProjectId>,
{
    let (ws_path, ws_uri) = parse_ws_ident(workspace)?;
    let endpoint = PresenceWalGuard::new(paths.clone())
        .discover_agy_daemon()
        .filter(|e| e.http_port > 0)
        .ok_or_else(|| {
            HostError::OnlineRpcUnavailable(
                "No live Antigravity LanguageServer HTTP endpoint available.".to_owned(),
            )
        })?;
    if let Some(existing) = lookup_fn(&ws_uri) {
        return Ok(existing.as_str().to_owned());
    }
    let project_id = Uuid::new_v4().to_string();
    let existing_names = existing_agy_project_names(paths);
    let candidates = agy_project_name_candidates(&ws_path, &project_id, &existing_names);
    for candidate in candidates {
        let payload = encode_agy_create_project_request(
            &ws_path.to_string(),
            Some(&project_id),
            Some(&candidate),
            1,
            false,
        )?;
        let (status, _) = post_agy_proto(
            &endpoint,
            "/exa.language_server_pb.LanguageServerService/CreateProject",
            &payload,
            timeout_s,
            None,
        )?;
        if status == 200 {
            let wait_secs = timeout_s.clamp(0.0, 0.25);
            let deadline = Instant::now() + Duration::from_secs_f64(wait_secs);
            loop {
                if let Some(found) = lookup_fn(&ws_uri) {
                    return Ok(found.as_str().to_owned());
                }
                if Instant::now() >= deadline {
                    return Ok(project_id);
                }
                thread::sleep(Duration::from_millis(20));
            }
        }
        if status != 409 {
            return Err(HostError::OnlineRpcUnavailable(format!(
                "Antigravity CreateProject RPC failed with HTTP {status}."
            )));
        }
        if let Some(concurrent) = lookup_fn(&ws_uri) {
            return Ok(concurrent.as_str().to_owned());
        }
    }
    Err(HostError::OnlineRpcUnavailable(
        "Antigravity CreateProject RPC failed with HTTP 409.".to_owned(),
    ))
}
