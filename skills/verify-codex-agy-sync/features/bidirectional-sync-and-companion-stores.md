# Bidirectional sync and companion stores

`sync-session` imports an unpaired Codex thread into Antigravity (or an Antigravity cascade into Codex), writes all required sidebar and CLI companion files, creates a SHA-256 pre-write backup manifest, records a sync cursor, and converges to `no_op` when re-run immediately.

## Sub-features

- `sync-codex-to-agy` imports a Codex thread into Antigravity with `source != 17`, `nesting_depth == 0`, `PRAGMA user_version >= 1`, `.pbtxt` title, `transcript.jsonl`, `conversation_summaries.db`, and `agyhub_summaries_proto.pb`.
- `sync-agy-to-codex` imports an Antigravity cascade into Codex with `archived == 0`, non-empty `preview`, valid `project_id`, and paired rollout JSONL records.
- `sync-rerun-noop` returns `"transition_kind": "no_op"` with `events_applied: 0` when re-executed without new completed turns.

## How to get to it (user POV)

- Run `./bin/codex-agy-sync sync-session --workspace <path> --codex-thread-id <uuid> [--cascade-id <uuid>] --direction codex_to_agy` in a terminal.
- Run `./bin/codex-agy-sync sync-session --workspace <path> --cascade-id <uuid> [--codex-thread-id <uuid>] --direction agy_to_codex` in a terminal.

## Driving it with control_sync_verify.py

Preconditions:

- `RUN_DIR` is initialized and `doctor` reports `"status": "PASS"`.
- Both `codex-basic` (`01960000-0000-7000-8000-000000000011`) and `agy-basic` (`00000000-0000-4000-8000-000000000021`) fixtures are seeded.

- **Seed fixtures.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture codex-basic` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture agy-basic`.
- **Sync Codex to Antigravity.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug sync-c2a -- sync-session --workspace @WORKSPACE --codex-thread-id 01960000-0000-7000-8000-000000000011 --cascade-id 00000000-0000-4000-8000-000000000011 --direction codex_to_agy --mode offline_storage`. Receipt reports `"transition_kind": "initial_import"`, `"events_applied" > 0`, non-null `backup_manifest`, and matching `agy_boundary_digest == codex_boundary_digest` in `updated_cursor`.
- **Confirm Antigravity companion stores.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-c2a --cascade-id 00000000-0000-4000-8000-000000000011 --codex-thread-id 01960000-0000-7000-8000-000000000011`. Confirm `agy_conv_db.user_version >= 1`, `agy_summary_row.source != 17`, `agy_summary_row.nesting_depth == 0`, `agy_annotation_pbtxt.exists == true`, `agy_transcript.line_count >= 1`, and `agyhub_summaries_pb_size > 0`.
- **Confirm immediate idempotency.** Re-run the exact `sync-session` command with `--direction auto_incremental`. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug sync-c2a-noop -- sync-session --workspace @WORKSPACE --codex-thread-id 01960000-0000-7000-8000-000000000011 --cascade-id 00000000-0000-4000-8000-000000000011 --direction auto_incremental`. Receipt reports `"transition_kind": "no_op"` and `"events_applied": 0`.
- **Sync Antigravity to Codex and confirm sidebar invariants.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug sync-a2c -- sync-session --workspace @WORKSPACE --cascade-id 00000000-0000-4000-8000-000000000021 --codex-thread-id 01960000-0000-7000-8000-000000000021 --direction agy_to_codex --mode offline_storage` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-a2c --cascade-id 00000000-0000-4000-8000-000000000021 --codex-thread-id 01960000-0000-7000-8000-000000000021`. Confirm `"transition_kind": "initial_import"`, `codex_thread_row.archived == 0`, `codex_thread_row.preview != ""`, and `codex_rollout.line_count >= 1`.

## Gotchas

- Antigravity hides sessions with `source == 17` (`CLI`) or forbidden project buckets (`default-cli-project`, `outside-of-project`). Always assert `agy_summary_row` fields in `views/*.json`, not just `conversations/<id>.db` existence.
- Codex hides threads when `preview == ""` or `archived != 0` due to partial SQLite indexes on `state_5.sqlite`. Always check `codex_thread_row.preview` in `views/*.json`.
