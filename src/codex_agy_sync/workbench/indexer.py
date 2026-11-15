"""Read-only transcript indexing, timeline collapse, and Union-Find Work clustering."""

from __future__ import annotations

import concurrent.futures
import contextlib
from dataclasses import dataclass
import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..bridge.handoff_store import HandoffReceipt, parse_ts_epoch_iso
from ..bridge.paths_and_locks import HostEnvironmentPaths, now_iso
from ..bridge.registry import WorkspaceProjectBinding
from ..bridge.transport import DefaultHostTransport
from ..codecs import decode_agy_trajectory, decode_codex_rollout, read_codex_rollout_lines
from ..events import (
    AssistantTextEvent,
    ThinkingEvent,
    ToolCallEvent,
    UserInputEvent,
    is_host_injected_user_event,
)
from ..ledger import TrajectoryLedger
from ..payloads import (
    FileEditPayload,
    ReadFilePayload,
    ReadResourcePayload,
    RunCommandPayload,
    SearchWebPayload,
)
from ..workspace import CodexThreadId, HostKind, WorkspacePath, WorkspaceRootUri, parse_workspace_identity
from .catalog import WorkspaceCatalogBucket, scan_host_catalog
from .discovery import UnionFind, discover_workspaces, load_preloaded_host_tables
from .models import (
    HandoffHopSummary,
    TimelineTurn,
    WorkCard,
    WorkbenchSnapshot,
    WorkspaceCard,
    WorkSessionLink,
)


def _summarize_tool_call(ev: ToolCallEvent) -> str:
    p = ev.payload
    if isinstance(p, RunCommandPayload) and p.command_line.strip():
        return f"{ev.tool_name}: {' '.join(p.command_line.split())[:140]}"
    if isinstance(p, (ReadFilePayload, FileEditPayload)) and p.path.strip():
        return f"{ev.tool_name}: {p.path}"
    if isinstance(p, SearchWebPayload) and p.query.strip():
        return f"{ev.tool_name}: {p.query[:120]}"
    if isinstance(p, ReadResourcePayload) and p.uri.strip():
        return f"{ev.tool_name}: {p.server_name}/{p.uri}"
    summary = " ".join((ev.tool_summary or "").split())
    if summary and summary != ev.tool_name:
        return f"{ev.tool_name}: {summary[:120]}"
    return ev.tool_name


def _ledger_to_turns(
    ledger: TrajectoryLedger,
    host: str,
    session_id: str,
    session_title: str,
    fallback_iso: str,
) -> list[TimelineTurn]:
    turns: list[TimelineTurn] = []
    turn_items = list(ledger.by_turn_id.items())
    for pos, (turn_id, seq_list) in enumerate(turn_items):
        events = [ledger.events[int(s)] for s in seq_list]
        user_parts = [
            ev.query_text.strip()
            for ev in events
            if isinstance(ev, UserInputEvent) and not is_host_injected_user_event(ev) and ev.query_text.strip()
        ]
        asst_parts = [ev.text.strip() for ev in events if isinstance(ev, AssistantTextEvent) and ev.text.strip()]
        tool_parts = [_summarize_tool_call(ev) for ev in events if isinstance(ev, ToolCallEvent)]
        has_thinking = any(isinstance(ev, ThinkingEvent) for ev in events)
        if not (user_parts or asst_parts or tool_parts or has_thinking):
            continue
        ts_iso = next(
            (ev.anchor.timestamp_iso for ev in events if ev.anchor.timestamp_iso and ev.anchor.timestamp_iso != "2026-01-01T00:00:00Z"),
            fallback_iso or (events[0].anchor.timestamp_iso if events else ""),
        )
        turns.append(TimelineTurn(
            turn_id=str(turn_id),
            seq=len(turns) + 1,
            host=host,
            session_id=session_id,
            session_title=session_title,
            timestamp_iso=ts_iso,
            user_text="\n\n".join(user_parts),
            assistant_text="\n\n".join(asst_parts),
            tool_summary="; ".join(tool_parts),
            is_inflight=ledger.has_inflight_turn and pos == len(turn_items) - 1,
            has_thinking=has_thinking,
        ))
    return turns


@dataclass(frozen=True)
class UnhydratedWorkSpec:
    """Deferred single-session work metadata enabling on-demand turn hydration."""

    ws_id: str
    binding: WorkspaceProjectBinding
    is_paired: bool
    session_key: tuple[str, str]
    entry: dict[str, Any]
    work_card_template: WorkCard


def _resolve_session_target_and_stat(
    paths: HostEnvironmentPaths, transport: DefaultHostTransport, entry: Mapping[str, Any],
) -> tuple[Path | None, str | None, int, int]:
    host: HostKind = entry["host"]
    sid = str(entry["session_id"])
    if host == HostKind.ANTIGRAVITY:
        conv_db = paths.agy_data_dir / "conversations" / f"{sid}.db"
        if not conv_db.is_file():
            return None, None, 0, 0
        st = conv_db.stat()
        return conv_db, None, st.st_mtime_ns, st.st_size
    cand = entry.get("rollout_path")
    rollout_path = cand if (isinstance(cand, Path) and cand.is_file()) else None
    probed_title: str | None = None
    if rollout_path is None:
        tid = CodexThreadId(sid)
        rollout_path, probed_title = transport.probe_codex_rollout_and_title(tid)
        if rollout_path is None:
            rollout_path, probed_title = transport.find_codex_rollout_and_title(tid)
    if rollout_path is None or not rollout_path.is_file():
        return None, probed_title, 0, 0
    st = rollout_path.stat()
    return rollout_path, probed_title, st.st_mtime_ns, st.st_size


def _decode_session_turns_from_file(
    transport: DefaultHostTransport,
    binding: WorkspaceProjectBinding,
    host: HostKind,
    sid: str,
    title: str,
    fallback_iso: str,
    target_file: Path,
    probed_title: str | None,
) -> list[TimelineTurn]:
    if host == HostKind.ANTIGRAVITY:
        raw_steps = transport.read_persisted_agy_steps(target_file)
        ledger = decode_agy_trajectory(
            binding.workspace_path, binding.workspace_uri, raw_steps, explicit_title_override=title,
        )
        return _ledger_to_turns(ledger, host.value, sid, title, fallback_iso)
    eff_title = probed_title or title
    ledger = decode_codex_rollout(
        binding.workspace_path, binding.workspace_uri, read_codex_rollout_lines(target_file), explicit_title_override=eff_title,
    )
    return _ledger_to_turns(ledger, host.value, sid, eff_title, fallback_iso)


def _read_session_turns(
    paths: HostEnvironmentPaths,
    transport: DefaultHostTransport,
    binding: WorkspaceProjectBinding,
    entry: Mapping[str, Any],
) -> list[TimelineTurn]:
    host: HostKind = entry["host"]
    sid, title, fallback_iso = str(entry["session_id"]), str(entry["title"]), str(entry["iso"])
    try:
        target_file, probed_title, _, _ = _resolve_session_target_and_stat(paths, transport, entry)
        if target_file is None:
            return []
        return _decode_session_turns_from_file(transport, binding, host, sid, title, fallback_iso, target_file, probed_title)
    except (sqlite3.DatabaseError, OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.SubprocessError):
        return []


def _turns_match_for_cursor(left: TimelineTurn, right: TimelineTurn) -> bool:
    if left.user_text.strip() != right.user_text.strip():
        return False
    la, ra = left.assistant_text.strip(), right.assistant_text.strip()
    return la == ra or (not la and not ra)


def _infer_handoff_prefix_skip(target_turns: Sequence[TimelineTurn], source_turns: Sequence[TimelineTurn]) -> int:
    skip = 0
    for t_turn, s_turn in zip(target_turns, source_turns):
        if t_turn.user_text.strip() != s_turn.user_text.strip():
            break
        s_asst, t_asst = s_turn.assistant_text.strip(), t_turn.assistant_text.strip()
        if s_asst and s_asst not in t_asst and "[Imported " not in t_asst:
            break
        skip += 1
    return skip


def _order_cluster_sessions(
    comp_keys: Sequence[tuple[str, str]],
    sessions_by_key: Mapping[tuple[str, str], dict[str, Any]],
    receipts: Sequence[tuple[str, HandoffReceipt, float]],
) -> list[tuple[str, str]]:
    comp_set = set(comp_keys)
    ready_recs = [rec for _, rec, _ in receipts if rec.status in ("ready", "no_op") and rec.target_id]
    targets = {(r.target_host, str(r.target_id)) for r in ready_recs}
    outgoing: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for rec in ready_recs:
        sk, tk = (rec.source_host, rec.source_id), (rec.target_host, str(rec.target_id))
        if sk in comp_set and tk in comp_set:
            outgoing.setdefault(sk, []).append(tk)

    def _epoch_key(k: tuple[str, str]) -> tuple[float, str, str]:
        return (float(sessions_by_key[k]["epoch"]), k[0], k[1])

    queue = sorted((k for k in comp_keys if k not in targets), key=_epoch_key)
    ordered: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        ordered.append(cur)
        queue.extend(nxt for nxt in outgoing.get(cur, ()) if nxt not in seen)
    ordered.extend(rem for rem in sorted(comp_keys, key=_epoch_key) if rem not in seen)
    return ordered


def _collapse_cluster_timeline(
    ordered_keys: Sequence[tuple[str, str]],
    sessions_by_key: Mapping[tuple[str, str], dict[str, Any]],
    receipts: Sequence[tuple[str, HandoffReceipt, float]],
    cursor_peers: Mapping[tuple[str, str], list[tuple[str, str]]],
) -> tuple[TimelineTurn, ...]:
    incoming_ready = {
        (rec.target_host, str(rec.target_id)): rec
        for _, rec, _ in receipts
        if rec.status in ("ready", "no_op") and rec.target_id
    }
    emitted_raw: dict[tuple[str, str], list[TimelineTurn]] = {}
    merged: list[TimelineTurn] = []
    for key in ordered_keys:
        turns: list[TimelineTurn] = sessions_by_key[key]["turns"]
        skip = 0
        rec = incoming_ready.get(key)
        if rec is not None and (src_key := (rec.source_host, rec.source_id)) in emitted_raw:
            skip = (
                min(len(turns), max(0, int(rec.target_seed_turn_count)))
                if rec.target_seed_turn_count is not None
                else _infer_handoff_prefix_skip(turns, emitted_raw[src_key])
            )
        else:
            for peer_key in cursor_peers.get(key, ()):
                if peer_key in emitted_raw:
                    peer_turns = emitted_raw[peer_key]
                    cand = 0
                    while cand < len(turns) and cand < len(peer_turns) and _turns_match_for_cursor(turns[cand], peer_turns[cand]):
                        cand += 1
                    skip = max(skip, cand)
        emitted_raw[key] = turns
        for t in turns[skip:]:
            merged.append(TimelineTurn(
                turn_id=t.turn_id, seq=len(merged) + 1, host=t.host, session_id=t.session_id,
                session_title=t.session_title, timestamp_iso=t.timestamp_iso,
                user_text=t.user_text, assistant_text=t.assistant_text,
                tool_summary=t.tool_summary, is_inflight=t.is_inflight, has_thinking=t.has_thinking,
            ))
    return tuple(merged)


def classify_session_links(
    comp_keys: Sequence[tuple[str, str]],
    sessions_by_key: Mapping[tuple[str, str], dict[str, Any]],
    sorted_hops: Sequence[tuple[str, HandoffReceipt, float]],
) -> tuple[tuple[WorkSessionLink, ...], set[tuple[str, str]]]:
    ready_recs = [r for _, r, _ in sorted_hops if r.status in ("ready", "no_op") and r.target_id]
    source_keys = {(r.source_host, r.source_id) for r in ready_recs}
    baton_targets = {(r.target_host, str(r.target_id)) for r in ready_recs if (r.target_host, str(r.target_id)) not in source_keys}
    latest_in = {(r.target_host, str(r.target_id)): r for r in ready_recs}
    latest_out: dict[tuple[str, str], HandoffReceipt] = {(r.source_host, r.source_id): r for r in ready_recs}
    for _, r, _ in sorted_hops:
        if r.status == "pending":
            latest_out.setdefault((r.source_host, r.source_id), r)
    untouched = {
        tk for tk, r in latest_in.items()
        if tk in sessions_by_key and (
            (r.target_seed_turn_count is not None and len(sessions_by_key[tk]["turns"]) <= int(r.target_seed_turn_count))
            or (r.target_seed_turn_count is None and len(sessions_by_key[tk]["turns"]) == 0)
        )
    }
    ordered_links = sorted(comp_keys, key=lambda x: (float(sessions_by_key[x]["epoch"]), x[0], x[1]), reverse=True)
    links = tuple(
        WorkSessionLink(
            host=k[0], session_id=k[1], title=str(sessions_by_key[k]["title"]),
            updated_at_iso=str(sessions_by_key[k]["iso"]), turn_or_step_count=int(sessions_by_key[k]["count"]),
            is_baton_target=(k in baton_targets),
            handoff_status=(latest_out[k].status if k in latest_out else (latest_in[k].status if k in latest_in else None)),
            handoff_direction=(("codex_to_agy" if k[0] == HostKind.CODEX.value else "agy_to_codex") if k in latest_out else None),
        )
        for k in ordered_links
    )
    return links, untouched


def build_work_card(
    ws_id: str,
    binding: WorkspaceProjectBinding,
    is_paired: bool,
    comp_keys: Sequence[tuple[str, str]],
    sessions_by_key: Mapping[tuple[str, str], dict[str, Any]],
    receipts: Sequence[tuple[str, HandoffReceipt, float]],
    cursor_peers: Mapping[tuple[str, str], list[tuple[str, str]]],
) -> WorkCard:
    ordered_keys = _order_cluster_sessions(comp_keys, sessions_by_key, receipts)
    sorted_hops = sorted(receipts, key=lambda it: (it[1].created_at_iso, it[1].chain_depth, it[2], it[0]))
    links, untouched_keys = classify_session_links(comp_keys, sessions_by_key, sorted_hops)
    hops = tuple(
        HandoffHopSummary(
            receipt_key=k, source_host=r.source_host, source_id=r.source_id, target_host=r.target_host,
            target_id=r.target_id, status=r.status, created_at_iso=r.created_at_iso,
            chain_id=r.chain_id, chain_depth=r.chain_depth, title=r.title,
        )
        for k, r, _ in sorted_hops
    )
    timeline = _collapse_cluster_timeline(ordered_keys, sessions_by_key, sorted_hops, cursor_peers)
    non_untouched = [k for k in comp_keys if k not in untouched_keys]
    active_pool = [k for k in non_untouched if sessions_by_key[k]["turns"]] or non_untouched or list(comp_keys)
    active_key = max(active_pool, key=lambda k: (float(sessions_by_key[k]["epoch"]), k[0], k[1]))
    newest_key = max(comp_keys, key=lambda k: (float(sessions_by_key[k]["epoch"]), k[0], k[1]))
    target_keys = {(r.target_host, str(r.target_id)) for _, r, _ in sorted_hops if r.target_id}
    origin_keys = sorted(k for k in comp_keys if k not in target_keys) or sorted(comp_keys)
    root_key = origin_keys[0]
    work_id = "work-" + hashlib.sha256(f"{ws_id}:{root_key[0]}:{root_key[1]}".encode("utf-8")).hexdigest()[:16]
    latest_hop = sorted_hops[-1][1] if sorted_hops else None
    updated_iso = max((str(sessions_by_key[k]["iso"]) for k in comp_keys), default="")
    title = next(
        (t for t in (sessions_by_key[active_key]["title"], sessions_by_key[newest_key]["title"]) if t and t != "Untitled Session"),
        "Untitled Session",
    )
    return WorkCard(
        work_id=work_id, workspace_id=ws_id, workspace_path=str(binding.workspace_path), title=title,
        updated_at_iso=updated_iso, active_host=active_key[0], latest_source_id=active_key[1],
        latest_direction=("codex_to_agy" if active_key[0] == HostKind.CODEX.value else "agy_to_codex"),
        chain_id=latest_hop.chain_id if latest_hop else None,
        chain_depth=latest_hop.chain_depth if latest_hop else 0,
        session_count=len(links), turn_count=len(timeline), is_paired=is_paired,
        sessions=links, handoffs=hops, turns=timeline,
    )


def _build_uf_components(
    session_keys: Sequence[tuple[str, str]],
    cursor_pairs: Sequence[tuple[str, str]],
    receipts: Sequence[tuple[str, HandoffReceipt, float]],
) -> tuple[dict[tuple[str, str], list[tuple[str, str]]], dict[tuple[str, str], list[tuple[str, str]]], dict[tuple[str, str], list[tuple[str, HandoffReceipt, float]]]]:
    all_nodes: set[tuple[str, str]] = set(session_keys)
    cursor_peers: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for cid_str, tid_str in cursor_pairs:
        ak, ck = (HostKind.ANTIGRAVITY.value, cid_str), (HostKind.CODEX.value, tid_str)
        all_nodes.update((ak, ck))
        cursor_peers.setdefault(ak, []).append(ck)
        cursor_peers.setdefault(ck, []).append(ak)
    for _, rec, _ in receipts:
        all_nodes.add((rec.source_host, rec.source_id))
        if rec.target_id:
            all_nodes.add((rec.target_host, rec.target_id))
    uf = UnionFind(all_nodes)
    for cid_str, tid_str in cursor_pairs:
        uf.union((HostKind.ANTIGRAVITY.value, cid_str), (HostKind.CODEX.value, tid_str))
    for _, rec, _ in receipts:
        if rec.target_id:
            uf.union((rec.source_host, rec.source_id), (rec.target_host, rec.target_id))
    comp_sessions: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key in session_keys:
        comp_sessions.setdefault(uf.find(key), []).append(key)
    comp_receipts: dict[tuple[str, str], list[tuple[str, HandoffReceipt, float]]] = {}
    for item in receipts:
        comp_receipts.setdefault(uf.find((item[1].source_host, item[1].source_id)), []).append(item)
    return cursor_peers, comp_sessions, comp_receipts


def _cluster_workspace_works(
    ws_id: str,
    binding: WorkspaceProjectBinding,
    is_paired: bool,
    sessions_by_key: Mapping[tuple[str, str], dict[str, Any]],
    cursor_pairs: Sequence[tuple[str, str]],
    receipts: Sequence[tuple[str, HandoffReceipt, float]],
) -> list[WorkCard]:
    cursor_peers, comp_sessions, comp_receipts = _build_uf_components(list(sessions_by_key.keys()), cursor_pairs, receipts)
    works = [
        build_work_card(ws_id, binding, is_paired, keys, sessions_by_key, comp_receipts.get(root, ()), cursor_peers)
        for root, keys in comp_sessions.items()
    ]
    works.sort(key=lambda w: (w.updated_at_iso, w.work_id), reverse=True)
    return works


def _apply_hydrated_entry(entry: Mapping[str, Any], turns: list[TimelineTurn]) -> dict[str, Any]:
    updated = dict(entry)
    updated["turns"] = turns
    if not updated["iso"] and turns:
        last_ts = turns[-1].timestamp_iso
        updated["iso"] = last_ts
        updated["epoch"] = parse_ts_epoch_iso(last_ts)[0]
    if updated["host"] == HostKind.CODEX and turns:
        updated["count"] = max(1, len(turns))
    return updated


def _build_unhydrated_work_card(
    ws_id: str,
    binding: WorkspaceProjectBinding,
    is_paired: bool,
    key: tuple[str, str],
    entry: Mapping[str, Any],
) -> WorkCard:
    work_id = "work-" + hashlib.sha256(f"{ws_id}:{key[0]}:{key[1]}".encode("utf-8")).hexdigest()[:16]
    raw_count = int(entry["count"])
    turn_count = max(1, raw_count) if raw_count > 0 else 0
    title = str(entry["title"] or "").strip() or "Untitled Session"
    iso = str(entry["iso"])
    link = WorkSessionLink(
        host=key[0], session_id=key[1], title=title, updated_at_iso=iso,
        turn_or_step_count=raw_count, is_baton_target=False, handoff_status=None, handoff_direction=None,
    )
    return WorkCard(
        work_id=work_id, workspace_id=ws_id, workspace_path=str(binding.workspace_path), title=title,
        updated_at_iso=iso, active_host=key[0], latest_source_id=key[1],
        latest_direction=("codex_to_agy" if key[0] == HostKind.CODEX.value else "agy_to_codex"),
        chain_id=None, chain_depth=0, session_count=1, turn_count=turn_count, is_paired=is_paired,
        sessions=(link,), handoffs=(), turns=(),
    )


def _splice_snapshot_works(
    base: WorkbenchSnapshot, hydrated: Mapping[str, WorkCard],
) -> WorkbenchSnapshot:
    if not hydrated:
        return base
    works_list = [hydrated.get(w.work_id, w) for w in base.works]
    works_list.sort(key=lambda w: (w.updated_at_iso, w.work_id), reverse=True)
    last_by_ws: dict[str, str] = {}
    for w in works_list:
        cur = last_by_ws.get(w.workspace_id, "")
        if w.updated_at_iso > cur:
            last_by_ws[w.workspace_id] = w.updated_at_iso
    ws_list = [
        WorkspaceCard(
            workspace_id=ws.workspace_id, workspace_path=ws.workspace_path, workspace_uri=ws.workspace_uri,
            name=ws.name, agy_registered=ws.agy_registered, codex_registered=ws.codex_registered,
            is_paired=ws.is_paired, agy_session_count=ws.agy_session_count,
            codex_session_count=ws.codex_session_count, work_count=ws.work_count,
            last_updated_iso=last_by_ws.get(ws.workspace_id, ws.last_updated_iso), diagnosis=ws.diagnosis,
        )
        for ws in base.workspaces
    ]
    ws_list.sort(key=lambda c: (c.last_updated_iso, c.name.lower(), c.workspace_path), reverse=True)
    return WorkbenchSnapshot(
        generated_at_iso=base.generated_at_iso, workspaces=tuple(ws_list), works=tuple(works_list),
        selected_workspace_id=base.selected_workspace_id, stage=base.stage, capabilities=base.capabilities,
    )


class WorkbenchIndexer:
    """Thread-safe read-only indexer discovering workspaces, sessions, and clustered Works."""

    def __init__(
        self,
        paths: HostEnvironmentPaths | None = None,
        *,
        seed_workspaces: Sequence[str | Path] = (),
        ttl_seconds: float = 3.0,
        eager_turn_budget: int = 24,
    ) -> None:
        self._paths = paths or HostEnvironmentPaths.discover()
        self._seed_workspaces = tuple(seed_workspaces)
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._eager_turn_budget = max(0, int(eager_turn_budget))
        self._lock = threading.RLock()
        self._cached_at: float = 0.0
        self._cached_snapshot: WorkbenchSnapshot | None = None
        self._generation: int = 0
        self._turn_cache: dict[tuple[str, str], tuple[int, int, tuple[TimelineTurn, ...]]] = {}
        self._agy_meta_cache: dict[str, tuple[int, int, dict[str, Any], int]] = {}
        self._unhydrated_specs: dict[str, UnhydratedWorkSpec] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._cached_at = 0.0
            self._cached_snapshot = None
            self._generation += 1
            self._unhydrated_specs.clear()
            self._turn_cache.clear()
            self._agy_meta_cache.clear()

    def discover_workspaces(self) -> tuple[tuple[WorkspacePath, WorkspaceRootUri], ...]:
        return discover_workspaces(self._paths, self._seed_workspaces)

    def _read_session_turns_cached(
        self,
        transport: DefaultHostTransport,
        binding: WorkspaceProjectBinding,
        entry: Mapping[str, Any],
        *,
        gen: int | None = None,
    ) -> list[TimelineTurn]:
        host: HostKind = entry["host"]
        sid, title, fallback_iso = str(entry["session_id"]), str(entry["title"]), str(entry["iso"])
        cache_key = (host.value, sid)
        try:
            target_file, probed_title, mtime_ns, size = _resolve_session_target_and_stat(self._paths, transport, entry)
        except (sqlite3.DatabaseError, OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.SubprocessError):
            return []
        if target_file is None:
            return []
        with self._lock:
            cached = self._turn_cache.get(cache_key)
            if cached is not None and cached[0] == mtime_ns and cached[1] == size:
                return list(cached[2])
        try:
            turns = _decode_session_turns_from_file(
                transport, binding, host, sid, title, fallback_iso, target_file, probed_title
            )
        except (sqlite3.DatabaseError, OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.SubprocessError):
            return []
        with self._lock:
            if gen is None or self._generation == gen:
                self._turn_cache[cache_key] = (mtime_ns, size, tuple(turns))
        return turns

    def _normalized_seed_paths(self) -> set[WorkspacePath]:
        seed_paths: set[WorkspacePath] = set()
        for raw_ws in self._seed_workspaces:
            with contextlib.suppress(ValueError):
                seed_paths.add(parse_workspace_identity(raw_ws)[0])
        return seed_paths

    def _build_full_snapshot(self) -> WorkbenchSnapshot:
        transport = DefaultHostTransport(self._paths)
        preloaded = load_preloaded_host_tables(self._paths)
        workspaces = (
            discover_workspaces(self._paths, self._seed_workspaces, preloaded=preloaded)
            if type(self).discover_workspaces is WorkbenchIndexer.discover_workspaces
            else self.discover_workspaces()
        )
        catalog = scan_host_catalog(self._paths, workspaces, transport, self._agy_meta_cache, preloaded=preloaded)
        seed_paths = self._normalized_seed_paths()

        bucket_data: list[tuple[WorkspaceCatalogBucket, dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], list[tuple[str, str]]], dict[tuple[str, str], list[tuple[str, str]]], dict[tuple[str, str], list[tuple[str, HandoffReceipt, float]]]]] = []
        eager_set: set[tuple[str, tuple[str, str]]] = set()
        standalone: list[tuple[tuple[float, str, str], str, tuple[str, str]]] = []
        for ws_path, _ in workspaces:
            bucket = catalog[ws_path]
            sessions_by_key: dict[tuple[str, str], dict[str, Any]] = {
                (e["host"].value, str(e["session_id"])): {**e, "turns": []}
                for e in [*bucket.agy_entries, *bucket.codex_entries]
            }
            cursor_peers, comp_sessions, comp_receipts = _build_uf_components(
                list(sessions_by_key.keys()), bucket.cursor_pairs, bucket.receipts
            )
            bucket_data.append((bucket, sessions_by_key, cursor_peers, comp_sessions, comp_receipts))
            for root, keys in comp_sessions.items():
                if len(keys) > 1 or bool(comp_receipts.get(root)) or bucket.ws_path in seed_paths:
                    eager_set.add((bucket.ws_id, root))
                else:
                    e = sessions_by_key[keys[0]]
                    standalone.append(((float(e["epoch"]), keys[0][0], keys[0][1]), bucket.ws_id, root))

        standalone.sort(key=lambda item: item[0], reverse=True)
        for _, ws_id, root in standalone[: self._eager_turn_budget]:
            eager_set.add((ws_id, root))

        ws_cards: list[WorkspaceCard] = []
        all_works: list[WorkCard] = []
        new_unhydrated: dict[str, UnhydratedWorkSpec] = {}
        for bucket, sessions_by_key, cursor_peers, comp_sessions, comp_receipts in bucket_data:
            ws_works = self._build_bucket_works(
                transport, bucket, sessions_by_key, cursor_peers, comp_sessions, comp_receipts, eager_set, new_unhydrated
            )
            all_works.extend(ws_works)
            ws_cards.append(WorkspaceCard(
                workspace_id=bucket.ws_id, workspace_path=str(bucket.ws_path), workspace_uri=str(bucket.ws_uri),
                name=bucket.diagnosis.workspace_name, agy_registered=bucket.diagnosis.agy_registered,
                codex_registered=bucket.diagnosis.codex_registered, is_paired=bucket.diagnosis.is_paired(),
                agy_session_count=len(bucket.agy_entries), codex_session_count=len(bucket.codex_entries),
                work_count=len(ws_works), last_updated_iso=max((w.updated_at_iso for w in ws_works), default=""),
                diagnosis=bucket.diagnosis.to_dict(),
            ))
        self._unhydrated_specs = new_unhydrated
        ws_cards.sort(key=lambda c: (c.last_updated_iso, c.name.lower(), c.workspace_path), reverse=True)
        all_works.sort(key=lambda w: (w.updated_at_iso, w.work_id), reverse=True)
        return WorkbenchSnapshot(generated_at_iso=now_iso(), workspaces=tuple(ws_cards), works=tuple(all_works))

    def _build_bucket_works(
        self,
        transport: DefaultHostTransport,
        bucket: WorkspaceCatalogBucket,
        sessions_by_key: dict[tuple[str, str], dict[str, Any]],
        cursor_peers: Mapping[tuple[str, str], list[tuple[str, str]]],
        comp_sessions: Mapping[tuple[str, str], list[tuple[str, str]]],
        comp_receipts: Mapping[tuple[str, str], list[tuple[str, HandoffReceipt, float]]],
        eager_set: set[tuple[str, tuple[str, str]]],
        new_unhydrated: dict[str, UnhydratedWorkSpec],
    ) -> list[WorkCard]:
        is_paired = bucket.diagnosis.is_paired()
        ws_works: list[WorkCard] = []
        for root, keys in comp_sessions.items():
            if (bucket.ws_id, root) in eager_set:
                for k in keys:
                    turns = self._read_session_turns_cached(transport, bucket.binding, sessions_by_key[k])
                    sessions_by_key[k] = _apply_hydrated_entry(sessions_by_key[k], turns)
                card = build_work_card(
                    bucket.ws_id, bucket.binding, is_paired, keys, sessions_by_key, comp_receipts.get(root, ()), cursor_peers
                )
            else:
                k = keys[0]
                card = _build_unhydrated_work_card(bucket.ws_id, bucket.binding, is_paired, k, sessions_by_key[k])
                new_unhydrated[card.work_id] = UnhydratedWorkSpec(
                    ws_id=bucket.ws_id, binding=bucket.binding, is_paired=is_paired,
                    session_key=k, entry=dict(sessions_by_key[k]), work_card_template=card,
                )
            ws_works.append(card)
        ws_works.sort(key=lambda w: (w.updated_at_iso, w.work_id), reverse=True)
        return ws_works

    def _hydrate_spec_card(self, transport: DefaultHostTransport, spec: UnhydratedWorkSpec, gen: int) -> WorkCard:
        turns = self._read_session_turns_cached(transport, spec.binding, spec.entry, gen=gen)
        updated = _apply_hydrated_entry(spec.entry, turns)
        return build_work_card(spec.ws_id, spec.binding, spec.is_paired, [spec.session_key], {spec.session_key: updated}, (), {})

    def _hydrate_specs(self, specs: Sequence[UnhydratedWorkSpec], gen: int) -> dict[str, WorkCard]:
        if not specs:
            return {}
        transport = DefaultHostTransport(self._paths)
        if len(specs) == 1:
            card = self._hydrate_spec_card(transport, specs[0], gen)
            return {card.work_id: card}
        hydrated: dict[str, WorkCard] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(specs))) as pool:
            for card in pool.map(lambda s: self._hydrate_spec_card(transport, s, gen), specs):
                hydrated[card.work_id] = card
        return hydrated

    def _ensure_base_snapshot(self, *, refresh: bool = False) -> tuple[WorkbenchSnapshot, int]:
        with self._lock:
            cache_valid = (
                not refresh
                and self._cached_snapshot is not None
                and self._ttl_seconds > 0
                and (time.monotonic() - self._cached_at) < self._ttl_seconds
            )
            if not cache_valid:
                self._cached_snapshot = self._build_full_snapshot()
                self._cached_at = time.monotonic()
            assert self._cached_snapshot is not None
            return self._cached_snapshot, self._generation

    def _commit_hydrated_works(self, base: WorkbenchSnapshot, gen: int, hydrated: Mapping[str, WorkCard]) -> WorkbenchSnapshot:
        if not hydrated:
            return base
        with self._lock:
            if self._generation == gen and self._cached_snapshot is not None:
                for wid in hydrated:
                    self._unhydrated_specs.pop(wid, None)
                self._cached_snapshot = _splice_snapshot_works(self._cached_snapshot, hydrated)
                return self._cached_snapshot
        return _splice_snapshot_works(base, hydrated)

    @staticmethod
    def _filter_snapshot(base: WorkbenchSnapshot, resolved_id: str | None) -> WorkbenchSnapshot:
        if resolved_id is None:
            return base
        return WorkbenchSnapshot(
            generated_at_iso=base.generated_at_iso, workspaces=base.workspaces,
            works=tuple(w for w in base.works if w.workspace_id == resolved_id),
            selected_workspace_id=resolved_id, stage=base.stage, capabilities=base.capabilities,
        )

    def get_snapshot(self, workspace_id: str | None = None, refresh: bool = False) -> WorkbenchSnapshot:
        base, gen = self._ensure_base_snapshot(refresh=refresh)
        sel = (workspace_id or "").strip() or None
        if sel is None:
            return base
        matched_ws = next((w for w in base.workspaces if w.workspace_id == sel or w.workspace_path == sel), None)
        resolved_id = matched_ws.workspace_id if matched_ws is not None else sel
        with self._lock:
            pending = [s for s in self._unhydrated_specs.values() if s.ws_id == resolved_id] if self._generation == gen else []
        if pending:
            hydrated = self._hydrate_specs(pending, gen)
            base = self._commit_hydrated_works(base, gen, hydrated)
        return self._filter_snapshot(base, resolved_id)

    def get_work(self, work_id: str, refresh: bool = False) -> WorkCard:
        cleaned = (work_id or "").strip()
        if not cleaned:
            raise ValueError("work_id is required.")
        base, gen = self._ensure_base_snapshot(refresh=refresh)
        with self._lock:
            spec = self._unhydrated_specs.get(cleaned) if self._generation == gen else None
        if spec is not None:
            hydrated = self._hydrate_specs([spec], gen)
            self._commit_hydrated_works(base, gen, hydrated)
            return hydrated[cleaned]
        for work in base.works:
            if work.work_id == cleaned:
                return work
        raise KeyError(f"Work not found: {cleaned}")

    def get_snapshot_for_search(self, *, workspace_id: str | None = None, scope: str = "all") -> WorkbenchSnapshot:
        snap = self.get_snapshot(workspace_id=workspace_id)
        norm_scope = HostKind.ANTIGRAVITY.value if scope.strip().lower() in {"agy", HostKind.ANTIGRAVITY.value} else scope.strip().lower()
        target_ids = {w.work_id for w in snap.works}
        with self._lock:
            gen = self._generation
            pending = [
                spec for wid, spec in self._unhydrated_specs.items()
                if wid in target_ids and (norm_scope == "all" or spec.session_key[0] == norm_scope)
            ]
        if not pending:
            return snap
        hydrated = self._hydrate_specs(pending, gen)
        updated_base = self._commit_hydrated_works(snap, gen, hydrated)
        return self._filter_snapshot(updated_base, snap.selected_workspace_id)

    def get_all_works(self, refresh: bool = False) -> tuple[WorkCard, ...]:
        return self.get_snapshot(workspace_id=None, refresh=refresh).works
