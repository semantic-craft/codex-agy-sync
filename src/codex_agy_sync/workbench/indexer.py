"""Read-only transcript indexing, timeline collapse, and Union-Find Work clustering."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..bridge.handoff_store import (
    HandoffReceipt,
    discover_agy_entries,
    discover_codex_entries,
    load_workspace_receipts,
    parse_ts_epoch_iso,
)
from ..bridge.paths_and_locks import HostEnvironmentPaths, now_iso
from ..bridge.registry import ProjectRegistry, WorkspaceProjectBinding, diagnose_workspace_binding
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
from ..workspace import CodexThreadId, HostKind, WorkspacePath, WorkspaceRootUri
from .discovery import UnionFind, discover_workspaces
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


def _read_session_turns(
    paths: HostEnvironmentPaths,
    transport: DefaultHostTransport,
    binding: WorkspaceProjectBinding,
    entry: Mapping[str, Any],
) -> list[TimelineTurn]:
    host: HostKind = entry["host"]
    sid, title, fallback_iso = str(entry["session_id"]), str(entry["title"]), str(entry["iso"])
    try:
        if host == HostKind.ANTIGRAVITY:
            conv_db = paths.agy_data_dir / "conversations" / f"{sid}.db"
            if not conv_db.is_file():
                return []
            raw_steps = transport.read_persisted_agy_steps(conv_db)
            ledger = decode_agy_trajectory(
                binding.workspace_path, binding.workspace_uri, raw_steps, explicit_title_override=title,
            )
            return _ledger_to_turns(ledger, host.value, sid, title, fallback_iso)

        tid = CodexThreadId(sid)
        rollout_path, probed_title = transport.probe_codex_rollout_and_title(tid)
        if rollout_path is None:
            rollout_path, probed_title = transport.find_codex_rollout_and_title(tid)
        eff_title = probed_title or title
        ledger = decode_codex_rollout(
            binding.workspace_path, binding.workspace_uri, read_codex_rollout_lines(rollout_path), explicit_title_override=eff_title,
        )
        return _ledger_to_turns(ledger, host.value, sid, eff_title, fallback_iso)
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


def _cluster_workspace_works(
    ws_id: str,
    binding: WorkspaceProjectBinding,
    is_paired: bool,
    sessions_by_key: Mapping[tuple[str, str], dict[str, Any]],
    cursor_pairs: Sequence[tuple[str, str]],
    receipts: Sequence[tuple[str, HandoffReceipt, float]],
) -> list[WorkCard]:
    all_nodes: set[tuple[str, str]] = set(sessions_by_key.keys())
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
    for key in sessions_by_key:
        comp_sessions.setdefault(uf.find(key), []).append(key)
    comp_receipts: dict[tuple[str, str], list[tuple[str, HandoffReceipt, float]]] = {}
    for item in receipts:
        comp_receipts.setdefault(uf.find((item[1].source_host, item[1].source_id)), []).append(item)
    works = [
        build_work_card(ws_id, binding, is_paired, keys, sessions_by_key, comp_receipts.get(root, ()), cursor_peers)
        for root, keys in comp_sessions.items()
    ]
    works.sort(key=lambda w: (w.updated_at_iso, w.work_id), reverse=True)
    return works


class WorkbenchIndexer:
    """Thread-safe read-only indexer discovering workspaces, sessions, and clustered Works."""

    def __init__(
        self,
        paths: HostEnvironmentPaths | None = None,
        *,
        seed_workspaces: Sequence[str | Path] = (),
        ttl_seconds: float = 3.0,
    ) -> None:
        self._paths = paths or HostEnvironmentPaths.discover()
        self._seed_workspaces = tuple(seed_workspaces)
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._lock = threading.RLock()
        self._cached_at: float = 0.0
        self._cached_snapshot: WorkbenchSnapshot | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._cached_at = 0.0
            self._cached_snapshot = None

    def discover_workspaces(self) -> tuple[tuple[WorkspacePath, WorkspaceRootUri], ...]:
        return discover_workspaces(self._paths, self._seed_workspaces)

    def _index_workspace(
        self, ws_path: WorkspacePath, ws_uri: WorkspaceRootUri, registry: ProjectRegistry, transport: DefaultHostTransport,
    ) -> tuple[WorkspaceCard, list[WorkCard]]:
        ws_id = "ws-" + hashlib.sha256(str(ws_uri).encode("utf-8")).hexdigest()[:16]
        diagnosis = diagnose_workspace_binding(self._paths, ws_path)
        binding = registry.resolve_readonly_binding(ws_path)
        try:
            agy_entries = discover_agy_entries(self._paths, transport, binding)
        except (sqlite3.DatabaseError, OSError, ValueError):
            agy_entries = []
        try:
            codex_entries = discover_codex_entries(self._paths, binding, transport)
        except (sqlite3.DatabaseError, OSError, ValueError):
            codex_entries = []
        sessions_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in [*agy_entries, *codex_entries]:
            key = (entry["host"].value, str(entry["session_id"]))
            enriched = dict(entry)
            enriched["turns"] = _read_session_turns(self._paths, transport, binding, entry)
            if not enriched["iso"] and enriched["turns"]:
                last_ts = enriched["turns"][-1].timestamp_iso
                enriched["iso"] = last_ts
                enriched["epoch"] = parse_ts_epoch_iso(last_ts)[0]
            sessions_by_key[key] = enriched
        cursor_pairs = registry.load_sorted_workspace_cursor_pairs(binding.workspace_uri)
        receipts = [(p.stem, r, mtime) for p, r, mtime in load_workspace_receipts(self._paths, ws_path)]
        works = _cluster_workspace_works(ws_id, binding, diagnosis.is_paired(), sessions_by_key, cursor_pairs, receipts)
        last_iso = max((w.updated_at_iso for w in works), default="")
        ws_card = WorkspaceCard(
            workspace_id=ws_id, workspace_path=str(ws_path), workspace_uri=str(ws_uri),
            name=diagnosis.workspace_name, agy_registered=diagnosis.agy_registered,
            codex_registered=diagnosis.codex_registered, is_paired=diagnosis.is_paired(),
            agy_session_count=len(agy_entries), codex_session_count=len(codex_entries),
            work_count=len(works), last_updated_iso=last_iso, diagnosis=diagnosis.to_dict(),
        )
        return ws_card, works

    def _build_full_snapshot(self) -> WorkbenchSnapshot:
        registry, transport = ProjectRegistry(self._paths), DefaultHostTransport(self._paths)
        ws_cards: list[WorkspaceCard] = []
        all_works: list[WorkCard] = []
        for ws_path, ws_uri in self.discover_workspaces():
            ws_card, works = self._index_workspace(ws_path, ws_uri, registry, transport)
            ws_cards.append(ws_card)
            all_works.extend(works)
        ws_cards.sort(key=lambda c: (c.last_updated_iso, c.name.lower(), c.workspace_path), reverse=True)
        all_works.sort(key=lambda w: (w.updated_at_iso, w.work_id), reverse=True)
        return WorkbenchSnapshot(generated_at_iso=now_iso(), workspaces=tuple(ws_cards), works=tuple(all_works))

    def get_snapshot(self, workspace_id: str | None = None, refresh: bool = False) -> WorkbenchSnapshot:
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
            base = self._cached_snapshot
            assert base is not None
        sel = (workspace_id or "").strip() or None
        if sel is None:
            return base
        matched_ws = next((w for w in base.workspaces if w.workspace_id == sel or w.workspace_path == sel), None)
        resolved_id = matched_ws.workspace_id if matched_ws is not None else sel
        return WorkbenchSnapshot(
            generated_at_iso=base.generated_at_iso, workspaces=base.workspaces,
            works=tuple(w for w in base.works if w.workspace_id == resolved_id),
            selected_workspace_id=resolved_id, stage=base.stage, capabilities=base.capabilities,
        )

    def get_all_works(self, refresh: bool = False) -> tuple[WorkCard, ...]:
        return self.get_snapshot(workspace_id=None, refresh=refresh).works
