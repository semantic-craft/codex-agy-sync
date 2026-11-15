"""Handoff workspace summary and pending-receipt reconciliation helpers."""

from __future__ import annotations

import contextlib
import dataclasses
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from ..codecs import decode_agy_trajectory, decode_codex_rollout, decode_codex_rpc_thread_to_ledger, read_codex_rollout_lines
from ..events import CascadeId, CodexThreadId, HostKind, WorkspacePath, WorkspaceRootUri
from ..workspace import parse_workspace_identity, validate_session_id
from . import rpc
from .handoff_store import (
    HandoffReceipt,
    ledger_seed_fingerprint,
    load_handoff_receipt,
    load_workspace_receipts,
    open_handoff_lock,
    parse_ts_epoch_iso,
    save_handoff_receipt,
)
from .paths_and_locks import (
    HostEnvironmentPaths,
    PresenceWalGuard,
    has_live_unix_socket,
    open_readonly_sqlite,
    validated_child_path,
)
from .store_catalog import optional_select_columns_sql, table_column_names
from .transport import DefaultHostTransport

__all__ = [
    "reconcile_workspace_handoffs",
    "summarize_workspace_handoffs",
]


def _sanitize_receipt_entry(receipt: HandoffReceipt) -> dict[str, Any]:
    return {
        "source_host": receipt.source_host,
        "source_id": receipt.source_id,
        "target_host": receipt.target_host,
        "target_id": receipt.target_id,
        "status": receipt.status,
        "title": receipt.title,
        "created_at_iso": receipt.created_at_iso,
        "chain_depth": receipt.chain_depth,
        "chained_from_source_id": receipt.chained_from_source_id,
        "error": receipt.error,
    }


def _sanitize_pending_entry(receipt: HandoffReceipt, receipt_path: Path) -> dict[str, Any]:
    return {
        "source_host": receipt.source_host,
        "source_id": receipt.source_id,
        "target_host": receipt.target_host,
        "target_id": receipt.target_id,
        "status": receipt.status,
        "error": receipt.error,
        "created_at_iso": receipt.created_at_iso,
        "title": receipt.title,
        "receipt_file": receipt_path.name,
    }


def summarize_workspace_handoffs(paths: HostEnvironmentPaths, workspace: str | Path) -> dict[str, Any]:
    """Summarize grouped handoff chains and pending receipts for one workspace."""
    ws_path, _ = parse_workspace_identity(workspace)
    loaded = load_workspace_receipts(paths, ws_path)
    grouped: dict[str, list[tuple[Path, HandoffReceipt, float]]] = {}
    pending_entries: list[tuple[tuple[str, float, str], dict[str, Any]]] = []

    for r_path, rec, mtime in loaded:
        group_key = rec.chain_id or f"single:{r_path.stem}"
        grouped.setdefault(group_key, []).append((r_path, rec, mtime))
        if rec.status == "pending":
            pending_entries.append(((rec.created_at_iso, mtime, r_path.name), _sanitize_pending_entry(rec, r_path)))

    chains: list[tuple[tuple[str, float, str], dict[str, Any]]] = []
    for chain_key, items in grouped.items():
        ordered = sorted(items, key=lambda it: (it[1].chain_depth, it[1].created_at_iso, it[2], it[0].name))
        tip = ordered[-1][1]
        updated_at = max((r.created_at_iso for _, r, _ in ordered if r.created_at_iso), default=tip.created_at_iso)
        max_mtime = max(m for _, _, m in ordered)
        chains.append((
            (updated_at, max_mtime, chain_key),
            {
                "chain_id": str(tip.chain_id) if tip.chain_id else chain_key,
                "tip_status": tip.status,
                "tip_source_host": tip.source_host,
                "tip_source_id": tip.source_id,
                "tip_target_host": tip.target_host,
                "tip_target_id": tip.target_id,
                "depth": int(tip.chain_depth),
                "hop_count": len(ordered),
                "updated_at_iso": updated_at,
                "receipts": [_sanitize_receipt_entry(r) for _, r, _ in ordered],
            },
        ))

    chains.sort(key=lambda item: item[0], reverse=True)
    pending_entries.sort(key=lambda item: item[0], reverse=True)
    return {
        "handoff_chains": [entry for _, entry in chains],
        "pending_handoffs": [entry for _, entry in pending_entries],
    }


def _validated_staging_dir(staging_root: Path, staging_path_str: str) -> Path | None:
    if not staging_path_str:
        return None
    raw = Path(staging_path_str)
    resolved_root = staging_root.resolve(strict=False)
    resolved_stage = raw.resolve(strict=False)
    if resolved_stage == resolved_root or not resolved_stage.is_relative_to(resolved_root):
        return None
    rel = resolved_stage.relative_to(resolved_root)
    return validated_child_path(resolved_root, *rel.parts, label="staging_path")


def _cleanup_staging_dir(staging_root: Path, staging_path_str: str) -> bool:
    safe_stage = _validated_staging_dir(staging_root, staging_path_str)
    if safe_stage is not None and safe_stage.exists():
        shutil.rmtree(safe_stage)
        return True
    return False


def _validated_receipt_file(handoffs_root: Path, receipt_path: Path) -> Path | None:
    resolved_root = handoffs_root.resolve(strict=False)
    resolved_file = receipt_path.resolve(strict=False)
    if resolved_file.parent != resolved_root:
        return None
    return validated_child_path(resolved_root, receipt_path.name, label="receipt_path")


def _staged_agy_step_count(paths: HostEnvironmentPaths, staging_root: Path, receipt: HandoffReceipt, target_id: str) -> int | None:
    safe_stage = _validated_staging_dir(staging_root, receipt.staging_path)
    if safe_stage is None:
        return None
    staged_db = validated_child_path(safe_stage, "conversations", f"{target_id}.db", label="target_id")
    if not staged_db.is_file():
        return None
    with contextlib.suppress(sqlite3.Error, OSError, ValueError):
        staged_transport = DefaultHostTransport(dataclasses.replace(paths, agy_data_dir=safe_stage, agy_cli_data_dir=None))
        return len(staged_transport.read_persisted_agy_steps(staged_db))
    return None


def _verify_agy_pending_receipt(
    paths: HostEnvironmentPaths,
    ws_path: WorkspacePath,
    ws_uri: WorkspaceRootUri,
    receipt: HandoffReceipt,
    staging_root: Path,
) -> tuple[str, str]:
    if not receipt.target_id:
        return ("cleared", "missing_target_id")
    try:
        target_id = validate_session_id(receipt.target_id, label="target_id")
    except ValueError:
        return ("cleared", "invalid_target_id")

    expected_steps = _staged_agy_step_count(paths, staging_root, receipt, target_id)
    guard = PresenceWalGuard(paths)
    agy_ep = guard.discover_agy_daemon()
    rpc_failed = False
    if agy_ep is not None:
        try:
            rpc_res = rpc.fetch_agy_trajectory_via_rpc(agy_ep, CascadeId(target_id), timeout_s=10.0)
            if rpc_res.raw_steps:
                if expected_steps is not None and len(rpc_res.raw_steps) >= expected_steps:
                    return ("promoted", "agy_rpc_step_count_matched")
                if expected_steps is None:
                    decoded = decode_agy_trajectory(ws_path, ws_uri, rpc_res.raw_steps)
                    turns, _ = ledger_seed_fingerprint(decoded)
                    if receipt.target_seed_turn_count is None or turns >= receipt.target_seed_turn_count:
                        return ("promoted", "agy_rpc_verified")
        except (rpc.OnlineRpcUnavailableError, TimeoutError, OSError, ValueError, RuntimeError):
            rpc_failed = True

    target_db = validated_child_path(paths.agy_data_dir, "conversations", f"{target_id}.db", label="target_id")
    target_annot = validated_child_path(paths.agy_data_dir, "annotations", f"{target_id}.pbtxt", label="target_id")
    if target_db.is_file() and target_annot.is_file():
        try:
            disk_steps = DefaultHostTransport(paths).read_persisted_agy_steps(target_db)
        except (sqlite3.Error, OSError, ValueError):
            return ("unresolved", "agy_target_db_unreadable")
        if expected_steps is not None:
            if disk_steps and len(disk_steps) >= expected_steps:
                return ("promoted", "agy_disk_step_count_matched")
            return ("unresolved", "agy_disk_step_count_mismatch")
        decoded = decode_agy_trajectory(ws_path, ws_uri, disk_steps)
        turns, _ = ledger_seed_fingerprint(decoded)
        if disk_steps and (receipt.target_seed_turn_count is None or turns >= receipt.target_seed_turn_count):
            return ("promoted", "agy_disk_verified")
        return ("unresolved", "agy_disk_seed_mismatch")
    if target_db.exists() or target_annot.exists():
        return ("unresolved", "agy_partial_target_files")
    locks = guard.probe_locks(CascadeId(target_id), None)
    if rpc_failed and (locks.agy_presence_locked or locks.agy_wal_active):
        return ("unresolved", "agy_daemon_active_rpc_unavailable")
    return ("cleared", "agy_target_absent")


def _codex_state_row_exists(state_db: Path, target_id: str) -> bool | None:
    if not state_db.is_file():
        return False
    try:
        with contextlib.closing(open_readonly_sqlite(state_db)) as conn:
            return conn.execute("SELECT 1 FROM threads WHERE id = ?", (target_id,)).fetchone() is not None
    except (sqlite3.Error, OSError):
        return None


def _codex_orphan_matches_receipt(
    codex_home_dir: Path, ws_path: WorkspacePath, receipt: HandoffReceipt,
) -> bool | None:
    state_db = codex_home_dir / "state_5.sqlite"
    if not state_db.is_file():
        return False
    receipt_epoch, _ = parse_ts_epoch_iso(receipt.created_at_iso)
    try:
        with contextlib.closing(open_readonly_sqlite(state_db)) as conn:
            cols = table_column_names(conn, "threads")
            if "id" not in cols or "title" not in cols:
                return None
            select_sql = optional_select_columns_sql(cols, ("id", "cwd", "title", "created_at", "created_at_ms"))
            rows = conn.execute(
                f"SELECT {select_sql} FROM threads WHERE id != ? AND title = ?",
                (receipt.source_id, receipt.title),
            ).fetchall()
    except (sqlite3.Error, OSError):
        return None
    for _, cwd, _, c_sec, c_ms in rows:
        if cwd:
            with contextlib.suppress(ValueError):
                if parse_workspace_identity(str(cwd))[0] != ws_path:
                    continue
        if c_ms is None and c_sec is None:
            return True
        row_epoch, _ = parse_ts_epoch_iso(c_ms or c_sec)
        if receipt_epoch <= 0.0 or row_epoch >= receipt_epoch - 5.0:
            return True
    return False


def _verify_codex_pending_receipt(
    paths: HostEnvironmentPaths,
    ws_path: WorkspacePath,
    ws_uri: WorkspaceRootUri,
    receipt: HandoffReceipt,
) -> tuple[str, str]:
    if not receipt.target_id:
        orphan = _codex_orphan_matches_receipt(paths.codex_home_dir, ws_path, receipt)
        if orphan is None:
            return ("unresolved", "codex_state_db_unreadable")
        if orphan:
            return ("unresolved", "codex_candidate_thread_found_without_id")
        return ("cleared", "codex_target_never_allocated")
    try:
        target_id = validate_session_id(receipt.target_id, label="target_id")
    except ValueError:
        return ("cleared", "invalid_target_id")

    target_tid = CodexThreadId(target_id)
    guard = PresenceWalGuard(paths)
    locks = guard.probe_locks(None, target_tid)
    rpc_failed = False
    if locks.codex_daemon is not None and has_live_unix_socket(locks.codex_daemon.socket_path):
        try:
            rpc_res = rpc.fetch_codex_thread_via_rpc(locks.codex_daemon, target_tid, timeout_s=10.0)
            decoded = decode_codex_rpc_thread_to_ledger(ws_path, ws_uri, rpc_res.thread)
            turns, _ = ledger_seed_fingerprint(decoded)
            if decoded.events and (receipt.target_seed_turn_count is None or turns >= receipt.target_seed_turn_count):
                return ("promoted", "codex_rpc_verified")
            return ("unresolved", "codex_rpc_empty_or_incomplete")
        except (rpc.OnlineRpcUnavailableError, TimeoutError, OSError, ValueError, RuntimeError):
            rpc_failed = True

    transport = DefaultHostTransport(paths)
    rollout_path, explicit_title = transport.probe_codex_rollout_and_title(target_tid)
    if rollout_path is None:
        with contextlib.suppress(FileNotFoundError):
            rollout_path, explicit_title = transport.find_codex_rollout_and_title(target_tid)
    in_catalog = _codex_state_row_exists(paths.codex_home_dir / "state_5.sqlite", target_id)
    if in_catalog is None:
        return ("unresolved", "codex_state_db_unreadable")
    if rollout_path is not None and rollout_path.is_file() and in_catalog:
        try:
            decoded = decode_codex_rollout(ws_path, ws_uri, read_codex_rollout_lines(rollout_path), explicit_title_override=explicit_title)
        except (OSError, ValueError):
            return ("unresolved", "codex_rollout_unreadable")
        turns, _ = ledger_seed_fingerprint(decoded)
        if decoded.events and (receipt.target_seed_turn_count is None or turns >= receipt.target_seed_turn_count):
            return ("promoted", "codex_disk_verified")
        return ("unresolved", "codex_disk_seed_mismatch")
    if (rollout_path is not None and rollout_path.is_file()) or in_catalog:
        return ("unresolved", "codex_partial_target_state")
    if rpc_failed and locks.codex_wal_active:
        return ("unresolved", "codex_daemon_wal_active_rpc_unavailable")
    return ("cleared", "codex_target_absent")


def _apply_reconcile_decision(
    handoffs_root: Path,
    staging_root: Path,
    receipt_path: Path,
    receipt: HandoffReceipt,
    decision: str,
    reason: str,
    *,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    entry = {
        "receipt_file": receipt_path.name,
        "source_host": receipt.source_host,
        "source_id": receipt.source_id,
        "target_host": receipt.target_host,
        "target_id": receipt.target_id,
        "title": receipt.title,
        "reason": reason,
        "staging_cleaned": False,
    }
    if dry_run:
        return decision, entry
    if decision == "promoted":
        safe_receipt = _validated_receipt_file(handoffs_root, receipt_path)
        if safe_receipt is None:
            return ("unresolved", {**entry, "reason": "invalid_receipt_path"})
        updated = dataclasses.replace(receipt, status="ready", error=None)
        save_handoff_receipt(safe_receipt, updated)
        entry["staging_cleaned"] = _cleanup_staging_dir(staging_root, receipt.staging_path)
    elif decision == "cleared":
        entry["staging_cleaned"] = _cleanup_staging_dir(staging_root, receipt.staging_path)
        safe_receipt = _validated_receipt_file(handoffs_root, receipt_path)
        if safe_receipt is not None:
            safe_receipt.unlink(missing_ok=True)
    return decision, entry


def reconcile_workspace_handoffs(
    paths: HostEnvironmentPaths,
    workspace: str | Path,
    *,
    source_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verify pending handoff receipts against target hosts and promote ready or clear absent entries."""
    ws_path, ws_uri = parse_workspace_identity(workspace)
    if source_id is not None:
        validate_session_id(source_id, label="source_id")

    promoted: list[dict[str, Any]] = []
    cleared: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    with open_handoff_lock(paths, ws_path) as handoffs_root:
        staging_root = handoffs_root / "staging"
        loaded = load_workspace_receipts(paths, ws_path)
        pending = [
            (r_path, rec)
            for r_path, rec, _ in loaded
            if rec.status == "pending" and (source_id is None or rec.source_id == source_id)
        ]
        total_before = len(pending)
        for r_path, _ in pending:
            fresh = load_handoff_receipt(r_path)
            if fresh.status != "pending":
                continue
            if fresh.target_host == HostKind.ANTIGRAVITY.value:
                decision, reason = _verify_agy_pending_receipt(paths, ws_path, ws_uri, fresh, staging_root)
            elif fresh.target_host == HostKind.CODEX.value:
                decision, reason = _verify_codex_pending_receipt(paths, ws_path, ws_uri, fresh)
            else:
                decision, reason = ("unresolved", f"unsupported_target_host:{fresh.target_host}")
            bucket, entry = _apply_reconcile_decision(
                handoffs_root, staging_root, r_path, fresh, decision, reason, dry_run=dry_run,
            )
            if bucket == "promoted":
                promoted.append(entry)
            elif bucket == "cleared":
                cleared.append(entry)
            else:
                unresolved.append(entry)

        total_after = len([
            1 for _, rec, _ in load_workspace_receipts(paths, ws_path)
            if rec.status == "pending" and (source_id is None or rec.source_id == source_id)
        ])
    return {
        "workspace": str(ws_path),
        "dry_run": dry_run,
        "promoted": promoted,
        "cleared": cleared,
        "unresolved": unresolved,
        "total_pending_before": total_before,
        "total_pending_after": total_after,
    }
