import Foundation
import XCTest
@testable import CodexAgyHandoffCore

private final class MockCLIRunner: CLIRunnerProtocol, @unchecked Sendable {
    private let lock = NSLock()
    private var feedResult: Result<MenubarFeedSnapshot, Error>
    private var handoffResult: Result<HandoffReceiptPayload, Error>
    private var reconcileResult: Result<ReconcileSummaryPayload, Error>

    private(set) var fetchLimits: [Int] = []
    private(set) var handoffCalls: [(workspacePath: String, sourceId: String)] = []
    private(set) var reconcileCalls: [String?] = []

    init(
        feedResult: Result<MenubarFeedSnapshot, Error>,
        handoffResult: Result<HandoffReceiptPayload, Error>,
        reconcileResult: Result<ReconcileSummaryPayload, Error>
    ) {
        self.feedResult = feedResult
        self.handoffResult = handoffResult
        self.reconcileResult = reconcileResult
    }

    func setFeedResult(_ result: Result<MenubarFeedSnapshot, Error>) {
        lock.withLock {
            feedResult = result
        }
    }

    func fetchFeed(limit: Int) async throws -> MenubarFeedSnapshot {
        let res = lock.withLock {
            fetchLimits.append(limit)
            return feedResult
        }
        return try res.get()
    }

    func executeHandoff(workspacePath: String, sourceId: String) async throws -> HandoffReceiptPayload {
        let res = lock.withLock {
            handoffCalls.append((workspacePath, sourceId))
            return handoffResult
        }
        return try res.get()
    }

    func reconcilePending(workspacePath: String?) async throws -> ReconcileSummaryPayload {
        let res = lock.withLock {
            reconcileCalls.append(workspacePath)
            return reconcileResult
        }
        return try res.get()
    }
}

private final class MockActivator: AppActivating, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var activatedHosts: [String] = []
    var returnValue = true

    func activateTarget(host: String) async -> Bool {
        let ok = lock.withLock {
            activatedHosts.append(host)
            return returnValue
        }
        return ok
    }
}

private func makeItem(
    host: String = "antigravity",
    targetHost: String = "codex",
    sessionId: String,
    title: String = "Investigate lock guard",
    workspacePath: String = "/repos/demo-app",
    workspaceName: String = "demo-app",
    state: MenubarCandidateState,
    turnCount: Int = 3
) -> MenubarSessionItem {
    MenubarSessionItem(
        host: host,
        targetHost: targetHost,
        direction: "\(host)_to_\(targetHost)",
        sessionId: sessionId,
        title: title,
        workspacePath: workspacePath,
        workspaceName: workspaceName,
        paired: true,
        state: state,
        updatedAtIso: "2026-11-15T08:05:00Z",
        sortEpoch: 1_794_730_000.0,
        turnCount: turnCount,
        hasInflightTurn: state == .interruptedStopped || state == .activeRunning
    )
}

final class MenubarViewModelTests: XCTestCase {
    func testDecodeMenubarFeedAllFiveStates() throws {
        let json = """
        {
          "generated_at_iso": "2026-11-15T08:06:40Z",
          "agy_daemon_active": true,
          "codex_daemon_active": false,
          "hero": {
            "host": "antigravity",
            "target_host": "codex",
            "direction": "antigravity_to_codex",
            "session_id": "cid-hero",
            "title": "Stopped mid turn",
            "workspace_path": "/repos/alpha",
            "workspace_name": "alpha",
            "paired": true,
            "state": "interrupted_stopped",
            "updated_at_iso": "2026-11-15T08:06:00Z",
            "sort_epoch": 1794729960.0,
            "turn_count": 4,
            "has_inflight_turn": true,
            "handed_target_id": null,
            "pending_error": null,
            "chain_depth": 1
          },
          "recent": [
            {
              "host": "antigravity",
              "target_host": "codex",
              "direction": "antigravity_to_codex",
              "session_id": "cid-hero",
              "title": "Stopped mid turn",
              "workspace_path": "/repos/alpha",
              "workspace_name": "alpha",
              "paired": true,
              "state": "interrupted_stopped",
              "updated_at_iso": "2026-11-15T08:06:00Z",
              "sort_epoch": 1794729960.0,
              "turn_count": 4,
              "has_inflight_turn": true,
              "handed_target_id": null,
              "pending_error": null,
              "chain_depth": 1
            },
            {
              "host": "codex",
              "target_host": "antigravity",
              "direction": "codex_to_antigravity",
              "session_id": "tid-ready",
              "title": "Ready codex thread",
              "workspace_path": "/repos/beta",
              "workspace_name": "beta",
              "paired": true,
              "state": "completed_ready",
              "updated_at_iso": "2026-11-15T08:05:00Z",
              "sort_epoch": 1794729900.0,
              "turn_count": 6,
              "has_inflight_turn": false,
              "handed_target_id": null,
              "pending_error": null,
              "chain_depth": 0
            },
            {
              "host": "antigravity",
              "target_host": "codex",
              "direction": "antigravity_to_codex",
              "session_id": "cid-pending",
              "title": "Pending reconcile item",
              "workspace_path": "/repos/alpha",
              "workspace_name": "alpha",
              "paired": false,
              "state": "pending_reconcile",
              "updated_at_iso": "2026-11-15T08:04:00Z",
              "sort_epoch": 1794729840.0,
              "turn_count": 2,
              "has_inflight_turn": false,
              "handed_target_id": null,
              "pending_error": "Daemon restart pending",
              "chain_depth": 0
            },
            {
              "host": "codex",
              "target_host": "antigravity",
              "direction": "codex_to_antigravity",
              "session_id": "tid-handed",
              "title": "Already handed item",
              "workspace_path": "/repos/beta",
              "workspace_name": "beta",
              "paired": true,
              "state": "already_handed",
              "updated_at_iso": "2026-11-15T08:03:00Z",
              "sort_epoch": 1794729780.0,
              "turn_count": 5,
              "has_inflight_turn": false,
              "handed_target_id": "cid-target-88",
              "pending_error": null,
              "chain_depth": 2
            },
            {
              "host": "antigravity",
              "target_host": "codex",
              "direction": "antigravity_to_codex",
              "session_id": "cid-running",
              "title": "Running live session",
              "workspace_path": "/repos/alpha",
              "workspace_name": "alpha",
              "paired": true,
              "state": "active_running",
              "updated_at_iso": "2026-11-15T08:02:00Z",
              "sort_epoch": 1794729720.0,
              "turn_count": 1,
              "has_inflight_turn": true,
              "handed_target_id": null,
              "pending_error": null,
              "chain_depth": 0
            }
          ]
        }
        """

        let data = try XCTUnwrap(json.data(using: .utf8))
        let snapshot = try JSONDecoder().decode(MenubarFeedSnapshot.self, from: data)
        XCTAssertTrue(snapshot.agyDaemonActive)
        XCTAssertFalse(snapshot.codexDaemonActive)
        XCTAssertEqual(snapshot.hero?.state, .interruptedStopped)
        XCTAssertEqual(snapshot.hero?.chainDepth, 1)
        XCTAssertEqual(
            snapshot.recent.map(\.state),
            [.interruptedStopped, .completedReady, .pendingReconcile, .alreadyHanded, .activeRunning]
        )
        XCTAssertEqual(snapshot.recent[2].pendingError, "Daemon restart pending")
        XCTAssertEqual(snapshot.recent[3].handedTargetId, "cid-target-88")
    }

    @MainActor
    func testPerformPrimaryActionRescuesHeroActivatesTargetAndSetsBanner() async {
        let hero = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-stopped-1",
            title: "Rescue broken turn",
            workspacePath: "/repos/alpha",
            workspaceName: "alpha",
            state: .interruptedStopped
        )
        let snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T08:06:40Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: hero,
            recent: [hero]
        )
        let receipt = HandoffReceiptPayload(
            workspace: "/repos/alpha",
            sourceHost: "antigravity",
            sourceId: "cid-stopped-1",
            targetHost: "codex",
            targetId: "01970000-0000-7000-8000-000000000077",
            title: "Rescue broken turn",
            status: "ready"
        )
        let cli = MockCLIRunner(
            feedResult: .success(snapshot),
            handoffResult: .success(receipt),
            reconcileResult: .success(ReconcileSummaryPayload(workspace: "/repos/alpha"))
        )
        let activator = MockActivator()
        var callbackCount = 0
        let vm = HandoffViewModel(
            cli: cli,
            activator: activator,
            onDidHandoffAndActivate: { callbackCount += 1 }
        )

        await vm.refresh()
        await vm.performPrimaryAction()

        XCTAssertEqual(cli.handoffCalls.count, 1)
        XCTAssertEqual(cli.handoffCalls.first?.workspacePath, "/repos/alpha")
        XCTAssertEqual(cli.handoffCalls.first?.sourceId, "cid-stopped-1")
        XCTAssertEqual(activator.activatedHosts, ["codex"])
        XCTAssertEqual(callbackCount, 1)
        XCTAssertFalse(vm.cliMissing)

        guard case .success(let message)? = vm.banner else {
            return XCTFail("Expected success banner, got \(String(describing: vm.banner))")
        }
        XCTAssertTrue(message.contains("已交接至"), "Unexpected banner: \(message)")
        XCTAssertTrue(message.contains("Codex"), "Unexpected banner: \(message)")
    }

    @MainActor
    func testNoOpHandoffIncludesNoOpNoticeAndActivatesTarget() async {
        let hero = makeItem(
            host: "codex",
            targetHost: "antigravity",
            sessionId: "tid-ready-2",
            title: "Reuse existing target",
            workspacePath: "/repos/beta",
            workspaceName: "beta",
            state: .completedReady
        )
        let snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T08:06:40Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: hero,
            recent: [hero]
        )
        let receipt = HandoffReceiptPayload(
            workspace: "/repos/beta",
            sourceHost: "codex",
            sourceId: "tid-ready-2",
            targetHost: "antigravity",
            targetId: "cid-existing-1",
            title: "Reuse existing target",
            status: "no_op"
        )
        let cli = MockCLIRunner(
            feedResult: .success(snapshot),
            handoffResult: .success(receipt),
            reconcileResult: .success(ReconcileSummaryPayload(workspace: "/repos/beta"))
        )
        let activator = MockActivator()
        let vm = HandoffViewModel(cli: cli, activator: activator)

        await vm.refresh()
        await vm.handoff(hero)

        XCTAssertEqual(activator.activatedHosts, ["antigravity"])
        guard case .success(let msg)? = vm.banner else {
            return XCTFail("Expected success banner for no_op")
        }
        XCTAssertTrue(msg.contains("已交接至 Antigravity"))
        XCTAssertTrue(msg.contains("no_op"))
    }

    @MainActor
    func testPendingReconcilePrimaryActionCallsReconcileAndRefreshes() async throws {
        let pendingHero = makeItem(
            sessionId: "cid-pending-9",
            title: "Pending turn",
            workspacePath: "/repos/gamma",
            workspaceName: "gamma",
            state: .pendingReconcile
        )
        let snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T08:06:40Z",
            agyDaemonActive: false,
            codexDaemonActive: true,
            hero: pendingHero,
            recent: [pendingHero]
        )
        let summary = ReconcileSummaryPayload(
            workspace: "/repos/gamma",
            dryRun: false,
            promoted: [
                ReconcileEntryPayload(
                    sourceHost: "antigravity",
                    sourceId: "cid-pending-9",
                    targetHost: "codex",
                    targetId: "tid-9",
                    title: "Pending turn",
                    reason: "promoted"
                )
            ],
            cleared: [],
            unresolved: [],
            totalPendingBefore: 1,
            totalPendingAfter: 0
        )
        let cli = MockCLIRunner(
            feedResult: .success(snapshot),
            handoffResult: .failure(CLIClientError.nonZeroExit(exitCode: 2, stderr: "unexpected")),
            reconcileResult: .success(summary)
        )
        let activator = MockActivator()
        let vm = HandoffViewModel(cli: cli, activator: activator)

        await vm.refresh()
        await vm.performPrimaryAction()

        XCTAssertEqual(cli.reconcileCalls, ["/repos/gamma"])
        XCTAssertEqual(cli.fetchLimits.count, 2)
        XCTAssertTrue(activator.activatedHosts.isEmpty)
        guard case .success(let msg)? = vm.banner else {
            return XCTFail("Expected success banner after reconcile")
        }
        XCTAssertTrue(msg.contains("晋升 1 项"))

        let defaultSummary = try await cli.reconcilePending()
        XCTAssertEqual(defaultSummary.workspace, "/repos/gamma")
        XCTAssertEqual(cli.reconcileCalls, ["/repos/gamma", nil])
    }

    @MainActor
    func testStructuredCLIErrorCliMissingAndSkipActivationEnv() async {
        let hero = makeItem(
            sessionId: "cid-1",
            workspacePath: "/repos/delta",
            state: .completedReady
        )
        let snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T08:06:40Z",
            agyDaemonActive: true,
            codexDaemonActive: false,
            hero: hero,
            recent: [hero]
        )
        let cliErrPayload = CLIErrorPayload(
            error: "source_active",
            message: "Source session is active",
            cause: "cascade_run_status=2"
        )
        let cli = MockCLIRunner(
            feedResult: .success(snapshot),
            handoffResult: .failure(CLIClientError.structuredError(cliErrPayload, exitCode: 2)),
            reconcileResult: .success(ReconcileSummaryPayload(workspace: "/repos/delta"))
        )
        let vm = HandoffViewModel(cli: cli, activator: MockActivator())

        await vm.refresh()
        await vm.handoff(hero)
        XCTAssertFalse(vm.cliMissing)
        guard case .error(let errMsg)? = vm.banner else {
            return XCTFail("Expected structured error banner")
        }
        XCTAssertTrue(errMsg.contains("Source session is active"))
        XCTAssertTrue(errMsg.contains("cascade_run_status=2"))

        cli.setFeedResult(.failure(CLIClientError.cliMissing(searchedLocations: ["/usr/local/bin/codex-agy-sync"])))
        await vm.refresh()
        XCTAssertTrue(vm.cliMissing)
        if case .error(let missingMsg)? = vm.banner {
            XCTAssertTrue(missingMsg.contains("codex-agy-sync"))
        } else {
            XCTFail("Expected cliMissing error banner")
        }

        let stderrJSON = Data(#"{"error":"bad_args","message":"Invalid workspace","cause":"missing"}"#.utf8)
        let decoded = SubprocessCLIClient.decodeCLIError(from: stderrJSON)
        XCTAssertEqual(decoded, CLIErrorPayload(error: "bad_args", message: "Invalid workspace", cause: "missing"))

        let activator = WorkspaceAppActivator(environment: ["MENUBAR_SKIP_APP_ACTIVATION": "1"])
        let skippedCodex = await activator.activateTarget(host: "codex")
        let skippedAgy = await activator.activateTarget(host: "antigravity")
        let unknownHost = await activator.activateTarget(host: "unknown")
        XCTAssertTrue(skippedCodex)
        XCTAssertTrue(skippedAgy)
        XCTAssertFalse(unknownHost)
        XCTAssertEqual(activator.recordedActivations, ["codex", "antigravity"])
    }

    func testSubprocessCLIClientExecutesAndDecodesStructuredError() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-cli-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let scriptURL = dir.appendingPathComponent("codex-agy-sync")
        let script = """
        #!/bin/sh
        if [ "$1" = "menubar-feed" ]; then
          cat <<'JSON'
        {"generated_at_iso":"2026-11-15T08:06:40Z","agy_daemon_active":true,"codex_daemon_active":true,"hero":null,"recent":[]}
        JSON
          exit 0
        fi
        cat <<'ERR' >&2
        {"error":"handoff_blocked","message":"Source session is active","cause":"running"}
        ERR
        exit 2
        """
        try script.write(to: scriptURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptURL.path)

        let client = SubprocessCLIClient(environment: ["CODEX_AGY_SYNC_BIN": scriptURL.path])
        let snap = try await client.fetchFeed(limit: 10)
        XCTAssertTrue(snap.agyDaemonActive)
        XCTAssertNil(snap.hero)

        do {
            _ = try await client.executeHandoff(workspacePath: "/repos/alpha", sourceId: "cid-1")
            XCTFail("Expected structured CLIErrorPayload")
        } catch let err as CLIClientError {
            XCTAssertEqual(
                err,
                .structuredError(
                    CLIErrorPayload(
                        error: "handoff_blocked",
                        message: "Source session is active",
                        cause: "running"
                    ),
                    exitCode: 2
                )
            )
        }
    }
}
