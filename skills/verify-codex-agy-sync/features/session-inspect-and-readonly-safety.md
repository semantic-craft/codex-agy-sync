# Session inspect and read-only safety

Session inspect decodes an Antigravity cascade or Codex thread into a canonical `TrajectoryLedger` summary JSON (title, event counts, completed turns, in-flight flag, and latest SHA-256 boundary watermark) without mutating any SQLite database or rollout file.

## Sub-features

- `inspect-codex` decodes a Codex thread by `--codex-thread-id` and reports user, thinking, tool call, tool result, and assistant counts.
- `inspect-agy` decodes an Antigravity cascade by `--cascade-id` and reports canonical title and watermark digest.
- `inspect-readonly` preserves exact SQLite file sizes and mtimes across `inspect` calls.

## How to get to it (user POV)

- Run `./bin/codex-agy-sync inspect --workspace <path> --codex-thread-id <uuid>` in a terminal.
- Run `./bin/codex-agy-sync inspect --workspace <path> --cascade-id <uuid>` in a terminal.

## Driving it with control_sync_verify.py

Preconditions:

- `RUN_DIR` is initialized and `doctor` reports `"status": "PASS"`.
- Both `codex-basic` and `agy-basic` fixtures are seeded in `_sandbox/`.

- **Seed both hosts and snapshot stores.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture codex-basic`, `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture agy-basic`, and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label before-inspect --cascade-id 00000000-0000-4000-8000-000000000021 --codex-thread-id 01960000-0000-7000-8000-000000000011`.
- **Inspect Codex thread.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug inspect-codex -- inspect --workspace @WORKSPACE --codex-thread-id 01960000-0000-7000-8000-000000000011`. Exit code is `0`, `completed_turns` is `2`, `has_inflight_turn` is `false`, `counts.tool_calls >= 1`, and `latest_watermark.boundary_digest` is a 64-hex SHA-256 string.
- **Inspect Antigravity cascade.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug inspect-agy -- inspect --workspace @WORKSPACE --cascade-id 00000000-0000-4000-8000-000000000021`. Exit code is `0`, `completed_turns` is `2`, `canonical_title` is non-empty, and `latest_watermark.last_agy_step_index` is an integer `>= 1`.
- **Verify zero store mutation.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-inspect --cascade-id 00000000-0000-4000-8000-000000000021 --codex-thread-id 01960000-0000-7000-8000-000000000011`. `cursors` remains empty and SQLite `(size, mtime_ns)` in `after-inspect.json` equals `before-inspect.json`.

## Gotchas

- Passing neither `--cascade-id` nor `--codex-thread-id` fails validation. Test both flags explicitly.
- Do not confuse `total_events` with Antigravity `steps` row count: one `PLANNER_RESPONSE` step unpacks into thinking, assistant text, and tool call events in the ledger.
