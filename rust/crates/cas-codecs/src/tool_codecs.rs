//! Antigravity `exa.cortex.core.tools.*` protobuf wire codecs, `TOOL_CODEC_TABLE`, and Codex adapters.

use std::collections::{BTreeMap, HashMap};
use std::sync::LazyLock;

use cas_core::{
    CanonicalToolPayload, FileEditOp, ManageSubagentsAction, ManageTaskAction, QuestionItem,
    QuestionOption, SearchWebDocument, SubagentWorkspaceMode, dump_canonical_json,
    normalize_tool_file_path, safe_json_dict, serialize_tool_payload_arguments_json,
    tool_payload_args_map,
};
use serde_json::{Map, Value, json};

use crate::schema_tags::{any_envelope, tool_tags};
use crate::wire::{
    FieldMap, encode_bytes_field, encode_string_field, encode_uvarint_field, encode_varint_field,
    fm_bytes, fm_last_str_fallback as fm_last_str, fm_last_varint, fm_opt_str, fm_strs,
    parse_proto_fields, signed_int32, signed_int64,
};

pub(crate) fn rewrite_patch_headers(text: &str) -> (Option<(&'static str, String)>, String) {
    let mut first: Option<(&'static str, String)> = None;
    let mut out_lines = Vec::new();
    for line in text.split('\n') {
        let matched = if let Some(rest) = line.strip_prefix("*** Add File:") {
            Some(("Add", rest.trim_start()))
        } else if let Some(rest) = line.strip_prefix("*** Update File:") {
            Some(("Update", rest.trim_start()))
        } else {
            line.strip_prefix("*** Delete File:")
                .map(|rest| ("Delete", rest.trim_start()))
        };
        if let Some((verb, raw_p)) = matched {
            if first.is_none() {
                first = Some((verb, raw_p.to_owned()));
            }
            out_lines.push(format!(
                "*** {verb} File: {}",
                normalize_tool_file_path(raw_p)
            ));
        } else {
            out_lines.push(line.to_owned());
        }
    }
    (first, out_lines.join("\n"))
}

fn pmap(buf: &[u8]) -> FieldMap {
    if buf.is_empty() {
        FieldMap::new()
    } else {
        parse_proto_fields(buf, None)
            .map(|(fm, _)| fm)
            .unwrap_or_default()
    }
}

fn first_present<'a>(map: &'a BTreeMap<String, Value>, keys: &[&str]) -> Option<&'a Value> {
    for &k in keys {
        if let Some(v) = map.get(k)
            && !v.is_null()
        {
            return Some(v);
        }
    }
    None
}

fn val_as_str(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => b.to_string(),
        _ => String::new(),
    }
}

fn val_as_opt_str(v: Option<&Value>) -> Option<String> {
    let s = val_as_str(v);
    if s.is_empty() { None } else { Some(s) }
}

fn val_as_i64(v: Option<&Value>) -> Option<i64> {
    match v {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Some(Value::String(s)) => s.trim().parse::<i64>().ok(),
        Some(Value::Bool(b)) => Some(i64::from(*b)),
        _ => None,
    }
}

fn val_as_bool(v: Option<&Value>) -> bool {
    match v {
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_i64().is_some_and(|i| i != 0),
        Some(Value::String(s)) => matches!(s.to_ascii_lowercase().as_str(), "true" | "1" | "yes"),
        _ => false,
    }
}

/// Decode `google.protobuf.Any` `(type_url, value_bytes)`.
#[must_use]
pub fn decode_any_envelope(buf: &[u8]) -> (String, Vec<u8>) {
    let fm = pmap(buf);
    let type_url = fm_last_str(&fm, any_envelope::TYPE_URL, "");
    let value_bytes = fm_bytes(&fm, any_envelope::VALUE)
        .into_iter()
        .last()
        .map(<[u8]>::to_vec)
        .unwrap_or_default();
    (type_url, value_bytes)
}

/// Encode `google.protobuf.Any`.
#[must_use]
pub fn encode_any_envelope(type_url: &str, value_bytes: &[u8]) -> Vec<u8> {
    let mut out = encode_string_field(any_envelope::TYPE_URL, type_url);
    out.extend_from_slice(&encode_bytes_field(any_envelope::VALUE, value_bytes));
    out
}

fn classify_unified_diff_lines(unified_diff: &str) -> Vec<(i64, String)> {
    let lines: Vec<&str> = unified_diff.split('\n').collect();
    let mut in_header = lines
        .iter()
        .any(|ln| ln.starts_with("@@") || ln.starts_with("diff --git "))
        || (lines.len() >= 2
            && (lines[0].starts_with("--- ") || lines[0].starts_with("---\t"))
            && (lines[1].starts_with("+++ ") || lines[1].starts_with("+++\t")));

    let mut out = Vec::with_capacity(lines.len());
    for line in lines {
        if line.starts_with("diff --git ") {
            in_header = true;
            out.push((
                tool_tags::unified_diff_line::TYPE_UNSPECIFIED,
                line.to_owned(),
            ));
        } else if line.starts_with("@@") {
            in_header = false;
            out.push((
                tool_tags::unified_diff_line::TYPE_UNSPECIFIED,
                line.to_owned(),
            ));
        } else if in_header
            && (line.starts_with("--- ")
                || line.starts_with("---\t")
                || line.starts_with("+++ ")
                || line.starts_with("+++\t"))
        {
            if line.starts_with("+++ ") || line.starts_with("+++\t") {
                in_header = false;
            }
            out.push((
                tool_tags::unified_diff_line::TYPE_UNSPECIFIED,
                line.to_owned(),
            ));
        } else if line.starts_with("*** ") {
            if line.starts_with("*** Update File:")
                || line.starts_with("*** Add File:")
                || line.starts_with("*** Delete File:")
            {
                in_header = false;
            }
            out.push((
                tool_tags::unified_diff_line::TYPE_UNSPECIFIED,
                line.to_owned(),
            ));
        } else if let Some(rest) = line.strip_prefix('+') {
            in_header = false;
            out.push((tool_tags::unified_diff_line::TYPE_INSERT, rest.to_owned()));
        } else if let Some(rest) = line.strip_prefix('-') {
            in_header = false;
            out.push((tool_tags::unified_diff_line::TYPE_DELETE, rest.to_owned()));
        } else if let Some(rest) = line.strip_prefix(' ') {
            in_header = false;
            out.push((
                tool_tags::unified_diff_line::TYPE_UNCHANGED,
                rest.to_owned(),
            ));
        } else {
            out.push((
                tool_tags::unified_diff_line::TYPE_UNSPECIFIED,
                line.to_owned(),
            ));
        }
    }
    out
}

fn compute_diff_stats(unified_diff: &str) -> (i64, i64) {
    let classified = classify_unified_diff_lines(unified_diff);
    let mut adds = 0i64;
    let mut dels = 0i64;
    for (lt, _) in classified {
        if lt == tool_tags::unified_diff_line::TYPE_INSERT {
            adds += 1;
        } else if lt == tool_tags::unified_diff_line::TYPE_DELETE {
            dels += 1;
        }
    }
    (adds, dels)
}

#[must_use]
pub fn decode_unified_diff_block(diff_block_bytes: &[u8]) -> String {
    let mut lines_out = Vec::new();
    for u_raw in fm_bytes(&pmap(diff_block_bytes), tool_tags::diff_block::UNIFIED_DIFF) {
        for l_raw in fm_bytes(&pmap(u_raw), tool_tags::unified_diff::LINES) {
            let lfm = pmap(l_raw);
            let l_text = fm_last_str(&lfm, tool_tags::unified_diff_line::TEXT, "");
            let l_type =
                fm_last_varint(&lfm, tool_tags::unified_diff_line::TYPE).unwrap_or(0) as i64;
            let prefix = if l_type == tool_tags::unified_diff_line::TYPE_INSERT {
                "+"
            } else if l_type == tool_tags::unified_diff_line::TYPE_DELETE {
                "-"
            } else if l_type == tool_tags::unified_diff_line::TYPE_UNCHANGED {
                " "
            } else {
                ""
            };
            lines_out.push(format!("{prefix}{l_text}"));
        }
    }
    lines_out.join("\n")
}

#[must_use]
pub fn encode_unified_diff_block(unified_diff: &str) -> Vec<u8> {
    if unified_diff.is_empty() {
        return Vec::new();
    }
    let mut ud_buf = Vec::new();
    for (l_type, l_text) in classify_unified_diff_lines(unified_diff) {
        let mut line_msg = Vec::new();
        if !l_text.is_empty() {
            line_msg.extend_from_slice(&encode_string_field(
                tool_tags::unified_diff_line::TEXT,
                &l_text,
            ));
        }
        if l_type != 0 {
            line_msg.extend_from_slice(&encode_varint_field(
                tool_tags::unified_diff_line::TYPE,
                l_type,
            ));
        }
        ud_buf.extend_from_slice(&encode_bytes_field(
            tool_tags::unified_diff::LINES,
            &line_msg,
        ));
    }
    encode_bytes_field(tool_tags::diff_block::UNIFIED_DIFF, &ud_buf)
}

fn encode_any_read_file(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::ReadFile {
        path,
        start_line,
        end_line,
        file_num_lines,
        file_num_bytes,
        ..
    } = p
    else {
        return Vec::new();
    };
    let mut out = Vec::new();
    if let Some(fl) = file_num_lines.filter(|&v| v != 0) {
        out.extend_from_slice(&encode_varint_field(
            tool_tags::read_file::FILE_NUM_LINES,
            fl,
        ));
    }
    if let Some(fb) = file_num_bytes.filter(|&v| v != 0) {
        out.extend_from_slice(&encode_varint_field(
            tool_tags::read_file::FILE_NUM_BYTES,
            fb,
        ));
    }
    out.extend_from_slice(&encode_string_field(
        tool_tags::read_file::PATH,
        &normalize_tool_file_path(path),
    ));
    if let Some(sl) = start_line {
        out.extend_from_slice(&encode_varint_field(tool_tags::read_file::START_LINE, *sl));
    }
    if let Some(el) = end_line {
        out.extend_from_slice(&encode_varint_field(tool_tags::read_file::END_LINE, *el));
    }
    out
}

fn decode_any_read_file(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let fallback_path = val_as_str(first_present(args, &["Path", "path"]));
    let path = fm_last_str(&fm, tool_tags::read_file::PATH, &fallback_path);
    let start_line = fm_last_varint(&fm, tool_tags::read_file::START_LINE)
        .map(|v| i64::from(signed_int32(v)))
        .or_else(|| val_as_i64(first_present(args, &["StartLine", "start_line"])));
    let end_line = fm_last_varint(&fm, tool_tags::read_file::END_LINE)
        .map(|v| i64::from(signed_int32(v)))
        .or_else(|| val_as_i64(first_present(args, &["EndLine", "end_line"])));
    let byte_offset = val_as_i64(first_present(args, &["ByteOffset", "byte_offset"]));
    let file_num_lines = fm_last_varint(&fm, tool_tags::read_file::FILE_NUM_LINES)
        .map(|v| v as i64)
        .or_else(|| val_as_i64(args.get("file_num_lines")));
    let file_num_bytes = fm_last_varint(&fm, tool_tags::read_file::FILE_NUM_BYTES)
        .map(|v| v as i64)
        .or_else(|| val_as_i64(args.get("file_num_bytes")));
    let content_excerpt = args
        .get("content_excerpt")
        .and_then(|v| v.as_str().map(str::to_owned));
    CanonicalToolPayload::ReadFile {
        path: normalize_tool_file_path(&path).into_owned(),
        start_line,
        end_line,
        byte_offset,
        file_num_lines,
        file_num_bytes,
        content_excerpt,
    }
}

fn encode_any_file_edit(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::FileEdit {
        path,
        edit_op,
        unified_diff,
        diff_stats,
        is_artifact,
        artifact_summary,
    } = p
    else {
        return Vec::new();
    };
    let mut buf = Vec::new();
    match edit_op {
        FileEditOp::Create | FileEditOp::Add => {
            buf.extend_from_slice(&encode_uvarint_field(
                tool_tags::file_edit::KIND,
                tool_tags::file_edit::KIND_CREATE,
            ));
        }
        FileEditOp::Modify => {
            buf.extend_from_slice(&encode_uvarint_field(
                tool_tags::file_edit::KIND,
                tool_tags::file_edit::KIND_EDIT,
            ));
        }
        FileEditOp::Delete => {}
    }
    buf.extend_from_slice(&encode_string_field(
        tool_tags::file_edit::PATH,
        &normalize_tool_file_path(path),
    ));
    if !unified_diff.is_empty() {
        let diff_block = encode_unified_diff_block(unified_diff);
        if !diff_block.is_empty() {
            buf.extend_from_slice(&encode_bytes_field(tool_tags::file_edit::DIFF, &diff_block));
        }
    }
    let (adds, dels) = *diff_stats;
    if adds != 0 || dels != 0 {
        let mut stats = encode_varint_field(tool_tags::diff_stats::ADDITIONS, adds);
        stats.extend_from_slice(&encode_varint_field(tool_tags::diff_stats::DELETIONS, dels));
        buf.extend_from_slice(&encode_bytes_field(
            tool_tags::file_edit::DIFF_STATS,
            &stats,
        ));
    }
    if let Some(summary) = artifact_summary.as_deref().filter(|s| !s.is_empty()) {
        let meta = encode_string_field(tool_tags::artifact_meta::SUMMARY, summary);
        buf.extend_from_slice(&encode_bytes_field(
            tool_tags::file_edit::ARTIFACT_METADATA,
            &meta,
        ));
    }
    if *is_artifact {
        buf.extend_from_slice(&encode_uvarint_field(
            tool_tags::file_edit::IS_ARTIFACT_FILE,
            1,
        ));
    }
    buf
}

fn decode_any_file_edit(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let fallback_path = val_as_str(first_present(args, &["Path", "path"]));
    let mut path = fm_last_str(&fm, tool_tags::file_edit::PATH, &fallback_path);

    let prior_op_raw = val_as_str(first_present(args, &["edit_op", "EditOp"])).to_ascii_uppercase();
    let prior_op = if prior_op_raw == "ADD" {
        "CREATE"
    } else {
        prior_op_raw.as_str()
    };
    let mut edit_op = match prior_op {
        "CREATE" => FileEditOp::Create,
        "DELETE" => FileEditOp::Delete,
        _ => FileEditOp::Modify,
    };
    if let Some(k_val) = fm_last_varint(&fm, tool_tags::file_edit::KIND) {
        edit_op = if k_val == tool_tags::file_edit::KIND_CREATE {
            FileEditOp::Create
        } else if k_val == 0 {
            FileEditOp::Delete
        } else {
            FileEditOp::Modify
        };
    } else if !fm.is_empty() && (prior_op == "DELETE" || prior_op.is_empty()) {
        edit_op = FileEditOp::Delete;
    }

    let mut unified_diff = val_as_str(args.get("unified_diff"));
    if unified_diff.is_empty()
        && (args.contains_key("Target")
            || args.contains_key("Replacement")
            || args.contains_key("Content"))
    {
        if args.contains_key("Content") && !args.contains_key("Target") {
            if prior_op.is_empty() {
                edit_op = FileEditOp::Create;
            }
            let content = val_as_str(args.get("Content"));
            unified_diff = content
                .lines()
                .map(|l| format!("+{l}"))
                .collect::<Vec<_>>()
                .join("\n");
        } else {
            let mut lines = Vec::new();
            for l in val_as_str(args.get("Target")).lines() {
                lines.push(format!("-{l}"));
            }
            for l in val_as_str(args.get("Replacement")).lines() {
                lines.push(format!("+{l}"));
            }
            unified_diff = lines.join("\n");
        }
    }
    let diff_raws = fm_bytes(&fm, tool_tags::file_edit::DIFF);
    if let Some(last_diff) = diff_raws.last() {
        unified_diff = decode_unified_diff_block(last_diff);
    }

    let (mut adds, mut dels) = match args.get("diff_stats") {
        Some(Value::Array(arr)) if arr.len() == 2 => (
            val_as_i64(arr.first()).unwrap_or(0),
            val_as_i64(arr.get(1)).unwrap_or(0),
        ),
        _ => (0, 0),
    };
    let mut saw_stats = false;
    for s_raw in fm_bytes(&fm, tool_tags::file_edit::DIFF_STATS) {
        saw_stats = true;
        let sfm = pmap(s_raw);
        if let Some(v) = fm_last_varint(&sfm, tool_tags::diff_stats::ADDITIONS) {
            adds = i64::from(signed_int32(v));
        }
        if let Some(v) = fm_last_varint(&sfm, tool_tags::diff_stats::DELETIONS) {
            dels = i64::from(signed_int32(v));
        }
    }

    let mut is_artifact = if fm.contains_key(&tool_tags::file_edit::IS_ARTIFACT_FILE) {
        fm_last_varint(&fm, tool_tags::file_edit::IS_ARTIFACT_FILE).is_some_and(|v| v != 0)
    } else {
        val_as_bool(first_present(args, &["IsArtifact", "is_artifact"]))
    };
    let mut art_sum = val_as_opt_str(first_present(
        args,
        &["ArtifactSummary", "artifact_summary"],
    ));
    if art_sum.is_none()
        && let Some(Value::Object(meta)) = args.get("ArtifactMetadata")
    {
        is_artifact = true;
        art_sum = val_as_opt_str(meta.get("Summary"));
    }
    for a_raw in fm_bytes(&fm, tool_tags::file_edit::ARTIFACT_METADATA) {
        art_sum = fm_opt_str(
            &pmap(a_raw),
            tool_tags::artifact_meta::SUMMARY,
            art_sum.as_deref(),
        );
    }

    if !unified_diff.is_empty() {
        let (first_hdr, rewritten) = rewrite_patch_headers(&unified_diff);
        if let Some((verb, cap_path)) = first_hdr {
            if path.is_empty() {
                path = cap_path;
            }
            edit_op = match verb {
                "Add" => FileEditOp::Create,
                "Delete" => FileEditOp::Delete,
                _ => FileEditOp::Modify,
            };
            unified_diff = rewritten;
        }
    }
    if !saw_stats && adds == 0 && dels == 0 && !unified_diff.is_empty() {
        (adds, dels) = compute_diff_stats(&unified_diff);
    }
    CanonicalToolPayload::FileEdit {
        path: normalize_tool_file_path(&path).into_owned(),
        edit_op,
        unified_diff,
        diff_stats: (adds, dels),
        is_artifact,
        artifact_summary: art_sum,
    }
}

fn encode_any_run_command(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::RunCommand {
        command_line,
        cwd,
        exit_code,
        timed_out,
        shell_name,
        ran_in_sandbox,
        sandbox_policy,
        ..
    } = p
    else {
        return Vec::new();
    };
    let mut sb = Vec::new();
    if *ran_in_sandbox {
        sb.extend_from_slice(&encode_uvarint_field(
            tool_tags::sandbox_details::RAN_IN_SANDBOX,
            1,
        ));
    }
    if let Some(pol) = sandbox_policy.as_deref().filter(|s| !s.is_empty()) {
        sb.extend_from_slice(&encode_string_field(
            tool_tags::sandbox_details::SANDBOX_ERRORS,
            pol,
        ));
    }
    let mut out = match exit_code {
        Some(ec) => encode_varint_field(tool_tags::run_command::EXIT_CODE, *ec),
        None => encode_uvarint_field(tool_tags::run_command::EXIT_CODE_UNKNOWN, 1),
    };
    if *timed_out {
        out.extend_from_slice(&encode_uvarint_field(tool_tags::run_command::TIMED_OUT, 1));
    }
    if !sb.is_empty() {
        out.extend_from_slice(&encode_bytes_field(
            tool_tags::run_command::SANDBOX_DETAILS,
            &sb,
        ));
    }
    if let Some(sh) = shell_name.as_deref().filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_string_field(tool_tags::run_command::SHELL_NAME, sh));
    }
    out.extend_from_slice(&encode_string_field(
        tool_tags::run_command::COMMAND_LINE,
        command_line,
    ));
    out.extend_from_slice(&encode_string_field(
        tool_tags::run_command::CWD,
        &normalize_tool_file_path(cwd),
    ));
    out
}

fn decode_any_run_command(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let cmd = fm_last_str(
        &fm,
        tool_tags::run_command::COMMAND_LINE,
        &val_as_str(first_present(
            args,
            &["CommandLine", "command_line", "cmd", "command"],
        )),
    );
    let cwd = fm_last_str(
        &fm,
        tool_tags::run_command::CWD,
        &val_as_str(first_present(args, &["Cwd", "cwd", "workdir"])),
    );
    let shell_name = fm_opt_str(
        &fm,
        tool_tags::run_command::SHELL_NAME,
        val_as_opt_str(args.get("shell_name")).as_deref(),
    );
    let timed_out = if fm.contains_key(&tool_tags::run_command::TIMED_OUT) {
        fm_last_varint(&fm, tool_tags::run_command::TIMED_OUT).is_some_and(|v| v != 0)
    } else {
        val_as_bool(args.get("timed_out"))
    };
    let mut ran_sb = val_as_bool(args.get("ran_in_sandbox"));
    let mut sb_pol = val_as_opt_str(args.get("sandbox_policy"));
    for sb_raw in fm_bytes(&fm, tool_tags::run_command::SANDBOX_DETAILS) {
        let sbfm = pmap(sb_raw);
        if sbfm.contains_key(&tool_tags::sandbox_details::RAN_IN_SANDBOX) {
            ran_sb = fm_last_varint(&sbfm, tool_tags::sandbox_details::RAN_IN_SANDBOX)
                .is_some_and(|v| v != 0);
        }
        sb_pol = fm_opt_str(
            &sbfm,
            tool_tags::sandbox_details::SANDBOX_ERRORS,
            sb_pol.as_deref(),
        );
    }
    let mut exit_code = val_as_i64(args.get("exit_code"));
    if let Some(ec_raw) = fm_last_varint(&fm, tool_tags::run_command::EXIT_CODE) {
        exit_code = Some(i64::from(signed_int32(ec_raw)));
    }
    if fm_last_varint(&fm, tool_tags::run_command::EXIT_CODE_UNKNOWN).is_some_and(|v| v != 0) {
        exit_code = None;
    } else if exit_code.is_none() && !fm.is_empty() && (3..=7).any(|t| fm.contains_key(&t)) {
        exit_code = Some(0);
    }
    CanonicalToolPayload::RunCommand {
        command_line: cmd,
        cwd: normalize_tool_file_path(&cwd).into_owned(),
        exit_code,
        timed_out,
        is_daemon: val_as_bool(first_present(args, &["IsDaemon", "is_daemon"])),
        shell_name,
        ran_in_sandbox: ran_sb,
        sandbox_policy: sb_pol,
    }
}

fn encode_any_search_web(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::SearchWeb {
        query,
        domain_filter,
        documents,
        summary,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::search_web::QUERY, query);
    if let Some(dom) = domain_filter.as_deref().filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_string_field(tool_tags::search_web::DOMAIN, dom));
    }
    for d in documents {
        let mut dm = encode_string_field(tool_tags::search_doc::SNIPPET, &d.snippet);
        dm.extend_from_slice(&encode_string_field(tool_tags::search_doc::URL, &d.url));
        dm.extend_from_slice(&encode_string_field(tool_tags::search_doc::TITLE, &d.title));
        out.extend_from_slice(&encode_bytes_field(
            tool_tags::search_web::WEB_DOCUMENTS,
            &dm,
        ));
    }
    if let Some(sum) = summary.as_deref().filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_string_field(tool_tags::search_web::SUMMARY, sum));
    }
    out
}

fn decode_any_search_web(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let query = fm_last_str(
        &fm,
        tool_tags::search_web::QUERY,
        &val_as_str(first_present(args, &["query", "q"])),
    );
    let domain_filter = fm_opt_str(
        &fm,
        tool_tags::search_web::DOMAIN,
        val_as_opt_str(first_present(args, &["domain", "domain_filter"])).as_deref(),
    );
    let summary = fm_opt_str(
        &fm,
        tool_tags::search_web::SUMMARY,
        val_as_opt_str(args.get("summary")).as_deref(),
    );
    let documents = if fm.contains_key(&tool_tags::search_web::WEB_DOCUMENTS) {
        fm_bytes(&fm, tool_tags::search_web::WEB_DOCUMENTS)
            .into_iter()
            .map(|d_raw| {
                let dfm = pmap(d_raw);
                let snippet = {
                    let s = fm_last_str(&dfm, tool_tags::search_doc::SNIPPET, "");
                    if s.is_empty() {
                        fm_last_str(&dfm, tool_tags::search_doc::FALLBACK_TEXT, "")
                    } else {
                        s
                    }
                };
                SearchWebDocument {
                    title: fm_last_str(&dfm, tool_tags::search_doc::TITLE, ""),
                    url: fm_last_str(&dfm, tool_tags::search_doc::URL, ""),
                    snippet,
                }
            })
            .collect()
    } else if let Some(Value::Array(arr)) = args.get("documents") {
        arr.iter()
            .filter_map(|v| v.as_object())
            .map(|obj| SearchWebDocument {
                title: val_as_str(obj.get("title")),
                url: val_as_str(obj.get("url")),
                snippet: val_as_str(obj.get("snippet")),
            })
            .collect()
    } else {
        Vec::new()
    };
    CanonicalToolPayload::SearchWeb {
        query,
        domain_filter,
        documents,
        summary,
    }
}

fn encode_any_invoke_subagent(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::InvokeSubagent {
        subagent_type,
        role,
        prompt,
        spawned_conversation_id,
        workspace_mode,
        task_mode,
    } = p
    else {
        return Vec::new();
    };
    let mut sub = encode_string_field(tool_tags::subagent_spec::TYPE_NAME, subagent_type);
    sub.extend_from_slice(&encode_string_field(tool_tags::subagent_spec::ROLE, role));
    sub.extend_from_slice(&encode_string_field(
        tool_tags::subagent_spec::PROMPT,
        prompt,
    ));
    match workspace_mode {
        SubagentWorkspaceMode::Branch => {
            sub.extend_from_slice(&encode_string_field(
                tool_tags::subagent_spec::BRANCH_WORKSPACE,
                "branch",
            ));
        }
        SubagentWorkspaceMode::Share => {
            sub.extend_from_slice(&encode_string_field(
                tool_tags::subagent_spec::SHARE_WORKSPACE,
                "share",
            ));
        }
        SubagentWorkspaceMode::Inherit => {
            sub.extend_from_slice(&encode_uvarint_field(
                tool_tags::subagent_spec::INHERIT_WORKSPACE,
                1,
            ));
        }
    }
    let mut out = encode_bytes_field(tool_tags::invoke_subagent::SUBAGENTS, &sub);
    if let Some(cid) = spawned_conversation_id.as_deref().filter(|s| !s.is_empty()) {
        let res = encode_string_field(tool_tags::subagent_result::CONVERSATION_ID, cid);
        out.extend_from_slice(&encode_bytes_field(
            tool_tags::invoke_subagent::RESULTS,
            &res,
        ));
    }
    if *task_mode {
        out.extend_from_slice(&encode_uvarint_field(
            tool_tags::invoke_subagent::TASK_MODE,
            1,
        ));
    }
    out
}

fn decode_any_invoke_subagent(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let mut sub_type = val_as_str(first_present(
        args,
        &["TypeName", "subagent_type", "agent_type"],
    ));
    let mut role = val_as_str(first_present(args, &["Role", "role"]));
    let mut prompt = val_as_str(first_present(args, &["Prompt", "prompt", "message"]));
    let mut ws_mode =
        match val_as_str(first_present(args, &["Workspace", "workspace_mode"])).as_str() {
            "branch" => SubagentWorkspaceMode::Branch,
            "share" => SubagentWorkspaceMode::Share,
            _ => SubagentWorkspaceMode::Inherit,
        };
    for s_raw in fm_bytes(&fm, tool_tags::invoke_subagent::SUBAGENTS) {
        let sfm = pmap(s_raw);
        sub_type = fm_last_str(&sfm, tool_tags::subagent_spec::TYPE_NAME, &sub_type);
        role = fm_last_str(&sfm, tool_tags::subagent_spec::ROLE, &role);
        prompt = fm_last_str(&sfm, tool_tags::subagent_spec::PROMPT, &prompt);
        if sfm.contains_key(&tool_tags::subagent_spec::BRANCH_WORKSPACE) {
            ws_mode = SubagentWorkspaceMode::Branch;
        } else if sfm.contains_key(&tool_tags::subagent_spec::SHARE_WORKSPACE) {
            ws_mode = SubagentWorkspaceMode::Share;
        }
    }
    let mut spawned_cid = val_as_opt_str(args.get("spawned_conversation_id"));
    for r_raw in fm_bytes(&fm, tool_tags::invoke_subagent::RESULTS) {
        spawned_cid = fm_opt_str(
            &pmap(r_raw),
            tool_tags::subagent_result::CONVERSATION_ID,
            spawned_cid.as_deref(),
        );
    }
    let task_mode = if fm.contains_key(&tool_tags::invoke_subagent::TASK_MODE) {
        fm_last_varint(&fm, tool_tags::invoke_subagent::TASK_MODE).is_some_and(|v| v != 0)
    } else {
        val_as_bool(args.get("task_mode"))
    };
    CanonicalToolPayload::InvokeSubagent {
        subagent_type: sub_type,
        role,
        prompt,
        spawned_conversation_id: spawned_cid,
        workspace_mode: ws_mode,
        task_mode,
    }
}

fn encode_any_define_subagent(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::DefineSubagent {
        name,
        tool_names,
        agent_path,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::define_subagent::NAME, name);
    for t in tool_names {
        out.extend_from_slice(&encode_string_field(
            tool_tags::define_subagent::TOOL_NAMES,
            t,
        ));
    }
    if let Some(ap) = agent_path.as_deref().filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_string_field(
            tool_tags::define_subagent::AGENT_PATH,
            &normalize_tool_file_path(ap),
        ));
    }
    out
}

fn decode_any_define_subagent(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let name = fm_last_str(
        &fm,
        tool_tags::define_subagent::NAME,
        &val_as_str(first_present(args, &["Name", "name"])),
    );
    let agent_path = fm_opt_str(
        &fm,
        tool_tags::define_subagent::AGENT_PATH,
        val_as_opt_str(first_present(args, &["AgentPath", "agent_path"])).as_deref(),
    )
    .map(|p| normalize_tool_file_path(&p).into_owned());
    let tool_names = if fm.contains_key(&tool_tags::define_subagent::TOOL_NAMES) {
        fm_strs(&fm, tool_tags::define_subagent::TOOL_NAMES)
    } else if let Some(Value::Array(arr)) = first_present(args, &["ToolNames", "tool_names"]) {
        arr.iter().map(|v| val_as_str(Some(v))).collect()
    } else {
        Vec::new()
    };
    CanonicalToolPayload::DefineSubagent {
        name,
        tool_names,
        agent_path,
    }
}

fn encode_any_manage_subagents(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::ManageSubagents {
        action,
        target_conversation_ids,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::manage_subagents::ACTION, action.as_str());
    for cid in target_conversation_ids {
        if *action == ManageSubagentsAction::List {
            let item = encode_string_field(tool_tags::subagent_state::CONVERSATION_ID, cid);
            out.extend_from_slice(&encode_bytes_field(
                tool_tags::manage_subagents::SUBAGENTS,
                &item,
            ));
        } else {
            out.extend_from_slice(&encode_string_field(
                tool_tags::manage_subagents::CONVERSATION_IDS,
                cid,
            ));
        }
    }
    out
}

fn decode_any_manage_subagents(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let raw_action = fm_last_str(
        &fm,
        tool_tags::manage_subagents::ACTION,
        &val_as_opt_str(first_present(args, &["Action", "action"]))
            .unwrap_or_else(|| "list".to_owned()),
    );
    let mut wire_cids = Vec::new();
    for cid in fm_strs(&fm, tool_tags::manage_subagents::CONVERSATION_IDS) {
        if !cid.is_empty() && !wire_cids.contains(&cid) {
            wire_cids.push(cid);
        }
    }
    for s_raw in fm_bytes(&fm, tool_tags::manage_subagents::SUBAGENTS) {
        let cid = fm_last_str(&pmap(s_raw), tool_tags::subagent_state::CONVERSATION_ID, "");
        if !cid.is_empty() && !wire_cids.contains(&cid) {
            wire_cids.push(cid);
        }
    }
    let has_wire = fm.contains_key(&tool_tags::manage_subagents::CONVERSATION_IDS)
        || fm.contains_key(&tool_tags::manage_subagents::SUBAGENTS);
    let mut cids = if has_wire {
        wire_cids
    } else if let Some(Value::Array(arr)) =
        first_present(args, &["ConversationIds", "target_conversation_ids", "ids"])
    {
        arr.iter().map(|v| val_as_str(Some(v))).collect()
    } else {
        Vec::new()
    };
    if cids.is_empty()
        && !has_wire
        && let Some(single) = val_as_opt_str(first_present(args, &["id", "agent_id"]))
    {
        cids.push(single);
    }
    let action = match raw_action.as_str() {
        "kill" => ManageSubagentsAction::Kill,
        "kill_all" => ManageSubagentsAction::KillAll,
        _ => ManageSubagentsAction::List,
    };
    CanonicalToolPayload::ManageSubagents {
        action,
        target_conversation_ids: cids,
    }
}

fn encode_any_manage_task(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::ManageTask {
        action,
        task_id,
        input_text,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::manage_task::ACTION, action.as_str());
    if let Some(tid) = task_id.as_deref().filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_string_field(tool_tags::manage_task::TASK_IDS, tid));
    }
    if let Some(inp) = input_text.as_deref().filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_string_field(tool_tags::manage_task::INPUT, inp));
    }
    out
}

fn decode_any_manage_task(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let raw_action = fm_last_str(
        &fm,
        tool_tags::manage_task::ACTION,
        &val_as_opt_str(first_present(args, &["Action", "action"]))
            .unwrap_or_else(|| "status".to_owned()),
    );
    let wire_ids = fm_strs(&fm, tool_tags::manage_task::TASK_IDS);
    let task_id = val_as_opt_str(first_present(args, &["TaskId", "task_id", "session_id"]))
        .or_else(|| wire_ids.into_iter().next());
    let input_text = fm_opt_str(
        &fm,
        tool_tags::manage_task::INPUT,
        val_as_opt_str(first_present(args, &["Input", "input_text", "chars"])).as_deref(),
    );
    let action = match raw_action.as_str() {
        "list" => ManageTaskAction::List,
        "send_input" => ManageTaskAction::SendInput,
        "kill" => ManageTaskAction::Kill,
        _ => ManageTaskAction::Status,
    };
    CanonicalToolPayload::ManageTask {
        action,
        task_id,
        input_text,
    }
}

fn encode_any_send_message(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::SendMessage {
        recipient_id,
        message_body,
        recipient_name,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::send_message::RECIPIENT, recipient_id);
    if let Some(rn) = recipient_name.as_deref().filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_string_field(
            tool_tags::send_message::RECIPIENT_NAME,
            rn,
        ));
    }
    out.extend_from_slice(&encode_string_field(
        tool_tags::send_message::MESSAGE,
        message_body,
    ));
    out
}

fn decode_any_send_message(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let recipient_id = fm_last_str(
        &fm,
        tool_tags::send_message::RECIPIENT,
        &val_as_str(first_present(
            args,
            &["Recipient", "recipient_id", "recipient", "to", "id"],
        )),
    );
    let message_body = fm_last_str(
        &fm,
        tool_tags::send_message::MESSAGE,
        &val_as_str(first_present(
            args,
            &["Message", "message_body", "message", "prompt"],
        )),
    );
    let recipient_name = fm_opt_str(
        &fm,
        tool_tags::send_message::RECIPIENT_NAME,
        val_as_opt_str(first_present(args, &["recipient_name", "reply_to"])).as_deref(),
    );
    CanonicalToolPayload::SendMessage {
        recipient_id,
        message_body,
        recipient_name,
    }
}

fn encode_any_schedule(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::Schedule {
        cron_expression,
        max_iterations,
        ..
    } = p
    else {
        return Vec::new();
    };
    let mode = if cron_expression.is_some() {
        "cron"
    } else {
        "timer"
    };
    let mut out = encode_string_field(tool_tags::schedule::MODE, mode);
    if let Some(iters) = max_iterations {
        out.extend_from_slice(&encode_varint_field(
            tool_tags::schedule::ITERATIONS,
            *iters,
        ));
    }
    out
}

fn decode_any_schedule(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let duration_seconds = val_as_i64(first_present(
        args,
        &["DurationSeconds", "duration_seconds"],
    ));
    let cron_expression =
        val_as_opt_str(first_present(args, &["CronExpression", "cron_expression"]));
    let max_iterations = val_as_i64(first_present(args, &["MaxIterations", "max_iterations"]))
        .or_else(|| fm_last_varint(&fm, tool_tags::schedule::ITERATIONS).map(signed_int64));
    CanonicalToolPayload::Schedule {
        prompt: val_as_str(first_present(args, &["Prompt", "prompt"])),
        duration_seconds,
        cron_expression,
        timer_condition: val_as_opt_str(first_present(
            args,
            &["TimerCondition", "timer_condition"],
        ))
        .unwrap_or_else(|| "never".to_owned()),
        is_daemon: val_as_bool(first_present(args, &["IsDaemon", "is_daemon"])),
        exact_timing: val_as_bool(first_present(args, &["ExactTiming", "exact_timing"])),
        max_iterations,
    }
}

fn encode_any_ask_question(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::AskQuestion { questions } = p else {
        return Vec::new();
    };
    let mut buf = Vec::new();
    for q in questions {
        let mut q_msg = encode_string_field(tool_tags::ask_question_entry::QUESTION, &q.question);
        for o in &q.options {
            let oid = if o.description.is_empty() {
                &o.label
            } else {
                &o.description
            };
            let mut om = encode_string_field(tool_tags::question_option::ID, oid);
            om.extend_from_slice(&encode_string_field(
                tool_tags::question_option::TEXT,
                &o.label,
            ));
            q_msg.extend_from_slice(&encode_bytes_field(
                tool_tags::ask_question_entry::OPTIONS,
                &om,
            ));
        }
        if q.is_multi_select {
            q_msg.extend_from_slice(&encode_uvarint_field(
                tool_tags::ask_question_entry::IS_MULTI_SELECT,
                1,
            ));
        }
        for a in &q.user_answers {
            q_msg.extend_from_slice(&encode_string_field(
                tool_tags::ask_question_entry::USER_ANSWERS,
                a,
            ));
        }
        buf.extend_from_slice(&encode_bytes_field(
            tool_tags::ask_question::QUESTIONS,
            &q_msg,
        ));
    }
    buf
}

fn decode_any_ask_question(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let ans_by_idx = args
        .get("user_answers_by_index")
        .and_then(|v| v.as_array())
        .map(Vec::as_slice)
        .unwrap_or(&[]);

    if fm.contains_key(&tool_tags::ask_question::QUESTIONS) {
        let mut qs = Vec::new();
        for (idx, q_raw) in fm_bytes(&fm, tool_tags::ask_question::QUESTIONS)
            .into_iter()
            .enumerate()
        {
            let qfm = pmap(q_raw);
            let options = fm_bytes(&qfm, tool_tags::ask_question_entry::OPTIONS)
                .into_iter()
                .map(|o_raw| {
                    let ofm = pmap(o_raw);
                    let oid = fm_last_str(&ofm, tool_tags::question_option::ID, "");
                    let txt = fm_last_str(&ofm, tool_tags::question_option::TEXT, "");
                    QuestionOption {
                        label: if txt.is_empty() {
                            oid.clone()
                        } else {
                            txt.clone()
                        },
                        description: if oid == txt { String::new() } else { oid },
                    }
                })
                .collect();
            let mut ans = fm_strs(&qfm, tool_tags::ask_question_entry::USER_ANSWERS);
            ans.extend(fm_strs(&qfm, tool_tags::ask_question_entry::ALT_ANSWERS));
            if ans.is_empty()
                && let Some(Value::Array(arr)) = ans_by_idx.get(idx)
            {
                ans = arr.iter().map(|v| val_as_str(Some(v))).collect();
            }
            qs.push(QuestionItem {
                question: fm_last_str(&qfm, tool_tags::ask_question_entry::QUESTION, ""),
                options,
                is_multi_select: fm_last_varint(
                    &qfm,
                    tool_tags::ask_question_entry::IS_MULTI_SELECT,
                )
                .is_some_and(|v| v != 0),
                user_answers: ans,
            });
        }
        return CanonicalToolPayload::AskQuestion { questions: qs };
    }

    let mut qs = Vec::new();
    if let Some(Value::Array(raw_qs)) = first_present(args, &["Questions", "questions"]) {
        for (idx, q_val) in raw_qs.iter().enumerate() {
            let Some(q_obj) = q_val.as_object() else {
                continue;
            };
            let q_map: BTreeMap<String, Value> =
                q_obj.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
            let raw_opts = q_obj
                .get("options")
                .or_else(|| q_obj.get("Options"))
                .and_then(|v| v.as_array());
            let options = raw_opts
                .map(|arr| {
                    arr.iter()
                        .map(|o| {
                            if let Some(oo) = o.as_object() {
                                let label = val_as_str(oo.get("label").or_else(|| oo.get("text")));
                                let description = val_as_str(oo.get("description"));
                                QuestionOption { label, description }
                            } else {
                                QuestionOption {
                                    label: val_as_str(Some(o)),
                                    description: String::new(),
                                }
                            }
                        })
                        .collect()
                })
                .unwrap_or_default();
            let fb_ans = if let Some(Value::Array(arr)) = ans_by_idx.get(idx) {
                arr.iter().map(|v| val_as_str(Some(v))).collect()
            } else if let Some(Value::Array(arr)) = q_obj.get("user_answers") {
                arr.iter().map(|v| val_as_str(Some(v))).collect()
            } else {
                Vec::new()
            };
            qs.push(QuestionItem {
                question: val_as_str(first_present(&q_map, &["Question", "question"])),
                options,
                is_multi_select: val_as_bool(first_present(
                    &q_map,
                    &["MultiSelect", "is_multi_select"],
                )),
                user_answers: fb_ans,
            });
        }
    }
    CanonicalToolPayload::AskQuestion { questions: qs }
}

fn encode_any_list_resources(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::ListResources {
        server_name,
        resource_uris,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::list_resources::SERVER_NAME, server_name);
    for u in resource_uris {
        let rm = encode_string_field(tool_tags::mcp_resource::URI, u);
        out.extend_from_slice(&encode_bytes_field(
            tool_tags::list_resources::RESOURCES,
            &rm,
        ));
    }
    out
}

fn decode_any_list_resources(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let server_name = fm_last_str(
        &fm,
        tool_tags::list_resources::SERVER_NAME,
        &val_as_str(first_present(
            args,
            &["ServerName", "server_name", "server"],
        )),
    );
    let resource_uris = if fm.contains_key(&tool_tags::list_resources::RESOURCES) {
        fm_bytes(&fm, tool_tags::list_resources::RESOURCES)
            .into_iter()
            .filter_map(|r| {
                let u = fm_last_str(&pmap(r), tool_tags::mcp_resource::URI, "");
                if u.is_empty() { None } else { Some(u) }
            })
            .collect()
    } else if let Some(Value::Array(arr)) = args.get("resource_uris") {
        arr.iter().map(|v| val_as_str(Some(v))).collect()
    } else {
        Vec::new()
    };
    CanonicalToolPayload::ListResources {
        server_name,
        resource_uris,
    }
}

fn encode_any_read_resource(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::ReadResource {
        server_name,
        uri,
        contents_text,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::read_resource::SERVER_NAME, server_name);
    out.extend_from_slice(&encode_string_field(tool_tags::read_resource::URI, uri));
    for t in contents_text {
        let mut cm = encode_string_field(tool_tags::mcp_resource_content::URI, uri);
        let tc = encode_string_field(tool_tags::mcp_text_content::TEXT, t);
        cm.extend_from_slice(&encode_bytes_field(
            tool_tags::mcp_resource_content::TEXT_CONTENT,
            &tc,
        ));
        out.extend_from_slice(&encode_bytes_field(tool_tags::read_resource::CONTENTS, &cm));
    }
    out
}

fn decode_any_read_resource(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let server_name = fm_last_str(
        &fm,
        tool_tags::read_resource::SERVER_NAME,
        &val_as_str(first_present(
            args,
            &["ServerName", "server_name", "server"],
        )),
    );
    let uri = fm_last_str(
        &fm,
        tool_tags::read_resource::URI,
        &val_as_str(first_present(args, &["Uri", "uri"])),
    );
    let contents_text = if fm.contains_key(&tool_tags::read_resource::CONTENTS) {
        let mut texts = Vec::new();
        for c_raw in fm_bytes(&fm, tool_tags::read_resource::CONTENTS) {
            for tc in fm_bytes(&pmap(c_raw), tool_tags::mcp_resource_content::TEXT_CONTENT) {
                texts.push(fm_last_str(
                    &pmap(tc),
                    tool_tags::mcp_text_content::TEXT,
                    "",
                ));
            }
        }
        texts
    } else if let Some(Value::Array(arr)) = args.get("contents_text") {
        arr.iter().map(|v| val_as_str(Some(v))).collect()
    } else {
        Vec::new()
    };
    CanonicalToolPayload::ReadResource {
        server_name,
        uri,
        contents_text,
    }
}

fn encode_any_generate_image(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::GenerateImage {
        prompt,
        image_paths,
        aspect_ratio,
        image_uri,
        model_name,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::generate_image::PROMPT, prompt);
    for x in image_paths {
        out.extend_from_slice(&encode_string_field(
            tool_tags::generate_image::IMAGE_PATHS,
            &normalize_tool_file_path(x),
        ));
    }
    out.extend_from_slice(&encode_string_field(
        tool_tags::generate_image::ASPECT_RATIO,
        aspect_ratio,
    ));
    if let Some(u) = image_uri.as_deref().filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_string_field(
            tool_tags::generate_image::IMAGE_URI,
            u,
        ));
    }
    if let Some(m) = model_name.as_deref().filter(|s| !s.is_empty()) {
        out.extend_from_slice(&encode_string_field(
            tool_tags::generate_image::MODEL_NAME,
            m,
        ));
    }
    out
}

fn decode_any_generate_image(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let prompt = fm_last_str(
        &fm,
        tool_tags::generate_image::PROMPT,
        &val_as_str(first_present(args, &["Prompt", "prompt"])),
    );
    let aspect_ratio = fm_last_str(
        &fm,
        tool_tags::generate_image::ASPECT_RATIO,
        &val_as_opt_str(first_present(args, &["AspectRatio", "aspect_ratio"]))
            .unwrap_or_else(|| "1:1".to_owned()),
    );
    let paths = if fm.contains_key(&tool_tags::generate_image::IMAGE_PATHS) {
        fm_strs(&fm, tool_tags::generate_image::IMAGE_PATHS)
    } else if let Some(Value::Array(arr)) = first_present(args, &["ImagePaths", "image_paths"]) {
        arr.iter().map(|v| val_as_str(Some(v))).collect()
    } else {
        Vec::new()
    };
    CanonicalToolPayload::GenerateImage {
        prompt,
        image_paths: paths
            .into_iter()
            .map(|p| normalize_tool_file_path(&p).into_owned())
            .collect(),
        aspect_ratio,
        image_uri: fm_opt_str(
            &fm,
            tool_tags::generate_image::IMAGE_URI,
            val_as_opt_str(args.get("image_uri")).as_deref(),
        ),
        model_name: fm_opt_str(
            &fm,
            tool_tags::generate_image::MODEL_NAME,
            val_as_opt_str(args.get("model_name")).as_deref(),
        ),
    }
}

fn encode_any_moma_search(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::MomaSearch {
        query,
        result_count,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::moma_search::QUERY, query);
    let cnt = (*result_count).max(0) as usize;
    for i in 0..cnt {
        let rm = encode_string_field(2, &format!("result-{i}"));
        out.extend_from_slice(&encode_bytes_field(tool_tags::moma_search::RESULTS, &rm));
    }
    out
}

fn decode_any_moma_search(
    args: &BTreeMap<String, Value>,
    _raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let query = fm_last_str(
        &fm,
        tool_tags::moma_search::QUERY,
        &val_as_str(first_present(args, &["Query", "query"])),
    );
    let result_count = if fm.contains_key(&tool_tags::moma_search::RESULTS) {
        fm_bytes(&fm, tool_tags::moma_search::RESULTS).len() as i64
    } else {
        val_as_i64(args.get("result_count")).unwrap_or(0)
    };
    CanonicalToolPayload::MomaSearch {
        query,
        result_count,
    }
}

fn encode_any_finish(p: &CanonicalToolPayload) -> Vec<u8> {
    let CanonicalToolPayload::Finish {
        arguments_json,
        has_result_schema,
    } = p
    else {
        return Vec::new();
    };
    let mut out = encode_string_field(tool_tags::finish::ARGUMENTS_JSON, arguments_json);
    if *has_result_schema {
        out.extend_from_slice(&encode_uvarint_field(
            tool_tags::finish::HAS_RESULT_SCHEMA,
            1,
        ));
    }
    out
}

fn decode_any_finish(
    args: &BTreeMap<String, Value>,
    raw_json: &str,
    any_val: &[u8],
) -> CanonicalToolPayload {
    let fm = pmap(any_val);
    let has_result_schema = if fm.contains_key(&tool_tags::finish::HAS_RESULT_SCHEMA) {
        fm_last_varint(&fm, tool_tags::finish::HAS_RESULT_SCHEMA).is_some_and(|v| v != 0)
    } else {
        val_as_bool(args.get("has_result_schema"))
    };
    let fallback = if raw_json.is_empty() { "{}" } else { raw_json };
    CanonicalToolPayload::Finish {
        arguments_json: fm_last_str(&fm, tool_tags::finish::ARGUMENTS_JSON, fallback),
        has_result_schema,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CodexCallKind {
    FunctionCall,
    CustomToolCall,
    ToolSearchCall,
}

impl CodexCallKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::FunctionCall => "function_call",
            Self::CustomToolCall => "custom_tool_call",
            Self::ToolSearchCall => "tool_search_call",
        }
    }
}

type EncodeAnyFn = fn(&CanonicalToolPayload) -> Vec<u8>;
type DecodeAnyFn = fn(&BTreeMap<String, Value>, &str, &[u8]) -> CanonicalToolPayload;
type EncodeCodexFn =
    fn(&CanonicalToolPayload) -> (&'static str, &'static str, String, Option<String>);
type DecodeCodexFn = fn(&str, &BTreeMap<String, Value>, &str) -> CanonicalToolPayload;
type ExtractMetaFn = fn(&CanonicalToolPayload) -> BTreeMap<String, Value>;

/// Specification row for one of the 16 canonical tool payload codecs.
#[derive(Clone, Copy)]
pub struct ToolCodecSpec {
    pub payload_kind: &'static str,
    pub agy_tool_name: &'static str,
    pub agy_any_type_url: &'static str,
    pub codex_call_kind: &'static str,
    pub codex_tool_name: &'static str,
    pub codex_fallback_tool_names: &'static [&'static str],
    pub encode_agy_any: EncodeAnyFn,
    pub decode_agy_any: DecodeAnyFn,
    pub encode_codex: EncodeCodexFn,
    pub decode_codex: DecodeCodexFn,
    pub extract_result_meta: Option<ExtractMetaFn>,
}

fn synthesize_codex_patch_input(payload: &CanonicalToolPayload) -> String {
    let CanonicalToolPayload::FileEdit {
        path,
        edit_op,
        unified_diff,
        ..
    } = payload
    else {
        return String::new();
    };
    if unified_diff.contains("*** Begin Patch") {
        return rewrite_patch_headers(unified_diff).1;
    }
    let norm = normalize_tool_file_path(path);
    let norm_path = if norm.is_empty() {
        "patch_target"
    } else {
        norm.as_ref()
    };
    let verb = match edit_op {
        FileEditOp::Create | FileEditOp::Add => "Add",
        FileEditOp::Delete => "Delete",
        FileEditOp::Modify => "Update",
    };
    let body = unified_diff.trim_end_matches(['\r', '\n']);
    if body.is_empty() {
        format!("*** Begin Patch\n*** {verb} File: {norm_path}\n*** End Patch")
    } else {
        format!("*** Begin Patch\n*** {verb} File: {norm_path}\n{body}\n*** End Patch")
    }
}

fn rmeta_read_file(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::ReadFile {
        file_num_lines,
        file_num_bytes,
        content_excerpt,
        ..
    } = p
    {
        if let Some(v) = file_num_lines {
            m.insert("file_num_lines".into(), json!(v));
        }
        if let Some(v) = file_num_bytes {
            m.insert("file_num_bytes".into(), json!(v));
        }
        if let Some(v) = content_excerpt {
            m.insert("content_excerpt".into(), json!(v));
        }
    }
    m
}

fn rmeta_file_edit(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::FileEdit { diff_stats, .. } = p
        && *diff_stats != (0, 0)
    {
        m.insert("diff_stats".into(), json!([diff_stats.0, diff_stats.1]));
    }
    m
}

fn rmeta_run_command(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::RunCommand {
        exit_code,
        timed_out,
        ..
    } = p
    {
        m.insert("exit_code_known".into(), json!(exit_code.is_some()));
        if let Some(ec) = exit_code {
            m.insert("exit_code".into(), json!(ec));
        }
        if *timed_out {
            m.insert("timed_out".into(), json!(true));
        }
    }
    m
}

fn rmeta_search_web(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::SearchWeb {
        documents, summary, ..
    } = p
    {
        if !documents.is_empty() {
            m.insert(
                "documents".into(),
                Value::Array(
                    documents
                        .iter()
                        .map(|d| json!({"title": d.title, "url": d.url, "snippet": d.snippet}))
                        .collect(),
                ),
            );
        }
        if let Some(s) = summary {
            m.insert("summary".into(), json!(s));
        }
    }
    m
}

fn rmeta_invoke_subagent(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::InvokeSubagent {
        spawned_conversation_id: Some(cid),
        ..
    } = p
    {
        m.insert("spawned_conversation_id".into(), json!(cid));
    }
    m
}

fn rmeta_manage_subagents(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::ManageSubagents {
        action: ManageSubagentsAction::List,
        target_conversation_ids,
    } = p
        && !target_conversation_ids.is_empty()
    {
        m.insert(
            "target_conversation_ids".into(),
            json!(target_conversation_ids),
        );
    }
    m
}

fn rmeta_ask_question(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::AskQuestion { questions } = p
        && questions.iter().any(|q| !q.user_answers.is_empty())
    {
        let arr: Vec<Value> = questions.iter().map(|q| json!(q.user_answers)).collect();
        m.insert("user_answers_by_index".into(), Value::Array(arr));
    }
    m
}

fn rmeta_list_resources(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::ListResources { resource_uris, .. } = p
        && !resource_uris.is_empty()
    {
        m.insert("resource_uris".into(), json!(resource_uris));
    }
    m
}

fn rmeta_read_resource(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::ReadResource { contents_text, .. } = p
        && !contents_text.is_empty()
    {
        m.insert("contents_text".into(), json!(contents_text));
    }
    m
}

fn rmeta_generate_image(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::GenerateImage {
        image_uri: Some(uri),
        ..
    } = p
    {
        m.insert("image_uri".into(), json!(uri));
    }
    m
}

fn rmeta_moma_search(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::MomaSearch { result_count, .. } = p
        && *result_count != 0
    {
        m.insert("result_count".into(), json!(result_count));
    }
    m
}

fn rmeta_finish(p: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    if let CanonicalToolPayload::Finish {
        has_result_schema: true,
        ..
    } = p
    {
        m.insert("has_result_schema".into(), json!(true));
    }
    m
}

fn encode_codex_default(
    p: &CanonicalToolPayload,
    kind: &'static str,
    name: &'static str,
) -> (&'static str, &'static str, String, Option<String>) {
    let args = tool_payload_args_map(p);
    (kind, name, dump_canonical_json(&args), None)
}

fn encode_codex_file_edit(
    p: &CanonicalToolPayload,
) -> (&'static str, &'static str, String, Option<String>) {
    let args = tool_payload_args_map(p);
    (
        "custom_tool_call",
        "apply_patch",
        dump_canonical_json(&args),
        Some(synthesize_codex_patch_input(p)),
    )
}

fn encode_codex_run_command(
    p: &CanonicalToolPayload,
) -> (&'static str, &'static str, String, Option<String>) {
    let mut d = tool_payload_args_map(p);
    if let CanonicalToolPayload::RunCommand {
        command_line, cwd, ..
    } = p
    {
        d.insert("cmd".into(), json!(command_line));
        d.insert(
            "workdir".into(),
            json!(normalize_tool_file_path(cwd).as_ref()),
        );
    }
    (
        "function_call",
        "exec_command",
        dump_canonical_json(&d),
        None,
    )
}

fn encode_codex_manage_subagents(
    p: &CanonicalToolPayload,
) -> (&'static str, &'static str, String, Option<String>) {
    let args = tool_payload_args_map(p);
    let cname = match p {
        CanonicalToolPayload::ManageSubagents {
            action: ManageSubagentsAction::List,
            ..
        } => "list_agents",
        _ => "interrupt_agent",
    };
    ("function_call", cname, dump_canonical_json(&args), None)
}

fn decode_codex_manage_subagents(
    tool_name: &str,
    args: &BTreeMap<String, Value>,
    raw_json: &str,
) -> CanonicalToolPayload {
    let mut enriched = args.clone();
    if first_present(&enriched, &["Action", "action"]).is_none() {
        let act = match tool_name {
            "interrupt_agent" | "close_agent" => "kill",
            "kill_all_agents" => "kill_all",
            _ => "list",
        };
        enriched.insert("action".into(), json!(act));
    }
    decode_any_manage_subagents(&enriched, raw_json, &[])
}

fn encode_codex_manage_task(
    p: &CanonicalToolPayload,
) -> (&'static str, &'static str, String, Option<String>) {
    let args = tool_payload_args_map(p);
    let cname = match p {
        CanonicalToolPayload::ManageTask { action, .. } => match action {
            ManageTaskAction::Status => "wait",
            ManageTaskAction::SendInput => "send_input",
            ManageTaskAction::Kill => "kill_task",
            ManageTaskAction::List => "list_tasks",
        },
        _ => "wait",
    };
    ("function_call", cname, dump_canonical_json(&args), None)
}

fn decode_codex_manage_task(
    tool_name: &str,
    args: &BTreeMap<String, Value>,
    raw_json: &str,
) -> CanonicalToolPayload {
    let mut enriched = args.clone();
    if first_present(&enriched, &["Action", "action"]).is_none() {
        let act = match tool_name {
            "send_input" | "write_stdin" => "send_input",
            "kill_task" => "kill",
            "list_tasks" => "list",
            _ => "status",
        };
        enriched.insert("action".into(), json!(act));
    }
    decode_any_manage_task(&enriched, raw_json, &[])
}

fn encode_codex_finish(
    p: &CanonicalToolPayload,
) -> (&'static str, &'static str, String, Option<String>) {
    let args = match p {
        CanonicalToolPayload::Finish { arguments_json, .. } if !arguments_json.is_empty() => {
            arguments_json.clone()
        }
        _ => "{}".to_owned(),
    };
    ("function_call", "finish", args, None)
}

macro_rules! fn_enc_codex {
    ($fn_name:ident, $ck:expr, $cn:expr) => {
        fn $fn_name(
            p: &CanonicalToolPayload,
        ) -> (&'static str, &'static str, String, Option<String>) {
            encode_codex_default(p, $ck, $cn)
        }
    };
}

macro_rules! fn_dec_codex {
    ($fn_name:ident, $dec_any:path) => {
        fn $fn_name(
            _tool_name: &str,
            args: &BTreeMap<String, Value>,
            raw_json: &str,
        ) -> CanonicalToolPayload {
            $dec_any(args, raw_json, &[])
        }
    };
}

fn_enc_codex!(enc_cx_read_file, "function_call", "view_file");
fn_enc_codex!(enc_cx_search_web, "function_call", "web_search");
fn_enc_codex!(enc_cx_invoke_subagent, "function_call", "spawn_agent");
fn_enc_codex!(enc_cx_define_subagent, "function_call", "define_subagent");
fn_enc_codex!(enc_cx_send_message, "function_call", "send_message");
fn_enc_codex!(enc_cx_schedule, "function_call", "schedule");
fn_enc_codex!(enc_cx_ask_question, "function_call", "ask_question");
fn_enc_codex!(enc_cx_list_resources, "function_call", "list_mcp_resources");
fn_enc_codex!(enc_cx_read_resource, "function_call", "read_mcp_resource");
fn_enc_codex!(enc_cx_generate_image, "function_call", "generate_image");
fn_enc_codex!(enc_cx_moma_search, "function_call", "moma_search");

fn_dec_codex!(dec_cx_read_file, decode_any_read_file);
fn_dec_codex!(dec_cx_file_edit, decode_any_file_edit);
fn_dec_codex!(dec_cx_run_command, decode_any_run_command);
fn_dec_codex!(dec_cx_search_web, decode_any_search_web);
fn_dec_codex!(dec_cx_invoke_subagent, decode_any_invoke_subagent);
fn_dec_codex!(dec_cx_define_subagent, decode_any_define_subagent);
fn_dec_codex!(dec_cx_send_message, decode_any_send_message);
fn_dec_codex!(dec_cx_schedule, decode_any_schedule);
fn_dec_codex!(dec_cx_ask_question, decode_any_ask_question);
fn_dec_codex!(dec_cx_list_resources, decode_any_list_resources);
fn_dec_codex!(dec_cx_read_resource, decode_any_read_resource);
fn_dec_codex!(dec_cx_generate_image, decode_any_generate_image);
fn_dec_codex!(dec_cx_moma_search, decode_any_moma_search);
fn_dec_codex!(dec_cx_finish, decode_any_finish);

/// Authoritative 16-entry bidirectional tool codec table.
pub const TOOL_CODEC_TABLE: &[ToolCodecSpec] = &[
    ToolCodecSpec {
        payload_kind: "read_file",
        agy_tool_name: "read_file",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.ReadFile",
        codex_call_kind: "function_call",
        codex_tool_name: "view_file",
        codex_fallback_tool_names: &["exec_command"],
        encode_agy_any: encode_any_read_file,
        decode_agy_any: decode_any_read_file,
        encode_codex: enc_cx_read_file,
        decode_codex: dec_cx_read_file,
        extract_result_meta: Some(rmeta_read_file),
    },
    ToolCodecSpec {
        payload_kind: "file_edit",
        agy_tool_name: "edit_file",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.FileEdit",
        codex_call_kind: "custom_tool_call",
        codex_tool_name: "apply_patch",
        codex_fallback_tool_names: &["write_file"],
        encode_agy_any: encode_any_file_edit,
        decode_agy_any: decode_any_file_edit,
        encode_codex: encode_codex_file_edit,
        decode_codex: dec_cx_file_edit,
        extract_result_meta: Some(rmeta_file_edit),
    },
    ToolCodecSpec {
        payload_kind: "run_command",
        agy_tool_name: "run_command",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.RunCommand",
        codex_call_kind: "function_call",
        codex_tool_name: "exec_command",
        codex_fallback_tool_names: &["custom_tool_call:exec", "shell", "exec"],
        encode_agy_any: encode_any_run_command,
        decode_agy_any: decode_any_run_command,
        encode_codex: encode_codex_run_command,
        decode_codex: dec_cx_run_command,
        extract_result_meta: Some(rmeta_run_command),
    },
    ToolCodecSpec {
        payload_kind: "search_web",
        agy_tool_name: "search_web",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.SearchWeb",
        codex_call_kind: "function_call",
        codex_tool_name: "web_search",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_search_web,
        decode_agy_any: decode_any_search_web,
        encode_codex: enc_cx_search_web,
        decode_codex: dec_cx_search_web,
        extract_result_meta: Some(rmeta_search_web),
    },
    ToolCodecSpec {
        payload_kind: "invoke_subagent",
        agy_tool_name: "invoke_subagent",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.InvokeSubagent",
        codex_call_kind: "function_call",
        codex_tool_name: "spawn_agent",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_invoke_subagent,
        decode_agy_any: decode_any_invoke_subagent,
        encode_codex: enc_cx_invoke_subagent,
        decode_codex: dec_cx_invoke_subagent,
        extract_result_meta: Some(rmeta_invoke_subagent),
    },
    ToolCodecSpec {
        payload_kind: "define_subagent",
        agy_tool_name: "define_subagent",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.DefineSubagent",
        codex_call_kind: "function_call",
        codex_tool_name: "define_subagent",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_define_subagent,
        decode_agy_any: decode_any_define_subagent,
        encode_codex: enc_cx_define_subagent,
        decode_codex: dec_cx_define_subagent,
        extract_result_meta: None,
    },
    ToolCodecSpec {
        payload_kind: "manage_subagents",
        agy_tool_name: "manage_subagents",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.ManageSubagents",
        codex_call_kind: "function_call",
        codex_tool_name: "list_agents",
        codex_fallback_tool_names: &[
            "wait_agent",
            "interrupt_agent",
            "close_agent",
            "kill_all_agents",
        ],
        encode_agy_any: encode_any_manage_subagents,
        decode_agy_any: decode_any_manage_subagents,
        encode_codex: encode_codex_manage_subagents,
        decode_codex: decode_codex_manage_subagents,
        extract_result_meta: Some(rmeta_manage_subagents),
    },
    ToolCodecSpec {
        payload_kind: "manage_task",
        agy_tool_name: "manage_task",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.ManageTask",
        codex_call_kind: "function_call",
        codex_tool_name: "wait",
        codex_fallback_tool_names: &["send_input", "write_stdin", "kill_task", "list_tasks"],
        encode_agy_any: encode_any_manage_task,
        decode_agy_any: decode_any_manage_task,
        encode_codex: encode_codex_manage_task,
        decode_codex: decode_codex_manage_task,
        extract_result_meta: None,
    },
    ToolCodecSpec {
        payload_kind: "send_message",
        agy_tool_name: "send_message",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.SendMessage",
        codex_call_kind: "function_call",
        codex_tool_name: "send_message",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_send_message,
        decode_agy_any: decode_any_send_message,
        encode_codex: enc_cx_send_message,
        decode_codex: dec_cx_send_message,
        extract_result_meta: None,
    },
    ToolCodecSpec {
        payload_kind: "schedule",
        agy_tool_name: "schedule",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.Schedule",
        codex_call_kind: "function_call",
        codex_tool_name: "schedule",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_schedule,
        decode_agy_any: decode_any_schedule,
        encode_codex: enc_cx_schedule,
        decode_codex: dec_cx_schedule,
        extract_result_meta: None,
    },
    ToolCodecSpec {
        payload_kind: "ask_question",
        agy_tool_name: "ask_question",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.AskQuestion",
        codex_call_kind: "function_call",
        codex_tool_name: "ask_question",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_ask_question,
        decode_agy_any: decode_any_ask_question,
        encode_codex: enc_cx_ask_question,
        decode_codex: dec_cx_ask_question,
        extract_result_meta: Some(rmeta_ask_question),
    },
    ToolCodecSpec {
        payload_kind: "list_resources",
        agy_tool_name: "list_resources",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.ListResources",
        codex_call_kind: "function_call",
        codex_tool_name: "list_mcp_resources",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_list_resources,
        decode_agy_any: decode_any_list_resources,
        encode_codex: enc_cx_list_resources,
        decode_codex: dec_cx_list_resources,
        extract_result_meta: Some(rmeta_list_resources),
    },
    ToolCodecSpec {
        payload_kind: "read_resource",
        agy_tool_name: "read_resource",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.ReadResource",
        codex_call_kind: "function_call",
        codex_tool_name: "read_mcp_resource",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_read_resource,
        decode_agy_any: decode_any_read_resource,
        encode_codex: enc_cx_read_resource,
        decode_codex: dec_cx_read_resource,
        extract_result_meta: Some(rmeta_read_resource),
    },
    ToolCodecSpec {
        payload_kind: "generate_image",
        agy_tool_name: "generate_image",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.GenerateImage",
        codex_call_kind: "function_call",
        codex_tool_name: "generate_image",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_generate_image,
        decode_agy_any: decode_any_generate_image,
        encode_codex: enc_cx_generate_image,
        decode_codex: dec_cx_generate_image,
        extract_result_meta: Some(rmeta_generate_image),
    },
    ToolCodecSpec {
        payload_kind: "moma_search",
        agy_tool_name: "moma_search",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.MomaSearch",
        codex_call_kind: "function_call",
        codex_tool_name: "moma_search",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_moma_search,
        decode_agy_any: decode_any_moma_search,
        encode_codex: enc_cx_moma_search,
        decode_codex: dec_cx_moma_search,
        extract_result_meta: Some(rmeta_moma_search),
    },
    ToolCodecSpec {
        payload_kind: "finish",
        agy_tool_name: "finish",
        agy_any_type_url: "type.googleapis.com/exa.cortex.core.tools.Finish",
        codex_call_kind: "function_call",
        codex_tool_name: "finish",
        codex_fallback_tool_names: &[],
        encode_agy_any: encode_any_finish,
        decode_agy_any: decode_any_finish,
        encode_codex: encode_codex_finish,
        decode_codex: dec_cx_finish,
        extract_result_meta: Some(rmeta_finish),
    },
];

static TOOL_BY_KIND: LazyLock<HashMap<&'static str, &'static ToolCodecSpec>> =
    LazyLock::new(|| {
        TOOL_CODEC_TABLE
            .iter()
            .map(|s| (s.payload_kind, s))
            .collect()
    });

static TOOL_BY_TYPE_URL: LazyLock<HashMap<&'static str, &'static ToolCodecSpec>> =
    LazyLock::new(|| {
        TOOL_CODEC_TABLE
            .iter()
            .map(|s| (s.agy_any_type_url, s))
            .collect()
    });

static TOOL_NAME_LOOKUP: LazyLock<HashMap<&'static str, &'static ToolCodecSpec>> =
    LazyLock::new(|| {
        let mut map = HashMap::new();
        for s in TOOL_CODEC_TABLE {
            for alias in [s.agy_tool_name, s.codex_tool_name, s.payload_kind] {
                map.entry(alias).or_insert(s);
            }
        }
        for s in TOOL_CODEC_TABLE {
            for &fb in s.codex_fallback_tool_names {
                if !fb.contains(':') {
                    map.entry(fb).or_insert(s);
                }
            }
        }
        map
    });

#[must_use]
pub fn find_tool_spec_by_name(name: &str) -> Option<&'static ToolCodecSpec> {
    TOOL_NAME_LOOKUP.get(name).copied()
}

/// Serialize call-input arguments for Antigravity `ChatToolCall.arguments_json`.
#[must_use]
pub fn serialize_agy_chat_tool_arguments(
    payload: &CanonicalToolPayload,
    has_result_step: bool,
) -> String {
    match payload {
        CanonicalToolPayload::Opaque {
            raw_arguments_json, ..
        } => {
            if raw_arguments_json.is_empty() {
                "{}".to_owned()
            } else {
                raw_arguments_json.clone()
            }
        }
        CanonicalToolPayload::Finish { arguments_json, .. } => {
            if arguments_json.is_empty() {
                "{}".to_owned()
            } else {
                arguments_json.clone()
            }
        }
        CanonicalToolPayload::FileEdit {
            path,
            edit_op,
            unified_diff,
            is_artifact,
            artifact_summary,
            ..
        } => {
            let mut m = BTreeMap::new();
            m.insert("EditOp".into(), json!(edit_op.canonical_str()));
            m.insert(
                "Path".into(),
                json!(normalize_tool_file_path(path).as_ref()),
            );
            if *is_artifact {
                m.insert("IsArtifact".into(), json!(true));
            }
            if let Some(s) = artifact_summary {
                m.insert("ArtifactSummary".into(), json!(s));
            }
            if !has_result_step && !unified_diff.is_empty() {
                m.insert("unified_diff".into(), json!(unified_diff));
            }
            dump_canonical_json(&m)
        }
        CanonicalToolPayload::RunCommand {
            command_line, cwd, ..
        } => {
            let mut m = safe_json_dict(&serialize_tool_payload_arguments_json(payload));
            m.insert("CommandLine".into(), json!(command_line));
            m.insert("Cwd".into(), json!(normalize_tool_file_path(cwd).as_ref()));
            dump_canonical_json(&m)
        }
        CanonicalToolPayload::ReadFile {
            path,
            start_line,
            end_line,
            ..
        } => {
            let mut m = safe_json_dict(&serialize_tool_payload_arguments_json(payload));
            m.insert(
                "Path".into(),
                json!(normalize_tool_file_path(path).as_ref()),
            );
            if let Some(sl) = start_line {
                m.insert("StartLine".into(), json!(sl));
            }
            if let Some(el) = end_line {
                m.insert("EndLine".into(), json!(el));
            }
            dump_canonical_json(&m)
        }
        _ => {
            let m = safe_json_dict(&serialize_tool_payload_arguments_json(payload));
            dump_canonical_json(&m)
        }
    }
}

/// Extract result-only fields from [`CanonicalToolPayload`].
#[must_use]
pub fn extract_tool_result_meta(payload: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    if let CanonicalToolPayload::Opaque {
        type_url,
        raw_any_bytes,
        ..
    } = payload
    {
        let mut m = BTreeMap::new();
        if let Some(tu) = type_url {
            m.insert("type_url".into(), json!(tu));
        }
        if let Some(raw) = raw_any_bytes {
            m.insert("raw_any_hex".into(), json!(hex::encode(raw)));
        }
        return m;
    }
    TOOL_BY_KIND
        .get(payload.kind_str())
        .and_then(|s| s.extract_result_meta)
        .map_or_else(BTreeMap::new, |f| f(payload))
}

/// Hydrate result-only fields onto `prior_payload` without mutating call-input arguments.
#[must_use]
pub fn apply_tool_result_meta(
    tool_name: &str,
    prior_payload: &CanonicalToolPayload,
    result_meta: &Map<String, Value>,
) -> CanonicalToolPayload {
    if result_meta.is_empty() {
        return prior_payload.clone();
    }
    if let CanonicalToolPayload::Opaque {
        tool_name: tname,
        raw_arguments_json,
        type_url,
        raw_any_bytes,
    } = prior_payload
    {
        let new_type_url = result_meta
            .get("type_url")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .or_else(|| type_url.clone());
        let new_raw = result_meta
            .get("raw_any_hex")
            .and_then(|v| v.as_str())
            .and_then(|h| hex::decode(h).ok())
            .or_else(|| raw_any_bytes.clone());
        return CanonicalToolPayload::Opaque {
            tool_name: tname.clone(),
            raw_arguments_json: raw_arguments_json.clone(),
            type_url: new_type_url,
            raw_any_bytes: new_raw,
        };
    }
    let mut merged = safe_json_dict(&serialize_tool_payload_arguments_json(prior_payload));
    for (k, v) in result_meta {
        merged.insert(k.clone(), v.clone());
    }
    if matches!(prior_payload, CanonicalToolPayload::RunCommand { .. })
        && result_meta.get("exit_code_known").and_then(Value::as_bool) == Some(false)
    {
        merged.remove("exit_code");
    }
    if let Some(spec) = TOOL_BY_KIND.get(prior_payload.kind_str()) {
        let raw_arg_str = match prior_payload {
            CanonicalToolPayload::Finish { arguments_json, .. } => arguments_json.clone(),
            _ => dump_canonical_json(&merged),
        };
        return (spec.decode_codex)(tool_name, &merged, &raw_arg_str);
    }
    prior_payload.clone()
}

/// Encode [`CanonicalToolPayload`] into serialized `google.protobuf.Any` bytes.
#[must_use]
pub fn encode_tool_payload_to_any(payload: &CanonicalToolPayload) -> Vec<u8> {
    if let CanonicalToolPayload::Opaque {
        type_url,
        raw_any_bytes,
        ..
    } = payload
    {
        return match (type_url.as_deref(), raw_any_bytes.as_deref()) {
            (Some(tu), Some(raw)) if !tu.is_empty() => encode_any_envelope(tu, raw),
            _ => Vec::new(),
        };
    }
    TOOL_BY_KIND
        .get(payload.kind_str())
        .map_or_else(Vec::new, |spec| {
            let val = (spec.encode_agy_any)(payload);
            encode_any_envelope(spec.agy_any_type_url, &val)
        })
}

/// Decode a tool payload from JSON arguments and optional Antigravity `google.protobuf.Any` fields.
#[must_use]
pub fn decode_tool_payload(
    tool_name: &str,
    arguments_json: &str,
    any_type_url: Option<&str>,
    any_value_bytes: Option<&[u8]>,
    is_codex_wire: bool,
) -> CanonicalToolPayload {
    let spec = any_type_url
        .and_then(|tu| TOOL_BY_TYPE_URL.get(tu).copied())
        .or_else(|| {
            if tool_name.is_empty() {
                None
            } else {
                TOOL_NAME_LOOKUP.get(tool_name).copied()
            }
        });
    let fallback_json = if arguments_json.is_empty() {
        "{}"
    } else {
        arguments_json
    };
    let Some(spec) = spec else {
        return CanonicalToolPayload::Opaque {
            tool_name: if tool_name.is_empty() {
                "unknown_tool".to_owned()
            } else {
                tool_name.to_owned()
            },
            raw_arguments_json: fallback_json.to_owned(),
            type_url: any_type_url.map(str::to_owned),
            raw_any_bytes: any_value_bytes.map(<[u8]>::to_vec),
        };
    };
    let args_dict = safe_json_dict(arguments_json);
    if is_codex_wire && any_value_bytes.is_none_or(<[u8]>::is_empty) {
        return (spec.decode_codex)(tool_name, &args_dict, fallback_json);
    }
    (spec.decode_agy_any)(&args_dict, fallback_json, any_value_bytes.unwrap_or(&[]))
}

/// Encode [`CanonicalToolPayload`] to Codex wire `(call_kind, tool_name, arguments_json, custom_input)`.
#[must_use]
pub fn encode_tool_for_codex(
    tool_name: &str,
    payload: &CanonicalToolPayload,
) -> (String, String, String, Option<String>) {
    if let CanonicalToolPayload::Opaque {
        raw_arguments_json, ..
    } = payload
    {
        let kind = if tool_name == "tool_search" {
            "tool_search_call"
        } else {
            "function_call"
        };
        let args = if raw_arguments_json.is_empty() {
            "{}".to_owned()
        } else {
            raw_arguments_json.clone()
        };
        return (kind.to_owned(), tool_name.to_owned(), args, None);
    }
    if let Some(spec) = TOOL_BY_KIND.get(payload.kind_str()) {
        let (k, n, a, ci) = (spec.encode_codex)(payload);
        return (k.to_owned(), n.to_owned(), a, ci);
    }
    (
        "function_call".to_owned(),
        tool_name.to_owned(),
        serialize_tool_payload_arguments_json(payload),
        None,
    )
}
