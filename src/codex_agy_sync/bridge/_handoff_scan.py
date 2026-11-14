"""Read-only host store scanners and timestamp helpers for handoff candidate resolution."""

from __future__ import annotations

from collections import deque
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Sequence

from ..codecs import decode_trajectory_metadata_proto, iter_codex_rollout_lines
from ..codecs.tools import safe_json_dict
from ..ledger import CascadeId, CodexThreadId, HostKind, WorkspacePath, parse_workspace_identity, validate_session_id
from .handoff import HandoffPendingError, HandoffReceipt, _receipt_matches_workspace, load_handoff_receipt
from .paths_and_locks import HostEnvironmentPaths, _open_readonly_sqlite, _validated_child_path
from .registry import (
    ProjectRegistry,
    WorkspaceProjectBinding,
    _build_combined_lineage,
    _count_codex_rollout_user_turns,
    _parse_codex_thread_source_lineage,
    _safe_valid_session_id,
)
from .rescue_and_fork import compute_codex_nesting_depth
from .state_machine import SyncDirection
from .transport import DefaultHostTransport


def parse_ts_epoch_iso(raw: Any, *, fallback_mtime: float = 0.0) -> tuple[float, str]:
    """Parse millisecond/second epochs or ISO strings into `(epoch_sec, utc_iso)`."""
    epoch = 0.0
    if isinstance(raw, (int, float)) and raw > 0:
        epoch = float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            num = float(text)
            if num > 0:
                epoch = num / 1000.0 if num > 1e11 else num
        except ValueError:
            with contextlib.suppress(ValueError):
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                epoch = dt.timestamp()
    if epoch <= 0.0 and fallback_mtime > 0.0:
        epoch = fallback_mtime
    iso = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if epoch > 0.0 else ""
    return epoch, iso


def load_workspace_receipts(paths: HostEnvironmentPaths, ws_path: WorkspacePath) -> list[tuple[Path, HandoffReceipt, float]]:
    """Load persisted handoff receipts scoped to `ws_path` with file mtimes."""
    state = paths.sync_state_dir / "handoffs"
    if not state.is_dir():
        return []
    out: list[tuple[Path, HandoffReceipt, float]] = []
    for p in sorted(state.glob("*.json")):
        if not p.is_file():
            continue
        try:
            receipt = load_handoff_receipt(p)
            mtime = p.stat().st_mtime
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            continue
        if _receipt_matches_workspace(receipt.workspace, ws_path):
            out.append((p, receipt, mtime))
    return out


def discover_agy_entries(
    paths: HostEnvironmentPaths,
    registry: ProjectRegistry,
    transport: DefaultHostTransport,
    binding: WorkspaceProjectBinding,
) -> list[dict[str, Any]]:
    sidecars = registry._discover_agy_subagent_sidecars()
    agy_sessions = registry._scan_agy_summaries_db(binding, sidecars)
    sum_mtimes: dict[str, Any] = {}
    if (sum_db := paths.agy_data_dir / "conversation_summaries.db").is_file():
        with contextlib.suppress(sqlite3.OperationalError):
            with contextlib.closing(_open_readonly_sqlite(sum_db)) as conn:
                for cid_val, lm_val in conn.execute("SELECT conversation_id, last_modified_time FROM conversation_summaries"):
                    if cid_val:
                        sum_mtimes[str(cid_val)] = lm_val
    if (conv_dir := paths.agy_data_dir / "conversations").is_dir():
        for db_file in sorted(conv_dir.glob("*.db")):
            registry._enrich_agy_session_from_conv_db(db_file, binding, sidecars, agy_sessions)
    results: list[dict[str, Any]] = []
    for cid_str, info in agy_sessions.items():
        if _build_combined_lineage(info, {}).is_subagent:
            continue
        db_path = _validated_child_path(paths.agy_data_dir, "conversations", f"{cid_str}.db", label="cascade_id")
        if not db_path.is_file():
            continue
        mtime = db_path.stat().st_mtime
        epoch, iso = parse_ts_epoch_iso(sum_mtimes.get(cid_str), fallback_mtime=mtime)
        explicit_title = transport._read_agy_explicit_title(CascadeId(cid_str))
        results.append({
            "host": HostKind.ANTIGRAVITY, "session_id": cid_str, "direction": SyncDirection.AGY_TO_CODEX,
            "title": explicit_title or str(info.get("title") or "Untitled Session"),
            "count": int(info.get("step_count") or 0), "epoch": epoch, "iso": iso,
        })
    return results


def codex_rollout_fallback_entries(
    paths: HostEnvironmentPaths,
    binding: WorkspaceProjectBinding,
    skip_ids: set[str],
    transport: DefaultHostTransport,
) -> list[dict[str, Any]]:
    sessions_dir = paths.codex_home_dir / "sessions"
    if not sessions_dir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for rpath in sorted({*sessions_dir.rglob("rollout-*.jsonl"), *sessions_dir.rglob("rollout-*.jsonl.zst")}):
        meta_payload: dict[str, Any] | None = None
        for row in iter_codex_rollout_lines(rpath):
            rec = safe_json_dict(row.raw_json)
            if rec.get("type") == "session_meta" and isinstance(rec.get("payload"), dict):
                meta_payload = rec["payload"]
                break
        if meta_payload is None:
            continue
        tid_str = _safe_valid_session_id(meta_payload.get("id"), "codex_thread_id")
        if not tid_str or tid_str in skip_ids:
            continue
        ws_ok = False
        if meta_payload.get("cwd"):
            with contextlib.suppress(ValueError):
                ws_ok = parse_workspace_identity(str(meta_payload["cwd"]))[0] == binding.workspace_path
        src_raw = meta_payload.get("source")
        src_is_sub, _, _, _, _ = _parse_codex_thread_source_lineage(
            json.dumps(src_raw) if isinstance(src_raw, dict) else (str(src_raw) if src_raw else None)
        )
        if not ws_ok or src_is_sub:
            continue
        skip_ids.add(tid_str)
        _, probed_title = transport._probe_codex_rollout_and_title(CodexThreadId(tid_str))
        epoch, iso = parse_ts_epoch_iso(meta_payload.get("timestamp"), fallback_mtime=rpath.stat().st_mtime)
        entries.append({
            "host": HostKind.CODEX, "session_id": tid_str, "direction": SyncDirection.CODEX_TO_AGY,
            "title": probed_title or "Untitled Session",
            "count": _count_codex_rollout_user_turns(str(rpath)), "epoch": epoch, "iso": iso,
        })
    return entries


def discover_codex_entries(
    paths: HostEnvironmentPaths,
    binding: WorkspaceProjectBinding,
    transport: DefaultHostTransport,
) -> list[dict[str, Any]]:
    seen_or_archived: set[str] = set()
    entries: list[dict[str, Any]] = []
    if (state_db := paths.codex_home_dir / "state_5.sqlite").is_file():
        with contextlib.suppress(sqlite3.OperationalError):
            with contextlib.closing(_open_readonly_sqlite(state_db)) as conn:
                rows = conn.execute(
                    "SELECT id, title, preview, cwd, project_id, attached_thread_parent_id, rollout_path, "
                    "agent_nickname, agent_role, source, recency_at_ms, updated_at_ms, updated_at, archived FROM threads"
                ).fetchall()
            for tid, title, preview, cwd, pid, ptid, rpath, nick, role, src, rec_ms, upd_ms, upd, archived in rows:
                tid_str = _safe_valid_session_id(tid, "codex_thread_id")
                if not tid_str:
                    continue
                seen_or_archived.add(tid_str)
                if archived:
                    continue
                ws_ok = str(pid or "") == str(binding.codex_project_id)
                if not ws_ok and cwd:
                    with contextlib.suppress(ValueError):
                        ws_ok = parse_workspace_identity(str(cwd))[0] == binding.workspace_path
                if not ws_ok:
                    continue
                src_sub, src_parent, src_depth, src_type, src_nick = _parse_codex_thread_source_lineage(str(src) if src else None)
                parent_str = _safe_valid_session_id(str(ptid or "").strip() or src_parent, "parent_codex_thread_id")
                if compute_codex_nesting_depth(src, src_sub, src_depth, parent_str, str(role or "").strip() or src_type, str(nick or "").strip() or src_nick) > 0:
                    continue
                rp = Path(str(rpath)) if rpath else None
                mtime = rp.stat().st_mtime if (rp and rp.is_file()) else 0.0
                epoch, iso = parse_ts_epoch_iso(rec_ms or upd_ms or upd, fallback_mtime=mtime)
                entries.append({
                    "host": HostKind.CODEX, "session_id": tid_str, "direction": SyncDirection.CODEX_TO_AGY,
                    "title": str(title or preview or "").strip() or "Untitled Session",
                    "count": _count_codex_rollout_user_turns(str(rpath or "")), "epoch": epoch, "iso": iso,
                })
    entries.extend(codex_rollout_fallback_entries(paths, binding, seen_or_archived, transport))
    return entries


def _ws_path_matches(raw_val: Any, target_ws: WorkspacePath) -> bool:
    text = str(raw_val or "").strip()
    if not text:
        return False
    with contextlib.suppress(ValueError):
        return parse_workspace_identity(text)[0] == target_ws
    return False


def _parse_summary_uris(raw_uris: Any) -> list[str]:
    text = str(raw_uris or "").strip()
    if not text:
        return []
    if text.startswith("["):
        with contextlib.suppress(ValueError):
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(u).strip() for u in parsed if str(u).strip()]
    return [text]


def _probe_agy_ownership(
    paths: HostEnvironmentPaths, source_id: str, binding: WorkspaceProjectBinding | None,
) -> tuple[bool, Literal["match", "foreign", "unknown"]]:
    conv_db = _validated_child_path(paths.agy_data_dir, "conversations", f"{source_id}.db", label="cascade_id")
    exists = conv_db.is_file()
    has_meta = matches = False
    if exists and binding is not None:
        with contextlib.suppress(sqlite3.OperationalError):
            with contextlib.closing(_open_readonly_sqlite(conv_db)) as conn:
                tblob = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
            if tblob and tblob[0]:
                meta = decode_trajectory_metadata_proto(bytes(tblob[0]))
                pid = str(meta.get("project_id") or "").strip()
                uris = [str(u).strip() for u in meta.get("workspace_uris", ()) if str(u).strip()]
                has_meta = has_meta or bool(pid or uris)
                matches = matches or pid == str(binding.agy_project_id) or ProjectRegistry._uris_match_workspace(uris, binding.workspace_uri)
    if (sum_db := paths.agy_data_dir / "conversation_summaries.db").is_file():
        with contextlib.suppress(sqlite3.OperationalError):
            with contextlib.closing(_open_readonly_sqlite(sum_db)) as conn:
                row = conn.execute("SELECT project_id, workspace_uris FROM conversation_summaries WHERE conversation_id = ?", (source_id,)).fetchone()
            if row is not None:
                exists = True
                if binding is not None:
                    pid = str(row[0] or "").strip()
                    uris = _parse_summary_uris(row[1])
                    has_meta = has_meta or bool(pid or uris)
                    matches = matches or pid == str(binding.agy_project_id) or ProjectRegistry._uris_match_workspace(uris, binding.workspace_uri)
    if (sidecar := ProjectRegistry(paths)._discover_agy_subagent_sidecars().get(source_id)) is not None:
        exists = True
        if binding is not None:
            sc_uris = [str(u).strip() for u in sidecar.get("workspace_uris", ()) if str(u).strip()]
            has_meta = has_meta or bool(sc_uris)
            matches = matches or ProjectRegistry._uris_match_workspace(sc_uris, binding.workspace_uri)
    if binding is None:
        return exists, "unknown"
    return exists, ("match" if matches else ("foreign" if has_meta else "unknown"))


def _probe_codex_ownership(
    paths: HostEnvironmentPaths, source_id: str, binding: WorkspaceProjectBinding | None, transport: DefaultHostTransport,
) -> tuple[bool, Literal["match", "foreign", "unknown"]]:
    exists = has_meta = matches = False
    if (state_db := paths.codex_home_dir / "state_5.sqlite").is_file():
        with contextlib.suppress(sqlite3.OperationalError):
            with contextlib.closing(_open_readonly_sqlite(state_db)) as conn:
                row = conn.execute("SELECT project_id, cwd FROM threads WHERE id = ?", (source_id,)).fetchone()
            if row is not None:
                exists = True
                if binding is not None:
                    pid_str, cwd_str = str(row[0] or "").strip(), str(row[1] or "").strip()
                    has_meta = has_meta or bool(pid_str or cwd_str)
                    if pid_str in {str(binding.codex_project_id), str(binding.codex_legacy_project_id)} or _ws_path_matches(cwd_str, binding.workspace_path):
                        matches = True
    rpath: Path | None = None
    with contextlib.suppress(FileNotFoundError):
        rpath, _ = transport._find_codex_rollout_and_title(CodexThreadId(source_id))
    if rpath is not None:
        exists = True
        if binding is not None:
            for row in iter_codex_rollout_lines(rpath):
                rec = safe_json_dict(row.raw_json)
                if rec.get("type") == "session_meta" and isinstance(rec.get("payload"), dict):
                    cwd_str = str(rec["payload"].get("cwd") or "").strip()
                    if cwd_str:
                        has_meta = True
                        matches = matches or _ws_path_matches(cwd_str, binding.workspace_path)
                    break
    if binding is None:
        return exists, "unknown"
    return exists, ("match" if matches else ("foreign" if has_meta else "unknown"))


def assert_source_belongs_to_binding(
    paths: HostEnvironmentPaths,
    binding: WorkspaceProjectBinding,
    direction: SyncDirection,
    source_id: str,
) -> None:
    """Reject `source_id` when explicit host store metadata places it in another workspace."""
    validate_session_id(source_id, label="source_id")
    if direction == SyncDirection.CODEX_TO_AGY:
        _, ownership = _probe_codex_ownership(paths, source_id, binding, DefaultHostTransport(paths))
    else:
        _, ownership = _probe_agy_ownership(paths, source_id, binding)
    if ownership == "foreign":
        raise ValueError(f"Session ID {source_id!r} does not belong to workspace {str(binding.workspace_path)!r}.")


def infer_direction_from_source_id(
    paths: HostEnvironmentPaths,
    source_id: str,
    binding: WorkspaceProjectBinding | None = None,
) -> SyncDirection:
    """Infer handoff direction by checking whether `source_id` belongs to `binding` in Codex or Antigravity."""
    validate_session_id(source_id, label="source_id")
    transport = DefaultHostTransport(paths)
    agy_exists, agy_own = _probe_agy_ownership(paths, source_id, binding)
    codex_exists, codex_own = _probe_codex_ownership(paths, source_id, binding, transport)
    if binding is None:
        agy_found, codex_found = agy_exists, codex_exists
    else:
        agy_found = agy_exists and agy_own != "foreign"
        codex_found = codex_exists and codex_own != "foreign"
        if not agy_found and not codex_found and (agy_own == "foreign" or codex_own == "foreign"):
            raise ValueError(f"Session ID {source_id!r} does not belong to workspace {str(binding.workspace_path)!r}.")
    if agy_found and codex_found:
        raise ValueError(f"Session ID {source_id!r} exists in both Codex and Antigravity; specify --direction explicitly.")
    if not agy_found and not codex_found:
        raise ValueError(f"Session ID {source_id!r} was not found in Codex or Antigravity stores.")
    return SyncDirection.CODEX_TO_AGY if codex_found else SyncDirection.AGY_TO_CODEX


def _pending_maps(
    receipts: Sequence[tuple[Path, HandoffReceipt, float]],
) -> tuple[dict[tuple[str, str], tuple[Path, HandoffReceipt, float]], dict[tuple[str, str], tuple[Path, HandoffReceipt, float]]]:
    by_src: dict[tuple[str, str], tuple[Path, HandoffReceipt, float]] = {}
    by_tgt: dict[tuple[str, str], tuple[Path, HandoffReceipt, float]] = {}
    for p, rec, mtime in receipts:
        if rec.status != "pending":
            continue
        sk = (rec.source_host, rec.source_id)
        if sk not in by_src or (rec.created_at_iso, mtime) > (by_src[sk][1].created_at_iso, by_src[sk][2]):
            by_src[sk] = (p, rec, mtime)
        if rec.target_id:
            tk = (rec.target_host, rec.target_id)
            if tk not in by_tgt or (rec.created_at_iso, mtime) > (by_tgt[tk][1].created_at_iso, by_tgt[tk][2]):
                by_tgt[tk] = (p, rec, mtime)
    return by_src, by_tgt


def _raise_if_nodes_pending(
    nodes: Sequence[tuple[str, str]],
    by_src: dict[tuple[str, str], tuple[Path, HandoffReceipt, float]],
    by_tgt: dict[tuple[str, str], tuple[Path, HandoffReceipt, float]],
) -> None:
    for key in nodes:
        hit = by_src.get(key) or by_tgt.get(key)
        if hit is not None:
            raise HandoffPendingError(hit[1], hit[0])


def _walk_ready_dag(
    ready_items: Sequence[tuple[Path, HandoffReceipt, float]],
    start_items: Sequence[tuple[Path, HandoffReceipt, float]],
) -> tuple[list[tuple[HandoffReceipt, float, int]], list[tuple[str, str]], set[Path]]:
    queue = deque((it, 1, frozenset({(it[1].source_host, it[1].source_id)})) for it in start_items)
    enqueued: set[Path] = {it[0] for it in start_items}
    terminals: list[tuple[HandoffReceipt, float, int]] = []
    visited: set[Path] = set()
    nodes: list[tuple[str, str]] = []
    while queue:
        (p, cur, mtime), hops, path_nodes = queue.popleft()
        visited.add(p)
        nodes.append((cur.source_host, cur.source_id))
        if cur.target_id:
            tgt_key = (cur.target_host, cur.target_id)
            nodes.append(tgt_key)
            all_nxt = [
                it for it in ready_items
                if it[1].source_host == cur.target_host and it[1].source_id == cur.target_id and it[0] != p
            ]
            if all_nxt and tgt_key not in path_nodes:
                for it in all_nxt:
                    if it[0] not in enqueued:
                        enqueued.add(it[0])
                        queue.append((it, hops + 1, path_nodes | {tgt_key}))
                continue
        terminals.append((cur, mtime, hops))
    return terminals, nodes, visited


def resolve_chain_tip(
    receipts: Sequence[tuple[Path, HandoffReceipt, float]],
    source_id: str | None,
) -> HandoffReceipt | None:
    """Resolve the active tip receipt across workspace handoff chains using recency-first DAG traversal."""
    by_src, by_tgt = _pending_maps(receipts)
    ready_items = [item for item in receipts if item[1].status == "ready"]
    if source_id is not None:
        direct_pending = [
            (p, rec, mtime) for p, rec, mtime in receipts
            if rec.status == "pending" and (rec.source_id == source_id or rec.target_id == source_id)
        ]
        if direct_pending:
            p_path, p_rec, _ = max(direct_pending, key=lambda it: (it[1].created_at_iso, it[2]))
            raise HandoffPendingError(p_rec, p_path)
        if not ready_items:
            return None
        starts = [it for it in ready_items if it[1].source_id == source_id]
        if starts:
            terminals, nodes, _ = _walk_ready_dag(ready_items, starts)
            win_rec, _, _ = max(terminals, key=lambda t: (t[0].chain_depth, t[2], t[0].created_at_iso, t[1]))
            _raise_if_nodes_pending(nodes, by_src, by_tgt)
            return win_rec
        as_target = [it for it in ready_items if it[1].target_id == source_id]
        if not as_target:
            return None
        _, win_rec, _ = max(as_target, key=lambda it: (it[1].chain_depth, it[1].created_at_iso, it[2]))
        _raise_if_nodes_pending([(win_rec.source_host, win_rec.source_id), (win_rec.target_host, win_rec.target_id or "")], by_src, by_tgt)
        return win_rec

    if not ready_items:
        return None
    target_keys = {(r.target_host, r.target_id) for _, r, _ in ready_items if r.target_id}
    roots = [it for it in ready_items if (it[1].source_host, it[1].source_id) not in target_keys]
    seen_paths: set[Path] = set()
    components: list[tuple[tuple[HandoffReceipt, float, int], list[tuple[str, str]]]] = []
    for seed in [*roots, *ready_items]:
        if seed in roots or seed[0] not in seen_paths:
            terminals, nodes, visited = _walk_ready_dag(ready_items, [seed])
            seen_paths.update(visited)
            comp_tip = max(terminals, key=lambda t: (t[0].created_at_iso, t[1], t[0].chain_depth, t[2]))
            components.append((comp_tip, nodes))
    win_tip, win_nodes = max(components, key=lambda c: (c[0][0].created_at_iso, c[0][1], c[0][0].chain_depth, c[0][2]))
    _raise_if_nodes_pending(win_nodes, by_src, by_tgt)
    return win_tip[0]
