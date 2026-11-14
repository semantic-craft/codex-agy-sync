# Desktop session handoff

Desktop handoff exports completed history from one host into a brand-new Desktop session on the opposite host without mutating running SQLite stores, supporting automatic direction inference, `--latest` target selection, `--chain` multi-hop tracking, `--pick` interactive selection, `--pair-if-offline`, and structured JSON error reporting on exit code `2`.

## Sub-features

- `handoff-unpaired-diagnosis` rejects unpaired or half-paired workspaces before creating `handoffs/`, emitting structured `unpaired_workspace` JSON with host-specific remediation steps to `stderr` at exit code `2`.
- `handoff-pair-if-offline` idempotently registers only the missing offline host binding (without touching an already-registered or locked opposite host) before proceeding, or fails with structured `sync_busy_lock` JSON if a daemon/WAL lock blocks offline pairing.
- `handoff-auto-direction` infers `codex_to_agy` or `agy_to_codex` when `--source-id` is passed without `--direction`, failing clearly on ambiguous IDs present on both hosts or missing IDs.
- `handoff-latest-and-chain` picks the newest non-subagent completed session that is not an untouched baton target (`--latest`) or advances/re-handoffs along the active workspace chain (`--chain`, returning `no_op` when neither target nor source advanced).
- `handoff-pick-tty-guard` renders numbered choices solely on `stderr` in interactive TTY sessions so `stdout` remains pure JSON, and fails fast in non-TTY runs with flag guidance (`--latest`, `--chain`, `--source-id`, `--pick`).

## How to get to it (user POV)

- Run `./bin/codex-agy-sync handoff --workspace <path-or-file-uri> --chain [--pair-if-offline]` in a terminal.
- Run `./bin/codex-agy-sync handoff --workspace <path-or-file-uri> --latest` in a terminal.
- Run `./bin/codex-agy-sync handoff --workspace <path-or-file-uri> --source-id <uuid> [--direction codex_to_agy|agy_to_codex] [--title <title>]` in a terminal.
- Run `./bin/codex-agy-sync handoff --workspace <path-or-file-uri> --pick` in an interactive TTY terminal.

## Driving it with control_sync_verify.py

Preconditions:

- `RUN_DIR` is initialized and `control_sync_verify.py doctor --run-dir "$RUN_DIR"` reports `"status": "PASS"`.
- No live target Desktop daemon is mocked inside offline sandbox runs; end-to-end target activation (`ready` receipt emission) requires an active target Desktop RPC endpoint and is verified by `tests/test_handoff_cli_ux.py` and `tests/test_desktop_handoff.py`.

- **Confirm unpaired diagnosis without side effects.** On an unpaired sandbox, run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug handoff-unpaired -- handoff --workspace @WORKSPACE --latest` (allowing exit code `2`) and `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-unpaired-handoff`. Exit code is `2`, `stderr` parses as JSON with `"error": "unpaired_workspace"` and concrete remediation steps mentioning `pair`, `stdout` is empty, and `sync_state_dir/handoffs` does not exist.
- **Confirm mutually exclusive flag rejection.** Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug handoff-mutex-chain-latest -- handoff --workspace @WORKSPACE --chain --latest` and `--slug handoff-mutex-src-pick -- handoff --workspace @WORKSPACE --source-id 01960000-0000-7000-8000-000000000011 --pick`. Both exit `2` cleanly before touching any host store.
- **Confirm non-TTY interactive picker fast-fail.** Pair and seed `@WORKSPACE` (`pair`, `seed --fixture codex-basic`), then run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug handoff-pick-nontty -- handoff --workspace @WORKSPACE --pick` (or omit `--source-id`/`--latest`/`--chain`). Exit code is `2`, `stdout` is empty, and `stderr` parses as JSON (`"error": "invalid_handoff_request"`) instructing callers to pass `--latest`, `--chain`, `--source-id`, or run with `--pick` in a TTY.
- **Confirm `--pair-if-offline` on half-paired offline sandbox.** When only Codex is registered and a Codex thread is seeded, running `handoff --workspace @WORKSPACE --source-id <tid> --pair-if-offline` registers Antigravity (`projects/<uuid>.json`) offline without rewriting Codex `state_5.sqlite`; with no live AGY target daemon in the offline sandbox, pairing and source-direction inference complete cleanly before the target daemon check (`Open the target Desktop app before handoff.`).

## Gotchas

- `handoff` never mutates SQLite offline on the target host without an active target Desktop RPC endpoint; sandbox CLI checks without a live target daemon intentionally stop at `Open the target Desktop app before handoff.` while unit tests in `tests/test_handoff_cli_ux.py` cover full multi-hop RPC + disk roundtrips.
- Passing `--source-id` together with `--chain` is valid (starts or walks from that seed session), whereas `--chain` combined with `--latest` or `--pick` is rejected with exit code `2`.
