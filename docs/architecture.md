# Architecture rationale (`codex-agy-sync`)

This document describes the offline synchronization engine. For Desktop handoff through new native sessions, see [Desktop 按需交接](../README.md#desktop-按需交接). That path preserves tool history as text rather than replaying host runtime state.

## Problem

`codex-agy-sync` synchronizes agent coding sessions bidirectionally between Google Antigravity 2.30+ (`LanguageServerService` Connect RPC and `~/.gemini/antigravity/conversations/<id>.db`) and OpenAI Codex 0.166+ (`codex app-server` JSON-RPC v2 and `~/.codex/state_5.sqlite` plus `rollout-*.jsonl`) inside a shared workspace directory. Phase 1 schema extraction and architectural grounding exposed three structural mismatches that break naive translators.

1. **Multi-Store Project Identity and Sidebar Filtering.** Each host hides imported sessions from its desktop sidebar unless multi-store project bindings and strict column predicates hold. On Antigravity, `project_id` must match `~/.gemini/config/projects/<uuid>.json` and never fall into `{"", "default-cli-project", "outside-of-project", "null", "none"}`, `source` must never equal enum `17` (`CORTEX_TRAJECTORY_SOURCE_CLI`), `nesting_depth` must equal `0` for root cascades even when `agent_name` is populated, title must agree across `annotations/<id>.pbtxt`, `CascadeTrajectorySummary.annotations.title`, and `conversation_summaries.title`, and `agy --conversation=<id>` requires `brain/<id>/.system_generated/logs/transcript.jsonl` across both `antigravity` and `antigravity-cli`. On Codex, SQLite indexes (`idx_threads_archived_cwd_recency_at_ms`, `idx_threads_visible_recency_at_ms`) require `threads.archived == 0`, `threads.preview <> ''`, and non-empty `threads.cwd` (`cwd_non_empty`, while `threads.project_id` may be `NULL`), UUIDv7 thread ordering, and `.codex-global-state.json` (`local-projects`, `electron-saved-workspace-roots`, `thread-project-assignments`).
2. **Flat Polymorphic Step Stream vs Turn-Scoped Rollout Log.** Antigravity stores a flat 0-indexed `steps` table where `steps.step_type` is an enum (`14`, `15`, `17`, `23`, `81`, `82`, `101`, `132`) while `steps.step_payload` is a `gemini_coder.Step` protobuf using distinct oneof wire tags (`19`, `20`, `24`, `30`, `93`, `94`, `114`, `140`), and where one `CortexStepPlannerResponse` (`step_type = 15`) declares `N` tool calls whose execution outcomes arrive in `N` separate `CortexStepGeneric` (`step_type = 132`) rows carrying `google.protobuf.Any` (`exa.cortex.core.tools.*`) payloads. Codex stores turn-scoped JSONL or `.jsonl.zst` items (`reasoning`, `message`, `agent_message`, `function_call`, `custom_tool_call`, `function_call_output`) and exposes live turns through `codex app-server` `thread/read`.
3. **Concurrent WAL Locks, In-Flight Turns, and Divergence.** Both hosts hold active SQLite WAL connections while running. Uncoordinated writes corrupt databases or get overwritten by in-memory Hub caches (`agyhub_summaries_proto.pb`). Synchronization therefore follows Option 1 (`A2 + B2 + C1`). Under `A2`, live source sessions are read lock-free via loopback RPC while target writes execute strictly against closed offline stores under held `flock` locks with automatic `BackupManifest.restore()` rollback. Under `B2`, watermarks advance only on completed turns by default, while `--include-interrupted` seals stopped halfway turns with canceled tool outputs. Under `C1`, execution is purely on-demand via explicit CLI subcommands with zero background polling daemon.

## Usage (caller's view)

```python
from pathlib import Path
from codex_agy_sync import (
    CascadeId,
    ForkResolutionPolicy,
    SyncBridge,
    SyncDirection,
    TransportMode,
)

bridge = SyncBridge()
binding = bridge.pair_workspace(Path.cwd())

receipt = bridge.sync_session(
    binding.workspace_path,
    cascade_id=CascadeId("eb95dc40-cb5e-4017-b4ff-f41223e1f701"),
    direction=SyncDirection.AUTO_INCREMENTAL,
    mode=TransportMode.AUTO,
    fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION,
    include_interrupted=False,
)

ledger = bridge.inspect_session_ledger(
    binding.workspace_path,
    codex_thread_id=receipt.codex_thread_id,
    mode=TransportMode.AUTO,
)
watermark = ledger.latest_watermark()
transcript_rows = ledger.project_transcript_rows()

all_receipts = bridge.sync_workspace(
    binding.workspace_path,
    fork_policy=ForkResolutionPolicy.HALT_ON_DIVERGENCE,
)
```

## Shape

The package under `src/codex_agy_sync/` is organized into a strict 6-tier downward DAG enforced by `./scripts/check` via `scripts/check_architecture.py` (`< 220` LOC for `_wire.py`, `< 450` LOC for `bridge/rpc.py`, `< 700` LOC per submodule in `codecs/` and `bridge/`, `< 800` LOC per file across `src/` and `tests/`, `< 80` LOC per function, zero cycles, zero cross-module `_private` leaks, zero function-scoped internal imports, and zero non-`self`/`cls` private attribute accesses).

1. **Tier 0 (`_wire.py`, `workspace.py`, `payloads.py`): Wire Primitives, Workspace Identity, and Tool Payloads.**
   - `_wire.py` owns the strict 64-bit Protobuf varint encoder/decoder, length-delimited bounds checking, `UnknownWireField` preservation, and ISO-8601 `google.protobuf.Timestamp` helpers.
   - `workspace.py` centralizes branded IDs (`CascadeId`, `CodexThreadId`, `WorkspacePath`, `WorkspaceRootUri`, `AgyProjectId`, `CodexProjectId`, `CodexLegacyProjectId`), `FORBIDDEN_AGY_PROJECT_IDS`, `HostKind`, `AgyGuiSafeSource`, `CsrfSecret` redaction, workspace canonicalization, and sensitive path exclusions.
   - `payloads.py` defines all 16 typed `CanonicalToolPayload` variants plus `OpaqueToolPayload` and `normalize_tool_file_path`.

2. **Tier 1 (`events.py`, `ledger.py`): Canonical Event Model, Ledger, and `B2` Rescue.**
   - `events.py` defines immutable `CanonicalEvent` dataclasses (`UserInputEvent`, `ThinkingEvent`, `AssistantTextEvent`, `ToolCallEvent`, `ToolResultEvent`, `TaskBoundaryEvent`, `CheckpointEvent`, `SystemEvent`), `SubagentLineage`, and companion projection records.
   - `ledger.py` implements `TrajectoryLedger.from_events` with four O(1) lookup structures (`by_step_index`, `by_call_id`, `by_turn_id`, and SHA-256 `completed_turn_watermarks`), `rescue_interrupted_ledger` (`B2`), and pure companion projection methods.

3. **Tier 2 (`codecs/`): Table-Driven Codec Subpackage.**
   - `schema_tags.py` centralizes schema-verified `CortexStepTypeCode`, `GeminiStepWireTag`, `StepStatusCode`, `STEP_WIRE_TABLE`, and `ToolCodecSpec` constants.
   - `tool_payloads.py` and `tools.py` bind all 16 `exa.cortex.core.tools.*` `google.protobuf.Any` payloads to `(encode_fn, decode_fn)` entries in `TOOL_CODEC_TABLE`.
   - `steps.py` encodes and decodes `gemini_coder.Step` rows (including `ERROR_MESSAGE` `step_type = 17` and unknown wire fields).
   - `rollout.py` and `rpc_thread.py` stream `.jsonl` and zstd-compressed `.jsonl.zst` Codex rollouts (`compress_zstd_rollout_bytes`, `decompress_zstd_rollout_bytes`) and decode `codex app-server` `thread/read` objects.
   - `projections.py` encodes and decodes `CascadeTrajectorySummary`, `TrajectoryMetadata`, `Annotations`, and `SummariesState` protobufs.

4. **Tier 3 (`bridge/`): Dual-Mode `A2` Bridge Subpackage.**
   - `paths_and_locks.py` resolves runtime host paths (`HostEnvironmentPaths`), discovers live daemons and symlinked macOS Codex Unix sockets (`PresenceWalGuard`, `is_pid_alive`), holds exclusive `flock` locks (`acquire_offline_write_locks`), and creates rollback-capable `BackupManifest` snapshots.
   - `store_catalog.py` provides shared SQLite DDL helpers and read-only store/lineage/sidecar readers across hosts.
   - `registry.py` manages `ProjectRegistry` pairing, `diagnose_workspace_binding`, `guarded_pair_workspace`, and read-only session discovery.
   - `state_machine.py` provides monotonic RFC 9562 `generate_uuid7()` and pure `CursorForkStateMachine.evaluate` transitions.
   - `rpc.py` implements proxy-free loopback Connect RPC `fetch_agy_trajectory_via_rpc` and Unix-socket WebSocket `fetch_codex_thread_via_rpc`, plus `native_codex.py` rollout import.
   - `transport.py`, `rescue_and_fork.py`, and `sync.py` implement `DefaultHostTransport`, single-pipeline transactional writes (`_execute_write_plan`), automatic `BackupManifest.restore()` rollback, `NoOpTransition` self-healing, and symmetric `FORK_NEW_SESSION` branching.
   - `handoff_store.py`, `handoff.py`, and `handoff_resolve.py` handle receipt/candidate scanning, Desktop handoff publication, and shared request resolution (`execute_handoff`).

5. **Tier 4 (`workbench/`) & Tier 5 (`cli.py`, `__init__.py`, `__main__.py`): Local Workbench and `C1` CLI.**
   - `workbench/` implements local workspace discovery (`discovery.py`), SQLite/rollout FTS indexing (`indexer.py`), `0o600` lease lifecycle (`lease.py`), token-authenticated loopback HTTP handlers (`handler.py`), and `WorkbenchService`/`WorkbenchServer` delegating Desktop handoff to `execute_handoff`.
   - `cli.py`, `__init__.py`, and `bin/codex-agy-sync` expose `C1` subcommands (`pair`, `status`, `inspect`, `sync-session`, `sync-workspace`, `handoff`, `serve`).

## Synthesis decision

Two structurally distinct candidates were evaluated during Arena Phase B by an independent Cross-Judge (`flash` subagent `c8dcd101-0a6e-4fdd-aed0-59b9de60b69e`).

| Rubric Criterion | Candidate 1 (`pro`) Turn-Aggregate | Candidate 2 (`inherit`) Event-Ledger & Codec Table | Verdict & Key Difference |
| --- | :---: | :---: | --- |
| 1. Domain & Invariant Encoding | 6 / 10 | **10 / 10** | C2 brands all 12 IDs/indices via `NewType`, redacts `CsrfSecret`, omits CLI source `17` from `AgyGuiSafeSource`, and enforces `CodexThreadSummaryProjection(archived=0, preview != '')`. |
| 2. Project Pairing Completeness | 7 / 10 | **10 / 10** | C2 models all 3 project IDs (`AgyProjectId`, `CodexProjectId`, `CodexLegacyProjectId`) and canonicalizes `(WorkspacePath, WorkspaceRootUri)`. |
| 3. Trajectory IR & Companion Fidelity | 7 / 10 | **10 / 10** | C2's 4-index `TrajectoryLedger` resolves 1-to-N (`step 15` -> N `step 132`) fanout in O(1) while preserving exact `StepIndex` for `transcript.jsonl` coalescing. |
| 4. Dual-Mode Sync & Fork State Machine | 8 / 10 | **9 / 10** | C2 models `flock(LOCK_EX \| LOCK_NB)` on `presence/<id>.lock` and PID liveness, while C1 contributed `BackupManifest` and pre-planned fork slices. |
| 5. Interface Depth & Boundary Discipline | 7 / 10 | **10 / 10** | C2's 4-method `SyncBridge` hides 6 stores and keeps wire envelopes private to `codecs/` and `bridge/`. |
| 6. Reader Load & Agent-Safety | 7 / 10 | **10 / 10** | C2 eliminates hand-synced `if/elif` chains via `STEP_WIRE_TABLE` and `TOOL_CODEC_TABLE`. |
| **Total** | **42 / 60** | **59 / 60** | **Base Selected Is Candidate 2** |

Five grafts from Candidate 1 were folded into the Candidate 2 base.
1. **Lossless Wire Envelopes.** `UnknownWireField` and `HostExtensionEnvelope` preserve unrecognized protobuf tags and Codex `encrypted_content` / `world_state` blobs across round-trips.
2. **Deterministic Multi-Store Title Resolver.** `resolve_canonical_title` enforces `sidebar_visibility_rules.json` precedence across `.pbtxt`, summary protobufs, SQLite tables, checkpoints, and first user queries.
3. **Transactional `BackupManifest`.** `PresenceWalGuard.create_offline_backup` records SHA-256 checksums, created-file targets, and `restore()` rollback hooks before every offline mutation.
4. **Pre-Planned Fork Slice.** `DivergedConflictTransition` carries `common_ancestor_watermark`, `preallocated_fork_session_id`, and `fork_events_to_apply`.
5. **Rich Tool Payloads and Offline Hub Summary Rebuild.** `CanonicalToolPayload` covers all 16 `exa.cortex.core.tools.*` messages and `SummariesStateProjection` rebuilds `agyhub_summaries_proto.pb` when Hub is offline.

## Tradeoffs accepted

- Eagerly building four O(1) secondary indices in `TrajectoryLedger.from_events` trades linear construction work for O(1) tool-call correlation, `transcript.jsonl` coalescing, and turn-watermark slicing.
- Maintaining 16 typed `CanonicalToolPayload` variants plus `OpaqueToolPayload` trades schema maintenance surface for native desktop UI rendering of diffs, command exit codes, and citations.
- Under `A2`, deferring target writes whenever the target host holds an active daemon or WAL lock trades live hot-injection into an open target window for zero SQLite WAL corruption and zero Hub cache races.
- Deriving a deterministic UUIDv5 target slot before cursor creation on first unpaired import trades random UUIDv7 sorting on initial import for crash-retry idempotency, while occupied slots and `FORK_NEW_SESSION` allocate fresh UUIDv7 identifiers.

## Alternatives considered

- **Pure Hierarchical Turn-Tree AST.** Storing trajectories solely as nested turn trees loses O(1) `StepIndex` addressing and forces mid-session `Checkpoint` or `SystemMessage` steps into artificial turn containers.
- **Direct Point-to-Point Streaming Translator.** Translating `gemini_coder.Step` rows directly into `rollout-*.jsonl` lines without an indexed ledger cannot correlate 1-to-N tool outputs (`step_type = 15` to `step_type = 132`) or compute canonical turn digests.
- **Background Polling Daemon (`C2`/`C3`).** Running a persistent background watcher increases idle lock contention against active desktop WAL writers without user intent, whereas `C1` explicit CLI subcommands synchronize deterministically at handoff boundaries.

## Implementation reconciliation

All six implementation units are reconciled with the modular package layout and enforced by `./scripts/check`.
- **Unit 1 (`_wire.py`, `payloads.py`, `ledger.py`).** Consolidated all Protobuf wire primitives into `_wire.py` (`< 220` LOC) and purified `ledger.py` summary projections.
- **Unit 2 (`codecs/`).** Split the codec layer into focused submodules (`< 700` LOC each) with true `(encode_fn, decode_fn)` dispatch, lossless `scope_items` / `thinking_signature` / `step_type = 17` preservation, and `.jsonl.zst` streaming.
- **Unit 3 (`ledger.py`, `bridge/registry.py`, `bridge/sync.py`, `cli.py`).** Tightened orthogonal `SubagentLineage.is_subagent` detection and added `B2` `--include-interrupted` turn sealing with peer sync-back cursor reconciliation.
- **Unit 4 (`bridge/paths_and_locks.py`, `bridge/state_machine.py`, `bridge/transport.py`, `bridge/sync.py`, `bridge/rescue_and_fork.py`).** Added held `flock` write critical sections, automatic `BackupManifest.restore()` rollback, `NoOpTransition` companion self-heal (including `antigravity-cli/brain`), and single-call symmetric UUIDv7 `FORK_NEW_SESSION` branching.
- **Unit 5 (`bridge/rpc.py`, `codecs/rpc_thread.py`, `bridge/paths_and_locks.py`, `bridge/state_machine.py`, `bridge/transport.py`, `bridge/sync.py`).** Implemented `A2` loopback Connect RPC (`GetCascadeTrajectory`) and Unix-socket WebSocket JSON-RPC (`codex app-server` `thread/read`), active-run deferral (`cascade_run_status in {2, 3, 4}`, active Codex threads, and POSIX `F_GETLK` `-shm` WAL locks), honest `SyncReceipt.transport_used` reporting, and `C1` CLI documentation alignment.
- **Unit 6 (`tests/`, `skills/verify-codex-agy-sync/`, `scripts/check`).** Decomposed the test suite into modular `< 800` LOC files, added the 7-scenario isolated sandbox `suite` runner, and hardened all 9 multi-model adversarial review edge cases.
- **Architectural Coherence Refactor.** Extracted Tier 0 `workspace.py`, Tier 1 `events.py`, `bridge/store_catalog.py`, and `bridge/handoff_store.py`; promoted `workbench/{discovery,lease,handler}.py` and shared `execute_handoff` / `compress_zstd_rollout_bytes` / `is_pid_alive`; reconciled Codex sidebar rules to `cwd_non_empty` (`schema/sidebar_visibility_rules.json`); and added the 5-part AST coherence gate in `scripts/check_architecture.py` plus `tests/test_architecture_coherence.py`.

## Open questions and risks

1. **Forked session naming (`FORK_NEW_SESSION`).** Resolved by appending ` (Fork)` to forked titles while recording top-level `parent_references` on Antigravity and `attached_thread_parent_id` on Codex with `nesting_depth = 0`.
2. **Command-to-tool mapping.** Resolved by preserving Codex `exec_command` as `RunCommandPayload` unless paired with an explicit `ReadFile` origin, avoiding heuristic false positives.
3. **Binary hash drift check.** Resolved by keeping `scripts/check` pinned against committed `schema/` descriptors while allowing explicit `--refresh-schema` updates when Antigravity upgrades on disk.
