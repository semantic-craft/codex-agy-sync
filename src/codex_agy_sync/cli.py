from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .bridge import (
    EmptySessionError,
    ForkResolutionPolicy,
    HandoffCancelledError,
    HandoffPendingError,
    HostEnvironmentPaths,
    OnlineRpcUnavailableError,
    SyncBridge,
    SyncBusyLockError,
    SyncDirection,
    SyncReceipt,
    TransportMode,
    UnpairedWorkspaceError,
    WorkspaceProjectBinding,
    diagnose_workspace_binding,
    execute_handoff,
    reconcile_workspace_handoffs,
    summarize_workspace_handoffs,
    validate_handoff_selector_flags,
)
from .events import (
    AssistantTextEvent,
    CheckpointEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserInputEvent,
)
from .workbench import build_menubar_feed, query_workbench_status, run_serve_server, stop_workbench_server
from .workspace import AgyGuiSafeSource, CascadeId, CodexThreadId, PACKAGE_VERSION, normalize_workspace_spec


def _binding_to_dict(binding: WorkspaceProjectBinding) -> dict[str, Any]:
    return {
        "workspace_path": str(binding.workspace_path),
        "workspace_uri": str(binding.workspace_uri),
        "workspace_name": binding.workspace_name,
        "agy_project_id": str(binding.agy_project_id),
        "codex_project_id": str(binding.codex_project_id),
        "codex_legacy_project_id": str(binding.codex_legacy_project_id),
    }


def _receipt_to_dict(receipt: SyncReceipt) -> dict[str, Any]:
    cursor_dict = None
    if receipt.updated_cursor is not None:
        c = receipt.updated_cursor
        cursor_dict = {
            "workspace_uri": str(c.workspace_uri),
            "cascade_id": str(c.cascade_id),
            "codex_thread_id": str(c.codex_thread_id),
            "last_synced_turn_id": str(c.last_synced_turn_id),
            "last_synced_agy_step_idx": int(c.last_synced_agy_step_idx),
            "last_synced_codex_line_no": int(c.last_synced_codex_line_no),
            "agy_boundary_digest": str(c.agy_boundary_digest),
            "codex_boundary_digest": str(c.codex_boundary_digest),
            "updated_at_iso": c.updated_at_iso,
            "rescued_inflight_turn": bool(c.rescued_inflight_turn),
            "pre_rescue_source_host": c.pre_rescue_source_host or None,
        }
    backup_dict = None
    if receipt.backup_manifest is not None:
        b = receipt.backup_manifest
        backup_dict = {
            "manifest_id": b.manifest_id,
            "created_at_iso": b.created_at_iso,
            "target_host": b.target_host.value,
            "target_session_id": b.target_session_id,
            "backup_dir": str(b.backup_dir),
            "backed_up_files": [str(p) for p in b.backed_up_files],
            "restoration_command": b.restoration_command,
        }
    return {
        "workspace": _binding_to_dict(receipt.workspace),
        "cascade_id": str(receipt.cascade_id),
        "codex_thread_id": str(receipt.codex_thread_id),
        "transition_kind": receipt.transition_kind,
        "transport_used": receipt.transport_used.value,
        "events_applied": receipt.events_applied,
        "updated_cursor": cursor_dict,
        "forked_from_cascade_id": str(receipt.forked_from_cascade_id) if receipt.forked_from_cascade_id else None,
        "forked_from_codex_thread_id": str(receipt.forked_from_codex_thread_id)
        if receipt.forked_from_codex_thread_id
        else None,
        "peer_fork_cascade_id": str(receipt.peer_fork_cascade_id) if receipt.peer_fork_cascade_id else None,
        "peer_fork_codex_thread_id": str(receipt.peer_fork_codex_thread_id)
        if receipt.peer_fork_codex_thread_id
        else None,
        "backup_manifest": backup_dict,
    }


def _add_sync_common_args(sub_p: argparse.ArgumentParser) -> None:
    sub_p.add_argument("--workspace", "-w", type=str, default=None, help="Workspace path or file:// URI.")
    sub_p.add_argument("--direction", choices=[d.value for d in SyncDirection], default=SyncDirection.AUTO_INCREMENTAL.value, help="Synchronization direction.")
    sub_p.add_argument("--mode", choices=[m.value for m in TransportMode], default=TransportMode.AUTO.value, help="Transport mode.")
    sub_p.add_argument("--fork-policy", choices=[p.value for p in ForkResolutionPolicy], default=ForkResolutionPolicy.HALT_ON_DIVERGENCE.value, help="Divergence resolution policy.")
    sub_p.add_argument(
        "--include-interrupted",
        action="store_true",
        help="Seal and synchronize a stopped trailing interrupted turn with canceled tool outputs.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-agy-sync",
        description="Same-directory bidirectional session bridge between Codex Desktop 0.166+ and Antigravity 2.30+.",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"%(prog)s {PACKAGE_VERSION}",
    )
    parser.add_argument("--agy-data-dir", type=Path, default=None, help="Override Antigravity data directory (defaults to $ANTIGRAVITY_DATA_DIR or ~/.gemini/antigravity).")
    parser.add_argument("--agy-cli-data-dir", type=Path, default=None, help="Override Antigravity CLI data directory (defaults to $ANTIGRAVITY_CLI_DATA_DIR or ~/.gemini/antigravity-cli if present).")
    parser.add_argument("--gemini-config-dir", type=Path, default=None, help="Override Gemini config directory (defaults to $GEMINI_CONFIG_DIR or ~/.gemini/config).")
    parser.add_argument("--codex-home-dir", type=Path, default=None, help="Override Codex home directory (defaults to $CODEX_HOME or ~/.codex).")
    parser.add_argument("--sync-state-dir", type=Path, default=None, help="Override sync cursor and backup state directory.")

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    pair_p = subparsers.add_parser("pair", help="Ensure bidirectional project bindings for a workspace.")
    pair_p.add_argument("--workspace", "-w", type=str, default=None, help="Workspace path or file:// URI.")

    status_p = subparsers.add_parser("status", help="Show workspace project bindings and discovered sessions.")
    status_p.add_argument("--workspace", "-w", type=str, default=None, help="Workspace path or file:// URI.")
    status_p.add_argument("--include-subagents", action="store_true", help="Include spawned subagent sessions in the workspace status listing.")

    inspect_p = subparsers.add_parser("inspect", help="Decode and summarize a session ledger without mutating state.")
    inspect_p.add_argument("--workspace", "-w", type=str, default=None, help="Workspace path or file:// URI.")
    inspect_p.add_argument("--cascade-id", type=str, default=None, help="Antigravity cascade UUID to inspect.")
    inspect_p.add_argument("--codex-thread-id", type=str, default=None, help="Codex thread UUID to inspect.")
    inspect_p.add_argument("--mode", choices=[m.value for m in TransportMode], default=TransportMode.AUTO.value, help="Transport mode.")

    sync_s = subparsers.add_parser(
        "sync-session",
        help="[Advanced/Offline] Synchronize one paired or unpaired session.",
        description="[Advanced/Offline] Synchronize one paired or unpaired session via offline/dual-mode reconciliation.",
    )
    _add_sync_common_args(sync_s)
    sync_s.add_argument("--cascade-id", type=str, default=None, help="Antigravity cascade UUID.")
    sync_s.add_argument("--codex-thread-id", type=str, default=None, help="Codex thread UUID.")
    sync_s.add_argument("--source-tag", choices=[s.name.lower() for s in AgyGuiSafeSource], default="cascade_client", help="Antigravity GUI-safe trajectory source tag.")

    _add_handoff_subparser(subparsers)

    sync_w = subparsers.add_parser(
        "sync-workspace",
        help="[Advanced/Offline] Synchronize all sessions in a workspace.",
        description="[Advanced/Offline] Synchronize all sessions in a workspace via offline/dual-mode reconciliation.",
    )
    _add_sync_common_args(sync_w)
    sync_w.add_argument("--include-subagents", action="store_true", help="Synchronize spawned subagent sessions in addition to top-level sessions.")

    _add_serve_subparser(subparsers)
    _add_menubar_feed_subparser(subparsers)
    return parser


def _add_handoff_subparser(subparsers: Any) -> None:
    handoff = subparsers.add_parser("handoff", help="Hand completed history to a new Desktop session; does not append to an existing target.")
    handoff.add_argument("--workspace", "-w", default=None, help="Workspace path or file:// URI, already registered on both hosts.")
    handoff.add_argument("--direction", "-d", choices=["codex_to_agy", "agy_to_codex"], default=None, help="Optional explicit handoff direction; inferred automatically when omitted.")
    handoff.add_argument("--source-id", default=None, help="Source Codex thread or Antigravity cascade UUID.")
    handoff.add_argument("--title", default=None, help="Title for the new Desktop session, defaulting to the source title.")
    handoff.add_argument("--latest", action="store_true", help="Select the newest completed non-untouched-target session in the workspace.")
    handoff.add_argument("--chain", action="store_true", help="Advance or re-handoff along the workspace's active handoff chain.")
    handoff.add_argument("--pick", action="store_true", help="Interactively choose a candidate session via stderr menu (requires TTY).")
    handoff.add_argument("--pair-if-offline", action="store_true", help="Automatically pair missing offline host registrations before handoff.")
    handoff.add_argument("--auto-pair", action="store_true", help="Automatically pair missing host registrations via online RPC, falling back to locked offline pairing.")
    handoff.add_argument("--include-interrupted", action="store_true", help="Rescue and hand off a stopped trailing interrupted turn (never active streaming turns).")
    handoff.add_argument("--reconcile", action="store_true", help="Verify pending handoff receipts for the workspace and promote ready or clear absent entries.")
    handoff.add_argument("--dry-run", action="store_true", help="Preview --reconcile changes without modifying handoff receipts or staging directories.")
    handoff.add_argument("--json", action="store_true", help="Emit JSON output (stdout output is JSON by default).")


def _add_serve_subparser(subparsers: Any) -> None:
    serve_p = subparsers.add_parser("serve", help="Start the Stage-1 read-only Local Workbench server.")
    serve_p.add_argument("--workspace", "-w", action="append", default=None, help="Seed workspace path or file:// URI (repeatable).")
    serve_p.add_argument("--ui", type=Path, default=None, help="Optional path to custom workbench HTML overriding the packaged default UI.")
    serve_p.add_argument("--port", type=int, default=8765, help="Loopback HTTP port (default: 8765; 0 picks an ephemeral port).")
    serve_p.add_argument("--idle-timeout", type=int, default=1800, help="Idle self-reclaim timeout in seconds (default: 1800; 0 disables).")
    serve_p.add_argument("--max-lifetime", type=int, default=14400, help="Maximum server lifetime in seconds (default: 14400; 0 disables).")
    serve_p.add_argument("--open", action="store_true", help="Open the workbench URL in the default browser after startup.")
    serve_p.add_argument("--status", action="store_true", help="Inspect running Local Workbench server status and exit.")
    serve_p.add_argument("--stop", action="store_true", help="Stop a running Local Workbench server via lease + loopback health check.")


def _add_menubar_feed_subparser(subparsers: Any) -> None:
    feed_p = subparsers.add_parser("menubar-feed", help="Emit a fast JSON snapshot of recent handoff candidates for the menubar app.")
    feed_p.add_argument("--workspace", "-w", action="append", default=None, help="Optional workspace path or file:// URI filter (repeatable).")
    feed_p.add_argument("--limit", type=int, default=6, help="Maximum recent items excluding hero (1..50, default: 6).")


def _resolve_paths(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> HostEnvironmentPaths:
    source_env = env if env is not None else os.environ
    discovered = HostEnvironmentPaths.discover(env=source_env)
    agy_dir = args.agy_data_dir.resolve() if args.agy_data_dir else discovered.agy_data_dir
    if getattr(args, "agy_cli_data_dir", None) is not None:
        cli_dir: Path | None = args.agy_cli_data_dir.resolve()
    elif args.agy_data_dir is not None and not source_env.get("ANTIGRAVITY_CLI_DATA_DIR"):
        sibling_cli = (agy_dir.parent / "antigravity-cli").resolve(strict=False)
        cli_dir = sibling_cli if sibling_cli.is_dir() else None
    else:
        cli_dir = discovered.agy_cli_data_dir
    return HostEnvironmentPaths(
        gemini_config_dir=(args.gemini_config_dir.resolve() if args.gemini_config_dir else discovered.gemini_config_dir),
        agy_data_dir=agy_dir,
        codex_home_dir=(args.codex_home_dir.resolve() if args.codex_home_dir else discovered.codex_home_dir),
        sync_state_dir=(args.sync_state_dir.resolve() if args.sync_state_dir else discovered.sync_state_dir),
        agy_cli_data_dir=cli_dir,
        codex_app_server_socket=discovered.codex_app_server_socket,
    )


def _run_status_cmd(bridge: SyncBridge, workspace_arg: str, include_subagents: bool) -> dict[str, Any]:
    binding = bridge.registry.resolve_readonly_binding(workspace_arg)
    sessions = bridge.registry.list_workspace_sessions(binding, include_subagents=include_subagents)
    diag = diagnose_workspace_binding(bridge.paths, binding.workspace_path)
    handoff_summary = summarize_workspace_handoffs(bridge.paths, binding.workspace_path)
    return {
        "workspace": _binding_to_dict(binding),
        "sessions": [
            {
                "cascade_id": str(s.cascade_id) if s.cascade_id else None,
                "codex_thread_id": str(s.codex_thread_id) if s.codex_thread_id else None,
                "title": s.title,
                "agy_step_count": s.agy_step_count,
                "codex_turn_count": s.codex_turn_count,
                "parent_cascade_id": str(s.parent_cascade_id) if s.parent_cascade_id else None,
                "parent_codex_thread_id": str(s.parent_codex_thread_id) if s.parent_codex_thread_id else None,
                "is_subagent": s.lineage.is_subagent,
                "nesting_depth": s.lineage.nesting_depth,
                "agent_type_name": s.lineage.agent_type_name or None,
                "agent_role": s.lineage.agent_role or None,
            }
            for s in sessions
        ],
        "pairing": {
            "paired": diag.is_paired(),
            "agy_registered": diag.agy_registered,
            "codex_registered": diag.codex_registered,
            "agy_pid": str(diag.agy_project_id) if diag.agy_project_id else None,
            "codex_pid": str(diag.codex_project_id) if diag.codex_project_id else None,
            "agy_project_id": str(diag.agy_project_id) if diag.agy_project_id else None,
            "codex_project_id": str(diag.codex_project_id) if diag.codex_project_id else None,
            "agy_daemon_active": diag.agy_daemon_active,
            "codex_daemon_active": diag.codex_daemon_active,
            "remediation_steps": list(diag.remediation_steps),
        },
        "handoff_chains": handoff_summary["handoff_chains"],
        "pending_handoffs": handoff_summary["pending_handoffs"],
    }


def _run_inspect_cmd(bridge: SyncBridge, workspace_arg: str, args: argparse.Namespace) -> dict[str, Any]:
    ledger = bridge.inspect_session_ledger(
        workspace_arg,
        cascade_id=CascadeId(args.cascade_id) if args.cascade_id else None,
        codex_thread_id=CodexThreadId(args.codex_thread_id) if args.codex_thread_id else None,
        mode=TransportMode(args.mode),
    )
    wm = ledger.latest_watermark()
    return {
        "workspace_path": str(ledger.workspace_path),
        "workspace_uri": str(ledger.workspace_uri),
        "canonical_title": ledger.title,
        "total_events": len(ledger.events),
        "completed_turns": len(ledger.completed_turn_watermarks),
        "has_inflight_turn": ledger.has_inflight_turn,
        "counts": {
            "user_inputs": sum(1 for e in ledger.events if isinstance(e, UserInputEvent)),
            "assistant_texts": sum(1 for e in ledger.events if isinstance(e, AssistantTextEvent)),
            "thinking_events": sum(1 for e in ledger.events if isinstance(e, ThinkingEvent)),
            "tool_calls": sum(1 for e in ledger.events if isinstance(e, ToolCallEvent)),
            "tool_results": sum(1 for e in ledger.events if isinstance(e, ToolResultEvent)),
            "checkpoints": sum(1 for e in ledger.events if isinstance(e, CheckpointEvent)),
        },
        "latest_watermark": {
            "turn_id": str(wm.turn_id),
            "boundary_digest": str(wm.boundary_digest),
            "last_agy_step_index": int(wm.last_agy_step_index) if wm.last_agy_step_index is not None else None,
            "last_codex_line_no": int(wm.last_codex_line_no) if wm.last_codex_line_no is not None else None,
        }
        if wm is not None
        else None,
    }


def _validate_cli_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.subcommand == "menubar-feed":
        if not (1 <= int(args.limit) <= 50):
            parser.error("--limit must be between 1 and 50.")
        return
    if args.subcommand == "serve":
        if args.status and args.stop:
            parser.error("--status cannot be combined with --stop.")
        if not (0 <= int(args.port) <= 65535):
            parser.error("--port must be between 0 and 65535.")
        if int(args.idle_timeout) < 0:
            parser.error("--idle-timeout must be >= 0.")
        if int(args.max_lifetime) < 0:
            parser.error("--max-lifetime must be >= 0.")
        return
    if args.subcommand != "handoff":
        return
    if args.dry_run and not args.reconcile:
        parser.error("--dry-run requires --reconcile.")
    if args.reconcile:
        if any([args.latest, args.chain, args.pick, args.pair_if_offline, args.auto_pair]):
            parser.error("--reconcile cannot be combined with --latest, --chain, --pick, --pair-if-offline, or --auto-pair.")
        return
    if args.auto_pair and args.pair_if_offline:
        parser.error("--auto-pair cannot be combined with --pair-if-offline.")
    try:
        validate_handoff_selector_flags(
            source_id=args.source_id,
            latest=bool(args.latest),
            chain=bool(args.chain),
            pick=bool(args.pick),
        )
    except ValueError as exc:
        parser.error(str(exc))


def _run_serve_cmd(paths: HostEnvironmentPaths, args: argparse.Namespace) -> int:
    if args.status:
        print(json.dumps(query_workbench_status(paths), ensure_ascii=False, indent=2))
        return 0
    if args.stop:
        print(json.dumps(stop_workbench_server(paths), ensure_ascii=False, indent=2))
        return 0
    ui_path: Path | None = None
    if args.ui is not None:
        ui_path = Path(args.ui).expanduser()
        if not ui_path.is_file():
            raise FileNotFoundError(f"UI file not found: {args.ui}")
    seeds = [item for raw in (args.workspace or ()) if (item := normalize_workspace_spec(raw))]
    return run_serve_server(
        paths,
        seed_workspaces=seeds,
        ui_path=ui_path,
        port=int(args.port),
        idle_timeout_sec=int(args.idle_timeout),
        max_lifetime_sec=int(args.max_lifetime),
        open_browser=bool(args.open),
    )


def _run_menubar_feed_cmd(paths: HostEnvironmentPaths, args: argparse.Namespace) -> int:
    seeds = [item for raw in (args.workspace or ()) if (item := normalize_workspace_spec(raw))]
    snapshot = build_menubar_feed(paths, limit=int(args.limit), seed_workspaces=seeds)
    print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _run_handoff_cmd(bridge: SyncBridge, workspace_arg: str, args: argparse.Namespace) -> int:
    if args.reconcile:
        result = reconcile_workspace_handoffs(
            bridge.paths,
            workspace_arg,
            source_id=args.source_id,
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    receipt, _ = execute_handoff(
        bridge.paths,
        workspace_arg,
        direction=SyncDirection(args.direction) if args.direction else None,
        source_id=args.source_id,
        title=args.title,
        latest=bool(args.latest),
        chain=bool(args.chain),
        pick=bool(args.pick),
        pair_if_offline=bool(args.pair_if_offline),
        auto_pair=bool(args.auto_pair),
        include_interrupted=bool(args.include_interrupted),
    )
    print(json.dumps(dataclasses.asdict(receipt), ensure_ascii=False, indent=2))
    return 0


def _exception_to_error_payload(exc: Exception, subcommand: str) -> dict[str, Any]:
    if isinstance(exc, (UnpairedWorkspaceError, SyncBusyLockError)):
        return exc.to_error_payload()
    if isinstance(exc, HandoffPendingError):
        return {
            **dataclasses.asdict(exc.receipt),
            "error": "handoff_pending",
            "message": str(exc),
            "cause": exc.receipt.error,
            "receipt": dataclasses.asdict(exc.receipt),
        }
    if isinstance(exc, HandoffCancelledError):
        return {"error": "handoff_cancelled", "message": str(exc)}
    if isinstance(exc, EmptySessionError):
        return {"error": "empty_session", "message": str(exc)}
    if isinstance(exc, OnlineRpcUnavailableError):
        return {"error": "online_rpc_unavailable", "message": str(exc)}
    if isinstance(exc, FileNotFoundError):
        return {"error": "file_not_found", "message": str(exc)}
    if isinstance(exc, ValueError):
        code = "invalid_handoff_request" if subcommand == "handoff" else "invalid_argument"
        return {"error": code, "message": str(exc)}
    return {"error": "runtime_error", "message": str(exc)}


def _dispatch_subcommand(args: argparse.Namespace, paths: HostEnvironmentPaths) -> int:
    if args.subcommand == "serve":
        return _run_serve_cmd(paths, args)
    if args.subcommand == "menubar-feed":
        return _run_menubar_feed_cmd(paths, args)
    bridge = SyncBridge(paths=paths)
    workspace_arg = normalize_workspace_spec(args.workspace) if args.workspace is not None else os.getcwd()
    if args.subcommand == "pair":
        print(json.dumps(_binding_to_dict(bridge.pair_workspace(workspace_arg)), ensure_ascii=False, indent=2))
        return 0
    if args.subcommand == "status":
        print(json.dumps(_run_status_cmd(bridge, workspace_arg, bool(args.include_subagents)), ensure_ascii=False, indent=2))
        return 0
    if args.subcommand == "inspect":
        print(json.dumps(_run_inspect_cmd(bridge, workspace_arg, args), ensure_ascii=False, indent=2))
        return 0
    if args.subcommand == "handoff":
        return _run_handoff_cmd(bridge, workspace_arg, args)
    if args.subcommand == "sync-session":
        receipt = bridge.sync_session(
            workspace_arg,
            cascade_id=CascadeId(args.cascade_id) if args.cascade_id else None,
            codex_thread_id=CodexThreadId(args.codex_thread_id) if args.codex_thread_id else None,
            direction=SyncDirection(args.direction),
            mode=TransportMode(args.mode),
            fork_policy=ForkResolutionPolicy(args.fork_policy),
            source_tag=AgyGuiSafeSource[args.source_tag.upper()],
            include_interrupted=bool(args.include_interrupted),
        )
        print(json.dumps(_receipt_to_dict(receipt), ensure_ascii=False, indent=2))
        return 0
    if args.subcommand == "sync-workspace":
        receipts = bridge.sync_workspace(
            workspace_arg,
            direction=SyncDirection(args.direction),
            mode=TransportMode(args.mode),
            fork_policy=ForkResolutionPolicy(args.fork_policy),
            include_subagents=bool(args.include_subagents),
            include_interrupted=bool(args.include_interrupted),
        )
        print(json.dumps({"synced_count": len(receipts), "receipts": [_receipt_to_dict(r) for r in receipts]}, ensure_ascii=False, indent=2))
        return 0
    return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _validate_cli_args(parser, args)
    try:
        paths = _resolve_paths(args, env=env)
        return _dispatch_subcommand(args, paths)
    except Exception as exc:
        print(json.dumps(_exception_to_error_payload(exc, str(args.subcommand)), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
