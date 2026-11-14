from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
from typing import Any
import urllib.error
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from codex_agy_sync import SyncBridge
from codex_agy_sync._wire import encode_bytes_field, encode_string_field, encode_varint_field, iter_wire_fields
from codex_agy_sync.codecs import encode_codex_rollout
from codex_agy_sync.bridge.handoff import HandoffReceipt, ledger_seed_fingerprint
from codex_agy_sync.bridge.paths_and_locks import HostEnvironmentPaths, TransportMode
from codex_agy_sync.bridge.state_machine import generate_uuid7
from codex_agy_sync.bridge.transport import DefaultHostTransport
from codex_agy_sync.ledger import (
    AssistantTextEvent,
    CascadeId,
    CodexThreadId,
    EventOriginAnchor,
    HostKind,
    TrajectoryLedger,
    TurnId,
    UserInputEvent,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def is_tmp_path(candidate: Path) -> bool:
    for probe in (candidate, candidate.resolve(strict=False)):
        for root in (Path("/tmp"), Path("/private/tmp")):
            if probe == root or probe.is_relative_to(root):
                return True
    return False


def validate_run_dir(raw_run_dir: Path | None) -> tuple[str, Path]:
    run_id = default_run_id()
    if raw_run_dir is None:
        candidate = Path.home() / "Projects" / "_artifacts" / "codex-agy-sync" / f"verify-workbench-{run_id}"
    else:
        candidate = raw_run_dir.expanduser()
        if is_tmp_path(candidate):
            raise ValueError(f"Refusing temporary directory path: {candidate}")
        candidate = candidate.resolve(strict=False)
        if is_tmp_path(candidate):
            raise ValueError(f"Refusing temporary directory path: {candidate}")
        run_id = candidate.name
    if candidate.exists():
        raise FileExistsError(f"Refusing pre-existing run directory: {candidate}")
    return run_id, candidate


def sandbox_layout(run_dir: Path) -> dict[str, Path]:
    sandbox = run_dir / "_sandbox"
    workspaces = sandbox / "workspaces"
    evidence = run_dir / "evidence"
    return {
        "run_dir": run_dir,
        "sandbox_dir": sandbox,
        "owner_file": sandbox / "owner.json",
        "workspaces_dir": workspaces,
        "ws_paired": workspaces / "repo-paired",
        "ws_unpaired": workspaces / "repo-unpaired",
        "ws_outside": sandbox / "outside-dir",
        "gemini_config_dir": sandbox / "gemini-config",
        "agy_data_dir": sandbox / "agy-data",
        "codex_home_dir": sandbox / "codex-home",
        "sync_state_dir": sandbox / "sync-state",
        "evidence_dir": evidence,
        "commands_dir": evidence / "commands",
        "views_dir": evidence / "views",
        "screenshots_dir": evidence / "screenshots",
        "env_receipt": evidence / "env-receipt.json",
        "cleanup_receipt": evidence / "cleanup-receipt.json",
        "suite_summary": evidence / "suite-summary.json",
    }


def _stat_fingerprint(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"exists": False}
    st = target.stat()
    return {"exists": True, "ino": int(st.st_ino), "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def capture_host_fingerprints() -> dict[str, dict[str, Any]]:
    home = Path.home()
    return {
        "agy_summaries": _stat_fingerprint(home / ".gemini" / "antigravity" / "conversation_summaries.db"),
        "codex_state": _stat_fingerprint(home / ".codex" / "state_5.sqlite"),
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n", encoding="utf-8")


def build_isolated_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ANTIGRAVITY_CLI_DATA_DIR", None)
    env.pop("CODEX_APP_SERVER_SOCKET", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


CLI_BIN = REPO_ROOT / "bin" / "codex-agy-sync"
REQUIRED_IDS = (
    "mainSearch",
    "panelSearch",
    "draftNote",
    "submitDraft",
    "handoffButton",
    "resetButton",
    "askButton",
    "themeButton",
    "sourceBanner",
    "sessionChips",
    "timeline",
    "livePairingBanner",
    "liveHandoffChain",
    "liveHandoffControls",
    "handoffPairIfOffline",
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def check_prototype(raw_path: str) -> dict[str, Any]:
    path = Path(raw_path).expanduser()
    if not path.is_file():
        return {"ok": False, "path": str(path), "reason": "Prototype file not found."}
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return {"ok": False, "path": str(path), "reason": "Prototype file is empty."}
    missing_ids = [item for item in REQUIRED_IDS if f'id="{item}"' not in text and f"id='{item}'" not in text]
    has_live_ctrl = "createLiveController" in text
    if missing_ids or not has_live_ctrl:
        return {
            "ok": False,
            "path": str(path),
            "missing_ids": missing_ids,
            "has_live_controller": has_live_ctrl,
            "reason": "Prototype is missing required live workbench IDs or controller.",
        }
    return {"ok": True, "path": str(path.resolve()), "size": path.stat().st_size, "sha256": sha256_file(path), "has_live_controller": True}


def run_doctor_checks(raw_prototype: str) -> tuple[bool, dict[str, Any]]:
    proto = check_prototype(raw_prototype)
    cli_ok = CLI_BIN.is_file()
    cli_help: dict[str, Any] = {"ok": False}
    if cli_ok:
        proc = subprocess.run([str(CLI_BIN), "serve", "--help"], cwd=str(REPO_ROOT), env=build_isolated_env(), capture_output=True, text=True, timeout=15, check=False)
        cli_help = {"ok": proc.returncode == 0 and "--status" in proc.stdout and "--stop" in proc.stdout and "--ui" in proc.stdout, "exit_code": proc.returncode}
    ab_bin = shutil.which("agent-browser")
    ab_info: dict[str, Any] = {"ok": False, "path": ab_bin}
    if ab_bin:
        proc = subprocess.run([ab_bin, "--version"], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15, check=False)
        ab_info = {"ok": proc.returncode == 0, "path": ab_bin, "version": proc.stdout.strip() or proc.stderr.strip()}
    ok = bool(proto.get("ok") and cli_ok and cli_help.get("ok") and ab_info.get("ok"))
    return ok, {"status": "PASS" if ok else "BLOCKED", "checked_at": now_iso(), "repo_root": str(REPO_ROOT), "prototype": proto, "cli": {"path": str(CLI_BIN), **cli_help}, "agent_browser": ab_info}


def http_call(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None, timeout: float = 5.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        return int(err.code), err.read().decode("utf-8", errors="replace")


def record_git_receipts(layout: dict[str, Path]) -> str:
    ev = layout["evidence_dir"]
    ev.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=False).stdout.strip()
    (ev / "git-head.txt").write_text(head + "\n", encoding="utf-8")
    for fname, cmd in (("git-status.txt", ["git", "status", "--short"]), ("git-diff.stat.txt", ["git", "diff", "--stat"]), ("git-dirty.diff", ["git", "diff"])):
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, check=False)
        (ev / fname).write_text(proc.stdout, encoding="utf-8")
    return head


def make_paths(layout: dict[str, Path]) -> HostEnvironmentPaths:
    return HostEnvironmentPaths(
        gemini_config_dir=layout["gemini_config_dir"],
        agy_data_dir=layout["agy_data_dir"],
        codex_home_dir=layout["codex_home_dir"],
        sync_state_dir=layout["sync_state_dir"],
    )


def _turn_events(host: HostKind, turn_id: str, ts_iso: str, user_text: str, asst_text: str) -> list[Any]:
    anchor = EventOriginAnchor(origin_host=host, turn_id=TurnId(turn_id), timestamp_iso=ts_iso)
    return [
        UserInputEvent(kind="user_input", anchor=anchor, query_text=user_text),
        AssistantTextEvent(kind="assistant_text", anchor=anchor, text=asst_text),
    ]


def _seed_excluded_rows(layout: dict[str, Path], fake_home: Path) -> dict[str, str]:
    markers = {
        "archive_marker": "ArchiveSecretMarker991",
        "private_keys_marker": "PrivateKeyMarker992",
        "outside_project_marker": "OutsideProjectMarker993",
        "lower_archive_marker": "LowerArchiveSecretMarker994",
        "symlink_archive_marker": "SymlinkArchiveSecretMarker995",
    }
    codex_db = layout["codex_home_dir"] / "state_5.sqlite"
    archive_dir = fake_home / "Archive" / "secret-test"
    lower_archive_dir = fake_home / "archive" / "lower-secret-test"
    priv_dir = fake_home / ".private_keys" / "key-test"
    symlink_dir = layout["workspaces_dir"] / "symlink-into-archive"
    for d in (archive_dir, lower_archive_dir, priv_dir, layout["workspaces_dir"]):
        d.mkdir(parents=True, exist_ok=True)
        (d / "README.md").write_text(f"# {d.name}\n", encoding="utf-8")
    if symlink_dir.is_symlink():
        symlink_dir.unlink()
    if not symlink_dir.exists():
        symlink_dir.symlink_to(archive_dir)
    sessions_dir = layout["codex_home_dir"] / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(codex_db), timeout=5.0)
    try:
        rows = (
            ("01960000-0000-7000-8000-00000000f001", str(archive_dir), markers["archive_marker"]),
            ("01960000-0000-7000-8000-00000000f002", str(priv_dir), markers["private_keys_marker"]),
            ("01960000-0000-7000-8000-00000000f004", str(lower_archive_dir), markers["lower_archive_marker"]),
            ("01960000-0000-7000-8000-00000000f005", str(symlink_dir), markers["symlink_archive_marker"]),
        )
        for idx, (tid, cwd_val, marker) in enumerate(rows, start=1):
            rollout = sessions_dir / f"excluded-{idx}-{tid}.jsonl"
            rollout_lines = encode_codex_rollout(
                _turn_events(HostKind.CODEX, f"excluded-t{idx}", "2026-11-14T00:05:00Z", marker, marker),
                session_id=tid,
                cwd=cwd_val,
            )
            rollout.write_text("".join(line.raw_json + "\n" for line in rollout_lines), encoding="utf-8")
            conn.execute(
                "INSERT OR REPLACE INTO threads "
                "(id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, has_user_event, first_user_message) "
                "VALUES (?, ?, 1763082000, 1763082000, 'desktop', 'openai', ?, ?, 'workspace-write', 'on-request', 1, ?)",
                (tid, str(rollout), cwd_val, marker, marker),
            )
        conn.commit()
    finally:
        conn.close()

    sum_db = layout["agy_data_dir"] / "conversation_summaries.db"
    sconn = sqlite3.connect(str(sum_db), timeout=5.0)
    try:
        sconn.execute(
            "INSERT OR REPLACE INTO conversation_summaries "
            "(conversation_id, title, last_modified_time, last_user_input_time, workspace_uris, project_id) "
            "VALUES (?, ?, '2026-11-14T00:10:00Z', '2026-11-14T00:10:00Z', ?, 'outside-of-project')",
            (
                "00000000-0000-4000-8000-00000000f003",
                markers["outside_project_marker"],
                json.dumps([layout["ws_outside"].resolve().as_uri()]),
            ),
        )
        sconn.commit()
    finally:
        sconn.close()

    outside_proj = layout["gemini_config_dir"] / "projects" / "outside-of-project.json"
    write_json(
        outside_proj,
        {"id": "outside-of-project", "folders": [{"uri": layout["ws_outside"].resolve().as_uri()}]},
    )
    return markers


def seed_workbench_sandbox(layout: dict[str, Path], run_id: str) -> dict[str, Any]:
    for key in (
        "sandbox_dir",
        "ws_paired",
        "ws_unpaired",
        "ws_outside",
        "gemini_config_dir",
        "agy_data_dir",
        "codex_home_dir",
        "sync_state_dir",
        "evidence_dir",
        "commands_dir",
        "views_dir",
        "screenshots_dir",
    ):
        layout[key].mkdir(parents=True, exist_ok=True)
    (layout["ws_paired"] / "README.md").write_text("# repo-paired\n", encoding="utf-8")
    (layout["ws_unpaired"] / "README.md").write_text("# repo-unpaired\n", encoding="utf-8")
    (layout["ws_outside"] / "README.md").write_text("# outside-dir\n", encoding="utf-8")
    write_json(
        layout["owner_file"],
        {
            "owner": "verify-codex-agy-sync",
            "suite": "workbench",
            "run_id": run_id,
            "run_dir": str(layout["run_dir"]),
            "created_at": now_iso(),
        },
    )

    paths = make_paths(layout)
    bridge = SyncBridge(paths=paths)
    transport = DefaultHostTransport(paths)
    locks = bridge.guard.probe_locks(None, None)

    now = datetime.now(timezone.utc)
    ts_a0 = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_t1_2 = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_t2 = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_r0 = (now - timedelta(minutes=8)).strftime("%Y-%m-%dT%H:%M:%SZ")

    binding_paired = bridge.pair_workspace(layout["ws_paired"])
    a0 = CascadeId("00000000-0000-4000-8000-00000000aa01")
    a0_events = _turn_events(
        HostKind.ANTIGRAVITY,
        "agy-t1",
        ts_a0,
        "Inspect pkce verifier contract in repo-paired",
        "Antigravity verified pkce verifier S256 contract across auth endpoints.",
    )
    a0_ledger = TrajectoryLedger.from_events(
        binding_paired.workspace_path,
        binding_paired.workspace_uri,
        a0_events,
        explicit_title_override="OAuth PKCE Architecture",
    )
    transport.apply_to_agy(binding_paired, a0, a0_events, a0_ledger, locks, TransportMode.OFFLINE_STORAGE)
    sum_conn = sqlite3.connect(str(layout["agy_data_dir"] / "conversation_summaries.db"), timeout=5.0)
    try:
        sum_conn.execute(
            "UPDATE conversation_summaries SET last_modified_time = ?, last_user_input_time = ? WHERE conversation_id = ?",
            (ts_a0, ts_a0, str(a0)),
        )
        sum_conn.commit()
    finally:
        sum_conn.close()

    t1 = CodexThreadId("01960000-0000-7000-8000-00000000cc01")
    t1_events = [
        *_turn_events(
            HostKind.CODEX,
            "cx-t1",
            ts_a0,
            "Inspect pkce verifier contract in repo-paired",
            "Antigravity verified pkce verifier S256 contract across auth endpoints.",
        ),
        *_turn_events(
            HostKind.CODEX,
            "cx-t2",
            ts_t1_2,
            "Implement pkce verifier callback handler in Codex",
            "Codex added pkce verifier replay protection and callback tests.",
        ),
    ]
    t1_ledger = TrajectoryLedger.from_events(
        binding_paired.workspace_path,
        binding_paired.workspace_uri,
        t1_events,
        explicit_title_override="OAuth PKCE Architecture",
    )
    transport.apply_to_codex(binding_paired, t1, t1_events, t1_ledger, locks, TransportMode.OFFLINE_STORAGE)

    seed_turns, seed_digest = ledger_seed_fingerprint(a0_ledger)
    chain_id = str(generate_uuid7())
    r0 = HandoffReceipt(
        workspace=str(binding_paired.workspace_path),
        source_host="antigravity",
        source_id=str(a0),
        source_digest=seed_digest,
        target_host="codex",
        target_id=str(t1),
        title="OAuth PKCE Architecture",
        status="ready",
        staging_path=str(layout["sync_state_dir"] / "staging" / "seed-r0"),
        created_at_iso=ts_r0,
        chain_id=chain_id,
        chained_from_source_id=None,
        chain_depth=0,
        target_seed_turn_count=seed_turns,
        target_seed_text_digest=seed_digest,
    )
    handoffs_dir = layout["sync_state_dir"] / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    write_json(handoffs_dir / "000000-seed-paired.json", dataclasses.asdict(r0))

    binding_unpaired = bridge.registry.ensure_codex_binding(layout["ws_unpaired"])
    t2 = CodexThreadId("01960000-0000-7000-8000-00000000cc02")
    t2_events = _turn_events(
        HostKind.CODEX,
        "unp-t1",
        ts_t2,
        "Document local cache eviction policy",
        "Documented bounded LRU eviction policy for sandbox cache entries.",
    )
    t2_ledger = TrajectoryLedger.from_events(
        binding_unpaired.workspace_path,
        binding_unpaired.workspace_uri,
        t2_events,
        explicit_title_override="Unpaired Codex Cache Notes",
    )
    transport.apply_to_codex(binding_unpaired, t2, t2_events, t2_ledger, locks, TransportMode.OFFLINE_STORAGE)

    markers = _seed_excluded_rows(layout, layout["sandbox_dir"])
    return {
        "ws_paired": str(binding_paired.workspace_path),
        "ws_unpaired": str(binding_unpaired.workspace_path),
        "a0": str(a0),
        "t1": str(t1),
        "t2": str(t2),
        "chain_id": chain_id,
        "search_query": "pkce verifier",
        "excluded_markers": markers,
    }


def _read_agy_rpc_trajectory(agy_data_dir: Path, cascade_id: str) -> bytes | None:
    db_path = agy_data_dir / "conversations" / f"{cascade_id}.db"
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        step_rows = conn.execute("SELECT step_payload FROM steps ORDER BY idx ASC").fetchall()
        tmeta_row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main' LIMIT 1").fetchone()
        meta_row = conn.execute("SELECT source FROM trajectory_meta LIMIT 1").fetchone()
    finally:
        conn.close()
    parts = [encode_string_field(1, cascade_id)]
    for (payload,) in step_rows:
        if payload:
            parts.append(encode_bytes_field(2, bytes(payload)))
    if tmeta_row and tmeta_row[0]:
        parts.append(encode_bytes_field(7, bytes(tmeta_row[0])))
    if meta_row and meta_row[0] is not None:
        parts.append(encode_varint_field(8, int(meta_row[0])))
    traj_blob = b"".join(parts)
    return encode_bytes_field(1, traj_blob) + encode_varint_field(2, 1)


class AgyLoopbackRpcStub:
    def __init__(self, agy_data_dir: Path) -> None:
        self._agy_data_dir = agy_data_dir
        self._csrf_token = secrets.token_urlsafe(18)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._hub_path: Path | None = None
        self.port: int = 0

    def start(self) -> int:
        agy_data_dir = self._agy_data_dir
        expected_token = self._csrf_token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return None

            def do_POST(self) -> None:
                if self.path != "/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory":
                    self.send_response(404)
                    self.end_headers()
                    return
                if self.headers.get("x-codeium-csrf-token") != expected_token:
                    self.send_response(403)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length > 0 else b""
                cascade_id = ""
                for fn, wt, _, raw in iter_wire_fields(body):
                    if fn == 1 and wt == 2:
                        cascade_id = raw.decode("utf-8", errors="replace").strip()
                payload = _read_agy_rpc_trajectory(agy_data_dir, cascade_id) if cascade_id else None
                if payload is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/proto")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        daemon_dir = agy_data_dir / "daemon"
        daemon_dir.mkdir(parents=True, exist_ok=True)
        self._hub_path = daemon_dir / f"hub_{self.port}.json"
        write_json(
            self._hub_path,
            {
                "pid": os.getpid(),
                "httpPort": self.port,
                "httpsPort": 0,
                "lsVersion": "2.31.2",
                "csrfToken": self._csrf_token,
            },
        )
        return self.port

    def stop(self) -> None:
        if self._hub_path is not None:
            self._hub_path.unlink(missing_ok=True)
            self._hub_path = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


def cleanup_sandbox(layout: dict[str, Path]) -> tuple[bool, dict[str, Any]]:
    sandbox_dir = layout["sandbox_dir"]
    owner_file = layout["owner_file"]
    if sandbox_dir.exists():
        if not owner_file.is_file():
            raise RuntimeError(f"Refusing to delete {sandbox_dir} without owner.json")
        shutil.rmtree(sandbox_dir)
    after_fp = capture_host_fingerprints()
    before_fp: dict[str, Any] = {}
    if layout["env_receipt"].is_file():
        env_data = json.loads(layout["env_receipt"].read_text(encoding="utf-8"))
        before_fp = env_data.get("host_fingerprints_before") or {}
    host_unchanged = bool(before_fp) and (before_fp == after_fp)
    preserved = sorted(
        str(p.relative_to(layout["evidence_dir"]))
        for p in layout["evidence_dir"].rglob("*")
        if p.is_file() and p != layout["cleanup_receipt"]
    ) if layout["evidence_dir"].is_dir() else []
    receipt = {
        "status": "PASS" if (not sandbox_dir.exists() and host_unchanged and bool(preserved)) else "FAIL",
        "sandbox_exists_after": sandbox_dir.exists(),
        "host_fingerprints_before": before_fp,
        "host_fingerprints_after": after_fp,
        "host_fingerprints_unchanged": host_unchanged,
        "preserved_evidence": preserved,
        "cleaned_at": now_iso(),
    }
    write_json(layout["cleanup_receipt"], receipt)
    return receipt["status"] == "PASS", receipt
