"""Fast bounded read-only feed snapshot for the Antigravity <-> Codex menubar app."""

from __future__ import annotations

import concurrent.futures, contextlib, http.client, json, os, sqlite3, threading, time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

from ..bridge import (
    DefaultHostTransport, HandoffReceipt, HostEnvironmentPaths, HostLockSnapshot,
    OnlineRpcUnavailableError, PresenceWalGuard, TransportMode, WorkspaceProjectBinding,
    deterministic_uuid, fetch_agy_run_statuses_via_rpc, fetch_agy_trajectory_via_rpc,
    handoff_ledger, ledger_seed_fingerprint, load_handoff_receipt, open_readonly_sqlite,
    validated_child_path,
)
from ..bridge.handoff_store import parse_ts_epoch_iso
from ..bridge.rescue_and_fork import is_source_actively_running
from ..bridge.store_catalog import (
    build_combined_lineage, compute_codex_nesting_depth, discover_agy_subagent_sidecar_ids,
    extract_agy_project_folder_uris, extract_rollout_filename_uuid, parse_codex_thread_source_lineage,
    parse_summary_workspace_uris, safe_valid_session_id,
)
from ..codecs import decode_trajectory_metadata_proto, iter_codex_rollout_lines
from ..events import TaskBoundaryEvent
from ..ledger import TrajectoryLedger, rescue_interrupted_ledger
from ..payloads import safe_json_dict
from ..workspace import (
    FORBIDDEN_AGY_PROJECT_IDS, AgyProjectId, CascadeId, CodexLegacyProjectId, CodexProjectId,
    CodexThreadId, HostKind, WorkspacePath, WorkspaceRootUri, candidate_home_dirs,
    is_excluded_for_homes, parse_workspace_identity, raw_spec_to_candidate_path,
)
from .discovery import PreloadedHostTables, load_preloaded_host_tables

_HERO_WINDOW_SEC = 900.0
_PLAIN_CODEX_SRC = frozenset({"", "cli", "vscode", "exec"})
_NormCache = dict[str, tuple[WorkspacePath, WorkspaceRootUri] | None]


class MenubarCandidateState(str, Enum):
    INTERRUPTED_STOPPED = "interrupted_stopped"
    COMPLETED_READY = "completed_ready"
    PENDING_RECONCILE = "pending_reconcile"
    ALREADY_HANDED = "already_handed"
    ACTIVE_RUNNING = "active_running"


_ACTIONABLE = frozenset({MenubarCandidateState.INTERRUPTED_STOPPED, MenubarCandidateState.COMPLETED_READY, MenubarCandidateState.PENDING_RECONCILE})


@dataclass(frozen=True)
class MenubarSessionItem:
    host: str
    target_host: str
    direction: str
    session_id: str
    title: str
    workspace_path: str
    workspace_name: str
    paired: bool
    state: MenubarCandidateState
    updated_at_iso: str
    sort_epoch: float
    turn_count: int
    has_inflight_turn: bool
    handed_target_id: str | None = None
    pending_error: str | None = None
    chain_depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "state": self.state.value}


@dataclass(frozen=True)
class MenubarFeedSnapshot:
    generated_at_iso: str
    agy_daemon_active: bool
    codex_daemon_active: bool
    hero: MenubarSessionItem | None
    recent: tuple[MenubarSessionItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"generated_at_iso": self.generated_at_iso, "agy_daemon_active": self.agy_daemon_active, "codex_daemon_active": self.codex_daemon_active, "hero": self.hero.to_dict() if self.hero is not None else None, "recent": [i.to_dict() for i in self.recent]}


_WsRec = NamedTuple("_WsRec", [("binding", WorkspaceProjectBinding), ("paired", bool)])
_WsLookup = NamedTuple("_WsLookup", [("by_path", dict[WorkspacePath, _WsRec]), ("by_uri", dict[WorkspaceRootUri, _WsRec]), ("by_agy_pid", dict[str, _WsRec]), ("by_codex_pid", dict[str, _WsRec]), ("known_agy_pids", frozenset[str]), ("homes", tuple[Path, ...]), ("cache", _NormCache)])
_ReceiptIdx = NamedTuple("_ReceiptIdx", [("pending", dict[tuple[str, str, str], HandoffReceipt]), ("ready_sources", dict[tuple[str, str, str], list[HandoffReceipt]]), ("ready_targets", dict[tuple[str, str, str], list[HandoffReceipt]]), ("workspace_specs", tuple[str, ...])])
_RawCand = NamedTuple("_RawCand", [("host", str), ("session_id", str), ("workspace", _WsRec), ("title", str), ("sort_epoch", float), ("updated_at_iso", str)])


def _norm_ws(raw: str | Path, homes: tuple[Path, ...], cache: _NormCache) -> tuple[WorkspacePath, WorkspaceRootUri] | None:
    if not (key := str(raw or "").strip()):
        return None
    if key in cache:
        return cache[key]
    cand, ident = raw_spec_to_candidate_path(key), None
    if cand is not None and not is_excluded_for_homes(cand, homes) and cand.is_dir():
        with contextlib.suppress(ValueError, OSError):
            ident = parse_workspace_identity(cand)
    cache[key] = ident
    return ident


def _load_receipt_index(paths: HostEnvironmentPaths, homes: tuple[Path, ...], cache: _NormCache) -> _ReceiptIdx:
    pend, ready_src, ready_tgt, specs = {}, {}, {}, []
    if (hdir := paths.sync_state_dir / "handoffs").is_dir():
        for hfile in sorted(hdir.glob("*.json")):
            try:
                rec, mtime = load_handoff_receipt(hfile), hfile.stat().st_mtime
            except (OSError, ValueError):
                continue
            if (ident := _norm_ws(rec.workspace, homes, cache)) is None:
                continue
            ws_str = str(ident[0])
            specs.append(ws_str)
            skey, stamp = (ws_str, rec.source_host, rec.source_id), (rec.created_at_iso, mtime)
            if rec.status == "pending" and (skey not in pend or stamp > pend[skey][:2]):
                pend[skey] = (*stamp, rec)
            elif rec.status == "ready":
                ready_src.setdefault(skey, []).append((*stamp, rec))
                if rec.target_id:
                    ready_tgt.setdefault((ws_str, rec.target_host, rec.target_id), []).append(rec)
    if (cdir := paths.sync_state_dir / "cursors").is_dir():
        for cfile in sorted(cdir.glob("*.json")):
            with contextlib.suppress(OSError, ValueError):
                if ws_raw := safe_json_dict(cfile.read_text(encoding="utf-8")).get("workspace_path"):
                    specs.append(str(ws_raw))
    return _ReceiptIdx({k: v[2] for k, v in pend.items()}, {k: [x[2] for x in sorted(v, key=lambda t: t[:2], reverse=True)] for k, v in ready_src.items()}, ready_tgt, tuple(specs))


def _build_workspace_lookup(
    paths: HostEnvironmentPaths, preloaded: PreloadedHostTables, receipts: _ReceiptIdx, seed_workspaces: Sequence[str | Path], homes: tuple[Path, ...], cache: _NormCache,
) -> _WsLookup:
    discovered: dict[WorkspacePath, WorkspaceRootUri] = {}
    agy_pids: dict[WorkspaceRootUri, AgyProjectId] = {}
    known_agy, codex_pids, legacy_pids = set(), {}, {}
    if (pdir := paths.gemini_config_dir / "projects").is_dir():
        for pfile in sorted(pdir.glob("*.json")):
            with contextlib.suppress(OSError, ValueError):
                data = safe_json_dict(pfile.read_text(encoding="utf-8"))
                if (pid := str(data.get("id") or pfile.stem).strip()) and pid not in FORBIDDEN_AGY_PROJECT_IDS:
                    known_agy.add(pid)
                    for uri in extract_agy_project_folder_uris(data):
                        if ident := _norm_ws(uri, homes, cache):
                            discovered[ident[0]] = ident[1]
                            agy_pids.setdefault(ident[1], AgyProjectId(pid))
    for pid_str, raw_root in preloaded.codex_project_roots:
        if pid_str and (ident := _norm_ws(raw_root, homes, cache)):
            discovered[ident[0]] = ident[1]
            codex_pids.setdefault(ident[0], pid_str)
    if (gs := paths.codex_home_dir / ".codex-global-state.json").is_file():
        with contextlib.suppress(OSError, ValueError):
            if isinstance(lp := safe_json_dict(gs.read_text(encoding="utf-8")).get("local-projects"), dict):
                for lpid, entry in lp.items():
                    roots = entry.get("rootPaths") if isinstance(entry, dict) else None
                    if (lpid_s := str(lpid or "").strip()) and isinstance(roots, list):
                        for root in roots:
                            if ident := _norm_ws(str(root), homes, cache):
                                discovered[ident[0]] = ident[1]
                                legacy_pids.setdefault(ident[0], lpid_s)
    if seed_workspaces:
        discovered = {id_[0]: id_[1] for s in seed_workspaces if (id_ := _norm_ws(s, homes, cache))}
    else:
        for spec in (*receipts.workspace_specs, *(str(r[4]) for r in preloaded.codex_thread_rows if r[4] and not r[14]), *(u for r in preloaded.agy_summary_rows if str(r[4] or "").strip() not in FORBIDDEN_AGY_PROJECT_IDS for u in parse_summary_workspace_uris(r[3]))):
            if ident := _norm_ws(spec, homes, cache):
                discovered[ident[0]] = ident[1]
    by_path, by_uri, by_agy_pid, by_codex_pid = {}, {}, {}, {}
    for ws_path, ws_uri in discovered.items():
        ro_agy, ro_codex = agy_pids.get(ws_uri), codex_pids.get(ws_path)
        binding = WorkspaceProjectBinding(ws_path, ws_uri, ws_path.name or "workspace", ro_agy or AgyProjectId(deterministic_uuid("agy-project", str(ws_uri))), CodexProjectId(ro_codex or deterministic_uuid("codex-project", str(ws_uri))), CodexLegacyProjectId(legacy_pids.get(ws_path) or deterministic_uuid("codex-legacy-project", str(ws_uri))))
        rec = _WsRec(binding, ro_agy is not None and ro_codex is not None)
        by_path[ws_path] = by_uri[ws_uri] = rec
        if ro_agy is not None:
            by_agy_pid.setdefault(str(ro_agy), rec)
        if ro_codex is not None:
            by_codex_pid.setdefault(str(ro_codex), rec)
    return _WsLookup(by_path, by_uri, by_agy_pid, by_codex_pid, frozenset(known_agy), homes, cache)


def _match_agy_ws(pid: str, uris: Sequence[str], lookup: _WsLookup) -> _WsRec | None:
    if pid and pid not in FORBIDDEN_AGY_PROJECT_IDS and pid in lookup.by_agy_pid:
        return lookup.by_agy_pid[pid]
    return next((lookup.by_uri[id_[1]] for u in uris if (id_ := _norm_ws(u, lookup.homes, lookup.cache)) and id_[1] in lookup.by_uri), None)


def _verify_agy_candidate(transport: DefaultHostTransport, cid: str, db_path: Path, sort_epoch: float, sm: tuple[Any, ...] | None, lookup: _WsLookup) -> _RawCand | None:
    meta: dict[str, Any] = {}
    try:
        with contextlib.closing(open_readonly_sqlite(db_path)) as conn, contextlib.suppress(sqlite3.DatabaseError):
            if (row := conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()) and row[0]:
                meta = decode_trajectory_metadata_proto(bytes(row[0]))
    except (sqlite3.DatabaseError, OSError, ValueError):
        return None
    if (eff_pid := (sm[2] if sm else "") or str(meta.get("project_id") or "").strip()) in FORBIDDEN_AGY_PROJECT_IDS:
        return None
    eff_parent = safe_valid_session_id((sm[3] if sm else "") or meta.get("parent_conversation_id") or "", "parent_conversation_id")
    eff_depth = max(sm[4] if sm else 0, int(meta.get("nesting_depth") or 0), 1 if eff_parent else 0)
    lineage = build_combined_lineage({"parent_cascade_id": CascadeId(eff_parent) if eff_parent else None, "nesting_depth": eff_depth, "agent_type_name": str(meta.get("agent_type_name") or ""), "agent_role": (sm[5] if sm else "") or str(meta.get("agent_role") or "")}, {})
    if lineage.is_subagent:
        return None
    eff_uris = sm[1] if (sm and sm[1]) else tuple(str(u) for u in meta.get("workspace_uris", []))
    if (ws_rec := _match_agy_ws(eff_pid, eff_uris, lookup)) is None:
        return None
    title = str(transport.read_agy_explicit_title(CascadeId(cid)) or "").strip() or (sm[0] if sm else "") or lineage.agent_role or "Untitled Session"
    raw_ts = sm[7] if (sm and sm[6] >= sort_epoch and sm[6] > 0) else None
    return _RawCand("antigravity", cid, ws_rec, title, sort_epoch, parse_ts_epoch_iso(raw_ts, fallback_mtime=sort_epoch)[1])


def _collect_agy_candidates(
    paths: HostEnvironmentPaths,
    transport: DefaultHostTransport,
    preloaded: PreloadedHostTables,
    lookup: _WsLookup,
    pending_cids: set[str],
    pool_size: int,
    *,
    verify_limit: int | None = None,
) -> list[_RawCand]:
    if not (conv_dir := paths.agy_data_dir / "conversations").is_dir():
        return []
    sidecar_ids = discover_agy_subagent_sidecar_ids(paths)
    sum_by_cid, excluded = {}, set(sidecar_ids)
    for row in preloaded.agy_summary_rows:
        if not (cid := safe_valid_session_id(row[0], "conversation_id")) or cid in sidecar_ids:
            continue
        pid, p_cid = str(row[4] or "").strip(), safe_valid_session_id(row[6], "parent_conversation_id")
        depth, uris = max(int(row[7] or 0), 1 if p_cid else 0), tuple(parse_summary_workspace_uris(row[3]))
        if depth > 0 or pid in FORBIDDEN_AGY_PROJECT_IDS or (_match_agy_ws(pid, uris, lookup) is None and (pid in lookup.known_agy_pids or bool(uris))):
            excluded.add(cid)
            continue
        sum_by_cid[cid] = (str(row[1] or "").strip(), uris, pid, p_cid, depth, str(row[5] or "").strip(), _fast_epoch(row[8]), row[8])
    db_entries: list[tuple[str, Path, float]] = []
    wal_mtime_ns: dict[str, int] = {}
    with os.scandir(conv_dir) as it:
        for entry in it:
            name = entry.name
            if name.endswith(".db"):
                if (cid := safe_valid_session_id(name[:-3], "conversation_id")) and cid not in excluded:
                    with contextlib.suppress(OSError):
                        db_entries.append((cid, Path(entry.path), entry.stat().st_mtime))
            elif name.endswith(".db-wal"):
                if (cid := safe_valid_session_id(name[:-7], "conversation_id")) and cid not in excluded:
                    with contextlib.suppress(OSError):
                        wal_mtime_ns[cid] = entry.stat().st_mtime_ns
    scored: list[tuple[float, str, Path, Any]] = []
    for cid, db_path, db_mtime in db_entries:
        sm = sum_by_cid.get(cid)
        wal_ns = wal_mtime_ns.get(cid, 0)
        scored.append((max(sm[6] if sm else 0.0, db_mtime, wal_ns / 1_000_000_000.0 if wal_ns > 0 else 0.0), cid, db_path, sm))
    scored.sort(key=lambda t: (-t[0], t[1]))
    cap = verify_limit if verify_limit is not None else pool_size
    chosen = scored[:pool_size] + [t for t in scored[pool_size:] if t[1] in pending_cids]
    out: list[_RawCand] = []
    verified_non_pending = 0
    for se, cid, dp, sm in chosen:
        is_pending = cid in pending_cids
        if not is_pending and verified_non_pending >= cap:
            continue
        if (c := _verify_agy_candidate(transport, cid, dp, se, sm, lookup)) is not None:
            out.append(c)
            if not is_pending:
                verified_non_pending += 1
    return out


def _resolve_codex_ws(cwd_raw: str, pid_raw: str, lookup: _WsLookup) -> _WsRec | None:
    if cwd_raw:
        if ident := _norm_ws(cwd_raw, lookup.homes, lookup.cache):
            if ident[0] in lookup.by_path:
                return lookup.by_path[ident[0]]
        elif (cand := raw_spec_to_candidate_path(cwd_raw)) is not None and is_excluded_for_homes(cand, lookup.homes):
            return None
    return lookup.by_codex_pid.get(pid_raw) if pid_raw else None


def _fast_epoch(raw: Any) -> float:
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
    return parse_ts_epoch_iso(raw, fallback_mtime=0.0)[0] if raw else 0.0


def _is_codex_subagent(src: str | None, parent_raw: Any, role_raw: Any, nick_raw: Any) -> bool:
    s_sub, s_par, s_dep, s_typ, s_nck = parse_codex_thread_source_lineage(src)
    par = safe_valid_session_id(parent_raw, "parent_codex_thread_id") if parent_raw else s_par
    return compute_codex_nesting_depth(src, s_sub, s_dep, par, str(role_raw or "").strip() or s_typ, str(nick_raw or "").strip() or s_nck) > 0


def _scan_orphan_codex_rollouts(codex_home: Path, lookup: _WsLookup, known: set[Path], seen: set[str], pool_size: int, cutoff: float) -> list[_RawCand]:
    if not (sessions_dir := codex_home / "sessions").is_dir():
        return []
    files = [(name, Path(dirpath, name), tid) for dirpath, dirs, names in os.walk(sessions_dir) if not dirs.sort(reverse=True) for name in sorted(names, reverse=True) if name.startswith("rollout-") and name.endswith((".jsonl", ".jsonl.zst")) and Path(dirpath, name) not in known and (tid := extract_rollout_filename_uuid(name)) and tid not in seen]
    out: list[_RawCand] = []
    for _, rp, tid in sorted(files, key=lambda t: t[0], reverse=True):
        try:
            st_mtime = rp.stat().st_mtime
        except OSError:
            continue
        if len(out) >= pool_size and st_mtime < cutoff:
            break
        meta: dict[str, Any] | None = None
        with contextlib.suppress(OSError, ValueError):
            for line in iter_codex_rollout_lines(rp):
                if int(line.line_no) > 6:
                    break
                if (obj := safe_json_dict(line.raw_json)).get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                    meta = {"timestamp": obj.get("timestamp"), **obj["payload"]}
                    break
        if meta is None:
            continue
        src = json.dumps(raw_src) if isinstance(raw_src := meta.get("source"), dict) else (str(raw_src) if raw_src is not None else None)
        if _is_codex_subagent(src, None, meta.get("agent_role"), meta.get("agent_nickname")):
            continue
        if (ws_rec := _resolve_codex_ws(str(meta.get("cwd") or ""), "", lookup)) is None or not (eff_tid := safe_valid_session_id(meta.get("id") or tid, "codex_thread_id")):
            continue
        sort_epoch = max(_fast_epoch(meta.get("timestamp")), st_mtime)
        title = next((str(x).strip() for x in (meta.get("title"), meta.get("name")) if x and str(x).strip()), "")
        out.append(_RawCand("codex", eff_tid, ws_rec, title, sort_epoch, parse_ts_epoch_iso(None, fallback_mtime=sort_epoch)[1]))
    return out


def _collect_codex_candidates(paths: HostEnvironmentPaths, preloaded: PreloadedHostTables, lookup: _WsLookup, pending_tids: set[str], pool_size: int) -> list[_RawCand]:
    known, seen, codex_home, pre_scored = set(), set(), paths.codex_home_dir.resolve(strict=False), []
    for row in preloaded.codex_thread_rows:
        if not (tid := str(row[0] or "").strip()):
            continue
        seen.add(tid)
        if rpath_str := str(row[7] or "").strip():
            known.add(Path(rpath_str) if os.path.isabs(rpath_str) else codex_home / rpath_str)
        if not row[14]:
            pre_scored.append((_fast_epoch(row[11] or row[12] or row[13]), tid, row))
    pre_scored.sort(key=lambda t: (-t[0], t[1]))
    out: list[_RawCand] = []
    for epoch, raw_tid, row in pre_scored:
        if len(out) >= pool_size and raw_tid not in pending_tids:
            continue
        src = str(row[10]) if row[10] is not None else None
        if (row[6] or (src and src not in _PLAIN_CODEX_SRC) or row[8] or row[9]) and _is_codex_subagent(src, row[6], row[9], row[8]):
            continue
        if (ws_rec := _resolve_codex_ws(str(row[4] or ""), str(row[5] or ""), lookup)) is None or not (tid := safe_valid_session_id(raw_tid, "codex_thread_id")):
            continue
        if rpath_str := str(row[7] or "").strip():
            with contextlib.suppress(ValueError, OSError):
                epoch = max(epoch, validated_child_path(codex_home, rpath_str).stat().st_mtime)
        title = next((str(x).strip() for x in (row[2], row[1], row[3]) if x and str(x).strip()), "")
        out.append(_RawCand("codex", tid, ws_rec, title, epoch, parse_ts_epoch_iso(None, fallback_mtime=epoch)[1]))
    cutoff = sorted((c.sort_epoch for c in out), reverse=True)[pool_size - 1] if len(out) >= pool_size else 0.0
    return out + _scan_orphan_codex_rollouts(codex_home, lookup, known, seen, pool_size, cutoff)


def _classify_ledger_item(
    cand: _RawCand,
    binding: WorkspaceProjectBinding,
    ledger: TrajectoryLedger,
    is_active: bool,
    receipts: _ReceiptIdx,
) -> MenubarSessionItem | None:
    ws_str = str(binding.workspace_path)
    key, title = (ws_str, cand.host, cand.session_id), cand.title or str(ledger.title or "").strip() or "Untitled Session"
    handed_id, pending_err, chain_depth, inflight, turn_count = None, None, 0, ledger.has_inflight_turn, len(ledger.completed_turn_watermarks)
    if (pending_rec := receipts.pending.get(key)) is not None:
        state, pending_err = MenubarCandidateState.PENDING_RECONCILE, pending_rec.error
    elif is_active:
        state = MenubarCandidateState.ACTIVE_RUNNING
    else:
        eff = rescue_interrupted_ledger(ledger)[0] if inflight else ledger
        if (wm := eff.latest_watermark()) is None:
            return None
        if not inflight and (tgt_recs := receipts.ready_targets.get(key)):
            seed_fp = ledger_seed_fingerprint(handoff_ledger(ledger, None))
            if any((r.target_seed_turn_count, r.target_seed_text_digest) == seed_fp for r in tgt_recs):
                return None
        turn_count = len(eff.completed_turn_watermarks)
        if matched := next((r for r in receipts.ready_sources.get(key, ()) if r.source_digest == str(wm.boundary_digest)), None):
            state, handed_id, chain_depth = MenubarCandidateState.ALREADY_HANDED, matched.target_id, matched.chain_depth
        else:
            state = MenubarCandidateState.INTERRUPTED_STOPPED if inflight else MenubarCandidateState.COMPLETED_READY
    return MenubarSessionItem(
        host=cand.host, target_host="codex" if cand.host == "antigravity" else "antigravity", direction="agy_to_codex" if cand.host == "antigravity" else "codex_to_agy",
        session_id=cand.session_id, title=title, workspace_path=ws_str, workspace_name=binding.workspace_name, paired=cand.workspace.paired, state=state,
        updated_at_iso=cand.updated_at_iso, sort_epoch=cand.sort_epoch, turn_count=turn_count, has_inflight_turn=inflight, handed_target_id=handed_id, pending_error=pending_err, chain_depth=chain_depth,
    )


def _decode_agy_disk_state(
    cand: _RawCand,
    guard: PresenceWalGuard,
    transport: DefaultHostTransport,
) -> tuple[WorkspaceProjectBinding, HostLockSnapshot, TrajectoryLedger] | None:
    binding, cid = cand.workspace.binding, CascadeId(cand.session_id)
    with contextlib.suppress(FileNotFoundError, ValueError, sqlite3.DatabaseError, OSError):
        locks = guard.probe_locks(cid, None)
        ledger, _, _ = transport.read_agy_ledger_with_meta(binding, cid, locks, TransportMode.OFFLINE_STORAGE)
        return binding, locks, ledger
    return None


def _agy_needs_rpc(
    cand: _RawCand,
    locks: HostLockSnapshot,
    ledger: TrajectoryLedger,
    agy_daemon: Any,
    eff_now: float | None,
) -> bool:
    return agy_daemon is not None and agy_daemon.http_port > 0 and (
        ledger.has_inflight_turn or locks.agy_presence_locked or eff_now is None or cand.sort_epoch >= eff_now - _HERO_WINDOW_SEC
    )


def _finalize_agy_item(
    cand: _RawCand,
    binding: WorkspaceProjectBinding,
    locks: HostLockSnapshot,
    ledger: TrajectoryLedger,
    agy_daemon: Any,
    receipts: _ReceiptIdx,
    eff_now: float | None,
    agy_http_conn: http.client.HTTPConnection | None,
    agy_run_statuses: Mapping[str, int] | None,
) -> MenubarSessionItem | None:
    cid, rpc_active, rpc_verified_inactive = CascadeId(cand.session_id), False, False
    if _agy_needs_rpc(cand, locks, ledger, agy_daemon, eff_now):
        if agy_run_statuses is not None and cand.session_id in agy_run_statuses:
            st = agy_run_statuses[cand.session_id]
            rpc_active, rpc_verified_inactive = st in {2, 3, 4}, st == 1
        else:
            with contextlib.suppress(FileNotFoundError, OnlineRpcUnavailableError, ValueError, OSError):
                rpc_res = (
                    fetch_agy_trajectory_via_rpc(agy_daemon, cid, include_steps=False)
                    if agy_http_conn is None
                    else fetch_agy_trajectory_via_rpc(agy_daemon, cid, include_steps=False, conn=agy_http_conn)
                )
                rpc_active = bool(rpc_res.is_active)
                rpc_verified_inactive = rpc_res.cascade_run_status == 1
    is_active = rpc_active or (agy_daemon is None and locks.agy_presence_locked) or (
        ledger.has_inflight_turn and not rpc_verified_inactive and is_source_actively_running(HostKind.ANTIGRAVITY, ledger, locks, rpc_active=False)
    )
    with contextlib.suppress(FileNotFoundError, ValueError, sqlite3.DatabaseError, OSError):
        return _classify_ledger_item(cand, binding, ledger, is_active, receipts)
    return None


def _probe_codex_ledger(
    cand: _RawCand,
    binding: WorkspaceProjectBinding,
    guard: PresenceWalGuard,
    transport: DefaultHostTransport,
    codex_daemon: Any,
) -> tuple[TrajectoryLedger, bool]:
    tid = CodexThreadId(cand.session_id)
    locks = guard.probe_locks(None, tid)
    try:
        ledger, read_mode, rpc_active = transport.read_codex_ledger_with_meta(
            binding, tid, locks, TransportMode.ONLINE_RPC if codex_daemon is not None else TransportMode.OFFLINE_STORAGE,
        )
    except (OnlineRpcUnavailableError, FileNotFoundError, OSError):
        ledger, read_mode, rpc_active = transport.read_codex_ledger_with_meta(binding, tid, locks, TransportMode.OFFLINE_STORAGE)
    has_in_progress_boundary = any(isinstance(ev, TaskBoundaryEvent) and ev.task_status.lower() == "in_progress" for ev in ledger.events)
    rpc_verified_inactive = read_mode == TransportMode.ONLINE_RPC and not rpc_active and not has_in_progress_boundary
    is_active = rpc_active or (has_in_progress_boundary and codex_daemon is not None) or (
        ledger.has_inflight_turn and not rpc_verified_inactive and is_source_actively_running(HostKind.CODEX, ledger, locks, rpc_active=False)
    )
    return ledger, is_active


def _read_and_classify(
    cand: _RawCand,
    guard: PresenceWalGuard,
    transport: DefaultHostTransport,
    agy_daemon: Any,
    codex_daemon: Any,
    receipts: _ReceiptIdx,
    *,
    eff_now: float | None = None,
    agy_http_conn: http.client.HTTPConnection | None = None,
    agy_run_statuses: Mapping[str, int] | None = None,
) -> MenubarSessionItem | None:
    if cand.host == "antigravity":
        if (disk := _decode_agy_disk_state(cand, guard, transport)) is None:
            return None
        return _finalize_agy_item(
            cand, disk[0], disk[1], disk[2], agy_daemon, receipts, eff_now, agy_http_conn, agy_run_statuses,
        )
    binding = cand.workspace.binding
    ledger, is_active = _probe_codex_ledger(cand, binding, guard, transport, codex_daemon)
    return _classify_ledger_item(cand, binding, ledger, is_active, receipts)


def _probe_primary_hosts(
    merged: Sequence[_RawCand],
    agy_primary: Sequence[int],
    codex_primary: Sequence[int],
    guard: PresenceWalGuard,
    transport: DefaultHostTransport,
    codex_daemon: Any,
    probe_one: Callable[[_RawCand, Mapping[str, int] | None], MenubarSessionItem | None],
) -> tuple[dict[int, tuple[WorkspaceProjectBinding, HostLockSnapshot, TrajectoryLedger]], dict[int, MenubarSessionItem | None]]:
    decoded_agy: dict[int, tuple[WorkspaceProjectBinding, HostLockSnapshot, TrajectoryLedger]] = {}
    results: dict[int, MenubarSessionItem | None] = {}

    def _decode_all_agy() -> None:
        for idx in agy_primary:
            if (disk := _decode_agy_disk_state(merged[idx], guard, transport)) is None:
                results[idx] = None
            else:
                decoded_agy[idx] = disk

    if len(codex_primary) > 1 and codex_daemon is not None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(codex_primary))) as cpool:
            cfuts = {idx: cpool.submit(probe_one, merged[idx], None) for idx in codex_primary}
            _decode_all_agy()
            for idx, fut in cfuts.items():
                results[idx] = fut.result()
    else:
        _decode_all_agy()
        for idx in codex_primary:
            results[idx] = probe_one(merged[idx], None)
    return decoded_agy, results


def _finalize_primary_agy(
    merged: Sequence[_RawCand],
    decoded_agy: Mapping[int, tuple[WorkspaceProjectBinding, HostLockSnapshot, TrajectoryLedger]],
    agy_daemon: Any,
    receipts: _ReceiptIdx,
    eff_now: float,
    agy_run_statuses: Mapping[str, int] | None,
    get_conn: Callable[[], http.client.HTTPConnection | None],
) -> dict[int, MenubarSessionItem | None]:
    if not decoded_agy:
        return {}
    fallback_count = sum(
        1 for idx, (_, locks, ledger) in decoded_agy.items()
        if _agy_needs_rpc(merged[idx], locks, ledger, agy_daemon, eff_now)
        and (agy_run_statuses is None or merged[idx].session_id not in agy_run_statuses)
    )
    if agy_run_statuses is None and fallback_count > 1:
        def _run(idx: int, b: WorkspaceProjectBinding, lks: HostLockSnapshot, ldg: TrajectoryLedger) -> MenubarSessionItem | None:
            return _finalize_agy_item(merged[idx], b, lks, ldg, agy_daemon, receipts, eff_now, get_conn(), None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(decoded_agy))) as pool:
            futs = {idx: pool.submit(_run, idx, b, lks, ldg) for idx, (b, lks, ldg) in decoded_agy.items()}
            return {idx: fut.result() for idx, fut in futs.items()}
    main_conn = get_conn() if fallback_count > 0 else None
    return {
        idx: _finalize_agy_item(merged[idx], b, lks, ldg, agy_daemon, receipts, eff_now, main_conn, agy_run_statuses)
        for idx, (b, lks, ldg) in decoded_agy.items()
    }


def _probe_candidates(
    merged: Sequence[_RawCand],
    target_n: int,
    guard: PresenceWalGuard,
    transport: DefaultHostTransport,
    agy_daemon: Any,
    codex_daemon: Any,
    receipts: _ReceiptIdx,
    eff_now: float,
) -> list[MenubarSessionItem]:
    if not merged:
        return []
    tls = threading.local()
    conns: list[http.client.HTTPConnection] = []
    conns_lock = threading.Lock()

    def _get_conn() -> http.client.HTTPConnection | None:
        if agy_daemon is None or agy_daemon.http_port <= 0:
            return None
        if (conn := getattr(tls, "agy_conn", None)) is None:
            conn = http.client.HTTPConnection("127.0.0.1", agy_daemon.http_port, timeout=2.0)
            tls.agy_conn = conn
            with conns_lock:
                conns.append(conn)
        return conn

    def _probe_one(cand: _RawCand, agy_statuses: Mapping[str, int] | None) -> MenubarSessionItem | None:
        agy_conn = _get_conn() if cand.host == "antigravity" else None
        with contextlib.suppress(FileNotFoundError, ValueError, sqlite3.DatabaseError, OSError):
            return _read_and_classify(
                cand, guard, transport, agy_daemon, codex_daemon, receipts,
                eff_now=eff_now, agy_http_conn=agy_conn, agy_run_statuses=agy_statuses,
            )
        return None

    primary_indices = [
        i for i, cand in enumerate(merged)
        if i < target_n or (str(cand.workspace.binding.workspace_path), cand.host, cand.session_id) in receipts.pending
    ]
    agy_primary = [i for i in primary_indices if merged[i].host == "antigravity"]
    codex_primary = [i for i in primary_indices if merged[i].host == "codex"]
    agy_run_statuses: dict[str, int] | None = None
    needs_batch = agy_daemon is not None and agy_daemon.http_port > 0 and any(c.host == "antigravity" for c in merged)
    try:
        batch_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1) if needs_batch else None
        batch_future = (
            batch_executor.submit(fetch_agy_run_statuses_via_rpc, agy_daemon, timeout_s=4.0, conn=None)
            if batch_executor is not None else None
        )
        try:
            decoded_agy, results = _probe_primary_hosts(merged, agy_primary, codex_primary, guard, transport, codex_daemon, _probe_one)
            if batch_future is not None:
                with contextlib.suppress(OnlineRpcUnavailableError, ValueError, OSError):
                    agy_run_statuses = batch_future.result()
        finally:
            if batch_executor is not None:
                batch_executor.shutdown(wait=True)
        results.update(_finalize_primary_agy(merged, decoded_agy, agy_daemon, receipts, eff_now, agy_run_statuses, _get_conn))
        valid_count = sum(1 for item in results.values() if item is not None)
        for idx in range(target_n, len(merged)):
            if valid_count >= target_n:
                break
            if idx not in results:
                results[idx] = (item := _probe_one(merged[idx], agy_run_statuses))
                valid_count += int(item is not None)
    finally:
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.close()
    return [item for idx in sorted(results) if (item := results[idx]) is not None]


def build_menubar_feed(paths: HostEnvironmentPaths, *, limit: int = 6, seed_workspaces: Sequence[str | Path] = (), now_epoch: float | None = None) -> MenubarFeedSnapshot:
    """Build a bounded snapshot of recent Antigravity and Codex sessions for the menubar feed."""
    if not (1 <= int(limit) <= 50):
        raise ValueError(f"limit must be between 1 and 50, got {limit!r}.")
    eff_now = float(now_epoch) if now_epoch is not None else time.time()
    generated_at_iso = datetime.fromtimestamp(eff_now, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    guard, preloaded, transport, homes, norm_cache = (
        PresenceWalGuard(paths),
        load_preloaded_host_tables(paths, include_raw_summary=False),
        DefaultHostTransport(paths),
        candidate_home_dirs(paths),
        {},
    )
    agy_daemon, codex_daemon = guard.discover_agy_daemon(), guard.discover_codex_daemon()
    receipts = _load_receipt_index(paths, homes, norm_cache)
    lookup = _build_workspace_lookup(paths, preloaded, receipts, seed_workspaces, homes, norm_cache)
    pool_size = max(int(limit) * 4, 24)
    verify_limit = max(int(limit) * 2 + 4, 16)
    merged = sorted(
        _collect_agy_candidates(
            paths, transport, preloaded, lookup, {k[2] for k in receipts.pending if k[1] == "antigravity"},
            pool_size, verify_limit=verify_limit,
        )
        + _collect_codex_candidates(paths, preloaded, lookup, {k[2] for k in receipts.pending if k[1] == "codex"}, pool_size),
        key=lambda c: (-c.sort_epoch, c.host, c.session_id),
    )
    items = _probe_candidates(merged, int(limit) + 2, guard, transport, agy_daemon, codex_daemon, receipts, eff_now)
    actionable = [it for it in items if it.state in _ACTIONABLE]
    hero: MenubarSessionItem | None = None
    if actionable:
        newest, interrupted = actionable[0], next((it for it in actionable if it.state is MenubarCandidateState.INTERRUPTED_STOPPED), None)
        hero = interrupted if (interrupted is not None and (interrupted is newest or interrupted.sort_epoch >= eff_now - _HERO_WINDOW_SEC)) else newest
    return MenubarFeedSnapshot(generated_at_iso, agy_daemon is not None, codex_daemon is not None, hero, tuple(it for it in items if it is not hero)[:int(limit)])


__all__ = ["MenubarCandidateState", "MenubarFeedSnapshot", "MenubarSessionItem", "build_menubar_feed"]
