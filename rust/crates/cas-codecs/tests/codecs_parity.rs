use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use cas_codecs::{
    STEP_WIRE_TABLE, TOOL_CODEC_TABLE, UnknownWireField, bind_agy_step,
    compress_zstd_rollout_bytes, decode_agy_trajectory, decode_codex_rollout,
    decode_codex_rpc_thread_to_ledger, decode_summaries_state_proto, decompress_zstd_rollout_bytes,
    encode_agy_trajectory, encode_bytes_field, encode_codex_rollout, encode_summaries_state_proto,
    encode_unknown_fields, parse_annotation_pbtxt_title, read_codex_rollout_lines,
    render_annotation_pbtxt, splice_rollout_completed_prefix,
};
use cas_core::{
    AgyGuiSafeSource, AgyProjectId, AnnotationPbtxtProjection, CanonicalEvent,
    CanonicalToolPayload, CascadeId, CascadeSummaryProjection, EventOriginAnchor, FileEditOp,
    HostKind, RolloutLineNo, StepIndex, SummariesStateProjection, TokenUsageAccounting, ToolCallId,
    TurnId, parse_workspace_identity,
};
use serde_json::json;

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
fn schema_proto_contains_all_step_and_tool_definitions() {
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let proto_path = manifest_dir
        .join("..")
        .join("..")
        .join("..")
        .join("schema")
        .join("antigravity_2_30.proto");
    let proto_text = fs::read_to_string(&proto_path).unwrap();

    for spec in STEP_WIRE_TABLE {
        let expected_field = format!(
            "{} = {};",
            spec.oneof_field_name, spec.step_oneof_tag as u32
        );
        assert!(
            proto_text.contains(&expected_field),
            "missing step field {expected_field:?} in schema/antigravity_2_30.proto"
        );
    }
    assert_eq!(TOOL_CODEC_TABLE.len(), 16);
    for tspec in TOOL_CODEC_TABLE {
        let full_name = tspec
            .agy_any_type_url
            .strip_prefix("type.googleapis.com/")
            .unwrap();
        if matches!(
            tspec.payload_kind,
            "generate_image" | "moma_search" | "finish"
        ) {
            continue;
        }
        let needle = format!("// {full_name}");
        assert!(
            proto_text.contains(&needle),
            "missing tool message {needle:?} in schema/antigravity_2_30.proto"
        );
    }
}

#[test]
fn agy_step_roundtrip_preserves_unknown_fields_and_tool_payloads() {
    let (ws_path, ws_uri) = parse_workspace_identity("/workspace/sample").unwrap();
    let t1 = TurnId::parse("turn-1").unwrap();
    let mut a1 = EventOriginAnchor::new(HostKind::Antigravity, t1, "2026-11-10T01:00:00Z");
    a1.unknown_wire_fields.push(UnknownWireField {
        field_tag: 199,
        wire_type: 2,
        raw_bytes: b"future-field".to_vec(),
    });

    let events = vec![
        CanonicalEvent::UserInput {
            anchor: a1.clone(),
            query_text: "Edit foo.py and run pytest".into(),
            scope_items: vec!["file:///workspace/sample/foo.py".into()],
            media: vec![],
        },
        CanonicalEvent::Thinking {
            anchor: a1.clone(),
            summary_text: "Planning edit".into(),
            raw_thinking: Some("raw chain".into()),
            thinking_signature: Some(b"sig-1".to_vec()),
            token_usage: TokenUsageAccounting {
                input_tokens: 10,
                output_tokens: 20,
                cache_read_tokens: 5,
                thinking_duration_ms: Some(250),
            },
        },
        CanonicalEvent::ToolCall {
            anchor: a1.clone(),
            call_id: ToolCallId::parse("call-edit-1").unwrap(),
            tool_name: "edit_file".into(),
            tool_summary: "Editing foo.py".into(),
            payload: CanonicalToolPayload::FileEdit {
                path: "/workspace/sample/foo.py".into(),
                edit_op: FileEditOp::Modify,
                unified_diff: "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new".into(),
                diff_stats: (1, 1),
                is_artifact: false,
                artifact_summary: None,
            },
            thinking_signature: Some(b"tool-sig".to_vec()),
        },
        CanonicalEvent::ToolResult {
            anchor: a1.clone(),
            call_id: ToolCallId::parse("call-edit-1").unwrap(),
            tool_name: "edit_file".into(),
            output_text: "Edited".into(),
            is_error: false,
            payload: CanonicalToolPayload::FileEdit {
                path: "/workspace/sample/foo.py".into(),
                edit_op: FileEditOp::Modify,
                unified_diff: "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new".into(),
                diff_stats: (1, 1),
                is_artifact: false,
                artifact_summary: None,
            },
            full_output_uri: None,
        },
        CanonicalEvent::AssistantText {
            anchor: a1,
            text: "All done.".into(),
            message_id: Some("msg-1".into()),
            stop_reason: Some("1".into()),
            generator_model: Some("42".into()),
            token_usage: TokenUsageAccounting::default(),
        },
    ];

    let rows = encode_agy_trajectory(&events, StepIndex(0));
    assert_eq!(rows.len(), 4);
    let bound = bind_agy_step(&rows[0], "traj-1", "casc-1").unwrap();
    assert!(!bound.metadata_blob.is_empty());

    let decoded = decode_agy_trajectory(ws_path, ws_uri, &rows, None).unwrap();
    assert_eq!(decoded.completed_turn_watermarks.len(), 1);
    assert!(!decoded.has_inflight_turn);
    assert_eq!(decoded.events[0].anchor().unknown_wire_fields.len(), 1);
    assert_eq!(
        decoded.events[0].anchor().unknown_wire_fields[0].field_tag,
        199
    );

    let encoded_unknown = encode_unknown_fields(&decoded.events[0].anchor().unknown_wire_fields);
    assert_eq!(encoded_unknown, encode_bytes_field(199, b"future-field"));
}

#[test]
fn codex_rollout_and_zstd_streaming_roundtrip() {
    let sandbox = TempArtifactsDir::new("cas-codecs-zst");
    let (ws_path, ws_uri) = parse_workspace_identity("/workspace/sample").unwrap();
    let t1 = TurnId::parse("turn-1").unwrap();
    let a1 = EventOriginAnchor::new(HostKind::Codex, t1, "2026-11-10T02:00:00Z");

    let events = vec![
        CanonicalEvent::UserInput {
            anchor: a1.clone(),
            query_text: "Run cargo test".into(),
            scope_items: vec![],
            media: vec![],
        },
        CanonicalEvent::ToolCall {
            anchor: a1.clone(),
            call_id: ToolCallId::parse("call-cmd-1").unwrap(),
            tool_name: "exec_command".into(),
            tool_summary: "exec_command".into(),
            payload: CanonicalToolPayload::RunCommand {
                command_line: "cargo test".into(),
                cwd: "/workspace/sample".into(),
                exit_code: Some(0),
                timed_out: false,
                is_daemon: false,
                shell_name: None,
                ran_in_sandbox: false,
                sandbox_policy: None,
            },
            thinking_signature: None,
        },
        CanonicalEvent::ToolResult {
            anchor: a1.clone(),
            call_id: ToolCallId::parse("call-cmd-1").unwrap(),
            tool_name: "exec_command".into(),
            output_text: "ok".into(),
            is_error: false,
            payload: CanonicalToolPayload::RunCommand {
                command_line: "cargo test".into(),
                cwd: "/workspace/sample".into(),
                exit_code: Some(0),
                timed_out: false,
                is_daemon: false,
                shell_name: None,
                ran_in_sandbox: false,
                sandbox_policy: None,
            },
            full_output_uri: None,
        },
        CanonicalEvent::AssistantText {
            anchor: a1,
            text: "Tests pass.".into(),
            message_id: None,
            stop_reason: None,
            generator_model: None,
            token_usage: TokenUsageAccounting::default(),
        },
    ];

    let lines = encode_codex_rollout(
        &events,
        RolloutLineNo(1),
        Some("codex-thread-1"),
        Some("/workspace/sample"),
    );
    let raw_jsonl: String = lines.iter().map(|l| format!("{}\n", l.raw_json)).collect();
    let zst_bytes = compress_zstd_rollout_bytes(&raw_jsonl).unwrap();
    let decompressed = decompress_zstd_rollout_bytes(&zst_bytes).unwrap();
    assert_eq!(decompressed, raw_jsonl);

    let zst_path = sandbox.path.join("rollout-test.jsonl.zst");
    fs::write(&zst_path, &zst_bytes).unwrap();
    let streamed_lines = read_codex_rollout_lines(&zst_path).unwrap();
    assert_eq!(streamed_lines.len(), lines.len());

    let ledger = decode_codex_rollout(ws_path, ws_uri, &streamed_lines, None).unwrap();
    assert_eq!(ledger.completed_turn_watermarks.len(), 1);
    assert!(!ledger.has_inflight_turn);
}

#[test]
fn summaries_pbtxt_and_rpc_thread_splice() {
    let (ws_path, ws_uri) = parse_workspace_identity("/workspace/sample").unwrap();
    let cid = CascadeId::parse("casc-1").unwrap();
    let pid = AgyProjectId::parse("proj-1").unwrap();
    let annot = AnnotationPbtxtProjection {
        cascade_id: cid.clone(),
        title: "Sample \"Quoted\" Title".into(),
        last_user_view_time_iso: "2026-11-10T03:00:00Z".into(),
        unknown_wire_fields: vec![],
    };
    let pbtxt = render_annotation_pbtxt(&annot);
    assert_eq!(
        parse_annotation_pbtxt_title(&pbtxt).as_deref(),
        Some("Sample \"Quoted\" Title")
    );

    let summary = CascadeSummaryProjection::try_new(
        cid.clone(),
        pid,
        vec![ws_uri.clone()],
        AgyGuiSafeSource::CascadeClient,
        "Sample Title".into(),
        "Sample Title".into(),
        3,
        "2026-11-10T02:00:00Z".into(),
        "2026-11-10T03:00:00Z".into(),
        annot,
        0,
        None,
        None,
        String::new(),
        String::new(),
        String::new(),
    )
    .unwrap();
    let mut summaries = BTreeMap::new();
    summaries.insert(cid.clone(), summary);
    let mut file_mod_times = BTreeMap::new();
    file_mod_times.insert("casc-1.pb".into(), 123456789);
    let state = SummariesStateProjection {
        summaries,
        file_mod_times,
        unknown_wire_fields: vec![],
    };
    let encoded = encode_summaries_state_proto(&state);
    let decoded = decode_summaries_state_proto(&encoded).unwrap();
    assert!(decoded.summaries.contains_key(&cid));
    assert_eq!(decoded.file_mod_times.get("casc-1.pb"), Some(&123456789));

    let rpc_obj = json!({
        "id": "codex-1",
        "name": "RPC Title",
        "createdAt": "2026-11-10T01:00:00Z",
        "updatedAt": "2026-11-10T02:00:00Z",
        "turns": [
            {
                "status": "completed",
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "Hi"}]},
                    {"type": "agentMessage", "text": "Hello"}
                ]
            },
            {
                "status": "inProgress",
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "Next"}]}
                ]
            }
        ]
    });
    let rpc_ledger = decode_codex_rpc_thread_to_ledger(
        ws_path.clone(),
        ws_uri.clone(),
        rpc_obj.as_object().unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(rpc_ledger.completed_turn_watermarks.len(), 1);
    assert!(rpc_ledger.has_inflight_turn);

    let disk_events = vec![
        CanonicalEvent::UserInput {
            anchor: EventOriginAnchor::new(
                HostKind::Codex,
                TurnId::parse("turn-1").unwrap(),
                "2026-11-10T01:00:00Z",
            ),
            query_text: "Hi".into(),
            scope_items: vec![],
            media: vec![],
        },
        CanonicalEvent::AssistantText {
            anchor: EventOriginAnchor::new(
                HostKind::Codex,
                TurnId::parse("turn-1").unwrap(),
                "2026-11-10T01:00:01Z",
            ),
            text: "Hello".into(),
            message_id: None,
            stop_reason: None,
            generator_model: None,
            token_usage: TokenUsageAccounting::default(),
        },
    ];
    let disk_ledger = cas_core::TrajectoryLedger::from_events(
        ws_path,
        ws_uri,
        &disk_events,
        Some("RPC Title"),
        false,
    );
    let spliced = splice_rollout_completed_prefix(&disk_ledger, &rpc_ledger, true, None);
    assert_eq!(spliced.completed_turn_watermarks.len(), 1);
    assert!(spliced.has_inflight_turn);
}
