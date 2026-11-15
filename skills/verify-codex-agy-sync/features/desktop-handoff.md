# Desktop session handoff

Desktop handoff exports completed history from one host into a brand-new Desktop session on the opposite host without mutating running SQLite stores, supporting automatic direction inference, `--latest` target selection, `--chain` multi-hop tracking, `--pick` interactive selection, `--auto-pair` (online-first RPC pairing) / `--pair-if-offline`, `--include-interrupted` stopped-turn rescue, `agy_cli_data_dir` transcript mirroring, `--reconcile` pending recovery, and structured JSON error reporting on exit code `2`.

## Sub-features

- `handoff-unpaired-diagnosis` rejects unpaired or half-paired workspaces before creating `handoffs/`, emitting structured JSON (`"error": "unpaired_workspace"`, `"message"`, `"diagnosis"`, `"remediation"`) to `stderr` at exit code `2`.
- `handoff-auto-pair-and-offline-pair` supports `--auto-pair` (registers running hosts via `LanguageServerService/CreateProject` and `codex app-server` `project/list` + `project/create`, falling back to locked offline pairing on stopped hosts and recording `pairing_mode` in `already_paired`/`online_rpc`/`hybrid`/`offline_locked`) and `--pair-if-offline` (strict offline-only fallback that emits `{"error": "sync_busy_lock", "message": ..., "host": ..., "remediation": [...]}` if a daemon/WAL lock blocks offline pairing).
- `handoff-auto-direction` infers `codex_to_agy` or `agy_to_codex` when `--source-id` is passed without `--direction`, failing clearly on ambiguous IDs present on both hosts or missing IDs.
- `handoff-latest-and-chain` picks the newest non-subagent completed session that is not an untouched baton target (`--latest`) or advances/re-handoffs along the active workspace chain (`--chain`, returning `no_op` when neither target nor source advanced).
- `handoff-pick-tty-guard` renders numbered choices solely on `stderr` in interactive TTY sessions so `stdout` remains pure JSON, and fails fast in non-TTY runs with flag guidance (`--latest`, `--chain`, `--source-id`, `--pick`).
- `handoff-include-interrupted-and-cli-mirror` lets `--include-interrupted` rescue a stopped trailing interrupted turn (still rejecting active streaming turns), while AGY imports also mirror `brain/<id>/.system_generated/logs/transcript.jsonl` into `--agy-cli-data-dir` (`$ANTIGRAVITY_CLI_DATA_DIR` or sibling `~/.gemini/antigravity-cli` when present).
- `handoff-reconcile` (`--reconcile [--source-id <uuid>] [--dry-run]`, or `POST /api/handoff/reconcile` / UI `#reconcilePendingHandoff`) verifies pending workspace receipts against target host RPC and read-only storage, reporting `promoted` (`status: "ready"` + staging cleanup), `cleared` (absent target receipt + validated staging cleanup), and `unresolved` (partial/locked/unattributed entries preserved intact).

## How to get to it (user POV)

- Run `./bin/codex-agy-sync handoff --workspace <path-or-file-uri> --chain [--auto-pair|--pair-if-offline]` in a terminal.
- Run `./bin/codex-agy-sync handoff --workspace <path-or-file-uri> --latest [--auto-pair] [--include-interrupted]` in a terminal.
- Run `./bin/codex-agy-sync handoff --workspace <path-or-file-uri> --source-id <uuid> [--direction codex_to_agy|agy_to_codex] [--title <title>]` in a terminal.
- Run `./bin/codex-agy-sync handoff --workspace <path-or-file-uri> --pick` in an interactive TTY terminal.
- Run `./bin/codex-agy-sync handoff --workspace <path-or-file-uri> --reconcile [--source-id <uuid>] [--dry-run]` to inspect or recover pending handoff receipts.

## Driving it with control_sync_verify.py

Preconditions:

- `RUN_DIR` is initialized and `control_sync_verify.py doctor --run-dir "$RUN_DIR"` reports `"status": "PASS"`.
- No live target Desktop daemon is mocked inside offline sandbox runs; end-to-end target activation (`ready` receipt emission) and live `--reconcile` RPC checks are covered by `tests/test_handoff_cli_ux.py`, `tests/test_online_pairing.py`, `tests/test_handoff_convergence.py`, and `tests/test_desktop_handoff.py`.

- **Confirm unpaired diagnosis without side effects.** On an unpaired sandbox, run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug handoff-unpaired -- handoff --workspace @WORKSPACE --latest` (allowing exit code `2`) and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-unpaired-handoff`. Exit code is `2`, `stderr` parses as JSON with `"error": "unpaired_workspace"`, `"message"`, `"diagnosis"`, and `"remediation"` mentioning `pair`, `stdout` is empty, and `sync_state_dir/handoffs` does not exist.
- **Confirm mutually exclusive flag rejection.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug handoff-mutex-chain-latest -- handoff --workspace @WORKSPACE --chain --latest`, `--slug handoff-mutex-src-pick -- handoff --workspace @WORKSPACE --source-id 01960000-0000-7000-8000-000000000011 --pick`, `--slug handoff-mutex-pair -- handoff --workspace @WORKSPACE --latest --auto-pair --pair-if-offline`, and `--slug handoff-mutex-reconcile -- handoff --workspace @WORKSPACE --reconcile --latest`. All exit `2` cleanly before touching any host store.
- **Confirm non-TTY interactive picker fast-fail.** Pair and seed `@WORKSPACE` (`pair`, `seed --fixture codex-basic`), then run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug handoff-pick-nontty -- handoff --workspace @WORKSPACE --pick` (or omit `--source-id`/`--latest`/`--chain`). Exit code is `2`, `stdout` is empty, and `stderr` parses as JSON (`"error": "invalid_handoff_request"`, `"message"`) instructing callers to pass `--latest`, `--chain`, `--source-id`, or run with `--pick` in a TTY.
- **Confirm `--pair-if-offline` / `--auto-pair` on half-paired offline sandbox.** When only Codex is registered and a Codex thread is seeded, running `handoff --workspace @WORKSPACE --source-id <tid> --pair-if-offline` (or `--auto-pair`) registers Antigravity (`projects/<uuid>.json`) offline without rewriting Codex `state_5.sqlite`; with no live AGY target daemon in the offline sandbox, pairing and source-direction inference complete cleanly before the target daemon check (`"error": "online_rpc_unavailable"`, `"message": "Open the target Desktop app before handoff."`).
- **Confirm `--reconcile` on staged/absent pending receipts.** When a pending receipt exists under `sync_state_dir/handoffs/` without a live or disk target, `handoff --workspace @WORKSPACE --reconcile --dry-run` reports `"cleared"` without deleting files, and `--reconcile` clears the stale pending receipt + staging directory cleanly (exit code `0`).

## Gotchas

- `handoff` never mutates SQLite offline on the target host without an active target Desktop RPC endpoint; sandbox CLI checks without a live target daemon intentionally stop at `online_rpc_unavailable` (`Open the target Desktop app before handoff.`), while `tests/test_handoff_cli_ux.py`, `tests/test_online_pairing.py`, and `tests/test_handoff_convergence.py` cover full RPC + disk roundtrips and reconciliation.
- Passing `--source-id` together with `--chain` or `--reconcile` is valid (walks from that seed session or filters pending receipts), whereas `--reconcile` cannot combine with `--latest`, `--chain`, `--pick`, `--pair-if-offline`, or `--auto-pair`, `--dry-run` requires `--reconcile`, and `--auto-pair` is mutually exclusive with `--pair-if-offline`.
- Exit-code-2 CLI errors always write JSON with `"error"` and `"message"` (plus context keys `"diagnosis"`, `"remediation"`, `"host"`, `"receipt"`, `"cause"`) to `stderr` rather than raw tracebacks.
