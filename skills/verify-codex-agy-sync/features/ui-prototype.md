# Verify the workspace UI prototype

The external `Main.html` demonstrates search, a shared draft, simulated review messages, and history handoff. This suite drives the prototype in an isolated browser. It does not connect to Codex or Antigravity Desktop.

## Sub-features

- `01-page-readiness` opens the external artifact and verifies the initial Atlas and Orbit projects and Codex login session.
- `02-main-search-and-filters` searches token history, filters Codex and AGY, and checks an empty result.
- `03-history-location-and-references` opens the original message, checks its highlight, and adds and removes a reference.
- `04-shared-draft-and-keyboard` edits the same note and reference across main and panel, using Cmd+K and Escape.
- `05-panel-search-reference` searches in the panel and adds a result to its shared draft.
- `06-simulated-roundtrip-and-pending` checks disabled pending controls, sends to the simulated AGY session in the prototype timeline, reads its mock question and reply, and brings the reply back to Codex in the prototype.
- `07-complete-handoff-and-deduplication` previews all completed prototype history, creates a simulated inherited session chip with original authors preserved, and repeats the same source history without creating another session.
- `08-new-history-creates-new-handoff` adds simulated completed history and creates a distinct simulated inherited session chip while retaining the previous one.
- `09-reset-restores-initial-state` removes simulated results and restores the initial conversation and closed draft.
- `10-theme-and-responsive-layout` captures main and panel in light and dark themes at 390, 780, and 1440 pixels and checks document horizontal overflow.
- `11-browser-errors-and-network` checks page errors and records requests observed by the owned browser session.

## How to get to it (user POV)

Open the supplied prototype HTML. Use the main search and source filters to find a message. Use **加入引用**, **请另一端看看**, and **换端继续** from the conversation. Open **轻面板** or press Cmd+K to use panel search and the same draft. Press Escape to return to the main window. Use **重置演示** to restore the example data.

## Driving it with ui_prototype_verify.py

Preconditions:

- Supply the existing external prototype with `--prototype`. The prototype and generated evidence do not enter the repository.
- Use the installed `agent-browser` CLI and its browser. Read the `agent-browser` skill and run `agent-browser skills get core` before the first drive. The suite installs no dependencies and uses Python's standard library.
- Keep the prototype and verification script unchanged during a run. The suite compares their hashes at the end and invalidates a changed run.

Run the read-only preflight, then the full suite:

```bash
python3 skills/verify-codex-agy-sync/scripts/ui_prototype_verify.py doctor --prototype "$PROTOTYPE"
python3 skills/verify-codex-agy-sync/scripts/ui_prototype_verify.py suite --prototype "$PROTOTYPE"
```

`doctor` checks the artifact's required controls and the installed CLI version without launching a browser. The suite's first scenario opens the page and checks usable initial UI. A missing dependency or unusable artifact returns `BLOCKED` with a nonzero exit code.

The suite allocates a unique session and a new `$HOME/Projects/_artifacts/codex-agy-sync/verify-ui-<run-id>/` directory. To select another evidence directory, pass `--run-dir`. Existing directories, `/tmp`, and `/private/tmp` are refused. `--timeout` sets the total timeout in seconds, with a default of 900. Each browser command has a 35-second timeout and the browser has a two-minute idle timeout. Success, failure, timeout, and interrupt paths close only the suite's named session. Evidence survives cleanup.

Read `evidence/suite-summary.json` for the prototype SHA-256, repository HEAD and dirty diff, tool versions, scenario actions, expected results, actual observations, screenshots, network observations, cleanup, and verdict. `commands/*.json` preserves exact command arguments, stdout, stderr, and exit codes. `views/` preserves DOM readbacks and accessibility snapshots. `screenshots/` contains each scenario and the theme and viewport captures.

After the scripted suite, the verification Agent reads its screenshots and writes a separate `evidence/visual-review.json` with the screenshot paths, observed layout findings, and its verdict. The Agent completes that review itself. The script always leaves `visual_semantics=REVIEW_REQUIRED`; mechanical geometry checks do not establish visual meaning. Keep the scripted summary intact and distinguish the Agent's review from its assertions.

A script `PASS` covers only the listed prototype interactions. `native_desktop=NOT_RUN` and `real_message_delivery=NOT_RUN` remain explicit. Existing `./scripts/check` and the CLI sandbox suite remain separate verification gates. Use the established Desktop verification workflow for actual host behavior.

## Gotchas

- Clicks, text entry, selection, and shortcuts go through `agent-browser`. DOM evaluation only reads rendered controls and messages. Do not invoke the prototype's internal state or functions, or change DOM values to manufacture an outcome.
- The prototype's target session is `ag-review`, and its initial source is `cx-login`. The suite checks these controls from the supplied artifact instead of guessing a real host session.
- Pending controls are checked during the prototype's real simulated send interval. A failed assertion remains a failure with evidence. Do not turn the case into a skipped pass.
- Request records cover page requests captured by this isolated browser session. They do not prove that every process on the machine made no external request.
- A changed artifact needs a fresh full run. Retain failed run directories when correcting verification code. Report product defects without changing the prototype to satisfy the suite.
