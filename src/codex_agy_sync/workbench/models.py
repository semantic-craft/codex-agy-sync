"""Typed domain models for the Stage-1 Local Workbench."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


STAGE_1_CAPABILITIES: dict[str, bool] = {
    "readonly_history": True,
    "cross_host_search": True,
    "citation_draft_copy": True,
    "desktop_handoff": True,
    "remote_message_send": False,
}


def _to_plain_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _to_plain_value(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(k): _to_plain_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_plain_value(item) for item in value]
    return value


class _DictSerializable:
    def to_dict(self) -> dict[str, Any]:
        return _to_plain_value(self)


@dataclass(frozen=True)
class WorkspaceCard(_DictSerializable):
    workspace_id: str
    workspace_path: str
    workspace_uri: str
    name: str
    agy_registered: bool
    codex_registered: bool
    is_paired: bool
    agy_session_count: int
    codex_session_count: int
    work_count: int
    last_updated_iso: str
    diagnosis: dict[str, Any]


@dataclass(frozen=True)
class WorkSessionLink(_DictSerializable):
    host: str
    session_id: str
    title: str
    updated_at_iso: str
    turn_or_step_count: int
    is_baton_target: bool = False
    handoff_status: str | None = None
    handoff_direction: str | None = None


@dataclass(frozen=True)
class HandoffHopSummary(_DictSerializable):
    receipt_key: str
    source_host: str
    source_id: str
    target_host: str
    target_id: str | None
    status: str
    created_at_iso: str
    chain_id: str | None
    chain_depth: int
    title: str


@dataclass(frozen=True)
class TimelineTurn(_DictSerializable):
    turn_id: str
    seq: int
    host: str
    session_id: str
    session_title: str
    timestamp_iso: str
    user_text: str
    assistant_text: str
    tool_summary: str
    is_inflight: bool
    has_thinking: bool


@dataclass(frozen=True)
class WorkCard(_DictSerializable):
    work_id: str
    workspace_id: str
    workspace_path: str
    title: str
    updated_at_iso: str
    active_host: str
    latest_source_id: str
    latest_direction: str
    chain_id: str | None
    chain_depth: int
    session_count: int
    turn_count: int
    is_paired: bool
    sessions: tuple[WorkSessionLink, ...]
    handoffs: tuple[HandoffHopSummary, ...]
    turns: tuple[TimelineTurn, ...]


@dataclass(frozen=True)
class SearchHit(_DictSerializable):
    hit_id: str
    workspace_id: str
    workspace_name: str
    work_id: str
    work_title: str
    host: str
    session_id: str
    turn_id: str
    timestamp_iso: str
    role: str
    snippet: str
    score: int


@dataclass(frozen=True)
class CitationRef(_DictSerializable):
    workspace_id: str
    work_id: str
    host: str
    session_id: str
    turn_id: str = ""
    excerpt: str = ""
    label: str = ""


@dataclass(frozen=True)
class DraftRenderResult(_DictSerializable):
    workspace_path: str
    work_id: str
    target_hint: str
    markdown: str
    citation_count: int
    notice: str
    stage: Literal["stage_1_copy_only"] = "stage_1_copy_only"
    delivery_executed: bool = False


@dataclass(frozen=True)
class WorkbenchSnapshot(_DictSerializable):
    generated_at_iso: str
    workspaces: tuple[WorkspaceCard, ...]
    works: tuple[WorkCard, ...]
    selected_workspace_id: str | None = None
    stage: Literal["stage_1"] = "stage_1"
    capabilities: Mapping[str, bool] = field(default_factory=lambda: dict(STAGE_1_CAPABILITIES))
