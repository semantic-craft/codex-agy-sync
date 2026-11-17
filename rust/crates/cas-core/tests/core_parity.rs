use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use cas_core::{
    AgyGuiSafeSource, CanonicalEvent, CanonicalToolPayload, CascadeId, CodexThreadId, CoreError,
    CsrfSecret, CursorForkStateMachine, CursorTransition, EventOriginAnchor, FileEditOp,
    ForkResolutionPolicy, HostKind, HostLockSnapshot, PendingReceiptErrorData, ReceiptEntry,
    ReceiptState, RolloutLineNo, SessionId, SessionSyncCursor, StepIndex, SyncDirection,
    SyncReceipt, ToolCallId, TrajectoryLedger, TransportMode, TurnId, dump_canonical_json,
    ledger_seed_fingerprint, normalize_tool_file_path, parse_workspace_identity,
    rescue_interrupted_turn, resolve_canonical_title, resolve_chain_tip, validate_session_id,
};

struct TempArtifactsDir {
    path: PathBuf,
}

impl TempArtifactsDir {
    fn new(prefix: &str) -> Self {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        let root = home
            .join("Projects")
            .join("_artifacts")
            .join("codex-agy-sync")
            .join(format!("{prefix}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        if let Err(err) = fs::create_dir_all(&root) {
            panic!(
                "failed to create test artifacts dir {}: {err}",
                root.display()
            );
        }
        Self { path: root }
    }
}

impl Drop for TempArtifactsDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

#[test]
fn id_validation_and_csrf_redaction() {
    assert_eq!(
        validate_session_id("abc-123_XYZ", "session_id").unwrap(),
        "abc-123_XYZ"
    );
    for bad in [
        "",
        "   ",
        "../evil",
        "a/b",
        "a\\b",
        " leading",
        "trailing ",
        "a\0b",
    ] {
        assert!(
            validate_session_id(bad, "session_id").is_err(),
            "expected error for {bad:?}"
        );
    }
    assert!(SessionId::parse("valid-session-1").is_ok());
    assert!(CascadeId::parse("../bad").is_err());
    assert!(CodexThreadId::parse("thread-01").is_ok());

    let secret = CsrfSecret::try_new("tok-very-secret-12345").unwrap();
    let dbg = format!("{secret:?}");
    let disp = format!("{secret}");
    assert!(!dbg.contains("tok-very-secret-12345"));
    assert!(!disp.contains("tok-very-secret-12345"));
    assert!(dbg.contains("REDACTED"));
    assert!(disp.contains("REDACTED"));
    assert_eq!(
        secret.as_loopback_header(),
        ("x-codeium-csrf-token", "tok-very-secret-12345")
    );
    assert!(matches!(
        CsrfSecret::try_new("   "),
        Err(CoreError::EmptyCsrfToken)
    ));
}

#[test]
fn gui_safe_source_excludes_cli_source_17() {
    assert!(AgyGuiSafeSource::from_proto_enum(17).is_none());
    assert_eq!(AgyGuiSafeSource::CascadeClient.proto_enum_value(), 1);
    assert_eq!(
        AgyGuiSafeSource::CascadeClient.summary_column_name(),
        "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT"
    );
    assert_eq!(AgyGuiSafeSource::Jetbox.proto_enum_value(), 18);
    assert_eq!(
        AgyGuiSafeSource::Jetbox.summary_column_name(),
        "CORTEX_TRAJECTORY_SOURCE_JETBOX"
    );
    assert_eq!(AgyGuiSafeSource::AgentApi.proto_enum_value(), 19);
    assert_eq!(AgyGuiSafeSource::Cider.proto_enum_value(), 20);
}

#[test]
fn parse_workspace_identity_normalizes_paths_uris_and_symlinks() {
    let sandbox = TempArtifactsDir::new("cas-core-ws");
    let ws = sandbox.path.join("sample-workspace");
    fs::create_dir_all(&ws).unwrap();
    let link = sandbox.path.join("ws-link");
    std::os::unix::fs::symlink(&ws, &link).unwrap();

    let (p1, u1) = parse_workspace_identity(&format!("{}///", ws.display())).unwrap();
    let uri = format!("file://{}/", p1.as_path().display());
    let (p2, u2) = parse_workspace_identity(&uri).unwrap();
    let (p3, u3) = parse_workspace_identity(link.to_str().unwrap()).unwrap();

    assert_eq!(p1, p2);
    assert_eq!(u1, u2);
    assert_eq!(p1, p3);
    assert_eq!(u1, u3);
    assert!(!u1.as_str().ends_with('/'));
    assert!(u1.as_str().starts_with("file:///"));

    for invalid in ["", "   ", "/", "file://", "http://example.com/repo", "."] {
        assert!(
            parse_workspace_identity(invalid).is_err(),
            "expected invalid for {invalid:?}"
        );
    }
}

#[test]
fn normalize_tool_file_path_percent_decodes_and_blocks_control_injection() {
    assert_eq!(
        normalize_tool_file_path("file:///Users/test/My%20Project/a%23b%25c.py"),
        "/Users/test/My Project/a#b%c.py"
    );
    assert_eq!(
        normalize_tool_file_path("`file:///Users/test/dir%20name/file.py` (lines 1-20)"),
        "/Users/test/dir name/file.py"
    );
    assert_eq!(
        normalize_tool_file_path("file://localhost/sandbox/space%20file.txt"),
        "/sandbox/space file.txt"
    );
    assert_eq!(
        normalize_tool_file_path("file:///C:/Users/test%20user/main.rs"),
        "C:/Users/test user/main.rs"
    );
    assert_eq!(
        normalize_tool_file_path("/workspace/100%_coverage/50%off.py"),
        "/workspace/100%_coverage/50%off.py"
    );
    assert_eq!(
        normalize_tool_file_path("src/100%_coverage.py"),
        "src/100%_coverage.py"
    );
    assert_eq!(
        normalize_tool_file_path("[app.py](file:///Users/alice/My Projects/app.py)"),
        "/Users/alice/My Projects/app.py"
    );
    let injected = normalize_tool_file_path("file:///repo/a.py%0A***%20Delete%20File:%20b.py");
    assert!(!injected.contains('\n'));
    assert!(!injected.contains('\r'));
    assert!(!injected.contains('\0'));
    assert_eq!(injected, "/repo/a.py*** Delete File: b.py");
}

#[test]
fn resolve_canonical_title_precedence_and_ledger_watermarks() {
    let (ws_path, ws_uri) = parse_workspace_identity("/workspace/sample").unwrap();
    let t1 = TurnId::parse("turn-1").unwrap();
    let t2 = TurnId::parse("turn-2").unwrap();
    let a1 = EventOriginAnchor::new(HostKind::Antigravity, t1.clone(), "2026-11-10T01:00:00Z");
    let mut a2 = EventOriginAnchor::new(HostKind::Antigravity, t2.clone(), "2026-11-10T01:01:00Z");
    a2.agy_step_index = Some(StepIndex(3));

    let events = vec![
        CanonicalEvent::UserInput {
            anchor: EventOriginAnchor {
                agy_step_index: Some(StepIndex(0)),
                ..a1.clone()
            },
            query_text: "<USER_REQUEST>\nFirst line\nSecond line\n</USER_REQUEST>".into(),
            scope_items: vec![],
            media: vec![],
        },
        CanonicalEvent::AssistantText {
            anchor: EventOriginAnchor {
                agy_step_index: Some(StepIndex(1)),
                ..a1.clone()
            },
            text: "Done.".into(),
            message_id: None,
            stop_reason: Some("stop".into()),
            generator_model: None,
            token_usage: Default::default(),
        },
        CanonicalEvent::Checkpoint {
            anchor: EventOriginAnchor {
                agy_step_index: Some(StepIndex(2)),
                ..a1
            },
            checkpoint_index: 1,
            user_intent: "intent".into(),
            session_summary: "summary".into(),
            code_change_summary: String::new(),
            conversation_title: Some("Latest Checkpoint Title".into()),
        },
        CanonicalEvent::UserInput {
            anchor: a2,
            query_text: "Inflight question".into(),
            scope_items: vec![],
            media: vec![],
        },
    ];

    assert_eq!(
        resolve_canonical_title(&events, None, None, None, Some("Cascade Fallback")),
        "Cascade Fallback"
    );
    assert_eq!(
        resolve_canonical_title(&events, None, None, None, None),
        "Latest Checkpoint Title"
    );

    let ledger = TrajectoryLedger::from_events(ws_path, ws_uri, &events, None, false);
    assert_eq!(ledger.completed_turn_watermarks.len(), 1);
    assert!(ledger.has_inflight_turn);
    let wm = ledger.latest_watermark().unwrap();
    assert_eq!(wm.turn_id, t1);
    assert_eq!(wm.last_agy_step_index, StepIndex(2));
    assert!(!wm.boundary_digest.as_str().is_empty());

    let (rescued, changed) = rescue_interrupted_turn(&ledger, None);
    assert!(changed);
    assert!(!rescued.has_inflight_turn);
    assert_eq!(rescued.completed_turn_watermarks.len(), 2);
    assert_eq!(rescued.completed_turn_watermarks[1].turn_id, t2);
}

#[test]
fn canonical_json_and_tool_payload_deterministic() {
    let payload = CanonicalToolPayload::FileEdit {
        path: "/workspace/foo.py".into(),
        edit_op: FileEditOp::Modify,
        unified_diff: "@@ -1 +1 @@".into(),
        diff_stats: (1, 1),
        is_artifact: false,
        artifact_summary: None,
    };
    let map: BTreeMap<String, serde_json::Value> =
        serde_json::from_value(serde_json::to_value(&payload).unwrap()).unwrap();
    let s1 = dump_canonical_json(&map);
    let s2 = dump_canonical_json(&map);
    assert_eq!(s1, s2);
    assert!(s1.contains("\"edit_op\":\"MODIFY\""));
}

#[test]
fn state_machine_initial_append_divergence_and_busy_lock() {
    let (ws_path, ws_uri) = parse_workspace_identity("/workspace/sample").unwrap();
    let t1 = TurnId::parse("turn-1").unwrap();
    let mut a1 = EventOriginAnchor::new(HostKind::Codex, t1.clone(), "2026-11-10T01:00:00Z");
    a1.codex_line_no = Some(RolloutLineNo(1));

    let codex_events = vec![
        CanonicalEvent::UserInput {
            anchor: a1.clone(),
            query_text: "Hello".into(),
            scope_items: vec![],
            media: vec![],
        },
        CanonicalEvent::ToolCall {
            anchor: EventOriginAnchor {
                codex_line_no: Some(RolloutLineNo(2)),
                ..a1.clone()
            },
            call_id: ToolCallId::parse("call-1").unwrap(),
            tool_name: "read_file".into(),
            tool_summary: String::new(),
            payload: CanonicalToolPayload::ReadFile {
                path: "/workspace/a.txt".into(),
                start_line: Some(1),
                end_line: Some(5),
                byte_offset: None,
                file_num_lines: None,
                file_num_bytes: None,
                content_excerpt: None,
            },
            thinking_signature: None,
        },
        CanonicalEvent::ToolResult {
            anchor: EventOriginAnchor {
                codex_line_no: Some(RolloutLineNo(3)),
                ..a1.clone()
            },
            call_id: ToolCallId::parse("call-1").unwrap(),
            tool_name: "read_file".into(),
            output_text: "ok".into(),
            is_error: false,
            payload: CanonicalToolPayload::Opaque {
                tool_name: "read_file".into(),
                raw_arguments_json: "{}".into(),
                type_url: None,
                raw_any_bytes: None,
            },
            full_output_uri: None,
        },
        CanonicalEvent::AssistantText {
            anchor: EventOriginAnchor {
                codex_line_no: Some(RolloutLineNo(4)),
                ..a1
            },
            text: "World".into(),
            message_id: None,
            stop_reason: Some("stop".into()),
            generator_model: None,
            token_usage: Default::default(),
        },
    ];
    let codex_ledger =
        TrajectoryLedger::from_events(ws_path, ws_uri.clone(), &codex_events, None, false);

    // Busy lock defers safely
    let busy_locks = HostLockSnapshot {
        agy_presence_locked: true,
        ..HostLockSnapshot::default()
    };
    let tr_busy = CursorForkStateMachine::evaluate(
        None,
        Some(&codex_ledger),
        None,
        SyncDirection::CodexToAgy,
        &busy_locks,
        TransportMode::Auto,
        ForkResolutionPolicy::HaltOnDivergence,
        false,
    )
    .unwrap();
    assert!(matches!(tr_busy, CursorTransition::DeferredBusy { .. }));

    // Offline clean lock yields InitialImport
    let clean_locks = HostLockSnapshot::default();
    let tr_init = CursorForkStateMachine::evaluate(
        None,
        Some(&codex_ledger),
        None,
        SyncDirection::CodexToAgy,
        &clean_locks,
        TransportMode::Auto,
        ForkResolutionPolicy::HaltOnDivergence,
        false,
    )
    .unwrap();
    assert!(matches!(
        tr_init,
        CursorTransition::InitialImport {
            source_host: HostKind::Codex,
            target_host: HostKind::Antigravity,
            ..
        }
    ));

    // Seed fingerprint & Receipt JSON parity + DAG chain tip resolution
    let (turns, digest) = ledger_seed_fingerprint(&codex_ledger);
    assert_eq!(turns, 1);
    assert_eq!(digest.len(), 64);

    let r1 = SyncReceipt {
        workspace: ws_uri.as_str().into(),
        source_host: "codex".into(),
        source_id: "codex-1".into(),
        source_digest: digest,
        target_host: "antigravity".into(),
        title: "Hello".into(),
        state: ReceiptState::Ready {
            target_id: "agy-1".into(),
            staging_path: "/workspace/staging/agy-1.json".into(),
        },
        runtime_state_not_transferred: true,
        created_at_iso: "2026-11-10T01:00:00Z".into(),
        chain_id: Some("chain-1".into()),
        chained_from_source_id: None,
        chain_depth: 1,
        target_seed_turn_count: Some(turns),
        target_seed_text_digest: Some("seed-digest".into()),
        pairing_mode: Some("paired".into()),
    };

    let json_text = serde_json::to_string(&r1).unwrap();
    assert!(json_text.contains("\"status\":\"ready\""));
    assert!(json_text.contains("\"target_id\":\"agy-1\""));
    let roundtrip: SyncReceipt = serde_json::from_str(&json_text).unwrap();
    assert_eq!(roundtrip.source_id, "codex-1");
    assert_eq!(roundtrip.status_str(), "ready");

    // Verify invalid state combination fails TryFrom
    assert!(serde_json::from_str::<SyncReceipt>(
        r#"{"workspace":"w","source_host":"codex","source_id":"s","source_digest":"d","target_host":"antigravity","title":"t","status":"ready","staging_path":"p","target_id":null}"#
    ).is_err());

    let mut r2 = r1.clone();
    r2.source_id = "agy-1".into();
    r2.source_host = "antigravity".into();
    r2.target_host = "codex".into();
    r2.chained_from_source_id = Some("codex-1".into());
    r2.chain_depth = 2;
    r2.created_at_iso = "2026-11-10T02:00:00Z".into();
    r2.state = ReceiptState::Pending {
        target_id: Some("codex-2".into()),
        staging_path: "/workspace/staging/codex-2.json".into(),
        error: Some("sync_busy_lock".into()),
    };

    let entries: Vec<ReceiptEntry> = vec![
        (PathBuf::from("r1.json"), r1.clone(), 100.0),
        (PathBuf::from("r2.json"), r2, 200.0),
    ];
    let err: Box<PendingReceiptErrorData> =
        resolve_chain_tip(&entries, Some("codex-1")).unwrap_err();
    assert_eq!(err.receipt.source_id, "agy-1");

    let resolved = resolve_chain_tip(&entries[..1], Some("codex-1"))
        .unwrap()
        .unwrap();
    assert_eq!(resolved.source_id, "codex-1");
    let _ = SessionSyncCursor {
        workspace_uri: ws_uri,
        cascade_id: CascadeId::parse("agy-1").unwrap(),
        codex_thread_id: CodexThreadId::parse("codex-1").unwrap(),
        last_synced_turn_id: t1,
        last_synced_agy_step_idx: StepIndex(0),
        last_synced_codex_line_no: RolloutLineNo(3),
        agy_boundary_digest: codex_ledger
            .latest_watermark()
            .unwrap()
            .boundary_digest
            .clone(),
        codex_boundary_digest: codex_ledger
            .latest_watermark()
            .unwrap()
            .boundary_digest
            .clone(),
        updated_at_iso: "2026-11-10T01:00:01Z".into(),
        rescued_inflight_turn: false,
        pre_rescue_source_host: String::new(),
    };
}
