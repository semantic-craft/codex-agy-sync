# Subagent isolation and lock deferral

By default, `status` and `sync-workspace` exclude spawned child subagent sessions so subagent transcripts do not clutter top-level project sidebars, unless `--include-subagents` is passed. When a target session holds an active `presence/<id>.lock` `flock` or requests unimplemented `online_rpc` writes, `sync-session` defers safely as `deferred_busy`.

## Sub-features

- `subagent-default-excluded` omits spawned child subagents from `status` and `sync-workspace` when `--include-subagents` is absent.
- `subagent-explicit-included` discovers and syncs spawned subagents while preserving `nesting_depth >= 1` and parent IDs when `--include-subagents` is passed.
- `lock-flock-deferred` returns `"transition_kind": "deferred_busy"` with zero target writes while `presence/<cascade_id>.lock` holds an active `LOCK_EX`.
- `online-rpc-write-deferred` returns `"transition_kind": "deferred_busy"` when `--mode online_rpc` is requested for a mutating sync.

## How to get to it (user POV)

- Run `./bin/codex-agy-sync status --workspace <path> [--include-subagents]` in a terminal.
- Run `./bin/codex-agy-sync sync-workspace --workspace <path> [--include-subagents]` in a terminal.
- Run `./bin/codex-agy-sync sync-session --workspace <path> ... --mode online_rpc` or while Antigravity holds `presence/<id>.lock`.

## Driving it with control_sync_verify.py

Preconditions:

- `RUN_DIR` is initialized and `doctor` reports `"status": "PASS"`.

- **Seed parent and child subagent cascades.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture subagent-pair`.
- **Confirm default subagent exclusion.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug status-top-only -- status --workspace @WORKSPACE`. Stdout `sessions` contains root cascade `00000000-0000-4000-8000-000000000031` and omits child cascade `00000000-0000-4000-8000-000000000032`.
- **Confirm explicit subagent inclusion and lineage preservation.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug status-with-sub -- status --workspace @WORKSPACE --include-subagents` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug sync-ws-sub -- sync-workspace --workspace @WORKSPACE --direction agy_to_codex --include-subagents`. Both parent and child cascades appear in `status-with-sub`, and `sync-ws-sub` syncs `synced_count: 2`.
- **Confirm active presence lock deferral.** Seed `codex-basic` and hold an exclusive `flock` on target `00000000-0000-4000-8000-000000000099` during `sync-session`. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture codex-basic` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py hold-lock --run-dir "$RUN_DIR" --cascade-id 00000000-0000-4000-8000-000000000099 --slug sync-locked -- sync-session --workspace @WORKSPACE --codex-thread-id 01960000-0000-7000-8000-000000000011 --cascade-id 00000000-0000-4000-8000-000000000099 --direction codex_to_agy`. Receipt reports `"transition_kind": "deferred_busy"` and `"events_applied": 0`.
- **Confirm online RPC write deferral.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug sync-online-defer -- sync-session --workspace @WORKSPACE --codex-thread-id 01960000-0000-7000-8000-000000000011 --cascade-id 00000000-0000-4000-8000-000000000098 --direction codex_to_agy --mode online_rpc`. Receipt reports `"transition_kind": "deferred_busy"`.

## Gotchas

- `hold-lock` acquires the `flock` in a separate file descriptor for the duration of the child CLI invocation and releases it automatically afterward. Never create locks inside `$HOME/.gemini/antigravity/presence/`.
- Test `--mode online_rpc` on an unsynced pair (`initial_import` or `incremental_append` candidate); on an already-synced pair, `sync_session` currently short-circuits to `no_op` before checking daemon availability.
