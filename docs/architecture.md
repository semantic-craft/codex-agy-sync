# Architecture rationale (`codex-agy-sync`)

This document describes the architecture of `codex-agy-sync`, covering the `A2 + B2 + C1` bidirectional synchronization engine, Desktop handoff pipeline, online project registration and cleanup RPCs, the bounded `menubar-feed` scanner and native Swift 6 macOS menu bar app (`CodexAgyHandoff.app`), and the Stage-1 Local Workbench (`codex-agy-sync serve`). Desktop handoff preserves visible history and flattened tool summaries across newly allocated native sessions rather than replaying host runtime state.

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
   - `workspace.py` centralizes branded IDs (`CascadeId`, `CodexThreadId`, `WorkspacePath`, `WorkspaceRootUri`, `AgyProjectId`, `CodexProjectId`, `CodexLegacyProjectId`), `FORBIDDEN_AGY_PROJECT_IDS`, `HostKind`, `AgyGuiSafeSource`, `CsrfSecret` redaction, workspace canonicalization, session ID validation (`validate_session_id`), and sensitive path exclusions.
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

4. **Tier 3 (`bridge/`): Dual-Mode `A2` Bridge, Online RPC, and Desktop Handoff Subpackage.**
   - `paths_and_locks.py` resolves runtime host paths (`HostEnvironmentPaths`, including optional `agy_cli_data_dir`), discovers live daemons and symlinked macOS Codex Unix sockets (`PresenceWalGuard`, `is_pid_alive`, `is_sqlite_shm_wal_locked`), holds exclusive `flock` locks (`acquire_offline_write_locks`), and creates rollback-capable `BackupManifest` snapshots.
   - `store_catalog.py` provides shared SQLite DDL helpers and read-only store, lineage, and sidecar readers across hosts.
   - `registry.py` manages `ProjectRegistry` pairing (`pair_workspace_with_mode`, `guarded_pair_workspace`), `diagnose_workspace_binding`, and read-only session discovery.
   - `rpc.py` implements proxy-free loopback Connect RPC `fetch_agy_trajectory_via_rpc` and Unix-socket WebSocket `UnixWebSocketClient` / `fetch_codex_thread_via_rpc`.
   - `project_rpc.py` handles Antigravity `CreateProject` RPC (with pre-scan duplicate-name disambiguation on HTTP 409), official Antigravity cleanup RPCs (`delete_agy_cascade_via_rpc` calling `DeleteCascadeTrajectory` + best-effort `JetboxDeleteSummary` + `agy_cli_data_dir/brain/<cid>` cleanup, and `delete_agy_project_via_rpc` calling `DeleteProject`), and Codex `project/list` + `project/create` over `codex app-server`.
   - `state_machine.py` provides monotonic RFC 9562 `generate_uuid7()` and pure `CursorForkStateMachine.evaluate` transitions.
   - `native_codex.py` handles Codex rollout import and lifecycle orchestration over `codex app-server` (`thread/fork`, project assignment, unsubscribe, and unload wait).
   - `transport.py`, `rescue_and_fork.py`, and `sync.py` implement `DefaultHostTransport`, single-pipeline transactional writes, automatic `BackupManifest.restore()` rollback, `NoOpTransition` self-healing, and symmetric `FORK_NEW_SESSION` branching (`SyncBridge`).
   - `handoff_store.py`, `handoff.py`, `handoff_resolve.py`, and `handoff_reconcile.py` manage receipt/candidate scanning, staging cleanup helpers (`validated_handoff_staging_dir`, `cleanup_handoff_staging_dir`, auto-called on `status == "ready"` while preserved when `pending`), Desktop handoff publication (`handoff_session`), shared request resolution (`execute_handoff`), and pending-receipt reconciliation (`reconcile_workspace_handoffs`).

5. **Tier 4 (`workbench/`) & Tier 5 (`cli.py`, `__init__.py`, `__main__.py`): Feed Scanner, Stage-1 Local Workbench, and `C1` CLI.**
   - `workbench/` implements bounded read-only feed scanning with online RPC `IDLE` (`cascade_run_status == 1` and `thread/read`) override over host-wide `-wal` locks (`menubar_feed.py`), domain models (`models.py`), union-find workspace discovery (`discovery.py`), single-pass preloaded SQLite host catalog scanning (`catalog.py`), tiered on-demand search indexing with WAL-aware `(mtime_ns, size, wal_mtime_ns, wal_size)` caching (`indexer.py`), `0o600` token-free lease persistence & packaged UI resolution (`lease.py`), service orchestration (`service.py`), token/loopback HTTP dispatch (`handler.py`), loopback HTTP daemon (`server.py`), and packaged built-in live UI (`ui/index.html`).
   - `cli.py`, `__init__.py`, `__main__.py`, and `bin/codex-agy-sync` expose `C1` subcommands (`pair`, `status`, `inspect`, `sync-session`, `sync-workspace`, `handoff`, `menubar-feed`, `serve`) and structured exit-code-2 JSON diagnostics.

6. **Native macOS Menu Bar Package (`macos/`): Swift 6 App (`CodexAgyHandoff.app`).**
   - `macos/Package.swift` targets macOS 14+ with `CodexAgyHandoffCore` (`SubprocessCLIClient`, `HandoffViewModel`, `PopoverView`, `SettingsView`, `PreferencesStore`, `MenuBarIconRenderer`, `UpdateService`, `AppActivator`), the executable target `CodexAgyHandoff` (`StatusBarController`, `SettingsWindowController`, `GlobalHotKey`, `main.swift`), and `CodexAgyHandoffTests`.
   - `scripts/build_menubar_app.sh` and `scripts/generate_app_icon.swift` sync `CFBundleShortVersionString` from `pyproject.toml`, generate the 10-size Retina `AppIcon.icns`, codesign inside a staging directory, and swap the bundle atomically with zero background polling.

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

The modular 6-tier package layout is enforced by `./scripts/check` (`scripts/check_architecture.py` and `tests/test_architecture_coherence.py`):
- **Core wire & codec layers (Tiers 0-2):** `_wire.py` (`< 220` LOC), `workspace.py`, `payloads.py`, `events.py`, `ledger.py`, and `codecs/*` (`< 700` LOC per submodule) unify table-driven protobuf codecs (`STEP_WIRE_TABLE`, `TOOL_CODEC_TABLE`), `.jsonl`/`.jsonl.zst` compression/streaming, and companion projections.
- **Bridge, pairing, and handoff convergence (Tier 3):** `bridge/*` (`< 700` LOC per submodule, `< 450` LOC for `rpc.py`) separates read-only catalog inspection (`store_catalog.py`), lock guards and backups (`paths_and_locks.py`), offline/hybrid/online project registration and cleanup RPCs (`registry.py`, `project_rpc.py`), offline incremental sync (`state_machine.py`, `transport.py`, `rescue_and_fork.py`, `sync.py`), and Desktop handoff + pending reconciliation + automatic ready-stage cleanup (`handoff_store.py`, `handoff.py`, `handoff_resolve.py`, `handoff_reconcile.py`, `native_codex.py`).
- **Stage-1 Local Workbench, Menu Bar App & CLI (Tiers 4-5 & `macos/`):** `workbench/` pairs single-pass catalog scanning (`catalog.py`, `discovery.py`) and tiered search indexing (`indexer.py`) with bounded `menubar_feed.py` snapshots, loopback token auth (`handler.py`, `server.py`, `lease.py`), packaged live UI (`workbench/ui/index.html`, with mock prototype fallback removed), and `cli.py` structured exit-code-2 JSON diagnostics. `macos/` delivers the zero-polling Swift 6 menu bar palette, native Settings window, 4 adaptive status icon styles, and SHA-256 verified dual App+CLI updater.
- **Verification & hygiene gates:** `./scripts/check` validates schema parity, `?mode=ro` read-only real-host decoding, AST module/tier boundaries, zero hardcoded home paths, zero CSRF leaks, Python regressions, and `[5b/5]` Swift XCTest (`swift test`) alongside isolated sandbox suites (`control_sync_verify.py`, `workbench_verify.py`, and `menubar_verify.py` via `cua-driver`).

## Open questions and risks

1. **Forked session naming (`FORK_NEW_SESSION`).** Resolved by appending ` (Fork)` to forked titles while recording top-level `parent_references` on Antigravity and `attached_thread_parent_id` on Codex with `nesting_depth = 0`.
2. **Command-to-tool mapping.** Resolved by preserving Codex `exec_command` as `RunCommandPayload` unless paired with an explicit `ReadFile` origin, avoiding heuristic false positives.
3. **Binary hash drift check.** Resolved by keeping `scripts/check` pinned against committed `schema/` descriptors while allowing explicit `--refresh-schema` updates when Antigravity upgrades on disk.
