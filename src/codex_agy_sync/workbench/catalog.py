"""Single-pass read-only host store catalog scanner for multi-workspace Workbench indexing."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from ..bridge import (
    DefaultHostTransport, HandoffReceipt, HostEnvironmentPaths, PresenceWalGuard, SyncDirection,
    WorkspaceBindingDiagnosis, WorkspaceProjectBinding, build_combined_lineage,
    build_workspace_binding_diagnosis, compute_codex_nesting_depth, deterministic_uuid,
    discover_agy_subagent_sidecars, extract_agy_project_folder_uris, extract_rollout_filename_uuid,
    load_handoff_receipt, open_readonly_sqlite, parse_codex_thread_source_lineage,
    parse_summary_workspace_uris, parse_ts_epoch_iso, receipt_matches_workspace,
    safe_valid_session_id, validated_child_path,
)
from ..codecs import decode_cascade_summary_proto, decode_trajectory_metadata_proto, iter_codex_rollout_lines, parse_annotation_pbtxt_title
from ..payloads import safe_json_dict
from ..workspace import (
    FORBIDDEN_AGY_PROJECT_IDS, AgyProjectId, CascadeId, CodexLegacyProjectId, CodexProjectId,
    HostKind, WorkspacePath, WorkspaceRootUri, parse_workspace_identity,
)
from .discovery import PreloadedHostTables, load_preloaded_host_tables, stat_with_wal_fingerprint

NormCache = dict[str, tuple[WorkspacePath, WorkspaceRootUri] | None]
AgyMetaCache = dict[str, tuple[tuple[int, int, int, int], dict[str, Any], int]]


@dataclass
class WorkspaceCatalogBucket:
    """Single-workspace catalog accumulator populated in one pass over host stores."""

    ws_path: WorkspacePath
    ws_uri: WorkspaceRootUri
    ws_id: str
    binding: WorkspaceProjectBinding
    diagnosis: WorkspaceBindingDiagnosis
    agy_entries: list[dict[str, Any]] = field(default_factory=list)
    codex_entries: list[dict[str, Any]] = field(default_factory=list)
    cursor_pairs: list[tuple[str, str]] = field(default_factory=list)
    receipts: list[tuple[str, HandoffReceipt, float]] = field(default_factory=list)


@dataclass
class _CatalogLookup:
    buckets: dict[WorkspacePath, WorkspaceCatalogBucket]
    by_uri: dict[WorkspaceRootUri, WorkspaceCatalogBucket]
    by_agy_pid: dict[str, list[WorkspaceCatalogBucket]]
    by_codex_pid: dict[str, list[WorkspaceCatalogBucket]]
    known_agy_pids: frozenset[str]
    norm_cache: NormCache


def _empty_meta_lineage() -> dict[str, Any]:
    return {"parent_conversation_id": "", "root_conversation_id": "", "workspace_uris": [], "agent_type_name": "", "agent_role": "", "nesting_depth": 0, "project_id": ""}


def _normalize_ident(raw: str, norm_cache: NormCache) -> tuple[WorkspacePath, WorkspaceRootUri] | None:
    if raw in norm_cache:
        return norm_cache[raw]
    try:
        resolved = parse_workspace_identity(raw)
    except ValueError:
        resolved = None
    norm_cache[raw] = resolved
    return resolved


def _scan_agy_project_ids(
    paths: HostEnvironmentPaths, norm_cache: NormCache,
) -> tuple[dict[WorkspaceRootUri, AgyProjectId], frozenset[str]]:
    agy_pid_by_uri: dict[WorkspaceRootUri, AgyProjectId] = {}
    known_project_pids: set[str] = set()
    projects_dir = paths.gemini_config_dir / "projects"
    if not projects_dir.is_dir():
        return agy_pid_by_uri, frozenset()
    for pfile in sorted(projects_dir.glob("*.json")):
        if pfile.stem:
            known_project_pids.add(pfile.stem)
        with contextlib.suppress(Exception):
            data = json.loads(pfile.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cand_id = str(data.get("id") or pfile.stem).strip()
                if cand_id:
                    known_project_pids.add(cand_id)
                if cand_id and cand_id not in FORBIDDEN_AGY_PROJECT_IDS:
                    for raw_u in extract_agy_project_folder_uris(data):
                        if ident := _normalize_ident(str(raw_u), norm_cache):
                            agy_pid_by_uri.setdefault(ident[1], AgyProjectId(cand_id))
    return agy_pid_by_uri, frozenset(known_project_pids)


def _scan_codex_project_ids(paths: HostEnvironmentPaths, norm_cache: NormCache, preloaded: PreloadedHostTables) -> tuple[dict[WorkspacePath, str], dict[WorkspacePath, str]]:
    codex_pid_by_path: dict[WorkspacePath, str] = {}
    legacy_pid_by_path: dict[WorkspacePath, str] = {}
    for pid_str, raw_root in preloaded.codex_project_roots:
        if pid_str and (ident := _normalize_ident(raw_root, norm_cache)):
            codex_pid_by_path.setdefault(ident[0], pid_str)
    gs_path = paths.codex_home_dir / ".codex-global-state.json"
    if gs_path.is_file():
        with contextlib.suppress(Exception):
            loaded = json.loads(gs_path.read_text(encoding="utf-8"))
            local_projects = loaded.get("local-projects") if isinstance(loaded, dict) else None
            if isinstance(local_projects, dict):
                for lpid, lentry in local_projects.items():
                    lpid_str = str(lpid or "").strip()
                    roots = lentry.get("rootPaths") if isinstance(lentry, dict) else None
                    if lpid_str and isinstance(roots, list):
                        for rp in roots:
                            if ident := _normalize_ident(str(rp), norm_cache):
                                legacy_pid_by_path.setdefault(ident[0], lpid_str)
    return codex_pid_by_path, legacy_pid_by_path


def _init_workspace_buckets(
    paths: HostEnvironmentPaths, workspaces: Sequence[tuple[WorkspacePath, WorkspaceRootUri]], norm_cache: NormCache, preloaded: PreloadedHostTables,
) -> _CatalogLookup:
    guard = PresenceWalGuard(paths)
    agy_active, codex_active = guard.discover_agy_daemon() is not None, guard.discover_codex_daemon() is not None
    agy_pids, known_agy_pids = _scan_agy_project_ids(paths, norm_cache)
    codex_pids, legacy_pids = _scan_codex_project_ids(paths, norm_cache, preloaded)
    buckets: dict[WorkspacePath, WorkspaceCatalogBucket] = {}
    by_uri: dict[WorkspaceRootUri, WorkspaceCatalogBucket] = {}
    by_agy_pid: dict[str, list[WorkspaceCatalogBucket]] = {}
    by_codex_pid: dict[str, list[WorkspaceCatalogBucket]] = {}
    for ws_path, ws_uri in workspaces:
        ws_id = "ws-" + hashlib.sha256(str(ws_uri).encode("utf-8")).hexdigest()[:16]
        ro_agy_pid, ro_codex_pid = agy_pids.get(ws_uri), codex_pids.get(ws_path)
        agy_pid = ro_agy_pid or AgyProjectId(deterministic_uuid("agy-project", str(ws_uri)))
        codex_pid = CodexProjectId(ro_codex_pid or deterministic_uuid("codex-project", str(ws_uri)))
        legacy_pid = CodexLegacyProjectId(legacy_pids.get(ws_path) or deterministic_uuid("codex-legacy-project", str(ws_uri)))
        binding = WorkspaceProjectBinding(ws_path, ws_uri, ws_path.name or "workspace", agy_pid, codex_pid, legacy_pid)
        diagnosis = build_workspace_binding_diagnosis(ws_path, ws_uri, ro_agy_pid, ro_codex_pid, agy_active, codex_active)
        bucket = WorkspaceCatalogBucket(ws_path=ws_path, ws_uri=ws_uri, ws_id=ws_id, binding=binding, diagnosis=diagnosis)
        buckets[ws_path] = by_uri[ws_uri] = bucket
        by_agy_pid.setdefault(str(agy_pid), []).append(bucket)
        by_codex_pid.setdefault(str(codex_pid), []).append(bucket)
    return _CatalogLookup(
        buckets=buckets, by_uri=by_uri, by_agy_pid=by_agy_pid, by_codex_pid=by_codex_pid,
        known_agy_pids=known_agy_pids, norm_cache=norm_cache,
    )


def _collect_cursors_and_handoffs(paths: HostEnvironmentPaths, lookup: _CatalogLookup) -> None:
    if (cursors_dir := paths.sync_state_dir / "cursors").is_dir():
        cursor_rows_by_ws: dict[WorkspacePath, list[tuple[tuple[str, str], str, str]]] = {}
        for cf in cursors_dir.glob("*.json"):
            with contextlib.suppress(Exception):
                raw = json.loads(cf.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                raw_uri = str(raw.get("workspace_uri") or "")
                bucket = lookup.by_uri.get(WorkspaceRootUri(raw_uri))
                if bucket is None and raw_uri and (ident := _normalize_ident(raw_uri, lookup.norm_cache)):
                    bucket = lookup.buckets.get(ident[0])
                cid_str = safe_valid_session_id(raw.get("cascade_id"), "cascade_id") if bucket is not None else ""
                tid_str = safe_valid_session_id(raw.get("codex_thread_id"), "codex_thread_id") if cid_str else ""
                if bucket is not None and cid_str and tid_str:
                    cursor_rows_by_ws.setdefault(bucket.ws_path, []).append(((str(raw.get("updated_at_iso") or ""), cf.name), cid_str, tid_str))
        for ws_path, rows in cursor_rows_by_ws.items():
            rows.sort(key=lambda item: item[0], reverse=True)
            lookup.buckets[ws_path].cursor_pairs = [(cid, tid) for _, cid, tid in rows]

    if (handoffs_dir := paths.sync_state_dir / "handoffs").is_dir():
        for hf in sorted(handoffs_dir.glob("*.json")):
            if not hf.is_file():
                continue
            try:
                receipt, mtime = load_handoff_receipt(hf), hf.stat().st_mtime
            except (json.JSONDecodeError, ValueError, TypeError, OSError):
                continue
            ident = _normalize_ident(receipt.workspace, lookup.norm_cache)
            if ident is not None and (bucket := lookup.buckets.get(ident[0])) is not None:
                bucket.receipts.append((hf.stem, receipt, mtime))
            else:
                for bucket in lookup.buckets.values():
                    if receipt_matches_workspace(receipt.workspace, bucket.ws_path):
                        bucket.receipts.append((hf.stem, receipt, mtime))


def _match_agy_buckets(lookup: _CatalogLookup, pid_val: str, raw_uris: Sequence[str]) -> list[WorkspaceCatalogBucket]:
    matched: dict[WorkspacePath, WorkspaceCatalogBucket] = {}
    if pid_val:
        for bucket in lookup.by_agy_pid.get(pid_val, ()):
            matched[bucket.ws_path] = bucket
    for raw_u in raw_uris:
        if (ident := _normalize_ident(str(raw_u), lookup.norm_cache)) and (bucket := lookup.buckets.get(ident[0])) is not None:
            matched[bucket.ws_path] = bucket
    return list(matched.values())


def _resolve_agy_title(paths: HostEnvironmentPaths, cid_str: str, sum_title: str, raw_summary: bytes | None) -> str:
    annot_path = validated_child_path(paths.agy_data_dir, "annotations", f"{cid_str}.pbtxt", label="cascade_id")
    pbtxt_title = parse_annotation_pbtxt_title(annot_path.read_text(encoding="utf-8", errors="replace")) if annot_path.is_file() else None
    summary_annot_title: str | None = None
    if not pbtxt_title and raw_summary:
        with contextlib.suppress(ValueError):
            summary_annot_title = decode_cascade_summary_proto(raw_summary, fallback_cascade_id=CascadeId(cid_str)).annotations.title.strip() or None
    return next((c for c in (pbtxt_title, summary_annot_title, sum_title.strip() or None) if c), "Untitled Session")


def _read_agy_conv_meta_cached(
    db_file: Path, cid_str: str, *, need_step_count: bool, in_summary: bool, agy_meta_cache: AgyMetaCache,
) -> tuple[dict[str, Any], int, float] | None:
    try:
        fp, mtime = stat_with_wal_fingerprint(db_file, include_wal=True)
    except OSError:
        return None
    cached = agy_meta_cache.get(cid_str)
    if cached is not None and cached[0] == fp and (not need_step_count or cached[2] >= 0):
        return cached[1], cached[2], mtime
    meta = _empty_meta_lineage()
    step_count = cached[2] if (cached is not None and cached[0] == fp) else -1
    try:
        with contextlib.closing(open_readonly_sqlite(db_file)) as conn:
            tblob = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
            if tblob and tblob[0]:
                meta = decode_trajectory_metadata_proto(bytes(tblob[0]))
            if need_step_count and step_count < 0:
                sc_row = conn.execute("SELECT COUNT(*) FROM steps").fetchone()
                step_count = int(sc_row[0]) if sc_row else 0
    except (sqlite3.DatabaseError, OSError, ValueError):
        return (_empty_meta_lineage(), -1, mtime) if in_summary else None
    agy_meta_cache[cid_str] = (fp, meta, step_count)
    return meta, step_count, mtime


def _classify_agy_summary_rows(
    summary_rows: Sequence[tuple[Any, ...]], sidecars: Mapping[str, dict[str, Any]], lookup: _CatalogLookup,
) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    summary_cids: set[str] = set()
    root_candidates: dict[str, dict[str, Any]] = {}
    unscoped_candidates: dict[str, dict[str, Any]] = {}
    summary_order: list[str] = []
    for row in summary_rows:
        cid, title, scount, uris_raw, pid, agent_name, parent_cid, nesting_depth, sum_mtime, raw_sum = row
        cid_str = safe_valid_session_id(cid, "cascade_id")
        if not cid_str:
            continue
        summary_cids.add(cid_str)
        sc_info = sidecars.get(cid_str, {})
        pid_str = str(pid or "").strip()
        cand_uris = list(sc_info.get("workspace_uris", [])) + parse_summary_workspace_uris(uris_raw)
        _ = safe_valid_session_id(parent_cid or sc_info.get("parent_cascade_id"), "parent_cascade_id")
        eff_nesting = max(0, int(nesting_depth or 0), 1 if sc_info else 0)
        if eff_nesting > 0 or sc_info:
            continue
        buckets = _match_agy_buckets(lookup, pid_str, cand_uris)
        rec = {
            "title": str(title or "").strip(), "step_count": int(scount or 0), "sum_mtime": sum_mtime,
            "raw_summary": bytes(raw_sum) if raw_sum else None, "agent_role": str(agent_name or "").strip(),
            "buckets": buckets,
        }
        if buckets:
            root_candidates[cid_str] = rec
            summary_order.append(cid_str)
        elif not cand_uris and pid_str not in lookup.known_agy_pids:
            unscoped_candidates[cid_str] = rec
            summary_order.append(cid_str)
    return summary_cids, root_candidates, unscoped_candidates, summary_order


def _collect_agy_sessions(paths: HostEnvironmentPaths, lookup: _CatalogLookup, agy_meta_cache: AgyMetaCache, preloaded: PreloadedHostTables) -> None:
    sidecars = discover_agy_subagent_sidecars(paths)
    summary_cids, root_candidates, unscoped_candidates, summary_order = _classify_agy_summary_rows(preloaded.agy_summary_rows, sidecars, lookup)
    if not (conv_dir := paths.agy_data_dir / "conversations").is_dir():
        return
    resolved: dict[str, tuple[list[WorkspaceCatalogBucket], dict[str, Any]]] = {}
    orphan_order: list[str] = []
    for db_file in sorted(conv_dir.glob("*.db")):
        cid_str = safe_valid_session_id(db_file.stem, "cascade_id")
        in_sum = bool(cid_str and cid_str in summary_cids)
        if not cid_str or (in_sum and cid_str not in root_candidates and cid_str not in unscoped_candidates) or (not in_sum and cid_str in sidecars):
            continue
        meta_res = _read_agy_conv_meta_cached(db_file, cid_str, need_step_count=not in_sum, in_summary=in_sum, agy_meta_cache=agy_meta_cache)
        if meta_res is None:
            continue
        meta, step_count, db_mtime = meta_res
        cand = root_candidates.get(cid_str) or unscoped_candidates.get(cid_str)
        eff_parent = safe_valid_session_id(meta["parent_conversation_id"], "parent_cascade_id") or None
        info = {
            "parent_cascade_id": eff_parent, "root_cascade_id": meta["root_conversation_id"] or eff_parent,
            "nesting_depth": int(meta["nesting_depth"]), "agent_type_name": meta["agent_type_name"],
            "agent_role": (cand["agent_role"] if cand else "") or meta["agent_role"], "spawn_step_index": None,
        }
        if build_combined_lineage(info, {}).is_subagent:
            continue
        buckets = cand["buckets"] if (cand and cand["buckets"]) else _match_agy_buckets(lookup, str(meta["project_id"] or ""), list(meta["workspace_uris"]))
        if not buckets:
            continue
        epoch, iso = parse_ts_epoch_iso(cand["sum_mtime"] if cand else None, fallback_mtime=db_mtime)
        title = _resolve_agy_title(paths, cid_str, cand["title"] if cand else (meta["agent_role"] or ""), cand["raw_summary"] if cand else None)
        resolved[cid_str] = (buckets, {"host": HostKind.ANTIGRAVITY, "session_id": cid_str, "direction": SyncDirection.AGY_TO_CODEX, "title": title, "count": cand["step_count"] if cand else max(0, step_count), "epoch": epoch, "iso": iso})
        if not in_sum:
            orphan_order.append(cid_str)
    for cid_str in [*summary_order, *orphan_order]:
        if cid_str in resolved:
            buckets, entry = resolved[cid_str]
            for bucket in buckets:
                bucket.agy_entries.append(dict(entry))


def _load_codex_session_index(paths: HostEnvironmentPaths) -> dict[str, str]:
    index_map: dict[str, str] = {}
    session_idx = paths.codex_home_dir / "session_index.jsonl"
    if not session_idx.is_file():
        return index_map
    with contextlib.suppress(Exception):
        for line in session_idx.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            with contextlib.suppress(json.JSONDecodeError):
                obj = json.loads(line)
                if isinstance(obj, dict) and (tid := safe_valid_session_id(obj.get("id"), "codex_thread_id")):
                    if tname := str(obj.get("thread_name") or "").strip():
                        index_map[tid] = tname
    return index_map


def _resolve_codex_rollout_candidate(raw_rp: str, resolved_codex_home: Path) -> Path | None:
    for cand_str in (raw_rp, f"{raw_rp}.zst", raw_rp.removesuffix(".zst")):
        cand = Path(cand_str)
        if cand.is_file() and cand.resolve(strict=False).is_relative_to(resolved_codex_home):
            return cand
    return None


def _collect_codex_sessions(
    paths: HostEnvironmentPaths, lookup: _CatalogLookup, preloaded: PreloadedHostTables,
) -> None:
    seen_or_archived: set[str] = set()
    known_rollout_paths: set[str] = set()
    pending_entries: list[tuple[list[WorkspaceCatalogBucket], dict[str, Any]]] = []
    session_index_cache: dict[str, str] | None = None
    resolved_codex_home = paths.codex_home_dir.resolve(strict=False)
    has_user_event_col = preloaded.codex_has_user_event_col

    for row in preloaded.codex_thread_rows:
        tid, title, name, preview, cwd, pid, ptid, rpath, nick, role, src, rec_ms, upd_ms, upd, archived, has_ue = row
        if rpath:
            raw_rp = str(rpath)
            known_rollout_paths.update((raw_rp, f"{raw_rp}.zst", raw_rp.removesuffix(".zst")))
        tid_str = safe_valid_session_id(tid, "codex_thread_id")
        if not tid_str:
            continue
        seen_or_archived.add(tid_str)
        if archived:
            continue
        matched: dict[WorkspacePath, WorkspaceCatalogBucket] = {b.ws_path: b for b in lookup.by_codex_pid.get(str(pid or ""), ())}
        if not matched and cwd and (ident := _normalize_ident(str(cwd), lookup.norm_cache)):
            if (b := lookup.buckets.get(ident[0])) is not None:
                matched[b.ws_path] = b
        if not matched:
            continue
        src_sub, src_parent, src_depth, src_type, src_nick = parse_codex_thread_source_lineage(str(src) if src else None)
        parent_str = safe_valid_session_id(str(ptid or "").strip() or src_parent, "parent_codex_thread_id")
        if compute_codex_nesting_depth(src, src_sub, src_depth, parent_str, str(role or "").strip() or src_type, str(nick or "").strip() or src_nick) > 0:
            continue
        resolved_rp = _resolve_codex_rollout_candidate(str(rpath), resolved_codex_home) if rpath else None
        mtime = resolved_rp.stat().st_mtime if resolved_rp is not None else 0.0
        epoch, iso = parse_ts_epoch_iso(rec_ms or upd_ms or upd, fallback_mtime=mtime)
        eff_title = next((str(x).strip() for x in (name, title, preview) if x and str(x).strip()), "")
        pending_entries.append((list(matched.values()), {
            "host": HostKind.CODEX, "session_id": tid_str, "direction": SyncDirection.CODEX_TO_AGY,
            "title": eff_title, "count": (1 if int(has_ue or 0) else 0) if has_user_event_col else 1,
            "epoch": epoch, "iso": iso, "rollout_path": resolved_rp,
        }))

    for buckets, entry in pending_entries:
        if not entry["title"]:
            if session_index_cache is None:
                session_index_cache = _load_codex_session_index(paths)
            entry["title"] = session_index_cache.get(entry["session_id"]) or "Untitled Session"
        for bucket in buckets:
            bucket.codex_entries.append(dict(entry))

    _collect_codex_fallbacks(paths, lookup, seen_or_archived, known_rollout_paths, session_index_cache)


def _collect_codex_fallbacks(
    paths: HostEnvironmentPaths,
    lookup: _CatalogLookup,
    seen_or_archived: set[str],
    known_rollout_paths: set[str],
    session_index_cache: dict[str, str] | None,
) -> None:
    sessions_dir = paths.codex_home_dir / "sessions"
    if not sessions_dir.is_dir():
        return
    for rpath in sorted({*sessions_dir.rglob("rollout-*.jsonl"), *sessions_dir.rglob("rollout-*.jsonl.zst")}):
        if str(rpath) in known_rollout_paths:
            continue
        if (tail_id := extract_rollout_filename_uuid(rpath.name)) and tail_id in seen_or_archived:
            continue
        meta_payload: dict[str, Any] | None = None
        bucket: WorkspaceCatalogBucket | None = None
        tid_str = ""
        user_turns = 0
        for row in iter_codex_rollout_lines(rpath):
            if '"role":"user"' in row.raw_json or '"role": "user"' in row.raw_json:
                user_turns += 1
            if meta_payload is None:
                rec = safe_json_dict(row.raw_json)
                if rec.get("type") == "session_meta" and isinstance(rec.get("payload"), dict):
                    meta_payload = rec["payload"]
                    tid_str = safe_valid_session_id(meta_payload.get("id"), "codex_thread_id")
                    if not tid_str or tid_str in seen_or_archived:
                        break
                    ident = _normalize_ident(str(meta_payload.get("cwd") or ""), lookup.norm_cache) if meta_payload.get("cwd") else None
                    bucket = lookup.buckets.get(ident[0]) if ident is not None else None
                    src_raw = meta_payload.get("source")
                    src_is_sub, _, _, _, _ = parse_codex_thread_source_lineage(
                        json.dumps(src_raw) if isinstance(src_raw, dict) else (str(src_raw) if src_raw else None)
                    )
                    if bucket is None or src_is_sub:
                        bucket = None
                        break
        if bucket is None or meta_payload is None:
            continue
        seen_or_archived.add(tid_str)
        if session_index_cache is None:
            session_index_cache = _load_codex_session_index(paths)
        epoch, iso = parse_ts_epoch_iso(meta_payload.get("timestamp"), fallback_mtime=rpath.stat().st_mtime)
        bucket.codex_entries.append({
            "host": HostKind.CODEX, "session_id": tid_str, "direction": SyncDirection.CODEX_TO_AGY,
            "title": session_index_cache.get(tid_str) or "Untitled Session",
            "count": max(1, user_turns), "epoch": epoch, "iso": iso, "rollout_path": rpath,
        })


def scan_host_catalog(
    paths: HostEnvironmentPaths,
    workspaces: Sequence[tuple[WorkspacePath, WorkspaceRootUri]],
    transport: DefaultHostTransport,
    agy_meta_cache: AgyMetaCache,
    *,
    preloaded: PreloadedHostTables | None = None,
) -> dict[WorkspacePath, WorkspaceCatalogBucket]:
    """Scan all Antigravity and Codex host stores once and bucket data per workspace."""
    _ = transport
    tables = preloaded if preloaded is not None else load_preloaded_host_tables(paths)
    norm_cache: NormCache = {}
    lookup = _init_workspace_buckets(paths, workspaces, norm_cache, tables)
    _collect_cursors_and_handoffs(paths, lookup)
    _collect_agy_sessions(paths, lookup, agy_meta_cache, tables)
    _collect_codex_sessions(paths, lookup, tables)
    return lookup.buckets
