"""Read-only search, citation draft rendering, and handoff orchestration for Stage-1 Workbench."""

from __future__ import annotations

import dataclasses
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from ..bridge.handoff import handoff_session
from ..bridge.handoff_resolve import resolve_handoff_request
from ..bridge.paths_and_locks import HostEnvironmentPaths
from ..bridge.state_machine import HostKind, SyncDirection
from ..ledger import parse_workspace_identity
from ._discovery import _candidate_home_dirs, _is_excluded_for_homes, _raw_spec_to_candidate_path
from .indexer import WorkbenchIndexer
from .models import CitationRef, DraftRenderResult, SearchHit, WorkCard, WorkbenchSnapshot

_HOST_LABELS = {HostKind.CODEX.value: "Codex", HostKind.ANTIGRAVITY.value: "Antigravity", "agy": "Antigravity"}


def _host_label(host: str) -> str:
    return _HOST_LABELS.get(host.strip().lower(), host or "Session")


def _normalize_scope(scope: str) -> str:
    cleaned = (scope or "all").strip().lower()
    if cleaned in {"all", HostKind.CODEX.value}:
        return cleaned
    if cleaned in {"agy", HostKind.ANTIGRAVITY.value}:
        return HostKind.ANTIGRAVITY.value
    raise ValueError(f"Unsupported search scope: {scope!r}")


def _normalize_target_hint(target_hint: str) -> str:
    cleaned = (target_hint or "codex").strip().lower()
    if cleaned == "codex":
        return "codex"
    if cleaned in {"agy", "antigravity"}:
        return HostKind.ANTIGRAVITY.value
    raise ValueError(f"Unsupported draft target_hint: {target_hint!r}")


def _build_snippet(text: str, q_norm: str, tokens: Sequence[str], max_len: int = 180) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    lower = collapsed.lower()
    idx = lower.find(q_norm)
    if idx < 0:
        positions = [lower.find(tok) for tok in tokens if tok in lower]
        idx = min(positions) if positions else 0
    start = max(0, idx - 48)
    end = min(len(collapsed), start + max_len)
    if end == len(collapsed):
        start = max(0, end - max_len)
    snippet = collapsed[start:end].strip()
    return f"{'...' if start > 0 else ''}{snippet}{'...' if end < len(collapsed) else ''}"


def _score_field(text: str, q_norm: str, tokens: Sequence[str], base_weight: int) -> int:
    if not text:
        return 0
    lower = text.lower()
    if q_norm in lower:
        return 100 + base_weight
    matched = sum(1 for tok in tokens if tok in lower)
    if matched == 0:
        return 0
    if len(tokens) == 1 or matched == len(tokens):
        return 50 + 8 * matched + base_weight
    return 10 * matched + (base_weight // 2)


def _collect_work_hits(
    work: WorkCard, workspace_name: str, q_norm: str, tokens: Sequence[str], scope: str,
) -> list[SearchHit]:
    hits: list[SearchHit] = []

    def _add(hit_id: str, host: str, sid: str, turn_id: str, ts: str, role: str, text: str, weight: int) -> None:
        if (score := _score_field(text, q_norm, tokens, weight)) <= 0:
            return
        hits.append(SearchHit(
            hit_id=hit_id, workspace_id=work.workspace_id, workspace_name=workspace_name,
            work_id=work.work_id, work_title=work.title, host=host, session_id=sid, turn_id=turn_id,
            timestamp_iso=ts, role=role, snippet=_build_snippet(text, q_norm, tokens), score=score,
        ))

    if scope == "all" or work.active_host == scope:
        _add(f"{work.work_id}:work_title", work.active_host, work.latest_source_id, "", work.updated_at_iso, "work_title", work.title, 40)
    for sess in work.sessions:
        if scope in {"all", sess.host}:
            _add(f"{work.work_id}:session:{sess.host}:{sess.session_id}", sess.host, sess.session_id, "", sess.updated_at_iso, "session_title", sess.title, 25)
    for turn_pos, turn in enumerate(work.turns, start=1):
        if scope not in {"all", turn.host}:
            continue
        ts = turn.timestamp_iso or work.updated_at_iso
        for role, text, weight in (("user", turn.user_text, 30), ("assistant", turn.assistant_text, 24), ("tool", turn.tool_summary, 18)):
            _add(
                f"{work.work_id}:{turn.host}:{turn.session_id}:{turn.turn_id}:{turn_pos}:{role}",
                turn.host, turn.session_id, turn.turn_id, ts, role, text, weight,
            )
    return hits


def _inline_text(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _coerce_citation(raw: CitationRef | Mapping[str, Any], work: WorkCard) -> CitationRef:
    if isinstance(raw, CitationRef):
        cit = raw
    elif isinstance(raw, Mapping):
        for field_name in ("workspace_id", "work_id", "host", "session_id", "turn_id", "excerpt", "label"):
            val = raw.get(field_name)
            if val is not None and not isinstance(val, str):
                raise ValueError(f"Citation field {field_name!r} must be a string.")
        cit = CitationRef(
            workspace_id=raw.get("workspace_id") or work.workspace_id,
            work_id=raw.get("work_id") or work.work_id,
            host=raw.get("host") or "",
            session_id=raw.get("session_id") or "",
            turn_id=raw.get("turn_id") or "",
            excerpt=raw.get("excerpt") or "",
            label=raw.get("label") or "",
        )
    else:
        raise ValueError("Invalid citation payload.")
    req_tid = cit.turn_id.strip()
    req_sid = cit.session_id.strip()
    req_host = cit.host.strip()
    norm_req_host = HostKind.ANTIGRAVITY.value if req_host.lower() == "agy" else req_host.lower()
    matched_turn = (
        next(
            (
                t for t in work.turns
                if t.turn_id == req_tid
                and (not req_sid or t.session_id == req_sid)
                and (not norm_req_host or t.host.lower() == norm_req_host)
            ),
            None,
        )
        if req_tid
        else None
    )
    host = req_host or (matched_turn.host if matched_turn else work.active_host)
    sid = req_sid or (matched_turn.session_id if matched_turn else work.latest_source_id)
    excerpt = cit.excerpt.strip()
    if not excerpt and matched_turn is not None:
        excerpt = (matched_turn.assistant_text or matched_turn.user_text or matched_turn.tool_summary).strip()
    return CitationRef(
        workspace_id=cit.workspace_id or work.workspace_id, work_id=cit.work_id or work.work_id,
        host=host, session_id=sid, turn_id=req_tid, excerpt=excerpt, label=_inline_text(cit.label),
    )


def _format_draft_markdown(work: WorkCard, target_hint: str, note: str, citations: Sequence[CitationRef]) -> str:
    lines = [
        f"## Workbench Draft: {_inline_text(work.title)}",
        "",
        f"- Workspace: `{work.workspace_path}`",
        f"- Work ID: `{work.work_id}`",
        f"- Target Hint: {_host_label(target_hint)} (`{target_hint}`)",
        "- Delivery Mode: `stage_1_copy_only` (manual paste only; remote delivery disabled)",
    ]
    if citations:
        lines.extend(["", "### Citations", ""])
        for idx, cit in enumerate(citations, start=1):
            clean_label = _inline_text(cit.label)
            label_part = f" - {clean_label}" if clean_label else ""
            clean_tid = _inline_text(cit.turn_id)
            turn_part = f" turn `{clean_tid}`" if clean_tid else ""
            lines.append(f"{idx}. [{_host_label(_inline_text(cit.host))}] session `{_inline_text(cit.session_id)}`{turn_part}{label_part}")
            if cit.excerpt:
                lines.append("\n".join(f"   > {line}" for line in cit.excerpt.splitlines()))
    if note:
        lines.extend(["", "### Note", "", note])
    return "\n".join(lines).rstrip() + "\n"


class WorkbenchService:
    """High-level Stage-1 service layer backed by read-only WorkbenchIndexer and Desktop handoff."""

    def __init__(
        self,
        paths: HostEnvironmentPaths | None = None,
        *,
        seed_workspaces: Sequence[str | Path] = (),
        ttl_seconds: float = 3.0,
    ) -> None:
        self._paths = paths or HostEnvironmentPaths.discover()
        self._indexer = WorkbenchIndexer(self._paths, seed_workspaces=seed_workspaces, ttl_seconds=ttl_seconds)

    @property
    def paths(self) -> HostEnvironmentPaths:
        return self._paths

    def invalidate(self) -> None:
        self._indexer.invalidate()

    def get_snapshot(self, workspace_id: str | None = None, refresh: bool = False) -> WorkbenchSnapshot:
        return self._indexer.get_snapshot(workspace_id=workspace_id, refresh=refresh)

    def get_work(self, work_id: str, refresh: bool = False) -> WorkCard:
        cleaned = (work_id or "").strip()
        if not cleaned:
            raise ValueError("work_id is required.")
        for work in self._indexer.get_snapshot(refresh=refresh).works:
            if work.work_id == cleaned:
                return work
        raise KeyError(f"Work not found: {cleaned}")

    def search(
        self, query: str, *, workspace_id: str | None = None, scope: str = "all", limit: int = 50,
    ) -> list[SearchHit]:
        norm_scope = _normalize_scope(scope)
        bound_limit = max(1, min(int(limit), 500))
        q_norm = " ".join((query or "").split()).lower()
        if not q_norm:
            return []
        tokens = list(dict.fromkeys(tok for tok in re.split(r"\s+", q_norm) if tok))
        snap = self._indexer.get_snapshot(workspace_id=workspace_id)
        ws_names = {ws.workspace_id: ws.name for ws in snap.workspaces}
        all_hits: list[SearchHit] = []
        for work in snap.works:
            ws_name = ws_names.get(work.workspace_id, Path(work.workspace_path).name)
            all_hits.extend(_collect_work_hits(work, ws_name, q_norm, tokens, norm_scope))
        all_hits.sort(key=lambda h: (h.score, h.timestamp_iso, h.hit_id), reverse=True)
        return all_hits[:bound_limit]

    def render_draft(
        self, work_id: str, note: str, *, citations: Sequence[CitationRef | Mapping[str, Any]] = (), target_hint: str = "codex",
    ) -> DraftRenderResult:
        if not isinstance(work_id, str):
            raise ValueError("work_id must be a string.")
        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string.")
        if target_hint is not None and not isinstance(target_hint, str):
            raise ValueError("target_hint must be a string.")
        work = self.get_work(work_id)
        norm_target = _normalize_target_hint(target_hint)
        note_clean = (note or "").strip()
        parsed_citations = tuple(_coerce_citation(item, work) for item in citations)
        if not note_clean and not parsed_citations:
            raise ValueError("Draft note and citations cannot both be empty.")
        return DraftRenderResult(
            workspace_path=work.workspace_path,
            work_id=work.work_id,
            target_hint=norm_target,
            markdown=_format_draft_markdown(work, norm_target, note_clean, parsed_citations),
            citation_count=len(parsed_citations),
            notice="Stage 1 copy-only draft ready. Remote session delivery is disabled.",
            stage="stage_1_copy_only",
            delivery_executed=False,
        )

    def _resolve_workspace_arg(self, workspace: str) -> str:
        if not isinstance(workspace, str):
            raise ValueError("workspace must be a string.")
        raw = workspace.strip()
        if not raw:
            raise ValueError("workspace is required.")
        target_spec = raw
        for ws in self._indexer.get_snapshot().workspaces:
            if ws.workspace_id == raw:
                target_spec = ws.workspace_path
                break
        raw_path = _raw_spec_to_candidate_path(target_spec)
        if raw_path is None:
            raise ValueError(f"Invalid workspace path or URI: {workspace!r}")
        homes = _candidate_home_dirs(self._paths)
        if _is_excluded_for_homes(raw_path, homes):
            raise ValueError(f"Workspace path is inside an excluded directory: {workspace!r}")
        ws_path, _ = parse_workspace_identity(target_spec)
        if _is_excluded_for_homes(ws_path, homes):
            raise ValueError(f"Workspace path is inside an excluded directory: {workspace!r}")
        if not ws_path.is_dir():
            raise ValueError(f"Workspace directory does not exist: {workspace!r}")
        return str(ws_path)

    def trigger_handoff(
        self,
        *,
        workspace: str,
        source_id: str | None = None,
        direction: str | None = None,
        latest: bool = False,
        chain: bool = False,
        title: str | None = None,
        pair_if_offline: bool = False,
    ) -> dict[str, Any]:
        for field_name, field_val in (("source_id", source_id), ("direction", direction), ("title", title)):
            if field_val is not None and not isinstance(field_val, str):
                raise ValueError(f"{field_name} must be a string.")
        clean_source = (source_id or "").strip() or None
        clean_dir = (direction or "").strip() or None
        if chain and latest:
            raise ValueError("chain cannot be combined with latest.")
        if clean_source is not None and latest:
            raise ValueError("source_id cannot be combined with latest.")
        if clean_source is None and not latest and not chain:
            raise ValueError("Specify source_id, latest=True, or chain=True for workbench handoff.")
        resolved = resolve_handoff_request(
            self._paths,
            self._resolve_workspace_arg(workspace),
            direction=SyncDirection(clean_dir) if clean_dir else None,
            source_id=clean_source,
            title=title,
            latest=bool(latest),
            chain=bool(chain),
            pick=False,
            pair_if_offline=bool(pair_if_offline),
        )
        if resolved.already_ready_receipt is not None:
            if resolved.paired_now:
                self._indexer.invalidate()
            return dataclasses.asdict(resolved.already_ready_receipt)
        receipt = handoff_session(
            self._paths,
            resolved.binding.workspace_path,
            direction=resolved.direction,
            source_id=resolved.source_id,
            title=resolved.title,
            chain_id=resolved.chain_id,
            chained_from_source_id=resolved.chained_from_source_id,
            chain_depth=resolved.chain_depth,
        )
        self._indexer.invalidate()
        return dataclasses.asdict(receipt)
