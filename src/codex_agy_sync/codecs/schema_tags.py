"""Schema-verified protobuf wire tags, step registry, tool codec spec, and raw row envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Literal, Mapping, Tuple

from ..payloads import CanonicalToolPayload
from ..workspace import RolloutLineNo, StepIndex


class CortexStepTypeCode(IntEnum):
    """Schema-verified `exa.cortex_pb.CortexStepType` enum values."""

    USER_INPUT = 14
    PLANNER_RESPONSE = 15
    ERROR_MESSAGE = 17
    CHECKPOINT = 23
    TASK_BOUNDARY = 81
    NOTIFY_USER = 82
    SYSTEM_MESSAGE = 101
    GENERIC = 132


class GeminiStepWireTag(IntEnum):
    """Schema-verified `gemini_coder.Step` oneof field tags."""

    USER_INPUT_TAG = 19
    PLANNER_RESPONSE_TAG = 20
    ERROR_MESSAGE_TAG = 24
    CHECKPOINT_TAG = 30
    TASK_BOUNDARY_TAG = 93
    NOTIFY_USER_TAG = 94
    SYSTEM_MESSAGE_TAG = 114
    GENERIC_TAG = 140


class StepFieldTag(IntEnum):
    TYPE = 1
    STATUS = 4
    METADATA = 5
    SUBTRAJECTORY = 6
    ERROR = 31


class StepStatusCode(IntEnum):
    PENDING = 1
    RUNNING = 2
    DONE = 3
    INVALID = 4
    CLEARED = 5
    CANCELED = 6
    ERROR = 7
    GENERATING = 8
    WAITING = 9
    QUEUED = 11
    INTERRUPTED = 12


ACTIVE_STEP_STATUS_CODES: frozenset[int] = frozenset({
    int(StepStatusCode.PENDING),
    int(StepStatusCode.RUNNING),
    int(StepStatusCode.GENERATING),
    int(StepStatusCode.WAITING),
    int(StepStatusCode.QUEUED),
})


class StepSourceCode(IntEnum):
    MODEL = 2
    USER_EXPLICIT = 4
    SYSTEM = 5


class StepMetaTag(IntEnum):
    CREATED_AT = 1
    SOURCE = 3
    TOOL_CALL = 4
    MODEL_USAGE = 9
    GENERATOR_MODEL = 11
    TOOL_SUMMARY = 30
    MAX_KNOWN_TAG = 39


class ModelUsageTag(IntEnum):
    MODEL = 1
    INPUT_TOKENS = 2
    OUTPUT_TOKENS = 3
    CACHE_READ_TOKENS = 5


class ChatToolCallTag(IntEnum):
    ID = 1
    NAME = 2
    ARGUMENTS_JSON = 3
    THOUGHT_SIGNATURE = 6
    THINKING_SIGNATURE = 7
    ORIGINAL_ARGUMENTS_JSON = 8
    ORIGINAL_NAME = 9


class UserInputTag(IntEnum):
    QUERY = 1
    USER_RESPONSE = 2
    ITEMS = 3
    MEDIA = 9
    CLIENT_MESSAGE_ID = 19


class TextOrScopeItemTag(IntEnum):
    TEXT = 1
    ITEM = 2


class ContextScopeItemTag(IntEnum):
    FILE = 1


class PathScopeItemTag(IntEnum):
    ABSOLUTE_PATH = 1
    ABSOLUTE_URI = 5


class MediaTag(IntEnum):
    MIME_TYPE = 1
    URI = 5


class PlannerResponseTag(IntEnum):
    RESPONSE = 1
    THINKING = 3
    SIGNATURE = 4
    MESSAGE_ID = 6
    TOOL_CALLS = 7
    MODIFIED_RESPONSE = 8
    THINKING_DURATION = 11
    STOP_REASON = 12
    THINKING_SIGNATURE = 14
    RAW_THINKING = 16
    MAX_KNOWN_TAG = 20


class GenericStepTag(IntEnum):
    ARGS = 1
    RESULT = 2


class GenericResultTag(IntEnum):
    RESULT = 1
    PAYLOAD = 6
    FULL_OUTPUT_URI = 7


class ErrorMessageStepTag(IntEnum):
    ERROR = 3
    SHOULD_SHOW_MODEL = 4
    SHOULD_SHOW_USER = 5


class ErrorDetailsTag(IntEnum):
    USER_ERROR_MESSAGE = 1
    SHORT_ERROR = 2
    FULL_ERROR = 3


class CheckpointTag(IntEnum):
    CHECKPOINT_INDEX = 1
    USER_INTENT = 4
    SESSION_SUMMARY = 5
    CODE_CHANGE_SUMMARY = 6
    CONVERSATION_TITLE = 10
    MAX_KNOWN_TAG = 31


class TaskBoundaryTag(IntEnum):
    TASK_NAME = 1
    TASK_STATUS = 2
    TASK_SUMMARY = 3
    MODE = 5
    MAX_KNOWN_TAG = 7


class NotifyUserTag(IntEnum):
    REVIEW_ABSOLUTE_URIS = 1
    NOTIFICATION_CONTENT = 2
    IS_BLOCKING = 3
    MAX_KNOWN_TAG = 8


class SystemMessageTag(IntEnum):
    MESSAGE = 1
    EVENT_TYPE = 3
    MAX_KNOWN_TAG = 6


class AnyEnvelopeTag(IntEnum):
    TYPE_URL = 1
    VALUE = 2


class MapEntryTag(IntEnum):
    KEY = 1
    VALUE = 2


class DurationProtoTag(IntEnum):
    SECONDS = 1
    NANOS = 2


class WorkspaceInfoFieldTag(IntEnum):
    WORKSPACE_FOLDER_ABSOLUTE_URI = 1
    GIT_ROOT_ABSOLUTE_URI = 2


class AnnotationsFieldTag(IntEnum):
    TITLE = 1
    LAST_USER_VIEW_TIME = 7


class TrajectoryMetaFieldTag(IntEnum):
    WORKSPACES = 1
    CREATED_AT = 2
    PARENT_CONVERSATION_ID = 5
    ROOT_CONVERSATION_ID = 6
    WORKSPACE_URIS = 7
    SUBAGENT_SPEC = 8
    NESTING_DEPTH = 17
    PROJECT_ID = 18


class SubagentMetaSpecTag(IntEnum):
    TYPE_NAME = 1
    ROLE = 2


class SummaryFieldTag(IntEnum):
    SUMMARY = 1
    STEP_COUNT = 2
    LAST_MODIFIED_TIME = 3
    TRAJECTORY_ID = 4
    STATUS = 5
    CREATED_TIME = 7
    WORKSPACES = 9
    LAST_USER_INPUT_TIME = 10
    ANNOTATIONS = 15
    LAST_USER_INPUT_STEP_INDEX = 16
    TRAJECTORY_METADATA = 17
    TRAJECTORY_SOURCE = 20
    TRAJECTORY_TYPE = 22


class SummariesStateFieldTag(IntEnum):
    SUMMARIES = 1
    FILE_MOD_TIMES = 2


class ReadFileTag(IntEnum):
    FILE_NUM_LINES = 1
    FILE_NUM_BYTES = 2
    PATH = 3
    START_LINE = 4
    END_LINE = 5
    LINE_RANGE_BYTES = 6


class FileEditTag(IntEnum):
    KIND = 1
    PATH = 2
    DIFF = 3
    DIFF_STATS = 4
    ARTIFACT_METADATA = 5
    IS_ARTIFACT_FILE = 6


class FileEditKindCode(IntEnum):
    UNSPECIFIED = 0
    CREATE = 1
    OVERWRITE = 2
    EDIT = 3


class DiffBlockTag(IntEnum):
    START_LINE = 1
    END_LINE = 2
    UNIFIED_DIFF = 3


class UnifiedDiffTag(IntEnum):
    LINES = 3


class UnifiedDiffLineTag(IntEnum):
    TEXT = 1
    TYPE = 2


class UnifiedDiffLineTypeCode(IntEnum):
    UNSPECIFIED = 0
    INSERT = 1
    DELETE = 2
    UNCHANGED = 3


class DiffStatsTag(IntEnum):
    ADDITIONS = 1
    DELETIONS = 2


class ArtifactMetaTag(IntEnum):
    SUMMARY = 2


class RunCommandTag(IntEnum):
    EXIT_CODE = 1
    TIMED_OUT = 2
    SANDBOX_DETAILS = 3
    IS_TRUSTED_CLI = 4
    SHELL_NAME = 5
    COMMAND_LINE = 6
    CWD = 7
    EXIT_CODE_UNKNOWN = 8


class SandboxDetailsTag(IntEnum):
    RAN_IN_SANDBOX = 1
    SANDBOX_ERRORS = 2


class SearchWebTag(IntEnum):
    QUERY = 1
    DOMAIN = 2
    WEB_DOCUMENTS = 3
    SUMMARY = 4


class SearchDocTag(IntEnum):
    SNIPPET = 2
    URL = 3
    TITLE = 4
    FALLBACK_TEXT = 7


class InvokeSubagentTag(IntEnum):
    SUBAGENTS = 1
    RESULTS = 2
    TASK_MODE = 3


class SubagentSpecFieldTag(IntEnum):
    TYPE_NAME = 1
    ROLE = 2
    PROMPT = 3
    INHERIT_WORKSPACE = 4
    BRANCH_WORKSPACE = 5
    SHARE_WORKSPACE = 10


class SubagentResultFieldTag(IntEnum):
    CONVERSATION_ID = 1


class DefineSubagentTag(IntEnum):
    NAME = 1
    TOOL_NAMES = 2
    AGENT_PATH = 3


class ManageSubagentsTag(IntEnum):
    ACTION = 1
    CONVERSATION_IDS = 2
    SUBAGENTS = 3


class SubagentStateFieldTag(IntEnum):
    CONVERSATION_ID = 1


class ManageTaskTag(IntEnum):
    ACTION = 1
    TASK_IDS = 2
    INPUT = 3


class SendMessageTag(IntEnum):
    RECIPIENT = 1
    RECIPIENT_NAME = 2
    MESSAGE = 3


class ScheduleTag(IntEnum):
    MODE = 1
    OUTCOME = 2
    ITERATIONS = 3
    CANCELLED_BY = 4


class AskQuestionTag(IntEnum):
    QUESTIONS = 1


class AskQuestionEntryTag(IntEnum):
    QUESTION = 1
    OPTIONS = 2
    IS_MULTI_SELECT = 3
    USER_ANSWERS = 4
    ALT_ANSWERS = 5


class QuestionOptionFieldTag(IntEnum):
    ID = 1
    TEXT = 2


class ListResourcesTag(IntEnum):
    SERVER_NAME = 1
    RESOURCES = 2


class McpResourceFieldTag(IntEnum):
    URI = 1


class ReadResourceTag(IntEnum):
    SERVER_NAME = 1
    URI = 2
    CONTENTS = 3


class McpResourceContentFieldTag(IntEnum):
    URI = 1
    TEXT_CONTENT = 2


class McpTextContentFieldTag(IntEnum):
    TEXT = 1


class GenerateImageTag(IntEnum):
    PROMPT = 1
    IMAGE_PATHS = 2
    ASPECT_RATIO = 3
    IMAGE_URI = 4
    MODEL_NAME = 6


class MomaSearchTag(IntEnum):
    QUERY = 1
    RESULTS = 2


class FinishTag(IntEnum):
    ARGUMENTS_JSON = 1
    HAS_RESULT_SCHEMA = 2


@dataclass(frozen=True)
class StepWireSpec:
    """Declarative specification for one `gemini_coder.Step` active variant."""

    step_type: CortexStepTypeCode
    step_oneof_tag: GeminiStepWireTag
    oneof_field_name: str
    proto_message_full_name: str
    transcript_type_label: str
    codex_record_kind: Literal["response_item", "response_item_bundle", "event_msg", "turn_context", "session_meta"]
    emits_multiple_canonical_events: bool


STEP_WIRE_TABLE: Mapping[CortexStepTypeCode, StepWireSpec] = {
    CortexStepTypeCode.USER_INPUT: StepWireSpec(
        CortexStepTypeCode.USER_INPUT, GeminiStepWireTag.USER_INPUT_TAG,
        "user_input", "exa.cortex_pb.CortexStepUserInput", "USER_INPUT", "response_item", False,
    ),
    CortexStepTypeCode.PLANNER_RESPONSE: StepWireSpec(
        CortexStepTypeCode.PLANNER_RESPONSE, GeminiStepWireTag.PLANNER_RESPONSE_TAG,
        "planner_response", "exa.cortex_pb.CortexStepPlannerResponse", "PLANNER_RESPONSE", "response_item_bundle", True,
    ),
    CortexStepTypeCode.CHECKPOINT: StepWireSpec(
        CortexStepTypeCode.CHECKPOINT, GeminiStepWireTag.CHECKPOINT_TAG,
        "checkpoint", "exa.cortex_pb.CortexStepCheckpoint", "CHECKPOINT", "turn_context", False,
    ),
    CortexStepTypeCode.TASK_BOUNDARY: StepWireSpec(
        CortexStepTypeCode.TASK_BOUNDARY, GeminiStepWireTag.TASK_BOUNDARY_TAG,
        "task_boundary", "exa.cortex_pb.CortexStepTaskBoundary", "TASK_BOUNDARY", "event_msg", False,
    ),
    CortexStepTypeCode.NOTIFY_USER: StepWireSpec(
        CortexStepTypeCode.NOTIFY_USER, GeminiStepWireTag.NOTIFY_USER_TAG,
        "notify_user", "exa.cortex_pb.CortexStepNotifyUser", "NOTIFY_USER", "event_msg", False,
    ),
    CortexStepTypeCode.SYSTEM_MESSAGE: StepWireSpec(
        CortexStepTypeCode.SYSTEM_MESSAGE, GeminiStepWireTag.SYSTEM_MESSAGE_TAG,
        "system_message", "exa.cortex_pb.CortexStepSystemMessage", "SYSTEM_MESSAGE", "session_meta", False,
    ),
    CortexStepTypeCode.GENERIC: StepWireSpec(
        CortexStepTypeCode.GENERIC, GeminiStepWireTag.GENERIC_TAG,
        "generic", "exa.cortex_pb.CortexStepGeneric", "GENERIC", "response_item_bundle", False,
    ),
}

STEP_TAG_TO_SPEC: Mapping[GeminiStepWireTag, StepWireSpec] = {
    spec.step_oneof_tag: spec for spec in STEP_WIRE_TABLE.values()
}


@dataclass(frozen=True)
class ToolCodecSpec:
    """Table-driven bidirectional codec entry for one `CanonicalToolPayload` variant."""

    payload_kind: str
    agy_tool_name: str
    agy_any_type_url: str
    codex_call_kind: Literal["function_call", "custom_tool_call"]
    codex_tool_name: str
    codex_fallback_tool_names: Tuple[str, ...] = ()
    encode_agy_any_fn: Callable[[Any], bytes] | None = None
    decode_agy_any_fn: Callable[[Mapping[str, Any], str, bytes], CanonicalToolPayload] | None = None
    serialize_args_fn: Callable[[Any], Mapping[str, Any]] | None = None
    encode_codex_fn: Callable[[Any], tuple[str, str, str, str | None]] | None = None
    decode_codex_fn: Callable[[str, Mapping[str, Any], str], CanonicalToolPayload] | None = None
    extract_result_meta_fn: Callable[[Any], Mapping[str, Any]] | None = None


@dataclass(frozen=True)
class AgyRawStepRow:
    """Raw row read from or written to `conversations/<cascade_id>.db` `steps` table."""

    idx: StepIndex
    step_type: int
    status: int
    has_subtrajectory: bool
    metadata_blob: bytes
    step_payload_blob: bytes
    step_format: int = 1


@dataclass(frozen=True)
class CodexRawRolloutLine:
    """Single JSONL line read from or written to `~/.codex/sessions/**/rollout-*.jsonl`."""

    line_no: RolloutLineNo
    raw_json: str
