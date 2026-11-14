---
name: verify-codex-agy-sync
description: "Drive and verify the codex-agy-sync CLI and its multi-store SQLite, Protobuf, and Rollout projections in an isolated sandbox. Use when changing workspace pairing, session inspection, bidirectional sync, incremental cursors, interrupted turn rescue, divergence handling, subagent filtering, or WAL lock guards."
---

# Verify codex-agy-sync

`codex-agy-sync` is a Python CLI (`./bin/codex-agy-sync`) that pairs workspace directories and synchronizes agent sessions between Google Antigravity 2.30+ and OpenAI Codex 0.166+. Every verification run drives the real `./bin/codex-agy-sync` executable against isolated sandbox directories outside `/tmp` and verifies resulting state from an independent read-only view.

Run the repository gate `./scripts/check` alongside this skill whenever source or schema files change. Read [`features/README.md`](./features/README.md) for the maintained feature map.

## UI prototype route

For the external workspace HTML prototype, follow [UI prototype verification](./features/ui-prototype.md). Supply the artifact explicitly. It stays outside the repository.

```bash
python3 skills/verify-codex-agy-sync/scripts/ui_prototype_verify.py doctor --prototype "$PROTOTYPE"
python3 skills/verify-codex-agy-sync/scripts/ui_prototype_verify.py suite --prototype "$PROTOTYPE"
```

This independent browser suite records simulated UI behavior. It runs independently of the CLI suite below. `./scripts/check` does not run this prototype end-to-end suite. After the run, the verification Agent reads the screenshots and records a separate visual review. The script leaves visual semantics as `REVIEW_REQUIRED`, and actual Desktop behavior and message delivery as `NOT_RUN`.

## Quick suite

For an automated end-to-end smoke verification covering `init`, `doctor`, read-only `status`, `pair`, initial `codex_to_agy` import with companion store readback and `transport_used` attribution, `incremental_append`, `B2` `--include-interrupted` stopped-turn rescue and `no_op` convergence, `hold-lock` deferral, and `cleanup`, run the `suite` subcommand.

```bash
python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py suite
```

`suite` allocates a fresh directory under `$HOME/Projects/_artifacts/codex-agy-sync/verify-<run-id>` when `--run-dir` is omitted, tears down `_sandbox/`, writes `$RUN_DIR/evidence/suite-summary.json`, and exits `0` only when `"overall_verdict"` is `"PASS"` and `"host_fingerprints_unchanged"` is `true`.

## Launch

`codex-agy-sync` is an on-demand CLI (`C1`) with no persistent background daemon. Launching a manual verification run means initializing a fresh run directory under `$HOME/Projects/_artifacts/codex-agy-sync/verify-<run-id>` (never under `/tmp` or `/private/tmp`), creating an isolated `_sandbox/` with disposable Antigravity and Codex data roots, and recording baseline git and host database fingerprints in `evidence/env-receipt.json`.

```bash
export RUN_DIR="$HOME/Projects/_artifacts/codex-agy-sync/verify-$(date -u +%Y%m%dT%H%M%SZ)-$$"
python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py init --run-dir "$RUN_DIR"
```

Usable state is reached when `init` exits `0` and prints JSON containing `"status": "INITIALIZED"`, `"sandbox_dir"`, and `"evidence_dir"`. Two verification runs execute safely in parallel as long as each uses its own `RUN_DIR`. Never run mutating `pair`, `sync-session`, or `sync-workspace` commands against live `$HOME/.gemini/antigravity` or `$HOME/.codex` during automated verification.

## Doctor

Run `doctor` before driving any feature and whenever a command behaves unexpectedly.

```bash
python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py doctor --run-dir "$RUN_DIR"
```

`doctor` performs a strictly read-only check and exits `0` with `"status": "PASS"` only when all conditions hold:
- `RUN_DIR` resolves via `Path.resolve()` outside `/tmp` and `/private/tmp`.
- `_sandbox/owner.json` exists and carries `"marker": "codex-agy-sync-verify-sandbox"`.
- `./bin/codex-agy-sync --help` exits `0` and exposes all five subcommands (`pair`, `status`, `inspect`, `sync-session`, `sync-workspace`).
- Live host files (`$HOME/.gemini/antigravity/conversation_summaries.db` and `$HOME/.codex/state_5.sqlite`, when present) match the `(inode, size, mtime_ns)` fingerprint captured at `init`.

## Drive

Drive every user-facing CLI flow through `control_sync_verify.py`, which injects `--gemini-config-dir`, `--agy-data-dir`, `--codex-home-dir`, and `--sync-state-dir` pointing at `_sandbox/` and substitutes `@WORKSPACE` with `_sandbox/workspace/sample-repo`.

1. Seed a deterministic starting state for the scenario under test:
   ```bash
   python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture codex-basic
   ```
2. Invoke `./bin/codex-agy-sync` with real subcommands and flags:
   ```bash
   python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug pair-ws -- pair --workspace @WORKSPACE
   python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug sync-c2a -- sync-session --workspace @WORKSPACE --codex-thread-id 01960000-0000-7000-8000-000000000011 --cascade-id 00000000-0000-4000-8000-000000000011 --direction codex_to_agy --mode auto
   ```
3. Read back persisted SQLite (`?mode=ro`), JSONL, `.pbtxt`, `.pb`, cursor, and backup state from an independent second view:
   ```bash
   python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py inspect-stores --run-dir "$RUN_DIR" --label after-c2a --cascade-id 00000000-0000-4000-8000-000000000011 --codex-thread-id 01960000-0000-7000-8000-000000000011
   ```
4. To test stopped interrupted-turn rescue (`B2`), seed `agy-interrupted`, confirm default sync excludes the stopped turn, then pass `--include-interrupted`:
   ```bash
   python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py seed --run-dir "$RUN_DIR" --fixture agy-interrupted
   python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cli --run-dir "$RUN_DIR" --slug sync-int -- sync-session --workspace @WORKSPACE --cascade-id 00000000-0000-4000-8000-000000000051 --codex-thread-id 01960000-0000-7000-8000-000000000051 --direction agy_to_codex --include-interrupted
   ```
5. To test active presence-lock deferral without touching live daemons, hold an exclusive `flock` on `_sandbox/agy-data/presence/<cascade_id>.lock` across a CLI call:
   ```bash
   python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py hold-lock --run-dir "$RUN_DIR" --cascade-id 00000000-0000-4000-8000-000000000011 --slug locked-sync -- sync-session --workspace @WORKSPACE --codex-thread-id 01960000-0000-7000-8000-000000000011 --cascade-id 00000000-0000-4000-8000-000000000011 --direction codex_to_agy
   ```

## Evidence

All proof artifacts live in `$RUN_DIR/evidence/` outside the git checkout and survive cleanup:
- `evidence/env-receipt.json` and `evidence/git-dirty.diff` record git HEAD SHA, uncommitted diff, Python and OS versions, `./bin/codex-agy-sync` SHA-256, and pre-run host DB fingerprints.
- `evidence/commands/<NNN>-<slug>.json` records exact `argv`, `exit_code`, `stdout`, `stderr`, and timestamps for every CLI invocation.
- `evidence/views/<label>.json` records second-view read-only inspections (`file:<db>?mode=ro` with `PRAGMA query_only=ON`) across `conversations/<id>.db`, `conversation_summaries.db`, `annotations/<id>.pbtxt`, `brain/<id>/.system_generated/logs/transcript.jsonl`, `agyhub_summaries_proto.pb`, `state_5.sqlite`, `.codex-global-state.json`, `rollout-*.jsonl`, `cursors/*.json`, and `backups/*/manifest.json`.
- `evidence/cleanup-receipt.json` records sandbox removal and post-run host DB fingerprint parity.
- `evidence/suite-summary.json` records per-scenario verdicts when running `suite`.

Proof standards:
- Drive `./bin/codex-agy-sync` directly, never internal Python helpers alone.
- Capture both the CLI receipt (`commands/*.json`, checking `"transition_kind"` and `"transport_used"`) and a second-view disk/SQLite readback (`views/*.json`). A CLI exit code `0` without store readback is not proof.
- Verify read-only commands (`status`, `inspect`) by diffing `views/before.json` and `views/after.json` to confirm zero unintended file creation or SQLite mutation.
- Report each checked feature with `PASS`, `FAIL`, `BLOCKED`, `NOT_RUN`, or `N/A` and cite the exact artifact path under `$RUN_DIR/evidence/`.

## Cleanup

Run `cleanup` after every manual verification run, including failed runs:

```bash
python3 skills/verify-codex-agy-sync/scripts/control_sync_verify.py cleanup --run-dir "$RUN_DIR"
```

`cleanup` verifies `_sandbox/owner.json` ownership before deleting `$RUN_DIR/_sandbox/`, never kills processes by name, verifies that live host DB fingerprints remain unchanged since `init`, and writes `$RUN_DIR/evidence/cleanup-receipt.json`. Confirm `ls "$RUN_DIR/evidence"` still lists `env-receipt.json`, `commands/`, `views/`, and `cleanup-receipt.json` after teardown.

## Helpers

`skills/verify-codex-agy-sync/scripts/control_sync_verify.py` is an executable Python 3 script supporting eight subcommands:
- `init [--run-dir <dir>] [--run-id <id>]`
- `doctor --run-dir <dir>`
- `seed --run-dir <dir> --fixture <codex-basic|codex-append-turn|agy-basic|agy-interrupted|subagent-pair|diverged-pair>`
- `cli --run-dir <dir> --slug <slug> -- <codex-agy-sync args...>`
- `inspect-stores --run-dir <dir> --label <label> [--cascade-id <id>] [--codex-thread-id <id>]`
- `hold-lock --run-dir <dir> --cascade-id <id> --slug <slug> -- <codex-agy-sync args...>`
- `cleanup --run-dir <dir>`
- `suite [--run-dir <dir>] [--run-id <id>]`

`skills/verify-codex-agy-sync/scripts/ui_prototype_verify.py` is an executable Python 3 script supporting two subcommands:
- `doctor --prototype <path>`
- `suite --prototype <path> [--run-dir <dir>] [--timeout <seconds>]`
