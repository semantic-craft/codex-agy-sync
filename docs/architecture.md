# Architecture rationale (`codex-agy-sync`)

## Problem

`codex-agy-sync` synchronizes agent coding sessions bidirectionally between Google Antigravity 2.30+ (`LanguageServerService` Connect RPC and `~/.gemini/antigravity/conversations/<id>.db`) and OpenAI Codex 0.166+ (`codex app-server` JSON-RPC v2 and `~/.codex/state_5.sqlite` plus `rollout-*.jsonl`) inside a shared workspace directory. Phase 1 schema extraction and Phase A architectural grounding exposed three structural mismatches that break naive translators:
1. **Multi-Store Project Identity & Sidebar Filtering**: Each host hides imported sessions from its desktop sidebar unless multi-store project bindings and strict column predicates hold. On Antigravity, `project_id` must match `~/.gemini/config/projects/<uuid>.json` and never fall into `{"", "default-cli-project", "outside-of-project", "null", "none"}`, `source` must never equal enum `17` (`CORTEX_TRAJECTORY_SOURCE_CLI`), `nesting_depth` must equal `0`, title must agree across `annotations/<id>.pbtxt`, `CascadeTrajectorySummary.annotations.title`, and `conversation_summaries.title`, and `agy --conversation=<id>` requires `brain/<id>/.system_generated/logs/transcript.jsonl`. On Codex, partial SQLite indexes require `threads.archived == 0`, `threads.preview <> ''`, `threads.project_id IS NOT NULL`, and `.codex-global-state.json` `app-server-project-id-by-legacy-project-id-by-host` bridge entries.
2. **Flat Polymorphic Step Stream vs Turn-Scoped Rollout Log**: Antigravity stores a flat 0-indexed `steps` table where `steps.step_type` is an enum (`14`, `15`, `23`, `81`, `82`, `101`, `132`) while `steps.step_payload` is a `gemini_coder.Step` protobuf using distinct oneof wire tags (`19`, `20`, `30`, `93`, `94`, `114`, `140`), and where one `CortexStepPlannerResponse` (`step_type = 15`) declares `N` tool calls whose execution outcomes arrive in `N` separate `CortexStepGeneric` (`step_type = 132`) rows carrying `google.protobuf.Any` (`exa.cortex.core.tools.*`) payloads. Codex stores turn-scoped JSONL items (`reasoning`, `message`, `function_call`, `function_call_output`).
3. **Concurrent WAL Locks & Divergence**: Both hosts hold active SQLite WAL connections while running. Uncoordinated writes corrupt databases or get overwritten by in-memory Hub caches (`agyhub_summaries_proto.pb`). Synchronization must route through online loopback RPC when daemons are live or guarded offline writes when stopped, advancing cursors only at completed-turn boundaries and halting or forking on independent divergence.

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

# 1. Pair a local workspace folder across Antigravity 2.30+ and Codex 0.166+
bridge = SyncBridge()
binding = bridge.pair_workspace(Path.cwd())

# 2. Incrementally sync an Antigravity cascade into Codex (auto-selecting Online RPC vs Offline WAL-safe mode)
receipt = bridge.sync_session(
    binding.workspace_path,
    cascade_id=CascadeId("eb95dc40-cb5e-4017-b4ff-f41223e1f701"),
    direction=SyncDirection.AUTO_INCREMENTAL,
    mode=TransportMode.AUTO,
    fork_policy=ForkResolutionPolicy.FORK_NEW_SESSION,
)

# 3. Inspect the indexed canonical event ledger and project companion views without mutating state
ledger = bridge.inspect_session_ledger(
    binding.workspace_path,
    codex_thread_id=receipt.codex_thread_id,
)
watermark = ledger.latest_watermark()
transcript_rows = ledger.project_transcript_rows()

# 4. Batch-sync every discovered session in the workspace idempotently
all_receipts = bridge.sync_workspace(
    binding.workspace_path,
    fork_policy=ForkResolutionPolicy.HALT_ON_DIVERGENCE,
)
```

## Shape

The synthesized architecture lives in three domain modules (`src/codex_agy_sync/ledger.py`, `src/codex_agy_sync/codecs.py`, `src/codex_agy_sync/bridge.py`) and one package root (`src/codex_agy_sync/__init__.py`), keeping every call chain within three files (`per principle-minimize-reader-load`).

1. **Indexed Canonical Event Ledger (`TrajectoryLedger` in `ledger.py`, `per principle-model-the-domain`).**
   - The IR is an immutable append-only tuple of discriminated `CanonicalEvent` nodes (`UserInputEvent`, `ThinkingEvent`, `AssistantTextEvent`, `ToolCallEvent`, `ToolResultEvent`, `TaskBoundaryEvent`, `CheckpointEvent`, `SystemEvent`).
   - `TrajectoryLedger.from_events` builds four O(1) secondary lookup indices in one pass:
     - `by_step_index: Mapping[StepIndex, Tuple[LedgerSeqNo, ...]]` resolves the 1-to-N fanout where one Antigravity `PLANNER_RESPONSE` (`step_type = 15`) unpacks into `ThinkingEvent`, `AssistantTextEvent`, and `N` `ToolCallEvent` nodes sharing the same `StepIndex`.
     - `by_call_id: Mapping[ToolCallId, Tuple[LedgerSeqNo, LedgerSeqNo | None]]` joins each `CortexStepGeneric` (`step_type = 132`) `ToolResultEvent` back to its declaring `ChatToolCall` via `CortexStepMetadata.tool_call.id` (field 4).
     - `by_turn_id: Mapping[TurnId, Tuple[LedgerSeqNo, ...]]` groups events by logical user turn.
     - `completed_turn_watermarks: Tuple[CompletedTurnWatermark, ...]` records SHA-256 boundary digests (`ContentDigest`) exclusively for turns whose tool calls have all resolved, ensuring in-flight turns never advance a sync cursor.
   - Every `EventOriginAnchor` carries `UnknownWireField` and `HostExtensionEnvelope` tuples so unrecognized protobuf tags and host extensions (`encrypted_content`, `world_state`) survive round-trip conversion without loss.
   - Companion artifacts (`TranscriptRowProjection`, `AnnotationPbtxtProjection`, `CascadeSummaryProjection`, `SummariesStateProjection` for offline `agyhub_summaries_proto.pb` rebuild when Hub is stopped, and `CodexThreadSummaryProjection`) are pure projection methods on `TrajectoryLedger`, backed by `resolve_canonical_title` enforcing the `sidebar_visibility_rules.json` `title_resolution_order` precedence (`annotations/<id>.pbtxt` `title` > `CascadeTrajectorySummary.annotations.title` > `conversation_summaries.title` / `threads.title` > latest `CheckpointEvent.conversation_title` > first `UserInputEvent.query_text` prefix).

2. **Table-Driven Wire & Tool Codec Registry (`STEP_WIRE_TABLE` and `TOOL_CODEC_TABLE` in `codecs.py`, `per principle-model-the-domain` and `per principle-boundary-discipline`).**
   - `STEP_WIRE_TABLE` binds every `CortexStepTypeCode` (`14`, `15`, `23`, `81`, `82`, `101`, `132`) to its distinct `GeminiStepWireTag` (`19`, `20`, `30`, `93`, `94`, `114`, `140`) in `gemini_coder.Step` and its Codex record category in one declarative table.
   - `TOOL_CODEC_TABLE` binds all 16 top-level `exa.cortex.core.tools.*` `google.protobuf.Any` type URLs (`ReadFile`, `FileEdit` with `diff_stats`, `RunCommand`, `SearchWeb`, `InvokeSubagent`, `DefineSubagent`, `ManageSubagents`, `ManageTask`, `SendMessage`, `Schedule`, `AskQuestion` with structured `QuestionItem`/`QuestionOption`, `ListResources`, `ReadResource`, `GenerateImage`, `MomaSearch`, `Finish`) to typed `CanonicalToolPayload` union variants and Codex `function_call` / `custom_tool_call` schemas, with `OpaqueToolPayload` preserving unknown tool bytes.

3. **Branded Primitives & Sidebar-Safe Types (`per principle-type-system-discipline`).**
   - `WorkspacePath`, `WorkspaceRootUri`, `AgyProjectId`, `CodexProjectId`, `CodexLegacyProjectId`, `CascadeId`, `TrajectoryId`, `CodexThreadId`, `StepIndex`, `RolloutLineNo`, `TurnId`, `ToolCallId`, `LedgerSeqNo`, and `ContentDigest` are distinct `NewType` brands validated at boundaries (`parse_workspace_identity`).
   - `AgyGuiSafeSource` enumerates only GUI-visible sources (`0`, `1`, `18`, `19`, `20`) and omits `17` (`CORTEX_TRAJECTORY_SOURCE_CLI`), making CLI-hidden trajectory writes unrepresentable.
   - `CsrfSecret` wraps the loopback `x-codeium-csrf-token` from `hub_<port>.json` with redacted `__repr__`, `__str__`, and `__format__`.

4. **Pure Cursor/Fork State Machine, WAL/Presence Guard, and Deep `SyncBridge` Facade (`bridge.py`, `per principle-make-operations-idempotent`).**
   - `PresenceWalGuard` tests `~/.gemini/antigravity/presence/<cascade_id>.lock` via non-blocking `flock(LOCK_EX | LOCK_NB)`, checks `-wal` activity and daemon liveness, enforces `file:<path>?mode=ro` on all SQLite reads, and writes a self-describing `BackupManifest` (`restoration_command` + SHA-256 map) before any offline mutation.
   - `CursorForkStateMachine.evaluate` is a pure function returning a 5-variant discriminated `CursorTransition` (`NoOpTransition`, `InitialImportTransition`, `IncrementalAppendTransition`, `DivergedConflictTransition` with pre-planned common ancestor and fork slice, `DeferredBusyTransition`).
   - `SyncBridge` exposes a four-method public surface (`pair_workspace`, `inspect_session_ledger`, `sync_session`, `sync_workspace`) that hides six underlying metadata stores, dual-mode RPC/SQLite switching, companion file generation, and atomic cursor persistence.

## Synthesis decision

Two structurally distinct candidates were generated in parallel during Arena Phase B and evaluated criterion-by-criterion by an independent Cross-Judge (`flash` subagent `c8dcd101-0a6e-4fdd-aed0-59b9de60b69e`):

| Rubric Criterion | Candidate 1 (`pro`): Turn-Aggregate & Pure Reducer | Candidate 2 (`inherit`): Canonical Indexed Event-Ledger & Codec Table | Verdict & Key Difference |
| --- | :---: | :---: | --- |
| 1. Domain & Invariant Encoding | 6 / 10 | **10 / 10** | C2 brands all 12 IDs/indices via `NewType`, redacts `CsrfSecret`, omits CLI source `17` from `AgyGuiSafeSource`, and enforces `CodexThreadSummaryProjection(archived=0, preview != '')`. |
| 2. Project Pairing Completeness | 7 / 10 | **10 / 10** | C2 models all 3 project IDs (`AgyProjectId`, `CodexProjectId`, `CodexLegacyProjectId`) and canonicalizes `(WorkspacePath, WorkspaceRootUri)`. |
| 3. Trajectory IR & Companion Fidelity | 7 / 10 | **10 / 10** | C2's 4-index `TrajectoryLedger` resolves 1-to-N (`step 15` -> N `step 132`) fanout in O(1) while preserving exact `StepIndex` for `GetCascadeTrajectorySteps(step_offset)` and `transcript.jsonl` coalescing. |
| 4. Dual-Mode Sync & Fork State Machine | 8 / 10 | **9 / 10** | C2 explicitly models `flock(LOCK_EX \| LOCK_NB)` on `presence/<id>.lock` and PID liveness; C1 contributed the self-describing `BackupManifest` and pre-planned fork slice. |
| 5. Interface Depth & Boundary Discipline | 7 / 10 | **10 / 10** | C2's 4-method `SyncBridge` is a deep module hiding 6 stores and keeping wire envelopes (`AgyRawStepRow`, `CodexRawRolloutLine`) private to `codecs.py`. |
| 6. Reader Load & Agent-Safety | 7 / 10 | **10 / 10** | C2 eliminates hand-synced `if/elif` chains via `STEP_WIRE_TABLE` and `TOOL_CODEC_TABLE`. |
| **Total** | **42 / 60** | **59 / 60** | **Base: Candidate 2** |

**Grafts folded from Candidate 1 into Candidate 2 (Phase E):**
1. **Lossless Wire Envelopes (`UnknownWireField` and `HostExtensionEnvelope`)**: Folded into `EventOriginAnchor` so unknown protobuf wire tags and Codex `encrypted_content` / `world_state` blobs round-trip losslessly.
2. **Deterministic Multi-Store Title Resolver (`resolve_canonical_title`)**: Folded into `ledger.py` following `sidebar_visibility_rules.json` `title_resolution_order` (`annotations/<id>.pbtxt` > `CascadeTrajectorySummary.annotations.title` > `conversation_summaries.title` / `threads.title` > latest `CheckpointEvent.conversation_title` > first `UserInputEvent.query_text` prefix).
3. **Self-Describing `BackupManifest`**: Folded into `PresenceWalGuard.create_offline_backup` and `SyncReceipt.backup_manifest`, including `file_sha256_map` and `restoration_command`.
4. **Pre-Planned Fork Target & Delta Slice on `DivergedConflictTransition`**: Folded `common_ancestor_watermark`, `preallocated_fork_session_id`, and `fork_events_to_apply` into `DivergedConflictTransition`.
5. **Rich Tool Payloads & Offline Hub `SummariesStateProjection`**: Folded `diff_stats: Tuple[int, int]` into `FileEditPayload`, structured `QuestionItem`/`QuestionOption` into `AskQuestionPayload`, all 16 top-level `exa.cortex.core.tools.*` payloads (`DefineSubagentPayload`, `ListResourcesPayload`, `ReadResourcePayload`, `GenerateImagePayload`, `MomaSearchPayload`, `FinishPayload`), and `SummariesStateProjection` (`jetbox_summaries_pb.SummariesState`, strictly for offline cache invalidation/rebuild when Hub is stopped) into `TrajectoryLedger`.

## Tradeoffs accepted

- We accept building four O(1) secondary indices eagerly in `TrajectoryLedger.from_events` in exchange for eliminating repeated O(N) scans during `step_type = 15` <-> `step_type = 132` tool call correlation, `transcript.jsonl` coalescing, and incremental watermark slicing.
- We accept maintaining an explicit `CanonicalToolPayload` union for all 16 top-level `exa.cortex.core.tools.*` protobuf payloads plus `OpaqueToolPayload` in exchange for rich desktop UI rendering (diff stats in `FileEdit`, exit codes and sandbox details in `RunCommand`, citations in `SearchWeb`) instead of degrading tool history into flat text blobs.
- We accept deferring synchronization of in-flight turns (`has_inflight_turn == True` or `DeferredBusyTransition`) in exchange for guaranteeing that neither host ever observes half-executed tool calls or corrupted WAL transactions.

## Alternatives considered

- **Pure Hierarchical Turn-Tree AST as Primary Storage (Candidate 1's pure tree without flat step index).** Storing the trajectory solely as a nested tree (`CompletedTurn -> PlannerReasoningRound -> ToolInvocation`) loses direct O(1) addressing by Antigravity `StepIndex` and forces every mid-session `Checkpoint` (`step_type = 23`), `TaskBoundary` (`step_type = 81`), or `SystemMessage` (`step_type = 101`) to be artificially attached to a turn node. Retaining the flat append-only `TrajectoryLedger` with `by_step_index`, `by_call_id`, `by_turn_id`, and `completed_turn_watermarks` gives both exact step-offset addressing (`GetCascadeTrajectorySteps`) and atomic completed-turn slicing in one structure.
- **Direct Point-to-Point Streaming Translator (No Intermediate Ledger).** Streaming `gemini_coder.Step` rows directly into `rollout-*.jsonl` lines without materializing an indexed ledger fails because a single Antigravity `PLANNER_RESPONSE` (`step_type = 15`) declares `N` tool calls whose outputs arrive in `N` separate `GENERIC` steps (`step_type = 132`), and generating `transcript.jsonl`, `CascadeTrajectorySummary`, and SHA-256 turn watermarks requires random-access correlation by `StepIndex` and `ToolCallId`.

## Implementation reconciliation

Stopped at Architect Phase C checkpoint for human review prior to Phase D implementation.

## Open questions and risks

1. When `CursorForkStateMachine` detects `DivergedConflictTransition` under `ForkResolutionPolicy.FORK_NEW_SESSION`, should the newly forked session on the target host receive a visible title suffix (for example, `"[Forked]"` in `annotations/<id>.pbtxt` and `threads.title`), or keep the identical title and rely solely on `parent_conversation_id` / `attached_thread_parent_id`?
2. When translating a Codex `exec_command` call that ran `cat` or `sed -n` into Antigravity, should `TrajectoryWireCodec` synthesize an `exa.cortex.core.tools.ReadFile` payload whenever the command matches a pure file-read pattern, or preserve it as `exa.cortex.core.tools.RunCommand` unless it originated from an Antigravity `ReadFile` step?
3. On this host, `/Applications/Antigravity.app/Contents/Resources/bin/language_server` was updated after Phase 1 commit `aa6ebb9` to match `agy` CLI (`sha256: e4c014b...`, adding field 15 `coordinator` to `exa.project_pb.Project`). Should Phase D start by refreshing `schema/antigravity_2_30_schema.json` via `scripts/extract_schema.py` so `scripts/check` reflects the updated binary, or pin `scripts/check` to validate against the committed `schema/` snapshot unless `--refresh-schema` is passed?

## Next implementation step

After Phase C sign-off, implement `parse_workspace_identity`, `resolve_canonical_title`, `TrajectoryLedger.from_events`, and the table-driven `TrajectoryWireCodec` (`STEP_WIRE_TABLE` and `TOOL_CODEC_TABLE`) with unit tests against the captured Phase 1 Antigravity `steps` blobs and Codex `rollout-*.jsonl` fixtures.
