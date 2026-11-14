# codex-agy-sync verification map

This directory is the maintained source for verifying the user-facing CLI and multi-store behavior of `codex-agy-sync`. Read this index before driving the CLI, then follow the matching feature file or run `control_sync_verify.py suite` for the full sandbox smoke gate.

## UI prototype route

[UI prototype verification](./ui-prototype.md) covers the separately supplied workspace `Main.html`. Run `ui_prototype_verify.py suite --prototype "$PROTOTYPE"` for its isolated browser interactions and evidence. The prototype is external and does not enter the repository. `./scripts/check` and the CLI suite do not establish prototype end-to-end behavior. The verification Agent reviews the generated screenshots separately and records its visual findings.

## Baseline preconditions

- Choose a unique `RUN_DIR="$HOME/Projects/_artifacts/codex-agy-sync/verify-<timestamp>-$$"` outside `/tmp` and `/private/tmp`.
- Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py init --run-dir "$RUN_DIR"`.
- Run `python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py doctor --run-dir "$RUN_DIR"` and require `"status": "PASS"`.
- Never point mutating commands (`pair`, `sync-session`, `sync-workspace`) at live `$HOME/.gemini` or `$HOME/.codex` directories.

## Driving conventions

- Start every feature recipe from a clean `init` + `doctor` state unless its preconditions state otherwise, or run `control_sync_verify.py suite` for a single-command end-to-end check.
- Run every `./bin/codex-agy-sync` subcommand through `control_sync_verify.py cli --run-dir "$RUN_DIR" --slug <slug> -- ...` so sandbox flags and command transcripts are captured automatically.
- Pass `@WORKSPACE` for `--workspace` to target the isolated `_sandbox/workspace/sample-repo` directory.
- Pair every mutating CLI action with `control_sync_verify.py inspect-stores` to confirm SQLite rows, companion files, cursors, and backup manifests from an independent read-only view.
- Run `control_sync_verify.py cleanup --run-dir "$RUN_DIR"` at the end of every manual run and confirm `$RUN_DIR/evidence/` survives teardown.

## Proof and skip reporting

- Capture both the CLI command output (`evidence/commands/<NNN>-<slug>.json`, checking `"transition_kind"` and `"transport_used"`) and a second-view store snapshot (`evidence/views/<label>.json`).
- Verify host isolation on every run via `evidence/cleanup-receipt.json` (`"host_fingerprints_unchanged": true`).
- Record the feature filename and sub-feature ID alongside each proof artifact.
- Report unrun or blocked paths as `NOT_RUN` or `BLOCKED` with the exact unmet precondition. Never report a skipped path as verified through another subcommand.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. CLI feature files use four H2 sections in this exact order:

1. `Sub-features` lists short IDs with one line per behavior.
2. `How to get to it (user POV)` lists every user CLI entry point.
3. `Driving it with control_sync_verify.py` starts with `Preconditions:` and uses labeled bullets pairing each user action with an exact command and observable result.
4. `Gotchas` lists traps that can invalidate a verification run.

Browser prototype feature files keep the same four-section layout, list prototype UI actions in section 2, and title section 3 `Driving it with ui_prototype_verify.py`.

## Features

- [Workspace pairing and status](./workspace-pairing-and-status.md) covers `pair` project registration across Antigravity and Codex stores and zero-mutation `status` session discovery.
- [Session inspect and read-only safety](./session-inspect-and-readonly-safety.md) covers `inspect` ledger decoding for Antigravity cascades and Codex threads without mutating SQLite or creating sidecar directories.
- [Bidirectional sync and companion stores](./bidirectional-sync-and-companion-stores.md) covers `sync-session` initial import in both directions, `transport_used` attribution (`A2`), sidebar visibility invariants, companion projections, and immediate re-run `no_op` idempotency.
- [Incremental append and divergence policy](./incremental-append-and-divergence-policy.md) covers completed-turn cursor advancement, `B2` `--include-interrupted` stopped-turn rescue (`agy-interrupted`), and `halt_on_divergence`, `fork_new_session`, and `backup_and_overwrite_target` conflict resolution.
- [Subagent isolation and lock deferral](./subagent-isolation-and-lock-deferral.md) covers `status` and `sync-workspace` subagent filtering (`--include-subagents`), active `presence/<id>.lock` deferral, and `online_rpc` write-guard deferral.
- [Desktop session handoff](./desktop-handoff.md) covers `handoff` unpaired diagnosis (`exit 2`), `--pair-if-offline`, `--source-id` direction auto-inference, `--latest`, `--chain`, and `--pick` TTY behavior.

- [Workspace UI prototype](./ui-prototype.md) covers search, shared draft, simulated review and handoff, reset, themes, and responsive views through an owned browser session.
