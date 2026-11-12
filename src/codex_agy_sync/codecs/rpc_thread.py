"""Codex `app-server` RPC `thread/read` decoder and rollout prefix splicer."""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Literal, Mapping

from ..ledger import (
    AssistantTextEvent,
    CanonicalEvent,
    EventOriginAnchor,
    FileEditPayload,
    HostKind,
    MediaAttachment,
    OpaqueToolPayload,
    RunCommandPayload,
    SearchWebPayload,
    SystemEvent,
    TaskBoundaryEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolCallId,
    ToolResultEvent,
    TrajectoryLedger,
    TurnId,
    UserInputEvent,
    WorkspacePath,
    WorkspaceRootUri,
    normalize_tool_file_path,
)

_COMPLETED_ITEM_STATUSES = frozenset({"completed", "failed", "declined", "aborted", "cancelled", "canceled"})
_ERROR_ITEM_STATUSES = frozenset({"failed", "declined", "aborted", "cancelled", "canceled", "error"})


def _extract_rpc_content_blocks(content_blocks: Any) -> tuple[str, tuple[MediaAttachment, ...]]:
    if isinstance(content_blocks, str):
        return content_blocks, ()
    texts: list[str] = []
    media: list[MediaAttachment] = []
    if isinstance(content_blocks, list):
        for blk in content_blocks:
            if isinstance(blk, Mapping):
                if blk.get("text") is not None:
                    texts.append(str(blk["text"]))
                elif blk.get("type") in ("input_image", "image"):
                    media.append(
                        MediaAttachment(
                            mime_type=str(blk.get("mime_type") or "image/png"),
                            uri=str(blk.get("image_url") or blk.get("uri") or ""),
                        )
                    )
    return "\n".join(texts), tuple(media)


def _stringify_rpc_output(raw_out: Any) -> str:
    if isinstance(raw_out, list):
        return "\n".join(
            str(it["text"]) if isinstance(it, Mapping) and "text" in it else json.dumps(it, ensure_ascii=False)
            for it in raw_out
        )
    if isinstance(raw_out, Mapping):
        return json.dumps(dict(raw_out), ensure_ascii=False, sort_keys=True)
    return str(raw_out or "")


def _item_status_flags(item: Mapping[str, Any]) -> tuple[bool, bool]:
    raw_st = item.get("status")
    st_lc = str((raw_st.get("type") if isinstance(raw_st, Mapping) else raw_st) or "completed").strip().lower()
    return st_lc in _COMPLETED_ITEM_STATUSES, st_lc in _ERROR_ITEM_STATUSES


def _map_file_change_op(raw_kind: Any) -> Literal["ADD", "DELETE", "MODIFY"]:
    kind_lc = str((raw_kind.get("type") if isinstance(raw_kind, Mapping) else raw_kind) or "").strip().lower()
    if kind_lc in ("add", "create"):
        return "ADD"
    if kind_lc == "delete":
        return "DELETE"
    return "MODIFY"


def _decode_rpc_turn_item(
    item: Mapping[str, Any], anchor: EventOriginAnchor, t_idx: int, workspace_path: WorkspacePath, events: list[CanonicalEvent],
) -> None:
    itype = str(item.get("type") or "")
    is_completed, is_error = _item_status_flags(item)
    if itype == "userMessage":
        raw_c = item.get("content") if item.get("content") is not None else (item.get("text") or "")
        txt, media = _extract_rpc_content_blocks(raw_c)
        events.append(UserInputEvent(kind="user_input", anchor=anchor, query_text=txt, media=media))
        return
    if itype == "agentMessage":
        am_txt = str(item["text"]) if item.get("text") is not None else _extract_rpc_content_blocks(item.get("content") or [])[0]
        events.append(AssistantTextEvent(kind="assistant_text", anchor=anchor, text=am_txt, message_id=str(item["id"]) if item.get("id") else None))
        return
    if itype == "reasoning":
        s_list = item.get("summary") or []
        summary = "\n".join(str(x) for x in s_list) if isinstance(s_list, list) else str(s_list or "")
        events.append(ThinkingEvent(kind="thinking", anchor=anchor, summary_text=summary))
        return
    if itype == "commandExecution":
        cid = ToolCallId(str(item.get("id") or f"rpc-cmd-{t_idx}-{len(events)}"))
        exit_c = int(item["exitCode"]) if item.get("exitCode") is not None else (0 if (is_completed and not is_error) else None)
        pl = RunCommandPayload(
            kind="run_command", command_line=str(item.get("command") or ""),
            cwd=normalize_tool_file_path(str(item.get("cwd") or workspace_path)), exit_code=exit_c,
        )
        events.append(ToolCallEvent(kind="tool_call", anchor=anchor, call_id=cid, tool_name="exec_command", tool_summary="exec_command", payload=pl))
        if is_completed:
            events.append(ToolResultEvent(kind="tool_result", anchor=anchor, call_id=cid, tool_name="exec_command", output_text=str(item.get("aggregatedOutput") or ""), is_error=is_error, payload=pl))
        return
    if itype == "fileChange":
        base_cid = str(item.get("id") or f"rpc-patch-{t_idx}-{len(events)}")
        raw_changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        changes = [ch for ch in raw_changes if isinstance(ch, Mapping)] or [{}]
        for idx, ch in enumerate(changes):
            cid = ToolCallId(base_cid if idx == 0 else f"{base_cid}:{idx}")
            f_pl = FileEditPayload(
                kind="file_edit", path=normalize_tool_file_path(str(ch.get("path") or "")),
                edit_op=_map_file_change_op(ch.get("kind")), unified_diff=str(ch.get("diff") or ""),
            )
            events.append(ToolCallEvent(kind="tool_call", anchor=anchor, call_id=cid, tool_name="apply_patch", tool_summary="apply_patch", payload=f_pl))
            if is_completed:
                events.append(ToolResultEvent(kind="tool_result", anchor=anchor, call_id=cid, tool_name="apply_patch", output_text="Applied patch", is_error=is_error, payload=f_pl))
        return
    if itype == "webSearch":
        cid = ToolCallId(str(item.get("id") or f"rpc-ws-{t_idx}-{len(events)}"))
        w_pl = SearchWebPayload(kind="search_web", query=str(item.get("query") or ""))
        events.append(ToolCallEvent(kind="tool_call", anchor=anchor, call_id=cid, tool_name="web_search", tool_summary="web_search", payload=w_pl))
        if is_completed:
            events.append(ToolResultEvent(kind="tool_result", anchor=anchor, call_id=cid, tool_name="web_search", output_text=f"Web search: {w_pl.query}", is_error=is_error, payload=w_pl))
        return
    if itype in ("mcpToolCall", "dynamicToolCall", "collabToolCall") or itype.endswith("ToolCall"):
        cid = ToolCallId(str(item.get("id") or f"rpc-tool-{t_idx}-{len(events)}"))
        tname = str(item.get("tool") or item.get("name") or itype)
        raw_args = item["arguments"] if item.get("arguments") is not None else item
        args_json = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, sort_keys=True, ensure_ascii=False)
        o_pl = OpaqueToolPayload(kind="opaque", tool_name=tname, raw_arguments_json=args_json)
        events.append(ToolCallEvent(kind="tool_call", anchor=anchor, call_id=cid, tool_name=tname, tool_summary=tname, payload=o_pl))
        if is_completed:
            raw_res = item.get("result") if item.get("result") is not None else (item.get("error") if is_error else "")
            events.append(ToolResultEvent(kind="tool_result", anchor=anchor, call_id=cid, tool_name=tname, output_text=_stringify_rpc_output(raw_res), is_error=is_error or bool(item.get("error")), payload=o_pl))


def decode_codex_rpc_thread_to_ledger(
    workspace_path: WorkspacePath,
    workspace_uri: WorkspaceRootUri,
    thread_obj: Mapping[str, Any],
    *,
    explicit_title_override: str | None = None,
) -> TrajectoryLedger:
    """Translate a Codex `app-server` `thread/read` payload (`turns[*].items[*]`) into `TrajectoryLedger`."""
    events: list[CanonicalEvent] = []
    ts_default = str(thread_obj.get("updatedAt") or thread_obj.get("createdAt") or "2026-01-01T00:00:00Z")
    for t_idx, turn in enumerate(thread_obj.get("turns") or (), 1):
        if not isinstance(turn, Mapping):
            continue
        anchor = EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=TurnId(f"turn-{t_idx}"), timestamp_iso=ts_default)
        for item in turn.get("items") or ():
            if isinstance(item, Mapping):
                _decode_rpc_turn_item(item, anchor, t_idx, workspace_path, events)
        raw_t_status = turn.get("status")
        t_status = str((raw_t_status.get("type") if isinstance(raw_t_status, Mapping) else raw_t_status) or "completed").lower()
        if t_status == "completed":
            events.append(TaskBoundaryEvent(kind="task_boundary", anchor=anchor, boundary_kind="task_boundary", task_name="", task_status="task_complete", task_summary="Turn complete"))
        elif t_status in ("inprogress", "in_progress"):
            events.append(TaskBoundaryEvent(kind="task_boundary", anchor=anchor, boundary_kind="task_boundary", task_name="", task_status="in_progress", task_summary=""))
        elif t_status in ("interrupted", "failed"):
            err_obj = turn.get("error")
            err_msg = str((err_obj.get("message") if isinstance(err_obj, Mapping) else err_obj) or turn.get("status") or t_status)
            events.append(SystemEvent(kind="system_message", anchor=anchor, event_type="codex_event_msg:turn_aborted", message_text=err_msg))
    return TrajectoryLedger.from_events(
        workspace_path=workspace_path, workspace_uri=workspace_uri, events=events,
        explicit_title_override=thread_obj.get("name") or thread_obj.get("title") or explicit_title_override,
    )


def splice_rollout_completed_prefix(
    disk_ledger: TrajectoryLedger,
    rpc_ledger: TrajectoryLedger,
    *,
    rpc_is_active: bool,
    explicit_title_override: str | None = None,
) -> TrajectoryLedger:
    """Splice flushed completed rollout turns ahead of newer RPC turns to preserve exact rollout digests."""
    disk_wm = disk_ledger.latest_watermark()
    if disk_wm is None:
        return dataclasses.replace(rpc_ledger, has_inflight_turn=True) if (rpc_is_active and not rpc_ledger.has_inflight_turn) else rpc_ledger
    disk_completed_turns = {wm.turn_id for wm in disk_ledger.completed_turn_watermarks}
    rpc_tail = [ev for ev in rpc_ledger.events if ev.anchor.turn_id not in disk_completed_turns]
    if not rpc_tail:
        if not rpc_is_active and len(disk_ledger.completed_turn_watermarks) >= len(rpc_ledger.completed_turn_watermarks) and disk_ledger.has_inflight_turn == rpc_ledger.has_inflight_turn:
            return disk_ledger
        return dataclasses.replace(disk_ledger, has_inflight_turn=True) if (rpc_is_active and not disk_ledger.has_inflight_turn) else disk_ledger
    prefix_events = list(disk_ledger.events[: int(disk_wm.last_ledger_seq) + 1])
    cleared_tail = [
        dataclasses.replace(ev, anchor=dataclasses.replace(ev.anchor, agy_step_index=None, codex_line_no=None))
        for ev in rpc_tail
    ]
    merged = TrajectoryLedger.from_events(
        rpc_ledger.workspace_path, rpc_ledger.workspace_uri, [*prefix_events, *cleared_tail],
        explicit_title_override=explicit_title_override or rpc_ledger.title,
    )
    return dataclasses.replace(merged, has_inflight_turn=True) if (rpc_is_active and not merged.has_inflight_turn) else merged
