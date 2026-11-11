#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import struct
import sys

_REPO_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from codex_agy_sync._wire import iter_wire_fields, read_varint

PROTO_TYPES = {
    1: "double",
    2: "float",
    3: "int64",
    4: "uint64",
    5: "int32",
    6: "fixed64",
    7: "fixed32",
    8: "bool",
    9: "string",
    10: "group",
    11: "message",
    12: "bytes",
    13: "uint32",
    14: "enum",
    15: "sfixed32",
    16: "sfixed64",
    17: "sint32",
    18: "sint64",
}

PROTO_LABELS = {
    1: "optional",
    2: "required",
    3: "repeated",
}

TARGET_DESCRIPTOR_FILES = [
    "third_party/gemini_coder/proto/trajectory.proto",
    "third_party/jetski/jetski_cortex_pb/jetski_cortex.proto",
    "third_party/jetski/cortex_pb/cortex.proto",
    "third_party/jetski/codeium_common_pb/codeium_common.proto",
    "third_party/jetski/language_server_pb/language_server.proto",
    "third_party/jetski/project_pb/project.proto",
    "third_party/jetski/jetbox_summaries_pb/jetbox_summaries.proto",
    "third_party/jetski/cortex/core/tools_pb/tools.proto",
    "third_party/jetski/diff_action_pb/diff_action.proto",
]

CORE_MESSAGES = {
    "gemini_coder": [
        "Conversation",
        "ConversationState",
        "Trajectory",
        "TaskDetails",
        "Step",
    ],
    "exa.jetski_cortex_pb": [
        "CascadeTrajectorySummary",
        "CascadeTrajectorySummaries",
        "ConversationAnnotations",
        "CortexTrajectoryStepWithIndex",
        "CascadeState",
        "AgentStateUpdate",
        "TrajectoryUpdate",
        "StepsUpdate",
    ],
    "exa.cortex_pb": [
        "CortexTrajectoryMetadata",
        "CortexWorkspaceMetadata",
        "CortexStepMetadata",
        "CortexStepUserInput",
        "CortexStepPlannerResponse",
        "CortexStepGeneric",
        "GenericStepResult",
        "CortexStepCheckpoint",
        "CortexStepSystemMessage",
        "CortexStepTaskBoundary",
        "CortexStepNotifyUser",
        "CortexStepRunCommand",
        "CortexStepViewFile",
        "CortexStepWriteToFile",
        "CortexStepCodeAction",
        "CortexStepMcpTool",
        "CortexStepInvokeSubagent",
        "StepRenderInfo",
        "CortexStepGeneratorMetadata",
        "ExecutorMetadata",
        "CortexErrorDetails",
        "SourceTrajectoryStepInfo",
        "CortexStepInternalMetadata",
        "DiffStats",
        "SubagentSpec",
        "SubagentResult",
    ],
    "exa.codeium_common_pb": [
        "ChatToolCall",
        "TextOrScopeItem",
        "ContextScopeItem",
        "Media",
        "ImageData",
        "ModelUsageStats",
        "ModelOrAlias",
        "ModelInfo",
        "ArtifactMetadata",
        "KnowledgeBaseItem",
    ],
    "exa.cortex.core.tools": [
        "ReadFile",
        "FileEdit",
        "RunCommand",
        "SearchWeb",
        "InvokeSubagent",
        "DefineSubagent",
        "ManageSubagents",
        "ManageTask",
        "SendMessage",
        "Schedule",
        "AskQuestion",
        "ListResources",
        "ReadResource",
    ],
    "exa.project_pb": [
        "Project",
        "Resources",
        "Resource",
        "GitFolder",
        "PermissionGrants",
        "ProjectSettings",
    ],
    "jetbox_summaries_pb": [
        "SummariesState",
    ],
    "exa.language_server_pb": [
        "GetCascadeTrajectoryRequest",
        "GetCascadeTrajectoryResponse",
        "GetCascadeTrajectoryStepsRequest",
        "GetCascadeTrajectoryStepsResponse",
        "SendUserCascadeMessageRequest",
        "StartCascadeRequest",
        "StartCascadeResponse",
        "UpdateConversationAnnotationsRequest",
        "CreateProjectRequest",
        "CreateProjectResponse",
        "ListProjectsRequest",
        "ListProjectsResponse",
        "ReadProjectsRequest",
        "ReadProjectsResponse",
    ],
}

CORE_ENUMS = {
    "exa.cortex_pb": [
        "CortexStepType",
        "CortexStepStatus",
        "CortexStepSource",
        "CortexTrajectorySource",
        "CortexTrajectoryType",
        "CascadeRunStatus",
    ],
    "exa.codeium_common_pb": [
        "StopReason",
        "Model",
    ],
    "exa.project_pb": [
        "BackendType",
    ],
}


def resolve_language_server_bin():
    env_bin = os.environ.get("ANTIGRAVITY_LANGUAGE_SERVER_BIN")
    if env_bin and os.path.isfile(env_bin):
        return pathlib.Path(env_bin)
    default_mac = pathlib.Path("/Applications/Antigravity.app/Contents/Resources/bin/language_server")
    if default_mac.is_file():
        return default_mac
    raise FileNotFoundError("Cannot locate Antigravity language_server binary")


def resolve_agy_bin():
    env_bin = os.environ.get("ANTIGRAVITY_CLI_BIN")
    if env_bin and os.path.isfile(env_bin):
        return pathlib.Path(env_bin)
    which_bin = shutil.which("agy")
    if which_bin and os.path.isfile(which_bin):
        return pathlib.Path(which_bin).resolve()
    home = pathlib.Path.home()
    for rel in [".local/bin/agy", ".local/libexec/agy/agy"]:
        cand = home / rel
        if cand.is_file():
            return cand
    raise FileNotFoundError("Cannot locate standalone agy CLI binary")


def resolve_asar_path():
    env_asar = os.environ.get("ANTIGRAVITY_APP_ASAR")
    if env_asar and os.path.isfile(env_asar):
        return pathlib.Path(env_asar)
    default_asar = pathlib.Path("/Applications/Antigravity.app/Contents/Resources/app.asar")
    if default_asar.is_file():
        return default_asar
    raise FileNotFoundError("Cannot locate Antigravity app.asar")


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def walk_fdp_length(buf: bytes, start: int) -> int:
    pos = start
    n = len(buf)
    while pos < n:
        try:
            tag, next_pos = read_varint(buf, pos)
        except (IndexError, ValueError):
            break
        fn = tag >> 3
        wt = tag & 7
        if fn not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14) or wt not in (0, 2):
            break
        if wt == 0:
            try:
                _, next_pos = read_varint(buf, next_pos)
            except (IndexError, ValueError):
                break
        else:
            try:
                ln, next_pos = read_varint(buf, next_pos)
            except (IndexError, ValueError):
                break
            if ln > 2_000_000 or next_pos + ln > n:
                break
            if fn == 1 and pos != start:
                break
            next_pos += ln
        pos = next_pos
    slice_bytes = buf[start:pos]
    for marker in (b"b\x06proto3", b"b\x06proto2"):
        idx = slice_bytes.rfind(marker)
        if idx != -1 and len(slice_bytes) - (idx + len(marker)) <= 16:
            return idx + len(marker)
    return pos - start


def parse_wire_message(buf: bytes):
    out = {}
    for fn, wt, ival, raw in iter_wire_fields(buf):
        out.setdefault(fn, []).append(ival if wt == 0 else raw)
    return out


def parse_enum_descriptor(buf: bytes):
    m = parse_wire_message(buf)
    name = m.get(1, [b""])[0].decode("utf-8", errors="ignore")
    values = {}
    values_by_name = {}
    for vbuf in m.get(2, []):
        vm = parse_wire_message(vbuf)
        vname = vm.get(1, [b""])[0].decode("utf-8", errors="ignore")
        vnum = vm.get(2, [0])[0]
        if vnum >= (1 << 31):
            vnum -= 1 << 64
        values[str(vnum)] = vname
        values_by_name[vname] = vnum
    return {
        "name": name,
        "values": values,
        "values_by_name": values_by_name,
    }


def parse_message_descriptor(buf: bytes, pkg_prefix: str):
    m = parse_wire_message(buf)
    name = m.get(1, [b""])[0].decode("utf-8", errors="ignore")
    full_name = f"{pkg_prefix}.{name}" if pkg_prefix else name
    oneofs = [
        parse_wire_message(ob).get(1, [b""])[0].decode("utf-8", errors="ignore")
        for ob in m.get(8, [])
    ]
    options_msg = parse_wire_message(m[7][0]) if 7 in m else {}
    is_map_entry = bool(options_msg.get(7, [0])[0]) if 7 in options_msg else False

    fields_by_number = {}
    fields_by_name = {}
    for fbuf in m.get(2, []):
        fm = parse_wire_message(fbuf)
        fname = fm.get(1, [b""])[0].decode("utf-8", errors="ignore")
        fnum = fm.get(3, [0])[0]
        flabel = fm.get(4, [1])[0]
        ftype = fm.get(5, [0])[0]
        ftypename = fm.get(6, [b""])[0].decode("utf-8", errors="ignore")
        json_name = fm.get(10, [b""])[0].decode("utf-8", errors="ignore")
        oneof_idx = fm.get(9, [None])[0]
        proto3_opt = bool(fm.get(17, [0])[0])
        field_info = {
            "number": fnum,
            "name": fname,
            "label": PROTO_LABELS.get(flabel, "optional"),
            "type": PROTO_TYPES.get(ftype, f"unknown_{ftype}"),
            "type_id": ftype,
            "type_name": ftypename.lstrip(".") if ftypename else None,
            "json_name": json_name,
            "oneof": oneofs[oneof_idx] if (oneof_idx is not None and oneof_idx < len(oneofs)) else None,
            "proto3_optional": proto3_opt,
        }
        fields_by_number[str(fnum)] = field_info
        fields_by_name[fname] = field_info

    nested_messages = {}
    for nbuf in m.get(3, []):
        nd = parse_message_descriptor(nbuf, full_name)
        nested_messages[nd["full_name"]] = nd

    nested_enums = {}
    for ebuf in m.get(4, []):
        ed = parse_enum_descriptor(ebuf)
        ed["full_name"] = f"{full_name}.{ed['name']}"
        nested_enums[ed["full_name"]] = ed

    return {
        "name": name,
        "full_name": full_name,
        "is_map_entry": is_map_entry,
        "oneofs": oneofs,
        "fields": fields_by_number,
        "fields_by_name": fields_by_name,
        "nested_messages": nested_messages,
        "nested_enums": nested_enums,
    }


def extract_file_descriptors(binary_bytes: bytes, target_files):
    extracted = {}
    for target in target_files:
        header = b"\x0a" + bytes([len(target)]) + target.encode("ascii") + b"\x12"
        pos = binary_bytes.find(header)
        if pos == -1:
            raise RuntimeError(f"Descriptor header for {target} not found in binary")
        ln = walk_fdp_length(binary_bytes, pos)
        raw = binary_bytes[pos : pos + ln]
        fdp = parse_wire_message(raw)
        fname = fdp[1][0].decode("utf-8")
        pkg = fdp.get(2, [b""])[0].decode("utf-8")
        syntax = fdp.get(12, [b"proto2"])[0].decode("utf-8")
        deps = [d.decode("utf-8") for d in fdp.get(3, [])]

        messages = {}
        enums = {}
        for mbuf in fdp.get(4, []):
            md = parse_message_descriptor(mbuf, pkg)
            messages[md["full_name"]] = md
            for k, v in md["nested_messages"].items():
                messages[k] = v
            for k, v in md["nested_enums"].items():
                enums[k] = v
        for ebuf in fdp.get(5, []):
            ed = parse_enum_descriptor(ebuf)
            ed["full_name"] = f"{pkg}.{ed['name']}" if pkg else ed["name"]
            enums[ed["full_name"]] = ed

        services = {}
        for sbuf in fdp.get(6, []):
            sm = parse_wire_message(sbuf)
            sname = sm.get(1, [b""])[0].decode("utf-8")
            methods = {}
            for mth_buf in sm.get(2, []):
                mm = parse_wire_message(mth_buf)
                mname = mm.get(1, [b""])[0].decode("utf-8")
                in_type = mm.get(2, [b""])[0].decode("utf-8").lstrip(".")
                out_type = mm.get(3, [b""])[0].decode("utf-8").lstrip(".")
                client_stream = bool(mm.get(5, [0])[0])
                server_stream = bool(mm.get(6, [0])[0])
                methods[mname] = {
                    "input_type": in_type,
                    "output_type": out_type,
                    "client_streaming": client_stream,
                    "server_streaming": server_stream,
                }
            services[f"{pkg}.{sname}" if pkg else sname] = methods

        extracted[fname] = {
            "file": fname,
            "package": pkg,
            "syntax": syntax,
            "offset": pos,
            "byte_length": ln,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "dependencies": deps,
            "messages": messages,
            "enums": enums,
            "services": services,
        }
    return extracted


def merge_and_diff_descriptors(ls_d: dict, agy_d: dict):
    merged_messages = {}
    merged_enums = {}
    merged_services = {}
    drift = {
        "messages_only_in_language_server": [],
        "messages_only_in_agy": [],
        "field_differences": {},
        "enum_differences": {},
        "rpc_only_in_language_server": [],
        "rpc_only_in_agy": [],
    }

    all_msg_names = sorted(set(ls_d["messages"].keys()) | set(agy_d["messages"].keys()))
    for mname in all_msg_names:
        in_ls = mname in ls_d["messages"]
        in_agy = mname in agy_d["messages"]
        if in_ls and not in_agy:
            drift["messages_only_in_language_server"].append(mname)
            merged_messages[mname] = ls_d["messages"][mname]
        elif in_agy and not in_ls:
            drift["messages_only_in_agy"].append(mname)
            merged_messages[mname] = agy_d["messages"][mname]
        else:
            lm = ls_d["messages"][mname]
            am = agy_d["messages"][mname]
            merged_fields = dict(lm["fields"])
            ls_fnums = set(lm["fields"].keys())
            agy_fnums = set(am["fields"].keys())
            only_ls_f = sorted(
                [f"{k}:{lm['fields'][k]['name']}" for k in (ls_fnums - agy_fnums)],
                key=lambda s: int(s.split(":")[0]),
            )
            only_agy_f = sorted(
                [f"{k}:{am['fields'][k]['name']}" for k in (agy_fnums - ls_fnums)],
                key=lambda s: int(s.split(":")[0]),
            )
            for k in agy_fnums - ls_fnums:
                merged_fields[k] = am["fields"][k]
            if only_ls_f or only_agy_f:
                drift["field_differences"][mname] = {
                    "only_in_language_server": only_ls_f,
                    "only_in_agy": only_agy_f,
                }
            merged_oneofs = list(dict.fromkeys(lm["oneofs"] + am["oneofs"]))
            merged_messages[mname] = {
                **lm,
                "oneofs": merged_oneofs,
                "fields": merged_fields,
            }

    all_enum_names = sorted(set(ls_d["enums"].keys()) | set(agy_d["enums"].keys()))
    for ename in all_enum_names:
        in_ls = ename in ls_d["enums"]
        in_agy = ename in agy_d["enums"]
        if in_ls and not in_agy:
            merged_enums[ename] = ls_d["enums"][ename]
        elif in_agy and not in_ls:
            merged_enums[ename] = agy_d["enums"][ename]
        else:
            le = ls_d["enums"][ename]
            ae = agy_d["enums"][ename]
            merged_vals = dict(le["values"])
            merged_by_name = dict(le["values_by_name"])
            ls_v = set(le["values"].keys())
            agy_v = set(ae["values"].keys())
            only_ls_v = sorted([f"{k}:{le['values'][k]}" for k in (ls_v - agy_v)], key=lambda s: int(s.split(":")[0]))
            only_agy_v = sorted([f"{k}:{ae['values'][k]}" for k in (agy_v - ls_v)], key=lambda s: int(s.split(":")[0]))
            for k in agy_v - ls_v:
                vname = ae["values"][k]
                merged_vals[k] = vname
                merged_by_name[vname] = int(k)
            if only_ls_v or only_agy_v:
                drift["enum_differences"][ename] = {
                    "only_in_language_server": only_ls_v,
                    "only_in_agy": only_agy_v,
                }
            merged_enums[ename] = {
                **le,
                "values": merged_vals,
                "values_by_name": merged_by_name,
            }

    all_svc_names = sorted(set(ls_d["services"].keys()) | set(agy_d["services"].keys()))
    for sname in all_svc_names:
        ls_m = ls_d["services"].get(sname, {})
        agy_m = agy_d["services"].get(sname, {})
        merged_m = dict(ls_m)
        for k, v in agy_m.items():
            if k not in merged_m:
                merged_m[k] = v
        merged_services[sname] = merged_m
        only_ls_rpc = sorted(set(ls_m.keys()) - set(agy_m.keys()))
        only_agy_rpc = sorted(set(agy_m.keys()) - set(ls_m.keys()))
        if only_ls_rpc:
            drift["rpc_only_in_language_server"].extend([f"{sname}/{m}" for m in only_ls_rpc])
        if only_agy_rpc:
            drift["rpc_only_in_agy"].extend([f"{sname}/{m}" for m in only_agy_rpc])

    return merged_messages, merged_enums, merged_services, drift


def render_proto_summary(schema_bundle: dict) -> str:
    lines = [
        "// Auto-generated by scripts/extract_schema.py from compiled FileDescriptorProto in Antigravity 2.30+ binaries.",
        "// DO NOT EDIT BY HAND. Re-run `python3 scripts/extract_schema.py` to regenerate.",
        'syntax = "proto3";',
        "",
    ]
    all_messages = schema_bundle["messages"]
    all_enums = schema_bundle["enums"]

    for pkg, enum_names in CORE_ENUMS.items():
        lines.append(f"// === Package: {pkg} (Enums) ===")
        for ename in enum_names:
            full_ename = f"{pkg}.{ename}"
            ed = all_enums.get(full_ename)
            if not ed:
                continue
            lines.append(f"enum {full_ename.replace('.', '_')} {{ // {full_ename}")
            for num_str, vname in sorted(ed["values"].items(), key=lambda kv: int(kv[0])):
                lines.append(f"  {vname} = {num_str};")
            lines.append("}")
            lines.append("")

    for pkg, msg_names in CORE_MESSAGES.items():
        lines.append(f"// === Package: {pkg} (Messages) ===")
        for mname in msg_names:
            full_mname = f"{pkg}.{mname}"
            md = all_messages.get(full_mname)
            if not md:
                continue
            lines.append(f"message {full_mname.replace('.', '_')} {{ // {full_mname}")
            for num_str, f in sorted(md["fields"].items(), key=lambda kv: int(kv[0])):
                t_str = f["type_name"] if f["type_name"] else f["type"]
                rep = "repeated " if f["label"] == "repeated" else ("optional " if f["proto3_optional"] else "")
                oneof_comment = f" // oneof {f['oneof']}" if f["oneof"] else ""
                lines.append(f"  {rep}{t_str} {f['name']} = {num_str};{oneof_comment}")
            lines.append("}")
            lines.append("")
    return "\n".join(lines)


def inspect_asar_metadata(asar_path: pathlib.Path) -> dict:
    with open(asar_path, "rb") as f:
        _, header_size, _, str_len = struct.unpack("<IIII", f.read(16))
        tree = json.loads(f.read(str_len).decode("utf-8"))
        data_offset = 8 + header_size

        def read_member(rel_path: str) -> str:
            node = tree
            for part in rel_path.split("/"):
                node = node["files"][part]
            f.seek(data_offset + int(node["offset"]))
            return f.read(int(node["size"])).decode("utf-8", errors="replace")

        ls_js = read_member("dist/languageServer.js")
        paths_js = read_member("dist/paths.js")

    flags = re.findall(r"'(--[a-z0-9_]+)'", ls_js)
    return {
        "asar_sha256": sha256_file(asar_path),
        "language_server_spawn_flags": sorted(set(flags)),
        "uses_hub_subclient": "'hub'" in ls_js and "'--subclient_type'" in ls_js,
        "app_data_dir_fn_present": "getAppDataDir" in paths_js,
        "settings_pb_path_fn_present": "getSettingsPbPath" in paths_js,
    }


def extract_sqlite_schemas() -> dict:
    home = pathlib.Path.home()
    out = {}
    agy_sum_db = home / ".gemini" / "antigravity" / "conversation_summaries.db"
    if agy_sum_db.is_file():
        conn = sqlite3.connect(f"file:{agy_sum_db}?mode=ro", uri=True)
        cols = [
            {"cid": r[0], "name": r[1], "type": r[2], "notnull": bool(r[3]), "default": r[4], "pk": bool(r[5])}
            for r in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
        ]
        conn.close()
        out["antigravity_conversation_summaries_db"] = {
            "table": "conversation_summaries",
            "column_count": len(cols),
            "columns": cols,
        }

    codex_db = home / ".codex" / "state_5.sqlite"
    if codex_db.is_file():
        conn = sqlite3.connect(f"file:{codex_db}?mode=ro", uri=True)
        tables = {}
        for tbl in ["threads", "projects", "project_roots", "thread_sections"]:
            cols = [
                {"cid": r[0], "name": r[1], "type": r[2], "notnull": bool(r[3]), "default": r[4], "pk": bool(r[5])}
                for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            ]
            idxs = [
                {"name": r[0], "sql": r[1]}
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY name",
                    (tbl,),
                ).fetchall()
            ]
            tables[tbl] = {"column_count": len(cols), "columns": cols, "indexes": idxs}
        conn.close()
        out["codex_state_5_sqlite"] = tables
    return out


def main():
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    schema_dir = repo_root / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)

    ls_path = resolve_language_server_bin()
    agy_path = resolve_agy_bin()
    asar_path = resolve_asar_path()

    ls_bytes = ls_path.read_bytes()
    agy_bytes = agy_path.read_bytes()

    ls_descriptors = extract_file_descriptors(ls_bytes, TARGET_DESCRIPTOR_FILES)
    agy_descriptors = extract_file_descriptors(agy_bytes, TARGET_DESCRIPTOR_FILES)

    merged_messages = {}
    merged_enums = {}
    merged_services = {}
    descriptor_inventory = []
    binary_drift_summary = {}

    for fname in TARGET_DESCRIPTOR_FILES:
        ls_d = ls_descriptors[fname]
        agy_d = agy_descriptors[fname]
        f_msgs, f_enums, f_svcs, f_drift = merge_and_diff_descriptors(ls_d, agy_d)
        has_drift = any(bool(v) for v in f_drift.values())
        if has_drift:
            binary_drift_summary[fname] = f_drift
        descriptor_inventory.append({
            "file": fname,
            "package": ls_d["package"],
            "syntax": ls_d["syntax"],
            "language_server_offset": ls_d["offset"],
            "language_server_byte_length": ls_d["byte_length"],
            "language_server_sha256": ls_d["sha256"],
            "agy_offset": agy_d["offset"],
            "agy_byte_length": agy_d["byte_length"],
            "agy_sha256": agy_d["sha256"],
            "union_message_count": len(f_msgs),
            "union_enum_count": len(f_enums),
            "has_ls_vs_agy_drift": has_drift,
        })
        merged_messages.update(f_msgs)
        merged_enums.update(f_enums)
        merged_services.update(f_svcs)

    well_known = {
        "google.protobuf.Timestamp": {
            "name": "Timestamp",
            "full_name": "google.protobuf.Timestamp",
            "is_map_entry": False,
            "oneofs": [],
            "fields": {
                "1": {"number": 1, "name": "seconds", "label": "optional", "type": "int64", "type_id": 3, "type_name": None, "json_name": "seconds", "oneof": None, "proto3_optional": False},
                "2": {"number": 2, "name": "nanos", "label": "optional", "type": "int32", "type_id": 5, "type_name": None, "json_name": "nanos", "oneof": None, "proto3_optional": False},
            },
            "fields_by_name": {},
        },
        "google.protobuf.Duration": {
            "name": "Duration",
            "full_name": "google.protobuf.Duration",
            "is_map_entry": False,
            "oneofs": [],
            "fields": {
                "1": {"number": 1, "name": "seconds", "label": "optional", "type": "int64", "type_id": 3, "type_name": None, "json_name": "seconds", "oneof": None, "proto3_optional": False},
                "2": {"number": 2, "name": "nanos", "label": "optional", "type": "int32", "type_id": 5, "type_name": None, "json_name": "nanos", "oneof": None, "proto3_optional": False},
            },
            "fields_by_name": {},
        },
        "google.protobuf.Any": {
            "name": "Any",
            "full_name": "google.protobuf.Any",
            "is_map_entry": False,
            "oneofs": [],
            "fields": {
                "1": {"number": 1, "name": "type_url", "label": "optional", "type": "string", "type_id": 9, "type_name": None, "json_name": "typeUrl", "oneof": None, "proto3_optional": False},
                "2": {"number": 2, "name": "value", "label": "optional", "type": "bytes", "type_id": 12, "type_name": None, "json_name": "value", "oneof": None, "proto3_optional": False},
            },
            "fields_by_name": {},
        },
    }
    merged_messages.update(well_known)

    reachable_msgs = {}
    queue = []
    for pkg, names in CORE_MESSAGES.items():
        for n in names:
            full = f"{pkg}.{n}"
            if full in merged_messages:
                queue.append(full)
    for fname in [
        "third_party/gemini_coder/proto/trajectory.proto",
        "third_party/jetski/jetski_cortex_pb/jetski_cortex.proto",
        "third_party/jetski/cortex/core/tools_pb/tools.proto",
        "third_party/jetski/project_pb/project.proto",
        "third_party/jetski/jetbox_summaries_pb/jetbox_summaries.proto",
    ]:
        for m in ls_descriptors[fname]["messages"]:
            queue.append(m)
        for m in agy_descriptors[fname]["messages"]:
            queue.append(m)

    visited = set()
    while queue:
        curr = queue.pop(0)
        if curr in visited or curr not in merged_messages:
            continue
        visited.add(curr)
        md = merged_messages[curr]
        reachable_msgs[curr] = {
            "name": md["name"],
            "full_name": md["full_name"],
            "is_map_entry": md["is_map_entry"],
            "oneofs": md["oneofs"],
            "fields": md["fields"],
        }
        for f in md["fields"].values():
            tn = f.get("type_name")
            if tn and tn in merged_messages and tn not in visited:
                queue.append(tn)

    reachable_enums = {}
    for pkg, enames in CORE_ENUMS.items():
        for en in enames:
            full = f"{pkg}.{en}"
            if full in merged_enums:
                reachable_enums[full] = merged_enums[full]
    for md in reachable_msgs.values():
        for f in md["fields"].values():
            tn = f.get("type_name")
            if tn and tn in merged_enums:
                reachable_enums[tn] = merged_enums[tn]

    schema_bundle = {
        "schema_version": "antigravity-2.30-codex-0.166-v1",
        "binaries": {
            "language_server": {
                "filename": ls_path.name,
                "sha256": sha256_file(ls_path),
                "size_bytes": ls_path.stat().st_size,
            },
            "agy": {
                "filename": agy_path.name,
                "sha256": sha256_file(agy_path),
                "size_bytes": agy_path.stat().st_size,
            },
        },
        "descriptors": descriptor_inventory,
        "language_server_vs_agy_drift": binary_drift_summary,
        "sqlite_table_mappings": {
            "conversations_db": {
                "pragma_user_version": 1,
                "tables": {
                    "trajectory_meta": "columns: trajectory_id (TEXT PK), cascade_id (TEXT), trajectory_type (INT -> exa.cortex_pb.CortexTrajectoryType), source (INT -> exa.cortex_pb.CortexTrajectorySource)",
                    "steps": "columns: idx (INT PK), step_type (INT -> exa.cortex_pb.CortexStepType), status (INT -> exa.cortex_pb.CortexStepStatus), has_subtrajectory (BOOL), metadata (BLOB -> exa.cortex_pb.CortexStepMetadata), error_details (BLOB -> exa.cortex_pb.CortexErrorDetails), permissions (BLOB -> exa.cortex_pb.TrajectoryPermissions), task_details (BLOB -> gemini_coder.TaskDetails), render_info (BLOB -> exa.cortex_pb.StepRenderInfo), step_payload (BLOB -> gemini_coder.Step), step_format (INT)",
                    "trajectory_metadata_blob": "columns: id (TEXT PK = 'main'), data (BLOB -> exa.cortex_pb.CortexTrajectoryMetadata)",
                    "gen_metadata": "columns: idx (INT PK), data (BLOB -> exa.cortex_pb.CortexStepGeneratorMetadata), size (INT)",
                    "executor_metadata": "columns: idx (INT PK), data (BLOB -> exa.cortex_pb.ExecutorMetadata)",
                    "parent_references": "columns: idx (INT PK), data (BLOB -> exa.cortex_pb.CortexTrajectoryReference)",
                    "battle_mode_infos": "columns: idx (INT PK), data (BLOB -> exa.cortex_pb.BattleModeInfo)",
                },
            },
            "conversation_summaries_db": {
                "table": "conversation_summaries",
                "raw_summary_protobuf_type": "exa.jetski_cortex_pb.CascadeTrajectorySummary",
            },
            "agyhub_summaries_proto_pb": {
                "protobuf_type": "jetbox_summaries_pb.SummariesState",
                "entry_value_protobuf_type": "exa.jetski_cortex_pb.CascadeTrajectorySummary",
            },
            "annotations_pbtxt": {
                "protobuf_type": "exa.jetski_cortex_pb.ConversationAnnotations",
            },
        },
        "messages": reachable_msgs,
        "enums": reachable_enums,
        "language_server_rpc_methods": merged_services.get("exa.language_server_pb.LanguageServerService", {}),
    }

    schema_json_path = schema_dir / "antigravity_2_30_schema.json"
    schema_json_path.write_text(json.dumps(schema_bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    proto_path = schema_dir / "antigravity_2_30.proto"
    proto_path.write_text(render_proto_summary(schema_bundle) + "\n", encoding="utf-8")

    traj_source_enum = reachable_enums.get("exa.cortex_pb.CortexTrajectorySource", {}).get("values", {})
    visible_enum_keys = ["0", "1", "18", "19", "20"]
    visible_sources = {k: traj_source_enum.get(k, f"UNKNOWN_{k}") for k in visible_enum_keys if k in traj_source_enum}

    sidebar_rules = {
        "schema_version": "sidebar-visibility-rules-v1",
        "asar_evidence": inspect_asar_metadata(asar_path),
        "sqlite_schemas": extract_sqlite_schemas(),
        "antigravity_desktop_2_30": {
            "visible_trajectory_sources_enum": visible_sources,
            "hidden_external_trajectory_sources_enum": {
                "17": traj_source_enum.get("17", "CORTEX_TRAJECTORY_SOURCE_CLI"),
            },
            "conversation_summaries_db_source_column": {
                "desktop_visible_values": [""] + [v for k, v in visible_sources.items() if k != "0"],
                "cli_hidden_values": ["CORTEX_TRAJECTORY_SOURCE_CLI"],
            },
            "forbidden_project_id_buckets": [
                "",
                "default-cli-project",
                "outside-of-project",
                "null",
                "none",
            ],
            "required_project_binding": {
                "project_config_glob": "~/.gemini/config/projects/<project_id>.json",
                "project_proto_type": "exa.project_pb.Project",
                "trajectory_metadata_fields": {
                    "workspace_uris": {"field_number": 7, "format": "file:///abs/path"},
                    "nesting_depth": {"field_number": 17, "root_value": 0},
                    "project_id": {"field_number": 18, "format": "UUID matching ~/.gemini/config/projects/<project_id>.json"},
                },
                "title_resolution_order": [
                    "~/.gemini/antigravity/annotations/<cascade_id>.pbtxt (ConversationAnnotations.title, field 1)",
                    "CascadeTrajectorySummary.annotations.title (field 15 -> field 1)",
                    "conversation_summaries.title column",
                ],
                "cli_resume_prerequisite": "~/.gemini/{antigravity,antigravity-cli}/brain/<cascade_id>/.system_generated/logs/",
            },
        },
        "codex_desktop_0_166": {
            "required_threads_predicate": {
                "archived": 0,
                "preview_non_empty": True,
                "project_id_not_null": True,
                "sql_index_evidence": [
                    "idx_threads_visible_recency_at_ms: ON threads(archived, recency_at_ms DESC, id DESC) WHERE preview <> ''",
                    "idx_threads_project_recency: ON threads(project_id, recency_at_ms DESC) WHERE archived = 0 AND project_id IS NOT NULL",
                ],
            },
            "project_binding_sources": [
                "~/.codex/state_5.sqlite (projects.id + project_roots.path)",
                "~/.codex/.codex-global-state.json (local-projects, project-order, thread-project-assignments, app-server-project-id-by-legacy-project-id-by-host)",
            ],
        },
    }
    rules_path = schema_dir / "sidebar_visibility_rules.json"
    rules_path.write_text(json.dumps(sidebar_rules, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "ok",
                "descriptors_extracted": len(descriptor_inventory),
                "descriptors_with_ls_vs_agy_drift": len(binary_drift_summary),
                "reachable_messages": len(reachable_msgs),
                "reachable_enums": len(reachable_enums),
                "rpc_methods": len(schema_bundle["language_server_rpc_methods"]),
                "outputs": [
                    str(schema_json_path.relative_to(repo_root)),
                    str(proto_path.relative_to(repo_root)),
                    str(rules_path.relative_to(repo_root)),
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
