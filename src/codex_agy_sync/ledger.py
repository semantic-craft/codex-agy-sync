"""Tier 1 canonical indexed event-ledger IR, rescue logic, and pure domain projections."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import hashlib
import json
from typing import Mapping, Sequence, Tuple

from ._wire import UnknownWireField, parse_iso_to_seconds_nanos
from .events import (
    INTERRUPTED_TOOL_OUTPUT,
    INTERRUPTED_TURN_TEXT,
    AnnotationPbtxtProjection,
    AssistantTextEvent,
    CanonicalEvent,
    CascadeSummaryProjection,
    CheckpointEvent,
    CodexThreadSummaryProjection,
    CompletedTurnWatermark,
    EventOriginAnchor,
    HostExtensionEnvelope,
    MediaAttachment,
    SubagentLineage,
    SummariesStateProjection,
    SystemEvent,
    TaskBoundaryEvent,
    ThinkingEvent,
    TokenUsageAccounting,
    ToolCallEvent,
    ToolResultEvent,
    TranscriptRowProjection,
    UserInputEvent,
    canonical_event_digest_payload,
    is_host_framing_event,
    is_host_injected_tool_result,
    is_host_injected_user_event,
    is_nonterminal_work_or_abort_event,
    is_terminal_completion_event,
    normalize_single_line_query,
    resolve_canonical_title,
)
from .payloads import (
    AskQuestionPayload,
    CanonicalToolPayload,
    DefineSubagentPayload,
    FileEditPayload,
    FinishPayload,
    GenerateImagePayload,
    InvokeSubagentPayload,
    ListResourcesPayload,
    ManageSubagentsPayload,
    ManageTaskPayload,
    MomaSearchPayload,
    OpaqueToolPayload,
    QuestionItem,
    QuestionOption,
    ReadFilePayload,
    ReadResourcePayload,
    RunCommandPayload,
    SchedulePayload,
    SearchWebDocument,
    SearchWebPayload,
    SendMessagePayload,
    serialize_tool_payload_arguments_json,
)
from .workspace import (
    FORBIDDEN_AGY_PROJECT_IDS,
    AgyGuiSafeSource,
    AgyProjectId,
    CascadeId,
    CodexLegacyProjectId,
    CodexProjectId,
    CodexThreadId,
    ContentDigest,
    HostKind,
    LedgerSeqNo,
    RolloutLineNo,
    SessionId,
    StepIndex,
    ToolCallId,
    TrajectoryId,
    TurnId,
    WorkspacePath,
    WorkspaceRootUri,
    normalize_tool_file_path,
    parse_workspace_identity,
    validate_session_id,
)


def _index_ledger_events(
    events: Sequence[CanonicalEvent],
    *,
    recompute_agy_step_indices: bool,
) -> tuple[
    list[CanonicalEvent],
    dict[StepIndex, list[LedgerSeqNo]],
    dict[ToolCallId, tuple[LedgerSeqNo, LedgerSeqNo | None]],
    dict[TurnId, list[LedgerSeqNo]],
]:
    normalized: list[CanonicalEvent] = []
    by_step: dict[StepIndex, list[LedgerSeqNo]] = {}
    by_call: dict[ToolCallId, tuple[LedgerSeqNo, LedgerSeqNo | None]] = {}
    by_turn: dict[TurnId, list[LedgerSeqNo]] = {}
    next_step, next_line = 0, 1
    planner_step: StepIndex | None = None
    planner_turn: TurnId | None = None
    planner_flags = (False, False, False)

    for idx, ev in enumerate(events):
        seq_no, anchor = LedgerSeqNo(idx), ev.anchor
        if not recompute_agy_step_indices and anchor.agy_step_index is not None:
            step_idx, next_step = anchor.agy_step_index, max(next_step, int(anchor.agy_step_index) + 1)
            planner_step, planner_flags = None, (False, False, False)
        elif isinstance(ev, (ThinkingEvent, AssistantTextEvent, ToolCallEvent)):
            has_think, has_text, has_tools = planner_flags
            can_reuse = (
                planner_step is not None
                and planner_turn == anchor.turn_id
                and (
                    (isinstance(ev, ThinkingEvent) and not (has_think or has_text or has_tools))
                    or (isinstance(ev, AssistantTextEvent) and not (has_text or has_tools))
                    or isinstance(ev, ToolCallEvent)
                )
            )
            if not can_reuse:
                planner_step, next_step, planner_turn = StepIndex(next_step), next_step + 1, anchor.turn_id
                has_think, has_text, has_tools = (False, False, False)
            planner_flags = (
                has_think or isinstance(ev, ThinkingEvent),
                has_text or isinstance(ev, AssistantTextEvent),
                has_tools or isinstance(ev, ToolCallEvent),
            )
            step_idx = planner_step
        else:
            planner_step, planner_flags = None, (False, False, False)
            step_idx, next_step = StepIndex(next_step), next_step + 1

        if anchor.codex_line_no is not None:
            line_no, next_line = anchor.codex_line_no, max(next_line, int(anchor.codex_line_no) + 1)
        else:
            line_no, next_line = RolloutLineNo(next_line), next_line + 1

        if recompute_agy_step_indices or anchor.agy_step_index != step_idx or anchor.codex_line_no != line_no:
            ev = dataclasses.replace(ev, anchor=dataclasses.replace(anchor, agy_step_index=step_idx, codex_line_no=line_no))
        normalized.append(ev)
        by_step.setdefault(step_idx, []).append(seq_no)
        by_turn.setdefault(ev.anchor.turn_id, []).append(seq_no)
        if isinstance(ev, ToolCallEvent):
            by_call[ev.call_id] = (seq_no, None)
        elif isinstance(ev, ToolResultEvent):
            existing = by_call.get(ev.call_id)
            by_call[ev.call_id] = (existing[0], seq_no) if existing is not None else (seq_no, seq_no)
    return normalized, by_step, by_call, by_turn


def _is_turn_completed(
    turn_events: Sequence[CanonicalEvent],
    by_call: Mapping[ToolCallId, tuple[LedgerSeqNo, LedgerSeqNo | None]],
) -> bool:
    if not turn_events:
        return True
    all_calls_resolved = all(
        (pair := by_call.get(tev.call_id)) is not None and pair[1] is not None
        for tev in turn_events if isinstance(tev, ToolCallEvent)
    )
    has_active_agy = any(
        ext.extension_type == "agy_step_status"
        for tev in turn_events for ext in tev.anchor.host_extensions
    )
    last_work = max((i for i, tev in enumerate(turn_events) if is_nonterminal_work_or_abort_event(tev)), default=-1)
    last_comp = max((i for i, tev in enumerate(turn_events) if is_terminal_completion_event(tev)), default=-1)
    return all_calls_resolved and not has_active_agy and last_work <= last_comp


def _build_turn_watermarks(
    normalized_events: Sequence[CanonicalEvent],
    by_turn: Mapping[TurnId, Sequence[LedgerSeqNo]],
    by_call: Mapping[ToolCallId, tuple[LedgerSeqNo, LedgerSeqNo | None]],
) -> tuple[Tuple[CompletedTurnWatermark, ...], bool]:
    watermarks: list[CompletedTurnWatermark] = []
    cumulative_hasher = hashlib.sha256()
    turn_items = list(by_turn.items())

    for turn_pos, (turn_id, seq_list) in enumerate(turn_items):
        turn_events = [normalized_events[int(s)] for s in seq_list]
        if turn_pos == len(turn_items) - 1 and not _is_turn_completed(turn_events, by_call):
            return tuple(watermarks), True

        max_step_idx, max_line_no = StepIndex(0), RolloutLineNo(1)
        for tev in turn_events:
            if not is_host_framing_event(tev):
                payload_bytes = json.dumps(
                    canonical_event_digest_payload(tev), sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                cumulative_hasher.update(payload_bytes + b"\n")
            if tev.anchor.agy_step_index is not None and int(tev.anchor.agy_step_index) > int(max_step_idx):
                max_step_idx = tev.anchor.agy_step_index
            if tev.anchor.codex_line_no is not None and int(tev.anchor.codex_line_no) > int(max_line_no):
                max_line_no = tev.anchor.codex_line_no
        watermarks.append(
            CompletedTurnWatermark(
                turn_id, seq_list[-1], max_step_idx, max_line_no, ContentDigest(cumulative_hasher.hexdigest())
            )
        )
    return tuple(watermarks), False


def rescue_interrupted_turn(
    ledger: TrajectoryLedger,
    target_turn_id: TurnId | None = None,
) -> tuple[TrajectoryLedger, bool]:
    """Seal an incomplete turn (final or targeted `target_turn_id`) with canceled tool outputs and interruption text."""
    if not ledger.events or not ledger.by_turn_id:
        return ledger, False
    if target_turn_id is None:
        if not ledger.has_inflight_turn:
            return ledger, False
        target_tid = list(ledger.by_turn_id.keys())[-1]
    else:
        if target_turn_id not in ledger.by_turn_id:
            return ledger, False
        target_tid = target_turn_id
    raw_target = [ledger.events[int(s)] for s in ledger.by_turn_id[target_tid]]
    if _is_turn_completed(raw_target, ledger.by_call_id):
        return ledger, False

    target_seqs = {int(s) for s in ledger.by_turn_id[target_tid]}
    max_target_seq = max(target_seqs)
    rescued_events: list[CanonicalEvent] = []
    turn_slice: list[CanonicalEvent] = []
    stripped_on_asst = False
    for idx, ev in enumerate(ledger.events[: max_target_seq + 1]):
        if idx in target_seqs and any(ext.extension_type == "agy_step_status" for ext in ev.anchor.host_extensions):
            kept = tuple(ext for ext in ev.anchor.host_extensions if ext.extension_type != "agy_step_status")
            stripped_on_asst = stripped_on_asst or isinstance(ev, AssistantTextEvent)
            ev = dataclasses.replace(ev, anchor=dataclasses.replace(ev.anchor, host_extensions=kept))
        rescued_events.append(ev)
        if idx in target_seqs:
            turn_slice.append(ev)

    last_ev = turn_slice[-1]
    last_ts = last_ev.anchor.timestamp_iso
    for tev in list(turn_slice):
        if isinstance(tev, ToolCallEvent) and ((pair := ledger.by_call_id.get(tev.call_id)) is None or pair[1] is None):
            res_ev = ToolResultEvent(
                kind="tool_result",
                anchor=EventOriginAnchor(origin_host=tev.anchor.origin_host, turn_id=target_tid, timestamp_iso=last_ts),
                call_id=tev.call_id, tool_name=tev.tool_name, output_text=INTERRUPTED_TOOL_OUTPUT,
                is_error=True, payload=tev.payload,
            )
            rescued_events.append(res_ev)
            turn_slice.append(res_ev)
    last_work = max((i for i, e in enumerate(turn_slice) if is_nonterminal_work_or_abort_event(e)), default=-1)
    last_asst = max((i for i, e in enumerate(turn_slice) if isinstance(e, AssistantTextEvent)), default=-1)
    if stripped_on_asst or last_asst == -1 or last_work > last_asst:
        rescued_events.append(
            AssistantTextEvent(
                kind="assistant_text",
                anchor=EventOriginAnchor(origin_host=last_ev.anchor.origin_host, turn_id=target_tid, timestamp_iso=last_ts),
                text=INTERRUPTED_TURN_TEXT,
            )
        )
    for trailing in ledger.events[max_target_seq + 1 :]:
        rescued_events.append(
            dataclasses.replace(trailing, anchor=dataclasses.replace(trailing.anchor, agy_step_index=None, codex_line_no=None))
        )
    return TrajectoryLedger.from_events(ledger.workspace_path, ledger.workspace_uri, rescued_events, explicit_title_override=ledger.title), True


def rescue_interrupted_ledger(ledger: TrajectoryLedger) -> tuple[TrajectoryLedger, bool]:
    """Seal a stopped trailing incomplete turn with canceled tool outputs and an interruption notice."""
    return rescue_interrupted_turn(ledger, None)


def derive_ledger_preview_text(ledger: TrajectoryLedger) -> str:
    """Derive single-line session preview text from user inputs, assistant messages, or title."""
    for ev in ledger.events:
        if isinstance(ev, UserInputEvent) and ev.query_text.strip():
            if normalized := normalize_single_line_query(ev.query_text, limit=160):
                return normalized
    for ev in ledger.events:
        if isinstance(ev, AssistantTextEvent) and ev.text.strip():
            if normalized := normalize_single_line_query(ev.text, limit=160):
                return normalized
    return ledger.title if ledger.title.strip() else "Synchronized Session"


def _project_planner_transcript_row(
    step_idx: StepIndex,
    created_at: str,
    step_events: Sequence[CanonicalEvent],
) -> TranscriptRowProjection:
    thinking_parts: list[str] = []
    text_parts: list[str] = []
    tool_calls: list[Mapping[str, object]] = []
    in_tok = out_tok = cache_tok = 0
    for ev in step_events:
        if isinstance(ev, (ThinkingEvent, AssistantTextEvent)):
            if isinstance(ev, ThinkingEvent) and (ev.summary_text or ev.raw_thinking):
                thinking_parts.append(ev.summary_text or ev.raw_thinking or "")
            elif isinstance(ev, AssistantTextEvent) and ev.text:
                text_parts.append(ev.text)
            u = ev.token_usage
            in_tok, out_tok, cache_tok = max(in_tok, u.input_tokens), max(out_tok, u.output_tokens), max(cache_tok, u.cache_read_tokens)
        elif isinstance(ev, ToolCallEvent):
            args = serialize_tool_payload_arguments_json(ev.payload)
            tool_calls.append({"id": str(ev.call_id), "name": ev.tool_name, "arguments_json": args})
    return TranscriptRowProjection(
        step_index=step_idx, source="MODEL", step_type_name="PLANNER_RESPONSE", status_name="DONE",
        created_at=created_at, content="\n".join(text_parts),
        thinking="\n".join(thinking_parts) if thinking_parts else None,
        tool_calls_json=tuple(tool_calls), input_tokens=in_tok, output_tokens=out_tok, cache_read_tokens=cache_tok,
    )


def project_ledger_transcript_rows(ledger: TrajectoryLedger) -> Tuple[TranscriptRowProjection, ...]:
    """Project `TrajectoryLedger` steps into Antigravity `transcript.jsonl` view rows."""
    rows: list[TranscriptRowProjection] = []
    for step_idx in sorted(ledger.by_step_index.keys(), key=int):
        step_events = ledger.events_for_step(step_idx)
        if not step_events:
            continue
        first = step_events[0]
        created_at = first.anchor.timestamp_iso or "2026-01-01T00:00:00Z"
        if isinstance(first, (ThinkingEvent, AssistantTextEvent, ToolCallEvent)):
            rows.append(_project_planner_transcript_row(step_idx, created_at, step_events))
        elif isinstance(first, UserInputEvent):
            media = tuple({"mime_type": m.mime_type, "uri": m.uri} for m in first.media)
            rows.append(TranscriptRowProjection(step_idx, "USER_EXPLICIT", "USER_INPUT", "DONE", created_at, first.query_text, media_uris=media))
        elif isinstance(first, ToolResultEvent):
            rows.append(TranscriptRowProjection(step_idx, "MODEL", "GENERIC", "ERROR" if first.is_error else "DONE", created_at, first.output_text))
        elif isinstance(first, CheckpointEvent):
            rows.append(TranscriptRowProjection(step_idx, "SYSTEM", "CHECKPOINT", "DONE", created_at, first.session_summary or first.user_intent))
        elif isinstance(first, TaskBoundaryEvent):
            label = "NOTIFY_USER" if first.boundary_kind == "notify_user" else "TASK_BOUNDARY"
            rows.append(TranscriptRowProjection(step_idx, "MODEL", label, "DONE", created_at, first.task_summary or first.task_name))
        elif isinstance(first, SystemEvent):
            rows.append(TranscriptRowProjection(step_idx, "SYSTEM", "SYSTEM_MESSAGE", "DONE", created_at, first.message_text))
    return tuple(rows)


def project_ledger_cascade_summary(
    ledger: TrajectoryLedger,
    cascade_id: CascadeId,
    project_id: AgyProjectId,
    viewed_at_iso: str | None = None,
    source: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
    lineage: SubagentLineage | None = None,
) -> CascadeSummaryProjection:
    """Project `TrajectoryLedger` into `CascadeSummaryProjection`."""
    created_iso = ledger.events[0].anchor.timestamp_iso if ledger.events else "2026-01-01T00:00:00Z"
    updated_iso = viewed_at_iso or (ledger.events[-1].anchor.timestamp_iso if ledger.events else created_iso)
    annot = ledger.project_annotation_pbtxt(cascade_id=cascade_id, viewed_at_iso=updated_iso)
    parent_cid = lineage.parent_cascade_id if lineage is not None else None
    root_cid = (lineage.root_cascade_id or parent_cid) if lineage is not None else None
    type_name, role = (lineage.agent_type_name, lineage.agent_role) if lineage is not None else ("", "")
    return CascadeSummaryProjection(
        cascade_id=cascade_id, project_id=project_id, workspace_uris=(ledger.workspace_uri,),
        source=source, title=ledger.title, preview=derive_ledger_preview_text(ledger),
        step_count=len(ledger.by_step_index), created_at_iso=created_iso, updated_at_iso=updated_iso,
        annotations=annot, nesting_depth=lineage.nesting_depth if lineage is not None else 0,
        parent_cascade_id=parent_cid, root_cascade_id=root_cid,
        agent_name=role or type_name, agent_type_name=type_name, agent_role=role,
    )


def project_ledger_codex_thread_summary(
    ledger: TrajectoryLedger,
    thread_id: CodexThreadId,
    project_id: CodexProjectId,
) -> CodexThreadSummaryProjection:
    """Project `TrajectoryLedger` into `CodexThreadSummaryProjection`."""
    created_iso = ledger.events[0].anchor.timestamp_iso if ledger.events else "2026-01-01T00:00:00Z"
    updated_iso = ledger.events[-1].anchor.timestamp_iso if ledger.events else created_iso
    c_sec, c_nanos = parse_iso_to_seconds_nanos(created_iso)
    u_sec, u_nanos = parse_iso_to_seconds_nanos(updated_iso)
    created_ms = c_sec * 1000 + (c_nanos // 1_000_000)
    updated_ms = max(created_ms, u_sec * 1000 + (u_nanos // 1_000_000))
    return CodexThreadSummaryProjection(
        thread_id=thread_id, project_id=project_id, cwd=ledger.workspace_path, title=ledger.title,
        preview=derive_ledger_preview_text(ledger), created_at_ms=created_ms, updated_at_ms=updated_ms, archived=0,
    )


@dataclass(frozen=True)
class TrajectoryLedger:
    """Append-only canonical event ledger with O(1) secondary lookup indices."""

    workspace_path: WorkspacePath
    workspace_uri: WorkspaceRootUri
    title: str
    events: Tuple[CanonicalEvent, ...]
    by_step_index: Mapping[StepIndex, Tuple[LedgerSeqNo, ...]]
    by_call_id: Mapping[ToolCallId, Tuple[LedgerSeqNo, LedgerSeqNo | None]]
    by_turn_id: Mapping[TurnId, Tuple[LedgerSeqNo, ...]]
    completed_turn_watermarks: Tuple[CompletedTurnWatermark, ...]
    has_inflight_turn: bool

    @classmethod
    def from_events(
        cls, workspace_path: WorkspacePath, workspace_uri: WorkspaceRootUri, events: Sequence[CanonicalEvent],
        *, explicit_title_override: str | None = None, recompute_agy_step_indices: bool = False,
    ) -> TrajectoryLedger:
        resolved_title = resolve_canonical_title(events, explicit_title_override=explicit_title_override)
        normalized, by_step, by_call, by_turn = _index_ledger_events(events, recompute_agy_step_indices=recompute_agy_step_indices)
        watermarks, has_inflight = _build_turn_watermarks(normalized, by_turn, by_call)
        return cls(
            workspace_path=workspace_path, workspace_uri=workspace_uri, title=resolved_title,
            events=tuple(normalized), by_step_index={k: tuple(v) for k, v in by_step.items()},
            by_call_id=dict(by_call), by_turn_id={k: tuple(v) for k, v in by_turn.items()},
            completed_turn_watermarks=watermarks, has_inflight_turn=has_inflight,
        )

    def latest_watermark(self) -> CompletedTurnWatermark | None:
        return self.completed_turn_watermarks[-1] if self.completed_turn_watermarks else None

    def find_watermark(self, turn_id: TurnId) -> CompletedTurnWatermark | None:
        return next((wm for wm in self.completed_turn_watermarks if wm.turn_id == turn_id), None)

    def events_for_step(self, step_index: StepIndex) -> Tuple[CanonicalEvent, ...]:
        return tuple(self.events[int(s)] for s in self.by_step_index.get(step_index, ()))

    def call_and_result(self, call_id: ToolCallId) -> Tuple[ToolCallEvent, ToolResultEvent | None]:
        if call_id not in self.by_call_id:
            raise KeyError(f"ToolCallId {call_id!r} not found in ledger.")
        call_seq, res_seq = self.by_call_id[call_id]
        call_ev = self.events[int(call_seq)]
        if not isinstance(call_ev, ToolCallEvent):
            raise ValueError(f"Expected ToolCallEvent at seq {call_seq}, found {type(call_ev).__name__}.")
        res_ev = self.events[int(res_seq)] if res_seq is not None and res_seq != call_seq else None
        return (call_ev, res_ev if isinstance(res_ev, ToolResultEvent) else None)

    def slice_after_watermark(self, cursor_turn_id: TurnId | CompletedTurnWatermark | None) -> Tuple[CanonicalEvent, ...]:
        if not self.completed_turn_watermarks:
            if cursor_turn_id is not None:
                raise KeyError(f"Turn {cursor_turn_id!r} not found in empty watermarks.")
            return ()
        last_completed_seq = int(self.completed_turn_watermarks[-1].last_ledger_seq)
        if cursor_turn_id is None:
            return self.events[: last_completed_seq + 1]
        target_id = cursor_turn_id.turn_id if isinstance(cursor_turn_id, CompletedTurnWatermark) else cursor_turn_id
        matched_wm = self.find_watermark(target_id)
        if matched_wm is None:
            raise KeyError(f"TurnId {target_id!r} not found in completed_turn_watermarks.")
        start_seq = int(matched_wm.last_ledger_seq) + 1
        return () if start_seq > last_completed_seq else self.events[start_seq : last_completed_seq + 1]

    def project_transcript_rows(self) -> Tuple[TranscriptRowProjection, ...]:
        return project_ledger_transcript_rows(self)

    def project_annotation_pbtxt(self, cascade_id: CascadeId, viewed_at_iso: str | None = None) -> AnnotationPbtxtProjection:
        ts = viewed_at_iso or (self.events[-1].anchor.timestamp_iso if self.events else "2026-01-01T00:00:00Z")
        return AnnotationPbtxtProjection(cascade_id=cascade_id, title=self.title, last_user_view_time_iso=ts)

    def project_agy_cascade_summary(
        self, cascade_id: CascadeId, project_id: AgyProjectId, viewed_at_iso: str | None = None,
        source: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT, lineage: SubagentLineage | None = None,
    ) -> CascadeSummaryProjection:
        return project_ledger_cascade_summary(self, cascade_id, project_id, viewed_at_iso=viewed_at_iso, source=source, lineage=lineage)

    def project_agy_hub_summaries_state(
        self, existing_state: SummariesStateProjection | None, updated_summary: CascadeSummaryProjection,
        conv_db_path: str, conv_db_mtime: int,
    ) -> SummariesStateProjection:
        merged_summaries = dict(existing_state.summaries) if existing_state is not None else {}
        merged_mtimes = dict(existing_state.file_mod_times) if existing_state is not None else {}
        unknowns = existing_state.unknown_wire_fields if existing_state is not None else ()
        merged_summaries[updated_summary.cascade_id] = updated_summary
        merged_mtimes[str(conv_db_path)] = int(conv_db_mtime)
        return SummariesStateProjection(summaries=merged_summaries, file_mod_times=merged_mtimes, unknown_wire_fields=unknowns)

    def project_codex_thread_summary(self, thread_id: CodexThreadId, project_id: CodexProjectId) -> CodexThreadSummaryProjection:
        return project_ledger_codex_thread_summary(self, thread_id, project_id)


__all__ = [
    "AgyGuiSafeSource",
    "AgyProjectId",
    "AnnotationPbtxtProjection",
    "AskQuestionPayload",
    "AssistantTextEvent",
    "CanonicalEvent",
    "CanonicalToolPayload",
    "CascadeId",
    "CascadeSummaryProjection",
    "CheckpointEvent",
    "CodexLegacyProjectId",
    "CodexProjectId",
    "CodexThreadId",
    "CodexThreadSummaryProjection",
    "CompletedTurnWatermark",
    "ContentDigest",
    "DefineSubagentPayload",
    "EventOriginAnchor",
    "FORBIDDEN_AGY_PROJECT_IDS",
    "FileEditPayload",
    "FinishPayload",
    "GenerateImagePayload",
    "HostExtensionEnvelope",
    "HostKind",
    "INTERRUPTED_TOOL_OUTPUT",
    "INTERRUPTED_TURN_TEXT",
    "InvokeSubagentPayload",
    "LedgerSeqNo",
    "ListResourcesPayload",
    "ManageSubagentsPayload",
    "ManageTaskPayload",
    "MediaAttachment",
    "MomaSearchPayload",
    "OpaqueToolPayload",
    "QuestionItem",
    "QuestionOption",
    "ReadFilePayload",
    "ReadResourcePayload",
    "RolloutLineNo",
    "RunCommandPayload",
    "SchedulePayload",
    "SearchWebDocument",
    "SearchWebPayload",
    "SendMessagePayload",
    "SessionId",
    "StepIndex",
    "SubagentLineage",
    "SummariesStateProjection",
    "SystemEvent",
    "TaskBoundaryEvent",
    "ThinkingEvent",
    "TokenUsageAccounting",
    "ToolCallEvent",
    "ToolCallId",
    "ToolResultEvent",
    "TrajectoryId",
    "TrajectoryLedger",
    "TranscriptRowProjection",
    "TurnId",
    "UnknownWireField",
    "UserInputEvent",
    "WorkspacePath",
    "WorkspaceRootUri",
    "canonical_event_digest_payload",
    "derive_ledger_preview_text",
    "is_host_framing_event",
    "is_host_injected_tool_result",
    "is_host_injected_user_event",
    "is_nonterminal_work_or_abort_event",
    "is_terminal_completion_event",
    "normalize_single_line_query",
    "normalize_tool_file_path",
    "parse_workspace_identity",
    "project_ledger_cascade_summary",
    "project_ledger_codex_thread_summary",
    "project_ledger_transcript_rows",
    "rescue_interrupted_ledger",
    "rescue_interrupted_turn",
    "resolve_canonical_title",
    "validate_session_id",
]
