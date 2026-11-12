#!/usr/bin/env python3
"""Verification harness controller for codex-agy-sync."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _fixtures import FIXTURE_CHOICES, seed_fixture  # noqa: E402
from _suite import run_verification_suite  # noqa: E402

REPO_ROOT = SCRIPTS_DIR.parents[2]
CLI_BIN = REPO_ROOT / "bin" / "codex-agy-sync"
OWNER_MARKER = "codex-agy-sync-verify-sandbox"
REQUIRED_SUBCOMMANDS = ("pair", "status", "inspect", "sync-session", "sync-workspace")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_run_id() -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{os.getpid()}"


def _is_tmp_path(candidate: Path) -> bool:
    resolved = candidate.expanduser().resolve(strict=False)
    for forbidden in (Path("/tmp"), Path("/private/tmp")):
        if resolved == forbidden or resolved.is_relative_to(forbidden):
            return True
    return False


def _validate_run_dir(raw_run_dir: Path) -> Path:
    resolved = raw_run_dir.expanduser().resolve(strict=False)
    if _is_tmp_path(resolved):
        print(
            f"ERROR: --run-dir must not resolve under /tmp or /private/tmp (got {resolved}).",
            file=sys.stderr,
        )
        sys.exit(2)
    return resolved


def _resolve_optional_run_dir(raw_run_dir: Path | None, run_id: str | None) -> tuple[str, Path]:
    resolved_id = str(run_id or _default_run_id()).strip()
    candidate = (
        raw_run_dir
        if raw_run_dir is not None
        else (Path.home() / "Projects" / "_artifacts" / "codex-agy-sync" / f"verify-{resolved_id}")
    )
    return resolved_id, _validate_run_dir(candidate)


def _stat_fingerprint(target: Path) -> dict[str, Any]:
    try:
        st = os.stat(target)
    except FileNotFoundError:
        return {"exists": False, "ino": None, "size": None, "mtime_ns": None}
    return {
        "exists": True,
        "ino": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }


def _host_fingerprints() -> dict[str, dict[str, Any]]:
    home = Path.home()
    return {
        "agy_summaries_db": _stat_fingerprint(
            home / ".gemini" / "antigravity" / "conversation_summaries.db"
        ),
        "codex_state_db": _stat_fingerprint(home / ".codex" / "state_5.sqlite"),
    }


def _sandbox_layout(run_dir: Path) -> dict[str, Path]:
    sandbox = run_dir / "_sandbox"
    evidence = run_dir / "evidence"
    return {
        "sandbox_dir": sandbox,
        "workspace_dir": sandbox / "workspace" / "sample-repo",
        "gemini_config_dir": sandbox / "gemini-config",
        "agy_data_dir": sandbox / "agy-data",
        "codex_home_dir": sandbox / "codex-home",
        "sync_state_dir": sandbox / "sync-state",
        "owner_file": sandbox / "owner.json",
        "evidence_dir": evidence,
        "commands_dir": evidence / "commands",
        "views_dir": evidence / "views",
        "env_receipt": evidence / "env-receipt.json",
        "git_dirty_diff": evidence / "git-dirty.diff",
        "cleanup_receipt": evidence / "cleanup-receipt.json",
        "suite_summary": evidence / "suite-summary.json",
    }


def _verify_owner_marker(owner_file: Path) -> bool:
    if not owner_file.is_file():
        return False
    try:
        payload = json.loads(owner_file.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and payload.get("marker") == OWNER_MARKER


def _require_initialized_sandbox(layout: dict[str, Path]) -> None:
    if not _verify_owner_marker(layout["owner_file"]):
        print(
            f"ERROR: Missing or invalid sandbox ownership marker at {layout['owner_file']}.",
            file=sys.stderr,
        )
        sys.exit(1)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _execute_init(run_id: str, run_dir: Path) -> dict[str, Any]:
    layout = _sandbox_layout(run_dir)
    for key in (
        "workspace_dir",
        "gemini_config_dir",
        "agy_data_dir",
        "codex_home_dir",
        "sync_state_dir",
        "commands_dir",
        "views_dir",
    ):
        layout[key].mkdir(parents=True, exist_ok=True)

    readme = layout["workspace_dir"] / "README.md"
    if not readme.exists():
        readme.write_text("# Sample Repo\n", encoding="utf-8")

    _write_json(
        layout["owner_file"],
        {
            "marker": OWNER_MARKER,
            "run_id": run_id,
            "pid": os.getpid(),
            "created_at": _now_iso(),
        },
    )

    git_sha_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    git_sha = git_sha_proc.stdout.strip() if git_sha_proc.returncode == 0 else "UNKNOWN"
    git_status_proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    git_diff_proc = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    layout["git_dirty_diff"].write_text(
        f"## git status --short\n{git_status_proc.stdout}\n## git diff HEAD\n{git_diff_proc.stdout}",
        encoding="utf-8",
    )

    cli_sha256 = hashlib.sha256(CLI_BIN.read_bytes()).hexdigest() if CLI_BIN.is_file() else None
    fingerprints = _host_fingerprints()
    _write_json(
        layout["env_receipt"],
        {
            "run_id": run_id,
            "recorded_at": _now_iso(),
            "repo_root": str(REPO_ROOT),
            "git_sha": git_sha,
            "git_dirty_diff_path": str(layout["git_dirty_diff"]),
            "python_version": sys.version,
            "platform": platform.platform(),
            "cli_bin_sha256": cli_sha256,
            "host_fingerprints_before": fingerprints,
        },
    )
    return {
        "status": "INITIALIZED",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "sandbox_dir": str(layout["sandbox_dir"]),
        "workspace_dir": str(layout["workspace_dir"]),
        "evidence_dir": str(layout["evidence_dir"]),
    }


def _execute_doctor(run_dir: Path) -> tuple[bool, dict[str, Any]]:
    layout = _sandbox_layout(run_dir)
    owner_ok = _verify_owner_marker(layout["owner_file"])
    help_proc = subprocess.run(
        [sys.executable, str(CLI_BIN), "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    cli_help_ok = help_proc.returncode == 0 and all(
        sub in help_proc.stdout for sub in REQUIRED_SUBCOMMANDS
    )

    import_env = dict(os.environ)
    import_env["PYTHONPATH"] = str(REPO_ROOT / "src")
    import_env["PYTHONDONTWRITEBYTECODE"] = "1"
    import_proc = subprocess.run(
        [sys.executable, "-c", "import codex_agy_sync"],
        cwd=str(REPO_ROOT),
        env=import_env,
        capture_output=True,
        text=True,
        check=False,
    )
    import_ok = import_proc.returncode == 0

    env_receipt_ok = False
    host_unchanged = False
    if layout["env_receipt"].is_file():
        try:
            env_data = json.loads(layout["env_receipt"].read_text(encoding="utf-8"))
            before = env_data.get("host_fingerprints_before")
            env_receipt_ok = isinstance(before, dict)
            if env_receipt_ok:
                host_unchanged = _host_fingerprints() == before
        except Exception:
            env_receipt_ok = False
            host_unchanged = False

    checks = {
        "run_dir_outside_tmp": not _is_tmp_path(run_dir),
        "owner_marker_valid": owner_ok,
        "cli_help_ok": cli_help_ok,
        "codex_agy_sync_import_ok": import_ok,
        "env_receipt_present": env_receipt_ok,
        "host_fingerprints_unchanged": host_unchanged,
    }
    all_ok = all(checks.values())
    return all_ok, {
        "status": "PASS" if all_ok else "FAIL",
        "checks": checks,
        "host_fingerprints_unchanged": host_unchanged,
    }


def _next_command_seq(commands_dir: Path) -> int:
    max_seq = 0
    if commands_dir.is_dir():
        for f in commands_dir.glob("*.json"):
            m = re.match(r"^(\d+)-", f.name)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def _execute_cli(
    layout: dict[str, Path],
    slug: str,
    raw_cli_args: list[str],
    *,
    echo_output: bool = True,
) -> tuple[int, dict[str, Any]]:
    cli_args = list(raw_cli_args)
    if cli_args and cli_args[0] == "--":
        cli_args = cli_args[1:]
    if not cli_args:
        raise ValueError("Missing codex-agy-sync subcommand arguments after --.")

    ws_str = str(layout["workspace_dir"])
    substituted_args = [ws_str if tok == "@WORKSPACE" else tok for tok in cli_args]
    full_argv = [
        sys.executable,
        str(CLI_BIN),
        "--gemini-config-dir",
        str(layout["gemini_config_dir"]),
        "--agy-data-dir",
        str(layout["agy_data_dir"]),
        "--codex-home-dir",
        str(layout["codex_home_dir"]),
        "--sync-state-dir",
        str(layout["sync_state_dir"]),
        *substituted_args,
    ]

    started_at = _now_iso()
    child_env = dict(os.environ)
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env.pop("ANTIGRAVITY_CLI_DATA_DIR", None)
    child_env.pop("CODEX_APP_SERVER_SOCKET", None)
    # BSD/macOS flock is scoped to the open file description; close_fds=True prevents
    # the child CLI process from inheriting the parent's locked descriptor during hold-lock.
    proc = subprocess.run(
        full_argv,
        cwd=str(REPO_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        close_fds=True,
        pass_fds=(),
        check=False,
    )
    completed_at = _now_iso()

    seq = _next_command_seq(layout["commands_dir"])
    safe_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug).strip("-") or "cmd"
    receipt_path = layout["commands_dir"] / f"{seq:03d}-{safe_slug}.json"
    record = {
        "seq": seq,
        "slug": safe_slug,
        "argv": full_argv,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    _write_json(receipt_path, record)

    if echo_output:
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
    return proc.returncode, record


def _execute_hold_lock(
    layout: dict[str, Path],
    cascade_id: str,
    slug: str,
    cli_args: list[str],
    *,
    echo_output: bool = True,
) -> tuple[int, dict[str, Any]]:
    presence_dir = layout["agy_data_dir"] / "presence"
    presence_dir.mkdir(parents=True, exist_ok=True)
    lock_path = presence_dir / f"{cascade_id}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _execute_cli(layout, slug, cli_args, echo_output=echo_output)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _open_ro_sqlite(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve(strict=False).as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only = ON;")
    return conn


def _stat_size_mtime(path: Path) -> dict[str, int] | None:
    if not path.is_file():
        return None
    st = path.stat()
    return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def _execute_inspect_stores(
    layout: dict[str, Path],
    label: str,
    *,
    cascade_id: str | None = None,
    codex_thread_id: str | None = None,
) -> dict[str, Any]:
    codex_db = layout["codex_home_dir"] / "state_5.sqlite"
    agy_sum_db = layout["agy_data_dir"] / "conversation_summaries.db"
    projects_dir = layout["gemini_config_dir"] / "projects"
    global_state = layout["codex_home_dir"] / ".codex-global-state.json"
    cursors_dir = layout["sync_state_dir"] / "cursors"
    superseded_dir = cursors_dir / "superseded"
    backups_dir = layout["sync_state_dir"] / "backups"

    has_legacy_bridge = False
    if global_state.is_file():
        try:
            gs = json.loads(global_state.read_text(encoding="utf-8"))
            local_projects = gs.get("local-projects")
            host_map = gs.get("app-server-project-id-by-legacy-project-id-by-host")
            if isinstance(local_projects, dict) and local_projects and isinstance(host_map, dict):
                has_legacy_bridge = any(isinstance(v, dict) and bool(v) for v in host_map.values())
        except Exception:
            has_legacy_bridge = False

    view: dict[str, Any] = {
        "label": label,
        "captured_at": _now_iso(),
        "codex_state_db_exists": codex_db.is_file(),
        "codex_state_db_stat": _stat_size_mtime(codex_db),
        "agy_summaries_db_stat": _stat_size_mtime(agy_sum_db),
        "agy_project_files_count": (
            len(list(projects_dir.glob("*.json"))) if projects_dir.is_dir() else 0
        ),
        "codex_global_state_has_legacy_bridge": has_legacy_bridge,
        "cursors": (
            sorted(p.name for p in cursors_dir.glob("*.json")) if cursors_dir.is_dir() else []
        ),
        "superseded_cursors": (
            sorted(p.name for p in superseded_dir.glob("*.json"))
            if superseded_dir.is_dir()
            else []
        ),
        "backup_manifests_count": (
            len(list(backups_dir.glob("*/manifest.json"))) if backups_dir.is_dir() else 0
        ),
    }

    if cascade_id:
        cid = str(cascade_id)
        conv_db = layout["agy_data_dir"] / "conversations" / f"{cid}.db"
        if conv_db.is_file():
            st = conv_db.stat()
            conn = _open_ro_sqlite(conv_db)
            try:
                uv = int((conn.execute("PRAGMA user_version").fetchone() or (0,))[0] or 0)
                step_cnt = int(conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0])
                pref_cnt = int(
                    conn.execute("SELECT COUNT(*) FROM parent_references").fetchone()[0]
                )
            finally:
                conn.close()
            view["agy_conv_db"] = {
                "exists": True,
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
                "user_version": uv,
                "step_count": step_cnt,
                "parent_references": pref_cnt,
            }
        else:
            view["agy_conv_db"] = {"exists": False}

        summary_row = None
        if agy_sum_db.is_file():
            conn = _open_ro_sqlite(agy_sum_db)
            try:
                row = conn.execute(
                    """
                    SELECT title, step_count, source, project_id, nesting_depth, parent_conversation_id
                    FROM conversation_summaries WHERE conversation_id = ?
                    """,
                    (cid,),
                ).fetchone()
                if row is not None:
                    summary_row = {
                        "title": row[0],
                        "step_count": int(row[1]),
                        "source": row[2],
                        "project_id": row[3],
                        "nesting_depth": int(row[4]),
                        "parent_conversation_id": row[5],
                    }
            finally:
                conn.close()
        view["agy_summary_row"] = summary_row

        annot = layout["agy_data_dir"] / "annotations" / f"{cid}.pbtxt"
        view["agy_annotation_pbtxt"] = (
            {"exists": True, "content": annot.read_text(encoding="utf-8", errors="replace")}
            if annot.is_file()
            else {"exists": False, "content": None}
        )

        transcript = (
            layout["agy_data_dir"]
            / "brain"
            / cid
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        if transcript.is_file():
            lines = [
                ln
                for ln in transcript.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip()
            ]
            view["agy_transcript"] = {"exists": True, "line_count": len(lines)}
        else:
            view["agy_transcript"] = {"exists": False, "line_count": 0}

        hub_pb = layout["agy_data_dir"] / "agyhub_summaries_proto.pb"
        view["agyhub_summaries_pb_size"] = int(hub_pb.stat().st_size) if hub_pb.is_file() else 0

    if codex_thread_id:
        tid = str(codex_thread_id)
        thread_row = None
        rollout_path: Path | None = None
        if codex_db.is_file():
            conn = _open_ro_sqlite(codex_db)
            try:
                row = conn.execute(
                    """
                    SELECT title, preview, archived, project_id, rollout_path, attached_thread_parent_id
                    FROM threads WHERE id = ?
                    """,
                    (tid,),
                ).fetchone()
                if row is not None:
                    thread_row = {
                        "title": row[0],
                        "preview": row[1],
                        "archived": int(row[2]),
                        "project_id": row[3],
                        "rollout_path": row[4],
                        "attached_thread_parent_id": row[5],
                    }
                    if row[4]:
                        rollout_path = Path(str(row[4]))
            finally:
                conn.close()
        view["codex_thread_row"] = thread_row
        if rollout_path is not None and rollout_path.is_file():
            rlines = [
                ln
                for ln in rollout_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip()
            ]
            view["codex_rollout"] = {"exists": True, "line_count": len(rlines)}
        else:
            view["codex_rollout"] = {"exists": False, "line_count": 0}

    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "view"
    _write_json(layout["views_dir"] / f"{safe_label}.json", view)
    return view


def _execute_cleanup(layout: dict[str, Path]) -> tuple[bool, dict[str, Any]]:
    sandbox_removed = False
    if layout["sandbox_dir"].exists():
        if not _verify_owner_marker(layout["owner_file"]):
            raise RuntimeError(
                f"Refusing to delete {layout['sandbox_dir']} without valid owner.json marker."
            )
        shutil.rmtree(layout["sandbox_dir"])
        sandbox_removed = True

    after = _host_fingerprints()
    host_unchanged = False
    if layout["env_receipt"].is_file():
        try:
            env_data = json.loads(layout["env_receipt"].read_text(encoding="utf-8"))
            host_unchanged = after == env_data.get("host_fingerprints_before")
        except Exception:
            host_unchanged = False

    preserved = (
        sorted(
            str(p.relative_to(layout["evidence_dir"]))
            for p in layout["evidence_dir"].rglob("*")
            if p.is_file() and p != layout["cleanup_receipt"]
        )
        if layout["evidence_dir"].is_dir()
        else []
    )
    receipt = {
        "cleaned_at": _now_iso(),
        "sandbox_removed": sandbox_removed,
        "sandbox_exists_after": layout["sandbox_dir"].exists(),
        "evidence_files_preserved": preserved,
        "host_fingerprints_after": after,
        "host_fingerprints_unchanged": host_unchanged,
    }
    _write_json(layout["cleanup_receipt"], receipt)
    ok = (not layout["sandbox_dir"].exists()) and bool(preserved) and host_unchanged
    return ok, receipt


def _cmd_init(args: argparse.Namespace) -> int:
    run_id, run_dir = _resolve_optional_run_dir(args.run_dir, args.run_id)
    _print_json(_execute_init(run_id, run_dir))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    run_dir = _validate_run_dir(args.run_dir)
    ok, out = _execute_doctor(run_dir)
    _print_json(out)
    return 0 if ok else 1


def _cmd_seed(args: argparse.Namespace) -> int:
    run_dir = _validate_run_dir(args.run_dir)
    layout = _sandbox_layout(run_dir)
    _require_initialized_sandbox(layout)
    _print_json(seed_fixture(layout, args.fixture))
    return 0


def _cmd_cli(args: argparse.Namespace) -> int:
    run_dir = _validate_run_dir(args.run_dir)
    layout = _sandbox_layout(run_dir)
    _require_initialized_sandbox(layout)
    try:
        rc, _ = _execute_cli(layout, args.slug, list(args.cli_args), echo_output=True)
        return rc
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _cmd_hold_lock(args: argparse.Namespace) -> int:
    run_dir = _validate_run_dir(args.run_dir)
    layout = _sandbox_layout(run_dir)
    _require_initialized_sandbox(layout)
    try:
        rc, _ = _execute_hold_lock(
            layout, args.cascade_id, args.slug, list(args.cli_args), echo_output=True
        )
        return rc
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _cmd_inspect_stores(args: argparse.Namespace) -> int:
    run_dir = _validate_run_dir(args.run_dir)
    layout = _sandbox_layout(run_dir)
    _require_initialized_sandbox(layout)
    view = _execute_inspect_stores(
        layout,
        args.label,
        cascade_id=args.cascade_id,
        codex_thread_id=args.codex_thread_id,
    )
    _print_json(view)
    return 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    run_dir = _validate_run_dir(args.run_dir)
    layout = _sandbox_layout(run_dir)
    try:
        ok, receipt = _execute_cleanup(layout)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _print_json(receipt)
    return 0 if ok else 1


def _cmd_suite(args: argparse.Namespace) -> int:
    run_id, run_dir = _resolve_optional_run_dir(args.run_dir, args.run_id)
    layout = _sandbox_layout(run_dir)
    rc, summary = run_verification_suite(
        run_id=run_id,
        run_dir=run_dir,
        layout=layout,
        execute_init=_execute_init,
        execute_doctor=_execute_doctor,
        execute_cli=_execute_cli,
        execute_hold_lock=_execute_hold_lock,
        execute_inspect_stores=_execute_inspect_stores,
        execute_cleanup=_execute_cleanup,
        write_json=_write_json,
    )
    _print_json(summary)
    return rc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="control_sync_verify.py",
        description="Isolated sandbox driver and read-only store verifier for codex-agy-sync.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--run-dir", type=Path, default=None)
    p_init.add_argument("--run-id", type=str, default=None)

    p_doc = sub.add_parser("doctor")
    p_doc.add_argument("--run-dir", type=Path, required=True)

    p_seed = sub.add_parser("seed")
    p_seed.add_argument("--run-dir", type=Path, required=True)
    p_seed.add_argument("--fixture", choices=FIXTURE_CHOICES, required=True)

    p_cli = sub.add_parser("cli")
    p_cli.add_argument("--run-dir", type=Path, required=True)
    p_cli.add_argument("--slug", type=str, required=True)
    p_cli.add_argument("cli_args", nargs=argparse.REMAINDER)

    p_lock = sub.add_parser("hold-lock")
    p_lock.add_argument("--run-dir", type=Path, required=True)
    p_lock.add_argument("--cascade-id", type=str, required=True)
    p_lock.add_argument("--slug", type=str, required=True)
    p_lock.add_argument("cli_args", nargs=argparse.REMAINDER)

    p_insp = sub.add_parser("inspect-stores")
    p_insp.add_argument("--run-dir", type=Path, required=True)
    p_insp.add_argument("--label", type=str, required=True)
    p_insp.add_argument("--cascade-id", type=str, default=None)
    p_insp.add_argument("--codex-thread-id", type=str, default=None)

    p_clean = sub.add_parser("cleanup")
    p_clean.add_argument("--run-dir", type=Path, required=True)

    p_suite = sub.add_parser("suite")
    p_suite.add_argument("--run-dir", type=Path, default=None)
    p_suite.add_argument("--run-id", type=str, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "init": _cmd_init,
        "doctor": _cmd_doctor,
        "seed": _cmd_seed,
        "cli": _cmd_cli,
        "hold-lock": _cmd_hold_lock,
        "inspect-stores": _cmd_inspect_stores,
        "cleanup": _cmd_cleanup,
        "suite": _cmd_suite,
    }
    return dispatch[args.subcommand](args)


if __name__ == "__main__":
    sys.exit(main())
