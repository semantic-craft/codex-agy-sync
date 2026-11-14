"""Table-driven `TOOL_CODEC_TABLE` registry, call-input JSON serializers, and Codex tool adapters."""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Literal, Mapping, Tuple

from ..payloads import (
    TOOL_ARG_SERIALIZERS,
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
    ReadFilePayload,
    ReadResourcePayload,
    RunCommandPayload,
    SchedulePayload,
    SearchWebPayload,
    SendMessagePayload,
    clean_dict,
    dump_canonical_json,
    safe_json_dict,
    serialize_tool_payload_arguments_json,
)
from ..workspace import normalize_tool_file_path
from .schema_tags import ToolCodecSpec
from .tool_payloads import (
    decode_any_ask_question,
    decode_any_define_subagent,
    decode_any_envelope,
    decode_any_file_edit,
    decode_any_finish,
    decode_any_generate_image,
    decode_any_invoke_subagent,
    decode_any_list_resources,
    decode_any_manage_subagents,
    decode_any_manage_task,
    decode_any_moma_search,
    decode_any_read_file,
    decode_any_read_resource,
    decode_any_run_command,
    decode_any_schedule,
    decode_any_search_web,
    decode_any_send_message,
    encode_any_ask_question,
    encode_any_define_subagent,
    encode_any_envelope,
    encode_any_file_edit,
    encode_any_finish,
    encode_any_generate_image,
    encode_any_invoke_subagent,
    encode_any_list_resources,
    encode_any_manage_subagents,
    encode_any_manage_task,
    encode_any_moma_search,
    encode_any_read_file,
    encode_any_read_resource,
    encode_any_run_command,
    encode_any_schedule,
    encode_any_search_web,
    encode_any_send_message,
    first_present,
)


def _synthesize_codex_patch_input(payload: FileEditPayload) -> str:
    if "*** Begin Patch" in payload.unified_diff:
        return re.sub(
            r"^(\*\*\* (?:Add|Update|Delete) File:\s*)(.+)$",
            lambda m: m.group(1) + normalize_tool_file_path(m.group(2)),
            payload.unified_diff,
            flags=re.MULTILINE,
        )
    norm_path = normalize_tool_file_path(payload.path) or "patch_target"
    verb = "Add" if payload.edit_op in ("CREATE", "ADD") else ("Delete" if payload.edit_op == "DELETE" else "Update")
    body = payload.unified_diff.rstrip("\r\n")
    return f"*** Begin Patch\n*** {verb} File: {norm_path}\n{body}\n*** End Patch" if body else f"*** Begin Patch\n*** {verb} File: {norm_path}\n*** End Patch"


def _rmeta_read_file(p: ReadFilePayload) -> Mapping[str, Any]:
    return clean_dict(file_num_lines=p.file_num_lines, file_num_bytes=p.file_num_bytes, content_excerpt=p.content_excerpt)


def _rmeta_file_edit(p: FileEditPayload) -> Mapping[str, Any]:
    return {"diff_stats": list(p.diff_stats)} if p.diff_stats != (0, 0) else {}


def _rmeta_run_command(p: RunCommandPayload) -> Mapping[str, Any]:
    out: dict[str, Any] = {"exit_code_known": p.exit_code is not None}
    if p.exit_code is not None:
        out["exit_code"] = p.exit_code
    if p.timed_out:
        out["timed_out"] = True
    return out


def _rmeta_search_web(p: SearchWebPayload) -> Mapping[str, Any]:
    return clean_dict(documents=[{"title": d.title, "url": d.url, "snippet": d.snippet} for d in p.documents] if p.documents else None, summary=p.summary)


def _rmeta_invoke_subagent(p: InvokeSubagentPayload) -> Mapping[str, Any]:
    return clean_dict(spawned_conversation_id=p.spawned_conversation_id)


def _rmeta_manage_subagents(p: ManageSubagentsPayload) -> Mapping[str, Any]:
    return {"target_conversation_ids": list(p.target_conversation_ids)} if (p.action == "list" and p.target_conversation_ids) else {}


def _rmeta_ask_question(p: AskQuestionPayload) -> Mapping[str, Any]:
    return {"user_answers_by_index": [list(q.user_answers) for q in p.questions]} if any(q.user_answers for q in p.questions) else {}


def _rmeta_list_resources(p: ListResourcesPayload) -> Mapping[str, Any]:
    return {"resource_uris": list(p.resource_uris)} if p.resource_uris else {}


def _rmeta_read_resource(p: ReadResourcePayload) -> Mapping[str, Any]:
    return {"contents_text": list(p.contents_text)} if p.contents_text else {}


def _rmeta_generate_image(p: GenerateImagePayload) -> Mapping[str, Any]:
    return clean_dict(image_uri=p.image_uri)


def _rmeta_moma_search(p: MomaSearchPayload) -> Mapping[str, Any]:
    return {"result_count": p.result_count} if p.result_count else {}


def _rmeta_finish(p: FinishPayload) -> Mapping[str, Any]:
    return {"has_result_schema": True} if p.has_result_schema else {}


def _make_codex_enc(call_kind: str, tool_name: str, args_fn: Any) -> Any:
    return lambda payload: (call_kind, tool_name, dump_canonical_json(args_fn(payload)), None)


def _make_codex_dec(decode_any_fn: Any) -> Any:
    return lambda _tname, args, raw_json: decode_any_fn(args, raw_json, b"")


def _encode_codex_file_edit(p: FileEditPayload) -> tuple[str, str, str, str | None]:
    return ("custom_tool_call", "apply_patch", dump_canonical_json(TOOL_ARG_SERIALIZERS["file_edit"](p)), _synthesize_codex_patch_input(p))


def _encode_codex_run_command(p: RunCommandPayload) -> tuple[str, str, str, str | None]:
    d = {**TOOL_ARG_SERIALIZERS["run_command"](p), "cmd": p.command_line, "workdir": normalize_tool_file_path(p.cwd)}
    return ("function_call", "exec_command", dump_canonical_json(d), None)


def _encode_codex_manage_subagents(p: ManageSubagentsPayload) -> tuple[str, str, str, str | None]:
    return ("function_call", "list_agents" if p.action == "list" else "interrupt_agent", dump_canonical_json(TOOL_ARG_SERIALIZERS["manage_subagents"](p)), None)


def _decode_codex_manage_subagents(tool_name: str, args: Mapping[str, Any], raw_json: str) -> ManageSubagentsPayload:
    enriched = dict(args)
    if first_present(enriched, "Action", "action") is None:
        enriched["action"] = "kill" if tool_name in ("interrupt_agent", "close_agent") else ("kill_all" if tool_name == "kill_all_agents" else "list")
    return decode_any_manage_subagents(enriched, raw_json, b"")


def _encode_codex_manage_task(p: ManageTaskPayload) -> tuple[str, str, str, str | None]:
    cname = {"status": "wait", "send_input": "send_input", "kill": "kill_task", "list": "list_tasks"}[p.action]
    return ("function_call", cname, dump_canonical_json(TOOL_ARG_SERIALIZERS["manage_task"](p)), None)


def _decode_codex_manage_task(tool_name: str, args: Mapping[str, Any], raw_json: str) -> ManageTaskPayload:
    enriched = dict(args)
    if first_present(enriched, "Action", "action") is None:
        enriched["action"] = (
            "send_input" if tool_name in ("send_input", "write_stdin")
            else "kill" if tool_name == "kill_task"
            else "list" if tool_name == "list_tasks"
            else "status"
        )
    return decode_any_manage_task(enriched, raw_json, b"")


def _encode_codex_finish(p: FinishPayload) -> tuple[str, str, str, str | None]:
    return ("function_call", "finish", p.arguments_json or "{}", None)


def _spec(
    kind: str, agy_name: str, msg_name: str, codex_kind: Literal["function_call", "custom_tool_call"],
    codex_name: str, fallbacks: Tuple[str, ...], enc_any: Any, dec_any: Any,
    rmeta_fn: Any = None, enc_codex: Any = None, dec_codex: Any = None,
) -> ToolCodecSpec:
    args_fn = TOOL_ARG_SERIALIZERS[kind]
    return ToolCodecSpec(
        kind, agy_name, f"type.googleapis.com/exa.cortex.core.tools.{msg_name}",
        codex_kind, codex_name, fallbacks, enc_any, dec_any, args_fn,
        enc_codex or _make_codex_enc(codex_kind, codex_name, args_fn),
        dec_codex or _make_codex_dec(dec_any),
        rmeta_fn,
    )


TOOL_CODEC_TABLE: Tuple[ToolCodecSpec, ...] = (
    _spec("read_file", "read_file", "ReadFile", "function_call", "view_file", ("exec_command",), encode_any_read_file, decode_any_read_file, _rmeta_read_file),
    _spec("file_edit", "edit_file", "FileEdit", "custom_tool_call", "apply_patch", ("write_file",), encode_any_file_edit, decode_any_file_edit, _rmeta_file_edit, _encode_codex_file_edit),
    _spec("run_command", "run_command", "RunCommand", "function_call", "exec_command", ("custom_tool_call:exec", "shell", "exec"), encode_any_run_command, decode_any_run_command, _rmeta_run_command, _encode_codex_run_command),
    _spec("search_web", "search_web", "SearchWeb", "function_call", "web_search", (), encode_any_search_web, decode_any_search_web, _rmeta_search_web),
    _spec("invoke_subagent", "invoke_subagent", "InvokeSubagent", "function_call", "spawn_agent", (), encode_any_invoke_subagent, decode_any_invoke_subagent, _rmeta_invoke_subagent),
    _spec("define_subagent", "define_subagent", "DefineSubagent", "function_call", "define_subagent", (), encode_any_define_subagent, decode_any_define_subagent),
    _spec("manage_subagents", "manage_subagents", "ManageSubagents", "function_call", "list_agents", ("wait_agent", "interrupt_agent", "close_agent", "kill_all_agents"), encode_any_manage_subagents, decode_any_manage_subagents, _rmeta_manage_subagents, _encode_codex_manage_subagents, _decode_codex_manage_subagents),
    _spec("manage_task", "manage_task", "ManageTask", "function_call", "wait", ("send_input", "write_stdin", "kill_task", "list_tasks"), encode_any_manage_task, decode_any_manage_task, None, _encode_codex_manage_task, _decode_codex_manage_task),
    _spec("send_message", "send_message", "SendMessage", "function_call", "send_message", (), encode_any_send_message, decode_any_send_message),
    _spec("schedule", "schedule", "Schedule", "function_call", "schedule", (), encode_any_schedule, decode_any_schedule),
    _spec("ask_question", "ask_question", "AskQuestion", "function_call", "ask_question", (), encode_any_ask_question, decode_any_ask_question, _rmeta_ask_question),
    _spec("list_resources", "list_resources", "ListResources", "function_call", "list_mcp_resources", (), encode_any_list_resources, decode_any_list_resources, _rmeta_list_resources),
    _spec("read_resource", "read_resource", "ReadResource", "function_call", "read_mcp_resource", (), encode_any_read_resource, decode_any_read_resource, _rmeta_read_resource),
    _spec("generate_image", "generate_image", "GenerateImage", "function_call", "generate_image", (), encode_any_generate_image, decode_any_generate_image, _rmeta_generate_image),
    _spec("moma_search", "moma_search", "MomaSearch", "function_call", "moma_search", (), encode_any_moma_search, decode_any_moma_search, _rmeta_moma_search),
    _spec("finish", "finish", "Finish", "function_call", "finish", (), encode_any_finish, decode_any_finish, _rmeta_finish, _encode_codex_finish),
)

TOOL_SPEC_BY_PAYLOAD_KIND: Mapping[str, ToolCodecSpec] = {s.payload_kind: s for s in TOOL_CODEC_TABLE}
TOOL_SPEC_BY_TYPE_URL: Mapping[str, ToolCodecSpec] = {s.agy_any_type_url: s for s in TOOL_CODEC_TABLE}
TOOL_NAME_LOOKUP: dict[str, ToolCodecSpec] = {}
for _s in TOOL_CODEC_TABLE:
    for _alias in (_s.agy_tool_name, _s.codex_tool_name, _s.payload_kind):
        TOOL_NAME_LOOKUP.setdefault(_alias, _s)
for _s in TOOL_CODEC_TABLE:
    for _fb in _s.codex_fallback_tool_names:
        if ":" not in _fb:
            TOOL_NAME_LOOKUP.setdefault(_fb, _s)


def serialize_agy_chat_tool_arguments(payload: CanonicalToolPayload, *, has_result_step: bool = True) -> str:
    """Serialize call-input arguments for Antigravity `ChatToolCall.arguments_json`."""
    if isinstance(payload, OpaqueToolPayload):
        return payload.raw_arguments_json or "{}"
    if isinstance(payload, FinishPayload):
        return payload.arguments_json or "{}"
    if isinstance(payload, FileEditPayload):
        canon_op = "CREATE" if payload.edit_op == "ADD" else payload.edit_op
        data = clean_dict(
            EditOp=canon_op,
            Path=normalize_tool_file_path(payload.path),
            IsArtifact=True if payload.is_artifact else None,
            ArtifactSummary=payload.artifact_summary,
            unified_diff=payload.unified_diff if (not has_result_step and payload.unified_diff) else None,
        )
        return dump_canonical_json(data)
    data = dict(safe_json_dict(serialize_tool_payload_arguments_json(payload)))
    if isinstance(payload, RunCommandPayload):
        data["CommandLine"], data["Cwd"] = payload.command_line, normalize_tool_file_path(payload.cwd)
    elif isinstance(payload, ReadFilePayload):
        data["Path"] = normalize_tool_file_path(payload.path)
        if payload.start_line is not None:
            data["StartLine"] = payload.start_line
        if payload.end_line is not None:
            data["EndLine"] = payload.end_line
    return dump_canonical_json(data)


def extract_tool_result_meta(payload: CanonicalToolPayload) -> dict[str, Any]:
    """Extract result-only fields from `CanonicalToolPayload` via `TOOL_CODEC_TABLE`."""
    if isinstance(payload, OpaqueToolPayload):
        return clean_dict(type_url=payload.type_url, raw_any_hex=payload.raw_any_bytes.hex() if payload.raw_any_bytes is not None else None)
    spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(payload.kind)
    return dict(spec.extract_result_meta_fn(payload)) if (spec and spec.extract_result_meta_fn) else {}


def apply_tool_result_meta(tool_name: str, prior_payload: CanonicalToolPayload, result_meta: Mapping[str, Any]) -> CanonicalToolPayload:
    """Hydrate result-only fields onto `prior_payload` without mutating call input arguments."""
    if not result_meta:
        return prior_payload
    if isinstance(prior_payload, OpaqueToolPayload):
        raw_hex = result_meta.get("raw_any_hex")
        return dataclasses.replace(
            prior_payload,
            type_url=str(result_meta["type_url"]) if result_meta.get("type_url") else prior_payload.type_url,
            raw_any_bytes=bytes.fromhex(raw_hex) if isinstance(raw_hex, str) else prior_payload.raw_any_bytes,
        )
    merged = {**safe_json_dict(serialize_tool_payload_arguments_json(prior_payload)), **dict(result_meta)}
    if isinstance(prior_payload, RunCommandPayload) and result_meta.get("exit_code_known") is False:
        merged.pop("exit_code", None)
    spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(prior_payload.kind)
    if spec and spec.decode_codex_fn:
        raw_arg_str = prior_payload.arguments_json if isinstance(prior_payload, FinishPayload) else dump_canonical_json(merged)
        return spec.decode_codex_fn(tool_name, merged, raw_arg_str)
    return prior_payload


def encode_tool_payload_to_any(payload: CanonicalToolPayload) -> bytes:
    if isinstance(payload, OpaqueToolPayload):
        return encode_any_envelope(payload.type_url, payload.raw_any_bytes) if (payload.type_url and payload.raw_any_bytes is not None) else b""
    spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(payload.kind)
    return encode_any_envelope(spec.agy_any_type_url, spec.encode_agy_any_fn(payload)) if (spec and spec.encode_agy_any_fn) else b""


def decode_tool_payload(
    tool_name: str,
    arguments_json: str,
    any_type_url: str | None = None,
    any_value_bytes: bytes | None = None,
    *,
    is_codex_wire: bool = False,
) -> CanonicalToolPayload:
    spec = (TOOL_SPEC_BY_TYPE_URL.get(any_type_url) if any_type_url else None) or (TOOL_NAME_LOOKUP.get(tool_name) if tool_name else None)
    if spec is None:
        return OpaqueToolPayload(kind="opaque", tool_name=tool_name or "unknown_tool", raw_arguments_json=arguments_json or "{}", type_url=any_type_url, raw_any_bytes=any_value_bytes)
    args_dict = safe_json_dict(arguments_json)
    if is_codex_wire and not any_value_bytes and spec.decode_codex_fn is not None:
        return spec.decode_codex_fn(tool_name, args_dict, arguments_json or "{}")
    if spec.decode_agy_any_fn is not None:
        return spec.decode_agy_any_fn(args_dict, arguments_json or "{}", any_value_bytes or b"")
    return OpaqueToolPayload(kind="opaque", tool_name=tool_name or spec.agy_tool_name, raw_arguments_json=arguments_json or "{}", type_url=any_type_url, raw_any_bytes=any_value_bytes)


def encode_tool_for_codex(tool_name: str, payload: CanonicalToolPayload) -> tuple[str, str, str, str | None]:
    if isinstance(payload, OpaqueToolPayload):
        return ("tool_search_call" if tool_name == "tool_search" else "function_call", tool_name, payload.raw_arguments_json or "{}", None)
    spec = TOOL_SPEC_BY_PAYLOAD_KIND.get(payload.kind)
    return spec.encode_codex_fn(payload) if (spec and spec.encode_codex_fn) else ("function_call", tool_name, serialize_tool_payload_arguments_json(payload), None)
