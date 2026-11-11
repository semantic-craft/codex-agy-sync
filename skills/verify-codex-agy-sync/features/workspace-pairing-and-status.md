# Workspace pairing and status

Workspace pairing registers a shared project directory across Antigravity (`projects/<uuid>.json`) and Codex (`state_5.sqlite` `projects` + `project_roots` and `.codex-global-state.json`), while `status` lists discovered sessions without creating or mutating any database on disk.

## Sub-features

- `status-readonly-unpaired` runs `status` on an uninitialized sandbox without creating SQLite files or project JSON files.
- `pair-create` creates matching Antigravity and Codex project bindings for a workspace path or `file://` URI.
- `pair-idempotent` reuses the exact project UUIDs on repeated `pair` invocations.
- `status-paired` reports discovered workspace sessions with step/turn counts after pairing and seeding.

## How to get to it (user POV)

- Run `./bin/codex-agy-sync status --workspace <path-or-file-uri>` in a terminal.
- Run `./bin/codex-agy-sync pair --workspace <path-or-file-uri>` in a terminal.

## Driving it with control_sync_verify.py

Preconditions:

- `RUN_DIR` is initialized and `control_sync_verify.py doctor --run-dir "$RUN_DIR"` reports `"status": "PASS"`.
- No project files or SQLite databases exist in `_sandbox/` yet.

- **Verify read-only status on empty state.** Query status before pairing. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug status-empty -- status --workspace @WORKSPACE` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-empty-status`. Exit code is `0`, `sessions` is `[]`, and `after-empty-status.json` shows `codex_state_db_exists: false` and `agy_project_files_count: 0`.
- **Pair workspace.** Register bidirectional project bindings. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug pair-1 -- pair --workspace @WORKSPACE`. Exit code is `0` and stdout JSON contains non-empty `agy_project_id`, `codex_project_id`, and `codex_legacy_project_id`, with `agy_project_id` not in `{"", "default-cli-project", "outside-of-project", "null", "none"}`.
- **Confirm pairing idempotency.** Run `pair` a second time using a `file://` URI or `@WORKSPACE`. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug pair-2 -- pair --workspace @WORKSPACE` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-pair`. Both `pair` receipts return identical `agy_project_id` and `codex_project_id`, and `after-pair.json` confirms `codex_global_state_has_legacy_bridge: true`.
- **Confirm session listing.** Seed a Codex thread and re-run `status`. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture codex-basic` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug status-seeded -- status --workspace @WORKSPACE`. Exit code is `0` and `sessions` lists `codex_thread_id: "01960000-0000-7000-8000-000000000011"` with `codex_turn_count >= 1`.

## Gotchas

- Running `pair` before testing `status-readonly-unpaired` creates `state_5.sqlite` and hides regressions where `status` mutates uninitialized directories. Always run `status` first on the clean sandbox.
- macOS symlinks (`/var` vs `/private/var`) change `workspace_uri` if paths are not resolved. Always compare the resolved `workspace_path` reported in the receipt.
