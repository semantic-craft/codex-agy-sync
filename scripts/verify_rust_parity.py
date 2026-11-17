#!/usr/bin/env python3
"""Differential parity verification harness: Python `./bin/codex-agy-sync` vs Rust `codex-agy-sync`."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from codex_agy_sync._wire import encode_bytes_field, encode_string_field, encode_varint_field, iter_wire_fields  # noqa: E402
from codex_agy_sync.codecs import (  # noqa: E402
    compress_zstd_rollout_bytes,
    encode_agy_trajectory,
    encode_cascade_summary_proto,
    encode_trajectory_metadata_proto,
)
from codex_agy_sync.codecs.steps import bind_agy_step  # noqa: E402
from codex_agy_sync.events import (  # noqa: E402
    AgyGuiSafeSource,
    AssistantTextEvent,
    CascadeId,
    EventOriginAnchor,
    HostKind,
    StepIndex,
    SubagentLineage,
    TurnId,
    UserInputEvent,
)
from codex_agy_sync.ledger import TrajectoryLedger  # noqa: E402
from codex_agy_sync.bridge.store_catalog import (  # noqa: E402
    ensure_agy_conversation_db_schema,
    ensure_agy_summaries_db_schema,
    ensure_codex_state_db_schema,
)
from codex_agy_sync.workspace import AgyProjectId, parse_workspace_identity  # noqa: E402


FIXTURES_DIR = REPO_ROOT / "rust" / "fixtures"
PYTHON_BIN = REPO_ROOT / "bin" / "codex-agy-sync"
CARGO_MANIFEST = REPO_ROOT / "rust" / "Cargo.toml"
CARGO_TARGET = REPO_ROOT / "out" / "rust-target"
RUST_BIN = CARGO_TARGET / "debug" / "codex-agy-sync"


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str

    def stdout_json(self) -> Any:
        return json.loads(self.stdout)

    def stderr_json(self) -> Any:
        return json.loads(self.stderr)


def build_rust_binary() -> None:
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(CARGO_TARGET)
    proc = subprocess.run(
        ["cargo", "build", "--manifest-path", str(CARGO_MANIFEST), "-p", "cas-cli"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Rust build failed:\n{proc.stdout}\n{proc.stderr}")
    if not RUST_BIN.is_file():
        raise FileNotFoundError(f"Expected built binary missing: {RUST_BIN}")


def run_cli(
    bin_path: Path,
    sandbox: dict[str, Path],
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> CmdResult:
    env = os.environ.copy()
    env["HOME"] = str(sandbox["home"])
    env["GEMINI_CONFIG_DIR"] = str(sandbox["gemini_config"])
    env["ANTIGRAVITY_DATA_DIR"] = str(sandbox["agy_data"])
    env["ANTIGRAVITY_CLI_DATA_DIR"] = str(sandbox["agy_cli"])
    env["CODEX_HOME"] = str(sandbox["codex_home"])
    env["CODEX_AGY_SYNC_STATE_DIR"] = str(sandbox["sync_state"])
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [
            str(bin_path),
            "--gemini-config-dir",
            str(sandbox["gemini_config"]),
            "--agy-data-dir",
            str(sandbox["agy_data"]),
            "--codex-home-dir",
            str(sandbox["codex_home"]),
            "--sync-state-dir",
            str(sandbox["sync_state"]),
            *args,
        ],
        cwd=str(sandbox["workspace"]),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return CmdResult(proc.returncode, proc.stdout, proc.stderr)


def _create_agy_db_fixture(
    sandbox: dict[str, Path],
    manifest: dict[str, Any],
    cid_str: str,
    title: str,
    user_text: str,
    assistant_text: str,
    *,
    parent_cid: str = "",
    nesting_depth: int = 0,
    agent_type: str = "",
    agent_role: str = "",
) -> None:
    ws_path, ws_uri = parse_workspace_identity(str(sandbox["workspace"]))
    t1 = TurnId("turn-1")
    events = [
        UserInputEvent(
            kind="user_input",
            anchor=EventOriginAnchor(HostKind.ANTIGRAVITY, t1, "2026-11-17T08:20:00Z"),
            query_text=user_text,
        ),
        AssistantTextEvent(
            kind="assistant_text",
            anchor=EventOriginAnchor(HostKind.ANTIGRAVITY, t1, "2026-11-17T08:20:05Z"),
            text=assistant_text,
        ),
    ]
    ledger = TrajectoryLedger.from_events(ws_path, ws_uri, events, explicit_title_override=title)
    cid = CascadeId(cid_str)
    agy_pid = AgyProjectId(manifest["agy_project_id"])
    lineage = SubagentLineage(
        parent_cascade_id=CascadeId(parent_cid) if parent_cid else None,
        root_cascade_id=CascadeId(parent_cid) if parent_cid else cid,
        nesting_depth=nesting_depth,
        agent_type_name=agent_type,
        agent_role=agent_role,
    )

    conv_db = sandbox["agy_data"] / "conversations" / f"{cid_str}.db"
    conv_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(conv_db))
    ensure_agy_conversation_db_schema(conn)
    traj_id = f"traj-{cid_str[:8]}"
    rows = encode_agy_trajectory(ledger, starting_step_idx=StepIndex(0))
    for r in rows:
        b = bind_agy_step(r, traj_id, cid_str)
        conn.execute(
            "INSERT OR REPLACE INTO steps (idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(b.idx), b.step_type, b.status, int(b.has_subtrajectory), b.metadata_blob, b.step_payload_blob, b.step_format),
        )
    conn.execute(
        "INSERT OR REPLACE INTO trajectory_meta (trajectory_id, cascade_id, trajectory_type, source) VALUES (?, ?, 4, 1)",
        (traj_id, cid_str),
    )
    tmeta = encode_trajectory_metadata_proto(
        project_id=agy_pid,
        workspace_uris=(ws_uri,),
        created_at_iso="2026-11-17T08:20:00Z",
        nesting_depth=nesting_depth,
        parent_conversation_id=parent_cid,
        root_conversation_id=parent_cid or cid_str,
        agent_type_name=agent_type,
        agent_role=agent_role,
    )
    conn.execute("INSERT OR REPLACE INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)", (tmeta,))
    conn.commit()
    conn.close()

    annot = sandbox["agy_data"] / "annotations" / f"{cid_str}.pbtxt"
    annot.parent.mkdir(parents=True, exist_ok=True)
    annot.write_text(f'title: "{title}"\nlast_user_view_time {{\n  seconds: 1794903605\n}}\n', encoding="utf-8")

    sum_db = sandbox["agy_data"] / "conversation_summaries.db"
    sconn = sqlite3.connect(str(sum_db))
    ensure_agy_summaries_db_schema(sconn)
    proj = ledger.project_agy_cascade_summary(
        cascade_id=cid,
        project_id=agy_pid,
        viewed_at_iso="2026-11-17T08:20:05Z",
        source=AgyGuiSafeSource.CASCADE_CLIENT,
        lineage=lineage,
    )
    sconn.execute(
        """
        INSERT OR REPLACE INTO conversation_summaries (
            conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, source,
            project_id, agent_name, parent_conversation_id, nesting_depth, battle_id, winning_conversation_id,
            not_fully_idle, killed, last_user_input_time, last_user_input_step_index, app_data_dir, raw_summary, group_id
        ) VALUES (?, ?, ?, ?, ?, ?, 'CASCADE_RUN_STATUS_IDLE', ?, ?, ?, ?, ?, '', '', 0, 0, ?, 0, ?, ?, '')
        """,
        (
            cid_str,
            title,
            proj.preview,
            len(rows),
            "2026-11-17T08:20:05Z",
            json.dumps([str(ws_uri)]),
            AgyGuiSafeSource.CASCADE_CLIENT.summary_column_name,
            str(agy_pid),
            agent_role or agent_type,
            parent_cid,
            nesting_depth,
            "2026-11-17T08:20:00Z",
            str(sandbox["agy_data"]),
            encode_cascade_summary_proto(proj),
        ),
    )
    sconn.commit()
    sconn.close()


def hydrate_sandbox(base_dir: Path, name: str, manifest: dict[str, Any], *, paired: bool = True) -> dict[str, Path]:
    root = base_dir / name
    ws = root / manifest["workspace_dirname"]
    gem = root / "gemini-config"
    agy = root / "agy-data"
    agy_cli = root / "antigravity-cli"
    codex = root / "codex-home"
    state = root / "sync-state"
    for d in (ws, gem, agy, agy_cli, codex, state):
        d.mkdir(parents=True, exist_ok=True)
    sandbox = {
        "home": root,
        "workspace": ws,
        "gemini_config": gem,
        "agy_data": agy,
        "agy_cli": agy_cli,
        "codex_home": codex,
        "sync_state": state,
    }
    ws_path, ws_uri = parse_workspace_identity(str(ws))
    ws_str = str(ws_path)
    sess = manifest["sessions"]

    # Hydrate Codex rollouts from golden fixtures
    rollout_dir = codex / "sessions" / "2026" / "11" / "17"
    rollout_dir.mkdir(parents=True, exist_ok=True)

    c1_id = sess["codex_completed_id"]
    c1_path = rollout_dir / f"rollout-2026-11-17T08-00-00-{c1_id}.jsonl"
    c1_text = (FIXTURES_DIR / "codex_rollout_completed.jsonl").read_text(encoding="utf-8").replace("__WORKSPACE__", ws_str)
    c1_path.write_text(c1_text, encoding="utf-8")

    c2_id = sess["codex_interrupted_id"]
    c2_path = rollout_dir / f"rollout-2026-11-17T09-00-00-{c2_id}.jsonl"
    c2_text = (FIXTURES_DIR / "codex_rollout_interrupted.jsonl").read_text(encoding="utf-8").replace("__WORKSPACE__", ws_str)
    c2_path.write_text(c2_text, encoding="utf-8")

    c3_id = sess["codex_zstd_id"]
    c3_path = rollout_dir / f"rollout-2026-11-17T08-10-00-{c3_id}.jsonl.zst"
    c3_text = (FIXTURES_DIR / "codex_rollout_zstd_source.jsonl").read_text(encoding="utf-8").replace("__WORKSPACE__", ws_str)
    c3_path.write_bytes(compress_zstd_rollout_bytes(c3_text))

    c4_id = sess["codex_subagent_id"]
    c4_path = rollout_dir / f"rollout-2026-11-17T08-15-00-{c4_id}.jsonl"
    c4_lines = [
        json.dumps({"timestamp": "2026-11-17T08:15:00Z", "type": "session_meta", "payload": {"id": c4_id, "cwd": ws_str, "source": "cli"}}),
        json.dumps({"timestamp": "2026-11-17T08:15:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Subagent helper check"}}),
        json.dumps({"timestamp": "2026-11-17T08:15:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Subagent done"}]}}),
        json.dumps({"timestamp": "2026-11-17T08:15:03Z", "type": "event_msg", "payload": {"type": "task_complete"}}),
    ]
    c4_path.write_text("\n".join(c4_lines) + "\n", encoding="utf-8")

    state_db = codex / "state_5.sqlite"
    cconn = sqlite3.connect(str(state_db))
    ensure_codex_state_db_schema(cconn)
    sub_src = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": c1_id, "depth": 1, "agent_nickname": "worker", "agent_role": "verifier"}}})
    now_sec = int(time.time())
    threads_meta = [
        (c1_id, str(c1_path), now_sec - 1200, "cli", "Completed Fixture Thread", "Inspect parity fixture workspace", "", ""),
        (c2_id, str(c2_path), now_sec - 60, "cli", "Interrupted Fixture Thread", "First completed step before interrupt", "", ""),
        (c3_id, str(c3_path), now_sec - 900, "cli", "Zstd Fixture Thread", "Compressed zstd rollout parity check", "", ""),
        (c4_id, str(c4_path), now_sec - 1000, sub_src, "Codex Subagent Child", "Subagent helper check", "worker", "verifier"),
    ]
    for tid, rp, upd, src, title, first_msg, nick, role in threads_meta:
        cconn.execute(
            """
            INSERT OR REPLACE INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                first_user_message, agent_nickname, agent_role, created_at_ms, updated_at_ms,
                preview, recency_at, recency_at_ms, name, project_id, originator
            ) VALUES (?, ?, ?, ?, ?, 'openai', ?, ?, 'workspace-write', 'never', 0, 1, 0, '0.166.0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'codex_cli')
            """,
            (
                tid, rp, upd, upd, src, ws_str, title, first_msg, nick or None, role or None,
                upd * 1000, upd * 1000, first_msg, upd, upd * 1000, title,
                manifest["codex_project_id"] if paired else None,
            ),
        )
    if paired:
        cconn.execute(
            "INSERT OR IGNORE INTO projects (id, name, metadata, position, created_at_ms, updated_at_ms) VALUES (?, ?, '{}', 0, ?, ?)",
            (manifest["codex_project_id"], ws.name, now_sec * 1000, now_sec * 1000),
        )
        cconn.execute(
            "INSERT OR REPLACE INTO project_roots (project_id, position, path) VALUES (?, 0, ?)",
            (manifest["codex_project_id"], ws_str),
        )
    cconn.commit()
    cconn.close()

    # Deterministic mtime ordering matching updated_at above
    os.utime(c1_path, (now_sec - 1200, now_sec - 1200))
    os.utime(c3_path, (now_sec - 900, now_sec - 900))
    os.utime(c4_path, (now_sec - 1000, now_sec - 1000))
    os.utime(c2_path, (now_sec - 60, now_sec - 60))

    if paired:
        gs = {
            "local-projects": {
                manifest["codex_legacy_project_id"]: {
                    "id": manifest["codex_legacy_project_id"],
                    "name": ws.name,
                    "rootPaths": [ws_str],
                    "createdAt": now_sec * 1000,
                    "updatedAt": now_sec * 1000,
                }
            }
        }
        (codex / ".codex-global-state.json").write_text(json.dumps(gs, indent=2) + "\n", encoding="utf-8")

        proj_dir = gem / "projects"
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / f"{manifest['agy_project_id']}.json").write_text(
            json.dumps({
                "id": manifest["agy_project_id"],
                "name": ws.name,
                "projectResources": {"resources": [{"folderUri": str(ws_uri)}]},
                "permissionGrants": {"v2Migrated": True},
                "settings": {},
                "isWorkspaceOnly": False,
            }, indent=2) + "\n",
            encoding="utf-8",
        )

    # Hydrate Antigravity parent + subagent sessions
    a1_id = sess["agy_completed_id"]
    _create_agy_db_fixture(
        sandbox, manifest, a1_id,
        "Completed Agy Fixture", "Run parity verification", "Agy parity verification passed.",
    )
    os.utime(agy / "conversations" / f"{a1_id}.db", (now_sec - 500, now_sec - 500))

    a2_id = sess["agy_subagent_id"]
    _create_agy_db_fixture(
        sandbox, manifest, a2_id,
        "Agy Subagent Child", "Subagent task", "Subagent result",
        parent_cid=a1_id, nesting_depth=1, agent_type="reviewer", agent_role="sub-verifier",
    )
    os.utime(agy / "conversations" / f"{a2_id}.db", (now_sec - 450, now_sec - 450))
    return sandbox


def assert_eq(label: str, py_val: Any, rs_val: Any) -> None:
    if py_val != rs_val:
        raise AssertionError(
            f"Parity mismatch [{label}]:\n  PYTHON: {json.dumps(py_val, indent=2, ensure_ascii=False)}\n  RUST:   {json.dumps(rs_val, indent=2, ensure_ascii=False)}"
        )


def start_mock_agy_server(sandbox: dict[str, Path], cid_str: str) -> tuple[socketserver.TCPServer, threading.Thread, Path]:
    step_payload = (
        encode_varint_field(1, 2)
        + encode_varint_field(4, 3)
        + encode_bytes_field(8, encode_string_field(1, "Live Agy RPC question") + encode_string_field(2, "Live Agy RPC question"))
    )
    traj_bytes = encode_string_field(1, "rpc-traj-1") + encode_bytes_field(2, step_payload)
    static_get_traj_resp = encode_bytes_field(1, traj_bytes) + encode_varint_field(2, 1)  # IDLE

    summary_bytes = encode_varint_field(5, 3)  # RUNNING=3 for cid_str in GetAllCascadeTrajectories
    all_traj_entry = encode_string_field(1, cid_str) + encode_bytes_field(2, summary_bytes)
    all_traj_resp = encode_bytes_field(1, all_traj_entry)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            clen = int(self.headers.get("Content-Length", "0") or 0)
            req_body = self.rfile.read(clen)
            if self.path.endswith("GetCascadeTrajectory"):
                req_cid = ""
                if len(req_body) >= 2 and req_body[0] == 0x0A:
                    ln = req_body[1]
                    req_cid = req_body[2 : 2 + ln].decode("utf-8", errors="ignore")
                conv_db = sandbox["agy_data"] / "conversations" / f"{req_cid}.db"
                if req_cid and conv_db.is_file():
                    tbytes = encode_string_field(1, f"traj-{req_cid[:8]}")
                    conn = sqlite3.connect(str(conv_db))
                    try:
                        for (blob,) in conn.execute("SELECT step_payload FROM steps ORDER BY idx ASC").fetchall():
                            tbytes += encode_bytes_field(2, bytes(blob))
                    finally:
                        conn.close()
                    body = encode_bytes_field(1, tbytes) + encode_varint_field(2, 1)
                else:
                    body = static_get_traj_resp
            elif self.path.endswith("GetAllCascadeTrajectories"):
                body = all_traj_resp
            elif self.path.endswith("CreateProject"):
                proj_blob = b""
                for fn, wt, _, raw in iter_wire_fields(req_body):
                    if fn == 1 and wt == 2:
                        proj_blob = bytes(raw)
                        break
                pid, pname = "", "workspace"
                for fn, wt, _, raw in iter_wire_fields(proj_blob):
                    if fn == 1 and wt == 2:
                        pid = bytes(raw).decode("utf-8", errors="ignore")
                    elif fn == 2 and wt == 2:
                        pname = bytes(raw).decode("utf-8", errors="ignore")
                if pid:
                    _, ws_uri = parse_workspace_identity(str(sandbox["workspace"]))
                    pdir = sandbox["gemini_config"] / "projects"
                    pdir.mkdir(parents=True, exist_ok=True)
                    (pdir / f"{pid}.json").write_text(
                        json.dumps({
                            "id": pid,
                            "name": pname,
                            "projectResources": {"resources": [{"folderUri": str(ws_uri)}]},
                            "permissionGrants": {"v2Migrated": True},
                            "settings": {},
                            "isWorkspaceOnly": False,
                        }, indent=2) + "\n",
                        encoding="utf-8",
                    )
                body = b""
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/proto")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    daemon_dir = sandbox["agy_data"] / "daemon"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    hub_path = daemon_dir / f"hub_{port}.json"
    hub_path.write_text(
        json.dumps({
            "pid": os.getpid(),
            "httpPort": port,
            "httpsPort": 0,
            "lsVersion": "2.30.0",
            "csrfToken": "ephemeral-parity-test-token",
        }),
        encoding="utf-8",
    )
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, hub_path


def _ws_recv_frame(conn: socket.socket) -> str | None:
    hdr = conn.recv(2)
    if len(hdr) < 2:
        return None
    opcode = hdr[0] & 0x0F
    if opcode == 0x8:
        return None
    masked = (hdr[1] & 0x80) != 0
    length = hdr[1] & 0x7F
    if length == 126:
        length = int.from_bytes(conn.recv(2), "big")
    elif length == 127:
        length = int.from_bytes(conn.recv(8), "big")
    mask_key = conn.recv(4) if masked else b""
    payload = bytearray()
    while len(payload) < length:
        chunk = conn.recv(length - len(payload))
        if not chunk:
            return None
        payload.extend(chunk)
    if masked:
        for i in range(len(payload)):
            payload[i] ^= mask_key[i % 4]
    return payload.decode("utf-8")


def _ws_send_text(conn: socket.socket, text: str) -> None:
    data = text.encode("utf-8")
    n = len(data)
    if n < 126:
        hdr = bytes([0x81, n])
    elif n < 65536:
        hdr = bytes([0x81, 126]) + n.to_bytes(2, "big")
    else:
        hdr = bytes([0x81, 127]) + n.to_bytes(8, "big")
    conn.sendall(hdr + data)


def start_mock_codex_ws(
    sock_path: Path,
    tid_str: str,
    sandbox: dict[str, Path] | None = None,
) -> tuple[socket.socket, threading.Event, threading.Thread]:
    if sock_path.exists():
        sock_path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(8)
    srv.settimeout(0.2)
    stop_evt = threading.Event()
    fork_counter = [0]
    threads_db: dict[str, dict[str, Any]] = {}

    def _rollout_to_rpc_thread(tid: str, rollout_path: Path, proj_id: str, title: str, cwd: str) -> dict[str, Any]:
        items = []
        for raw_ln in rollout_path.read_text(encoding="utf-8").splitlines():
            if not raw_ln.strip():
                continue
            obj = json.loads(raw_ln)
            rtype = obj.get("type", "")
            pl = obj.get("payload", {})
            if rtype == "event_msg" and pl.get("type") == "user_message":
                items.append({"type": "userMessage", "content": [{"type": "text", "text": pl.get("message", "")}]})
            elif rtype == "response_item" and pl.get("type") == "message" and pl.get("role") == "assistant":
                txt = "\n".join(c.get("text", "") for c in pl.get("content", []) if isinstance(c, dict) and c.get("text"))
                if txt:
                    items.append({"type": "agentMessage", "text": txt})
        return {
            "id": tid,
            "name": title,
            "projectId": proj_id,
            "cwd": cwd,
            "path": str(rollout_path),
            "status": {"type": "idle"},
            "turns": [{"id": "turn-fork-1", "status": "completed", "items": items}] if items else [],
        }

    def serve() -> None:
        while not stop_evt.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                req_buf = b""
                while b"\r\n\r\n" not in req_buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    req_buf += chunk
                if b"Sec-WebSocket-Key:" not in req_buf:
                    conn.close()
                    continue
                key = ""
                for line in req_buf.decode("utf-8", errors="replace").split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                accept_val = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ).decode()
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept_val}\r\n\r\n"
                    ).encode()
                )
                while not stop_evt.is_set():
                    txt = _ws_recv_frame(conn)
                    if txt is None:
                        break
                    msg = json.loads(txt)
                    rid = msg.get("id")
                    method = msg.get("method", "")
                    params = msg.get("params", {}) or {}
                    if method == "initialize" and rid is not None:
                        _ws_send_text(conn, json.dumps({"id": rid, "result": {}}))
                    elif method == "thread/fork" and rid is not None:
                        fork_counter[0] += 1
                        new_tid = f"01950000-0000-7000-8000-00000000f{fork_counter[0]:03d}"
                        staged_rp = Path(params["path"])
                        cwd = params.get("cwd", "")
                        if sandbox is not None:
                            dst_dir = sandbox["codex_home"] / "sessions" / "2026" / "11" / "17"
                            dst_dir.mkdir(parents=True, exist_ok=True)
                            dst_rp = dst_dir / f"rollout-2026-11-17T10-00-{fork_counter[0]:02d}-{new_tid}.jsonl"
                            dst_rp.write_text(staged_rp.read_text(encoding="utf-8"), encoding="utf-8")
                            threads_db[new_tid] = {"path": dst_rp, "cwd": cwd, "projectId": "", "name": "Synced Thread"}
                        _ws_send_text(conn, json.dumps({"id": rid, "result": {"thread": {"id": new_tid}}}))
                    elif method == "thread/metadata/update" and rid is not None:
                        ftid = params.get("threadId", "")
                        if ftid in threads_db:
                            threads_db[ftid]["projectId"] = params.get("projectId", "")
                        _ws_send_text(conn, json.dumps({"id": rid, "result": {}}))
                    elif method == "thread/name/set" and rid is not None:
                        ftid = params.get("threadId", "")
                        if ftid in threads_db:
                            threads_db[ftid]["name"] = params.get("name", "")
                        if sandbox is not None and ftid in threads_db:
                            info = threads_db[ftid]
                            now_s = int(time.time())
                            sconn = sqlite3.connect(str(sandbox["codex_home"] / "state_5.sqlite"))
                            ensure_codex_state_db_schema(sconn)
                            sconn.execute(
                                """
                                INSERT OR REPLACE INTO threads (
                                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                                    first_user_message, created_at_ms, updated_at_ms, preview, recency_at, recency_at_ms,
                                    name, project_id, originator
                                ) VALUES (?, ?, ?, ?, 'desktop', 'openai', ?, ?, 'workspace-write', 'never', 0, 1, 0, '0.166.0', ?, ?, ?, ?, ?, ?, ?, ?, 'codex_cli')
                                """,
                                (
                                    ftid, str(info["path"]), now_s, now_s, info["cwd"], info["name"],
                                    info["name"], now_s * 1000, now_s * 1000, info["name"], now_s, now_s * 1000,
                                    info["name"], info["projectId"],
                                ),
                            )
                            sconn.commit()
                            sconn.close()
                        _ws_send_text(conn, json.dumps({"id": rid, "result": {}}))
                    elif method == "thread/unsubscribe" and rid is not None:
                        _ws_send_text(conn, json.dumps({"id": rid, "result": {"status": "notLoaded"}}))
                    elif method == "thread/read" and rid is not None:
                        rtid = params.get("threadId", tid_str)
                        if rtid in threads_db:
                            info = threads_db[rtid]
                            thread_obj = _rollout_to_rpc_thread(rtid, info["path"], info["projectId"], info["name"], info["cwd"])
                        else:
                            thread_obj = {
                                "id": rtid,
                                "name": "Live Codex WS Thread",
                                "status": {"type": "idle"},
                                "turns": [{
                                    "id": "turn-live-1",
                                    "status": "completed",
                                    "items": [
                                        {"type": "userMessage", "content": [{"type": "text", "text": "Hello WS"}]},
                                        {"type": "agentMessage", "text": "Reply from mock WS"},
                                    ],
                                }],
                            }
                        _ws_send_text(conn, json.dumps({"id": rid, "result": {"thread": thread_obj}}))
            except Exception:
                pass
            finally:
                conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return srv, stop_evt, t


def _strip_dynamic_receipt(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    out.pop("target_id", None)
    out.pop("staging_path", None)
    out.pop("created_at_iso", None)
    out.pop("workspace", None)
    # Note: source_digest and target_seed_text_digest embed workspace workdir paths when tool payloads
    # reference `__WORKSPACE__`, so twin sandboxes (`py/` vs `rs/`) differ in path string only;
    # same-workspace digests are verified separately against Python.
    out.pop("source_digest", None)
    out.pop("target_seed_text_digest", None)
    return out


def verify_all() -> dict[str, Any]:
    manifest = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
    sess = manifest["sessions"]
    home = Path(os.environ.get("HOME", "."))
    run_id = f"{os.getpid()}-{int(time.time())}"
    artifacts_dir = home / "Projects" / "_artifacts" / "codex-agy-sync" / f"parity-{run_id}"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    short_sock_dir = home / "Projects" / "_artifacts" / "codex-agy-sync" / "rt"
    short_sock_dir.mkdir(parents=True, exist_ok=True)

    checks_passed: list[str] = []
    try:
        py_sb = hydrate_sandbox(artifacts_dir, "py", manifest, paired=True)
        rs_sb = hydrate_sandbox(artifacts_dir, "rs", manifest, paired=True)
        ws_py = str(py_sb["workspace"])
        ws_rs = str(rs_sb["workspace"])

        # 0. Version parity
        py_ver = subprocess.run([str(PYTHON_BIN), "--version"], capture_output=True, text=True, check=True)
        rs_ver = subprocess.run([str(RUST_BIN), "--version"], capture_output=True, text=True, check=True)
        assert_eq("version_exit", py_ver.returncode, rs_ver.returncode)
        assert_eq("version_text", py_ver.stdout.strip(), rs_ver.stdout.strip())
        checks_passed.append("--version")

        # 1. `pair` parity on unpaired twin sandboxes
        py_unp = hydrate_sandbox(artifacts_dir, "py-unpaired", manifest, paired=False)
        rs_unp = hydrate_sandbox(artifacts_dir, "rs-unpaired", manifest, paired=False)
        # First verify sync without --auto-pair fails with unpaired_workspace (exit 2) on both!
        py_sync_unp = run_cli(PYTHON_BIN, py_unp, ["sync", "-w", str(py_unp["workspace"]), "--latest"])
        rs_sync_unp = run_cli(RUST_BIN, rs_unp, ["sync", "-w", str(rs_unp["workspace"]), "--latest"])
        assert_eq("unpaired_sync_exit", py_sync_unp.returncode, rs_sync_unp.returncode)
        assert_eq("unpaired_sync_err_code", py_sync_unp.stderr_json()["error"], rs_sync_unp.stderr_json()["error"])
        assert_eq("unpaired_sync_paired_flag", False, py_sync_unp.returncode == 0)

        py_pair = run_cli(PYTHON_BIN, py_unp, ["pair", "-w", str(py_unp["workspace"])])
        rs_pair = run_cli(RUST_BIN, rs_unp, ["pair", "-w", str(rs_unp["workspace"])])
        assert_eq("pair_exit", py_pair.returncode, rs_pair.returncode)
        assert_eq("pair_ws_name", py_pair.stdout_json()["workspace_name"], rs_pair.stdout_json()["workspace_name"])
        assert_eq("pair_keys", sorted(py_pair.stdout_json().keys()), sorted(rs_pair.stdout_json().keys()))
        checks_passed.append("pair (+ unpaired_workspace exit 2)")

        # 2. `status` & `status --include-subagents`
        for inc_flag in ([], ["--include-subagents"]):
            py_st = run_cli(PYTHON_BIN, py_sb, ["status", "-w", ws_py, *inc_flag])
            rs_st = run_cli(RUST_BIN, rs_sb, ["status", "-w", ws_rs, *inc_flag])
            assert_eq(f"status_exit_{inc_flag}", py_st.returncode, rs_st.returncode)
            py_j, rs_j = py_st.stdout_json(), rs_st.stdout_json()
            assert_eq(f"status_pairing_{inc_flag}", py_j["pairing"]["paired"], rs_j["pairing"]["paired"])
            py_sessions = sorted(
                ((s["cascade_id"], s["codex_thread_id"], s["title"], s["agy_step_count"], s["codex_turn_count"], s["is_subagent"], s["nesting_depth"]) for s in py_j["sessions"]),
                key=lambda x: str(x),
            )
            rs_sessions = sorted(
                ((s["cascade_id"], s["codex_thread_id"], s["title"], s["agy_step_count"], s["codex_turn_count"], s["is_subagent"], s["nesting_depth"]) for s in rs_j["sessions"]),
                key=lambda x: str(x),
            )
            assert_eq(f"status_sessions_{inc_flag}", py_sessions, rs_sessions)
        checks_passed.append("status (default & --include-subagents)")

        # 3. `inspect` parity (Codex jsonl, Codex .jsonl.zst, Codex interrupted, Antigravity DB)
        for label, flag, sid in [
            ("inspect_codex_completed", "--codex-thread-id", sess["codex_completed_id"]),
            ("inspect_codex_zstd", "--codex-thread-id", sess["codex_zstd_id"]),
            ("inspect_codex_interrupted", "--codex-thread-id", sess["codex_interrupted_id"]),
            ("inspect_agy_completed", "--cascade-id", sess["agy_completed_id"]),
        ]:
            py_i = run_cli(PYTHON_BIN, py_sb, ["inspect", "-w", ws_py, flag, sid, "--mode", "offline_storage"])
            rs_i = run_cli(RUST_BIN, rs_sb, ["inspect", "-w", ws_rs, flag, sid, "--mode", "offline_storage"])
            rs_same = run_cli(RUST_BIN, py_sb, ["inspect", "-w", ws_py, flag, sid, "--mode", "offline_storage"])
            assert_eq(f"{label}_exit", py_i.returncode, rs_i.returncode)
            assert_eq(f"{label}_same_exit", 0, rs_same.returncode)
            pi, ri, r_same = py_i.stdout_json(), rs_i.stdout_json(), rs_same.stdout_json()
            for k in ("canonical_title", "total_events", "completed_turns", "has_inflight_turn", "counts"):
                assert_eq(f"{label}_{k}", pi[k], ri[k])
            assert_eq(
                f"{label}_wm_shape",
                (pi["latest_watermark"]["turn_id"], pi["latest_watermark"]["last_agy_step_index"], pi["latest_watermark"]["last_codex_line_no"]),
                (ri["latest_watermark"]["turn_id"], ri["latest_watermark"]["last_agy_step_index"], ri["latest_watermark"]["last_codex_line_no"]),
            )
            # On identical workspace bytes, canonical SHA-256 boundary_digest must match Python byte-for-byte!
            assert_eq(f"{label}_same_ws_exact_payload", pi, r_same)
        checks_passed.append("inspect (offline_storage: jsonl, zstd, interrupted, agy)")

        # 3b. `inspect --mode online_rpc` against live loopback mock servers (clean up hub_*.json after shutdown!)
        for sb, bin_p, tag in ((py_sb, PYTHON_BIN, "py"), (rs_sb, RUST_BIN, "rs")):
            srv, _, hub_p = start_mock_agy_server(sb, sess["agy_rpc_live_id"])
            try:
                res = run_cli(bin_p, sb, ["inspect", "-w", str(sb["workspace"]), "--cascade-id", sess["agy_rpc_live_id"], "--mode", "online_rpc"])
                assert_eq(f"online_agy_exit_{tag}", 0, res.returncode)
                assert_eq(f"online_agy_turns_{tag}", 1, res.stdout_json()["total_events"])
            finally:
                srv.shutdown()
                srv.server_close()
                hub_p.unlink(missing_ok=True)

        sock_py = short_sock_dir / f"vpy-{os.getpid()}.sock"
        sock_rs = short_sock_dir / f"vrs-{os.getpid()}.sock"
        for sb, bin_p, sock, tag in ((py_sb, PYTHON_BIN, sock_py, "py"), (rs_sb, RUST_BIN, sock_rs, "rs")):
            wsrv, stop_evt, wt = start_mock_codex_ws(sock, sess["codex_rpc_live_id"])
            try:
                res = run_cli(
                    bin_p, sb,
                    ["inspect", "-w", str(sb["workspace"]), "--codex-thread-id", sess["codex_rpc_live_id"], "--mode", "online_rpc"],
                    extra_env={"CODEX_APP_SERVER_SOCKET": str(sock)},
                )
                assert_eq(f"online_codex_exit_{tag}", 0, res.returncode)
                j = res.stdout_json()
                assert_eq(f"online_codex_title_{tag}", "Live Codex WS Thread", j["canonical_title"])
                assert_eq(f"online_codex_turns_{tag}", 1, j["completed_turns"])
            finally:
                stop_evt.set()
                wsrv.close()
                wt.join(timeout=1.0)
                if sock.exists():
                    sock.unlink()
        checks_passed.append("inspect (online_rpc: HTTP Connect-RPC & AF_UNIX WebSocket)")

        # 4. `menubar-feed` initial parity (hero must be interrupted_stopped!)
        py_mf = run_cli(PYTHON_BIN, py_sb, ["menubar-feed", "-w", ws_py, "--limit", "6"])
        rs_mf = run_cli(RUST_BIN, rs_sb, ["menubar-feed", "-w", ws_rs, "--limit", "6"])
        assert_eq("mf_exit", py_mf.returncode, rs_mf.returncode)
        pmf, rmf = py_mf.stdout_json(), rs_mf.stdout_json()
        assert_eq("mf_hero_id", pmf["hero"]["session_id"], rmf["hero"]["session_id"])
        assert_eq("mf_hero_state", pmf["hero"]["state"], rmf["hero"]["state"])
        assert_eq("mf_hero_state_val", "interrupted_stopped", rmf["hero"]["state"])
        assert_eq(
            "mf_recent_ids_states",
            [(x["session_id"], x["state"], x["turn_count"]) for x in pmf["recent"]],
            [(x["session_id"], x["state"], x["turn_count"]) for x in rmf["recent"]],
        )
        checks_passed.append("menubar-feed (initial hero + recent + zstd + subagent filter)")

        # 5. `sync`: offline target daemon check -> online_rpc_unavailable (exit 2), then live mock target daemons!
        py_err_off = run_cli(PYTHON_BIN, py_sb, ["sync", "-w", ws_py, "--source-id", sess["codex_completed_id"]])
        rs_err_off = run_cli(RUST_BIN, rs_sb, ["sync", "-w", ws_rs, "--source-id", sess["codex_completed_id"]])
        assert_eq("sync_offline_target_exit", (2, 2), (py_err_off.returncode, rs_err_off.returncode))
        assert_eq("sync_offline_target_err", py_err_off.stderr_json()["error"], rs_err_off.stderr_json()["error"])
        assert_eq("sync_offline_target_code", "online_rpc_unavailable", rs_err_off.stderr_json()["error"])

        py_err_int = run_cli(PYTHON_BIN, py_sb, ["sync", "-w", ws_py, "--source-id", sess["codex_interrupted_id"]])
        rs_err_int = run_cli(RUST_BIN, rs_sb, ["sync", "-w", ws_rs, "--source-id", sess["codex_interrupted_id"]])
        assert_eq("sync_int_err_exit", py_err_int.returncode, rs_err_int.returncode)
        assert_eq("sync_int_err_code", py_err_int.stderr_json()["error"], rs_err_int.stderr_json()["error"])

        # Start live mock Agy + Codex servers for both sandboxes during live sync checks
        py_agy_srv, _, py_hub = start_mock_agy_server(py_sb, sess["agy_rpc_live_id"])
        rs_agy_srv, _, rs_hub = start_mock_agy_server(rs_sb, sess["agy_rpc_live_id"])
        py_ws_sock = short_sock_dir / f"spy-{os.getpid()}.sock"
        rs_ws_sock = short_sock_dir / f"srs-{os.getpid()}.sock"
        py_wsrv, py_stop, py_wt = start_mock_codex_ws(py_ws_sock, sess["codex_rpc_live_id"], py_sb)
        rs_wsrv, rs_stop, rs_wt = start_mock_codex_ws(rs_ws_sock, sess["codex_rpc_live_id"], rs_sb)
        try:
            py_sync_int = run_cli(PYTHON_BIN, py_sb, ["sync", "-w", ws_py, "--source-id", sess["codex_interrupted_id"], "--include-interrupted"])
            rs_sync_int = run_cli(RUST_BIN, rs_sb, ["sync", "-w", ws_rs, "--source-id", sess["codex_interrupted_id"], "--include-interrupted"])
            assert_eq("sync_int_ok_exit", (0, 0), (py_sync_int.returncode, rs_sync_int.returncode))
            assert_eq("sync_int_receipt", _strip_dynamic_receipt(py_sync_int.stdout_json()), _strip_dynamic_receipt(rs_sync_int.stdout_json()))

            # Re-check menubar-feed after handing off the interrupted hero (disable live daemon probe for deterministic offline feed)
            py_hub.unlink(missing_ok=True)
            rs_hub.unlink(missing_ok=True)
            py_mf2 = run_cli(PYTHON_BIN, py_sb, ["menubar-feed", "-w", ws_py, "--limit", "6"])
            rs_mf2 = run_cli(RUST_BIN, rs_sb, ["menubar-feed", "-w", ws_rs, "--limit", "6"])
            assert_eq("mf2_hero", (py_mf2.stdout_json()["hero"]["session_id"], py_mf2.stdout_json()["hero"]["state"]), (rs_mf2.stdout_json()["hero"]["session_id"], rs_mf2.stdout_json()["hero"]["state"]))
            assert_eq(
                "mf2_recent",
                [(x["session_id"], x["state"]) for x in py_mf2.stdout_json()["recent"]],
                [(x["session_id"], x["state"]) for x in rs_mf2.stdout_json()["recent"]],
            )

            # `sync --latest -d agy_to_codex` on Agy completed session (uses live Codex WS mock)
            py_lat = run_cli(PYTHON_BIN, py_sb, ["sync", "-w", ws_py, "-d", "agy_to_codex", "--latest"], extra_env={"CODEX_APP_SERVER_SOCKET": str(py_ws_sock)})
            rs_lat = run_cli(RUST_BIN, rs_sb, ["sync", "-w", ws_rs, "-d", "agy_to_codex", "--latest"], extra_env={"CODEX_APP_SERVER_SOCKET": str(rs_ws_sock)})
            assert_eq("sync_latest_exit", (0, 0), ((py_lat.returncode, py_lat.stderr), (rs_lat.returncode, rs_lat.stderr)) if (py_lat.returncode != 0 or rs_lat.returncode != 0) else (0, 0))
            assert_eq("sync_latest_receipt", _strip_dynamic_receipt(py_lat.stdout_json()), _strip_dynamic_receipt(rs_lat.stdout_json()))
            py_target_codex_id = py_lat.stdout_json()["target_id"]
            rs_target_codex_id = rs_lat.stdout_json()["target_id"]

            # Idempotent re-run on already-handed source returns `no_op` (for `--source-id`) parity
            py_lat2 = run_cli(PYTHON_BIN, py_sb, ["sync", "-w", ws_py, "--source-id", sess["agy_completed_id"]], extra_env={"CODEX_APP_SERVER_SOCKET": str(py_ws_sock)})
            rs_lat2 = run_cli(RUST_BIN, rs_sb, ["sync", "-w", ws_rs, "--source-id", sess["agy_completed_id"]], extra_env={"CODEX_APP_SERVER_SOCKET": str(rs_ws_sock)})
            assert_eq("sync_noop_exit", (0, 0), (py_lat2.returncode, rs_lat2.returncode))
            assert_eq("sync_noop_status", py_lat2.stdout_json()["status"], rs_lat2.stdout_json()["status"])

            # Advance both target codex rollouts by 1 completed turn, restore Agy mock hub for chain hop back to Agy!
            for sb, tid in ((py_sb, py_target_codex_id), (rs_sb, rs_target_codex_id)):
                rp = next((sb["codex_home"] / "sessions").rglob(f"*{tid}.jsonl"))
                extra = [
                    json.dumps({"timestamp": "2026-11-17T10:30:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "follow-1"}}),
                    json.dumps({"timestamp": "2026-11-17T10:30:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Follow-up turn on target"}}),
                    json.dumps({"timestamp": "2026-11-17T10:30:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Follow-up reply."}]}}),
                    json.dumps({"timestamp": "2026-11-17T10:30:03Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "follow-1"}}),
                ]
                rp.write_text(rp.read_text(encoding="utf-8") + "\n".join(extra) + "\n", encoding="utf-8")
            for port_srv, hub_p in ((py_agy_srv, py_hub), (rs_agy_srv, rs_hub)):
                hub_p.write_text(json.dumps({"pid": os.getpid(), "httpPort": port_srv.server_address[1], "httpsPort": 0, "lsVersion": "2.30.0", "csrfToken": "ephemeral-parity-test-token"}), encoding="utf-8")

            py_ch = run_cli(PYTHON_BIN, py_sb, ["sync", "-w", ws_py, "--chain", "--source-id", sess["agy_completed_id"]], extra_env={"CODEX_APP_SERVER_SOCKET": str(py_ws_sock)})
            rs_ch = run_cli(RUST_BIN, rs_sb, ["sync", "-w", ws_rs, "--chain", "--source-id", sess["agy_completed_id"]], extra_env={"CODEX_APP_SERVER_SOCKET": str(rs_ws_sock)})
            assert_eq("sync_chain_exit", (0, 0), (py_ch.returncode, rs_ch.returncode))
            assert_eq("sync_chain_depth", py_ch.stdout_json()["chain_depth"], rs_ch.stdout_json()["chain_depth"])
            assert_eq("sync_chain_target_host", py_ch.stdout_json()["target_host"], rs_ch.stdout_json()["target_host"])
            assert_eq("sync_chain_depth_val", 1, rs_ch.stdout_json()["chain_depth"])
        finally:
            py_hub.unlink(missing_ok=True)
            rs_hub.unlink(missing_ok=True)
            py_agy_srv.shutdown()
            py_agy_srv.server_close()
            rs_agy_srv.shutdown()
            rs_agy_srv.server_close()
            py_stop.set()
            rs_stop.set()
            py_wsrv.close()
            rs_wsrv.close()
            py_wt.join(timeout=1.0)
            rs_wt.join(timeout=1.0)
            py_ws_sock.unlink(missing_ok=True)
            rs_ws_sock.unlink(missing_ok=True)

        # `--auto-pair` on unpaired sandbox with live Agy mock daemon
        py_ap = hydrate_sandbox(artifacts_dir, "py-autopair", manifest, paired=False)
        rs_ap = hydrate_sandbox(artifacts_dir, "rs-autopair", manifest, paired=False)
        py_ap_srv, _, py_ap_hub = start_mock_agy_server(py_ap, sess["agy_rpc_live_id"])
        rs_ap_srv, _, rs_ap_hub = start_mock_agy_server(rs_ap, sess["agy_rpc_live_id"])
        try:
            py_sync_ap = run_cli(PYTHON_BIN, py_ap, ["sync", "-w", str(py_ap["workspace"]), "--source-id", sess["codex_completed_id"], "--auto-pair"])
            rs_sync_ap = run_cli(RUST_BIN, rs_ap, ["sync", "-w", str(rs_ap["workspace"]), "--source-id", sess["codex_completed_id"], "--auto-pair"])
            assert_eq("sync_autopair_exit", (0, 0), (py_sync_ap.returncode, rs_sync_ap.returncode))
            assert_eq("sync_autopair_receipt", _strip_dynamic_receipt(py_sync_ap.stdout_json()), _strip_dynamic_receipt(rs_sync_ap.stdout_json()))
        finally:
            py_ap_hub.unlink(missing_ok=True)
            rs_ap_hub.unlink(missing_ok=True)
            py_ap_srv.shutdown()
            py_ap_srv.server_close()
            rs_ap_srv.shutdown()
            rs_ap_srv.server_close()

        # `--reconcile --dry-run` & `--reconcile` parity
        for sb in (py_sb, rs_sb):
            pdir = sb["sync_state"] / "handoffs"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "pend-test.json").write_text(
                json.dumps({
                    "workspace": str(sb["workspace"]),
                    "source_host": "codex",
                    "source_id": sess["codex_completed_id"],
                    "source_digest": "deadbeef",
                    "target_host": "antigravity",
                    "target_id": sess["pending_absent_target_id"],
                    "title": "Pending Target",
                    "status": "pending",
                    "staging_path": str(pdir / "staging" / "missing"),
                    "runtime_state_not_transferred": True,
                    "error": "simulated interruption",
                    "created_at_iso": "2026-11-17T10:40:00Z",
                    "chain_depth": 0,
                }),
                encoding="utf-8",
            )
        py_dry = run_cli(PYTHON_BIN, py_sb, ["sync", "-w", ws_py, "--reconcile", "--dry-run"])
        rs_dry = run_cli(RUST_BIN, rs_sb, ["sync", "-w", ws_rs, "--reconcile", "--dry-run"])
        assert_eq("reconcile_dry_exit", py_dry.returncode, rs_dry.returncode)
        assert_eq("reconcile_dry_counts", (py_dry.stdout_json()["total_pending_before"], len(py_dry.stdout_json()["cleared"])), (rs_dry.stdout_json()["total_pending_before"], len(rs_dry.stdout_json()["cleared"])))

        py_rec = run_cli(PYTHON_BIN, py_sb, ["sync", "-w", ws_py, "--reconcile"])
        rs_rec = run_cli(RUST_BIN, rs_sb, ["sync", "-w", ws_rs, "--reconcile"])
        assert_eq("reconcile_exit", py_rec.returncode, rs_rec.returncode)
        assert_eq("reconcile_after", (py_rec.stdout_json()["total_pending_after"], len(py_rec.stdout_json()["cleared"])), (rs_rec.stdout_json()["total_pending_after"], len(rs_rec.stdout_json()["cleared"])))

        # CLI argument validation error exit codes
        for bad_args in (
            ["sync", "-w", ws_py, "--dry-run"],
            ["sync", "-w", ws_py, "--reconcile", "--latest"],
            ["sync", "-w", ws_py, "--auto-pair", "--pair-if-offline"],
            ["sync", "-w", ws_py, "--latest", "--chain"],
            ["menubar-feed", "-w", ws_py, "--limit", "99"],
        ):
            ws_flag_args = [ws_rs if a == ws_py else a for a in bad_args]
            p_bad = run_cli(PYTHON_BIN, py_sb, bad_args)
            r_bad = run_cli(RUST_BIN, rs_sb, ws_flag_args)
            assert_eq(f"bad_args_exit_{bad_args}", 2, p_bad.returncode)
            assert_eq(f"bad_args_exit_rs_{bad_args}", p_bad.returncode, r_bad.returncode)

        checks_passed.append("sync (--source-id, --include-interrupted, --latest, --chain, --auto-pair, --reconcile, exit 2 validation)")

        # Cross-store compatibility: Rust reads target written by Python & Python reads target written by Rust!
        cross_from_py = run_cli(RUST_BIN, py_sb, ["inspect", "-w", ws_py, "--codex-thread-id", py_target_codex_id, "--mode", "offline_storage"])
        cross_from_rs = run_cli(PYTHON_BIN, rs_sb, ["inspect", "-w", ws_rs, "--codex-thread-id", rs_target_codex_id, "--mode", "offline_storage"])
        assert_eq("cross_py_rs_exit", (0, 0), (cross_from_py.returncode, cross_from_rs.returncode))
        assert_eq(
            "cross_inspect_turns",
            cross_from_py.stdout_json()["completed_turns"],
            cross_from_rs.stdout_json()["completed_turns"],
        )
        checks_passed.append("cross-host storage compatibility (Python <-> Rust)")

        return {
            "status": "PASS",
            "checks_passed": checks_passed,
            "total_groups": len(checks_passed),
        }
    finally:
        shutil.rmtree(artifacts_dir, ignore_errors=True)


def main() -> int:
    build_rust_binary()
    report = verify_all()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
