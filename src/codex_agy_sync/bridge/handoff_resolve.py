"""Handoff candidate discovery, UX mode resolution, and single execution entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
import dataclasses
from pathlib import Path
import sys
from typing import Callable, Sequence

from ..events import HostKind
from ..ledger import TrajectoryLedger
from ..workspace import validate_session_id
from .handoff import handoff_ledger, handoff_session, read_handoff_source
from .handoff_store import (
    HandoffCancelledError,
    HandoffPendingError,
    HandoffReceipt,
    assert_source_belongs_to_binding,
    discover_agy_entries,
    discover_codex_entries,
    infer_direction_from_source_id,
    ledger_seed_fingerprint,
    load_workspace_receipts,
    resolve_chain_tip,
)
from .paths_and_locks import HostEnvironmentPaths, PresenceWalGuard
from .registry import (
    ProjectRegistry,
    UnpairedWorkspaceError,
    WorkspaceBindingDiagnosis,
    WorkspaceProjectBinding,
    diagnose_workspace_binding,
    guarded_pair_workspace,
)
from .state_machine import SyncDirection, generate_uuid7
from .transport import DefaultHostTransport

__all__ = [
    "HandoffCancelledError",
    "HandoffCandidate",
    "ResolvedHandoffRequest",
    "UnpairedWorkspaceError",
    "WorkspaceBindingDiagnosis",
    "diagnose_workspace_binding",
    "execute_handoff",
    "list_handoff_candidates",
    "resolve_handoff_request",
    "validate_handoff_selector_flags",
]


@dataclass(frozen=True)
class HandoffCandidate:
    """Non-subagent session candidate eligible for interactive or automatic handoff selection."""

    host: HostKind
    session_id: str
    direction: SyncDirection
    title: str
    updated_at_iso: str
    turn_or_step_count: int
    already_handed_ready: bool
    handed_target_host: str | None
    handed_target_id: str | None
    is_untouched_baton_target: bool
    chain_id: str | None
    chain_depth: int
    sort_epoch: float


@dataclass(frozen=True)
class ResolvedHandoffRequest:
    """Validated execution plan resolved from CLI/API handoff UX options."""

    binding: WorkspaceProjectBinding
    direction: SyncDirection
    source_id: str
    title: str | None
    selection_mode: str
    chain_id: str | None = None
    chained_from_source_id: str | None = None
    chain_depth: int = 0
    already_ready_receipt: HandoffReceipt | None = None
    paired_now: bool = False


def validate_handoff_selector_flags(
    *,
    source_id: str | None = None,
    latest: bool = False,
    chain: bool = False,
    pick: bool = False,
    require_selector: bool = False,
) -> None:
    if chain and (latest or pick):
        raise ValueError("--chain cannot be combined with --latest or --pick.")
    if source_id is not None and (latest or pick):
        raise ValueError("--source-id cannot be combined with --latest or --pick.")
    if latest and pick:
        raise ValueError("--latest cannot be combined with --pick.")
    if require_selector and source_id is None and not latest and not chain and not pick:
        raise ValueError("Specify source_id, latest, chain, or pick for handoff.")


def _read_ledger_for_chain(
    paths: HostEnvironmentPaths,
    binding: WorkspaceProjectBinding,
    direction: SyncDirection,
    sid: str,
    *,
    propagate_active: bool = False,
) -> TrajectoryLedger | None:
    try:
        return read_handoff_source(paths, binding, direction, sid)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        if propagate_active and any(tok in str(exc).lower() for tok in ("active", "incomplete", "locked")):
            raise
        return None
    except (RuntimeError, OSError):
        return None


def _did_target_advance_beyond_seed(
    paths: HostEnvironmentPaths,
    binding: WorkspaceProjectBinding,
    receipt: HandoffReceipt,
    *,
    propagate_active: bool = False,
) -> bool | None:
    """Return `True` if baton target grew beyond seed, `False` if still at seed, `None` if unreadable."""
    if not receipt.target_id:
        return None
    t_dir = SyncDirection.AGY_TO_CODEX if receipt.target_host == HostKind.ANTIGRAVITY.value else SyncDirection.CODEX_TO_AGY
    t_ledger = _read_ledger_for_chain(paths, binding, t_dir, receipt.target_id, propagate_active=propagate_active)
    if t_ledger is None:
        return None
    t_turns, t_digest = ledger_seed_fingerprint(t_ledger)
    if receipt.target_seed_turn_count is not None and receipt.target_seed_text_digest is not None:
        if t_turns == receipt.target_seed_turn_count and t_digest == receipt.target_seed_text_digest:
            return False
        return (t_turns > receipt.target_seed_turn_count) or (t_turns == receipt.target_seed_turn_count and t_digest != receipt.target_seed_text_digest)
    s_dir = SyncDirection.CODEX_TO_AGY if receipt.source_host == HostKind.CODEX.value else SyncDirection.AGY_TO_CODEX
    s_ledger = _read_ledger_for_chain(paths, binding, s_dir, receipt.source_id, propagate_active=False)
    if s_ledger is None:
        return None
    matched_wm = next((wm for wm in s_ledger.completed_turn_watermarks if str(wm.boundary_digest) == receipt.source_digest), None)
    if matched_wm is None:
        return None
    seeded_source = TrajectoryLedger.from_events(
        s_ledger.workspace_path,
        s_ledger.workspace_uri,
        s_ledger.events[: int(matched_wm.last_ledger_seq) + 1],
        explicit_title_override=s_ledger.title,
    )
    seed_turns, seed_dig = ledger_seed_fingerprint(handoff_ledger(seeded_source, None))
    if t_turns == seed_turns and t_digest == seed_dig:
        return False
    return (t_turns > seed_turns) or (t_turns == seed_turns and t_digest != seed_dig)


def list_handoff_candidates(
    paths: HostEnvironmentPaths,
    binding: WorkspaceProjectBinding,
    *,
    direction: SyncDirection | None = None,
) -> tuple[HandoffCandidate, ...]:
    """List non-subagent workspace candidates newest-first without mutating host or receipt state."""
    transport = DefaultHostTransport(paths)
    ready_by_source: dict[tuple[str, str], tuple[str, float, int, HandoffReceipt]] = {}
    ready_by_target: dict[tuple[str, str], tuple[str, float, int, HandoffReceipt]] = {}
    for _, rec, mtime in load_workspace_receipts(paths, binding.workspace_path):
        if rec.status != "ready":
            continue
        rank = (rec.created_at_iso, mtime, rec.chain_depth, rec)
        src_k = (rec.source_host, rec.source_id)
        if src_k not in ready_by_source or rank[:3] > ready_by_source[src_k][:3]:
            ready_by_source[src_k] = rank
        if rec.target_id:
            tgt_k = (rec.target_host, rec.target_id)
            if tgt_k not in ready_by_target or rank[:3] > ready_by_target[tgt_k][:3]:
                ready_by_target[tgt_k] = rank

    raw_entries: list[dict] = []
    if direction != SyncDirection.CODEX_TO_AGY:
        raw_entries.extend(discover_agy_entries(paths, transport, binding))
    if direction != SyncDirection.AGY_TO_CODEX:
        raw_entries.extend(discover_codex_entries(paths, binding, transport))

    candidates: list[HandoffCandidate] = []
    for item in raw_entries:
        host, sid = item["host"], item["session_id"]
        src_match = ready_by_source.get((host.value, sid))
        tgt_match = ready_by_target.get((host.value, sid))
        handed_rec = src_match[3] if src_match else None
        chain_rec = handed_rec or (tgt_match[3] if tgt_match else None)
        untouched = (_did_target_advance_beyond_seed(paths, binding, tgt_match[3]) is False) if tgt_match else False
        candidates.append(HandoffCandidate(
            host=host, session_id=sid, direction=item["direction"], title=item["title"],
            updated_at_iso=item["iso"], turn_or_step_count=item["count"],
            already_handed_ready=handed_rec is not None,
            handed_target_host=handed_rec.target_host if handed_rec else None,
            handed_target_id=handed_rec.target_id if handed_rec else None,
            is_untouched_baton_target=untouched,
            chain_id=chain_rec.chain_id if chain_rec else None,
            chain_depth=chain_rec.chain_depth if chain_rec else 0,
            sort_epoch=float(item["epoch"]),
        ))
    candidates.sort(key=lambda c: (c.sort_epoch, c.session_id), reverse=True)
    return tuple(candidates)


def _select_latest_candidate(paths: HostEnvironmentPaths, binding: WorkspaceProjectBinding, candidates: Sequence[HandoffCandidate]) -> HandoffCandidate:
    if not candidates:
        raise ValueError(f"No handoff candidates found for workspace {str(binding.workspace_path)!r}.")
    eligible = [c for c in candidates if c.turn_or_step_count > 0]
    if not eligible:
        raise ValueError(f"No completed handoff candidates found for workspace {str(binding.workspace_path)!r}.")
    non_untouched = [c for c in eligible if not c.is_untouched_baton_target]
    chosen = non_untouched[0] if non_untouched else eligible[0]
    for p_path, rec, _ in load_workspace_receipts(paths, binding.workspace_path):
        if rec.status == "pending" and (
            (rec.source_host == chosen.host.value and rec.source_id == chosen.session_id)
            or (rec.target_id and rec.target_host == chosen.host.value and rec.target_id == chosen.session_id)
        ):
            raise HandoffPendingError(rec, p_path)
    read_handoff_source(paths, binding, chosen.direction, chosen.session_id)
    return chosen


def _choose_interactive_candidate(
    candidates: Sequence[HandoffCandidate],
    *,
    chooser: Callable[[Sequence[HandoffCandidate]], HandoffCandidate] | None,
    stdin_isatty: bool | None,
    stderr_isatty: bool | None,
) -> HandoffCandidate:
    if chooser is not None:
        if not candidates:
            raise ValueError("No handoff candidates found in this workspace.")
        return chooser(candidates)
    in_tty = bool(sys.stdin.isatty()) if stdin_isatty is None else stdin_isatty
    err_tty = bool(sys.stderr.isatty()) if stderr_isatty is None else stderr_isatty
    if not (in_tty and err_tty):
        raise ValueError("Interactive session picker (--pick) requires a TTY. Pass --latest, --chain, or --source-id explicitly (or run in a TTY with --pick).")
    if not candidates:
        raise ValueError("No handoff candidates found in this workspace.")
    for idx, cand in enumerate(candidates, start=1):
        tag = " (untouched baton target)" if cand.is_untouched_baton_target else (
            f" (handed -> {cand.handed_target_host}:{cand.handed_target_id[:8]})" if cand.already_handed_ready and cand.handed_target_id else ""
        )
        print(f"[{idx}] {cand.direction.value} | {cand.title} | {cand.session_id} | count={cand.turn_or_step_count}{tag}", file=sys.stderr)
    sys.stderr.write(f"Select candidate [1-{len(candidates)}] (or q to cancel): ")
    sys.stderr.flush()
    raw_choice = sys.stdin.readline().strip()
    if not raw_choice or raw_choice.lower() in {"q", "quit", "cancel", "exit"}:
        raise HandoffCancelledError("Interactive handoff selection cancelled.")
    try:
        picked_idx = int(raw_choice)
    except ValueError as exc:
        raise ValueError(f"Invalid selection {raw_choice!r}; expected 1-{len(candidates)} or 'q'.") from exc
    if picked_idx < 1 or picked_idx > len(candidates):
        raise ValueError(f"Selection {picked_idx} out of range 1-{len(candidates)}.")
    return candidates[picked_idx - 1]


def _resolve_chain_request(
    paths: HostEnvironmentPaths,
    binding: WorkspaceProjectBinding,
    direction: SyncDirection | None,
    source_id: str | None,
    title: str | None,
    paired_now: bool,
) -> ResolvedHandoffRequest:
    if source_id is not None:
        validate_session_id(source_id, label="source_id")
    tip = resolve_chain_tip(load_workspace_receipts(paths, binding.workspace_path), source_id)
    if tip is None:
        if source_id is not None:
            eff_dir = direction or infer_direction_from_source_id(paths, source_id, binding=binding)
            assert_source_belongs_to_binding(paths, binding, eff_dir, source_id)
            return ResolvedHandoffRequest(binding, eff_dir, source_id, title, "chain", chain_id=generate_uuid7(), chain_depth=0, paired_now=paired_now)
        chosen = _select_latest_candidate(paths, binding, list_handoff_candidates(paths, binding, direction=direction))
        return ResolvedHandoffRequest(binding, chosen.direction, chosen.session_id, title, "chain", chain_id=generate_uuid7(), chain_depth=0, paired_now=paired_now)

    if tip.target_id and _did_target_advance_beyond_seed(paths, binding, tip, propagate_active=True) is True:
        next_dir = SyncDirection.AGY_TO_CODEX if tip.target_host == HostKind.ANTIGRAVITY.value else SyncDirection.CODEX_TO_AGY
        if direction is not None and direction != next_dir:
            raise ValueError(f"--direction {direction.value} contradicts --chain next hop {next_dir.value} from baton {tip.target_id}.")
        return ResolvedHandoffRequest(
            binding, next_dir, tip.target_id, title, "chain",
            chain_id=tip.chain_id or generate_uuid7(), chained_from_source_id=tip.source_id, chain_depth=tip.chain_depth + 1, paired_now=paired_now,
        )

    s_dir = SyncDirection.CODEX_TO_AGY if tip.source_host == HostKind.CODEX.value else SyncDirection.AGY_TO_CODEX
    s_ledger = _read_ledger_for_chain(paths, binding, s_dir, tip.source_id, propagate_active=True)
    s_wm = s_ledger.latest_watermark() if s_ledger is not None else None
    if s_wm is not None and str(s_wm.boundary_digest) != tip.source_digest:
        if direction is not None and direction != s_dir:
            raise ValueError(f"--direction {direction.value} contradicts --chain source re-handoff {s_dir.value} from {tip.source_id}.")
        return ResolvedHandoffRequest(
            binding, s_dir, tip.source_id, title, "chain",
            chain_id=tip.chain_id or generate_uuid7(), chained_from_source_id=tip.chained_from_source_id, chain_depth=tip.chain_depth + 1, paired_now=paired_now,
        )

    if direction is not None and direction != s_dir:
        raise ValueError(f"--direction {direction.value} contradicts --chain tip {s_dir.value} untouched baton state for {tip.target_id or tip.source_id}.")
    return ResolvedHandoffRequest(
        binding, s_dir, tip.source_id, title, "chain",
        chain_id=tip.chain_id, chained_from_source_id=tip.chained_from_source_id, chain_depth=tip.chain_depth,
        already_ready_receipt=dataclasses.replace(tip, status="no_op"), paired_now=paired_now,
    )


def resolve_handoff_request(
    paths: HostEnvironmentPaths,
    workspace: str | Path,
    *,
    direction: SyncDirection | None = None,
    source_id: str | None = None,
    title: str | None = None,
    latest: bool = False,
    chain: bool = False,
    pick: bool = False,
    pair_if_offline: bool = False,
    interactive_chooser: Callable[[Sequence[HandoffCandidate]], HandoffCandidate] | None = None,
    stdin_isatty: bool | None = None,
    stderr_isatty: bool | None = None,
) -> ResolvedHandoffRequest:
    """Resolve CLI/API handoff flags into a concrete direction, source ID, and chain lineage."""
    validate_handoff_selector_flags(source_id=source_id, latest=latest, chain=chain, pick=pick)
    if direction is not None and direction not in (SyncDirection.CODEX_TO_AGY, SyncDirection.AGY_TO_CODEX):
        raise ValueError("Handoff direction must be codex_to_agy or agy_to_codex.")

    paired_now = False
    diagnosis = diagnose_workspace_binding(paths, workspace)
    if not diagnosis.is_paired():
        if not pair_if_offline:
            raise UnpairedWorkspaceError(diagnosis)
        binding = guarded_pair_workspace(paths, PresenceWalGuard(paths), ProjectRegistry(paths), workspace)
        paired_now = True
    else:
        binding = ProjectRegistry(paths).resolve_readonly_binding(workspace)

    if chain:
        return _resolve_chain_request(paths, binding, direction, source_id, title, paired_now)
    if source_id is not None:
        validate_session_id(source_id, label="source_id")
        eff_dir = direction if direction is not None else infer_direction_from_source_id(paths, source_id, binding=binding)
        assert_source_belongs_to_binding(paths, binding, eff_dir, source_id)
        return ResolvedHandoffRequest(binding, eff_dir, source_id, title, "source_id", paired_now=paired_now)
    if latest:
        chosen = _select_latest_candidate(paths, binding, list_handoff_candidates(paths, binding, direction=direction))
        return ResolvedHandoffRequest(binding, chosen.direction, chosen.session_id, title, "latest", paired_now=paired_now)
    if interactive_chooser is None:
        in_tty = bool(sys.stdin.isatty()) if stdin_isatty is None else stdin_isatty
        err_tty = bool(sys.stderr.isatty()) if stderr_isatty is None else stderr_isatty
        if not (in_tty and err_tty):
            raise ValueError("Interactive session picker (--pick) requires a TTY. Pass --latest, --chain, or --source-id explicitly (or run in a TTY with --pick).")
    chosen = _choose_interactive_candidate(
        list_handoff_candidates(paths, binding, direction=direction),
        chooser=interactive_chooser, stdin_isatty=stdin_isatty, stderr_isatty=stderr_isatty,
    )
    return ResolvedHandoffRequest(binding, chosen.direction, chosen.session_id, title, "pick", paired_now=paired_now)


def execute_handoff(
    paths: HostEnvironmentPaths,
    workspace: str | Path,
    *,
    direction: SyncDirection | None = None,
    source_id: str | None = None,
    title: str | None = None,
    latest: bool = False,
    chain: bool = False,
    pick: bool = False,
    pair_if_offline: bool = False,
    interactive_chooser: Callable[[Sequence[HandoffCandidate]], HandoffCandidate] | None = None,
    stdin_isatty: bool | None = None,
    stderr_isatty: bool | None = None,
) -> tuple[HandoffReceipt, bool]:
    """Resolve a handoff request and execute it, returning `(receipt, mutated_state)`."""
    resolved = resolve_handoff_request(
        paths, workspace, direction=direction, source_id=source_id, title=title,
        latest=latest, chain=chain, pick=pick, pair_if_offline=pair_if_offline,
        interactive_chooser=interactive_chooser, stdin_isatty=stdin_isatty, stderr_isatty=stderr_isatty,
    )
    if resolved.already_ready_receipt is not None:
        return resolved.already_ready_receipt, resolved.paired_now
    receipt = handoff_session(
        paths, resolved.binding.workspace_path, direction=resolved.direction, source_id=resolved.source_id,
        title=resolved.title, chain_id=resolved.chain_id, chained_from_source_id=resolved.chained_from_source_id,
        chain_depth=resolved.chain_depth,
    )
    return receipt, True
