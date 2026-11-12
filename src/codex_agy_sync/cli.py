"""Command-line interface for `codex-agy-sync`.

Exposes five deterministic subcommands backed by `SyncBridge`:
1. `pair`: Register or verify bidirectional project bindings for a workspace folder.
2. `status`: List paired and unpaired sessions in a workspace along with lock/cursor state.
3. `inspect`: Decode an Antigravity cascade or Codex thread into `TrajectoryLedger` summary JSON.
4. `sync-session`: Synchronize a single session idempotently with explicit direction, transport, and fork policy.
5. `sync-workspace`: Discover and synchronize all sessions in a workspace folder.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .bridge import (
    ForkResolutionPolicy,
    HostEnvironmentPaths,
    SyncBridge,
    SyncDirection,
    SyncReceipt,
    TransportMode,
    WorkspaceProjectBinding,
)
from .ledger import (
    AgyGuiSafeSource,
    AssistantTextEvent,
    CascadeId,
    CheckpointEvent,
    CodexThreadId,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserInputEvent,
)


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
    parser.add_argument("--agy-data-dir", type=Path, default=None, help="Override Antigravity data directory (defaults to $ANTIGRAVITY_DATA_DIR or ~/.gemini/antigravity).")
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

    sync_s = subparsers.add_parser("sync-session", help="Synchronize one paired or unpaired session.")
    _add_sync_common_args(sync_s)
    sync_s.add_argument("--cascade-id", type=str, default=None, help="Antigravity cascade UUID.")
    sync_s.add_argument("--codex-thread-id", type=str, default=None, help="Codex thread UUID.")
    sync_s.add_argument("--source-tag", choices=[s.name.lower() for s in AgyGuiSafeSource], default="cascade_client", help="Antigravity GUI-safe trajectory source tag.")

    sync_w = subparsers.add_parser("sync-workspace", help="Synchronize all sessions in a workspace.")
    _add_sync_common_args(sync_w)
    sync_w.add_argument("--include-subagents", action="store_true", help="Synchronize spawned subagent sessions in addition to top-level sessions.")

    return parser


def _resolve_paths(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> HostEnvironmentPaths:
    discovered = HostEnvironmentPaths.discover(env=env)
    return HostEnvironmentPaths(
        gemini_config_dir=(args.gemini_config_dir.resolve() if args.gemini_config_dir else discovered.gemini_config_dir),
        agy_data_dir=(args.agy_data_dir.resolve() if args.agy_data_dir else discovered.agy_data_dir),
        codex_home_dir=(args.codex_home_dir.resolve() if args.codex_home_dir else discovered.codex_home_dir),
        sync_state_dir=(args.sync_state_dir.resolve() if args.sync_state_dir else discovered.sync_state_dir),
    )


def _run_status_cmd(bridge: SyncBridge, workspace_arg: str, include_subagents: bool) -> dict[str, Any]:
    binding = bridge.registry.resolve_readonly_binding(workspace_arg)
    sessions = bridge.registry.list_workspace_sessions(binding, include_subagents=include_subagents)
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


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    bridge = SyncBridge(paths=_resolve_paths(args, env=env))
    if args.workspace is not None:
        raw_ws = str(args.workspace).strip()
        workspace_arg = (
            str(Path(os.path.expandvars(raw_ws)).expanduser().resolve(strict=False))
            if (raw_ws and not raw_ws.startswith("file://") and "://" not in raw_ws)
            else raw_ws
        )
    else:
        workspace_arg = os.getcwd()

    if args.subcommand == "pair":
        print(json.dumps(_binding_to_dict(bridge.pair_workspace(workspace_arg)), ensure_ascii=False, indent=2))
        return 0
    if args.subcommand == "status":
        print(json.dumps(_run_status_cmd(bridge, workspace_arg, bool(args.include_subagents)), ensure_ascii=False, indent=2))
        return 0
    if args.subcommand == "inspect":
        print(json.dumps(_run_inspect_cmd(bridge, workspace_arg, args), ensure_ascii=False, indent=2))
        return 0
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


if __name__ == "__main__":
    sys.exit(main())
