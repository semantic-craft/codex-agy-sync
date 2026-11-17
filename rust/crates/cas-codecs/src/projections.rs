//! Companion summary, trajectory metadata, annotation pbtxt, and transcript wire codecs.

use std::collections::BTreeMap;

use cas_core::{
    AgyGuiSafeSource, AgyProjectId, AnnotationPbtxtProjection, CascadeId, CascadeSummaryProjection,
    SummariesStateProjection, TrajectoryId, TranscriptRowProjection, WorkspaceRootUri,
    parse_iso_to_seconds_nanos,
};
use serde_json::{Value, json};

use crate::error::CodecError;
use crate::schema_tags::{
    annotations_tag, map_entry, subagent_meta_spec_tag, summaries_state_tag, summary_field_tag,
    trajectory_meta_tag, workspace_info_tag,
};
use crate::wire::{
    FieldMap, UnknownWireField, decode_timestamp_proto, encode_bytes_field, encode_string_field,
    encode_timestamp_proto, encode_unknown_fields, encode_varint, encode_varint_field, fm_bytes,
    fm_last_str, fm_last_varint, iter_wire_fields, parse_proto_fields, signed_int32, signed_int64,
};

fn last_str_trimmed(fm: &FieldMap, tag: u32) -> String {
    fm_last_str(fm, tag).trim().to_owned()
}

fn last_timestamp(fm: &FieldMap, tag: u32, default_iso: &str) -> Result<String, CodecError> {
    let mut out = default_iso.to_owned();
    for raw in fm_bytes(fm, tag) {
        out = decode_timestamp_proto(raw, default_iso)?;
    }
    Ok(out)
}

/// Render one compact JSONL line matching Antigravity `transcript.jsonl` format.
#[must_use]
pub fn render_transcript_row_jsonl(row: &TranscriptRowProjection) -> String {
    let mut map = serde_json::Map::new();
    map.insert("step_index".into(), json!(row.step_index.0));
    map.insert("source".into(), json!(row.source));
    map.insert("type".into(), json!(row.step_type_name));
    map.insert("status".into(), json!(row.status_name));
    map.insert("created_at".into(), json!(row.created_at));
    map.insert("content".into(), json!(row.content));
    if let Some(thinking) = &row.thinking {
        map.insert("thinking".into(), json!(thinking));
    }
    if !row.tool_calls_json.is_empty() {
        map.insert("tool_calls".into(), json!(row.tool_calls_json));
    }
    if !row.media_uris.is_empty() {
        map.insert("media".into(), json!(row.media_uris));
    }
    if row.input_tokens != 0 || row.output_tokens != 0 || row.cache_read_tokens != 0 {
        map.insert("input_tokens".into(), json!(row.input_tokens));
        map.insert("cache_read_tokens".into(), json!(row.cache_read_tokens));
        map.insert("output_tokens".into(), json!(row.output_tokens));
    }
    if !row.truncated_fields.is_empty() {
        map.insert("truncated_fields".into(), json!(row.truncated_fields));
    }
    Value::Object(map).to_string()
}

/// Serialize `exa.jetski_cortex_pb.ConversationAnnotations` textproto.
#[must_use]
pub fn render_annotation_pbtxt(proj: &AnnotationPbtxtProjection) -> String {
    let single_line = proj.title.split_whitespace().collect::<Vec<_>>().join(" ");
    let escaped = single_line.replace('\\', "\\\\").replace('"', "\\\"");
    let (seconds, nanos) = parse_iso_to_seconds_nanos(&proj.last_user_view_time_iso);
    if nanos > 0 {
        format!(
            "title: \"{escaped}\"\nlast_user_view_time {{\n  seconds: {seconds}\n  nanos: {nanos}\n}}\n"
        )
    } else {
        format!("title: \"{escaped}\"\nlast_user_view_time {{\n  seconds: {seconds}\n}}\n")
    }
}

/// Extract unescaped `title` string from `ConversationAnnotations` textproto.
#[must_use]
pub fn parse_annotation_pbtxt_title(raw_text: &str) -> Option<String> {
    let idx = raw_text.find("title:")?;
    let after = raw_text[idx + "title:".len()..].trim_start();
    let rest = after.strip_prefix('"')?;
    let mut raw_title = String::new();
    let mut chars = rest.chars();
    let mut closed = false;
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            if let Some(esc) = chars.next() {
                raw_title.push('\\');
                raw_title.push(esc);
            }
        } else if ch == '"' {
            closed = true;
            break;
        } else {
            raw_title.push(ch);
        }
    }
    if !closed || raw_title.trim().is_empty() {
        return None;
    }
    let unescaped = raw_title
        .replace("\\\"", "\"")
        .replace("\\n", "\n")
        .replace("\\\\", "\\");
    let trimmed = unescaped.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_owned())
    }
}

/// Encode `exa.cortex_pb.WorkspaceInfo` wire bytes.
#[must_use]
pub fn encode_workspace_info_proto(uri: &WorkspaceRootUri) -> Vec<u8> {
    let text = uri.as_str();
    let mut out = encode_string_field(workspace_info_tag::WORKSPACE_FOLDER_ABSOLUTE_URI, text);
    out.extend_from_slice(&encode_string_field(
        workspace_info_tag::GIT_ROOT_ABSOLUTE_URI,
        text,
    ));
    out
}

/// Encode `exa.jetski_cortex_pb.ConversationAnnotations` wire bytes.
#[must_use]
pub fn encode_annotations_proto(proj: &AnnotationPbtxtProjection) -> Vec<u8> {
    let mut buf = Vec::new();
    if !proj.title.is_empty() {
        buf.extend_from_slice(&encode_string_field(annotations_tag::TITLE, &proj.title));
    }
    let ts_bytes = encode_timestamp_proto(&proj.last_user_view_time_iso);
    if !ts_bytes.is_empty() {
        buf.extend_from_slice(&encode_bytes_field(
            annotations_tag::LAST_USER_VIEW_TIME,
            &ts_bytes,
        ));
    }
    if !proj.unknown_wire_fields.is_empty() {
        buf.extend_from_slice(&encode_unknown_fields(&proj.unknown_wire_fields));
    }
    buf
}

/// Decode `exa.jetski_cortex_pb.ConversationAnnotations` wire bytes preserving unknown fields.
///
/// # Errors
/// Returns [`CodecError::Wire`] if protobuf wire bytes are malformed.
pub fn decode_annotations_proto(
    raw_bytes: &[u8],
    cascade_id: &CascadeId,
    default_view_iso: &str,
) -> Result<AnnotationPbtxtProjection, CodecError> {
    let known = [annotations_tag::TITLE, annotations_tag::LAST_USER_VIEW_TIME];
    let (fm, unknowns) = parse_proto_fields(raw_bytes, Some(&known))?;
    Ok(AnnotationPbtxtProjection {
        cascade_id: cascade_id.clone(),
        title: fm_last_str(&fm, annotations_tag::TITLE),
        last_user_view_time_iso: last_timestamp(
            &fm,
            annotations_tag::LAST_USER_VIEW_TIME,
            default_view_iso,
        )?,
        unknown_wire_fields: unknowns,
    })
}

/// Decoded fields from `exa.cortex_pb.CortexTrajectoryMetadata`.
#[derive(Debug, Clone, Default)]
pub struct DecodedTrajectoryMetadata {
    pub project_id: String,
    pub workspace_uris: Vec<String>,
    pub created_at_iso: String,
    pub parent_conversation_id: String,
    pub root_conversation_id: String,
    pub agent_type_name: String,
    pub agent_role: String,
    pub nesting_depth: u32,
    pub unknown_wire_fields: Vec<UnknownWireField>,
}

/// Parameters to encode `exa.cortex_pb.CortexTrajectoryMetadata` wire bytes.
#[derive(Debug, Clone, Copy)]
pub struct TrajectoryMetadataEncodeParams<'a> {
    pub project_id: &'a AgyProjectId,
    pub workspace_uris: &'a [WorkspaceRootUri],
    pub created_at_iso: &'a str,
    pub nesting_depth: i64,
    pub parent_conversation_id: &'a str,
    pub root_conversation_id: &'a str,
    pub agent_type_name: &'a str,
    pub agent_role: &'a str,
    pub unknown_wire_fields: &'a [UnknownWireField],
}

/// Encode `exa.cortex_pb.CortexTrajectoryMetadata` wire bytes.
#[must_use]
pub fn encode_trajectory_metadata_proto(p: &TrajectoryMetadataEncodeParams<'_>) -> Vec<u8> {
    let mut buf = Vec::new();
    for uri in p.workspace_uris {
        buf.extend_from_slice(&encode_bytes_field(
            trajectory_meta_tag::WORKSPACES,
            &encode_workspace_info_proto(uri),
        ));
    }
    let ts_bytes = encode_timestamp_proto(p.created_at_iso);
    if !ts_bytes.is_empty() {
        buf.extend_from_slice(&encode_bytes_field(
            trajectory_meta_tag::CREATED_AT,
            &ts_bytes,
        ));
    }
    if !p.parent_conversation_id.is_empty() {
        buf.extend_from_slice(&encode_string_field(
            trajectory_meta_tag::PARENT_CONVERSATION_ID,
            p.parent_conversation_id,
        ));
    }
    if !p.root_conversation_id.is_empty() {
        buf.extend_from_slice(&encode_string_field(
            trajectory_meta_tag::ROOT_CONVERSATION_ID,
            p.root_conversation_id,
        ));
    }
    for uri in p.workspace_uris {
        buf.extend_from_slice(&encode_string_field(
            trajectory_meta_tag::WORKSPACE_URIS,
            uri.as_str(),
        ));
    }
    if !p.agent_type_name.is_empty() || !p.agent_role.is_empty() {
        let mut spec_bytes =
            encode_string_field(subagent_meta_spec_tag::TYPE_NAME, p.agent_type_name);
        spec_bytes.extend_from_slice(&encode_string_field(
            subagent_meta_spec_tag::ROLE,
            p.agent_role,
        ));
        buf.extend_from_slice(&encode_bytes_field(
            trajectory_meta_tag::SUBAGENT_SPEC,
            &spec_bytes,
        ));
    }
    if p.nesting_depth != 0 {
        buf.extend_from_slice(&encode_varint_field(
            trajectory_meta_tag::NESTING_DEPTH,
            p.nesting_depth,
        ));
    }
    if !p.project_id.as_str().is_empty() {
        buf.extend_from_slice(&encode_string_field(
            trajectory_meta_tag::PROJECT_ID,
            p.project_id.as_str(),
        ));
    }
    if !p.unknown_wire_fields.is_empty() {
        buf.extend_from_slice(&encode_unknown_fields(p.unknown_wire_fields));
    }
    buf
}

/// Decode `exa.cortex_pb.CortexTrajectoryMetadata` wire bytes preserving unknown fields.
///
/// # Errors
/// Returns [`CodecError::Wire`] if protobuf wire bytes are malformed.
pub fn decode_trajectory_metadata_proto(
    raw_bytes: &[u8],
) -> Result<DecodedTrajectoryMetadata, CodecError> {
    let (fm, unknowns) = parse_proto_fields(raw_bytes, Some(trajectory_meta_tag::ALL))?;
    let mut workspace_uris = Vec::new();
    for val in fm_bytes(&fm, trajectory_meta_tag::WORKSPACES) {
        for (sfn, swt, _, sraw) in iter_wire_fields(val)? {
            if sfn == workspace_info_tag::WORKSPACE_FOLDER_ABSOLUTE_URI && swt == 2 {
                let u = String::from_utf8_lossy(&sraw).trim().to_owned();
                if !u.is_empty() && !workspace_uris.contains(&u) {
                    workspace_uris.push(u);
                }
            }
        }
    }
    for u_raw in fm_bytes(&fm, trajectory_meta_tag::WORKSPACE_URIS) {
        let u = String::from_utf8_lossy(u_raw).trim().to_owned();
        if !u.is_empty() && !workspace_uris.contains(&u) {
            workspace_uris.push(u);
        }
    }
    let mut agent_type_name = String::new();
    let mut agent_role = String::new();
    for val in fm_bytes(&fm, trajectory_meta_tag::SUBAGENT_SPEC) {
        for (sfn, swt, _, sraw) in iter_wire_fields(val)? {
            if sfn == subagent_meta_spec_tag::TYPE_NAME && swt == 2 {
                agent_type_name = String::from_utf8_lossy(&sraw).trim().to_owned();
            } else if sfn == subagent_meta_spec_tag::ROLE && swt == 2 {
                agent_role = String::from_utf8_lossy(&sraw).trim().to_owned();
            }
        }
    }
    let nesting_depth = fm_last_varint(&fm, trajectory_meta_tag::NESTING_DEPTH)
        .map_or(0, |v| signed_int32(v).max(0) as u32);
    Ok(DecodedTrajectoryMetadata {
        project_id: last_str_trimmed(&fm, trajectory_meta_tag::PROJECT_ID),
        workspace_uris,
        created_at_iso: last_timestamp(&fm, trajectory_meta_tag::CREATED_AT, "")?,
        parent_conversation_id: last_str_trimmed(&fm, trajectory_meta_tag::PARENT_CONVERSATION_ID),
        root_conversation_id: last_str_trimmed(&fm, trajectory_meta_tag::ROOT_CONVERSATION_ID),
        agent_type_name,
        agent_role,
        nesting_depth,
        unknown_wire_fields: unknowns,
    })
}

/// Encode `exa.jetski_cortex_pb.CascadeTrajectorySummary` wire bytes.
#[must_use]
pub fn encode_cascade_summary_proto(proj: &CascadeSummaryProjection) -> Vec<u8> {
    let mut buf = Vec::new();
    if !proj.title.is_empty() {
        buf.extend_from_slice(&encode_string_field(
            summary_field_tag::SUMMARY,
            &proj.title,
        ));
    }
    if proj.step_count > 0 {
        buf.extend_from_slice(&encode_varint_field(
            summary_field_tag::STEP_COUNT,
            i64::from(proj.step_count),
        ));
    }
    let mod_ts = encode_timestamp_proto(&proj.updated_at_iso);
    if !mod_ts.is_empty() {
        buf.extend_from_slice(&encode_bytes_field(
            summary_field_tag::LAST_MODIFIED_TIME,
            &mod_ts,
        ));
    }
    let traj_id_str = proj
        .trajectory_id
        .as_ref()
        .map_or(proj.cascade_id.as_str(), TrajectoryId::as_str);
    if !traj_id_str.is_empty() {
        buf.extend_from_slice(&encode_string_field(
            summary_field_tag::TRAJECTORY_ID,
            traj_id_str,
        ));
    }
    if proj.status > 0 {
        buf.extend_from_slice(&encode_varint_field(
            summary_field_tag::STATUS,
            i64::from(proj.status),
        ));
    }
    let created_ts = encode_timestamp_proto(&proj.created_at_iso);
    if !created_ts.is_empty() {
        buf.extend_from_slice(&encode_bytes_field(
            summary_field_tag::CREATED_TIME,
            &created_ts,
        ));
    }
    for uri in &proj.workspace_uris {
        buf.extend_from_slice(&encode_bytes_field(
            summary_field_tag::WORKSPACES,
            &encode_workspace_info_proto(uri),
        ));
    }
    let user_iso = if proj.last_user_input_time_iso.is_empty() {
        proj.updated_at_iso.as_str()
    } else {
        proj.last_user_input_time_iso.as_str()
    };
    let user_ts = encode_timestamp_proto(user_iso);
    if !user_ts.is_empty() {
        buf.extend_from_slice(&encode_bytes_field(
            summary_field_tag::LAST_USER_INPUT_TIME,
            &user_ts,
        ));
    }
    let annot_bytes = encode_annotations_proto(&proj.annotations);
    if !annot_bytes.is_empty() {
        buf.extend_from_slice(&encode_bytes_field(
            summary_field_tag::ANNOTATIONS,
            &annot_bytes,
        ));
    }
    if let Some(step_idx) = proj.last_user_input_step_index
        && step_idx >= 0
    {
        buf.extend_from_slice(&encode_varint_field(
            summary_field_tag::LAST_USER_INPUT_STEP_INDEX,
            i64::from(step_idx),
        ));
    }
    let traj_meta_bytes = encode_trajectory_metadata_proto(&TrajectoryMetadataEncodeParams {
        project_id: &proj.project_id,
        workspace_uris: &proj.workspace_uris,
        created_at_iso: &proj.created_at_iso,
        nesting_depth: i64::from(proj.nesting_depth),
        parent_conversation_id: proj
            .parent_cascade_id
            .as_ref()
            .map_or("", CascadeId::as_str),
        root_conversation_id: proj.root_cascade_id.as_ref().map_or("", CascadeId::as_str),
        agent_type_name: &proj.agent_type_name,
        agent_role: &proj.agent_role,
        unknown_wire_fields: &proj.trajectory_metadata_unknown_fields,
    });
    if !traj_meta_bytes.is_empty() {
        buf.extend_from_slice(&encode_bytes_field(
            summary_field_tag::TRAJECTORY_METADATA,
            &traj_meta_bytes,
        ));
    }
    if proj.source.proto_enum_value() != 0 {
        buf.extend_from_slice(&encode_varint_field(
            summary_field_tag::TRAJECTORY_SOURCE,
            i64::from(proj.source.proto_enum_value()),
        ));
    }
    if proj.trajectory_type > 0 {
        buf.extend_from_slice(&encode_varint_field(
            summary_field_tag::TRAJECTORY_TYPE,
            i64::from(proj.trajectory_type),
        ));
    }
    if !proj.unknown_wire_fields.is_empty() {
        buf.extend_from_slice(&encode_unknown_fields(&proj.unknown_wire_fields));
    }
    buf
}

/// Decode `exa.jetski_cortex_pb.CascadeTrajectorySummary` wire bytes faithfully.
///
/// # Errors
/// Returns [`CodecError::Wire`] or [`CodecError::Core`] if wire bytes or IDs are invalid.
pub fn decode_cascade_summary_proto(
    raw_bytes: &[u8],
    fallback_cascade_id: Option<&CascadeId>,
) -> Result<CascadeSummaryProjection, CodecError> {
    let (fm, mut unknowns) = parse_proto_fields(raw_bytes, Some(summary_field_tag::ALL))?;
    let summary_str = fm_last_str(&fm, summary_field_tag::SUMMARY);
    let traj_id_str = last_str_trimmed(&fm, summary_field_tag::TRAJECTORY_ID);
    let updated_iso = last_timestamp(&fm, summary_field_tag::LAST_MODIFIED_TIME, "")?;
    let created_iso = last_timestamp(&fm, summary_field_tag::CREATED_TIME, "")?;
    let last_user_iso = last_timestamp(&fm, summary_field_tag::LAST_USER_INPUT_TIME, "")?;

    let effective_cid = match fallback_cascade_id {
        Some(cid) => cid.clone(),
        None => CascadeId::parse(if traj_id_str.is_empty() {
            "unknown-cascade"
        } else {
            &traj_id_str
        })?,
    };

    let mut ws_uris: Vec<WorkspaceRootUri> = Vec::new();
    for val in fm_bytes(&fm, summary_field_tag::WORKSPACES) {
        for (sfn, swt, _, sraw) in iter_wire_fields(val)? {
            if sfn == workspace_info_tag::WORKSPACE_FOLDER_ABSOLUTE_URI && swt == 2 {
                let u_str = String::from_utf8_lossy(&sraw).trim().to_owned();
                if let Ok(u) = WorkspaceRootUri::parse(&u_str)
                    && !ws_uris.contains(&u)
                {
                    ws_uris.push(u);
                }
            }
        }
    }

    let mut tmeta = DecodedTrajectoryMetadata::default();
    for val in fm_bytes(&fm, summary_field_tag::TRAJECTORY_METADATA) {
        tmeta = decode_trajectory_metadata_proto(val)?;
        for u_str in &tmeta.workspace_uris {
            if let Ok(u) = WorkspaceRootUri::parse(u_str)
                && !ws_uris.contains(&u)
            {
                ws_uris.push(u);
            }
        }
    }

    let eff_created = if created_iso.is_empty() {
        tmeta.created_at_iso.clone()
    } else {
        created_iso
    };
    let eff_updated = if !updated_iso.is_empty() {
        updated_iso
    } else if !last_user_iso.is_empty() {
        last_user_iso.clone()
    } else {
        eff_created.clone()
    };

    let mut annot = AnnotationPbtxtProjection {
        cascade_id: effective_cid.clone(),
        title: String::new(),
        last_user_view_time_iso: eff_updated.clone(),
        unknown_wire_fields: Vec::new(),
    };
    for val in fm_bytes(&fm, summary_field_tag::ANNOTATIONS) {
        annot = decode_annotations_proto(val, &effective_cid, &eff_updated)?;
    }

    let mut source = AgyGuiSafeSource::Unspecified;
    if let Some(raw_src) = fm_last_varint(&fm, summary_field_tag::TRAJECTORY_SOURCE) {
        if let Ok(u) = u32::try_from(raw_src)
            && let Some(mapped) = AgyGuiSafeSource::from_proto_enum(u)
        {
            source = mapped;
        } else {
            unknowns.push(UnknownWireField {
                field_tag: summary_field_tag::TRAJECTORY_SOURCE,
                wire_type: 0,
                raw_bytes: encode_varint(raw_src),
            });
        }
    }

    let resolved_title = if annot.title.is_empty() {
        summary_str.clone()
    } else {
        annot.title.clone()
    };
    let preview = if summary_str.is_empty() {
        resolved_title.clone()
    } else {
        summary_str
    };
    let agent_name = if tmeta.agent_role.is_empty() {
        tmeta.agent_type_name.clone()
    } else {
        tmeta.agent_role.clone()
    };
    let parent_cid = if tmeta.parent_conversation_id.is_empty() {
        None
    } else {
        CascadeId::parse(&tmeta.parent_conversation_id).ok()
    };
    let root_cid = if tmeta.root_conversation_id.is_empty() {
        None
    } else {
        CascadeId::parse(&tmeta.root_conversation_id).ok()
    };
    let proj_id = AgyProjectId::from_wire_lossy(&tmeta.project_id);
    let step_count = fm_last_varint(&fm, summary_field_tag::STEP_COUNT).unwrap_or(0) as u32;
    let status = fm_last_varint(&fm, summary_field_tag::STATUS).unwrap_or(0) as i32;
    let trajectory_id = if traj_id_str.is_empty() {
        None
    } else {
        TrajectoryId::parse(&traj_id_str).ok()
    };
    let trajectory_type =
        fm_last_varint(&fm, summary_field_tag::TRAJECTORY_TYPE).unwrap_or(0) as i32;
    let last_user_input_step_index =
        fm_last_varint(&fm, summary_field_tag::LAST_USER_INPUT_STEP_INDEX).map(|v| v as i32);

    Ok(CascadeSummaryProjection {
        cascade_id: effective_cid,
        project_id: proj_id,
        workspace_uris: ws_uris,
        source,
        title: resolved_title,
        preview,
        step_count,
        created_at_iso: eff_created,
        updated_at_iso: eff_updated,
        annotations: annot,
        nesting_depth: tmeta.nesting_depth,
        parent_cascade_id: parent_cid,
        root_cascade_id: root_cid,
        agent_name,
        agent_type_name: tmeta.agent_type_name,
        agent_role: tmeta.agent_role,
        status,
        trajectory_id,
        trajectory_type,
        last_user_input_time_iso: last_user_iso,
        last_user_input_step_index,
        trajectory_metadata_unknown_fields: tmeta.unknown_wire_fields,
        unknown_wire_fields: unknowns,
    })
}

/// Encode `jetbox_summaries_pb.SummariesState` wire bytes.
#[must_use]
pub fn encode_summaries_state_proto(state: &SummariesStateProjection) -> Vec<u8> {
    let mut buf = Vec::new();
    for (cid, summary_proj) in &state.summaries {
        let mut entry = encode_string_field(map_entry::KEY, cid.as_str());
        entry.extend_from_slice(&encode_bytes_field(
            map_entry::VALUE,
            &encode_cascade_summary_proto(summary_proj),
        ));
        buf.extend_from_slice(&encode_bytes_field(summaries_state_tag::SUMMARIES, &entry));
    }
    for (path_key, mtime_val) in &state.file_mod_times {
        let mut entry = encode_string_field(map_entry::KEY, path_key);
        entry.extend_from_slice(&encode_varint_field(map_entry::VALUE, *mtime_val));
        buf.extend_from_slice(&encode_bytes_field(
            summaries_state_tag::FILE_MOD_TIMES,
            &entry,
        ));
    }
    if !state.unknown_wire_fields.is_empty() {
        buf.extend_from_slice(&encode_unknown_fields(&state.unknown_wire_fields));
    }
    buf
}

/// Decode `jetbox_summaries_pb.SummariesState` wire bytes faithfully.
///
/// # Errors
/// Returns [`CodecError::Wire`] or [`CodecError::Core`] if wire bytes or IDs are malformed.
pub fn decode_summaries_state_proto(
    raw_bytes: &[u8],
) -> Result<SummariesStateProjection, CodecError> {
    let (fm, unknowns) = parse_proto_fields(raw_bytes, Some(summaries_state_tag::ALL))?;
    let mut summaries = BTreeMap::new();
    let mut file_mod_times = BTreeMap::new();

    for payload in fm_bytes(&fm, summaries_state_tag::SUMMARIES) {
        let mut cid_str = String::new();
        let mut sum_bytes = Vec::new();
        for (efn, ewt, _, epayload) in iter_wire_fields(payload)? {
            if efn == map_entry::KEY && ewt == 2 {
                cid_str = String::from_utf8_lossy(&epayload).into_owned();
            } else if efn == map_entry::VALUE && ewt == 2 {
                sum_bytes = epayload;
            }
        }
        if !cid_str.is_empty()
            && let Ok(cid) = CascadeId::parse(&cid_str)
        {
            let proj = decode_cascade_summary_proto(&sum_bytes, Some(&cid))?;
            summaries.insert(cid, proj);
        }
    }

    for payload in fm_bytes(&fm, summaries_state_tag::FILE_MOD_TIMES) {
        let mut pkey = String::new();
        let mut mval = 0i64;
        for (efn, ewt, eval_int, epayload) in iter_wire_fields(payload)? {
            if efn == map_entry::KEY && ewt == 2 {
                pkey = String::from_utf8_lossy(&epayload).into_owned();
            } else if efn == map_entry::VALUE && ewt == 0 {
                mval = signed_int64(eval_int);
            }
        }
        if !pkey.is_empty() {
            file_mod_times.insert(pkey, mval);
        }
    }

    Ok(SummariesStateProjection {
        summaries,
        file_mod_times,
        unknown_wire_fields: unknowns,
    })
}
