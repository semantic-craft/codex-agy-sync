"""Typed canonical tool payload domain value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Tuple, TypeAlias


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
