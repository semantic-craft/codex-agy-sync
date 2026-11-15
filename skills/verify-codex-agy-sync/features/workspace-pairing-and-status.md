# Workspace pairing and status

Workspace pairing registers a shared project directory across Antigravity (`CreateProject` RPC or offline `projects/<uuid>.json`) and Codex (`codex app-server` `project/list` + `project/create` RPC or offline `state_5.sqlite` `projects` + `project_roots` and `.codex-global-state.json`), while `status` reports workspace sessions plus enriched `pairing`, `handoff_chains`, and `pending_handoffs` metadata without creating or mutating any database on disk.

## Sub-features

- `status-readonly-unpaired` runs `status` on an uninitialized sandbox without creating SQLite files or project JSON files, reporting `pairing`, `handoff_chains: []`, and `pending_handoffs: []`.
- `pair-create` creates matching Antigravity and Codex project bindings for a workspace path or `file://` URI using online-first RPC registration (`CreateProject` / `project/create`) with locked offline fallback and accurate `pairing_mode` attribution (`already_paired` / `online_rpc` / `hybrid` / `offline_locked`).
- `pair-idempotent` reuses the exact project UUIDs on repeated `pair` invocations without acquiring offline write locks or rewriting host files.
- `status-paired` reports discovered workspace sessions with step/turn counts along with `pairing`, `handoff_chains`, and `pending_handoffs` after pairing, seeding, or handoffs.

## How to get to it (user POV)

- Run `./bin/codex-agy-sync status --workspace <path-or-file-uri>` in a terminal.
- Run `./bin/codex-agy-sync pair --workspace <path-or-file-uri>` (or pass `--pair-if-offline` to refuse running daemon writes) in a terminal.

## Driving it with control_sync_verify.py

Preconditions:

- `RUN_DIR` is initialized and `control_sync_verify.py doctor --run-dir "$RUN_DIR"` reports `"status": "PASS"`.
- No project files or SQLite databases exist in `_sandbox/` yet.

- **Verify read-only status on empty state.** Query status before pairing. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug status-empty -- status --workspace @WORKSPACE` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-empty-status`. Exit code is `0`, `sessions`, `handoff_chains`, and `pending_handoffs` are `[]`, `pairing.paired` is `false`, and `after-empty-status.json` shows `codex_state_db_exists` equal to `false` and `agy_project_files_count` equal to `0`.
- **Pair workspace.** Register bidirectional project bindings. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug pair-1 -- pair --workspace @WORKSPACE`. Exit code is `0` and stdout JSON contains non-empty `agy_project_id`, `codex_project_id`, `codex_legacy_project_id`, and `pairing_mode` (`"offline_locked"` in an offline sandbox, or `"online_rpc"`/`"hybrid"` against live daemons tested in `tests/test_online_pairing.py`), with `agy_project_id` not in `{"", "default-cli-project", "outside-of-project", "null", "none"}`.
- **Confirm pairing idempotency.** Run `pair` a second time using a `file://` URI or `@WORKSPACE`. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug pair-2 -- pair --workspace @WORKSPACE` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-pair`. Both `pair` receipts return identical `agy_project_id` and `codex_project_id`, the second run reports `pairing_mode` equal to `"already_paired"`, and `after-pair.json` confirms `codex_global_state_has_legacy_bridge` equal to `true`.
- **Confirm session listing.** Seed a Codex thread and re-run `status`. Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture codex-basic` and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug status-seeded -- status --workspace @WORKSPACE`. Exit code is `0`, `sessions` lists `codex_thread_id` equal to `"01960000-0000-7000-8000-000000000011"` with `codex_turn_count >= 1`, and `pairing.paired` is `true`.

## Gotchas

- Running `pair` before testing `status-readonly-unpaired` creates `state_5.sqlite` and hides regressions where `status` mutates uninitialized directories. Always run `status` first on the clean sandbox.
- macOS symlinks (`/var` vs `/private/var`) change `workspace_uri` if paths are not resolved. Always compare the resolved `workspace_path` reported in the receipt.
