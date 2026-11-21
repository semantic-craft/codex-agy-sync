# Implementation decisions (`codex-agy-sync`)

This document records non-obvious operating system, protocol, and concurrency constraints that shape specific code paths across `scripts/`, `macos/`, `src/codex_agy_sync/`, `rust/`, and `tests/`. High-level system structure lives in `docs/architecture.md`.

## Reset macOS TCC grants before LaunchServices unregistration

`tccutil reset All <bundle_id>` resolves the target bundle identifier through the LaunchServices database. If a script unregisters the `.app` bundle with `lsregister -u` or deletes the bundle directory first, `tccutil` fails with `OSStatus -10814` (`kLSApplicationNotFoundErr`) and leaves orphaned entries in System Settings.

Both `scripts/build_menubar_app.sh` (during `--install` retirement of superseded `.app` bundles) and `skills/verify-codex-agy-sync/scripts/menubar_verify.py` (inside `cleanup_qa_env` for `QA_BUNDLE_ID`) follow a strict three-step teardown sequence:

1. Run `tccutil reset All <bundle_id>` while the `.app` bundle still exists on disk and remains registered.
2. Run `lsregister -u <app_bundle>` to remove the LaunchServices registration.
3. Delete the `.app` bundle directory last.

## Re-unlink retired preference plists after the 7-second `cfprefsd` flush window

macOS `cfprefsd` holds an asynchronous `7.000s` dirty-domain flush timer on reverse-DNS preference domains in `~/Library/Preferences/`. When `UpdatePathGuard.cleanupLegacyUpdateResidue` in `macos/Sources/CodexAgySyncCore/UpdatePathGuard.swift` clears keys with `CFPreferencesSetMultiple`, calls `CFPreferencesSynchronize`, and unlinks `<domain>.plist`, `cfprefsd` can write back a 42-byte empty `bplist00` file 7 seconds later.

To prevent orphan `.plist` files from reappearing, `UpdatePathGuard.cleanupLegacyUpdateResidue` splits cleanup by key state:

- When the retired domain still has keys in `CFPreferencesCopyKeyList`, `cleanupLegacyUpdateResidue` clears those keys, synchronizes, removes the `.plist` immediately, and spawns a detached utility task that sleeps 8 seconds before calling `FileManager.default.removeItem(at:)` a second time.
- When an existing `.plist` file already has zero keys, `cleanupLegacyUpdateResidue` unlinks the file directly without calling `UserDefaults` or `CFPreferences`, avoiding any new dirty-domain timer in `cfprefsd`.

## Close inherited file descriptors and bound `AF_UNIX` paths in test harnesses

On BSD and macOS, `flock(2)` locks belong to the kernel open file description rather than a single PID. If a parent verification script holds an exclusive lock and spawns a child CLI process without closing file descriptors, the child inherits the open descriptor and observes no lock conflict. `skills/verify-codex-agy-sync/scripts/control_sync_verify.py` passes `close_fds=True` and `pass_fds=()` to `subprocess.run` so `hold-lock` contention checks isolate the child process from parent descriptors.

macOS also caps `AF_UNIX` socket paths (`sockaddr_un.sun_path`) at 104 bytes (`SUN_LEN`). Both runtime discovery (`unix_socket_connect_path` in Python and Rust) and mock `codex app-server` WebSocket servers (`rust/crates/cas-host/tests/host_parity.rs` and `scripts/verify_rust_parity.py`) place or resolve socket paths under short directory prefixes to stay below the 104-byte kernel limit.

## Treat empty `CODEX_AGY_SYNC_SERVE_DELEGATED` as unset and walk 8 ancestor levels for `serve`

The Rust binary (`rust/crates/cas-cli/src/main.rs`) delegates the frozen `serve` subcommand to the Python CLI via `delegate_serve_to_python` and exports `CODEX_AGY_SYNC_SERVE_DELEGATED=1` to stop infinite self-recursion when `PATH` or `CODEX_AGY_SYNC_PYTHON_BIN` points back to a Rust binary. Callers and test harnesses sometimes clear environment variables by setting `CODEX_AGY_SYNC_SERVE_DELEGATED=""`. `delegate_serve_to_python` filters out empty values (`.filter(|v| !v.is_empty())`) so an empty string proceeds normally while any non-empty value exits `2` with `serve_python_unavailable` (`rust/crates/cas-cli/tests/cli_smoke.rs`).

When `CodexAgySync.app` launches from `<repo>/out/<dir>/CodexAgySync.app/Contents/Helpers/codex-agy-sync` with a minimal GUI `PATH`, `<repo>/bin/codex-agy-sync` sits 6 directory levels above `Helpers/`. `resolve_python_serve_target` inspects `exe.ancestors().skip(1).take(8)` so bundled local builds still find the repository Python entrypoint without relying on `PATH` or `HOME`.

## Guard `publish_new_file` when `agy_cli_data_dir` aliases `agy_data_dir`

`publish_new_file` in `src/codex_agy_sync/bridge/handoff.py` (and its Rust counterpart in `rust/crates/cas-host/src/sync_engine.rs`) publishes staged files with `os.link(temporary, target)`, which raises `FileExistsError` if `target` already exists. During `codex_to_agy` sync, `_import_agy` publishes `brain/<target_id>/.system_generated/logs/transcript.jsonl` into `agy_data_dir` and then mirrors that file into `agy_cli_data_dir` so standalone `agy --conversation=<id>` can read the transcript.

If `agy_cli_data_dir` points to the same directory as `agy_data_dir` (directly or through symlinks), mirroring unconditionally would call `os.link` against the already-published desktop transcript path and fail with `FileExistsError`. `_import_agy` compares `resolve(strict=False)` paths and verifies `not cli_tx.exists()` before publishing the CLI mirror (`tests/test_handoff_convergence.py`).

## Strip workspace-dependent digests only on twin-sandbox receipts

Canonical `RunCommandPayload` and file tool events hash the resolved workspace path into `source_digest` and `target_seed_text_digest`. In `scripts/verify_rust_parity.py`, side-by-side `sync` runs execute against separate `py/` and `rs/` sandbox workspace directories, so their path strings legitimately produce distinct digests. `_strip_dynamic_receipt` removes `source_digest` and `target_seed_text_digest` when diffing twin-sandbox receipts, while the `inspect` parity check runs both Python and Rust against the exact same workspace directory to assert byte-for-byte SHA-256 `boundary_digest` equality.

## Overlay live `language_server` descriptors on host-upgraded protobuf blobs

`scripts/check` pins committed descriptors in `schema/antigravity_2_30_schema.json` against the 16-tool `TOOL_CODEC_TABLE` contract, while step `[2/6]` (`scripts/verify_session_mapping.py`) reads live `~/.gemini/antigravity/` databases written by the currently installed `/Applications/Antigravity.app`. When Antigravity upgrades on disk and adds new fields to nested metadata messages (such as `exa.cortex_pb.ToolSourceMetadata.title` on field `4`), decoding live blobs solely against the committed 2.30 snapshot flags those new fields as unknown wire tags. When `decode_antigravity_session` encounters unknown fields against the committed snapshot, `_overlay_installed_binary_schema` extracts `FileDescriptorProto` definitions from the installed `language_server` binary in memory and re-verifies the session without mutating `schema/`, while `validate_negative_controls` still verifies that synthetic unknown tag `9999` is caught against the pinned schema.

