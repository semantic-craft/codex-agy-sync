#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import re
import sqlite3

FORBIDDEN_AGY_PROJECT_BUCKETS = {"", "default-cli-project", "outside-of-project", "null", "none"}

CANONICAL_TOOL_MAPPING = {
    "run_command": "exec_command / custom_tool_call:exec",
    "read_file": "view_file / exec_command(cat/sed)",
    "edit_file": "apply_patch",
    "write_file": "apply_patch (Add File)",
    "invoke_subagent": "spawn_agent",
    "send_message": "send_message (collaboration)",
    "manage_subagents": "list_agents / wait_agent / interrupt_agent",
    "manage_task": "wait / send_input",
    "search_web": "web_search",
}


def read_varint(buf: bytes, pos: int):
    res = 0
    shift = 0
    while True:
        b = buf[pos]
        res |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            return res, pos
        shift += 7
        if shift > 70:
            raise ValueError("Varint too long")


def decode_protobuf(buf: bytes, msg_name: str, schema: dict, unknown_log: list, depth: int = 0) -> dict:
    msgs = schema["messages"]
    enums = schema["enums"]
    md = msgs.get(msg_name)
    if not md:
        unknown_log.append(f"missing_message_descriptor:{msg_name}")
        return {}
    fields = md["fields"]
    pos = 0
    n = len(buf)
    out = {}
    while pos < n:
        tag, pos = read_varint(buf, pos)
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:
            val, pos = read_varint(buf, pos)
        elif wt == 2:
            ln, pos = read_varint(buf, pos)
            val = buf[pos : pos + ln]
            pos += ln
        elif wt == 5:
            val = buf[pos : pos + 4]
            pos += 4
        elif wt == 1:
            val = buf[pos : pos + 8]
            pos += 8
        else:
            raise ValueError(f"Invalid wire type {wt} in {msg_name}")

        finfo = fields.get(str(fn))
        if not finfo:
            unknown_log.append(f"{msg_name}:field_{fn}(wt={wt})")
            continue

        fname = finfo["name"]
        ftype = finfo["type"]
        tname = finfo.get("type_name")
        is_repeated = finfo["label"] == "repeated"

        if is_repeated and wt == 2 and ftype in ("int32", "int64", "uint32", "uint64", "bool", "enum"):
            ppos = 0
            vals = []
            while ppos < len(val):
                pval, ppos = read_varint(val, ppos)
                if ftype == "enum" and tname in enums:
                    pval = enums[tname]["values"].get(str(pval), pval)
                vals.append(pval)
            out.setdefault(fname, []).extend(vals)
            continue

        if ftype == "message" and isinstance(val, bytes) and depth < 7:
            if tname == "google.protobuf.Any":
                any_obj = decode_protobuf(val, "google.protobuf.Any", schema, unknown_log, depth + 1)
                turl = any_obj.get("type_url", "")
                inner_type = turl.split("/")[-1] if "/" in turl else ""
                if inner_type in msgs and isinstance(any_obj.get("value"), bytes):
                    any_obj["decoded"] = decode_protobuf(
                        any_obj["value"], inner_type, schema, unknown_log, depth + 1
                    )
                any_obj.pop("value", None)
                val = any_obj
            elif tname in msgs:
                val = decode_protobuf(val, tname, schema, unknown_log, depth + 1)
            else:
                val = f"<bytes:{len(val)}>"
        elif ftype == "string" and isinstance(val, bytes):
            val = val.decode("utf-8", errors="replace")
        elif ftype == "bytes" and isinstance(val, bytes):
            if msg_name != "google.protobuf.Any":
                val = f"<bytes:{len(val)}>"
        elif ftype == "bool" and isinstance(val, int):
            val = bool(val)
        elif ftype == "enum" and isinstance(val, int) and tname in enums:
            val = enums[tname]["values"].get(str(val), val)

        if is_repeated:
            out.setdefault(fname, []).append(val)
        else:
            out[fname] = val
    return out


def open_sqlite_readonly(path: pathlib.Path):
    if not path.is_file():
        raise FileNotFoundError(f"SQLite file not found: {path}")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON;")
    return conn


def snapshot_file_stat(path: pathlib.Path) -> tuple:
    st = path.stat()
    wal = path.with_name(path.name + "-wal")
    wal_mtime = wal.stat().st_mtime_ns if wal.exists() else None
    return (st.st_ino, st.st_size, st.st_mtime_ns, wal_mtime)


def parse_pbtxt_title(pbtxt_path: pathlib.Path) -> str:
    if not pbtxt_path.is_file():
        return ""
    raw = pbtxt_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'title:\s*"((?:[^"\\]|\\.)*)"', raw)
    if not m:
        return ""
    return m.group(1)


def evaluate_agy_sidebar_visibility(project_binding: dict, sidebar_rules: dict) -> dict:
    agy_rules = sidebar_rules["antigravity_desktop_2_30"]
    forbidden_buckets = set(agy_rules["forbidden_project_id_buckets"])
    visible_sources = {int(k) for k in agy_rules["visible_trajectory_sources_enum"].keys()}
    visible_db_sources = set(agy_rules["conversation_summaries_db_source_column"]["desktop_visible_values"])

    pid = (project_binding.get("project_id") or "").strip()
    non_bucket = pid not in forbidden_buckets
    has_uris = bool(project_binding.get("workspace_uris"))
    src_enum = project_binding.get("trajectory_source_enum")
    src_enum_ok = (src_enum in visible_sources) if src_enum is not None else True
    src_col = project_binding.get("summary_source_col")
    src_col_ok = (src_col in visible_db_sources) if src_col is not None else True

    return {
        "is_valid_non_bucket_project": non_bucket,
        "has_workspace_uris": has_uris,
        "trajectory_source_visible": src_enum_ok,
        "summary_source_visible": src_col_ok,
        "sidebar_visible_predicate_met": non_bucket and has_uris and src_enum_ok and src_col_ok,
    }


def evaluate_codex_sidebar_visibility(project_binding: dict, sidebar_rules: dict) -> dict:
    req = sidebar_rules["codex_desktop_0_166"]["required_threads_predicate"]
    archived_ok = project_binding.get("archived") == req["archived"]
    preview_ok = bool(project_binding.get("preview_non_empty")) is req["preview_non_empty"]
    pid_ok = (project_binding.get("project_id") is not None and str(project_binding.get("project_id")).strip() != "") is req["project_id_not_null"]
    return {
        "archived_ok": archived_ok,
        "preview_non_empty_ok": preview_ok,
        "project_id_not_null_ok": pid_ok,
        "sidebar_visible_predicate_met": archived_ok and preview_ok and pid_ok,
    }


def select_default_agy_conversation(agy_root: pathlib.Path) -> str:
    sum_db = agy_root / "conversation_summaries.db"
    conn = open_sqlite_readonly(sum_db)
    rows = conn.execute(
        """
        SELECT conversation_id, step_count, project_id, workspace_uris
        FROM conversation_summaries
        WHERE source = ''
          AND project_id NOT IN ('', 'default-cli-project', 'outside-of-project', 'null', 'none')
          AND step_count >= 15
        ORDER BY CASE WHEN workspace_uris LIKE '%agent-fleet%' THEN 0 ELSE 1 END,
                 last_modified_time DESC
        """
    ).fetchall()
    conn.close()
    best_cid = None
    best_score = -1
    for cid, _, _, uris in rows:
        db_file = agy_root / "conversations" / f"{cid}.db"
        if not db_file.is_file():
            continue
        cconn = open_sqlite_readonly(db_file)
        stypes = {r[0] for r in cconn.execute("SELECT DISTINCT step_type FROM steps").fetchall()}
        cconn.close()
        if not {14, 15, 132}.issubset(stypes):
            continue
        score = len(stypes & {14, 15, 23, 101, 132}) * 10 + (5 if "agent-fleet" in (uris or "") else 0)
        if score > best_score:
            best_score = score
            best_cid = cid
        if score >= 55:
            return cid
    if best_cid:
        return best_cid
    raise RuntimeError("No suitable Antigravity 2.30+ conversation (.db) found with step_types 14, 15, 132")


def select_default_codex_thread(codex_root: pathlib.Path, preferred_cwd: str = "") -> str:
    state_db = codex_root / "state_5.sqlite"
    conn = open_sqlite_readonly(state_db)
    rows = conn.execute(
        """
        SELECT id, rollout_path, cwd, project_id
        FROM threads
        WHERE archived = 0
          AND preview <> ''
          AND project_id IS NOT NULL
        ORDER BY CASE WHEN cwd = ? THEN 0 ELSE 1 END,
                 recency_at_ms DESC
        LIMIT 80
        """,
        (preferred_cwd,),
    ).fetchall()
    conn.close()
    best_tid = None
    best_score = -1
    for tid, rpath, cwd, _ in rows:
        p = pathlib.Path(rpath)
        if not p.is_file():
            continue
        has_user = False
        has_reasoning = False
        tool_names = set()
        with open(p, encoding="utf-8") as f:
            for line in f:
                obj = json.loads(line)
                if obj.get("type") == "response_item":
                    pl = obj.get("payload", {})
                    pt = pl.get("type")
                    if pt == "message" and pl.get("role") == "user":
                        has_user = True
                    elif pt in ("reasoning", "magpie_reasoning"):
                        has_reasoning = True
                    elif pt in ("function_call", "custom_tool_call"):
                        tool_names.add(pl.get("name", ""))
        if not (has_user and has_reasoning and tool_names):
            continue
        has_exec_or_patch = bool(tool_names & {"exec_command", "exec", "apply_patch", "view_file"})
        score = (20 if has_exec_or_patch else 0) + (10 if cwd == preferred_cwd else 0) + min(len(tool_names), 5)
        if score > best_score:
            best_score = score
            best_tid = tid
        if score >= 32:
            return tid
    if best_tid:
        return best_tid
    raise RuntimeError("No suitable Codex 0.166+ thread found with user, reasoning, and tool calls")


def decode_antigravity_session(
    agy_root: pathlib.Path, config_root: pathlib.Path, cascade_id: str, schema: dict, sidebar_rules: dict
) -> dict:
    conv_db = agy_root / "conversations" / f"{cascade_id}.db"
    sum_db = agy_root / "conversation_summaries.db"
    proto_pb = agy_root / "agyhub_summaries_proto.pb"
    annot_pbtxt = agy_root / "annotations" / f"{cascade_id}.pbtxt"
    transcript_jsonl = agy_root / "brain" / cascade_id / ".system_generated" / "logs" / "transcript.jsonl"

    before_conv_stat = snapshot_file_stat(conv_db)
    before_sum_stat = snapshot_file_stat(sum_db)

    unknown_wire_fields = []

    cconn = open_sqlite_readonly(conv_db)
    user_version = cconn.execute("PRAGMA user_version;").fetchone()[0]
    tmeta_row = cconn.execute(
        "SELECT trajectory_id, cascade_id, trajectory_type, source FROM trajectory_meta LIMIT 1"
    ).fetchone()
    tblob_row = cconn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
    traj_meta = (
        decode_protobuf(tblob_row[0], "exa.cortex_pb.CortexTrajectoryMetadata", schema, unknown_wire_fields)
        if tblob_row and tblob_row[0]
        else {}
    )

    step_rows = cconn.execute(
        "SELECT idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format FROM steps ORDER BY idx ASC"
    ).fetchall()
    gen_count = cconn.execute("SELECT count(*) FROM gen_metadata").fetchone()[0]
    exec_count = cconn.execute("SELECT count(*) FROM executor_metadata").fetchone()[0]
    cconn.close()

    sconn = open_sqlite_readonly(sum_db)
    srow = sconn.execute(
        "SELECT title, preview, step_count, workspace_uris, status, source, project_id, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
        (cascade_id,),
    ).fetchone()
    sconn.close()

    raw_summary_decoded = {}
    if srow and srow[7]:
        raw_summary_decoded = decode_protobuf(
            srow[7], "exa.jetski_cortex_pb.CascadeTrajectorySummary", schema, unknown_wire_fields
        )

    hub_proto_state = {}
    if proto_pb.is_file():
        hub_proto_state = decode_protobuf(
            proto_pb.read_bytes(), "jetbox_summaries_pb.SummariesState", schema, unknown_wire_fields
        )

    user_inputs = []
    planner_responses = []
    tool_executions = []
    checkpoints = []
    system_messages = []
    step_type_counts = {}

    for idx, stype, status, has_sub, mblob, pblob, sfmt in step_rows:
        step_type_counts[str(stype)] = step_type_counts.get(str(stype), 0) + 1
        m_dec = (
            decode_protobuf(mblob, "exa.cortex_pb.CortexStepMetadata", schema, unknown_wire_fields)
            if mblob
            else {}
        )
        p_dec = decode_protobuf(pblob, "gemini_coder.Step", schema, unknown_wire_fields) if pblob else {}

        if stype == 14 or "user_input" in p_dec:
            ui = p_dec.get("user_input", {})
            items_text = [it.get("text", "") for it in ui.get("items", []) if isinstance(it, dict) and "text" in it]
            text = ui.get("user_response") or "\n".join(items_text) or ui.get("query", "")
            user_inputs.append({
                "step_index": idx,
                "source": m_dec.get("source"),
                "client_message_id": ui.get("client_message_id"),
                "text_length": len(text),
                "items_count": len(ui.get("items", [])),
            })
        elif stype == 15 or "planner_response" in p_dec:
            pr = p_dec.get("planner_response", {})
            tcalls = pr.get("tool_calls", [])
            planner_responses.append({
                "step_index": idx,
                "message_id": pr.get("message_id"),
                "response_length": len(pr.get("response", "")),
                "thinking_length": len(pr.get("thinking", "")),
                "has_thinking_signature": "thinking_signature" in pr or "signature" in pr,
                "stop_reason": pr.get("stop_reason"),
                "generator_model": m_dec.get("generator_model"),
                "tool_calls": [
                    {
                        "call_id": tc.get("id"),
                        "tool_name": tc.get("name"),
                        "arguments_json_len": len(tc.get("arguments_json", "")),
                    }
                    for tc in tcalls
                ],
            })
        elif stype == 132 or "generic" in p_dec:
            gen = p_dec.get("generic", {})
            res = gen.get("result", {})
            tc = m_dec.get("tool_call", {})
            any_payload = res.get("payload", {})
            tool_executions.append({
                "step_index": idx,
                "call_id": tc.get("id"),
                "tool_name": tc.get("name"),
                "tool_summary": m_dec.get("tool_summary"),
                "args_entries": [a.get("key") for a in gen.get("args", []) if isinstance(a, dict)],
                "result_text_len": len(res.get("result", "")),
                "typed_payload_url": any_payload.get("type_url") if isinstance(any_payload, dict) else None,
                "typed_payload_keys": sorted(any_payload.get("decoded", {}).keys())
                if isinstance(any_payload, dict) and isinstance(any_payload.get("decoded"), dict)
                else [],
            })
        elif stype == 23 or "checkpoint" in p_dec:
            cp = p_dec.get("checkpoint", {})
            checkpoints.append({
                "step_index": idx,
                "checkpoint_index": cp.get("checkpoint_index"),
                "conversation_title": cp.get("conversation_title"),
                "user_intent_len": len(cp.get("user_intent", "")),
                "session_summary_len": len(cp.get("session_summary", "")),
            })
        elif stype == 101 or "system_message" in p_dec:
            sm = p_dec.get("system_message", {})
            system_messages.append({
                "step_index": idx,
                "event_type": sm.get("event_type"),
                "message_len": len(sm.get("message", "")),
            })

    project_id = traj_meta.get("project_id") or (srow[6] if srow else "")
    project_json_path = config_root / "projects" / f"{project_id}.json" if project_id else None
    project_json = (
        json.loads(project_json_path.read_text(encoding="utf-8"))
        if project_json_path and project_json_path.is_file()
        else None
    )

    transcript_lines = 0
    if transcript_jsonl.is_file():
        with open(transcript_jsonl, encoding="utf-8") as tf:
            transcript_lines = sum(1 for _ in tf)

    after_conv_stat = snapshot_file_stat(conv_db)
    after_sum_stat = snapshot_file_stat(sum_db)

    raw_binding = {
        "project_id": project_id,
        "project_config_exists": bool(project_json),
        "project_name": project_json.get("name") if project_json else None,
        "workspace_uris": traj_meta.get("workspace_uris", []),
        "nesting_depth": traj_meta.get("nesting_depth", 0),
        "trajectory_source_enum": tmeta_row[3] if tmeta_row else None,
        "summary_source_col": srow[5] if srow else None,
        "pbtxt_title": parse_pbtxt_title(annot_pbtxt),
        "raw_summary_annotation_title": raw_summary_decoded.get("annotations", {}).get("title"),
        "summary_db_title": srow[0] if srow else None,
        "agyhub_summaries_total_count": len(hub_proto_state.get("summaries", [])),
    }
    raw_binding.update(evaluate_agy_sidebar_visibility(raw_binding, sidebar_rules))

    return {
        "cascade_id": cascade_id,
        "trajectory_id": tmeta_row[0] if tmeta_row else None,
        "pragma_user_version": user_version,
        "readonly_integrity": {
            "conv_db_unchanged": before_conv_stat == after_conv_stat,
            "summaries_db_unchanged": before_sum_stat == after_sum_stat,
        },
        "project_binding": raw_binding,
        "counts": {
            "total_steps": len(step_rows),
            "step_type_distribution": step_type_counts,
            "user_inputs": len(user_inputs),
            "planner_responses": len(planner_responses),
            "tool_executions": len(tool_executions),
            "checkpoints": len(checkpoints),
            "system_messages": len(system_messages),
            "gen_metadata_rows": gen_count,
            "executor_metadata_rows": exec_count,
            "companion_transcript_lines": transcript_lines,
            "unknown_wire_fields_count": len(unknown_wire_fields),
        },
        "unknown_wire_fields": sorted(set(unknown_wire_fields)),
        "samples": {
            "first_user_input": user_inputs[0] if user_inputs else None,
            "first_planner_with_tools": next((p for p in planner_responses if p["tool_calls"]), None),
            "first_tool_execution": tool_executions[0] if tool_executions else None,
            "first_checkpoint": checkpoints[0] if checkpoints else None,
        },
    }


def decode_codex_session(codex_root: pathlib.Path, thread_id: str, sidebar_rules: dict) -> dict:
    state_db = codex_root / "state_5.sqlite"
    global_state_json = codex_root / ".codex-global-state.json"

    before_stat = snapshot_file_stat(state_db)
    conn = open_sqlite_readonly(state_db)
    trow = conn.execute(
        """
        SELECT id, rollout_path, cwd, title, preview, source, originator,
               archived, project_id, created_at_ms, updated_at_ms, recency_at_ms,
               model, reasoning_effort, git_branch, git_sha
        FROM threads
        WHERE id = ?
        """,
        (thread_id,),
    ).fetchone()
    if not trow:
        conn.close()
        raise ValueError(f"Codex thread_id {thread_id} not found in state_5.sqlite")

    project_id = trow[8]
    proj_row = None
    roots = []
    if project_id:
        proj_row = conn.execute("SELECT id, name, position FROM projects WHERE id = ?", (project_id,)).fetchone()
        roots = [
            r[0]
            for r in conn.execute(
                "SELECT path FROM project_roots WHERE project_id = ? ORDER BY position ASC", (project_id,)
            ).fetchall()
        ]
    conn.close()
    after_stat = snapshot_file_stat(state_db)

    rollout_path = pathlib.Path(trow[1])
    if not rollout_path.is_file():
        raise FileNotFoundError(f"Codex rollout file missing: {rollout_path}")

    session_meta = {}
    user_messages = []
    assistant_messages = []
    reasoning_items = []
    tool_calls = []
    tool_outputs = {}
    record_type_counts = {}

    with open(rollout_path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            obj = json.loads(line)
            rtype = obj.get("type", "unknown")
            record_type_counts[rtype] = record_type_counts.get(rtype, 0) + 1
            pl = obj.get("payload", {})
            if rtype == "session_meta" and isinstance(pl, dict):
                session_meta = {
                    "id": pl.get("id") or pl.get("session_id"),
                    "cwd": pl.get("cwd"),
                    "originator": pl.get("originator"),
                    "cli_version": pl.get("cli_version"),
                    "source": pl.get("source"),
                    "model_provider": pl.get("model_provider"),
                }
            elif rtype == "response_item" and isinstance(pl, dict):
                ptype = pl.get("type")
                if ptype == "message":
                    role = pl.get("role")
                    content = pl.get("content", [])
                    text_len = sum(len(c.get("text", "")) for c in content if isinstance(c, dict))
                    entry = {
                        "line": line_no,
                        "id": pl.get("id"),
                        "role": role,
                        "content_blocks": len(content),
                        "text_length": text_len,
                    }
                    if role == "user":
                        user_messages.append(entry)
                    elif role == "assistant":
                        assistant_messages.append(entry)
                elif ptype in ("reasoning", "magpie_reasoning"):
                    summary = pl.get("summary") or []
                    summary_len = sum(
                        len(s.get("text", "")) if isinstance(s, dict) else len(str(s)) for s in summary
                    )
                    reasoning_items.append({
                        "line": line_no,
                        "type": ptype,
                        "id": pl.get("id"),
                        "summary_blocks": len(summary),
                        "summary_text_len": summary_len,
                        "has_encrypted_content": bool(pl.get("encrypted_content")),
                    })
                elif ptype in ("function_call", "custom_tool_call", "tool_search_call"):
                    args_raw = pl.get("arguments") or pl.get("input") or ""
                    tool_calls.append({
                        "line": line_no,
                        "item_type": ptype,
                        "call_id": pl.get("call_id"),
                        "tool_name": pl.get("name", "tool_search"),
                        "namespace": pl.get("namespace"),
                        "arguments_len": len(str(args_raw)),
                    })
                elif ptype in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
                    cid = pl.get("call_id")
                    out_raw = pl.get("output") or pl.get("tools") or ""
                    tool_outputs[cid] = {
                        "line": line_no,
                        "item_type": ptype,
                        "call_id": cid,
                        "output_len": len(str(out_raw)),
                    }

    paired_tools = []
    for tc in tool_calls:
        cid = tc["call_id"]
        out_match = tool_outputs.get(cid)
        paired_tools.append({
            **tc,
            "paired_output": out_match is not None,
            "output_len": out_match["output_len"] if out_match else 0,
        })

    gs_data = json.loads(global_state_json.read_text(encoding="utf-8")) if global_state_json.is_file() else {}
    legacy_map = gs_data.get("app-server-project-id-by-legacy-project-id-by-host", {})

    raw_binding = {
        "project_id": project_id,
        "project_name": proj_row[1] if proj_row else None,
        "project_roots": roots,
        "cwd": trow[2],
        "archived": trow[7],
        "preview_non_empty": bool(trow[4] and trow[4].strip()),
        "global_state_legacy_bridge_hosts": len(legacy_map),
    }
    raw_binding.update(evaluate_codex_sidebar_visibility(raw_binding, sidebar_rules))

    preferred_sample_tool = next(
        (t for t in paired_tools if t["tool_name"] in ("exec_command", "exec", "apply_patch", "view_file")),
        paired_tools[0] if paired_tools else None,
    )

    return {
        "thread_id": thread_id,
        "rollout_path": str(rollout_path),
        "readonly_integrity": {
            "state_5_sqlite_unchanged": before_stat == after_stat,
        },
        "project_binding": raw_binding,
        "session_meta": session_meta,
        "counts": {
            "record_types": record_type_counts,
            "user_messages": len(user_messages),
            "assistant_messages": len(assistant_messages),
            "reasoning_items": len(reasoning_items),
            "tool_calls": len(tool_calls),
            "tool_outputs": len(tool_outputs),
            "unpaired_tool_calls": sum(1 for t in paired_tools if not t["paired_output"]),
        },
        "samples": {
            "first_user_message": user_messages[0] if user_messages else None,
            "first_reasoning_item": reasoning_items[0] if reasoning_items else None,
            "first_paired_tool_call": preferred_sample_tool,
            "first_assistant_message": assistant_messages[0] if assistant_messages else None,
        },
    }


def build_bidirectional_mapping_matrix() -> list:
    return [
        {
            "domain_concept": "Project & Sidebar Binding",
            "antigravity_2_30_fields": (
                "CortexTrajectoryMetadata.workspace_uris (field 7, repeated string 'file:///...') + "
                "CortexTrajectoryMetadata.project_id (field 18, UUID) + "
                "~/.gemini/config/projects/<uuid>.json (exa.project_pb.Project) + "
                "conversation_summaries.source == '' (enum 0/1/18/19/20) + "
                "CascadeTrajectorySummary.annotations.title (field 15 -> 1) + annotations/<id>.pbtxt"
            ),
            "codex_0_166_fields": (
                "threads.project_id (FK -> projects.id) + project_roots.path + threads.cwd + "
                "threads.archived == 0 + threads.preview <> '' (idx_threads_visible_recency_at_ms & idx_threads_project_recency) + "
                ".codex-global-state.json (local-projects, thread-project-assignments)"
            ),
        },
        {
            "domain_concept": "User Input & Attachments",
            "antigravity_2_30_fields": (
                "steps.step_type = 14 (CORTEX_STEP_TYPE_USER_INPUT), "
                "gemini_coder.Step.user_input (field 19 -> exa.cortex_pb.CortexStepUserInput): "
                "items (field 3, repeated TextOrScopeItem), user_response (field 2), media (field 9), client_message_id (field 19)"
            ),
            "codex_0_166_fields": (
                "rollout-*.jsonl response_item (payload.type = 'message', payload.role = 'user'): "
                "payload.id, payload.content[] (type = 'input_text' | 'input_image', text)"
            ),
        },
        {
            "domain_concept": "Assistant Response & Thinking Summary",
            "antigravity_2_30_fields": (
                "steps.step_type = 15 (CORTEX_STEP_TYPE_PLANNER_RESPONSE), "
                "gemini_coder.Step.planner_response (field 20 -> exa.cortex_pb.CortexStepPlannerResponse): "
                "response (field 1), thinking (field 3), raw_thinking (field 16), thinking_signature (field 14, bytes), "
                "thinking_duration (field 11), message_id (field 6), tool_calls (field 7, repeated ChatToolCall); "
                "plus step_type = 82 / Step.notify_user (field 94 -> CortexStepNotifyUser)"
            ),
            "codex_0_166_fields": (
                "rollout-*.jsonl response_item (payload.type = 'reasoning' | 'magpie_reasoning': "
                "payload.summary[].text, payload.encrypted_content) + "
                "response_item (payload.type = 'message', payload.role = 'assistant': "
                "payload.id, payload.phase, payload.content[].text)"
            ),
        },
        {
            "domain_concept": "Tool Call Invocation",
            "antigravity_2_30_fields": (
                "CortexStepPlannerResponse.tool_calls (field 7 -> exa.codeium_common_pb.ChatToolCall: "
                "id=1, name=2, arguments_json=3, thinking_signature=7) paired with "
                "steps.step_type = 132 (CORTEX_STEP_TYPE_GENERIC), "
                "CortexStepMetadata.tool_call (field 4 -> ChatToolCall) & tool_summary (field 30)"
            ),
            "codex_0_166_fields": (
                "rollout-*.jsonl response_item (payload.type = 'function_call' | 'custom_tool_call'): "
                "payload.call_id, payload.name, payload.namespace, payload.arguments (JSON) | payload.input (JS script)"
            ),
        },
        {
            "domain_concept": "Tool Execution Result",
            "antigravity_2_30_fields": (
                "steps.step_type = 132 (CORTEX_STEP_TYPE_GENERIC), "
                "gemini_coder.Step.generic (field 140 -> exa.cortex_pb.CortexStepGeneric): "
                "args (field 1, map<string,string>), result (field 2 -> exa.cortex_pb.GenericStepResult: "
                "result=1 text, payload=6 google.protobuf.Any -> exa.cortex.core.tools.{ReadFile,FileEdit,RunCommand,SearchWeb,...})"
            ),
            "codex_0_166_fields": (
                "rollout-*.jsonl response_item (payload.type = 'function_call_output' | 'custom_tool_call_output'): "
                "payload.call_id (matches function_call.call_id), payload.output (string or input_text array)"
            ),
        },
        {
            "domain_concept": "Turn / Checkpoint Boundary & Companion Logs",
            "antigravity_2_30_fields": (
                "steps.step_type = 23 (CORTEX_STEP_TYPE_CHECKPOINT), "
                "gemini_coder.Step.checkpoint (field 30 -> CortexStepCheckpoint: user_intent=4, session_summary=5, conversation_title=10) + "
                "Step.task_boundary (field 93 -> CortexStepTaskBoundary) + "
                "brain/<id>/.system_generated/logs/transcript.jsonl"
            ),
            "codex_0_166_fields": (
                "rollout-*.jsonl event_msg (payload.type = 'task_started' | 'item_completed' | 'task_complete', "
                "turn_id, root_turn_id, last_agent_message) + token_usage_record"
            ),
        },
    ]


def validate_negative_controls(schema: dict, sidebar_rules: dict, agy_report: dict, codex_report: dict) -> dict:
    # Control 1: Inject an unknown wire field (field_number = 9999, wire_type = 0, value = 42) into CortexTrajectoryMetadata
    unknown_tag = (9999 << 3) | 0
    bad_buf = bytearray()
    val = unknown_tag
    while val >= 0x80:
        bad_buf.append((val & 0x7F) | 0x80)
        val >>= 7
    bad_buf.append(val)
    bad_buf.append(42)

    unknowns = []
    decode_protobuf(bytes(bad_buf), "exa.cortex_pb.CortexTrajectoryMetadata", schema, unknowns)
    caught_unknown_wire = any("9999" in u for u in unknowns)

    # Control 2: Mutate the real decoded Antigravity session's project_binding to a CLI bucket / source 17 and verify evaluate_agy_sidebar_visibility rejects both
    mutated_agy_bucket = {**agy_report["project_binding"], "project_id": "default-cli-project"}
    mutated_agy_source = {**agy_report["project_binding"], "trajectory_source_enum": 17, "summary_source_col": "CORTEX_TRAJECTORY_SOURCE_CLI"}
    res_bucket = evaluate_agy_sidebar_visibility(mutated_agy_bucket, sidebar_rules)
    res_source = evaluate_agy_sidebar_visibility(mutated_agy_source, sidebar_rules)
    caught_forbidden_bucket = (
        res_bucket["is_valid_non_bucket_project"] is False
        and res_bucket["sidebar_visible_predicate_met"] is False
        and res_source["trajectory_source_visible"] is False
        and res_source["sidebar_visible_predicate_met"] is False
    )

    # Control 3: Mutate the real decoded Codex session's project_binding (empty preview or archived=1) and verify evaluate_codex_sidebar_visibility rejects both
    mutated_codex_preview = {**codex_report["project_binding"], "preview_non_empty": False}
    mutated_codex_archived = {**codex_report["project_binding"], "archived": 1}
    res_c_prev = evaluate_codex_sidebar_visibility(mutated_codex_preview, sidebar_rules)
    res_c_arch = evaluate_codex_sidebar_visibility(mutated_codex_archived, sidebar_rules)
    caught_empty_preview = (
        res_c_prev["preview_non_empty_ok"] is False
        and res_c_prev["sidebar_visible_predicate_met"] is False
        and res_c_arch["archived_ok"] is False
        and res_c_arch["sidebar_visible_predicate_met"] is False
    )

    if not (caught_unknown_wire and caught_forbidden_bucket and caught_empty_preview):
        raise RuntimeError("Negative control validation failed!")

    return {
        "unknown_wire_field_9999_caught": caught_unknown_wire,
        "forbidden_project_bucket_caught": caught_forbidden_bucket,
        "codex_empty_preview_caught": caught_empty_preview,
    }


def main():
    parser = argparse.ArgumentParser(description="Read-only verifier for Antigravity 2.30+ and Codex 0.166+ sessions")
    parser.add_argument("--agy-id", default=os.environ.get("AGY_CONVERSATION_ID", ""), help="Antigravity cascade_id")
    parser.add_argument("--codex-id", default=os.environ.get("CODEX_THREAD_ID", ""), help="Codex thread_id")
    parser.add_argument("--output", default="", help="Optional path to write JSON report")
    args = parser.parse_args()

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    schema_path = repo_root / "schema" / "antigravity_2_30_schema.json"
    rules_path = repo_root / "schema" / "sidebar_visibility_rules.json"
    if not schema_path.is_file() or not rules_path.is_file():
        raise FileNotFoundError("Schema files missing in schema/; run scripts/extract_schema.py first")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    sidebar_rules = json.loads(rules_path.read_text(encoding="utf-8"))

    home = pathlib.Path.home()
    agy_root = pathlib.Path(os.environ.get("ANTIGRAVITY_DATA_DIR", str(home / ".gemini" / "antigravity")))
    config_root = pathlib.Path(os.environ.get("GEMINI_CONFIG_DIR", str(home / ".gemini" / "config")))
    codex_root = pathlib.Path(os.environ.get("CODEX_HOME", str(home / ".codex")))

    agy_id = args.agy_id or select_default_agy_conversation(agy_root)
    agy_report = decode_antigravity_session(agy_root, config_root, agy_id, schema, sidebar_rules)

    preferred_cwd = ""
    uris = agy_report["project_binding"].get("workspace_uris", [])
    if uris and uris[0].startswith("file://"):
        preferred_cwd = uris[0][len("file://") :]

    codex_id = args.codex_id or select_default_codex_thread(codex_root, preferred_cwd)
    codex_report = decode_codex_session(codex_root, codex_id, sidebar_rules)

    negative_controls = validate_negative_controls(schema, sidebar_rules, agy_report, codex_report)

    if agy_report["counts"]["unknown_wire_fields_count"] != 0:
        raise RuntimeError(
            f"Lossless wire check failed: unknown wire fields found: {agy_report['unknown_wire_fields']}"
        )
    if not agy_report["readonly_integrity"]["conv_db_unchanged"] or not agy_report["readonly_integrity"]["summaries_db_unchanged"]:
        raise RuntimeError("Read-only violation detected on Antigravity SQLite files!")
    if not codex_report["readonly_integrity"]["state_5_sqlite_unchanged"]:
        raise RuntimeError("Read-only violation detected on Codex state_5.sqlite!")

    full_report = {
        "status": "VERIFIED",
        "schema_version": schema["schema_version"],
        "negative_controls": negative_controls,
        "antigravity_session": agy_report,
        "codex_session": codex_report,
        "canonical_tool_mapping": CANONICAL_TOOL_MAPPING,
        "bidirectional_field_mapping_matrix": build_bidirectional_mapping_matrix(),
    }

    if args.output:
        out_path = pathlib.Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(full_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps(full_report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
