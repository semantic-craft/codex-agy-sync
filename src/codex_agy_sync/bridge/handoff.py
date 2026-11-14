from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Literal

from ..codecs import TrajectoryWireCodec, serialize_tool_payload_arguments_json
from ..codecs.tools import safe_json_dict
from ..ledger import AgyGuiSafeSource, AssistantTextEvent, CascadeId, CheckpointEvent, CodexThreadId, HostKind, SubagentLineage, TrajectoryLedger, UserInputEvent, ThinkingEvent, ToolCallEvent, ToolResultEvent, SystemEvent, parse_workspace_identity, validate_session_id
from .paths_and_locks import HostEnvironmentPaths, PresenceWalGuard, TransportMode, _atomic_write_text, _now_iso
from .registry import ProjectRegistry, WorkspaceProjectBinding
from .rescue_and_fork import SyncBusyLockError
from . import rpc
from .state_machine import SyncDirection, generate_uuid7
from .transport import DefaultHostTransport


_CODEX_RUNTIME_EVENTS = frozenset({
    "session_meta", "world_state", "codex_developer_message",
    "codex_event_msg:thread_settings_applied", "codex_response_item:additional_tools",
})

@dataclass(frozen=True)
class HandoffReceipt:
    workspace: str
    source_host: str
    source_id: str
    source_digest: str
    target_host: str
    target_id: str | None
    title: str
    status: Literal["pending", "ready", "no_op"]
    staging_path: str
    runtime_state_not_transferred: bool = True
    error: str | None = None
    created_at_iso: str = ""
    chain_id: str | None = None
    chained_from_source_id: str | None = None
    chain_depth: int = 0
    target_seed_turn_count: int | None = None
    target_seed_text_digest: str | None = None


_RECEIPT_FIELDS = frozenset(field.name for field in dataclasses.fields(HandoffReceipt))


def load_handoff_receipt(path: Path) -> HandoffReceipt:
    """Parse a persisted handoff receipt JSON file while preserving legacy schema compatibility."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid handoff receipt payload in {path}: expected JSON object.")
    return HandoffReceipt(**{key: value for key, value in raw.items() if key in _RECEIPT_FIELDS})


def ledger_seed_fingerprint(ledger: TrajectoryLedger) -> tuple[int, str]:
    """Compute `(completed_turn_count, sha256_digest)` for completed user/assistant texts without extra RPCs."""
    pairs: list[list[str]] = []
    for event in ledger.slice_after_watermark(None):
        if isinstance(event, UserInputEvent):
            if not _host_injected_user(event):
                pairs.append(["user_input", event.query_text.strip()])
        elif isinstance(event, AssistantTextEvent):
            pairs.append(["assistant_text", event.text.strip()])
    digest = hashlib.sha256(
        json.dumps(pairs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return len(ledger.completed_turn_watermarks), digest


class HandoffPendingError(RuntimeError):
    def __init__(self, receipt: HandoffReceipt, receipt_path: Path) -> None:
        self.receipt = receipt
        super().__init__(f"Handoff remains pending; target={receipt.target_id or 'unknown'}. Inspect {receipt_path} before retrying; no new session was allocated on retry. Cause: {receipt.error or 'Previous operation did not finish.'}")


def _save_receipt(path: Path, receipt: HandoffReceipt) -> None:
    _atomic_write_text(path, json.dumps(dataclasses.asdict(receipt), ensure_ascii=False, indent=2) + "\n")


def _receipt_matches_workspace(receipt_ws: str, ws_path: Path) -> bool:
    if receipt_ws == str(ws_path):
        return True
    with contextlib.suppress(ValueError):
        return parse_workspace_identity(receipt_ws)[0] == ws_path
    return False


def _require_binding(paths: HostEnvironmentPaths, workspace: str | Path) -> WorkspaceProjectBinding:
    from .handoff_resolve import UnpairedWorkspaceError, diagnose_workspace_binding

    diagnosis = diagnose_workspace_binding(paths, workspace)
    if not diagnosis.is_paired():
        raise UnpairedWorkspaceError(diagnosis)
    return ProjectRegistry(paths).resolve_readonly_binding(workspace)


def _read_source(paths: HostEnvironmentPaths, binding: WorkspaceProjectBinding, direction: SyncDirection, source_id: str) -> TrajectoryLedger:
    from ._handoff_scan import assert_source_belongs_to_binding

    assert_source_belongs_to_binding(paths, binding, direction, source_id)
    transport = DefaultHostTransport(paths)
    to_agy = direction == SyncDirection.CODEX_TO_AGY
    locks = PresenceWalGuard(paths).probe_locks(None if to_agy else CascadeId(source_id), CodexThreadId(source_id) if to_agy else None)
    endpoint = locks.codex_daemon if to_agy else locks.agy_daemon
    mode = TransportMode.ONLINE_RPC if endpoint is not None else TransportMode.OFFLINE_STORAGE
    if not to_agy and endpoint is None and locks.agy_presence_locked:
        raise ValueError("Source Antigravity session is locked and its live status cannot be read.")
    if to_agy:
        ledger, _, active = transport.read_codex_ledger_with_meta(binding, CodexThreadId(source_id), locks, mode)
    else:
        active = endpoint is not None and rpc.fetch_agy_trajectory_via_rpc(endpoint, CascadeId(source_id)).is_active
        if active:
            raise ValueError("Source session is active. Finish the turn before handoff.")
        ledger, _, _ = transport.read_agy_ledger_with_meta(binding, CascadeId(source_id), locks, TransportMode.OFFLINE_STORAGE)
    if active or ledger.has_inflight_turn:
        raise ValueError("Source session is active or has an incomplete turn. Finish the turn before handoff.")
    if ledger.latest_watermark() is None:
        raise ValueError("Source session has no completed history to hand off.")
    return ledger


def _checkpoint_text(event: CheckpointEvent) -> str:
    summary = event.session_summary
    for extension in event.anchor.host_extensions:
        if extension.extension_type == "turn_context" and extension.raw_payload:
            summary = str(json.loads(extension.raw_payload).get("summary") or "")
    parts = [event.user_intent, summary if summary not in ("none", "turn_context") else "", event.code_change_summary]
    return "\n".join(part for part in parts if part.strip())


def _host_injected_user(event: UserInputEvent) -> bool:
    if event.anchor.origin_host != HostKind.CODEX:
        return False
    for extension in event.anchor.host_extensions:
        if extension.extension_type != "codex_message_provenance":
            continue
        data = safe_json_dict(extension.raw_payload.decode("utf-8", errors="replace"))
        metadata = data.get("content_item_metadata")
        kinds = data.get("content_item_kinds") or []
        if any(isinstance(kind, str) and kind.startswith("user.") for kind in kinds):
            return False
        return isinstance(metadata, list) and bool(metadata) and all(
            isinstance(item, dict) and isinstance(item.get("provenance"), dict)
            and item["provenance"].get("type") in {"agents_md", "harness"} for item in metadata
        )
    return False


def _host_injected_tool_result(event: ToolResultEvent) -> bool:
    if event.anchor.origin_host != HostKind.CODEX:
        return False
    for extension in event.anchor.host_extensions:
        if extension.extension_type == "codex_unpaired_tool_output":
            data = safe_json_dict(extension.raw_payload.decode("utf-8", errors="replace"))
            return (not data.get("call_id") and data.get("namespace") == "agent"
                    and isinstance(data.get("name"), str) and data["name"].startswith("codex_apps_"))
    return False


def _handoff_ledger(ledger: TrajectoryLedger, title: str | None) -> TrajectoryLedger:
    events = []
    calls = {event.call_id for event in ledger.events if isinstance(event, ToolCallEvent)}
    for event in ledger.slice_after_watermark(None):
        if (isinstance(event, SystemEvent) and event.anchor.origin_host == HostKind.CODEX
                and event.event_type in _CODEX_RUNTIME_EVENTS):
            continue
        if isinstance(event, UserInputEvent) and _host_injected_user(event):
            continue
        if isinstance(event, ToolResultEvent) and event.call_id not in calls and _host_injected_tool_result(event):
            continue
        anchor = dataclasses.replace(event.anchor, host_extensions=(), unknown_wire_fields=())
        if isinstance(event, ThinkingEvent):
            if event.summary_text:
                events.append(AssistantTextEvent(kind="assistant_text", anchor=anchor, text="[Imported reasoning summary]\n" + event.summary_text))
        elif isinstance(event, CheckpointEvent):
            if summary := _checkpoint_text(event):
                events.append(AssistantTextEvent(kind="assistant_text", anchor=anchor, text="[Imported checkpoint summary]\n" + summary))
        elif isinstance(event, ToolCallEvent):
            text = f"[Imported tool call] {event.tool_name}\nCall ID: {event.call_id}\nArguments:\n{serialize_tool_payload_arguments_json(event.payload)}"
            events.append(AssistantTextEvent(kind="assistant_text", anchor=anchor, text=text))
        elif isinstance(event, ToolResultEvent):
            text = f"[Imported tool result] {event.tool_name}\nCall ID: {event.call_id}\nis_error: {str(event.is_error).lower()}\nOutput:\n{event.output_text}"
            if event.full_output_uri:
                text += f"\nFull output URI: {event.full_output_uri}"
            events.append(AssistantTextEvent(kind="assistant_text", anchor=anchor, text=text))
        else:
            events.append(dataclasses.replace(event, anchor=anchor))
    return TrajectoryLedger.from_events(ledger.workspace_path, ledger.workspace_uri, events, explicit_title_override=title or ledger.title)


def _conversation_text(ledger: TrajectoryLedger) -> list[tuple[str, str]]:
    return [(event.kind, event.query_text if isinstance(event, UserInputEvent) else event.text)
            for event in ledger.events if isinstance(event, (UserInputEvent, AssistantTextEvent))]


def _verify_context(expected: TrajectoryLedger, actual: TrajectoryLedger) -> None:
    if actual.latest_watermark() is None or _conversation_text(expected) != _conversation_text(actual):
        raise RuntimeError("Imported user and assistant history does not match the completed source conversation.")


def _stage_agy(paths: HostEnvironmentPaths, binding: WorkspaceProjectBinding, ledger: TrajectoryLedger, stage: Path, target_id: str) -> tuple[Path, ...]:
    staged_paths = dataclasses.replace(paths, agy_data_dir=stage, agy_cli_data_dir=None)
    transport = DefaultHostTransport(staged_paths)
    cid = CascadeId(target_id)
    lineage = SubagentLineage(root_cascade_id=cid)
    db, rows = transport._write_agy_conversation_db(binding, cid, ledger.events, ledger, AgyGuiSafeSource.CASCADE_CLIENT, False, lineage, None)
    persisted = TrajectoryWireCodec.decode_agy_steps_to_ledger(binding.workspace_path, binding.workspace_uri, rows, explicit_title_override=ledger.title)
    _verify_context(ledger, persisted)
    projection = persisted.project_agy_cascade_summary(cascade_id=cid, project_id=binding.agy_project_id, viewed_at_iso=_now_iso(), source=AgyGuiSafeSource.UNSPECIFIED, lineage=lineage)
    transport._write_agy_brain_and_pbtxt(binding, cid, persisted, projection, _now_iso(), lineage)
    return (stage / "annotations" / f"{cid}.pbtxt", stage / "brain" / str(cid) / ".system_generated" / "logs" / "transcript.jsonl", db)


def _publish_new_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".handoff-", delete=False) as dst:
            temporary = Path(dst.name)
            with source.open("rb") as src:
                shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _import_agy(paths: HostEnvironmentPaths, binding: WorkspaceProjectBinding, ledger: TrajectoryLedger, receipt: HandoffReceipt) -> str:
    stage = Path(receipt.staging_path)
    target_id = receipt.target_id
    assert target_id is not None
    files = _stage_agy(paths, binding, ledger, stage, target_id)
    for file in files:
        _publish_new_file(file, paths.agy_data_dir / file.relative_to(stage))
    endpoint = PresenceWalGuard(paths)._discover_agy_daemon()
    result = rpc.fetch_agy_trajectory_via_rpc(endpoint, CascadeId(target_id), timeout_s=30.0)
    if len(result.raw_steps) != len(DefaultHostTransport(dataclasses.replace(paths, agy_data_dir=stage))._read_persisted_agy_steps(files[-1])):
        raise RuntimeError("Antigravity did not load all staged steps; handoff remains pending.")
    loaded = TrajectoryWireCodec.decode_agy_steps_to_ledger(binding.workspace_path, binding.workspace_uri, result.raw_steps)
    _verify_context(ledger, loaded)
    return target_id


def _import_codex(paths: HostEnvironmentPaths, binding: WorkspaceProjectBinding, ledger: TrajectoryLedger, receipt: HandoffReceipt) -> str:
    stage = Path(receipt.staging_path)
    stage.mkdir(parents=True, exist_ok=True)
    rollout = stage / "rollout.jsonl"
    rows = TrajectoryWireCodec.encode_ledger_to_codex_rollout(ledger, session_id=generate_uuid7(), cwd=str(binding.workspace_path))
    with rollout.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(row.raw_json for row in rows) + "\n")
    return str(rpc.import_codex_rollout_via_rpc(
        PresenceWalGuard(paths)._discover_codex_daemon(), rollout, binding.workspace_path,
        str(binding.codex_project_id), ledger.title, timeout_s=90.0,
    ))


def _find_prior_chain_receipt(
    state: Path,
    ws_path: Path,
    source_host: HostKind,
    source_id: str,
) -> tuple[str, str, int] | None:
    matches: list[HandoffReceipt] = []
    for existing in sorted(state.glob("*.json")):
        try:
            previous = load_handoff_receipt(existing)
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            continue
        if (
            previous.status == "ready"
            and _receipt_matches_workspace(previous.workspace, ws_path)
            and previous.target_host == source_host.value
            and previous.target_id == source_id
        ):
            matches.append(previous)
    if not matches:
        return None
    prev = max(matches, key=lambda item: (item.chain_depth, item.created_at_iso))
    return (prev.chain_id or generate_uuid7(), prev.source_id, prev.chain_depth + 1)


def _ensure_no_pending_sibling(
    state: Path,
    receipt_path: Path,
    ws_path: Path,
    source_host: HostKind,
    source_id: str,
) -> None:
    for existing in sorted(state.glob("*.json")):
        if existing == receipt_path:
            continue
        try:
            previous = load_handoff_receipt(existing)
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            continue
        if (
            previous.status == "pending"
            and _receipt_matches_workspace(previous.workspace, ws_path)
            and previous.source_host == source_host.value
            and previous.source_id == source_id
        ):
            raise HandoffPendingError(previous, existing)


def _handoff_locked(
    paths: HostEnvironmentPaths,
    binding: WorkspaceProjectBinding,
    direction: SyncDirection,
    source_id: str,
    title: str | None,
    state: Path,
    *,
    chain_id: str | None = None,
    chained_from_source_id: str | None = None,
    chain_depth: int = 0,
) -> HandoffReceipt:
    ledger = _read_source(paths, binding, direction, source_id)
    watermark = ledger.latest_watermark()
    assert watermark is not None
    to_agy = direction == SyncDirection.CODEX_TO_AGY
    source_host = HostKind.CODEX if to_agy else HostKind.ANTIGRAVITY
    target_host = HostKind.ANTIGRAVITY if to_agy else HostKind.CODEX
    identity = [str(binding.workspace_uri), source_host.value, source_id, str(watermark.boundary_digest)]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    receipt_path = state / f"{key}.json"
    if receipt_path.exists():
        previous = load_handoff_receipt(receipt_path)
        if previous.status == "ready":
            return dataclasses.replace(previous, status="no_op")
        raise HandoffPendingError(previous, receipt_path)
    _ensure_no_pending_sibling(state, receipt_path, binding.workspace_path, source_host, source_id)
    guard = PresenceWalGuard(paths)
    if (guard._discover_agy_daemon() if to_agy else guard._discover_codex_daemon()) is None:
        raise RuntimeError("Open the target Desktop app before handoff.")
    handed = _handoff_ledger(ledger, title)
    seed_turns, seed_digest = ledger_seed_fingerprint(handed)
    eff_chain_id, eff_from_source, eff_depth = chain_id, chained_from_source_id, chain_depth
    if eff_chain_id is None:
        inherited = _find_prior_chain_receipt(state, binding.workspace_path, source_host, source_id)
        if inherited is not None:
            eff_chain_id, eff_from_source, eff_depth = inherited
    receipt = HandoffReceipt(
        str(binding.workspace_path),
        source_host.value,
        source_id,
        str(watermark.boundary_digest),
        target_host.value,
        generate_uuid7() if to_agy else None,
        handed.title,
        "pending",
        str(state / "staging" / key),
        created_at_iso=_now_iso(),
        chain_id=eff_chain_id,
        chained_from_source_id=eff_from_source,
        chain_depth=eff_depth,
        target_seed_turn_count=seed_turns,
        target_seed_text_digest=seed_digest,
    )
    _save_receipt(receipt_path, receipt)
    try:
        target_id = _import_agy(paths, binding, handed, receipt) if to_agy else _import_codex(paths, binding, handed, receipt)
    except Exception as exc:
        receipt = dataclasses.replace(receipt, error=f"{type(exc).__name__}: {exc}")
        if getattr(exc, "thread_id", None):
            receipt = dataclasses.replace(receipt, target_id=str(exc.thread_id))
        _save_receipt(receipt_path, receipt)
        raise HandoffPendingError(receipt, receipt_path) from exc
    receipt = dataclasses.replace(receipt, target_id=target_id, status="ready")
    _save_receipt(receipt_path, receipt)
    return receipt


def handoff_session(
    paths: HostEnvironmentPaths,
    workspace: str | Path,
    *,
    direction: SyncDirection,
    source_id: str,
    title: str | None = None,
    chain_id: str | None = None,
    chained_from_source_id: str | None = None,
    chain_depth: int = 0,
) -> HandoffReceipt:
    """Create a new Desktop session once per completed source history."""
    if direction not in (SyncDirection.CODEX_TO_AGY, SyncDirection.AGY_TO_CODEX):
        raise ValueError("Handoff requires an explicit direction.")
    validate_session_id(source_id, label="source_id")
    binding = _require_binding(paths, workspace)
    state = paths.sync_state_dir / "handoffs"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "handoff.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncBusyLockError(
                "Another handoff operation is currently running; wait and retry.",
                host="handoff",
                remediation=(f'Wait for the active handoff to finish, then retry: codex-agy-sync handoff -w "{binding.workspace_path}"',),
            ) from exc
        return _handoff_locked(
            paths,
            binding,
            direction,
            source_id,
            title,
            state,
            chain_id=chain_id,
            chained_from_source_id=chained_from_source_id,
            chain_depth=chain_depth,
        )
