# Incremental append and divergence policy

Once a session pair shares a cursor, appending new completed turns on one host triggers `incremental_append`, stopped interrupted turns stay excluded unless `--include-interrupted` (`B2`) seals and rescues them, and independent edits past the cursor on both hosts trigger conflict resolution governed by `--fork-policy` (`halt_on_divergence`, `fork_new_session`, or `backup_and_overwrite_target`).

## Sub-features

- `append-turn` appends newly completed turns from Codex into an already-synced Antigravity cascade while keeping `steps.idx` and `conversation_summaries.step_count` aligned.
- `rescue-interrupted` excludes a stopped mid-tool-call turn by default, seals and syncs it when `--include-interrupted` is passed (`rescued_inflight_turn == true`), and converges to `no_op` on an immediate repeat run.
- `diverge-halt` halts without mutating either store when both hosts diverge and `--fork-policy halt_on_divergence` is set.
- `diverge-fork` derives symmetric top-level forked sessions (`nesting_depth == 0`, `parent_references` populated, old cursor retired to `cursors/superseded/`) under `--fork-policy fork_new_session`.
- `diverge-overwrite` replaces target history after writing a pre-overwrite backup manifest under `--fork-policy backup_and_overwrite_target`.

## How to get to it (user POV)

- Run `./bin/codex-agy-sync sync-session --workspace <path> --cascade-id <uuid> --codex-thread-id <uuid> --direction auto_incremental` after completing a new turn on one host.
- Run `./bin/codex-agy-sync sync-session --workspace <path> --cascade-id <uuid> --codex-thread-id <uuid> --direction agy_to_codex --include-interrupted` after stopping a turn mid-tool-call.
- Run `./bin/codex-agy-sync sync-workspace --workspace <path> --fork-policy <halt_on_divergence|fork_new_session|backup_and_overwrite_target>` when both hosts have diverged.

## Driving it with control_sync_verify.py

Preconditions:

- `RUN_DIR` is initialized and `doctor` reports `"status": "PASS"`.

- **Verify incremental append.** Seed `codex-basic`, run initial `codex_to_agy` sync, append a third turn via `codex-append-turn`, and sync again. Run:
  `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture codex-basic`
  `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug init-c2a -- sync-session --workspace @WORKSPACE --codex-thread-id 01960000-0000-7000-8000-000000000011 --cascade-id 00000000-0000-4000-8000-000000000011 --direction codex_to_agy`
  `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture codex-append-turn`
  `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug append-c2a -- sync-session --workspace @WORKSPACE --codex-thread-id 01960000-0000-7000-8000-000000000011 --cascade-id 00000000-0000-4000-8000-000000000011 --direction auto_incremental`
  `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-append --cascade-id 00000000-0000-4000-8000-000000000011 --codex-thread-id 01960000-0000-7000-8000-000000000011`
  Confirm `"transition_kind": "incremental_append"`, `updated_cursor.last_synced_turn_id == "turn-3"`, and `agy_conv_db.step_count == agy_summary_row.step_count`.
- **Verify stopped interrupted-turn exclusion and rescue.** Seed `agy-interrupted` (`00000000-0000-4000-8000-000000000051`), run default `agy_to_codex` sync, then run `--include-interrupted` twice. Run:
  `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture agy-interrupted`
  `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug int-default -- sync-session --workspace @WORKSPACE --cascade-id 00000000-0000-4000-8000-000000000051 --codex-thread-id 01960000-0000-7000-8000-000000000051 --direction agy_to_codex`
  `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug int-rescue -- sync-session --workspace @WORKSPACE --cascade-id 00000000-0000-4000-8000-000000000051 --codex-thread-id 01960000-0000-7000-8000-000000000051 --direction agy_to_codex --include-interrupted`
  `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug int-noop -- sync-session --workspace @WORKSPACE --cascade-id 00000000-0000-4000-8000-000000000051 --codex-thread-id 01960000-0000-7000-8000-000000000051 --direction agy_to_codex --include-interrupted`
  Confirm `int-default` imports only `turn-1` (`events_applied == 2`, `rescued_inflight_turn == false`), `int-rescue` appends sealed `turn-2` (`"transition_kind": "incremental_append"`, `rescued_inflight_turn == true`), and `int-noop` returns `"transition_kind": "no_op"`.
- **Verify halt on divergence.** Seed `diverged-pair` and run with default `halt_on_divergence`. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture diverged-pair` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug div-halt -- sync-session --workspace @WORKSPACE --cascade-id 00000000-0000-4000-8000-000000000041 --codex-thread-id 01960000-0000-7000-8000-000000000041 --fork-policy halt_on_divergence`. Receipt reports `"transition_kind": "diverged_halted"` and `"events_applied": 0`.
- **Verify fork convergence.** Run workspace sync with `fork_new_session` followed by a second pass to confirm convergence. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug div-fork-1 -- sync-workspace --workspace @WORKSPACE --fork-policy fork_new_session`, `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug div-fork-2 -- sync-workspace --workspace @WORKSPACE --fork-policy fork_new_session`, and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-fork`. All receipts on `div-fork-2` report `"transition_kind": "no_op"`, and `after-fork.json` shows the retired cursor under `superseded_cursors`.

## Gotchas

- `--include-interrupted` rescues only stopped offline turns. An actively running turn (live presence lock or active RPC step status) still defers with `"transition_kind": "deferred_busy"`.
- After `incremental_append`, verify `agy_conv_db.step_count == agy_summary_row.step_count` in `views/after-append.json` to catch companion index drift.
