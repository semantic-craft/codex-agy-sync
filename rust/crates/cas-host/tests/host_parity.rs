#![allow(clippy::expect_used)]

use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;

use cas_codecs::{encode_bytes_field, encode_string_field, encode_varint_field, iter_wire_fields};
use cas_core::{
    AgyGuiSafeSource, CanonicalEvent, CascadeId, CodexThreadId, EventOriginAnchor, HostKind,
    PairingPolicy, StepIndex, SyncDirection, SyncReceipt, TrajectoryLedger, TransportMode, TurnId,
    WorkspaceRootUri,
};
use cas_host::{
    DefaultHostTransport, HostEnvironmentPaths, HostError, MenubarCandidateState, PresenceWalGuard,
    ProjectRegistry, build_menubar_feed, diagnose_workspace_binding, execute_handoff,
    handoff_session, inspect_session_ledger, is_pid_alive, open_locked_rw_sqlite,
    open_readonly_sqlite, pair_workspace_with_mode, parse_ws_ident, reconcile_workspace_handoffs,
    resolve_handoff_request, summarize_workspace_handoffs,
};
use serde_json::json;
use tempfile::TempDir;

static TEST_SEQ: AtomicUsize = AtomicUsize::new(1);

fn make_sandbox() -> TempDir {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let base = home
        .join("Projects")
        .join("_artifacts")
        .join("codex-agy-sync")
        .join("rust-host-tests");
    fs::create_dir_all(&base).expect("mkdir artifacts");
    let seq = TEST_SEQ.fetch_add(1, Ordering::Relaxed);
    let prefix = format!("host-test-{}-{seq}-", std::process::id());
    tempfile::Builder::new()
        .prefix(&prefix)
        .tempdir_in(&base)
        .expect("tempdir in artifacts")
}

fn make_sandbox_paths(root: &Path) -> (HostEnvironmentPaths, PathBuf) {
    let ws = root.join("workspace");
    fs::create_dir_all(&ws).expect("mkdir ws");
    let mut env_map = BTreeMap::new();
    env_map.insert("HOME".to_owned(), root.display().to_string());
    env_map.insert(
        "GEMINI_CONFIG_DIR".to_owned(),
        root.join("gemini-config").display().to_string(),
    );
    env_map.insert(
        "ANTIGRAVITY_DATA_DIR".to_owned(),
        root.join("agy-data").display().to_string(),
    );
    env_map.insert(
        "CODEX_HOME".to_owned(),
        root.join("codex-home").display().to_string(),
    );
    env_map.insert(
        "CODEX_AGY_SYNC_STATE_DIR".to_owned(),
        root.join("sync-state").display().to_string(),
    );
    let paths = HostEnvironmentPaths::discover(Some(&env_map));
    fs::create_dir_all(&paths.gemini_config_dir).expect("mkdir gemini");
    fs::create_dir_all(&paths.agy_data_dir).expect("mkdir agy");
    fs::create_dir_all(&paths.codex_home_dir).expect("mkdir codex");
    fs::create_dir_all(&paths.sync_state_dir).expect("mkdir sync");
    (paths, ws)
}

fn sample_ledger(ws: &Path, interrupted: bool) -> TrajectoryLedger {
    let (wp, wu) = parse_ws_ident(&ws.display().to_string()).expect("ws");
    let t1 = TurnId::parse("turn-1").expect("t1");
    let mut events = vec![
        CanonicalEvent::UserInput {
            anchor: EventOriginAnchor::new(HostKind::Codex, t1.clone(), "2026-04-01T10:00:00Z"),
            query_text: "Implement Wave 1 parity".to_owned(),
            scope_items: Vec::new(),
            media: Vec::new(),
        },
        CanonicalEvent::AssistantText {
            anchor: EventOriginAnchor::new(HostKind::Codex, t1, "2026-04-01T10:00:05Z"),
            text: "Implemented Wave 1 parity.".to_owned(),
            message_id: None,
            stop_reason: Some("end_turn".to_owned()),
            generator_model: None,
            token_usage: Default::default(),
        },
    ];
    if interrupted {
        let t2 = TurnId::parse("turn-2").expect("t2");
        events.push(CanonicalEvent::UserInput {
            anchor: EventOriginAnchor::new(HostKind::Codex, t2, "2026-04-01T10:05:00Z"),
            query_text: "Second interrupted turn".to_owned(),
            scope_items: Vec::new(),
            media: Vec::new(),
        });
    }
    TrajectoryLedger::from_events(wp, wu, &events, Some("Wave 1 Session"), false)
}

#[test]
fn readonly_sqlite_blocks_mutations_and_pid_alive_checks_self() {
    let tmp = make_sandbox();
    let db = tmp.path().join("ro.sqlite");
    {
        let rw = open_locked_rw_sqlite(&db).expect("rw");
        rw.execute("CREATE TABLE t (x INT)", []).expect("create");
    }
    let ro = open_readonly_sqlite(&db).expect("ro");
    assert!(ro.execute("INSERT INTO t VALUES (1)", []).is_err());
    assert!(is_pid_alive(std::process::id()));
    assert!(!is_pid_alive(999_999));
}

#[test]
fn pair_apply_inspect_handoff_and_reconcile_offline_roundtrip() {
    let tmp = make_sandbox();
    let (paths, ws) = make_sandbox_paths(tmp.path());
    let ws_str = ws.display().to_string();

    let diag_before = diagnose_workspace_binding(&paths, &ws_str).expect("diag");
    assert!(!diag_before.is_paired());

    let guard = PresenceWalGuard::new(paths.clone());
    let registry = ProjectRegistry::new(paths.clone());
    let (binding, mode) =
        pair_workspace_with_mode(&paths, &guard, &registry, &ws_str, false).expect("pair");
    assert_eq!(mode, "offline_locked");
    assert!(
        diagnose_workspace_binding(&paths, &ws_str)
            .expect("diag2")
            .is_paired()
    );

    let transport = DefaultHostTransport::new(paths.clone());
    let ledger = sample_ledger(&ws, false);
    let locks = guard
        .probe_locks(None, None, false, false, false)
        .expect("locks");
    let tid = transport
        .apply_to_codex(
            &binding,
            None,
            &ledger.events,
            &ledger,
            &locks,
            TransportMode::OfflineStorage,
            None,
            false,
            None,
        )
        .expect("apply codex");

    let insp = inspect_session_ledger(
        &paths,
        &ws_str,
        None,
        Some(tid.as_str()),
        TransportMode::OfflineStorage,
    )
    .expect("inspect");
    assert_eq!(insp.canonical_title, "Wave 1 Session");
    assert_eq!(insp.completed_turns, 1);
    assert!(!insp.has_inflight_turn);

    let req = resolve_handoff_request(
        &paths,
        &ws_str,
        None,
        Some(tid.as_str()),
        None,
        false,
        false,
        false,
        PairingPolicy::RequirePaired,
        false,
    )
    .expect("resolve");
    // Without target daemon, handoff_session requires live target Desktop app (OnlineRpcUnavailable).
    let offline_err = execute_handoff(&paths, &req, false);
    assert!(
        matches!(offline_err, Err(HostError::OnlineRpcUnavailable(_))),
        "expected OnlineRpcUnavailable, got {offline_err:?}"
    );

    // Start live mock Antigravity HTTP Connect-RPC server reading published conversations/<cid>.db.
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind tcp");
    let port = listener.local_addr().expect("addr").port();
    let daemon_dir = paths.agy_data_dir.join("daemon");
    fs::create_dir_all(&daemon_dir).expect("daemon dir");
    let hub_file = daemon_dir.join(format!("hub_{port}.json"));
    fs::write(
        &hub_file,
        json!({
            "pid": std::process::id(),
            "httpPort": port,
            "httpsPort": 0,
            "lsVersion": "2.30.0",
            "csrfToken": "ephemeral-test-token"
        })
        .to_string(),
    )
    .expect("hub json");

    let agy_dir_srv = paths.agy_data_dir.clone();
    let srv_handle = thread::spawn(move || {
        for conn in listener.incoming().take(2) {
            let Ok(mut stream) = conn else { break };
            let mut reader = BufReader::new(stream.try_clone().expect("clone"));
            let mut req_line = String::new();
            if reader.read_line(&mut req_line).unwrap_or(0) == 0 {
                continue;
            }
            let mut content_len = 0usize;
            loop {
                let mut h = String::new();
                if reader.read_line(&mut h).unwrap_or(0) == 0 {
                    break;
                }
                let t = h.trim();
                if t.is_empty() {
                    break;
                }
                if let Some((k, v)) = t.split_once(':')
                    && k.trim().eq_ignore_ascii_case("content-length")
                {
                    content_len = v.trim().parse().unwrap_or(0);
                }
            }
            let mut body = vec![0u8; content_len];
            let _ = reader.read_exact(&mut body);
            let mut cid_req = String::new();
            for (fn_tag, wt, _, raw) in iter_wire_fields(&body).unwrap_or_default() {
                if fn_tag == 1 && wt == 2 {
                    cid_req = String::from_utf8_lossy(&raw).into_owned();
                }
            }
            let conv_db = agy_dir_srv
                .join("conversations")
                .join(format!("{cid_req}.db"));
            let mut traj = encode_string_field(1, "traj-live");
            if let Ok(c) = open_readonly_sqlite(&conv_db)
                && let Ok(mut stmt) = c.prepare("SELECT step_payload FROM steps ORDER BY idx ASC")
                && let Ok(mut rows) = stmt.query([])
            {
                while let Ok(Some(row)) = rows.next() {
                    if let Ok(blob) = row.get::<_, Vec<u8>>(0) {
                        traj.extend_from_slice(&encode_bytes_field(2, &blob));
                    }
                }
            }
            let mut resp = encode_bytes_field(1, &traj);
            resp.extend_from_slice(&encode_varint_field(2, 1));
            let hdr = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/proto\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                resp.len()
            );
            let _ = stream.write_all(hdr.as_bytes());
            let _ = stream.write_all(&resp);
            if req_line.contains("GetCascadeTrajectory") && conv_db.is_file() {
                break;
            }
        }
    });

    let receipt = execute_handoff(&paths, &req, false).expect("handoff");
    let _ = srv_handle.join();
    let _ = fs::remove_file(&hub_file);
    assert_eq!(receipt.status_str(), "ready");
    assert_eq!(receipt.target_host, "antigravity");
    let target_cid = receipt.target_id().expect("target cid").to_owned();

    // Re-running same source must yield idempotent no_op.
    let receipt2 = handoff_session(
        &paths,
        &ws_str,
        SyncDirection::CodexToAgy,
        tid.as_str(),
        None,
        false,
        None,
        None,
        0,
        None,
    )
    .expect("noop");
    assert_eq!(receipt2.status_str(), "no_op");
    assert_eq!(receipt2.target_id(), Some(target_cid.as_str()));

    // Menubar feed: source is already_handed, untouched target is suppressed!
    let feed = build_menubar_feed(
        &paths,
        6,
        std::slice::from_ref(&ws_str),
        Some(2_000_000_000.0),
    )
    .expect("feed");
    assert!(feed.hero.is_none());
    assert_eq!(feed.recent.len(), 1);
    assert_eq!(feed.recent[0].state, MenubarCandidateState::AlreadyHanded);
    assert_eq!(
        feed.recent[0].handed_target_id.as_deref(),
        Some(target_cid.as_str())
    );

    // Status summary includes chain.
    let summary = summarize_workspace_handoffs(&paths, &ws_str).expect("summary");
    assert_eq!(summary.handoff_chains.len(), 1);
    assert!(summary.pending_handoffs.is_empty());

    // Simulate a pending receipt whose target absent -> reconcile clears it.
    let pending_path = paths
        .sync_state_dir
        .join("handoffs")
        .join("manual-pending.json");
    let pending_json = json!({
        "workspace": ws_str,
        "source_host": "codex",
        "source_id": tid.as_str(),
        "source_digest": "deadbeef",
        "target_host": "antigravity",
        "target_id": "11111111-1111-4111-8111-111111111111",
        "title": "Pending Check",
        "status": "pending",
        "staging_path": paths.sync_state_dir.join("handoffs/staging/nonexistent").display().to_string(),
        "runtime_state_not_transferred": true,
        "error": "simulated crash",
        "created_at_iso": "2026-04-01T10:10:00Z",
        "chain_depth": 0
    });
    fs::write(&pending_path, pending_json.to_string()).expect("write pending");
    let _: SyncReceipt = serde_json::from_value(pending_json).expect("valid receipt");

    let dry = reconcile_workspace_handoffs(&paths, &ws_str, None, true).expect("dry");
    assert_eq!(dry.total_pending_before, 1);
    assert_eq!(dry.cleared.len(), 1);
    assert_eq!(dry.total_pending_after, 1);

    let applied = reconcile_workspace_handoffs(&paths, &ws_str, None, false).expect("reconcile");
    assert_eq!(applied.cleared.len(), 1);
    assert_eq!(applied.total_pending_after, 0);
    assert!(!pending_path.exists());

    let _ = (
        AgyGuiSafeSource::CascadeClient,
        CascadeId::parse,
        StepIndex(0),
    );
    let _ = WorkspaceRootUri::parse;
}

#[test]
fn interrupted_session_promoted_as_hero_within_window() {
    let tmp = make_sandbox();
    let (paths, ws) = make_sandbox_paths(tmp.path());
    let ws_str = ws.display().to_string();
    let guard = PresenceWalGuard::new(paths.clone());
    let registry = ProjectRegistry::new(paths.clone());
    let (binding, _) =
        pair_workspace_with_mode(&paths, &guard, &registry, &ws_str, false).expect("pair");
    let transport = DefaultHostTransport::new(paths.clone());
    let locks = guard
        .probe_locks(None, None, false, false, false)
        .expect("locks");

    let interrupted_ledger = sample_ledger(&ws, true);
    let tid = transport
        .apply_to_codex(
            &binding,
            None,
            &interrupted_ledger.events,
            &interrupted_ledger,
            &locks,
            TransportMode::OfflineStorage,
            None,
            false,
            None,
        )
        .expect("apply interrupted");

    let feed = build_menubar_feed(&paths, 6, std::slice::from_ref(&ws_str), None).expect("feed");
    let hero = feed
        .hero
        .clone()
        .unwrap_or_else(|| panic!("hero missing: {feed:?}"));
    assert_eq!(hero.session_id, tid.as_str());
    assert_eq!(hero.state, MenubarCandidateState::InterruptedStopped);
    assert!(hero.has_inflight_turn);
}

#[test]
fn loopback_agy_http_and_codex_unix_ws_rpc_clients_work() {
    let tmp = make_sandbox();
    let (paths, ws) = make_sandbox_paths(tmp.path());
    let ws_str = ws.display().to_string();
    let registry = ProjectRegistry::new(paths.clone());
    let binding = registry.resolve_readonly_binding(&ws_str).expect("ro");

    // 1) Mock Antigravity HTTP Connect-RPC server
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind tcp");
    let port = listener.local_addr().expect("addr").port();
    let cid_str = "22222222-2222-4222-8222-222222222222";
    let daemon_dir = paths.agy_data_dir.join("daemon");
    fs::create_dir_all(&daemon_dir).expect("daemon dir");
    fs::write(
        daemon_dir.join(format!("hub_{port}.json")),
        json!({
            "pid": std::process::id(),
            "httpPort": port,
            "httpsPort": 0,
            "lsVersion": "2.30.0",
            "csrfToken": "ephemeral-test-token"
        })
        .to_string(),
    )
    .expect("hub json");

    let step_payload = {
        let mut user_in = encode_string_field(1, "Hello via Agy RPC");
        user_in.extend_from_slice(&encode_string_field(2, "Hello via Agy RPC"));
        let mut st = encode_varint_field(1, 2); // USER_INPUT = 2
        st.extend_from_slice(&encode_varint_field(4, 3)); // DONE = 3
        st.extend_from_slice(&encode_bytes_field(8, &user_in));
        st
    };
    let mut traj = encode_string_field(1, "traj-1");
    traj.extend_from_slice(&encode_bytes_field(2, &step_payload));
    let mut get_traj_resp = encode_bytes_field(1, &traj);
    get_traj_resp.extend_from_slice(&encode_varint_field(2, 1)); // IDLE = 1

    let server_handle = thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            let mut reader = BufReader::new(stream.try_clone().expect("clone"));
            let mut req_line = String::new();
            let _ = reader.read_line(&mut req_line);
            let mut content_len = 0usize;
            loop {
                let mut h = String::new();
                if reader.read_line(&mut h).unwrap_or(0) == 0 {
                    break;
                }
                let t = h.trim();
                if t.is_empty() {
                    break;
                }
                if let Some((k, v)) = t.split_once(':')
                    && k.trim().eq_ignore_ascii_case("content-length")
                {
                    content_len = v.trim().parse().unwrap_or(0);
                }
            }
            let mut body = vec![0u8; content_len];
            let _ = reader.read_exact(&mut body);
            let hdr = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/proto\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                get_traj_resp.len()
            );
            let _ = stream.write_all(hdr.as_bytes());
            let _ = stream.write_all(&get_traj_resp);
        }
    });

    let guard = PresenceWalGuard::new(paths.clone());
    let cid = CascadeId::parse(cid_str).expect("cid");
    let locks = guard
        .probe_locks(Some(&cid), None, false, false, false)
        .expect("locks");
    let transport = DefaultHostTransport::new(paths.clone());
    let (agy_ledger, used_mode, active) = transport
        .read_agy_ledger_with_meta(&binding, &cid, &locks, TransportMode::OnlineRpc)
        .expect("agy rpc");
    assert_eq!(used_mode, TransportMode::OnlineRpc);
    assert!(!active);
    assert_eq!(agy_ledger.events.len(), 1);
    let _ = server_handle.join();

    // 2) Mock Codex AF_UNIX WebSocket server (keep socket path under macOS 104-byte SUN_LEN)
    let short_sock_dir = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Projects")
        .join("_artifacts")
        .join("codex-agy-sync")
        .join("rt");
    fs::create_dir_all(&short_sock_dir).expect("mkdir rt");
    let sock_path = short_sock_dir.join(format!("c-{}.sock", std::process::id()));
    let _ = fs::remove_file(&sock_path);
    let ulistener = UnixListener::bind(&sock_path).expect("bind unix");
    let tid_str = "0194b456-1111-7111-8111-111111111111";
    let tid_copy = tid_str.to_owned();
    let ws_srv = thread::spawn(move || {
        for conn in ulistener.incoming() {
            let Ok(stream) = conn else { break };
            let Ok(mut ws) = tungstenite::accept(stream) else {
                continue;
            };
            let mut served = false;
            while let Ok(msg) = ws.read() {
                if let tungstenite::Message::Text(txt) = msg
                    && let Ok(val) = serde_json::from_str::<serde_json::Value>(&txt)
                {
                    let id = val.get("id").and_then(|v| v.as_i64());
                    let method = val.get("method").and_then(|v| v.as_str()).unwrap_or("");
                    if method == "initialize"
                        && let Some(rid) = id
                    {
                        let _ = ws.send(tungstenite::Message::Text(
                            json!({"id": rid, "result": {}}).to_string().into(),
                        ));
                    } else if method == "thread/read"
                        && let Some(rid) = id
                    {
                        let reply = json!({
                            "id": rid,
                            "result": {
                                "thread": {
                                    "id": tid_copy,
                                    "name": "RPC Thread",
                                    "status": {"type": "idle"},
                                    "turns": [{
                                        "id": "t1",
                                        "status": "completed",
                                        "items": [
                                            {"type": "userMessage", "content": [{"type": "text", "text": "Hi Codex"}]},
                                            {"type": "agentMessage", "text": "Hello from Codex WS"}
                                        ]
                                    }]
                                }
                            }
                        });
                        let _ = ws.send(tungstenite::Message::Text(reply.to_string().into()));
                        served = true;
                        break;
                    }
                }
            }
            if served {
                break;
            }
        }
    });

    let mut paths_ws = paths;
    paths_ws.codex_app_server_socket = Some(sock_path.clone());
    let tid = CodexThreadId::parse(tid_str).expect("tid");
    let cguard = PresenceWalGuard::new(paths_ws.clone());
    let clocks = cguard
        .probe_locks(None, Some(&tid), false, false, false)
        .expect("clocks");
    let ctransport = DefaultHostTransport::new(paths_ws);
    let (codex_ledger, cmode, cactive) = ctransport
        .read_codex_ledger_with_meta(&binding, &tid, &clocks, TransportMode::OnlineRpc)
        .expect("codex rpc");
    assert_eq!(cmode, TransportMode::OnlineRpc);
    assert!(!cactive);
    assert_eq!(codex_ledger.completed_turn_watermarks.len(), 1);
    let _ = ws_srv.join();
    let _ = fs::remove_file(&sock_path);
}
