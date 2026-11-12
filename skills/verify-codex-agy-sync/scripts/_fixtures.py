"""Deterministic sandbox fixtures for verify-codex-agy-sync."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]

FIXTURE_CHOICES = (
    "codex-basic",
    "codex-append-turn",
    "agy-basic",
    "agy-interrupted",
    "subagent-pair",
    "diverged-pair",
)


def _ensure_src_on_syspath() -> None:
    src_str = str(REPO_ROOT / "src")
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seed_fixture(layout: dict[str, Path], fixture: str) -> dict[str, Any]:
    _ensure_src_on_syspath()
    from codex_agy_sync.bridge import (
        DefaultHostTransport,
        HostEnvironmentPaths,
        SyncBridge,
        SyncDirection,
        TransportMode,
    )
    from codex_agy_sync.ledger import (
        AssistantTextEvent,
        CascadeId,
        CodexThreadId,
        EventOriginAnchor,
        HostKind,
        InvokeSubagentPayload,
        ReadFilePayload,
        RunCommandPayload,
        StepIndex,
        SubagentLineage,
        ThinkingEvent,
        ToolCallEvent,
        ToolCallId,
        ToolResultEvent,
        TrajectoryLedger,
        TurnId,
        UserInputEvent,
    )

    sandbox_paths = HostEnvironmentPaths(
        gemini_config_dir=layout["gemini_config_dir"],
        agy_data_dir=layout["agy_data_dir"],
        codex_home_dir=layout["codex_home_dir"],
        sync_state_dir=layout["sync_state_dir"],
    )
    bridge = SyncBridge(paths=sandbox_paths)
    binding = bridge.pair_workspace(layout["workspace_dir"])
    transport = DefaultHostTransport(sandbox_paths)

    if fixture == "codex-basic":
        tid = CodexThreadId("01960000-0000-7000-8000-000000000011")
        locks = bridge.guard.probe_locks(None, tid)
        cmd_payload = RunCommandPayload(
            kind="run_command",
            command_line="ls -la",
            cwd=str(binding.workspace_path),
            exit_code=0,
        )
        t1 = EventOriginAnchor(HostKind.CODEX, TurnId("turn-1"), "2026-01-15T10:00:00Z")
        t2 = EventOriginAnchor(HostKind.CODEX, TurnId("turn-2"), "2026-01-15T10:01:00Z")
        events = [
            UserInputEvent(kind="user_input", anchor=t1, query_text="Inspect workspace files"),
            ThinkingEvent(kind="thinking", anchor=t1, summary_text="Listing files in workspace."),
            ToolCallEvent(
                kind="tool_call",
                anchor=t1,
                call_id=ToolCallId("call-codex-1"),
                tool_name="exec_command",
                tool_summary="ls -la",
                payload=cmd_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=t1,
                call_id=ToolCallId("call-codex-1"),
                tool_name="exec_command",
                output_text="Process exited with code 0\nREADME.md\n",
                is_error=False,
                payload=cmd_payload,
            ),
            AssistantTextEvent(kind="assistant_text", anchor=t1, text="Workspace contains README.md."),
            UserInputEvent(kind="user_input", anchor=t2, query_text="Confirm repository readiness"),
            AssistantTextEvent(
                kind="assistant_text",
                anchor=t2,
                text="Repository is ready for sync verification.",
            ),
        ]
        ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            events,
            explicit_title_override="Codex Seed Session",
        )
        transport.apply_to_codex(
            binding=binding,
            thread_id=tid,
            events=events,
            full_ledger=ledger,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )
        return {"status": "SEEDED", "fixture": fixture, "codex_thread_id": str(tid)}

    if fixture == "codex-append-turn":
        tid = CodexThreadId("01960000-0000-7000-8000-000000000011")
        locks = bridge.guard.probe_locks(None, tid)
        existing = transport.read_codex_ledger(binding, tid, locks, TransportMode.OFFLINE_STORAGE)
        t3 = EventOriginAnchor(HostKind.CODEX, TurnId("turn-3"), "2026-01-15T10:02:00Z")
        delta = [
            UserInputEvent(kind="user_input", anchor=t3, query_text="Add turn 3"),
            AssistantTextEvent(kind="assistant_text", anchor=t3, text="Turn 3 complete"),
        ]
        merged = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            list(existing.events) + delta,
            explicit_title_override=existing.title,
        )
        transport.apply_to_codex(
            binding=binding,
            thread_id=tid,
            events=delta,
            full_ledger=merged,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )
        return {"status": "SEEDED", "fixture": fixture, "codex_thread_id": str(tid)}

    if fixture == "agy-basic":
        cid = CascadeId("00000000-0000-4000-8000-000000000021")
        locks = bridge.guard.probe_locks(cid, None)
        rf_payload = ReadFilePayload(
            kind="read_file",
            path=str(binding.workspace_path / "README.md"),
            start_line=1,
            end_line=20,
        )
        t1 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), "2026-01-15T11:00:00Z")
        t2 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-2"), "2026-01-15T11:01:00Z")
        events = [
            UserInputEvent(kind="user_input", anchor=t1, query_text="Read README.md"),
            ThinkingEvent(kind="thinking", anchor=t1, summary_text="Reading project README."),
            ToolCallEvent(
                kind="tool_call",
                anchor=t1,
                call_id=ToolCallId("call-agy-1"),
                tool_name="view_file",
                tool_summary="README.md",
                payload=rf_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=t1,
                call_id=ToolCallId("call-agy-1"),
                tool_name="view_file",
                output_text="# Sample Repo\n",
                is_error=False,
                payload=rf_payload,
            ),
            AssistantTextEvent(kind="assistant_text", anchor=t1, text="Read README.md successfully."),
            UserInputEvent(kind="user_input", anchor=t2, query_text="Provide turn 2 status"),
            AssistantTextEvent(kind="assistant_text", anchor=t2, text="AGY turn 2 complete."),
        ]
        ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            events,
            explicit_title_override="AGY Seed Session",
            recompute_agy_step_indices=True,
        )
        transport.apply_to_agy(
            binding=binding,
            cascade_id=cid,
            events=ledger.events,
            full_ledger=ledger,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )
        return {"status": "SEEDED", "fixture": fixture, "cascade_id": str(cid)}

    if fixture == "agy-interrupted":
        cid = CascadeId("00000000-0000-4000-8000-000000000051")
        locks = bridge.guard.probe_locks(cid, None)
        t1 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), "2026-01-15T11:30:00Z")
        t2 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-2"), "2026-01-15T11:31:00Z")
        events = [
            UserInputEvent(kind="user_input", anchor=t1, query_text="Complete initial check"),
            AssistantTextEvent(kind="assistant_text", anchor=t1, text="Initial check finished."),
            UserInputEvent(kind="user_input", anchor=t2, query_text="Run test suite before stop"),
            ThinkingEvent(kind="thinking", anchor=t2, summary_text="About to execute pytest."),
            ToolCallEvent(
                kind="tool_call",
                anchor=t2,
                call_id=ToolCallId("call-agy-interrupted-2"),
                tool_name="run_command",
                tool_summary="pytest -q",
                payload=RunCommandPayload(
                    kind="run_command",
                    command_line="pytest -q",
                    cwd=str(binding.workspace_path),
                ),
            ),
        ]
        ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            events,
            explicit_title_override="AGY Interrupted Session",
            recompute_agy_step_indices=True,
        )
        transport.apply_to_agy(
            binding=binding,
            cascade_id=cid,
            events=ledger.events,
            full_ledger=ledger,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )
        return {"status": "SEEDED", "fixture": fixture, "cascade_id": str(cid)}

    if fixture == "subagent-pair":
        parent_cid = CascadeId("00000000-0000-4000-8000-000000000031")
        child_cid = CascadeId("00000000-0000-4000-8000-000000000032")
        locks = bridge.guard.probe_locks(parent_cid, None)
        spawn_payload = InvokeSubagentPayload(
            kind="invoke_subagent",
            subagent_type="research",
            role="Explorer",
            prompt="Explore sample repository",
            spawned_conversation_id=str(child_cid),
        )
        p_t1 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), "2026-01-15T12:00:00Z")
        parent_events = [
            UserInputEvent(kind="user_input", anchor=p_t1, query_text="Delegate exploration to subagent"),
            ToolCallEvent(
                kind="tool_call",
                anchor=p_t1,
                call_id=ToolCallId("call-sub-1"),
                tool_name="invoke_subagent",
                tool_summary="Explorer",
                payload=spawn_payload,
            ),
            ToolResultEvent(
                kind="tool_result",
                anchor=p_t1,
                call_id=ToolCallId("call-sub-1"),
                tool_name="invoke_subagent",
                output_text="Spawned Explorer subagent.",
                is_error=False,
                payload=spawn_payload,
            ),
            AssistantTextEvent(kind="assistant_text", anchor=p_t1, text="Subagent completed exploration."),
        ]
        parent_ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            parent_events,
            explicit_title_override="Root Parent Session",
            recompute_agy_step_indices=True,
        )
        transport.apply_to_agy(
            binding=binding,
            cascade_id=parent_cid,
            events=parent_ledger.events,
            full_ledger=parent_ledger,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )

        child_lineage = SubagentLineage(
            parent_cascade_id=parent_cid,
            root_cascade_id=parent_cid,
            nesting_depth=1,
            agent_type_name="research",
            agent_role="Explorer",
            spawn_step_index=StepIndex(1),
        )
        c_t1 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), "2026-01-15T12:00:02Z")
        child_events = [
            UserInputEvent(kind="user_input", anchor=c_t1, query_text="Explore sample repository"),
            AssistantTextEvent(kind="assistant_text", anchor=c_t1, text="Subagent findings ready."),
        ]
        child_ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            child_events,
            explicit_title_override="Explorer Subagent",
            recompute_agy_step_indices=True,
        )
        child_locks = bridge.guard.probe_locks(child_cid, None)
        transport.apply_to_agy(
            binding=binding,
            cascade_id=child_cid,
            events=child_ledger.events,
            full_ledger=child_ledger,
            lock_snapshot=child_locks,
            mode=TransportMode.OFFLINE_STORAGE,
            parent_cascade_id=parent_cid,
            lineage=child_lineage,
        )
        sidecar = (
            layout["agy_data_dir"]
            / "brain"
            / str(parent_cid)
            / ".system_generated"
            / "subagents"
            / f"{child_cid}.json"
        )
        _write_json(
            sidecar,
            {
                "conversationId": str(child_cid),
                "subagentDescriptor": {"typeName": "research", "role": "Explorer"},
                "spawnStepIndex": 1,
                "workspaceUris": [str(binding.workspace_uri)],
            },
        )
        return {
            "status": "SEEDED",
            "fixture": fixture,
            "parent_cascade_id": str(parent_cid),
            "child_cascade_id": str(child_cid),
        }

    if fixture == "diverged-pair":
        cid = CascadeId("00000000-0000-4000-8000-000000000041")
        tid = CodexThreadId("01960000-0000-7000-8000-000000000041")
        locks = bridge.guard.probe_locks(cid, tid)
        t1 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-1"), "2026-01-15T13:00:00Z")
        turn1_events = [
            UserInputEvent(kind="user_input", anchor=t1, query_text="Shared turn 1"),
            AssistantTextEvent(kind="assistant_text", anchor=t1, text="Shared turn 1 complete."),
        ]
        turn1_ledger = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            turn1_events,
            explicit_title_override="Diverged Pair Session",
            recompute_agy_step_indices=True,
        )
        transport.apply_to_agy(
            binding=binding,
            cascade_id=cid,
            events=turn1_ledger.events,
            full_ledger=turn1_ledger,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
        )
        bridge.sync_session(
            binding.workspace_path,
            cascade_id=cid,
            codex_thread_id=tid,
            direction=SyncDirection.AGY_TO_CODEX,
            mode=TransportMode.OFFLINE_STORAGE,
        )

        agy_base = transport.read_agy_ledger(binding, cid, locks, TransportMode.OFFLINE_STORAGE)
        a_t2 = EventOriginAnchor(HostKind.ANTIGRAVITY, TurnId("turn-2"), "2026-01-15T13:01:00Z")
        agy_turn2 = [
            UserInputEvent(kind="user_input", anchor=a_t2, query_text="AGY diverged turn"),
            AssistantTextEvent(kind="assistant_text", anchor=a_t2, text="AGY diverged response."),
        ]
        agy_merged = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            list(agy_base.events) + agy_turn2,
            explicit_title_override=agy_base.title,
            recompute_agy_step_indices=True,
        )
        transport.apply_to_agy(
            binding=binding,
            cascade_id=cid,
            events=agy_turn2,
            full_ledger=agy_merged,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )

        codex_base = transport.read_codex_ledger(binding, tid, locks, TransportMode.OFFLINE_STORAGE)
        c_t2 = EventOriginAnchor(HostKind.CODEX, TurnId("turn-2"), "2026-01-15T13:01:10Z")
        codex_turn2 = [
            UserInputEvent(kind="user_input", anchor=c_t2, query_text="Codex diverged turn"),
            AssistantTextEvent(kind="assistant_text", anchor=c_t2, text="Codex diverged response."),
        ]
        codex_merged = TrajectoryLedger.from_events(
            binding.workspace_path,
            binding.workspace_uri,
            list(codex_base.events) + codex_turn2,
            explicit_title_override=codex_base.title,
        )
        transport.apply_to_codex(
            binding=binding,
            thread_id=tid,
            events=codex_turn2,
            full_ledger=codex_merged,
            lock_snapshot=locks,
            mode=TransportMode.OFFLINE_STORAGE,
            incremental=True,
        )
        return {
            "status": "SEEDED",
            "fixture": fixture,
            "cascade_id": str(cid),
            "codex_thread_id": str(tid),
        }

    raise ValueError(f"Unsupported fixture {fixture!r}.")
