//! Canonical typed tool payloads and deterministic argument JSON serializers.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::id::normalize_tool_file_path;

/// Document item returned by `search_web`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SearchWebDocument {
    pub title: String,
    pub url: String,
    pub snippet: String,
}

/// Option entry inside `ask_question`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QuestionOption {
    pub label: String,
    pub description: String,
}

/// Single question item inside `ask_question`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QuestionItem {
    pub question: String,
    pub options: Vec<QuestionOption>,
    pub is_multi_select: bool,
    pub user_answers: Vec<String>,
}

/// Canonical edit operation for `file_edit`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum FileEditOp {
    Create,
    Add,
    Modify,
    Delete,
}

impl FileEditOp {
    #[must_use]
    pub const fn canonical_str(self) -> &'static str {
        match self {
            Self::Create | Self::Add => "CREATE",
            Self::Modify => "MODIFY",
            Self::Delete => "DELETE",
        }
    }
}

/// Subagent workspace isolation mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubagentWorkspaceMode {
    #[default]
    Inherit,
    Branch,
    Share,
}

impl SubagentWorkspaceMode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Inherit => "inherit",
            Self::Branch => "branch",
            Self::Share => "share",
        }
    }
}

/// Action for `manage_subagents`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ManageSubagentsAction {
    List,
    Kill,
    KillAll,
}

impl ManageSubagentsAction {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::List => "list",
            Self::Kill => "kill",
            Self::KillAll => "kill_all",
        }
    }
}

/// Action for `manage_task`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ManageTaskAction {
    List,
    Status,
    SendInput,
    Kill,
}

impl ManageTaskAction {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::List => "list",
            Self::Status => "status",
            Self::SendInput => "send_input",
            Self::Kill => "kill",
        }
    }
}

/// All 16 schema-verified tool payload variants plus `Opaque`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CanonicalToolPayload {
    ReadFile {
        path: String,
        start_line: Option<i64>,
        end_line: Option<i64>,
        byte_offset: Option<i64>,
        file_num_lines: Option<i64>,
        file_num_bytes: Option<i64>,
        content_excerpt: Option<String>,
    },
    FileEdit {
        path: String,
        edit_op: FileEditOp,
        unified_diff: String,
        diff_stats: (i64, i64),
        is_artifact: bool,
        artifact_summary: Option<String>,
    },
    RunCommand {
        command_line: String,
        cwd: String,
        exit_code: Option<i64>,
        timed_out: bool,
        is_daemon: bool,
        shell_name: Option<String>,
        ran_in_sandbox: bool,
        sandbox_policy: Option<String>,
    },
    SearchWeb {
        query: String,
        domain_filter: Option<String>,
        documents: Vec<SearchWebDocument>,
        summary: Option<String>,
    },
    InvokeSubagent {
        subagent_type: String,
        role: String,
        prompt: String,
        spawned_conversation_id: Option<String>,
        workspace_mode: SubagentWorkspaceMode,
        task_mode: bool,
    },
    DefineSubagent {
        name: String,
        tool_names: Vec<String>,
        agent_path: Option<String>,
    },
    ManageSubagents {
        action: ManageSubagentsAction,
        target_conversation_ids: Vec<String>,
    },
    ManageTask {
        action: ManageTaskAction,
        task_id: Option<String>,
        input_text: Option<String>,
    },
    SendMessage {
        recipient_id: String,
        message_body: String,
        recipient_name: Option<String>,
    },
    Schedule {
        prompt: String,
        duration_seconds: Option<i64>,
        cron_expression: Option<String>,
        timer_condition: String,
        is_daemon: bool,
        exact_timing: bool,
        max_iterations: Option<i64>,
    },
    AskQuestion {
        questions: Vec<QuestionItem>,
    },
    ListResources {
        server_name: String,
        resource_uris: Vec<String>,
    },
    ReadResource {
        server_name: String,
        uri: String,
        contents_text: Vec<String>,
    },
    GenerateImage {
        prompt: String,
        image_paths: Vec<String>,
        aspect_ratio: String,
        image_uri: Option<String>,
        model_name: Option<String>,
    },
    MomaSearch {
        query: String,
        result_count: i64,
    },
    Finish {
        arguments_json: String,
        has_result_schema: bool,
    },
    Opaque {
        tool_name: String,
        raw_arguments_json: String,
        type_url: Option<String>,
        raw_any_bytes: Option<Vec<u8>>,
    },
}

impl CanonicalToolPayload {
    /// Canonical `kind` tag matching Python `payload.kind`.
    #[must_use]
    pub const fn kind_str(&self) -> &'static str {
        match self {
            Self::ReadFile { .. } => "read_file",
            Self::FileEdit { .. } => "file_edit",
            Self::RunCommand { .. } => "run_command",
            Self::SearchWeb { .. } => "search_web",
            Self::InvokeSubagent { .. } => "invoke_subagent",
            Self::DefineSubagent { .. } => "define_subagent",
            Self::ManageSubagents { .. } => "manage_subagents",
            Self::ManageTask { .. } => "manage_task",
            Self::SendMessage { .. } => "send_message",
            Self::Schedule { .. } => "schedule",
            Self::AskQuestion { .. } => "ask_question",
            Self::ListResources { .. } => "list_resources",
            Self::ReadResource { .. } => "read_resource",
            Self::GenerateImage { .. } => "generate_image",
            Self::MomaSearch { .. } => "moma_search",
            Self::Finish { .. } => "finish",
            Self::Opaque { .. } => "opaque",
        }
    }
}

/// Parse a JSON string into a sorted `BTreeMap<String, Value>`, returning empty on invalid or non-object input.
#[must_use]
pub fn safe_json_dict(raw_json: &str) -> BTreeMap<String, Value> {
    if raw_json.trim().is_empty() {
        return BTreeMap::new();
    }
    match serde_json::from_str::<Value>(raw_json) {
        Ok(Value::Object(map)) => map.into_iter().collect(),
        _ => BTreeMap::new(),
    }
}

/// Serialize a sorted map into compact deterministic UTF-8 JSON (matching Python `sort_keys=True, separators=(",", ":")`).
#[must_use]
pub fn dump_canonical_json(data: &BTreeMap<String, Value>) -> String {
    let sorted = sort_json_value(&Value::Object(
        data.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
    ));
    serde_json::to_string(&sorted).unwrap_or_else(|_| "{}".to_owned())
}

/// Recursively canonicalize object keys in a `serde_json::Value` for deterministic hashing/serialization.
#[must_use]
pub fn sort_json_value(val: &Value) -> Value {
    match val {
        Value::Object(map) => {
            let mut sorted: Vec<(&String, &Value)> = map.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(b.0));
            let mut out = serde_json::Map::with_capacity(sorted.len());
            for (k, v) in sorted {
                out.insert(k.clone(), sort_json_value(v));
            }
            Value::Object(out)
        }
        Value::Array(arr) => Value::Array(arr.iter().map(sort_json_value).collect()),
        other => other.clone(),
    }
}

/// Build the sorted argument map for a [`CanonicalToolPayload`], omitting `None` fields.
#[must_use]
pub fn tool_payload_args_map(payload: &CanonicalToolPayload) -> BTreeMap<String, Value> {
    let mut m = BTreeMap::new();
    match payload {
        CanonicalToolPayload::ReadFile {
            path,
            start_line,
            end_line,
            byte_offset,
            ..
        } => {
            m.insert(
                "path".to_owned(),
                Value::String(normalize_tool_file_path(path).into_owned()),
            );
            if let Some(v) = start_line {
                m.insert("start_line".to_owned(), Value::from(*v));
            }
            if let Some(v) = end_line {
                m.insert("end_line".to_owned(), Value::from(*v));
            }
            if let Some(v) = byte_offset {
                m.insert("byte_offset".to_owned(), Value::from(*v));
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
            m.insert(
                "edit_op".to_owned(),
                Value::String(edit_op.canonical_str().to_owned()),
            );
            m.insert("is_artifact".to_owned(), Value::Bool(*is_artifact));
            m.insert(
                "path".to_owned(),
                Value::String(normalize_tool_file_path(path).into_owned()),
            );
            m.insert(
                "unified_diff".to_owned(),
                Value::String(unified_diff.clone()),
            );
            if let Some(v) = artifact_summary {
                m.insert("artifact_summary".to_owned(), Value::String(v.clone()));
            }
        }
        CanonicalToolPayload::RunCommand {
            command_line,
            cwd,
            is_daemon,
            shell_name,
            ran_in_sandbox,
            sandbox_policy,
            ..
        } => {
            m.insert(
                "command_line".to_owned(),
                Value::String(command_line.clone()),
            );
            m.insert(
                "cwd".to_owned(),
                Value::String(normalize_tool_file_path(cwd).into_owned()),
            );
            m.insert("is_daemon".to_owned(), Value::Bool(*is_daemon));
            m.insert("ran_in_sandbox".to_owned(), Value::Bool(*ran_in_sandbox));
            if let Some(v) = shell_name {
                m.insert("shell_name".to_owned(), Value::String(v.clone()));
            }
            if let Some(v) = sandbox_policy {
                m.insert("sandbox_policy".to_owned(), Value::String(v.clone()));
            }
        }
        CanonicalToolPayload::SearchWeb {
            query,
            domain_filter,
            ..
        } => {
            m.insert("query".to_owned(), Value::String(query.clone()));
            if let Some(v) = domain_filter {
                m.insert("domain_filter".to_owned(), Value::String(v.clone()));
            }
        }
        CanonicalToolPayload::InvokeSubagent {
            subagent_type,
            role,
            prompt,
            workspace_mode,
            task_mode,
            ..
        } => {
            m.insert("prompt".to_owned(), Value::String(prompt.clone()));
            m.insert("role".to_owned(), Value::String(role.clone()));
            m.insert(
                "subagent_type".to_owned(),
                Value::String(subagent_type.clone()),
            );
            m.insert("task_mode".to_owned(), Value::Bool(*task_mode));
            m.insert(
                "workspace_mode".to_owned(),
                Value::String(workspace_mode.as_str().to_owned()),
            );
        }
        CanonicalToolPayload::DefineSubagent {
            name,
            tool_names,
            agent_path,
        } => {
            m.insert("name".to_owned(), Value::String(name.clone()));
            m.insert(
                "tool_names".to_owned(),
                Value::Array(tool_names.iter().cloned().map(Value::String).collect()),
            );
            if let Some(p) = agent_path {
                m.insert(
                    "agent_path".to_owned(),
                    Value::String(normalize_tool_file_path(p).into_owned()),
                );
            }
        }
        CanonicalToolPayload::ManageSubagents {
            action,
            target_conversation_ids,
        } => {
            m.insert(
                "action".to_owned(),
                Value::String(action.as_str().to_owned()),
            );
            if *action != ManageSubagentsAction::List && !target_conversation_ids.is_empty() {
                m.insert(
                    "target_conversation_ids".to_owned(),
                    Value::Array(
                        target_conversation_ids
                            .iter()
                            .cloned()
                            .map(Value::String)
                            .collect(),
                    ),
                );
            }
        }
        CanonicalToolPayload::ManageTask {
            action,
            task_id,
            input_text,
        } => {
            m.insert(
                "action".to_owned(),
                Value::String(action.as_str().to_owned()),
            );
            if let Some(v) = task_id {
                m.insert("task_id".to_owned(), Value::String(v.clone()));
            }
            if let Some(v) = input_text {
                m.insert("input_text".to_owned(), Value::String(v.clone()));
            }
        }
        CanonicalToolPayload::SendMessage {
            recipient_id,
            message_body,
            recipient_name,
        } => {
            m.insert(
                "message_body".to_owned(),
                Value::String(message_body.clone()),
            );
            m.insert(
                "recipient_id".to_owned(),
                Value::String(recipient_id.clone()),
            );
            if let Some(v) = recipient_name {
                m.insert("recipient_name".to_owned(), Value::String(v.clone()));
            }
        }
        CanonicalToolPayload::Schedule {
            prompt,
            duration_seconds,
            cron_expression,
            timer_condition,
            is_daemon,
            exact_timing,
            max_iterations,
        } => {
            m.insert("prompt".to_owned(), Value::String(prompt.clone()));
            if let Some(v) = duration_seconds {
                m.insert("duration_seconds".to_owned(), Value::from(*v));
            }
            if let Some(v) = cron_expression {
                m.insert("cron_expression".to_owned(), Value::String(v.clone()));
            }
            m.insert(
                "timer_condition".to_owned(),
                Value::String(timer_condition.clone()),
            );
            m.insert("is_daemon".to_owned(), Value::Bool(*is_daemon));
            m.insert("exact_timing".to_owned(), Value::Bool(*exact_timing));
            if let Some(v) = max_iterations {
                m.insert("max_iterations".to_owned(), Value::from(*v));
            }
        }
        CanonicalToolPayload::AskQuestion { questions } => {
            let qs: Vec<Value> = questions
                .iter()
                .map(|q| {
                    let opts: Vec<Value> = q
                        .options
                        .iter()
                        .map(|o| {
                            let mut om = serde_json::Map::new();
                            om.insert(
                                "description".to_owned(),
                                Value::String(o.description.clone()),
                            );
                            om.insert("label".to_owned(), Value::String(o.label.clone()));
                            Value::Object(om)
                        })
                        .collect();
                    let mut qm = serde_json::Map::new();
                    qm.insert("is_multi_select".to_owned(), Value::Bool(q.is_multi_select));
                    qm.insert("options".to_owned(), Value::Array(opts));
                    qm.insert("question".to_owned(), Value::String(q.question.clone()));
                    Value::Object(qm)
                })
                .collect();
            m.insert("questions".to_owned(), Value::Array(qs));
        }
        CanonicalToolPayload::ListResources { server_name, .. } => {
            m.insert("server_name".to_owned(), Value::String(server_name.clone()));
        }
        CanonicalToolPayload::ReadResource {
            server_name, uri, ..
        } => {
            m.insert("server_name".to_owned(), Value::String(server_name.clone()));
            m.insert("uri".to_owned(), Value::String(uri.clone()));
        }
        CanonicalToolPayload::GenerateImage {
            prompt,
            image_paths,
            aspect_ratio,
            model_name,
            ..
        } => {
            m.insert(
                "aspect_ratio".to_owned(),
                Value::String(aspect_ratio.clone()),
            );
            m.insert(
                "image_paths".to_owned(),
                Value::Array(
                    image_paths
                        .iter()
                        .map(|x| Value::String(normalize_tool_file_path(x).into_owned()))
                        .collect(),
                ),
            );
            m.insert("prompt".to_owned(), Value::String(prompt.clone()));
            if let Some(v) = model_name {
                m.insert("model_name".to_owned(), Value::String(v.clone()));
            }
        }
        CanonicalToolPayload::MomaSearch { query, .. } => {
            m.insert("query".to_owned(), Value::String(query.clone()));
        }
        CanonicalToolPayload::Finish { arguments_json, .. } => {
            return safe_json_dict(arguments_json);
        }
        CanonicalToolPayload::Opaque {
            raw_arguments_json, ..
        } => {
            return safe_json_dict(raw_arguments_json);
        }
    }
    m
}

/// Serialize call-input arguments of `CanonicalToolPayload` into deterministic JSON.
#[must_use]
pub fn serialize_tool_payload_arguments_json(payload: &CanonicalToolPayload) -> String {
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
        _ => dump_canonical_json(&tool_payload_args_map(payload)),
    }
}
