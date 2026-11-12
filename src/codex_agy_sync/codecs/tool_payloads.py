"""Antigravity `exa.cortex.core.tools.*` Protobuf wire encoders and decoders."""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping, Sequence

from .._wire import (
    encode_bytes_field,
    encode_string_field,
    encode_varint_field,
    parse_proto_fields,
    signed_int32,
    signed_int64,
)
from ..ledger import (
    AskQuestionPayload,
    DefineSubagentPayload,
    FileEditPayload,
    FinishPayload,
    GenerateImagePayload,
    InvokeSubagentPayload,
    ListResourcesPayload,
    ManageSubagentsPayload,
    ManageTaskPayload,
    MomaSearchPayload,
    QuestionItem,
    QuestionOption,
    ReadFilePayload,
    ReadResourcePayload,
    RunCommandPayload,
    SchedulePayload,
    SearchWebDocument,
    SearchWebPayload,
    SendMessagePayload,
    normalize_tool_file_path,
)
from .schema_tags import (
    AnyEnvelopeTag,
    ArtifactMetaTag,
    AskQuestionEntryTag,
    AskQuestionTag,
    DefineSubagentTag,
    DiffBlockTag,
    DiffStatsTag,
    FileEditKindCode,
    FileEditTag,
    FinishTag,
    GenerateImageTag,
    InvokeSubagentTag,
    ListResourcesTag,
    ManageSubagentsTag,
    ManageTaskTag,
    McpResourceContentFieldTag,
    McpResourceFieldTag,
    McpTextContentFieldTag,
    MomaSearchTag,
    QuestionOptionFieldTag,
    ReadFileTag,
    ReadResourceTag,
    RunCommandTag,
    SandboxDetailsTag,
    ScheduleTag,
    SearchDocTag,
    SearchWebTag,
    SendMessageTag,
    SubagentResultFieldTag,
    SubagentSpecFieldTag,
    SubagentStateFieldTag,
    UnifiedDiffLineTag,
    UnifiedDiffLineTypeCode,
    UnifiedDiffTag,
)

_PATCH_HEADER_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File:\s*(.+)$", flags=re.MULTILINE)
_FieldMap = Mapping[int, Sequence[tuple[int, int | bytes]]]


def first_present(d: Mapping[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _pmap(buf: bytes) -> _FieldMap:
    return parse_proto_fields(buf)[0] if buf else {}


def _fm_str(fm: _FieldMap, tag: int, fallback: str = "") -> str:
    out = fallback
    for wt, val in fm.get(int(tag), ()):
        if wt == 2 and isinstance(val, bytes):
            s = val.decode("utf-8", errors="replace")
            if s or not fallback:
                out = s
    return out


def _fm_opt_str(fm: _FieldMap, tag: int, fallback: Any = None) -> str | None:
    out: str | None = str(fallback) if fallback is not None and str(fallback) != "" else None
    for wt, val in fm.get(int(tag), ()):
        if wt == 2 and isinstance(val, bytes):
            s = val.decode("utf-8", errors="replace")
            out = s if s != "" else None
    return out


def _fm_strs(fm: _FieldMap, tag: int) -> list[str]:
    return [val.decode("utf-8", errors="replace") for wt, val in fm.get(int(tag), ()) if wt == 2 and isinstance(val, bytes)]


def _fm_bytes(fm: _FieldMap, tag: int) -> list[bytes]:
    return [val for wt, val in fm.get(int(tag), ()) if wt == 2 and isinstance(val, bytes)]


def _fm_varint(fm: _FieldMap, tag: int) -> int | None:
    out: int | None = None
    for wt, val in fm.get(int(tag), ()):
        if wt == 0 and isinstance(val, int):
            out = int(val)
    return out


def decode_any_envelope(buf: bytes) -> tuple[str, bytes]:
    fm = _pmap(buf)
    raw_vals = _fm_bytes(fm, AnyEnvelopeTag.VALUE)
    return _fm_str(fm, AnyEnvelopeTag.TYPE_URL), (raw_vals[-1] if raw_vals else b"")


def encode_any_envelope(type_url: str, value_bytes: bytes) -> bytes:
    return encode_string_field(AnyEnvelopeTag.TYPE_URL, type_url) + encode_bytes_field(AnyEnvelopeTag.VALUE, value_bytes)


def _compute_diff_stats(unified_diff: str) -> tuple[int, int]:
    adds = sum(1 for ln in unified_diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in unified_diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return adds, dels


def decode_unified_diff_block(diff_block_bytes: bytes) -> str:
    lines_out: list[str] = []
    for u_raw in _fm_bytes(_pmap(diff_block_bytes), DiffBlockTag.UNIFIED_DIFF):
        for l_raw in _fm_bytes(_pmap(u_raw), UnifiedDiffTag.LINES):
            lfm = _pmap(l_raw)
            l_text = _fm_str(lfm, UnifiedDiffLineTag.TEXT)
            l_type = _fm_varint(lfm, UnifiedDiffLineTag.TYPE) or 0
            prefix = (
                "+" if l_type == UnifiedDiffLineTypeCode.INSERT
                else "-" if l_type == UnifiedDiffLineTypeCode.DELETE
                else " " if l_type == UnifiedDiffLineTypeCode.UNCHANGED
                else ""
            )
            lines_out.append(f"{prefix}{l_text}")
    return "\n".join(lines_out)


def encode_unified_diff_block(unified_diff: str) -> bytes:
    if not unified_diff:
        return b""
    ud_buf = bytearray()
    for line in unified_diff.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            l_type, l_text = UnifiedDiffLineTypeCode.INSERT, line[1:]
        elif line.startswith("-") and not line.startswith("---"):
            l_type, l_text = UnifiedDiffLineTypeCode.DELETE, line[1:]
        elif line.startswith(" "):
            l_type, l_text = UnifiedDiffLineTypeCode.UNCHANGED, line[1:]
        else:
            l_type, l_text = UnifiedDiffLineTypeCode.UNSPECIFIED, line
        line_msg = (encode_string_field(UnifiedDiffLineTag.TEXT, l_text) if l_text else b"") + (
            encode_varint_field(UnifiedDiffLineTag.TYPE, int(l_type)) if l_type != 0 else b""
        )
        ud_buf.extend(encode_bytes_field(UnifiedDiffTag.LINES, line_msg))
    return encode_bytes_field(DiffBlockTag.UNIFIED_DIFF, bytes(ud_buf))


def encode_any_read_file(p: ReadFilePayload) -> bytes:
    return (
        (encode_varint_field(ReadFileTag.FILE_NUM_LINES, p.file_num_lines) if p.file_num_lines else b"")
        + (encode_varint_field(ReadFileTag.FILE_NUM_BYTES, p.file_num_bytes) if p.file_num_bytes else b"")
        + encode_string_field(ReadFileTag.PATH, normalize_tool_file_path(p.path))
        + (encode_varint_field(ReadFileTag.START_LINE, p.start_line) if p.start_line is not None else b"")
        + (encode_varint_field(ReadFileTag.END_LINE, p.end_line) if p.end_line is not None else b"")
    )


def decode_any_read_file(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> ReadFilePayload:
    fm = _pmap(any_val)
    path = _fm_str(fm, ReadFileTag.PATH, str(first_present(args, "Path", "path") or ""))
    s_raw, e_raw = _fm_varint(fm, ReadFileTag.START_LINE), _fm_varint(fm, ReadFileTag.END_LINE)
    start_line = signed_int32(s_raw) if s_raw is not None else first_present(args, "StartLine", "start_line")
    end_line = signed_int32(e_raw) if e_raw is not None else first_present(args, "EndLine", "end_line")
    byte_off = first_present(args, "ByteOffset", "byte_offset")
    fl, fb = _fm_varint(fm, ReadFileTag.FILE_NUM_LINES), _fm_varint(fm, ReadFileTag.FILE_NUM_BYTES)
    f_lines = fl if fl is not None else args.get("file_num_lines")
    f_bytes = fb if fb is not None else args.get("file_num_bytes")
    return ReadFilePayload(
        kind="read_file", path=normalize_tool_file_path(path),
        start_line=int(start_line) if start_line is not None else None,
        end_line=int(end_line) if end_line is not None else None,
        byte_offset=int(byte_off) if byte_off is not None else None,
        file_num_lines=int(f_lines) if f_lines is not None else None,
        file_num_bytes=int(f_bytes) if f_bytes is not None else None,
        content_excerpt=args.get("content_excerpt"),
    )


def encode_any_file_edit(p: FileEditPayload) -> bytes:
    buf = bytearray()
    if p.edit_op == "CREATE":
        buf.extend(encode_varint_field(FileEditTag.KIND, int(FileEditKindCode.CREATE)))
    elif p.edit_op == "MODIFY":
        buf.extend(encode_varint_field(FileEditTag.KIND, int(FileEditKindCode.EDIT)))
    buf.extend(encode_string_field(FileEditTag.PATH, normalize_tool_file_path(p.path)))
    if p.unified_diff and (diff_block := encode_unified_diff_block(p.unified_diff)):
        buf.extend(encode_bytes_field(FileEditTag.DIFF, diff_block))
    adds, dels = p.diff_stats
    if adds or dels:
        buf.extend(encode_bytes_field(FileEditTag.DIFF_STATS, encode_varint_field(DiffStatsTag.ADDITIONS, adds) + encode_varint_field(DiffStatsTag.DELETIONS, dels)))
    if p.artifact_summary:
        buf.extend(encode_bytes_field(FileEditTag.ARTIFACT_METADATA, encode_string_field(ArtifactMetaTag.SUMMARY, p.artifact_summary)))
    if p.is_artifact:
        buf.extend(encode_varint_field(FileEditTag.IS_ARTIFACT_FILE, 1))
    return bytes(buf)


def decode_any_file_edit(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> FileEditPayload:
    fm = _pmap(any_val)
    path = _fm_str(fm, FileEditTag.PATH, str(first_present(args, "Path", "path") or ""))
    prior_op = str(first_present(args, "edit_op", "EditOp") or "").upper()
    edit_op: Literal["CREATE", "MODIFY", "DELETE"] = prior_op if prior_op in ("CREATE", "MODIFY", "DELETE") else "MODIFY"  # type: ignore[assignment]
    if (k_val := _fm_varint(fm, FileEditTag.KIND)) is not None:
        edit_op = "CREATE" if k_val == FileEditKindCode.CREATE else ("DELETE" if k_val == 0 else "MODIFY")
    elif fm and (prior_op == "DELETE" or not prior_op):
        edit_op = "DELETE"

    unified_diff = str(args.get("unified_diff") or "")
    if not unified_diff and any(k in args for k in ("Target", "Replacement", "Content")):
        if "Content" in args and "Target" not in args:
            edit_op = "CREATE" if not prior_op else edit_op
            unified_diff = "\n".join(f"+{l}" for l in str(args.get("Content", "")).splitlines())
        else:
            unified_diff = "\n".join(
                [f"-{l}" for l in str(args.get("Target", "")).splitlines()]
                + [f"+{l}" for l in str(args.get("Replacement", "")).splitlines()]
            )
    if diff_raws := _fm_bytes(fm, FileEditTag.DIFF):
        unified_diff = decode_unified_diff_block(diff_raws[-1])

    raw_stats = args.get("diff_stats")
    adds, dels = (int(raw_stats[0]), int(raw_stats[1])) if isinstance(raw_stats, (list, tuple)) and len(raw_stats) == 2 else (0, 0)
    saw_stats = False
    for s_raw in _fm_bytes(fm, FileEditTag.DIFF_STATS):
        saw_stats, sfm = True, _pmap(s_raw)
        adds = signed_int32(v) if (v := _fm_varint(sfm, DiffStatsTag.ADDITIONS)) is not None else adds
        dels = signed_int32(v) if (v := _fm_varint(sfm, DiffStatsTag.DELETIONS)) is not None else dels

    is_art = bool(_fm_varint(fm, FileEditTag.IS_ARTIFACT_FILE)) if FileEditTag.IS_ARTIFACT_FILE in fm else bool(first_present(args, "IsArtifact", "is_artifact") or False)
    art_sum: str | None = first_present(args, "ArtifactSummary", "artifact_summary")
    if isinstance(args.get("ArtifactMetadata"), dict) and not art_sum:
        is_art, art_sum = True, (str(args["ArtifactMetadata"].get("Summary") or "") or None)
    for a_raw in _fm_bytes(fm, FileEditTag.ARTIFACT_METADATA):
        art_sum = _fm_opt_str(_pmap(a_raw), ArtifactMetaTag.SUMMARY, art_sum)

    if unified_diff and (m := _PATCH_HEADER_RE.search(unified_diff)):
        path = path or m.group(2)
        edit_op = "CREATE" if m.group(1) == "Add" else ("DELETE" if m.group(1) == "Delete" else "MODIFY")
        unified_diff = _PATCH_HEADER_RE.sub(lambda hm: f"*** {hm.group(1)} File: {normalize_tool_file_path(hm.group(2))}", unified_diff)
    if not saw_stats and adds == 0 and dels == 0 and unified_diff:
        adds, dels = _compute_diff_stats(unified_diff)
    return FileEditPayload(
        kind="file_edit", path=normalize_tool_file_path(path), edit_op=edit_op,
        unified_diff=unified_diff, diff_stats=(adds, dels), is_artifact=is_art, artifact_summary=art_sum,
    )


def encode_any_run_command(p: RunCommandPayload) -> bytes:
    sb = (
        (encode_varint_field(SandboxDetailsTag.RAN_IN_SANDBOX, 1) if p.ran_in_sandbox else b"")
        + (encode_string_field(SandboxDetailsTag.SANDBOX_ERRORS, p.sandbox_policy) if p.sandbox_policy else b"")
    )
    return (
        (encode_varint_field(RunCommandTag.EXIT_CODE, p.exit_code) if p.exit_code is not None else encode_varint_field(RunCommandTag.EXIT_CODE_UNKNOWN, 1))
        + (encode_varint_field(RunCommandTag.TIMED_OUT, 1) if p.timed_out else b"")
        + (encode_bytes_field(RunCommandTag.SANDBOX_DETAILS, sb) if sb else b"")
        + (encode_string_field(RunCommandTag.SHELL_NAME, p.shell_name) if p.shell_name else b"")
        + encode_string_field(RunCommandTag.COMMAND_LINE, p.command_line)
        + encode_string_field(RunCommandTag.CWD, normalize_tool_file_path(p.cwd))
    )


def decode_any_run_command(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> RunCommandPayload:
    fm = _pmap(any_val)
    cmd = _fm_str(fm, RunCommandTag.COMMAND_LINE, str(first_present(args, "CommandLine", "command_line", "cmd", "command") or ""))
    cwd = _fm_str(fm, RunCommandTag.CWD, str(first_present(args, "Cwd", "cwd", "workdir") or ""))
    shell = _fm_opt_str(fm, RunCommandTag.SHELL_NAME, args.get("shell_name"))
    timed_out = bool(_fm_varint(fm, RunCommandTag.TIMED_OUT)) if RunCommandTag.TIMED_OUT in fm else bool(args.get("timed_out", False))
    ran_sb, sb_pol = bool(args.get("ran_in_sandbox", False)), args.get("sandbox_policy")
    for sb_raw in _fm_bytes(fm, RunCommandTag.SANDBOX_DETAILS):
        sbfm = _pmap(sb_raw)
        if SandboxDetailsTag.RAN_IN_SANDBOX in sbfm:
            ran_sb = bool(_fm_varint(sbfm, SandboxDetailsTag.RAN_IN_SANDBOX))
        sb_pol = _fm_opt_str(sbfm, SandboxDetailsTag.SANDBOX_ERRORS, sb_pol)
    exit_code: int | None = args.get("exit_code")
    if (ec_raw := _fm_varint(fm, RunCommandTag.EXIT_CODE)) is not None:
        exit_code = signed_int32(ec_raw)
    if _fm_varint(fm, RunCommandTag.EXIT_CODE_UNKNOWN):
        exit_code = None
    elif exit_code is None and fm and any(t in fm for t in (3, 4, 5, 6, 7)):
        exit_code = 0
    return RunCommandPayload(
        kind="run_command", command_line=cmd, cwd=normalize_tool_file_path(cwd),
        exit_code=int(exit_code) if exit_code is not None else None, timed_out=timed_out,
        is_daemon=bool(first_present(args, "IsDaemon", "is_daemon") or False),
        shell_name=shell, ran_in_sandbox=ran_sb, sandbox_policy=str(sb_pol) if sb_pol else None,
    )


def encode_any_search_web(p: SearchWebPayload) -> bytes:
    docs = b"".join(
        encode_bytes_field(
            SearchWebTag.WEB_DOCUMENTS,
            encode_string_field(SearchDocTag.SNIPPET, d.snippet)
            + encode_string_field(SearchDocTag.URL, d.url)
            + encode_string_field(SearchDocTag.TITLE, d.title),
        )
        for d in p.documents
    )
    return (
        encode_string_field(SearchWebTag.QUERY, p.query)
        + (encode_string_field(SearchWebTag.DOMAIN, p.domain_filter) if p.domain_filter else b"")
        + docs
        + (encode_string_field(SearchWebTag.SUMMARY, p.summary) if p.summary else b"")
    )


def decode_any_search_web(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> SearchWebPayload:
    fm = _pmap(any_val)
    query = _fm_str(fm, SearchWebTag.QUERY, str(first_present(args, "query", "q") or ""))
    domain = _fm_opt_str(fm, SearchWebTag.DOMAIN, first_present(args, "domain", "domain_filter"))
    summary = _fm_opt_str(fm, SearchWebTag.SUMMARY, args.get("summary"))
    if SearchWebTag.WEB_DOCUMENTS in fm:
        docs = [
            SearchWebDocument(
                title=_fm_str(dfm, SearchDocTag.TITLE),
                url=_fm_str(dfm, SearchDocTag.URL),
                snippet=_fm_str(dfm, SearchDocTag.SNIPPET) or _fm_str(dfm, SearchDocTag.FALLBACK_TEXT),
            )
            for d_raw in _fm_bytes(fm, SearchWebTag.WEB_DOCUMENTS)
            for dfm in (_pmap(d_raw),)
        ]
    else:
        docs = [
            SearchWebDocument(str(d.get("title", "")), str(d.get("url", "")), str(d.get("snippet", "")))
            for d in (args.get("documents") or []) if isinstance(d, dict)
        ]
    return SearchWebPayload(kind="search_web", query=query, domain_filter=domain, documents=tuple(docs), summary=summary)


def encode_any_invoke_subagent(p: InvokeSubagentPayload) -> bytes:
    ws_field = (
        encode_string_field(SubagentSpecFieldTag.BRANCH_WORKSPACE, "branch") if p.workspace_mode == "branch"
        else encode_string_field(SubagentSpecFieldTag.SHARE_WORKSPACE, "share") if p.workspace_mode == "share"
        else encode_varint_field(SubagentSpecFieldTag.INHERIT_WORKSPACE, 1)
    )
    sub = (
        encode_string_field(SubagentSpecFieldTag.TYPE_NAME, p.subagent_type)
        + encode_string_field(SubagentSpecFieldTag.ROLE, p.role)
        + encode_string_field(SubagentSpecFieldTag.PROMPT, p.prompt)
        + ws_field
    )
    return (
        encode_bytes_field(InvokeSubagentTag.SUBAGENTS, sub)
        + (encode_bytes_field(InvokeSubagentTag.RESULTS, encode_string_field(SubagentResultFieldTag.CONVERSATION_ID, p.spawned_conversation_id)) if p.spawned_conversation_id else b"")
        + (encode_varint_field(InvokeSubagentTag.TASK_MODE, 1) if p.task_mode else b"")
    )


def decode_any_invoke_subagent(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> InvokeSubagentPayload:
    fm = _pmap(any_val)
    sub_type = str(first_present(args, "TypeName", "subagent_type", "agent_type") or "")
    role = str(first_present(args, "Role", "role") or "")
    prompt = str(first_present(args, "Prompt", "prompt", "message") or "")
    raw_ws = str(first_present(args, "Workspace", "workspace_mode") or "inherit")
    ws_mode: Literal["inherit", "branch", "share"] = raw_ws if raw_ws in ("inherit", "branch", "share") else "inherit"  # type: ignore[assignment]
    for s_raw in _fm_bytes(fm, InvokeSubagentTag.SUBAGENTS):
        sfm = _pmap(s_raw)
        sub_type = _fm_str(sfm, SubagentSpecFieldTag.TYPE_NAME, sub_type)
        role = _fm_str(sfm, SubagentSpecFieldTag.ROLE, role)
        prompt = _fm_str(sfm, SubagentSpecFieldTag.PROMPT, prompt)
        ws_mode = "branch" if SubagentSpecFieldTag.BRANCH_WORKSPACE in sfm else ("share" if SubagentSpecFieldTag.SHARE_WORKSPACE in sfm else ws_mode)
    spawned_cid: str | None = args.get("spawned_conversation_id")
    for r_raw in _fm_bytes(fm, InvokeSubagentTag.RESULTS):
        spawned_cid = _fm_opt_str(_pmap(r_raw), SubagentResultFieldTag.CONVERSATION_ID, spawned_cid)
    task_mode = bool(_fm_varint(fm, InvokeSubagentTag.TASK_MODE)) if InvokeSubagentTag.TASK_MODE in fm else bool(args.get("task_mode", False))
    return InvokeSubagentPayload(kind="invoke_subagent", subagent_type=sub_type, role=role, prompt=prompt, spawned_conversation_id=spawned_cid, workspace_mode=ws_mode, task_mode=task_mode)


def encode_any_define_subagent(p: DefineSubagentPayload) -> bytes:
    return (
        encode_string_field(DefineSubagentTag.NAME, p.name)
        + b"".join(encode_string_field(DefineSubagentTag.TOOL_NAMES, t) for t in p.tool_names)
        + (encode_string_field(DefineSubagentTag.AGENT_PATH, normalize_tool_file_path(p.agent_path)) if p.agent_path else b"")
    )


def decode_any_define_subagent(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> DefineSubagentPayload:
    fm = _pmap(any_val)
    name = _fm_str(fm, DefineSubagentTag.NAME, str(first_present(args, "Name", "name") or ""))
    agent_path = _fm_opt_str(fm, DefineSubagentTag.AGENT_PATH, first_present(args, "AgentPath", "agent_path"))
    fb = first_present(args, "ToolNames", "tool_names")
    tools = _fm_strs(fm, DefineSubagentTag.TOOL_NAMES) if DefineSubagentTag.TOOL_NAMES in fm else ([str(t) for t in fb] if isinstance(fb, list) else [])
    return DefineSubagentPayload(kind="define_subagent", name=name, tool_names=tuple(tools), agent_path=normalize_tool_file_path(agent_path) if agent_path is not None else None)


def encode_any_manage_subagents(p: ManageSubagentsPayload) -> bytes:
    items = (
        b"".join(encode_bytes_field(ManageSubagentsTag.SUBAGENTS, encode_string_field(SubagentStateFieldTag.CONVERSATION_ID, c)) for c in p.target_conversation_ids)
        if p.action == "list"
        else b"".join(encode_string_field(ManageSubagentsTag.CONVERSATION_IDS, c) for c in p.target_conversation_ids)
    )
    return encode_string_field(ManageSubagentsTag.ACTION, p.action) + items


def decode_any_manage_subagents(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> ManageSubagentsPayload:
    fm = _pmap(any_val)
    raw_action = _fm_str(fm, ManageSubagentsTag.ACTION, str(first_present(args, "Action", "action") or "list"))
    wire_cids: list[str] = []
    for cid in _fm_strs(fm, ManageSubagentsTag.CONVERSATION_IDS):
        if cid and cid not in wire_cids:
            wire_cids.append(cid)
    for s_raw in _fm_bytes(fm, ManageSubagentsTag.SUBAGENTS):
        if (cid := _fm_str(_pmap(s_raw), SubagentStateFieldTag.CONVERSATION_ID)) and cid not in wire_cids:
            wire_cids.append(cid)
    has_wire = ManageSubagentsTag.CONVERSATION_IDS in fm or ManageSubagentsTag.SUBAGENTS in fm
    fb = first_present(args, "ConversationIds", "target_conversation_ids", "ids")
    cids = wire_cids if has_wire else ([str(c) for c in fb] if isinstance(fb, list) else [])
    if not cids and not has_wire and (single := first_present(args, "id", "agent_id")):
        cids = [str(single)]
    action_norm: Literal["list", "kill", "kill_all"] = raw_action if raw_action in ("list", "kill", "kill_all") else "list"  # type: ignore[assignment]
    return ManageSubagentsPayload(kind="manage_subagents", action=action_norm, target_conversation_ids=tuple(cids))


def encode_any_manage_task(p: ManageTaskPayload) -> bytes:
    return (
        encode_string_field(ManageTaskTag.ACTION, p.action)
        + (encode_string_field(ManageTaskTag.TASK_IDS, p.task_id) if p.task_id else b"")
        + (encode_string_field(ManageTaskTag.INPUT, p.input_text) if p.input_text else b"")
    )


def decode_any_manage_task(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> ManageTaskPayload:
    fm = _pmap(any_val)
    raw_action = _fm_str(fm, ManageTaskTag.ACTION, str(first_present(args, "Action", "action") or "status"))
    wire_ids = _fm_strs(fm, ManageTaskTag.TASK_IDS)
    task_id = first_present(args, "TaskId", "task_id", "session_id") or (wire_ids[0] if wire_ids else None)
    inp = _fm_opt_str(fm, ManageTaskTag.INPUT, first_present(args, "Input", "input_text", "chars"))
    action_norm: Literal["list", "status", "send_input", "kill"] = raw_action if raw_action in ("list", "status", "send_input", "kill") else "status"  # type: ignore[assignment]
    return ManageTaskPayload(kind="manage_task", action=action_norm, task_id=str(task_id) if task_id else None, input_text=inp)


def encode_any_send_message(p: SendMessagePayload) -> bytes:
    return (
        encode_string_field(SendMessageTag.RECIPIENT, p.recipient_id)
        + (encode_string_field(SendMessageTag.RECIPIENT_NAME, p.recipient_name) if p.recipient_name else b"")
        + encode_string_field(SendMessageTag.MESSAGE, p.message_body)
    )


def decode_any_send_message(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> SendMessagePayload:
    fm = _pmap(any_val)
    recipient = _fm_str(fm, SendMessageTag.RECIPIENT, str(first_present(args, "Recipient", "recipient_id", "recipient", "to", "id") or ""))
    msg = _fm_str(fm, SendMessageTag.MESSAGE, str(first_present(args, "Message", "message_body", "message", "prompt") or ""))
    rname = _fm_opt_str(fm, SendMessageTag.RECIPIENT_NAME, first_present(args, "recipient_name", "reply_to"))
    return SendMessagePayload(kind="send_message", recipient_id=recipient, message_body=msg, recipient_name=rname)


def encode_any_schedule(p: SchedulePayload) -> bytes:
    return encode_string_field(ScheduleTag.MODE, "cron" if p.cron_expression else "timer") + (
        encode_varint_field(ScheduleTag.ITERATIONS, p.max_iterations) if p.max_iterations is not None else b""
    )


def decode_any_schedule(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> SchedulePayload:
    fm = _pmap(any_val)
    dur = first_present(args, "DurationSeconds", "duration_seconds")
    cron = first_present(args, "CronExpression", "cron_expression")
    max_iters = first_present(args, "MaxIterations", "max_iterations")
    if max_iters is None and (it_raw := _fm_varint(fm, ScheduleTag.ITERATIONS)) is not None:
        max_iters = signed_int64(it_raw)
    return SchedulePayload(
        kind="schedule", prompt=str(first_present(args, "Prompt", "prompt") or ""),
        duration_seconds=int(dur) if dur is not None else None,
        cron_expression=str(cron) if cron is not None and str(cron) != "" else None,
        timer_condition=str(first_present(args, "TimerCondition", "timer_condition") or "never"),
        is_daemon=bool(first_present(args, "IsDaemon", "is_daemon") or False),
        exact_timing=bool(first_present(args, "ExactTiming", "exact_timing") or False),
        max_iterations=int(max_iters) if max_iters is not None else None,
    )


def encode_any_ask_question(p: AskQuestionPayload) -> bytes:
    buf = bytearray()
    for q in p.questions:
        opts = b"".join(
            encode_bytes_field(AskQuestionEntryTag.OPTIONS, encode_string_field(QuestionOptionFieldTag.ID, o.description or o.label) + encode_string_field(QuestionOptionFieldTag.TEXT, o.label))
            for o in q.options
        )
        ans = b"".join(encode_string_field(AskQuestionEntryTag.USER_ANSWERS, a) for a in q.user_answers)
        q_msg = encode_string_field(AskQuestionEntryTag.QUESTION, q.question) + opts + (encode_varint_field(AskQuestionEntryTag.IS_MULTI_SELECT, 1) if q.is_multi_select else b"") + ans
        buf.extend(encode_bytes_field(AskQuestionTag.QUESTIONS, q_msg))
    return bytes(buf)


def decode_any_ask_question(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> AskQuestionPayload:
    fm = _pmap(any_val)
    ans_by_idx = args.get("user_answers_by_index") if isinstance(args.get("user_answers_by_index"), list) else []
    if AskQuestionTag.QUESTIONS in fm:
        qs: list[QuestionItem] = []
        for idx, q_raw in enumerate(_fm_bytes(fm, AskQuestionTag.QUESTIONS)):
            qfm = _pmap(q_raw)
            opts = [
                QuestionOption(label=txt or oid, description="" if oid == txt else oid)
                for o_raw in _fm_bytes(qfm, AskQuestionEntryTag.OPTIONS)
                for ofm in (_pmap(o_raw),)
                for oid, txt in ((_fm_str(ofm, QuestionOptionFieldTag.ID), _fm_str(ofm, QuestionOptionFieldTag.TEXT)),)
            ]
            ans = _fm_strs(qfm, AskQuestionEntryTag.USER_ANSWERS) + _fm_strs(qfm, AskQuestionEntryTag.ALT_ANSWERS)
            if not ans and idx < len(ans_by_idx) and isinstance(ans_by_idx[idx], list):
                ans = [str(a) for a in ans_by_idx[idx]]
            qs.append(QuestionItem(_fm_str(qfm, AskQuestionEntryTag.QUESTION), tuple(opts), bool(_fm_varint(qfm, AskQuestionEntryTag.IS_MULTI_SELECT)), tuple(ans)))
        return AskQuestionPayload(kind="ask_question", questions=tuple(qs))
    raw_qs = first_present(args, "Questions", "questions")
    qs = []
    if isinstance(raw_qs, list):
        for idx, q in enumerate(raw_qs):
            if isinstance(q, dict):
                opts = [
                    QuestionOption(str(o.get("label", o.get("text", ""))), str(o.get("description", "")))
                    if isinstance(o, dict) else QuestionOption(str(o))
                    for o in (q.get("options") or q.get("Options") or [])
                ]
                fb_ans = ans_by_idx[idx] if idx < len(ans_by_idx) and isinstance(ans_by_idx[idx], list) else (q.get("user_answers") or [])
                qs.append(QuestionItem(str(first_present(q, "Question", "question") or ""), tuple(opts), bool(first_present(q, "MultiSelect", "is_multi_select") or False), tuple(str(a) for a in fb_ans)))
    return AskQuestionPayload(kind="ask_question", questions=tuple(qs))


def encode_any_list_resources(p: ListResourcesPayload) -> bytes:
    return encode_string_field(ListResourcesTag.SERVER_NAME, p.server_name) + b"".join(
        encode_bytes_field(ListResourcesTag.RESOURCES, encode_string_field(McpResourceFieldTag.URI, u)) for u in p.resource_uris
    )


def decode_any_list_resources(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> ListResourcesPayload:
    fm = _pmap(any_val)
    server = _fm_str(fm, ListResourcesTag.SERVER_NAME, str(first_present(args, "ServerName", "server_name", "server") or ""))
    uris = [u for r in _fm_bytes(fm, ListResourcesTag.RESOURCES) if (u := _fm_str(_pmap(r), McpResourceFieldTag.URI))] if ListResourcesTag.RESOURCES in fm else [str(u) for u in (args.get("resource_uris") or [])]
    return ListResourcesPayload(kind="list_resources", server_name=server, resource_uris=tuple(uris))


def encode_any_read_resource(p: ReadResourcePayload) -> bytes:
    items = b"".join(
        encode_bytes_field(
            ReadResourceTag.CONTENTS,
            encode_string_field(McpResourceContentFieldTag.URI, p.uri)
            + encode_bytes_field(McpResourceContentFieldTag.TEXT_CONTENT, encode_string_field(McpTextContentFieldTag.TEXT, t)),
        )
        for t in p.contents_text
    )
    return encode_string_field(ReadResourceTag.SERVER_NAME, p.server_name) + encode_string_field(ReadResourceTag.URI, p.uri) + items


def decode_any_read_resource(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> ReadResourcePayload:
    fm = _pmap(any_val)
    server = _fm_str(fm, ReadResourceTag.SERVER_NAME, str(first_present(args, "ServerName", "server_name", "server") or ""))
    uri = _fm_str(fm, ReadResourceTag.URI, str(first_present(args, "Uri", "uri") or ""))
    if ReadResourceTag.CONTENTS in fm:
        texts = [
            _fm_str(_pmap(tc), McpTextContentFieldTag.TEXT)
            for c_raw in _fm_bytes(fm, ReadResourceTag.CONTENTS)
            for tc in _fm_bytes(_pmap(c_raw), McpResourceContentFieldTag.TEXT_CONTENT)
        ]
    else:
        texts = [str(t) for t in (args.get("contents_text") or [])]
    return ReadResourcePayload(kind="read_resource", server_name=server, uri=uri, contents_text=tuple(texts))


def encode_any_generate_image(p: GenerateImagePayload) -> bytes:
    return (
        encode_string_field(GenerateImageTag.PROMPT, p.prompt)
        + b"".join(encode_string_field(GenerateImageTag.IMAGE_PATHS, normalize_tool_file_path(x)) for x in p.image_paths)
        + encode_string_field(GenerateImageTag.ASPECT_RATIO, p.aspect_ratio)
        + (encode_string_field(GenerateImageTag.IMAGE_URI, p.image_uri) if p.image_uri else b"")
        + (encode_string_field(GenerateImageTag.MODEL_NAME, p.model_name) if p.model_name else b"")
    )


def decode_any_generate_image(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> GenerateImagePayload:
    fm = _pmap(any_val)
    prompt = _fm_str(fm, GenerateImageTag.PROMPT, str(first_present(args, "Prompt", "prompt") or ""))
    aspect = _fm_str(fm, GenerateImageTag.ASPECT_RATIO, str(first_present(args, "AspectRatio", "aspect_ratio") or "1:1"))
    fb_paths = first_present(args, "ImagePaths", "image_paths")
    paths = _fm_strs(fm, GenerateImageTag.IMAGE_PATHS) if GenerateImageTag.IMAGE_PATHS in fm else ([str(x) for x in fb_paths] if isinstance(fb_paths, list) else [])
    return GenerateImagePayload(
        kind="generate_image", prompt=prompt, image_paths=tuple(normalize_tool_file_path(x) for x in paths),
        aspect_ratio=aspect, image_uri=_fm_opt_str(fm, GenerateImageTag.IMAGE_URI, args.get("image_uri")),
        model_name=_fm_opt_str(fm, GenerateImageTag.MODEL_NAME, args.get("model_name")),
    )


def encode_any_moma_search(p: MomaSearchPayload) -> bytes:
    return encode_string_field(MomaSearchTag.QUERY, p.query) + b"".join(
        encode_bytes_field(MomaSearchTag.RESULTS, encode_string_field(2, f"result-{i}")) for i in range(p.result_count)
    )


def decode_any_moma_search(args: Mapping[str, Any], _raw_json: str, any_val: bytes) -> MomaSearchPayload:
    fm = _pmap(any_val)
    cnt = len(_fm_bytes(fm, MomaSearchTag.RESULTS)) if MomaSearchTag.RESULTS in fm else int(args.get("result_count", 0))
    return MomaSearchPayload(kind="moma_search", query=_fm_str(fm, MomaSearchTag.QUERY, str(first_present(args, "Query", "query") or "")), result_count=cnt)


def encode_any_finish(p: FinishPayload) -> bytes:
    return encode_string_field(FinishTag.ARGUMENTS_JSON, p.arguments_json) + (encode_varint_field(FinishTag.HAS_RESULT_SCHEMA, 1) if p.has_result_schema else b"")


def decode_any_finish(args: Mapping[str, Any], raw_json: str, any_val: bytes) -> FinishPayload:
    fm = _pmap(any_val)
    has_schema = bool(_fm_varint(fm, FinishTag.HAS_RESULT_SCHEMA)) if FinishTag.HAS_RESULT_SCHEMA in fm else bool(args.get("has_result_schema", False))
    return FinishPayload(kind="finish", arguments_json=_fm_str(fm, FinishTag.ARGUMENTS_JSON, raw_json or "{}"), has_result_schema=has_schema)
