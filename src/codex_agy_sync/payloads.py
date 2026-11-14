"""Tier 0 typed canonical tool payload value objects and argument JSON serializers."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Literal, Mapping, Tuple, TypeAlias

from .workspace import normalize_tool_file_path


@dataclass(frozen=True)
class ReadFilePayload:
    kind: Literal["read_file"]
    path: str
    start_line: int | None = None
    end_line: int | None = None
    byte_offset: int | None = None
    file_num_lines: int | None = None
    file_num_bytes: int | None = None
    content_excerpt: str | None = None


@dataclass(frozen=True)
class FileEditPayload:
    kind: Literal["file_edit"]
    path: str
    edit_op: Literal["CREATE", "ADD", "MODIFY", "DELETE"]
    unified_diff: str
    diff_stats: Tuple[int, int] = (0, 0)
    is_artifact: bool = False
    artifact_summary: str | None = None


@dataclass(frozen=True)
class RunCommandPayload:
    kind: Literal["run_command"]
    command_line: str
    cwd: str
    exit_code: int | None = None
    timed_out: bool = False
    is_daemon: bool = False
    shell_name: str | None = None
    ran_in_sandbox: bool = False
    sandbox_policy: str | None = None


@dataclass(frozen=True)
class SearchWebDocument:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class SearchWebPayload:
    kind: Literal["search_web"]
    query: str
    domain_filter: str | None = None
    documents: Tuple[SearchWebDocument, ...] = ()
    summary: str | None = None


@dataclass(frozen=True)
class InvokeSubagentPayload:
    kind: Literal["invoke_subagent"]
    subagent_type: str
    role: str
    prompt: str
    spawned_conversation_id: str | None = None
    workspace_mode: Literal["inherit", "branch", "share"] = "inherit"
    task_mode: bool = False


@dataclass(frozen=True)
class DefineSubagentPayload:
    kind: Literal["define_subagent"]
    name: str
    tool_names: Tuple[str, ...] = ()
    agent_path: str | None = None


@dataclass(frozen=True)
class ManageSubagentsPayload:
    kind: Literal["manage_subagents"]
    action: Literal["list", "kill", "kill_all"]
    target_conversation_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ManageTaskPayload:
    kind: Literal["manage_task"]
    action: Literal["list", "status", "send_input", "kill"]
    task_id: str | None = None
    input_text: str | None = None


@dataclass(frozen=True)
class SendMessagePayload:
    kind: Literal["send_message"]
    recipient_id: str
    message_body: str
    recipient_name: str | None = None


@dataclass(frozen=True)
class SchedulePayload:
    kind: Literal["schedule"]
    prompt: str
    duration_seconds: int | None = None
    cron_expression: str | None = None
    timer_condition: str = "never"
    is_daemon: bool = False
    exact_timing: bool = False
    max_iterations: int | None = None


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str = ""


@dataclass(frozen=True)
class QuestionItem:
    question: str
    options: Tuple[QuestionOption, ...] = ()
    is_multi_select: bool = False
    user_answers: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AskQuestionPayload:
    kind: Literal["ask_question"]
    questions: Tuple[QuestionItem, ...] = ()


@dataclass(frozen=True)
class ListResourcesPayload:
    kind: Literal["list_resources"]
    server_name: str
    resource_uris: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadResourcePayload:
    kind: Literal["read_resource"]
    server_name: str
    uri: str
    contents_text: Tuple[str, ...] = ()


@dataclass(frozen=True)
class GenerateImagePayload:
    kind: Literal["generate_image"]
    prompt: str
    image_paths: Tuple[str, ...] = ()
    aspect_ratio: str = "1:1"
    image_uri: str | None = None
    model_name: str | None = None


@dataclass(frozen=True)
class MomaSearchPayload:
    kind: Literal["moma_search"]
    query: str
    result_count: int = 0


@dataclass(frozen=True)
class FinishPayload:
    kind: Literal["finish"]
    arguments_json: str = "{}"
    has_result_schema: bool = False


@dataclass(frozen=True)
class OpaqueToolPayload:
    kind: Literal["opaque"]
    tool_name: str
    raw_arguments_json: str
    type_url: str | None = None
    raw_any_bytes: bytes | None = None


CanonicalToolPayload: TypeAlias = (
    ReadFilePayload | FileEditPayload | RunCommandPayload | SearchWebPayload
    | InvokeSubagentPayload | DefineSubagentPayload | ManageSubagentsPayload
    | ManageTaskPayload | SendMessagePayload | SchedulePayload | AskQuestionPayload
    | ListResourcesPayload | ReadResourcePayload | GenerateImagePayload
    | MomaSearchPayload | FinishPayload | OpaqueToolPayload
)


def safe_json_dict(raw_json: str) -> dict[str, Any]:
    """Parse a JSON string into a shallow `dict`, returning `{}` on invalid or non-object inputs."""
    if not raw_json or not raw_json.strip():
        return {}
    try:
        parsed = json.loads(raw_json)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def dump_canonical_json(data: Mapping[str, Any]) -> str:
    """Serialize a mapping into deterministic compact UTF-8 JSON."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clean_dict(**kwargs: Any) -> dict[str, Any]:
    """Build a dict omitting entries whose value is `None`."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _args_read_file(p: ReadFilePayload) -> Mapping[str, Any]:
    return clean_dict(
        path=normalize_tool_file_path(p.path),
        start_line=p.start_line,
        end_line=p.end_line,
        byte_offset=p.byte_offset,
    )


def _args_file_edit(p: FileEditPayload) -> Mapping[str, Any]:
    canon_op = "CREATE" if p.edit_op == "ADD" else p.edit_op
    return clean_dict(
        edit_op=canon_op,
        is_artifact=p.is_artifact,
        path=normalize_tool_file_path(p.path),
        unified_diff=p.unified_diff,
        artifact_summary=p.artifact_summary,
    )


def _args_run_command(p: RunCommandPayload) -> Mapping[str, Any]:
    return clean_dict(
        command_line=p.command_line,
        cwd=normalize_tool_file_path(p.cwd),
        is_daemon=p.is_daemon,
        ran_in_sandbox=p.ran_in_sandbox,
        shell_name=p.shell_name,
        sandbox_policy=p.sandbox_policy,
    )


def _args_search_web(p: SearchWebPayload) -> Mapping[str, Any]:
    return clean_dict(query=p.query, domain_filter=p.domain_filter)


def _args_invoke_subagent(p: InvokeSubagentPayload) -> Mapping[str, Any]:
    return {
        "prompt": p.prompt,
        "role": p.role,
        "subagent_type": p.subagent_type,
        "task_mode": p.task_mode,
        "workspace_mode": p.workspace_mode,
    }


def _args_define_subagent(p: DefineSubagentPayload) -> Mapping[str, Any]:
    return clean_dict(
        name=p.name,
        tool_names=list(p.tool_names),
        agent_path=normalize_tool_file_path(p.agent_path) if p.agent_path is not None else None,
    )


def _args_manage_subagents(p: ManageSubagentsPayload) -> Mapping[str, Any]:
    if p.action != "list" and p.target_conversation_ids:
        return {"action": p.action, "target_conversation_ids": list(p.target_conversation_ids)}
    return {"action": p.action}


def _args_manage_task(p: ManageTaskPayload) -> Mapping[str, Any]:
    return clean_dict(action=p.action, task_id=p.task_id, input_text=p.input_text)


def _args_send_message(p: SendMessagePayload) -> Mapping[str, Any]:
    return clean_dict(
        message_body=p.message_body,
        recipient_id=p.recipient_id,
        recipient_name=p.recipient_name,
    )


def _args_schedule(p: SchedulePayload) -> Mapping[str, Any]:
    return clean_dict(
        prompt=p.prompt,
        duration_seconds=p.duration_seconds,
        cron_expression=p.cron_expression,
        timer_condition=p.timer_condition,
        is_daemon=p.is_daemon,
        exact_timing=p.exact_timing,
        max_iterations=p.max_iterations,
    )


def _args_ask_question(p: AskQuestionPayload) -> Mapping[str, Any]:
    return {
        "questions": [
            {
                "is_multi_select": q.is_multi_select,
                "options": [{"description": o.description, "label": o.label} for o in q.options],
                "question": q.question,
            }
            for q in p.questions
        ]
    }


def _args_list_resources(p: ListResourcesPayload) -> Mapping[str, Any]:
    return {"server_name": p.server_name}


def _args_read_resource(p: ReadResourcePayload) -> Mapping[str, Any]:
    return {"server_name": p.server_name, "uri": p.uri}


def _args_generate_image(p: GenerateImagePayload) -> Mapping[str, Any]:
    return clean_dict(
        prompt=p.prompt,
        image_paths=[normalize_tool_file_path(x) for x in p.image_paths],
        aspect_ratio=p.aspect_ratio,
        model_name=p.model_name,
    )


def _args_moma_search(p: MomaSearchPayload) -> Mapping[str, Any]:
    return {"query": p.query}


def _args_finish(p: FinishPayload) -> Mapping[str, Any]:
    return safe_json_dict(p.arguments_json)


TOOL_ARG_SERIALIZERS: Mapping[str, Callable[[Any], Mapping[str, Any]]] = {
    "read_file": _args_read_file,
    "file_edit": _args_file_edit,
    "run_command": _args_run_command,
    "search_web": _args_search_web,
    "invoke_subagent": _args_invoke_subagent,
    "define_subagent": _args_define_subagent,
    "manage_subagents": _args_manage_subagents,
    "manage_task": _args_manage_task,
    "send_message": _args_send_message,
    "schedule": _args_schedule,
    "ask_question": _args_ask_question,
    "list_resources": _args_list_resources,
    "read_resource": _args_read_resource,
    "generate_image": _args_generate_image,
    "moma_search": _args_moma_search,
    "finish": _args_finish,
}


def serialize_tool_payload_arguments_json(payload: CanonicalToolPayload) -> str:
    """Serialize call-input arguments of `CanonicalToolPayload` into deterministic JSON."""
    if isinstance(payload, OpaqueToolPayload):
        return payload.raw_arguments_json or "{}"
    if isinstance(payload, FinishPayload):
        return payload.arguments_json or "{}"
    serializer = TOOL_ARG_SERIALIZERS.get(payload.kind)
    return dump_canonical_json(serializer(payload)) if serializer is not None else "{}"

