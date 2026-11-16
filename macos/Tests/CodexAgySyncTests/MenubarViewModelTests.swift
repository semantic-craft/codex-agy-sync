import AppKit
import Foundation
import SwiftUI
import XCTest
@testable import CodexAgySyncCore

private let forbiddenJargonTokens = ["抢救", "no_op", "挂起交接", "晋升"]

private func assertNoForbiddenJargon(_ text: String, file: StaticString = #filePath, line: UInt = #line) {
    for token in forbiddenJargonTokens {
        XCTAssertFalse(
            text.contains(token),
            "Unexpected jargon '\(token)' in user-facing text: \(text)",
            file: file,
            line: line
        )
    }
}

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

    func setHandoffResult(_ result: Result<HandoffReceiptPayload, Error>) {
        lock.withLock {
            handoffResult = result
        }
    }

    func setReconcileResult(_ result: Result<ReconcileSummaryPayload, Error>) {
        lock.withLock {
            reconcileResult = result
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
    sortEpoch: Double = 1_794_730_000.0,
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
        sortEpoch: sortEpoch,
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

    func testMenubarRowPresentationAndCandidateBadgeNormalizedCopy() {
        for state in MenubarCandidateState.allCases {
            assertNoForbiddenJargon(state.statusBadgeText)
        }
        XCTAssertEqual(MenubarCandidateState.interruptedStopped.statusBadgeText, "未完成回复")
        XCTAssertEqual(MenubarCandidateState.completedReady.statusBadgeText, "可继续")
        XCTAssertEqual(MenubarCandidateState.pendingReconcile.statusBadgeText, "待确认")
        XCTAssertEqual(MenubarCandidateState.alreadyHanded.statusBadgeText, "已同步")
        XCTAssertEqual(MenubarCandidateState.activeRunning.statusBadgeText, "对话中")

        let interrupted = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-int",
            title: "Fix stream parser",
            workspaceName: "parser",
            state: .interruptedStopped,
            turnCount: 4
        )
        let intPres = interrupted.presentation()
        XCTAssertEqual(intPres.monogram, "A")
        XCTAssertTrue(intPres.isActionable)
        XCTAssertEqual(intPres.subtitle, "parser · 4 轮 · 未完成回复")
        XCTAssertEqual(intPres.selectedActionTitle, "用 Codex 继续")
        XCTAssertEqual(intPres.unselectedTrailingTitle, "→ Codex")
        XCTAssertEqual(intPres.accessibilityLabel, "用 Codex 继续 · Fix stream parser")

        let agyAlias = makeItem(host: "agy", targetHost: "codex", sessionId: "cid-alias", state: .completedReady)
        XCTAssertEqual(agyAlias.presentation().monogram, "A")

        let ready = makeItem(
            host: "codex",
            targetHost: "antigravity",
            sessionId: "tid-ready",
            title: "Add migrations",
            workspaceName: "backend",
            state: .completedReady,
            sortEpoch: 1_000.0,
            turnCount: 6
        )
        XCTAssertEqual(ready.presentation().subtitle, "backend · 6 轮")
        XCTAssertEqual(ready.presentation(nowEpoch: 1_025.0).subtitle, "backend · 6 轮 · 刚刚")
        XCTAssertEqual(ready.presentation(nowEpoch: 1_300.0).subtitle, "backend · 6 轮 · 5 分钟前")
        XCTAssertEqual(ready.presentation(nowEpoch: 8_200.0).subtitle, "backend · 6 轮 · 2 小时前")
        XCTAssertEqual(ready.presentation(nowEpoch: 173_800.0).subtitle, "backend · 6 轮 · 2 天前")
        let readyPres = ready.presentation(nowEpoch: 1_300.0)
        XCTAssertEqual(readyPres.monogram, "C")
        XCTAssertTrue(readyPres.isActionable)
        XCTAssertEqual(readyPres.selectedActionTitle, "用 Antigravity 继续")
        XCTAssertEqual(readyPres.unselectedTrailingTitle, "→ Antigravity")
        XCTAssertEqual(readyPres.accessibilityLabel, "用 Antigravity 继续 · Add migrations")

        let pending = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-pend",
            title: "Sync workspace",
            workspaceName: "bridge",
            state: .pendingReconcile
        )
        let pendingPres = pending.presentation()
        XCTAssertTrue(pendingPres.isActionable)
        XCTAssertEqual(pendingPres.subtitle, "bridge · 上次同步未完成确认")
        XCTAssertEqual(pendingPres.selectedActionTitle, "重新同步状态")
        XCTAssertEqual(pendingPres.unselectedTrailingTitle, "重试")
        XCTAssertEqual(pendingPres.accessibilityLabel, "重新同步状态 · Sync workspace")

        let handed = makeItem(
            host: "codex",
            targetHost: "antigravity",
            sessionId: "tid-done",
            title: "Ship release",
            workspaceName: "bridge",
            state: .alreadyHanded,
            turnCount: 5
        )
        let handedPres = handed.presentation()
        XCTAssertFalse(handedPres.isActionable)
        XCTAssertEqual(handedPres.subtitle, "bridge · 5 轮 · 已同步")
        XCTAssertEqual(handedPres.selectedActionTitle, "已同步")
        XCTAssertEqual(handedPres.unselectedTrailingTitle, "已同步")
        XCTAssertEqual(handedPres.accessibilityLabel, "Ship release · 已同步")

        let running = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-live",
            title: "Live debugging",
            workspaceName: "bridge",
            state: .activeRunning
        )
        let runningPres = running.presentation()
        XCTAssertFalse(runningPres.isActionable)
        XCTAssertEqual(runningPres.subtitle, "bridge · 对话进行中")
        XCTAssertEqual(runningPres.selectedActionTitle, "对话中")
        XCTAssertEqual(runningPres.unselectedTrailingTitle, "对话中")
        XCTAssertEqual(runningPres.accessibilityLabel, "Live debugging · 对话中")

        for pres in [intPres, readyPres, pendingPres, handedPres, runningPres] {
            assertNoForbiddenJargon(pres.subtitle)
            assertNoForbiddenJargon(pres.selectedActionTitle)
            assertNoForbiddenJargon(pres.unselectedTrailingTitle)
            assertNoForbiddenJargon(pres.accessibilityLabel)
        }
    }

    @MainActor
    func testSearchFilteringDeduplicationAndKeyboardSelectionNavigation() async {
        let hero = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-hero",
            title: "Refactor auth token flow",
            workspacePath: "/repos/auth-service",
            workspaceName: "auth-service",
            state: .interruptedStopped
        )
        let handed = makeItem(
            host: "codex",
            targetHost: "antigravity",
            sessionId: "tid-handed",
            title: "Old synced chat",
            workspacePath: "/repos/auth-service",
            workspaceName: "auth-service",
            state: .alreadyHanded
        )
        let second = makeItem(
            host: "codex",
            targetHost: "antigravity",
            sessionId: "tid-second",
            title: "Optimize SQLite query",
            workspacePath: "/repos/storage-engine",
            workspaceName: "storage-engine",
            state: .completedReady
        )
        let third = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-third",
            title: "Verify billing webhook",
            workspacePath: "/repos/billing",
            workspaceName: "billing",
            state: .pendingReconcile
        )
        let snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T08:06:40Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: hero,
            recent: [hero, handed, second, third]
        )
        let receipt = HandoffReceiptPayload(
            workspace: "/repos/storage-engine",
            sourceHost: "codex",
            sourceId: "tid-second",
            targetHost: "antigravity",
            targetId: "cid-target-2",
            title: "Optimize SQLite query",
            status: "ready"
        )
        let cli = MockCLIRunner(
            feedResult: .success(snapshot),
            handoffResult: .success(receipt),
            reconcileResult: .success(ReconcileSummaryPayload(workspace: "/repos/billing"))
        )
        let vm = HandoffViewModel(cli: cli, activator: MockActivator())

        await vm.refresh()

        XCTAssertEqual(vm.sections.map(\.id), ["continue", "recent"])
        XCTAssertEqual(vm.sections.first?.title, "继续当前工作")
        XCTAssertEqual(vm.sections.last?.title, "最近对话")
        XCTAssertEqual(vm.sections.last?.items.map(\.id), [handed.id, second.id, third.id])
        XCTAssertEqual(vm.selectedItemId, hero.id)

        vm.moveSelectionDown()
        XCTAssertEqual(vm.selectedItemId, second.id)

        vm.moveSelectionDown()
        XCTAssertEqual(vm.selectedItemId, third.id)

        vm.moveSelectionUp()
        XCTAssertEqual(vm.selectedItemId, second.id)

        await vm.performSelectedAction()
        XCTAssertEqual(cli.handoffCalls.count, 1)
        XCTAssertEqual(cli.handoffCalls.first?.workspacePath, "/repos/storage-engine")
        XCTAssertEqual(cli.handoffCalls.first?.sourceId, "tid-second")

        vm.searchQuery = "BILLING"
        XCTAssertEqual(vm.sections.map(\.id), ["recent"])
        XCTAssertEqual(vm.visibleItems.map(\.id), [third.id])
        XCTAssertEqual(vm.selectedItemId, third.id)

        vm.searchQuery = "unmatched-keyword-xyz"
        XCTAssertTrue(vm.sections.isEmpty)
        XCTAssertTrue(vm.visibleItems.isEmpty)
        XCTAssertNil(vm.selectedItemId)

        vm.searchQuery = ""
        XCTAssertEqual(vm.sections.count, 2)
        XCTAssertEqual(vm.selectedItemId, hero.id)
    }

    @MainActor
    func testPerformPrimaryActionHandoffHeroActivatesTargetAndSetsBanner() async {
        let hero = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-stopped-1",
            title: "Resume broken turn",
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
            title: "Resume broken turn",
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

        guard let banner = vm.banner else {
            return XCTFail("Expected banner after handoff")
        }
        XCTAssertEqual(banner.kind, .success)
        XCTAssertEqual(banner.text, "已在 Codex 打开对话 · Resume broken turn")
        assertNoForbiddenJargon(banner.text)
    }

    @MainActor
    func testNoOpAndUnconfirmedHandoffBannersUseNormalizedCopy() async {
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
        guard let infoBanner = vm.banner else {
            return XCTFail("Expected info banner for no_op")
        }
        XCTAssertEqual(infoBanner.kind, .info)
        XCTAssertEqual(infoBanner.text, "已在 Antigravity 打开现有对话 · Reuse existing target")
        assertNoForbiddenJargon(infoBanner.text)

        let pendingReceipt = HandoffReceiptPayload(
            workspace: "/repos/beta",
            sourceHost: "codex",
            sourceId: "tid-ready-2",
            targetHost: "antigravity",
            targetId: nil,
            title: "Reuse existing target",
            status: "pending"
        )
        cli.setHandoffResult(.success(pendingReceipt))
        await vm.handoff(hero)
        guard let pendingBanner = vm.banner else {
            return XCTFail("Expected error banner for pending receipt")
        }
        XCTAssertEqual(pendingBanner.kind, .error)
        XCTAssertEqual(pendingBanner.text, "同步尚未完成确认，请稍后点击重试同步状态。")
        assertNoForbiddenJargon(pendingBanner.text)
    }

    @MainActor
    func testPendingReconcilePrimaryActionCallsReconcileAndNormalizesBanners() async throws {
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
        let resolvedSummary = ReconcileSummaryPayload(
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
            reconcileResult: .success(resolvedSummary)
        )
        let activator = MockActivator()
        let vm = HandoffViewModel(cli: cli, activator: activator)

        await vm.refresh()
        await vm.performPrimaryAction()

        XCTAssertEqual(cli.reconcileCalls, ["/repos/gamma"])
        XCTAssertEqual(cli.fetchLimits.count, 2)
        XCTAssertTrue(activator.activatedHosts.isEmpty)
        XCTAssertEqual(vm.banner, StatusBanner(kind: .success, text: "状态已同步"))
        assertNoForbiddenJargon(vm.banner?.text ?? "")

        let unresolvedEntry = ReconcileEntryPayload(
            sourceHost: "antigravity",
            sourceId: "cid-pending-9",
            targetHost: "codex",
            targetId: nil,
            title: "Pending turn",
            reason: "locked"
        )
        cli.setReconcileResult(
            .success(
                ReconcileSummaryPayload(
                    workspace: "/repos/gamma",
                    unresolved: [unresolvedEntry, unresolvedEntry]
                )
            )
        )
        await vm.reconcile(workspacePath: "/repos/gamma")
        XCTAssertEqual(vm.banner, StatusBanner(kind: .success, text: "状态已同步，仍有 2 条对话等待响应"))
        assertNoForbiddenJargon(vm.banner?.text ?? "")

        cli.setReconcileResult(.failure(CLIClientError.nonZeroExit(exitCode: 2, stderr: "daemon offline")))
        await vm.reconcile(workspacePath: "/repos/gamma")
        guard let errBanner = vm.banner else {
            return XCTFail("Expected error banner when reconcile fails")
        }
        XCTAssertEqual(errBanner.kind, .error)
        XCTAssertTrue(errBanner.text.hasPrefix("状态同步未成功："))
        assertNoForbiddenJargon(errBanner.text)

        cli.setReconcileResult(.success(resolvedSummary))
        let defaultSummary = try await cli.reconcilePending()
        XCTAssertEqual(defaultSummary.workspace, "/repos/gamma")

        let readyHero = makeItem(
            sessionId: "cid-ready-only",
            title: "No pending items",
            workspacePath: "/repos/clean",
            workspaceName: "clean",
            state: .completedReady
        )
        let cleanSnapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T08:10:00Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: readyHero,
            recent: [readyHero]
        )
        let cleanCLI = MockCLIRunner(
            feedResult: .success(cleanSnapshot),
            handoffResult: .failure(CLIClientError.nonZeroExit(exitCode: 2, stderr: "unused")),
            reconcileResult: .failure(CLIClientError.nonZeroExit(exitCode: 2, stderr: "cwd=/ must never be reconciled"))
        )
        let cleanVM = HandoffViewModel(cli: cleanCLI, activator: MockActivator())
        await cleanVM.refresh()
        await cleanVM.reconcile(workspacePath: nil)
        XCTAssertTrue(cleanCLI.reconcileCalls.isEmpty)
        XCTAssertEqual(cleanCLI.fetchLimits.count, 2)
        XCTAssertEqual(cleanVM.banner, StatusBanner(kind: .success, text: "状态已同步"))
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
        XCTAssertTrue(errMsg.hasPrefix("同步未成功："))
        XCTAssertTrue(errMsg.contains("Source session is active"))
        XCTAssertTrue(errMsg.contains("cascade_run_status=2"))
        assertNoForbiddenJargon(errMsg)

        cli.setFeedResult(.failure(CLIClientError.cliMissing(searchedLocations: ["/usr/local/bin/codex-agy-sync"])))
        await vm.refresh()
        XCTAssertTrue(vm.cliMissing)
        if case .error(let missingMsg)? = vm.banner {
            XCTAssertTrue(missingMsg.contains("codex-agy-sync"))
            XCTAssertTrue(missingMsg.contains("选择工具…"))
            assertNoForbiddenJargon(missingMsg)
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

        let argsLogURL = dir.appendingPathComponent("argv.log")
        let scriptURL = dir.appendingPathComponent("codex-agy-sync")
        let script = """
        #!/bin/sh
        printf '%s\\n' "$*" >> "\(argsLogURL.path)"
        if [ "$1" = "menubar-feed" ]; then
          cat <<'JSON'
        {"generated_at_iso":"2026-11-15T08:06:40Z","agy_daemon_active":true,"codex_daemon_active":true,"hero":null,"recent":[]}
        JSON
          exit 0
        fi
        if [ "$1" = "sync" ] && [ "$2" = "--reconcile" ]; then
          cat <<'JSON'
        {"workspace":"","dry_run":false,"promoted":[],"cleared":[],"unresolved":[],"total_pending_before":0,"total_pending_after":0}
        JSON
          exit 0
        fi
        if [ "$1" = "sync" ] && [ "$4" = "--reconcile" ]; then
          cat <<'JSON'
        {"workspace":"/repos/alpha","dry_run":false,"promoted":[],"cleared":[],"unresolved":[],"total_pending_before":0,"total_pending_after":0}
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

        let recWorkspace = try await client.reconcilePending(workspacePath: "/repos/alpha")
        XCTAssertEqual(recWorkspace.workspace, "/repos/alpha")
        let recGlobal = try await client.reconcilePending()
        XCTAssertEqual(recGlobal.workspace, "")

        let loggedLines = try String(contentsOf: argsLogURL, encoding: .utf8)
            .split(whereSeparator: \.isNewline)
            .map(String.init)
        XCTAssertEqual(
            loggedLines,
            [
                "menubar-feed --limit 10",
                "sync --workspace /repos/alpha --source-id cid-1 --auto-pair --include-interrupted",
                "sync --workspace /repos/alpha --reconcile",
                "sync --reconcile",
            ]
        )
    }

    @MainActor
    func testPopoverHostingZeroTitlebarSafeAreaAndAccessibilitySearchSetValue() async throws {
        let hero = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-ax-1",
            title: "Verify AX search input",
            workspacePath: "/repos/ax-demo",
            workspaceName: "ax-demo",
            state: .interruptedStopped
        )
        let snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T08:06:40Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: hero,
            recent: [hero]
        )
        let cli = MockCLIRunner(
            feedResult: .success(snapshot),
            handoffResult: .failure(CLIClientError.nonZeroExit(exitCode: 2, stderr: "unused")),
            reconcileResult: .success(ReconcileSummaryPayload(workspace: "/repos/ax-demo"))
        )
        let vm = HandoffViewModel(cli: cli, activator: MockActivator())
        await vm.refresh()

        let hosting = NSHostingController(rootView: PopoverView(viewModel: vm))
        hosting.sizingOptions = [.preferredContentSize]
        hosting.safeAreaRegions = []

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 392, height: 240),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.contentViewController = hosting
        hosting.view.layoutSubtreeIfNeeded()

        XCTAssertEqual(hosting.view.safeAreaInsets.top, 0)
        XCTAssertEqual(hosting.view.safeAreaInsets.bottom, 0)

        func findSearchField(in root: NSView?) -> PaletteSearchTextField? {
            guard let root else { return nil }
            if let tf = root as? PaletteSearchTextField {
                return tf
            }
            for child in root.subviews {
                if let found = findSearchField(in: child) {
                    return found
                }
            }
            return nil
        }

        let searchField = try XCTUnwrap(findSearchField(in: panel.contentView))
        XCTAssertEqual(searchField.focusRingType, .none)
        XCTAssertFalse(searchField.isBordered)
        XCTAssertFalse(searchField.drawsBackground)
        XCTAssertEqual(searchField.accessibilityLabel(), "筛选项目或对话标题")
        XCTAssertTrue(
            searchField.isAccessibilitySelectorAllowed(
                #selector(PaletteSearchTextField.setAccessibilityValue(_:))
            )
        )

        _ = panel.makeFirstResponder(searchField)
        searchField.setAccessibilityValue("zzz-unmatched-query")
        XCTAssertEqual(searchField.stringValue, "zzz-unmatched-query")
        if let editor = searchField.currentEditor() {
            XCTAssertEqual(editor.string, "zzz-unmatched-query")
        }
        XCTAssertEqual(vm.searchQuery, "zzz-unmatched-query")
        XCTAssertTrue(vm.sections.isEmpty)

        searchField.cell?.setAccessibilityValue("")
        XCTAssertEqual(searchField.stringValue, "")
        XCTAssertEqual(vm.searchQuery, "")
        XCTAssertEqual(vm.sections.count, 1)
    }

    @MainActor
    func testRefreshAndPerformPrimaryActionAwaitsFreshAndInflightFeed() async {
        let staleHero = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-stale",
            title: "Stale turn",
            workspacePath: "/repos/stale",
            workspaceName: "stale",
            state: .completedReady
        )
        let freshHero = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-fresh",
            title: "Fresh interrupted turn",
            workspacePath: "/repos/fresh",
            workspaceName: "fresh",
            state: .interruptedStopped
        )
        let staleSnapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T08:00:00Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: staleHero,
            recent: [staleHero]
        )
        let freshSnapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T08:06:40Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: freshHero,
            recent: [freshHero]
        )
        let freshReceipt = HandoffReceiptPayload(
            workspace: "/repos/fresh",
            sourceHost: "antigravity",
            sourceId: "cid-fresh",
            targetHost: "codex",
            targetId: "tid-fresh-1",
            title: "Fresh interrupted turn",
            status: "ready"
        )
        let cli = MockCLIRunner(
            feedResult: .success(staleSnapshot),
            handoffResult: .success(freshReceipt),
            reconcileResult: .success(ReconcileSummaryPayload(workspace: "/repos/fresh"))
        )
        let activator = MockActivator()
        let vm = HandoffViewModel(cli: cli, activator: activator)

        await vm.refresh()
        XCTAssertEqual(vm.snapshot?.hero?.sessionId, "cid-stale")

        cli.setFeedResult(.success(freshSnapshot))
        await vm.refreshAndPerformPrimaryAction()

        XCTAssertEqual(cli.handoffCalls.count, 1)
        XCTAssertEqual(cli.handoffCalls.first?.sourceId, "cid-fresh")
        XCTAssertEqual(activator.activatedHosts, ["codex"])
        XCTAssertEqual(vm.banner, StatusBanner(kind: .success, text: "已在 Codex 打开对话 · Fresh interrupted turn"))

        final class DelayedFeedCLIRunner: CLIRunnerProtocol, @unchecked Sendable {
            private let lock = NSLock()
            private let snapshot: MenubarFeedSnapshot
            private let receipt: HandoffReceiptPayload
            private(set) var fetchCount = 0
            private(set) var handedIds: [String] = []

            init(snapshot: MenubarFeedSnapshot, receipt: HandoffReceiptPayload) {
                self.snapshot = snapshot
                self.receipt = receipt
            }

            func fetchFeed(limit: Int) async throws -> MenubarFeedSnapshot {
                lock.withLock { fetchCount += 1 }
                try? await Task.sleep(nanoseconds: 60_000_000)
                return snapshot
            }

            func executeHandoff(workspacePath: String, sourceId: String) async throws -> HandoffReceiptPayload {
                lock.withLock { handedIds.append(sourceId) }
                return receipt
            }

            func reconcilePending(workspacePath: String?) async throws -> ReconcileSummaryPayload {
                ReconcileSummaryPayload(workspace: workspacePath ?? "")
            }
        }

        let inflightCLI = DelayedFeedCLIRunner(snapshot: freshSnapshot, receipt: freshReceipt)
        let inflightVM = HandoffViewModel(cli: inflightCLI, activator: MockActivator())
        let backgroundRefresh = Task { await inflightVM.refresh() }
        await Task.yield()
        await inflightVM.refreshAndPerformPrimaryAction()
        await backgroundRefresh.value

        XCTAssertEqual(inflightCLI.fetchCount, 2)
        XCTAssertEqual(inflightCLI.handedIds, ["cid-fresh"])
        XCTAssertEqual(inflightVM.banner, StatusBanner(kind: .success, text: "已在 Codex 打开对话 · Fresh interrupted turn"))
    }

    func testNormalizedModifierFlagsAndUserPythonBinDiscovery() throws {
        let noisyFlags: NSEvent.ModifierFlags = [.command, .capsLock, .numericPad, .function]
        XCTAssertEqual(HotKeyShortcut.normalizedEventFlags(noisyFlags), .command)

        let fakeHome = FileManager.default.temporaryDirectory
            .resolvingSymlinksInPath()
            .appendingPathComponent("codex-agy-home-\(UUID().uuidString)", isDirectory: true)
        let py39Bin = fakeHome.appendingPathComponent("Library/Python/3.9/bin", isDirectory: true)
        let py312Bin = fakeHome.appendingPathComponent("Library/Python/3.12/bin", isDirectory: true)
        let pathDir = fakeHome.appendingPathComponent("path-bin", isDirectory: true)
        let customDir = fakeHome.appendingPathComponent("custom-bin", isDirectory: true)
        let envDir = fakeHome.appendingPathComponent("env-bin", isDirectory: true)
        for dir in [py39Bin, py312Bin, pathDir, customDir, envDir] {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        defer { try? FileManager.default.removeItem(at: fakeHome) }

        func makeExec(in dir: URL) throws -> URL {
            let file = dir.appendingPathComponent("codex-agy-sync")
            try "#!/bin/sh\nexit 0\n".write(to: file, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
            return file
        }

        _ = try makeExec(in: py39Bin)
        let py312CLI = try makeExec(in: py312Bin)

        let suiteName = "dev.semanticcraft.codex-agy-sync.cli.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { cleanupIsolatedTestDefaults(defaults, suiteName: suiteName) }

        let pyBins = SubprocessCLIClient.userPythonBinDirectories(homeDirectory: fakeHome)
        XCTAssertEqual(
            pyBins.map { URL(fileURLWithPath: $0).resolvingSymlinksInPath().path },
            [py312Bin.resolvingSymlinksInPath().path, py39Bin.resolvingSymlinksInPath().path]
        )

        let autoResolved = SubprocessCLIClient.resolveExecutableWithSource(
            environment: ["PATH": "/nonexistent"],
            defaults: defaults,
            homeDirectory: fakeHome
        )
        XCTAssertEqual(
            autoResolved.url?.resolvingSymlinksInPath().path,
            py312CLI.resolvingSymlinksInPath().path
        )
        XCTAssertEqual(autoResolved.source, .autoDiscovered)

        _ = try makeExec(in: pathDir)
        let customCLI = try makeExec(in: customDir)
        defaults.set(customCLI.path, forKey: SubprocessCLIClient.cliExecutableDefaultsKey)

        let customResolved = SubprocessCLIClient.resolveExecutableWithSource(
            environment: ["PATH": pathDir.path],
            defaults: defaults,
            homeDirectory: fakeHome
        )
        XCTAssertEqual(customResolved.url?.path, customCLI.path)
        XCTAssertEqual(customResolved.source, .customPath)

        let envCLI = try makeExec(in: envDir)
        let envResolved = SubprocessCLIClient.resolveExecutableWithSource(
            environment: [
                "CODEX_AGY_SYNC_BIN": envCLI.path,
                "PATH": pathDir.path,
            ],
            defaults: defaults,
            homeDirectory: fakeHome
        )
        XCTAssertEqual(envResolved.url?.path, envCLI.path)
        XCTAssertEqual(envResolved.source, .envOverride)
    }

    func testRunProcessAsyncLargeStdoutNonZeroExitAndTimeout() async throws {
        let largeOut = try await SubprocessCLIClient.runProcessAsync(
            executableURL: URL(fileURLWithPath: "/usr/bin/perl"),
            arguments: ["-e", #"print "A" x 300000; print STDERR "E" x 40000;"#],
            environment: [:],
            timeoutSeconds: 5.0
        )
        XCTAssertEqual(largeOut.exitCode, 0)
        XCTAssertEqual(largeOut.stdout.count, 300_000)
        XCTAssertEqual(largeOut.stderr.count, 40_000)

        let failOut = try await SubprocessCLIClient.runProcessAsync(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", "echo async-err-detail >&2; exit 7"],
            environment: [:],
            timeoutSeconds: 5.0
        )
        XCTAssertEqual(failOut.exitCode, 7)
        XCTAssertEqual(
            String(data: failOut.stderr, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
            "async-err-detail"
        )

        let start = Date()
        do {
            _ = try await SubprocessCLIClient.runProcessAsync(
                executableURL: URL(fileURLWithPath: "/bin/sleep"),
                arguments: ["5"],
                environment: [:],
                timeoutSeconds: 0.15
            )
            XCTFail("Expected CLIClientError.timedOut")
        } catch let err as CLIClientError {
            guard case .timedOut(_, let seconds) = err else {
                return XCTFail("Expected timedOut, got \(err)")
            }
            XCTAssertEqual(seconds, 0.15, accuracy: 0.001)
            XCTAssertLessThan(Date().timeIntervalSince(start), 2.0)
        }

        let cmdResult = try await DefaultSystemCommandRunner().run(
            executableURL: URL(fileURLWithPath: "/bin/echo"),
            arguments: ["runner-ok"],
            environment: [:],
            timeoutSeconds: 2.0
        )
        XCTAssertEqual(cmdResult.exitCode, 0)
        XCTAssertEqual(cmdResult.stdoutString.trimmingCharacters(in: .whitespacesAndNewlines), "runner-ok")
    }

    func testRunAndDecodeSingleScanPythonBinParityAndLargeFeed() async throws {
        let dir = FileManager.default.temporaryDirectory
            .resolvingSymlinksInPath()
            .appendingPathComponent("codex-agy-parity-\(UUID().uuidString)", isDirectory: true)
        let py311Bin = dir.appendingPathComponent("Library/Python/3.11/bin", isDirectory: true)
        let pathBin = dir.appendingPathComponent("bin", isDirectory: true)
        try FileManager.default.createDirectory(at: py311Bin, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: pathBin, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let suiteName = "dev.semanticcraft.codex-agy-sync.parity.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { cleanupIsolatedTestDefaults(defaults, suiteName: suiteName) }

        let pyBins = SubprocessCLIClient.userPythonBinDirectories(homeDirectory: dir)
        let env: [String: String] = ["PATH": pathBin.path]

        let resolvedDefault = SubprocessCLIClient.resolveExecutableWithSource(
            environment: env,
            defaults: defaults,
            homeDirectory: dir
        )
        let resolvedPrecomputed = SubprocessCLIClient.resolveExecutableWithSource(
            environment: env,
            defaults: defaults,
            homeDirectory: dir,
            pythonBinDirectories: pyBins
        )
        XCTAssertEqual(resolvedDefault.url, resolvedPrecomputed.url)
        XCTAssertEqual(resolvedDefault.source, resolvedPrecomputed.source)
        XCTAssertEqual(resolvedDefault.searched, resolvedPrecomputed.searched)

        let mergedDefault = SubprocessCLIClient.mergedEnvironment(from: env, homeDirectory: dir)
        let mergedPrecomputed = SubprocessCLIClient.mergedEnvironment(
            from: env,
            homeDirectory: dir,
            pythonBinDirectories: pyBins
        )
        XCTAssertEqual(mergedDefault, mergedPrecomputed)

        let scriptURL = pathBin.appendingPathComponent("codex-agy-sync")
        let paddedTitle = String(repeating: "x", count: 140_000)
        let jsonBody = """
        {"generated_at_iso":"2026-11-16T00:00:00Z","agy_daemon_active":true,"codex_daemon_active":false,"hero":null,"recent":[{"host":"codex","target_host":"antigravity","direction":"codex_to_agy","session_id":"tid-large","title":"\(paddedTitle)","workspace_path":"/repos/large","workspace_name":"large","paired":true,"state":"completed_ready","updated_at_iso":"2026-11-16T00:00:00Z","sort_epoch":1794730000.0,"turn_count":2,"has_inflight_turn":false,"handed_target_id":null,"pending_error":null,"chain_depth":0}]}
        """
        let feedFile = dir.appendingPathComponent("large-feed.json")
        try jsonBody.write(to: feedFile, atomically: true, encoding: .utf8)
        let script = """
        #!/bin/sh
        cat "\(feedFile.path)"
        """
        try script.write(to: scriptURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptURL.path)

        let client = SubprocessCLIClient(environment: env, defaultsSuiteName: suiteName)
        let snap = try await client.fetchFeed(limit: 5)
        XCTAssertEqual(snap.recent.count, 1)
        XCTAssertEqual(snap.recent.first?.title.count, 140_000)
    }

    func testMenubarSessionItemMatchesSearchQueryFieldsAndStatusLabels() {
        let interrupted = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-1",
            title: "Fix Wire Decoder",
            workspacePath: "/repos/bridge-core",
            workspaceName: "bridge-core",
            state: .interruptedStopped,
            turnCount: 4
        )
        let ready = makeItem(
            host: "codex",
            targetHost: "antigravity",
            sessionId: "tid-2",
            title: "Lazy CLI Startup",
            workspacePath: "/repos/cli-fast",
            workspaceName: "cli-fast",
            state: .completedReady,
            turnCount: 6
        )
        let pending = makeItem(
            sessionId: "cid-3",
            title: "Pending Sync",
            workspaceName: "sync-ws",
            state: .pendingReconcile,
            turnCount: 7
        )
        let handed = makeItem(
            sessionId: "cid-4",
            title: "Done Sync",
            workspaceName: "done-ws",
            state: .alreadyHanded,
            turnCount: 5
        )
        let running = makeItem(
            sessionId: "cid-5",
            title: "Active Work",
            workspaceName: "live-ws",
            state: .activeRunning,
            turnCount: 8
        )

        XCTAssertTrue(interrupted.matchesSearchQuery("  "))
        XCTAssertTrue(interrupted.matchesSearchQuery("wire decoder"))
        XCTAssertTrue(interrupted.matchesSearchQuery("bridge-core"))
        XCTAssertTrue(interrupted.matchesSearchQuery("/repos/bridge-core"))
        XCTAssertTrue(interrupted.matchesSearchQuery("ANTIGRAVITY"))
        XCTAssertTrue(interrupted.matchesSearchQuery("codex"))
        XCTAssertTrue(interrupted.matchesSearchQuery("4 轮"))
        XCTAssertTrue(interrupted.matchesSearchQuery("未完成回复"))
        XCTAssertFalse(interrupted.matchesSearchQuery("可继续"))

        XCTAssertTrue(ready.matchesSearchQuery("可继续"))
        XCTAssertTrue(ready.matchesSearchQuery("6 轮"))
        XCTAssertTrue(ready.matchesSearchQuery("Antigravity"))

        XCTAssertTrue(pending.matchesSearchQuery("上次同步未完成确认"))
        XCTAssertFalse(pending.matchesSearchQuery("7 轮"))

        XCTAssertTrue(handed.matchesSearchQuery("已同步"))
        XCTAssertTrue(handed.matchesSearchQuery("5 轮"))

        XCTAssertTrue(running.matchesSearchQuery("对话进行中"))
        XCTAssertFalse(running.matchesSearchQuery("8 轮"))
    }

    @MainActor
    func testLiveLanguageSwitchingUpdatesBannerSectionsPresentationAndSearchQuery() async {
        for key in AppCopyKey.allCases {
            let zh = AppCopy.text(key, in: .zhHans)
            let en = AppCopy.text(key, in: .en)
            assertNoForbiddenJargon(zh)
            XCTAssertFalse(zh.contains("接力") || zh.contains("交接"), "Legacy handoff copy in \(key): \(zh)")
            XCTAssertFalse(
                en.localizedCaseInsensitiveContains("handoff") || en.localizedCaseInsensitiveContains("hand off"),
                "Legacy handoff copy in \(key): \(en)"
            )
        }

        let suiteName = "dev.semanticcraft.codex-agy-sync.lang-vm.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { cleanupIsolatedTestDefaults(defaults, suiteName: suiteName) }

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())

        let singleTurnReady = makeItem(
            host: "codex",
            targetHost: "antigravity",
            sessionId: "tid-1turn",
            title: "Verify schema",
            workspacePath: "/repos/alpha",
            workspaceName: "alpha",
            state: .completedReady,
            sortEpoch: 1_000.0,
            turnCount: 1
        )
        let interrupted = makeItem(
            host: "antigravity",
            targetHost: "codex",
            sessionId: "cid-int",
            title: "Fix parser",
            workspacePath: "/repos/beta",
            workspaceName: "beta",
            state: .interruptedStopped,
            turnCount: 4
        )
        let pending = makeItem(
            sessionId: "cid-pend",
            title: "Sync pending",
            workspaceName: "gamma",
            state: .pendingReconcile
        )
        let handed = makeItem(
            sessionId: "cid-done",
            title: "Shipped",
            workspaceName: "delta",
            state: .alreadyHanded,
            turnCount: 5
        )
        let running = makeItem(
            sessionId: "cid-live",
            title: "Running",
            workspaceName: "epsilon",
            state: .activeRunning
        )

        let readyEn = singleTurnReady.presentation(nowEpoch: 1_025.0, language: .en)
        XCTAssertEqual(readyEn.subtitle, "alpha · 1 turn · Just now")
        XCTAssertEqual(readyEn.selectedActionTitle, "Continue in Antigravity")
        XCTAssertEqual(singleTurnReady.presentation(nowEpoch: 1_300.0, language: .en).subtitle, "alpha · 1 turn · 5m ago")
        XCTAssertEqual(singleTurnReady.presentation(nowEpoch: 8_200.0, language: .en).subtitle, "alpha · 1 turn · 2h ago")
        XCTAssertEqual(singleTurnReady.presentation(nowEpoch: 173_800.0, language: .en).subtitle, "alpha · 1 turn · 2d ago")

        XCTAssertEqual(interrupted.presentation(language: .en).subtitle, "beta · 4 turns · Incomplete Reply")
        XCTAssertEqual(interrupted.presentation(language: .en).selectedActionTitle, "Continue in Codex")
        XCTAssertEqual(pending.presentation(language: .en).subtitle, "gamma · Previous sync unconfirmed")
        XCTAssertEqual(pending.presentation(language: .en).selectedActionTitle, "Resync Status")
        XCTAssertEqual(pending.presentation(language: .en).unselectedTrailingTitle, "Retry")
        XCTAssertEqual(handed.presentation(language: .en).subtitle, "delta · 5 turns · Synced")
        XCTAssertEqual(running.presentation(language: .en).subtitle, "epsilon · Session active")

        XCTAssertTrue(singleTurnReady.matchesSearchQuery("1 turn"))
        XCTAssertTrue(singleTurnReady.matchesSearchQuery("ready"))
        XCTAssertTrue(interrupted.matchesSearchQuery("incomplete reply"))
        XCTAssertTrue(interrupted.matchesSearchQuery("4 turns"))
        XCTAssertTrue(pending.matchesSearchQuery("needs confirm"))
        XCTAssertTrue(pending.matchesSearchQuery("previous sync unconfirmed"))
        XCTAssertTrue(handed.matchesSearchQuery("synced"))
        XCTAssertTrue(running.matchesSearchQuery("session active"))

        let snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-16T08:00:00Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: interrupted,
            recent: [interrupted, singleTurnReady]
        )
        let receipt = HandoffReceiptPayload(
            workspace: "/repos/beta",
            sourceHost: "antigravity",
            sourceId: "cid-int",
            targetHost: "codex",
            targetId: "tid-99",
            title: "Fix parser",
            status: "ready"
        )
        let cli = MockCLIRunner(
            feedResult: .success(snapshot),
            handoffResult: .success(receipt),
            reconcileResult: .success(ReconcileSummaryPayload(workspace: "/repos/beta"))
        )
        let vm = HandoffViewModel(cli: cli, activator: MockActivator(), preferencesStore: prefs)

        await vm.refresh()
        await vm.performPrimaryAction()
        XCTAssertEqual(vm.sections.map(\.title), ["继续当前工作", "最近对话"])
        XCTAssertEqual(vm.banner?.text, "已在 Codex 打开对话 · Fix parser")

        prefs.setLanguage(.en)
        XCTAssertEqual(vm.sections.map(\.title), ["Continue Current Work", "Recent Sessions"])
        XCTAssertEqual(vm.banner?.text, "Opened session in Codex · Fix parser")

        cli.setFeedResult(.failure(CLIClientError.cliMissing(searchedLocations: ["/usr/bin/codex-agy-sync"])))
        await vm.refresh()
        XCTAssertEqual(vm.banner?.text, AppCopy.text(.cliMissingError, in: .en))

        prefs.setLanguage(.zhHans)
        XCTAssertEqual(vm.banner?.text, AppCopy.text(.cliMissingError, in: .zhHans))

        let timeoutErr = CLIClientError.timedOut(command: ["menubar-feed"], seconds: 15)
        XCTAssertTrue(timeoutErr.localizedMessage(in: .zhHans).contains("15 秒"))
        XCTAssertTrue(timeoutErr.localizedMessage(in: .en).contains("15s"))
    }

    func testRunProcessAsyncDrainGraceCompletesPromptlyWithGrandchildHoldingStdout() async throws {
        let start = Date()
        let out = try await SubprocessCLIClient.runProcessAsync(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", "echo prompt-line; (sleep 2) &"],
            environment: [:],
            timeoutSeconds: 10.0,
            drainGraceSeconds: 0.25
        )
        let elapsed = Date().timeIntervalSince(start)
        XCTAssertEqual(out.exitCode, 0)
        XCTAssertEqual(
            String(data: out.stdout, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
            "prompt-line"
        )
        XCTAssertLessThan(elapsed, 1.5)
    }

    @MainActor
    func testLocalizedFailureUpdateServiceErrorAndUpdaterMutationGuard() async {
        let httpFailure = LocalizedFailure(error: UpdateServiceError.httpError(statusCode: 503))
        XCTAssertTrue(httpFailure.text(in: .zhHans).contains("503"))
        XCTAssertTrue(httpFailure.text(in: .en).contains("Remote release request failed (HTTP 503)"))

        let installFailure = LocalizedFailure(error: UpdateServiceError.updateInstallationInProgress)
        XCTAssertEqual(installFailure.text(in: .zhHans), AppCopy.text(.updatesErrInstallationInProgress, in: .zhHans))
        XCTAssertEqual(installFailure.text(in: .en), AppCopy.text(.updatesErrInstallationInProgress, in: .en))

        let suiteName = "dev.semanticcraft.codex-agy-sync.guard.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { cleanupIsolatedTestDefaults(defaults, suiteName: suiteName) }

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        let hero = makeItem(sessionId: "cid-guard", state: .completedReady)
        let pending = makeItem(sessionId: "cid-pend", state: .pendingReconcile)
        let snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-16T08:00:00Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: hero,
            recent: [hero, pending]
        )
        let receipt = HandoffReceiptPayload(
            workspace: hero.workspacePath,
            sourceHost: hero.host,
            sourceId: hero.sessionId,
            targetHost: hero.targetHost,
            targetId: "tid-ok",
            title: hero.title,
            status: "ready"
        )
        let cli = MockCLIRunner(
            feedResult: .success(snapshot),
            handoffResult: .success(receipt),
            reconcileResult: .success(ReconcileSummaryPayload(workspace: pending.workspacePath))
        )
        @MainActor
        final class GuardState {
            var updaterInstalling = true
        }
        let guardState = GuardState()
        let vm = HandoffViewModel(
            cli: cli,
            activator: MockActivator(),
            preferencesStore: prefs,
            canMutateEnvironment: { !guardState.updaterInstalling }
        )

        await vm.refresh()
        await vm.handoff(hero)
        XCTAssertTrue(cli.handoffCalls.isEmpty)
        XCTAssertEqual(
            vm.banner,
            StatusBanner(
                kind: .error,
                text: AppCopy.handoffFailedBanner(
                    AppCopy.text(.updatesErrInstallationInProgress, in: .zhHans),
                    in: .zhHans
                )
            )
        )

        prefs.setLanguage(.en)
        XCTAssertEqual(
            vm.banner,
            StatusBanner(
                kind: .error,
                text: AppCopy.handoffFailedBanner(
                    AppCopy.text(.updatesErrInstallationInProgress, in: .en),
                    in: .en
                )
            )
        )

        await vm.reconcile(workspacePath: pending.workspacePath, itemId: pending.id)
        XCTAssertTrue(cli.reconcileCalls.isEmpty)
        XCTAssertEqual(
            vm.banner,
            StatusBanner(
                kind: .error,
                text: AppCopy.reconcileFailedBanner(
                    AppCopy.text(.updatesErrInstallationInProgress, in: .en),
                    in: .en
                )
            )
        )

        guardState.updaterInstalling = false
        await vm.handoff(hero)
        XCTAssertEqual(cli.handoffCalls.count, 1)

        let updateVM = UpdateViewModel(preferencesStore: prefs, appVersion: "0.4.1")
        XCTAssertFalse(updateVM.isInstallingUpdate)
    }
}
