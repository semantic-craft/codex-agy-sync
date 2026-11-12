"""Codex JSONL / `.jsonl.zst` rollout and RPC thread trajectory codecs."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence, Tuple

from ..ledger import (
    INTERRUPTED_TOOL_OUTPUT, AssistantTextEvent, CanonicalEvent, CanonicalToolPayload,
    CheckpointEvent, EventOriginAnchor, FileEditPayload, HostExtensionEnvelope, HostKind,
    MediaAttachment, OpaqueToolPayload, RolloutLineNo, RunCommandPayload,
    SearchWebDocument, SearchWebPayload, SystemEvent, TaskBoundaryEvent,
    ThinkingEvent, ToolCallEvent, ToolCallId, ToolResultEvent, TrajectoryLedger,
    TurnId, UserInputEvent, WorkspacePath, WorkspaceRootUri, normalize_tool_file_path,
)
from .schema_tags import CodexRawRolloutLine
from .tools import (
    TOOL_SPEC_BY_PAYLOAD_KIND, apply_tool_result_meta, decode_tool_payload,
    encode_tool_for_codex, extract_tool_result_meta, safe_json_dict,
)

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_PATCH_FILE_RE = re.compile(r"^\*\*\* (Update|Add|Delete) File:\s*(.+)$", flags=re.MULTILINE)
_EXIT_CODE_RE = re.compile(r"Process exited with code (-?\d+)")
_IGNORED_RECORD_TYPES = frozenset({"token_usage_record", "inter_agent_communication_metadata"})
_IGNORED_EVENT_MSG_SUBTYPES = frozenset({
    "token_count", "item_started", "item_completed", "exec_command_begin", "exec_command_end",
    "exec_command_output_delta", "mcp_tool_call_begin", "mcp_tool_call_end",
    "patch_apply_begin", "patch_apply_end", "web_search_begin", "web_search_end",
    "turn_diff", "background_event",
})


@dataclasses.dataclass
class _RolloutTurnTracker:
    turn_num: int = 1
    current_turn_id: TurnId = TurnId("turn-1")
    turn_has_response: bool = False
    cp_counter: int = 0
    raw_turn_to_canonical: dict[str, TurnId] = dataclasses.field(default_factory=dict)
    user_sources: list[tuple[int, str, str]] = dataclasses.field(default_factory=list)
    asst_sources: list[tuple[int, str, str]] = dataclasses.field(default_factory=list)

    def switch_turn(self, new_tid: TurnId) -> None:
        if new_tid != self.current_turn_id:
            self.user_sources.clear()
            self.asst_sources.clear()
            self.current_turn_id = new_tid

    def advance_sequential(self) -> None:
        self.turn_num += 1
        self.switch_turn(TurnId(f"turn-{self.turn_num}"))
        self.turn_has_response = False

    def canonical_for_raw(self, raw_tid: object) -> TurnId:
        s = str(raw_tid or "").strip()
        if not s:
            return self.current_turn_id
        if s not in self.raw_turn_to_canonical:
            if not self.raw_turn_to_canonical and not self.turn_has_response:
                self.raw_turn_to_canonical[s], self.turn_num = TurnId("turn-1"), 1
            else:
                self.turn_num = max(self.turn_num, len(self.raw_turn_to_canonical)) + 1
                self.raw_turn_to_canonical[s] = TurnId(f"turn-{self.turn_num}")
        self.switch_turn(self.raw_turn_to_canonical[s])
        return self.current_turn_id


def decompress_zstd_rollout_bytes(raw_bytes: bytes) -> str:
    """Decompress zstd-compressed Codex rollout bytes into UTF-8 text."""
    try:
        from compression import zstd  # type: ignore[attr-defined]

        return zstd.decompress(raw_bytes).decode("utf-8", errors="replace")
    except ImportError:
        try:
            import zstandard  # type: ignore[import-not-found]

            return zstandard.ZstdDecompressor().decompress(raw_bytes).decode("utf-8", errors="replace")
        except ImportError:
            proc = subprocess.run(["zstd", "-d", "-c"], input=raw_bytes, capture_output=True, check=True)
            return proc.stdout.decode("utf-8", errors="replace")


def iter_codex_rollout_lines(path: Path) -> Iterator[CodexRawRolloutLine]:
    """Stream `.jsonl` or `.jsonl.zst` Codex rollout lines splitting strictly on LF."""
    with path.open("rb") as fh:
        magic = fh.read(4)
        fh.seek(0)
        if path.name.endswith(".zst") or magic == _ZSTD_MAGIC:
            text = decompress_zstd_rollout_bytes(fh.read())
            for i, ln in enumerate(text.split("\n")):
                if ln.strip():
                    yield CodexRawRolloutLine(line_no=RolloutLineNo(i + 1), raw_json=ln)
            return
        for i, raw_line in enumerate(fh):
            ln = raw_line.rstrip(b"\n").decode("utf-8", errors="replace")
            if ln.strip():
                yield CodexRawRolloutLine(line_no=RolloutLineNo(i + 1), raw_json=ln)


def read_codex_rollout_lines(path: Path) -> Tuple[CodexRawRolloutLine, ...]:
    """Read `.jsonl` or `.jsonl.zst` Codex rollout lines splitting strictly on LF."""
    return tuple(iter_codex_rollout_lines(path))


def _extract_content_blocks(content_blocks: Any) -> tuple[str, tuple[MediaAttachment, ...]]:
    if isinstance(content_blocks, str):
        return content_blocks, ()
    texts: list[str] = []
    media: list[MediaAttachment] = []
    if isinstance(content_blocks, list):
        for blk in content_blocks:
            if isinstance(blk, dict):
                if blk.get("text") is not None:
                    texts.append(str(blk["text"]))
                elif blk.get("type") in ("input_image", "image"):
                    media.append(MediaAttachment(mime_type=str(blk.get("mime_type") or "image/png"), uri=str(blk.get("image_url") or blk.get("uri") or "")))
    return "\n".join(texts), tuple(media)


def _stringify_tool_output(raw_out: Any) -> str:
    if isinstance(raw_out, list):
        return "\n".join(str(it["text"]) if isinstance(it, dict) and "text" in it else json.dumps(it, ensure_ascii=False) for it in raw_out)
    return json.dumps(raw_out, ensure_ascii=False) if isinstance(raw_out, dict) else str(raw_out or "")


def _normalize_patch_call_args(raw_args: str) -> str:
    m = _PATCH_FILE_RE.search(raw_args)
    verb = m.group(1) if m else "Update"
    inferred_op = "CREATE" if verb == "Add" else ("DELETE" if verb == "Delete" else "MODIFY")
    inferred_path = normalize_tool_file_path(m.group(2)) if m else "patch_target"
    norm_patch = _PATCH_FILE_RE.sub(lambda hm: f"*** {hm.group(1)} File: {normalize_tool_file_path(hm.group(2))}", raw_args)
    return json.dumps({"edit_op": inferred_op, "path": inferred_path, "unified_diff": norm_patch}, ensure_ascii=False, sort_keys=True)


def _decode_web_search_call(
    pl: Mapping[str, Any], anchor: EventOriginAnchor, line_no: RolloutLineNo,
    events: list[CanonicalEvent], call_index_by_id: dict[ToolCallId, int], ws_result_index: dict[ToolCallId, int],
) -> None:
    cid = ToolCallId(str(pl.get("call_id") or pl.get("id") or f"ws-line-{int(line_no)}"))
    action = pl.get("action") if isinstance(pl.get("action"), dict) else {}
    queries = action.get("queries") if isinstance(action.get("queries"), list) else []
    query = str(action.get("query") or (queries[0] if queries else pl.get("query") or ""))
    domain = action.get("domain") or pl.get("domain")
    docs = tuple(SearchWebDocument(str(d.get("title", "")), str(d.get("url", "")), str(d.get("snippet", ""))) for d in (action.get("results") or pl.get("results") or []) if isinstance(d, dict))
    payload = SearchWebPayload(kind="search_web", query=query, domain_filter=str(domain) if domain else None, documents=docs)
    raw_ext = HostExtensionEnvelope(host=HostKind.CODEX, extension_type="codex_web_search_call", raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    call_index_by_id[cid] = len(events)
    events.append(ToolCallEvent(kind="tool_call", anchor=dataclasses.replace(anchor, host_extensions=(raw_ext,)), call_id=cid, tool_name="web_search", tool_summary="web_search", payload=payload))
    status_str = str(pl.get("status") or "completed").lower()
    if status_str != "in_progress":
        out_text = str(pl.get("output") or (f"Web search: {query}" if query else "Web search completed"))
        ws_result_index[cid] = len(events)
        events.append(ToolResultEvent(kind="tool_result", anchor=anchor, call_id=cid, tool_name="web_search", output_text=out_text, is_error=status_str in ("failed", "error"), payload=payload))


def _decode_event_msg_record(pl: Mapping[str, Any], ts_iso: str, line_no: RolloutLineNo, tracker: _RolloutTurnTracker, events: list[CanonicalEvent]) -> tuple[bool, bool]:
    ev_sub = str(pl.get("type") or "")
    if not ev_sub or ev_sub in _IGNORED_EVENT_MSG_SUBTYPES or ev_sub.endswith("_delta"):
        return False, False
    anchor = EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=tracker.current_turn_id, timestamp_iso=ts_iso, codex_line_no=line_no)
    if ev_sub == "task_started":
        b_kind: Literal["task_boundary", "notify_user"] = "notify_user" if pl.get("boundary_kind") == "notify_user" else "task_boundary"
        events.append(TaskBoundaryEvent(kind="task_boundary", anchor=anchor, boundary_kind=b_kind, task_name=str(pl.get("task_name") or ""), task_status=str(pl.get("task_status") or "task_started"), task_summary=str(pl.get("task_summary") or "")))
        return False, True
    if ev_sub == "task_complete":
        b_kind = "notify_user" if pl.get("boundary_kind") == "notify_user" else "task_boundary"
        t_sum = str(pl["task_summary"]) if pl.get("task_summary") is not None else str(pl.get("last_agent_message") or "Turn complete")
        events.append(TaskBoundaryEvent(kind="task_boundary", anchor=anchor, boundary_kind=b_kind, task_name=str(pl.get("task_name") or ""), task_status=str(pl.get("task_status") or "task_complete"), task_summary=t_sum))
        return True, False
    if ev_sub == "user_message":
        q_text = str(pl.get("message") or pl.get("text") or "")
        if not any(txt == q_text.strip() and src == "response_item" for _, txt, src in tracker.user_sources):
            tracker.user_sources.append((len(events), q_text.strip(), "event_msg"))
            events.append(UserInputEvent(kind="user_input", anchor=anchor, query_text=q_text))
        return False, False
    if ev_sub == "agent_message":
        a_text = str(pl.get("message") or pl.get("text") or "")
        if not any(txt == a_text.strip() and src == "response_item" for _, txt, src in tracker.asst_sources):
            tracker.asst_sources.append((len(events), a_text.strip(), "event_msg"))
            events.append(AssistantTextEvent(kind="assistant_text", anchor=anchor, text=a_text))
        return True, False
    if ev_sub == "agent_reasoning":
        events.append(ThinkingEvent(kind="thinking", anchor=anchor, summary_text=str(pl.get("text") or pl.get("summary") or "")))
        return True, False
    if ev_sub == "system_message":
        ev_type = str(pl.get("event_type") or "system_message")
        pt = pl.get("internal_chat_message_metadata_passthrough")
        exts: list[HostExtensionEnvelope] = []
        if isinstance(pt, dict) and isinstance(pt.get("agy_ext"), dict):
            ae = pt["agy_ext"]
            if ae.get("type") and isinstance(ae.get("payload_hex"), str):
                md_dict = ae.get("metadata") if isinstance(ae.get("metadata"), dict) else {}
                exts.append(HostExtensionEnvelope(host=HostKind.ANTIGRAVITY, extension_type=str(ae["type"]), raw_payload=bytes.fromhex(ae["payload_hex"]), metadata=tuple((str(k), str(v)) for k, v in md_dict.items())))
        events.append(SystemEvent(kind="system_message", anchor=dataclasses.replace(anchor, host_extensions=tuple(exts)), event_type=ev_type, message_text=str(pl.get("message") or "")))
        return (ev_type == "error_message" or ev_type.startswith("opaque_agy_step:")), False

    raw_ext = HostExtensionEnvelope(host=HostKind.CODEX, extension_type="codex_raw_record", raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"), metadata=(("record_type", "event_msg"),))
    events.append(SystemEvent(kind="system_message", anchor=dataclasses.replace(anchor, host_extensions=(raw_ext,)), event_type=f"codex_event_msg:{ev_sub}", message_text=str(pl.get("message") or pl.get("reason") or ev_sub)))
    return ev_sub in ("turn_aborted", "error"), False


def _decode_tool_response_item(
    ptype: str, pl: Mapping[str, Any], pt_dict: Mapping[str, Any], base_anchor: EventOriginAnchor, line_no: RolloutLineNo,
    events: list[CanonicalEvent], call_index_by_id: dict[ToolCallId, int],
    call_has_explicit_exit: set[ToolCallId], ws_result_index: dict[ToolCallId, int],
) -> None:
    if ptype == "web_search_call":
        _decode_web_search_call(pl, base_anchor, line_no, events, call_index_by_id, ws_result_index)
        return
    if ptype in ("function_call", "custom_tool_call", "tool_search_call"):
        cid = ToolCallId(str(pl.get("call_id") or pl.get("id") or f"call-line-{int(line_no)}"))
        tname = str(pl.get("name") or ("tool_search" if ptype == "tool_search_call" else "unknown_tool"))
        raw_args = pl.get("arguments") if pl.get("arguments") not in (None, "") else pl.get("input", "{}")
        if isinstance(raw_args, (dict, list)):
            args_json = json.dumps(raw_args, ensure_ascii=False, sort_keys=True)
        elif isinstance(raw_args, str):
            args_json = _normalize_patch_call_args(raw_args) if (ptype == "custom_tool_call" and tname == "apply_patch" and not raw_args.strip().startswith("{")) else raw_args
        else:
            args_json = "{}"
        if "exit_code" in safe_json_dict(args_json):
            call_has_explicit_exit.add(cid)
        payload = decode_tool_payload(tname, args_json, is_codex_wire=True)
        tsig_hex = pt_dict.get("thinking_signature_hex")
        tsig = bytes.fromhex(tsig_hex) if isinstance(tsig_hex, str) and tsig_hex else b""
        exts = (HostExtensionEnvelope(host=HostKind.CODEX, extension_type="tool_namespace", raw_payload=str(pl["namespace"]).encode("utf-8"), metadata=(("item_type", ptype),)),) if pl.get("namespace") else ()
        call_index_by_id[cid] = len(events)
        events.append(ToolCallEvent(kind="tool_call", anchor=dataclasses.replace(base_anchor, host_extensions=exts), call_id=cid, tool_name=tname, tool_summary=tname, payload=payload, thinking_signature=tsig))
        return

    cid = ToolCallId(str(pl.get("call_id") or pl.get("id") or f"call-out-{int(line_no)}"))
    out_text = _stringify_tool_output(pl.get("output") if "output" in pl else pl.get("tools", ""))
    prior_tname = "unknown_tool"
    prior_payload: CanonicalToolPayload = OpaqueToolPayload(kind="opaque", tool_name=prior_tname, raw_arguments_json="{}")
    rmeta = pt_dict.get("tool_result_meta") if isinstance(pt_dict.get("tool_result_meta"), dict) else {}
    if cid in call_index_by_id and isinstance(events[call_pos := call_index_by_id[cid]], ToolCallEvent):
        prior_ev = events[call_pos]
        assert isinstance(prior_ev, ToolCallEvent)
        prior_tname, prior_payload = prior_ev.tool_name, prior_ev.payload
        if rmeta:
            prior_payload = apply_tool_result_meta(prior_tname, prior_payload, rmeta)
            events[call_pos] = dataclasses.replace(prior_ev, payload=prior_payload)
        elif isinstance(prior_payload, RunCommandPayload) and prior_payload.exit_code is None and cid not in call_has_explicit_exit:
            m_exit = _EXIT_CODE_RE.search(out_text)
            prior_payload = dataclasses.replace(prior_payload, exit_code=int(m_exit.group(1)) if m_exit else 0)
            events[call_pos] = dataclasses.replace(prior_ev, payload=prior_payload)
    is_err = bool(pl.get("is_error", False)) or str(pl.get("status") or "").lower() in ("failed", "error")
    res_exts = (HostExtensionEnvelope(host=HostKind.CODEX, extension_type="synthesized_dangling_output", raw_payload=b"1"),) if pt_dict.get("synthesized_dangling_output") else ()
    res_ev = ToolResultEvent(kind="tool_result", anchor=dataclasses.replace(base_anchor, host_extensions=res_exts), call_id=cid, tool_name=prior_tname, output_text=out_text, is_error=is_err, payload=prior_payload)
    if cid in ws_result_index:
        events[ws_result_index[cid]] = res_ev
    else:
        events.append(res_ev)


def _decode_response_item_record(
    pl: Mapping[str, Any], ts_iso: str, line_no: RolloutLineNo, tracker: _RolloutTurnTracker,
    events: list[CanonicalEvent], call_index_by_id: dict[ToolCallId, int],
    call_has_explicit_exit: set[ToolCallId], ws_result_index: dict[ToolCallId, int],
) -> bool:
    ptype = str(pl.get("type") or "")
    pt_raw = pl.get("internal_chat_message_metadata_passthrough")
    pt_dict = pt_raw if isinstance(pt_raw, dict) else {}
    base_anchor = EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=tracker.current_turn_id, timestamp_iso=ts_iso, codex_line_no=line_no)

    if ptype == "message":
        role = str(pl.get("role") or "user")
        full_text, media_items = _extract_content_blocks(pl.get("content") or [])
        msg_id = str(pl["id"]) if pl.get("id") else None
        if role == "user":
            scopes = tuple(str(s) for s in pt_dict["scope_items"]) if isinstance(pt_dict.get("scope_items"), list) else ()
            u_ev = UserInputEvent(kind="user_input", anchor=dataclasses.replace(base_anchor, client_message_id=msg_id), query_text=full_text, scope_items=scopes, media=media_items)
            for idx_src, (ev_pos, txt, src) in enumerate(tracker.user_sources):
                if src == "event_msg" and txt == full_text.strip():
                    events[ev_pos], tracker.user_sources[idx_src] = u_ev, (ev_pos, txt, "merged")
                    return False
            tracker.user_sources.append((len(events), full_text.strip(), "response_item"))
            events.append(u_ev)
            return False
        if role == "assistant":
            a_ev = AssistantTextEvent(kind="assistant_text", anchor=base_anchor, text=full_text, message_id=msg_id)
            for idx_src, (ev_pos, txt, src) in enumerate(tracker.asst_sources):
                if src == "event_msg" and txt == full_text.strip():
                    events[ev_pos], tracker.asst_sources[idx_src] = a_ev, (ev_pos, txt, "merged")
                    return True
            tracker.asst_sources.append((len(events), full_text.strip(), "response_item"))
            events.append(a_ev)
            return True
        ext = HostExtensionEnvelope(host=HostKind.CODEX, extension_type=f"message_{role}", raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        events.append(SystemEvent(kind="system_message", anchor=dataclasses.replace(base_anchor, host_extensions=(ext,)), event_type=f"codex_{role}_message", message_text=full_text))
        return False

    if ptype == "agent_message":
        full_text, _ = _extract_content_blocks(pl.get("content") or [])
        ext = HostExtensionEnvelope(host=HostKind.CODEX, extension_type="codex_agent_message", raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        events.append(AssistantTextEvent(kind="assistant_text", anchor=dataclasses.replace(base_anchor, host_extensions=(ext,)), text=full_text or str(pl.get("text") or pl.get("message") or ""), message_id=str(pl["id"]) if pl.get("id") else None))
        return True

    if ptype in ("reasoning", "magpie_reasoning"):
        s_parts = [str(it["text"]) if isinstance(it, dict) and "text" in it else str(it) for it in (pl.get("summary") or []) if isinstance(it, (dict, str))]
        enc = pl.get("encrypted_content")
        ext = (
            HostExtensionEnvelope(host=HostKind.CODEX, extension_type="encrypted_content", raw_payload=str(enc).encode("utf-8"), metadata=(("reasoning_type", ptype),))
            if enc else HostExtensionEnvelope(host=HostKind.CODEX, extension_type="reasoning_meta", raw_payload=b"", metadata=(("reasoning_type", ptype),))
        )
        events.append(ThinkingEvent(kind="thinking", anchor=dataclasses.replace(base_anchor, host_extensions=(ext,)), summary_text="\n".join(s_parts), raw_thinking=pl["content"] if isinstance(pl.get("content"), str) else None))
        return True

    if ptype in ("web_search_call", "function_call", "custom_tool_call", "tool_search_call", "function_call_output", "custom_tool_call_output", "tool_search_output", "web_search_call_output"):
        _decode_tool_response_item(ptype, pl, pt_dict, base_anchor, line_no, events, call_index_by_id, call_has_explicit_exit, ws_result_index)
        return True

    raw_ext = HostExtensionEnvelope(host=HostKind.CODEX, extension_type="codex_raw_record", raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"), metadata=(("record_type", "response_item"),))
    events.append(SystemEvent(kind="system_message", anchor=dataclasses.replace(base_anchor, host_extensions=(raw_ext,)), event_type=f"codex_response_item:{ptype or 'unknown'}", message_text=ptype or "response_item"))
    return False


def decode_codex_rollout(
    workspace_path: WorkspacePath,
    workspace_uri: WorkspaceRootUri,
    raw_lines: Sequence[CodexRawRolloutLine],
    *,
    explicit_title_override: str | None = None,
) -> TrajectoryLedger:
    """Parse Codex `rollout-*.jsonl` lines into an indexed `TrajectoryLedger`."""
    events: list[CanonicalEvent] = []
    call_index_by_id: dict[ToolCallId, int] = {}
    call_has_explicit_exit: set[ToolCallId] = set()
    ws_result_index: dict[ToolCallId, int] = {}
    tracker = _RolloutTurnTracker()

    for row in raw_lines:
        if not (raw_str := row.raw_json.strip()):
            continue
        obj = json.loads(raw_str)
        rtype = str(obj.get("type") or "")
        pl = obj.get("payload")
        if rtype in _IGNORED_RECORD_TYPES or not isinstance(pl, dict):
            continue
        ts_iso = str(obj.get("timestamp") or "2026-01-01T00:00:00Z")

        if rtype in ("session_meta", "world_state"):
            ext = HostExtensionEnvelope(host=HostKind.CODEX, extension_type=rtype, raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            msg = f"codex_session:{pl.get('id') or pl.get('session_id') or ''}" if rtype == "session_meta" else "codex_world_state"
            anchor = EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=tracker.current_turn_id, timestamp_iso=ts_iso, codex_line_no=row.line_no, host_extensions=(ext,))
            events.append(SystemEvent(kind="system_message", anchor=anchor, event_type=rtype, message_text=msg))
        elif rtype == "turn_context":
            if pl.get("turn_id"):
                tracker.canonical_for_raw(pl["turn_id"])
            tracker.cp_counter += 1
            ext = HostExtensionEnvelope(host=HostKind.CODEX, extension_type="turn_context", raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            anchor = EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=tracker.current_turn_id, timestamp_iso=ts_iso, codex_line_no=row.line_no, host_extensions=(ext,))
            sum_str = str(pl["summary"]) if pl.get("summary") is not None else str(pl.get("model") or "turn_context")
            conv_title = pl.get("conversation_title") or pl.get("title")
            cp_idx = int(pl["checkpoint_index"]) if pl.get("checkpoint_index") is not None else tracker.cp_counter
            events.append(CheckpointEvent(kind="checkpoint", anchor=anchor, checkpoint_index=cp_idx, user_intent=str(pl.get("user_intent") or ""), session_summary=sum_str, code_change_summary=str(pl.get("code_change_summary") or ""), conversation_title=str(conv_title) if conv_title else None))
        elif rtype == "event_msg":
            ev_sub, ev_turn = str(pl.get("type") or ""), pl.get("turn_id")
            if ev_sub in ("task_started", "user_message"):
                if ev_turn:
                    tracker.canonical_for_raw(ev_turn)
                elif tracker.turn_has_response:
                    tracker.advance_sequential()
                if ev_sub == "task_started":
                    tracker.turn_has_response = False
            elif ev_turn and ev_sub not in _IGNORED_EVENT_MSG_SUBTYPES and not ev_sub.endswith("_delta"):
                tracker.canonical_for_raw(ev_turn)
            resp_flag, reset_flag = _decode_event_msg_record(pl, ts_iso, row.line_no, tracker, events)
            if reset_flag:
                tracker.turn_has_response = False
            elif resp_flag:
                tracker.turn_has_response = True
        elif rtype == "response_item":
            pt = pl.get("internal_chat_message_metadata_passthrough")
            if isinstance(pt, dict) and pt.get("turn_id"):
                tracker.canonical_for_raw(pt["turn_id"])
            elif pl.get("type") == "message" and str(pl.get("role") or "user") == "user" and tracker.turn_has_response:
                tracker.advance_sequential()
            if _decode_response_item_record(pl, ts_iso, row.line_no, tracker, events, call_index_by_id, call_has_explicit_exit, ws_result_index):
                tracker.turn_has_response = True
        else:
            raw_ext = HostExtensionEnvelope(host=HostKind.CODEX, extension_type="codex_raw_record", raw_payload=json.dumps(pl, ensure_ascii=False, sort_keys=True).encode("utf-8"), metadata=(("record_type", rtype or "unknown"),))
            anchor = EventOriginAnchor(origin_host=HostKind.CODEX, turn_id=tracker.current_turn_id, timestamp_iso=ts_iso, codex_line_no=row.line_no, host_extensions=(raw_ext,))
            events.append(SystemEvent(kind="system_message", anchor=anchor, event_type=f"codex_record:{rtype}", message_text=str(pl.get("message") or rtype)))

    return TrajectoryLedger.from_events(workspace_path=workspace_path, workspace_uri=workspace_uri, events=events, explicit_title_override=explicit_title_override)


def _encode_codex_system_event(ev: SystemEvent, ts: str, append_fn: Callable[[str, str, Mapping[str, Any]], None]) -> None:
    for ext in ev.anchor.host_extensions:
        if ext.extension_type == "codex_raw_record" and ext.raw_payload:
            raw_d = dict(safe_json_dict(ext.raw_payload.decode("utf-8", errors="replace")))
            if "turn_id" in raw_d:
                raw_d["turn_id"] = str(ev.anchor.turn_id)
            if "root_turn_id" in raw_d:
                raw_d["root_turn_id"] = str(ev.anchor.turn_id)
            append_fn(ts, dict(ext.metadata).get("record_type", "event_msg"), raw_d)
            return
    if ev.event_type.startswith("codex_") and ev.event_type.endswith("_message"):
        role = ev.event_type.removeprefix("codex_").removesuffix("_message")
        for ext in ev.anchor.host_extensions:
            if ext.extension_type == f"message_{role}" and ext.raw_payload and (raw_pl := dict(safe_json_dict(ext.raw_payload.decode("utf-8", errors="replace")))):
                if isinstance(raw_pl.get("internal_chat_message_metadata_passthrough"), dict):
                    raw_pl["internal_chat_message_metadata_passthrough"] = {**raw_pl["internal_chat_message_metadata_passthrough"], "turn_id": str(ev.anchor.turn_id)}
                append_fn(ts, "response_item", raw_pl)
                return
        append_fn(ts, "response_item", {"type": "message", "role": role, "content": [{"type": "input_text", "text": ev.message_text}], "internal_chat_message_metadata_passthrough": {"turn_id": str(ev.anchor.turn_id)}})
        return
    pl_out: dict[str, Any] = {"type": "system_message", "turn_id": str(ev.anchor.turn_id), "event_type": ev.event_type, "message": ev.message_text}
    for ext in ev.anchor.host_extensions:
        if ext.extension_type in ("agy_error_message_step", "agy_opaque_step"):
            pl_out["internal_chat_message_metadata_passthrough"] = {
                "turn_id": str(ev.anchor.turn_id),
                "agy_ext": {"type": ext.extension_type, "payload_hex": ext.raw_payload.hex(), "metadata": dict(ext.metadata)},
            }
            break
    append_fn(ts, "event_msg", pl_out)


def _encode_single_codex_event(
    ev: CanonicalEvent, ts: str, next_line_hint: int, ws_native_calls: set[ToolCallId],
    append_fn: Callable[[str, str, Mapping[str, Any]], None],
) -> bool:
    if isinstance(ev, UserInputEvent):
        content_list: list[dict[str, Any]] = [{"type": "input_text", "text": ev.query_text}]
        content_list.extend({"type": "input_image", "mime_type": m.mime_type, "image_url": m.uri} for m in ev.media)
        u_pt: dict[str, Any] = {"turn_id": str(ev.anchor.turn_id)}
        if ev.scope_items:
            u_pt["scope_items"] = list(ev.scope_items)
        append_fn(ts, "response_item", {"type": "message", "id": ev.anchor.client_message_id or f"msg_user_{next_line_hint}", "role": "user", "content": content_list, "internal_chat_message_metadata_passthrough": u_pt})
        return False
    if isinstance(ev, ThinkingEvent):
        r_type, enc_val = "reasoning", None
        for ext in ev.anchor.host_extensions:
            for mk, mv in ext.metadata:
                if mk == "reasoning_type" and mv in ("reasoning", "magpie_reasoning"):
                    r_type = mv
            if ext.extension_type == "encrypted_content" and ext.raw_payload:
                enc_val = ext.raw_payload.decode("utf-8", errors="replace")
        pl_d: dict[str, Any] = {"type": r_type, "summary": [{"type": "summary_text", "text": ev.summary_text}], "content": ev.raw_thinking, "internal_chat_message_metadata_passthrough": {"turn_id": str(ev.anchor.turn_id)}}
        if enc_val is not None:
            pl_d["encrypted_content"] = enc_val
        append_fn(ts, "response_item", pl_d)
        return False
    if isinstance(ev, AssistantTextEvent):
        raw_am = next((dict(safe_json_dict(ext.raw_payload.decode("utf-8", errors="replace"))) for ext in ev.anchor.host_extensions if ext.extension_type == "codex_agent_message" and ext.raw_payload), None)
        if raw_am and isinstance(raw_am.get("internal_chat_message_metadata_passthrough"), dict):
            raw_am["internal_chat_message_metadata_passthrough"] = {**raw_am["internal_chat_message_metadata_passthrough"], "turn_id": str(ev.anchor.turn_id)}
        append_fn(ts, "response_item", raw_am or {"type": "message", "status": "completed", "id": ev.message_id or f"msg_asst_{next_line_hint}", "role": "assistant", "content": [{"type": "output_text", "text": ev.text}], "internal_chat_message_metadata_passthrough": {"turn_id": str(ev.anchor.turn_id)}})
        return False
    if isinstance(ev, ToolCallEvent):
        if raw_ws := next((safe_json_dict(ext.raw_payload.decode("utf-8", errors="replace")) for ext in ev.anchor.host_extensions if ext.extension_type == "codex_web_search_call" and ext.raw_payload), None):
            ws_native_calls.add(ev.call_id)
            append_fn(ts, "response_item", raw_ws)
            return False
        c_kind, c_name, args_json, custom_input = encode_tool_for_codex(ev.tool_name, ev.payload)
        call_pt: dict[str, Any] = {"turn_id": str(ev.anchor.turn_id)}
        if ev.thinking_signature:
            call_pt["thinking_signature_hex"] = ev.thinking_signature.hex()
        call_pl: dict[str, Any] = {"type": c_kind, "status": "completed", "call_id": str(ev.call_id), "name": c_name, "arguments": args_json, "internal_chat_message_metadata_passthrough": call_pt}
        if custom_input is not None:
            call_pl["input"] = custom_input
        for ext in ev.anchor.host_extensions:
            if ext.extension_type == "tool_namespace" and ext.raw_payload:
                call_pl["namespace"] = ext.raw_payload.decode("utf-8", errors="replace")
        append_fn(ts, "response_item", call_pl)
        return False
    if isinstance(ev, ToolResultEvent):
        if ev.call_id not in ws_native_calls:
            spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(ev.payload.kind)
            out_kind = "custom_tool_call_output" if (spec and spec.codex_call_kind == "custom_tool_call") else ("tool_search_output" if ev.tool_name == "tool_search" else "function_call_output")
            res_pt: dict[str, Any] = {"turn_id": str(ev.anchor.turn_id)}
            if rmeta := extract_tool_result_meta(ev.payload):
                res_pt["tool_result_meta"] = rmeta
            if any(ext.extension_type == "synthesized_dangling_output" for ext in ev.anchor.host_extensions):
                res_pt["synthesized_dangling_output"] = True
            append_fn(ts, "response_item", {"type": out_kind, "call_id": str(ev.call_id), "output": ev.output_text, "is_error": ev.is_error, "status": "failed" if ev.is_error else "completed", "internal_chat_message_metadata_passthrough": res_pt})
        return False
    if isinstance(ev, CheckpointEvent):
        cp_pl: dict[str, Any] = {"turn_id": str(ev.anchor.turn_id), "root_turn_id": str(ev.anchor.turn_id), "checkpoint_index": ev.checkpoint_index, "user_intent": ev.user_intent, "summary": ev.session_summary, "code_change_summary": ev.code_change_summary}
        if ev.conversation_title:
            cp_pl["conversation_title"] = ev.conversation_title
        for ext in ev.anchor.host_extensions:
            if ext.extension_type == "turn_context" and ext.raw_payload:
                orig_tc = safe_json_dict(ext.raw_payload.decode("utf-8", errors="replace"))
                orig_tc.update(cp_pl)
                cp_pl = orig_tc
        append_fn(ts, "turn_context", cp_pl)
        return False
    if isinstance(ev, TaskBoundaryEvent):
        append_fn(ts, "event_msg", {"type": ev.task_status if ev.task_status in ("task_started", "task_complete") else "task_complete", "turn_id": str(ev.anchor.turn_id), "root_turn_id": str(ev.anchor.turn_id), "task_name": ev.task_name, "task_summary": ev.task_summary, "last_agent_message": ev.task_summary, "boundary_kind": ev.boundary_kind, "task_status": ev.task_status})
        return ev.task_status == "task_complete"
    if isinstance(ev, SystemEvent):
        _encode_codex_system_event(ev, ts, append_fn)
    return False


def encode_codex_rollout(
    events: Sequence[CanonicalEvent],
    starting_line_no: RolloutLineNo = RolloutLineNo(1),
    session_id: str | None = None,
    cwd: str | None = None,
) -> Tuple[CodexRawRolloutLine, ...]:
    """Serialize a slice of `CanonicalEvent` nodes into Codex `rollout-*.jsonl` lines."""
    lines: list[CodexRawRolloutLine] = []
    next_line = int(starting_line_no)

    def _append_record(ts: str, rtype: str, payload: Mapping[str, Any]) -> None:
        nonlocal next_line
        record = {"timestamp": ts or "2026-01-01T00:00:00Z", "ordinal": next_line - 1, "type": rtype, "payload": payload}
        lines.append(CodexRawRolloutLine(line_no=RolloutLineNo(next_line), raw_json=json.dumps(record, ensure_ascii=False, separators=(",", ":"))))
        next_line += 1

    if events and int(starting_line_no) == 1 and not any(isinstance(e, SystemEvent) and e.event_type == "session_meta" for e in events):
        first_ts = events[0].anchor.timestamp_iso or "2026-01-01T00:00:00Z"
        eff_sid = session_id or "codex-synced-session"
        meta_pl: dict[str, Any] = {"id": eff_sid, "session_id": eff_sid, "timestamp": first_ts, "originator": "codex_agy_sync", "source": "desktop", "model_provider": "openai"}
        if cwd is not None:
            meta_pl["cwd"] = cwd
        _append_record(first_ts, "session_meta", meta_pl)

    resolved_ids = {e.call_id for e in events if isinstance(e, ToolResultEvent)}
    dangling_turn_calls: list[ToolCallEvent] = []
    synth_ext = (HostExtensionEnvelope(host=HostKind.CODEX, extension_type="synthesized_dangling_output", raw_payload=b"1"),)
    active_turn: TurnId | None = None
    active_turn_closed = True
    turn_events_buf: list[CanonicalEvent] = []
    last_ts = "2026-01-01T00:00:00Z"
    ws_native_calls: set[ToolCallId] = set()

    for ev in events:
        ts = last_ts = ev.anchor.timestamp_iso or last_ts
        if isinstance(ev, SystemEvent) and ev.event_type in ("session_meta", "world_state"):
            raw_dict = next((safe_json_dict(ext.raw_payload.decode("utf-8", errors="replace")) for ext in ev.anchor.host_extensions if ext.extension_type == ev.event_type and ext.raw_payload), {})
            _append_record(ts, ev.event_type, raw_dict or {"message": ev.message_text, "event_type": ev.event_type})
            continue
        if ev.anchor.turn_id != active_turn:
            if active_turn is not None:
                for dc in dangling_turn_calls:
                    if dc.call_id not in ws_native_calls:
                        s_res = ToolResultEvent(kind="tool_result", anchor=EventOriginAnchor(origin_host=dc.anchor.origin_host, turn_id=active_turn, timestamp_iso=ts, host_extensions=synth_ext), call_id=dc.call_id, tool_name=dc.tool_name, output_text=INTERRUPTED_TOOL_OUTPUT, is_error=True, payload=dc.payload)
                        _encode_single_codex_event(s_res, ts, next_line, ws_native_calls, _append_record)
                dangling_turn_calls.clear()
                if not active_turn_closed:
                    _append_record(ts, "event_msg", {"type": "task_complete", "turn_id": str(active_turn), "root_turn_id": str(active_turn), "last_agent_message": "Turn complete"})
            active_turn, active_turn_closed = ev.anchor.turn_id, False
            turn_events_buf.clear()
            if not (isinstance(ev, TaskBoundaryEvent) and ev.task_status == "task_started"):
                _append_record(ts, "event_msg", {"type": "task_started", "turn_id": str(active_turn), "root_turn_id": str(active_turn)})
        turn_events_buf.append(ev)
        if _encode_single_codex_event(ev, ts, next_line, ws_native_calls, _append_record):
            active_turn_closed = True
        if isinstance(ev, ToolCallEvent) and ev.call_id not in resolved_ids and ev.call_id not in ws_native_calls:
            dangling_turn_calls.append(ev)

    if active_turn is not None and not active_turn_closed and not dangling_turn_calls:
        from ..ledger import _is_nonterminal_work_or_abort_event, _is_terminal_completion_event

        lw = max((i for i, e in enumerate(turn_events_buf) if _is_nonterminal_work_or_abort_event(e)), default=-1)
        lc = max((i for i, e in enumerate(turn_events_buf) if _is_terminal_completion_event(e)), default=-1)
        if lw <= lc and not any(ext.extension_type == "agy_step_status" for e in turn_events_buf for ext in e.anchor.host_extensions):
            _append_record(last_ts, "event_msg", {"type": "task_complete", "turn_id": str(active_turn), "root_turn_id": str(active_turn), "last_agent_message": "Turn complete"})
    return tuple(lines)


decode_codex_rollout_to_ledger = decode_codex_rollout
encode_events_to_codex_rollout = encode_codex_rollout


def encode_ledger_to_codex_rollout(
    ledger_or_events: TrajectoryLedger | Sequence[CanonicalEvent],
    starting_line_no: RolloutLineNo = RolloutLineNo(1),
    session_id: str | None = None,
    cwd: str | None = None,
) -> Tuple[CodexRawRolloutLine, ...]:
    """Encode a `TrajectoryLedger` or event sequence into Codex `rollout-*.jsonl` lines."""
    if isinstance(ledger_or_events, TrajectoryLedger):
        return encode_codex_rollout(ledger_or_events.events, starting_line_no, session_id=session_id, cwd=cwd if cwd is not None else str(ledger_or_events.workspace_path))
    return encode_codex_rollout(ledger_or_events, starting_line_no, session_id=session_id, cwd=cwd)


from .rpc_thread import decode_codex_rpc_thread_to_ledger, splice_rollout_completed_prefix  # noqa: E402,F401
