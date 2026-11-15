# Live Stage-1 workbench

## Behavior contract

- `codex-agy-sync serve [-w <workspace>] [--ui <path>] [--port 8765] [--idle-timeout 1800] [--max-lifetime 14400] [--open]` starts a loopback-only `127.0.0.1` HTTP server hosting the live workbench UI (`stage == "stage_1"`, `remote_message_send == false`; `--port` defaults to `8765` while `0` picks an ephemeral port, omitting `--ui` serves packaged `src/codex_agy_sync/workbench/ui/index.html` out of the box, whereas passing `--ui <path>` serves only that path without falling back to the packaged default: CLI `serve --ui <missing>` exits `2` with `file_not_found` before binding or opening a browser, and `GET /` returns `404 ui_not_found` if an explicitly configured `WorkbenchServer.ui_path` is missing) and writes a `0o600` lease file under `<sync-state-dir>/workbench/server.json` containing zero tokens.
- `codex-agy-sync serve --status` queries running lease/health JSON (`running`, `pid`, `port`, `url`, `health`) without modifying host stores; `codex-agy-sync serve --stop` terminates the server cleanly (`SIGTERM`), waits for exit, and removes `server.json` (preserving `server.json` and reporting `stopped == false` if the process remains alive past timeout).
- `GET /api/health` requires loopback `Host` (`127.0.0.1`, `localhost`, or `[::1]`); all `/api/*` endpoints reject missing/invalid `X-Workbench-Token` (`401`), reject non-loopback `Host`/`Origin`/`Sec-Fetch-Site` (`403`, allowing `http://[::1]:<port>`), and keep `POST /api/send` disabled (`404`).
- Discovery (`workbench/catalog.py`) preloads `state_5.sqlite` and `conversation_summaries.db` once per snapshot and filters out `Archive` (case-insensitively on macOS, e.g. `archive`), `.private_keys`, symlink escapes into them, and `outside-of-project`. Tiered indexing (`workbench/indexer.py`, `eager_turn_budget=24`) eagerly decodes multi-session clusters, receipt-backed works, seed workspaces, and the top `24` standalone works while hydrating remaining cards on demand via `GET /api/works/<work_id>`, `GET /api/snapshot?workspace_id=...`, and `GET /api/search`.
- `POST /api/draft/render` produces `stage_1_copy_only` Markdown (`delivery_executed: false`) with IME-safe `Enter` handling in `#draftNote`. `POST /api/handoff` supports `auto_pair` (`#handoffPairIfOffline` and `#retryPairHandoff`, registering live hosts via `CreateProject` / `project/create` RPC with `pairing_mode == "online_rpc"` or `hybrid`), `pair_if_offline` (strict offline check returning `409 sync_busy_lock` while target daemon locks are held), and `include_interrupted` (`#handoffIncludeInterrupted`); on `409 handoff_pending`, `#reconcilePendingHandoff` posts to `POST /api/handoff/reconcile` (`reconcile_workspace_handoffs`) to promote verified targets or clear absent receipts safely.

## Verification procedure

1. Run `python3 skills/verify-codex-agy-sync/scripts/workbench_verify.py doctor` (optionally `--prototype "<Main.html>"` to test an external HTML override) and confirm `status == "PASS"`.
2. Run `python3 skills/verify-codex-agy-sync/scripts/workbench_verify.py suite` (optionally `--prototype "<Main.html>"`):
   - `01-server-launch-status-and-security`: spawn `serve` inside an isolated sandbox with an ephemeral loopback AGY Connect-RPC stub (`GetCascadeTrajectory` and `CreateProject`, `target_daemon_mode: "sandbox_loopback_rpc_stub"`), verify startup JSON, `GET /api/health`, `401` unauthenticated `/api/snapshot`, `404` `POST /api/send`, `serve --status`, and token-free `0o600` `server.json`.
   - `02-live-hydration-and-exclusions`: open the live URL in headless `agent-browser` (`1440x900`), verify `Stage 1 本地工作台` hydration for paired (`repo-paired`) and unpaired (`repo-unpaired`) workspaces, unpaired banner diagnosis, on-demand work hydration on click, and zero leakage of seeded `Archive`, `.private_keys`, or `outside-of-project` entries.
   - `03-cross-host-search-and-locate`: search across both hosts in `#mainSearch` (triggering full-scope search hydration), toggle `Codex`/`AGY`/`全部` filter pills, locate a matching turn with `#sourceBanner` + highlight, and repeat search/navigation through the Cmd+K palette (`#panelSearch`).
   - `04-stage1-citation-draft-and-ime`: cite a turn, verify IME `isComposing` `Enter` inside `#draftNote` does not prematurely submit, submit draft, verify `#draftMarkdownPreview`, `delivery_executed: false`, and `#copyDraftMarkdown`.
   - `05-one-click-handoff-and-refresh`: verify `repo-unpaired` handoff returns `422 unpaired_workspace` + `#retryPairHandoff`, assert direct `POST /api/handoff` with `pair_if_offline: true` returns `409 sync_busy_lock`, click `#retryPairHandoff` (sends `auto_pair: true`) to verify `200` `HandoffReceipt` (`pairing_mode == "online_rpc"` via `CreateProject` RPC stub and `.db` creation), then run one-click handoff on `repo-paired` and confirm real `target_id`, `chain_depth >= 1`, `.db` + `handoffs/*.json` creation, and UI chip/chain refresh.
   - `06-responsive-themes-and-errors`: assert `scrollWidth <= innerWidth` at `1440x900` (light) and `390x844` (light & dark), `agent-browser errors == []`, and loopback-only network requests.
   - `07-server-stop-and-cleanup`: run `serve --stop`, assert exit code `0` and removal of `server.json`, tear down `_sandbox/` and RPC stub, and confirm `host_fingerprints_unchanged == true`.

## Real-host evidence

- Isolated sandbox verification uses `DefaultHostTransport`, `SyncBridge`, `WorkbenchServer`, real `codex-agy-sync serve` CLI, and an ephemeral loopback AGY Connect-RPC stub writing real staged/published `.db` and `handoffs/*.json` inside `_sandbox/` while leaving live Desktop apps (`native_desktop: "NOT_RUN"`) and live host databases untouched (`host_fingerprints_unchanged: true`).
- Evidence is written to `$HOME/Projects/_artifacts/codex-agy-sync/verify-workbench-<run-id>/evidence/` (`env-receipt.json`, `git-*.txt`, `commands/*.json`, `views/*.json`, `screenshots/live-*.png`, `cleanup-receipt.json`, `suite-summary.json`).

## Pass criteria

- `suite-summary.json` has `status == "PASS"` and `host_fingerprints_unchanged == true` across all 7 scenarios.
- `server.json` uses mode `0o600`, stores no tokens, and is removed after `serve --stop`.
- No `Archive`, `.private_keys`, or `outside-of-project` row appears in API responses or rendered DOM, and all browser requests target loopback `127.0.0.1:<port>` with zero page errors.
- Automated checks leave `visual_semantics = "REVIEW_REQUIRED"` and `native_desktop = "NOT_RUN"` until screenshots are visually reviewed and real Desktop targets are tested separately.
