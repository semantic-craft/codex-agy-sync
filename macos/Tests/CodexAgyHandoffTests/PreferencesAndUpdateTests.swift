import AppKit
import Carbon.HIToolbox
import Combine
import Foundation
import XCTest
@testable import CodexAgyHandoffCore

private final class FailingLoginItemManager: LoginItemManaging, @unchecked Sendable {
    struct MockError: LocalizedError {
        var errorDescription: String? { "SMAppService mock denied" }
    }

    private let lock = NSLock()
    private var state = false
    var shouldFail = false

    var isEnabled: Bool {
        lock.withLock { state }
    }

    func setEnabled(_ enabled: Bool) throws {
        let fail = lock.withLock { shouldFail }
        if fail {
            throw MockError()
        }
        lock.withLock {
            state = enabled
        }
    }
}

private final class StubCommandRunner: SystemCommandRunning, @unchecked Sendable {
    struct Invocation: Equatable, Sendable {
        let executablePath: String
        let arguments: [String]
    }

    private let lock = NSLock()
    private(set) var invocations: [Invocation] = []
    private let handler: @Sendable (URL, [String]) throws -> CommandExecutionResult

    init(handler: @escaping @Sendable (URL, [String]) throws -> CommandExecutionResult) {
        self.handler = handler
    }

    func run(
        executableURL: URL,
        arguments: [String],
        environment: [String: String],
        timeoutSeconds: TimeInterval
    ) async throws -> CommandExecutionResult {
        lock.withLock {
            invocations.append(Invocation(executablePath: executableURL.path, arguments: arguments))
        }
        return try handler(executableURL, arguments)
    }
}

private final class StubHTTPFetcher: ReleaseHTTPFetching, @unchecked Sendable {
    private let lock = NSLock()
    private var routes: [URL: (Data, Int)]
    private(set) var requestedURLs: [URL] = []

    init(routes: [URL: (Data, Int)]) {
        self.routes = routes
    }

    func setRoutes(_ newRoutes: [URL: (Data, Int)]) {
        lock.withLock {
            routes = newRoutes
        }
    }

    func fetchData(
        from url: URL,
        headers: [String: String],
        timeoutSeconds: TimeInterval
    ) async throws -> (data: Data, statusCode: Int) {
        let match = lock.withLock { () -> (Data, Int)? in
            requestedURLs.append(url)
            return routes[url]
        }
        guard let match else {
            return (Data("not found".utf8), 404)
        }
        return match
    }
}

private final class StubRelaunchExecutor: RelaunchExecuting, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var relaunchedURLs: [URL] = []
    private let onInvoke: (@MainActor () -> Void)?

    init(onInvoke: (@MainActor () -> Void)? = nil) {
        self.onInvoke = onInvoke
    }

    @MainActor
    func relaunch(bundleURL: URL) throws {
        lock.withLock {
            relaunchedURLs.append(bundleURL)
        }
        onInvoke?()
    }
}

private final class SimpleFeedCLIRunner: CLIRunnerProtocol, @unchecked Sendable {
    private let lock = NSLock()
    var snapshot: MenubarFeedSnapshot
    var receipt: HandoffReceiptPayload
    private(set) var recordedLimits: [Int] = []

    init(snapshot: MenubarFeedSnapshot, receipt: HandoffReceiptPayload) {
        self.snapshot = snapshot
        self.receipt = receipt
    }

    func fetchFeed(limit: Int) async throws -> MenubarFeedSnapshot {
        lock.withLock {
            recordedLimits.append(limit)
            return snapshot
        }
    }

    func executeHandoff(workspacePath: String, sourceId: String) async throws -> HandoffReceiptPayload {
        lock.withLock { receipt }
    }

    func reconcilePending(workspacePath: String?) async throws -> ReconcileSummaryPayload {
        ReconcileSummaryPayload(workspace: workspacePath ?? "")
    }
}

private final class RecordingActivator: AppActivating, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var hosts: [String] = []

    func activateTarget(host: String) async -> Bool {
        lock.withLock {
            hosts.append(host)
            return true
        }
    }
}

final class PreferencesAndUpdateTests: XCTestCase {
    private func makeIsolatedDefaults() -> (UserDefaults, String) {
        let suiteName = "dev.semanticcraft.codex-agy-handoff.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defaults.removePersistentDomain(forName: suiteName)
        return (defaults, suiteName)
    }

    @MainActor
    func testPreferencesPersistenceMigrationAndLoginItemToggle() throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        defaults.set("/custom/legacy/codex-agy-sync", forKey: SubprocessCLIClient.cliExecutableDefaultsKey)

        let loginMock = FailingLoginItemManager()
        let store = PreferencesStore(defaults: defaults, loginItemManager: loginMock)

        XCTAssertEqual(store.preferences.customCLIPath, "/custom/legacy/codex-agy-sync")
        XCTAssertTrue(store.preferences.showAttentionDotOnStatusIcon)
        XCTAssertEqual(store.preferences.hotKey, .defaultShortcut)
        XCTAssertEqual(store.preferences.hotKey.keyCode, 4)
        XCTAssertEqual(store.preferences.hotKey.carbonModifiers, 6144)
        XCTAssertEqual(store.preferences.hotKeyBehavior, .instantPrimaryRescue)
        XCTAssertTrue(store.preferences.activateTargetAppAfterHandoff)
        XCTAssertEqual(store.preferences.recentFeedLimit, 6)

        store.setShowAttentionDot(false)
        store.setHotKeyBehavior(.togglePanelOnly)
        store.setActivateTargetAppAfterHandoff(false)
        store.setRecentFeedLimit(8)
        store.setLocalRepoPathForSync("/repos/codex-agy-sync")

        let customShortcut = try XCTUnwrap(
            HotKeyShortcut.from(
                keyCode: UInt16(kVK_ANSI_K),
                modifierFlags: [.control, .shift],
                charactersIgnoringModifiers: "k"
            )
        )
        XCTAssertEqual(customShortcut.displayString, "⌃⇧K")
        store.setHotKey(customShortcut)

        let reloaded = PreferencesStore(defaults: defaults, loginItemManager: loginMock)
        XCTAssertFalse(reloaded.preferences.showAttentionDotOnStatusIcon)
        XCTAssertEqual(reloaded.preferences.hotKey, customShortcut)
        XCTAssertEqual(reloaded.preferences.hotKeyBehavior, .togglePanelOnly)
        XCTAssertFalse(reloaded.preferences.activateTargetAppAfterHandoff)
        XCTAssertEqual(reloaded.preferences.recentFeedLimit, 8)
        XCTAssertEqual(reloaded.preferences.localRepoPathForSync, "/repos/codex-agy-sync")

        reloaded.setShowAttentionDot(true)
        XCTAssertTrue(reloaded.preferences.showAttentionDotOnStatusIcon)

        reloaded.resetCustomCLIPath()
        XCTAssertNil(reloaded.preferences.customCLIPath)
        XCTAssertNil(defaults.string(forKey: SubprocessCLIClient.cliExecutableDefaultsKey))

        reloaded.setLaunchAtLogin(true)
        XCTAssertTrue(reloaded.launchAtLogin)
        XCTAssertNil(reloaded.loginItemError)

        loginMock.shouldFail = true
        reloaded.setLaunchAtLogin(false)
        XCTAssertTrue(reloaded.launchAtLogin)
        XCTAssertEqual(reloaded.loginItemError, "SMAppService mock denied")

        XCTAssertNil(
            HotKeyShortcut.from(
                keyCode: UInt16(kVK_ANSI_H),
                modifierFlags: [.shift],
                charactersIgnoringModifiers: "h"
            )
        )
    }

    func testMenuBarIconRendererDrawsForwardProgressIconAndAttentionDot() throws {
        let img = MenuBarIconRenderer.image(showAttentionDot: false)
        XCTAssertTrue(img.isTemplate)
        XCTAssertEqual(img.size, NSSize(width: 18, height: 18))
        XCTAssertEqual(img.accessibilityDescription, "对话接力")

        let baseRep = try XCTUnwrap(
            MenuBarIconRenderer.renderBitmap(showAttentionDot: false, pixelSize: 36)
        )
        let dotRep = try XCTUnwrap(
            MenuBarIconRenderer.renderBitmap(showAttentionDot: true, pixelSize: 36)
        )

        var opaqueCount = 0
        for y in 0..<36 {
            for x in 0..<36 {
                let alpha = UInt8(((baseRep.colorAt(x: x, y: y)?.alphaComponent ?? 0) * 255.0).rounded())
                if alpha > 24 {
                    opaqueCount += 1
                }
            }
        }

        XCTAssertGreaterThan(opaqueCount, 60, "Forward progress icon rendered too few pixels")

        // NSBitmapImageRep.colorAt(x:y:) uses top-down Y (0 = top, 35 = bottom).
        // Unflipped point (14.6, 3.4) maps to pixel x ≈ 29, y ≈ 36 - 7 = 29.
        let dotCenterAlpha = dotRep.colorAt(x: 29, y: 29)?.alphaComponent ?? 0
        XCTAssertGreaterThan(dotCenterAlpha, 0.85, "Attention dot missing at bottom-right")

        // Halo at radius ~3.0pt from (14.6, 3.4), e.g. point (11.7, 3.4) -> pixel x = 23, y = 29.
        let haloAlpha = dotRep.colorAt(x: 23, y: 29)?.alphaComponent ?? 1.0
        XCTAssertLessThan(haloAlpha, 0.15, "Clear halo not cut around dot")
    }

    @MainActor
    func testHandoffViewModelRespectsDynamicFeedLimitActivationPreferenceAndAttentionBadge() async {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        prefs.setRecentFeedLimit(4)
        prefs.setActivateTargetAppAfterHandoff(false)

        let stoppedHero = MenubarSessionItem(
            host: "antigravity",
            targetHost: "codex",
            direction: "antigravity_to_codex",
            sessionId: "cid-1",
            title: "Interrupted session",
            workspacePath: "/repos/one",
            workspaceName: "one",
            paired: true,
            state: .interruptedStopped,
            updatedAtIso: "2026-11-15T10:00:00Z",
            sortEpoch: 100,
            turnCount: 2,
            hasInflightTurn: true
        )
        let snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T10:00:00Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: stoppedHero,
            recent: [stoppedHero]
        )
        let receipt = HandoffReceiptPayload(
            workspace: "/repos/one",
            sourceHost: "antigravity",
            sourceId: "cid-1",
            targetHost: "codex",
            targetId: "tid-99",
            title: "Interrupted session",
            status: "ready"
        )

        let cli = SimpleFeedCLIRunner(snapshot: snapshot, receipt: receipt)
        let activator = RecordingActivator()
        var closeCount = 0
        let vm = HandoffViewModel(
            cli: cli,
            activator: activator,
            preferencesStore: prefs,
            onDidHandoffAndActivate: { closeCount += 1 }
        )

        await vm.refresh()
        XCTAssertEqual(cli.recordedLimits, [4])
        XCTAssertTrue(vm.needsAttentionBadge)

        await vm.handoff(stoppedHero)
        XCTAssertTrue(activator.hosts.isEmpty)
        XCTAssertEqual(closeCount, 0)
        if case .success(let text)? = vm.banner {
            XCTAssertTrue(text.contains("Codex"))
        } else {
            XCTFail("Expected success banner")
        }

        prefs.setRecentFeedLimit(8)
        prefs.setActivateTargetAppAfterHandoff(true)
        let readyHero = MenubarSessionItem(
            host: "antigravity",
            targetHost: "codex",
            direction: "antigravity_to_codex",
            sessionId: "cid-2",
            title: "Completed session",
            workspacePath: "/repos/one",
            workspaceName: "one",
            paired: true,
            state: .completedReady,
            updatedAtIso: "2026-11-15T10:01:00Z",
            sortEpoch: 101,
            turnCount: 3,
            hasInflightTurn: false
        )
        cli.snapshot = MenubarFeedSnapshot(
            generatedAtIso: "2026-11-15T10:01:00Z",
            agyDaemonActive: true,
            codexDaemonActive: true,
            hero: readyHero,
            recent: [readyHero]
        )
        await vm.refresh()
        XCTAssertEqual(cli.recordedLimits.last, 8)
        XCTAssertFalse(vm.needsAttentionBadge)

        await vm.handoff(readyHero)
        XCTAssertEqual(activator.hosts, ["codex"])
        XCTAssertEqual(closeCount, 1)
    }

    func testSemanticVersionAndPathGuardValidation() throws {
        let v030 = try XCTUnwrap(SemanticVersion.parse("codex-agy-sync 0.3.0\n"))
        let v031 = try XCTUnwrap(SemanticVersion.parse("v0.3.1"))
        let v031rc = try XCTUnwrap(SemanticVersion.parse("0.3.1-rc.1"))
        XCTAssertLessThan(v030, v031rc)
        XCTAssertLessThan(v031rc, v031)
        XCTAssertEqual(v030.description, "0.3.0")

        XCTAssertThrowsError(try UpdatePathGuard.validateNonTemporaryURL(URL(fileURLWithPath: "/tmp/stage")))
        XCTAssertThrowsError(try UpdatePathGuard.validateNonTemporaryURL(URL(fileURLWithPath: "/private/tmp/stage")))
        XCTAssertNoThrow(try UpdatePathGuard.validateNonTemporaryURL(FileManager.default.temporaryDirectory))
    }

    @MainActor
    func testUpdateViewModelAlignmentReleaseCheckAndChecksumVerification() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-update-test-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: tempRoot, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        let fakeCLI = tempRoot.appendingPathComponent("codex-agy-sync")
        try "#!/usr/bin/env python3\n".write(to: fakeCLI, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: fakeCLI.path)

        let cliVersionBox = NSLock()
        nonisolated(unsafe) var reportedCLIVersion = "0.2.9"

        let wheelBytes = Data("mock-wheel-binary-v0.4.0".utf8)
        let zipBytes = Data("mock-macos-zip-v0.4.0".utf8)
        let wheelSHA = SHA256Verifier.sha256Hex(of: wheelBytes)
        let zipSHA = SHA256Verifier.sha256Hex(of: zipBytes)

        let apiURL = URL(string: "http://127.0.0.1:19999/releases/latest")!
        let sumsURL = URL(string: "http://127.0.0.1:19999/download/SHA256SUMS")!
        let wheelURL = URL(string: "http://127.0.0.1:19999/download/codex_agy_sync-0.4.0-py3-none-any.whl")!
        let zipURL = URL(string: "http://127.0.0.1:19999/download/CodexAgyHandoff-macos.zip")!

        let releaseJSON = """
        {
          "tag_name": "v0.4.0",
          "html_url": "https://github.com/semantic-craft/codex-agy-sync/releases/tag/v0.4.0",
          "body": "## Highlights\\n- Unified Settings window and dual sync update",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "\(sumsURL.absoluteString)", "size": 128},
            {"name": "codex_agy_sync-0.4.0-py3-none-any.whl", "browser_download_url": "\(wheelURL.absoluteString)", "size": \(wheelBytes.count)},
            {"name": "CodexAgyHandoff-macos.zip", "browser_download_url": "\(zipURL.absoluteString)", "size": \(zipBytes.count)}
          ]
        }
        """

        let validSums = """
        \(wheelSHA)  codex_agy_sync-0.4.0-py3-none-any.whl
        \(zipSHA)  CodexAgyHandoff-macos.zip
        """

        let runner = StubCommandRunner { exe, args in
            if args == ["--version"] {
                let ver = cliVersionBox.withLock { reportedCLIVersion }
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync \(ver)\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = #"{"executable":"/usr/bin/python3","in_venv":true,"editable":false,"repo_path":null}"#
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            if args.prefix(3) == ["-m", "pip", "install"] {
                cliVersionBox.withLock { reportedCLIVersion = "0.4.0" }
                return CommandExecutionResult(exitCode: 0, stdout: Data("installed\n".utf8), stderr: Data())
            }
            if exe.path == "/usr/bin/ditto", let destPath = args.last {
                let appDir = URL(fileURLWithPath: destPath).appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
                try FileManager.default.createDirectory(at: appDir, withIntermediateDirectories: true)
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            if exe.path == "/usr/bin/codesign" {
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }

        let badSums = """
        \(String(repeating: "0", count: 64))  codex_agy_sync-0.4.0-py3-none-any.whl
        \(zipSHA)  CodexAgyHandoff-macos.zip
        """
        let badFetcher = StubHTTPFetcher(routes: [
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data(badSums.utf8), 200),
            wheelURL: (wheelBytes, 200),
            zipURL: (zipBytes, 200),
        ])

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        let env = [
            "CODEX_AGY_SYNC_BIN": fakeCLI.path,
            UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            UpdateViewModel.skipSelfReplaceEnvKey: "1",
        ]

        let vmBad = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.3.0",
            environment: env,
            commandRunner: runner,
            httpFetcher: badFetcher,
            stagingRootOverride: tempRoot.appendingPathComponent("staging", isDirectory: true)
        )

        await vmBad.probeEnvironment()
        XCTAssertEqual(vmBad.cliSource, .envOverride)
        XCTAssertEqual(vmBad.alignment, .mismatched(appVersion: "0.3.0", cliVersion: "0.2.9"))

        await vmBad.checkForUpdates()
        XCTAssertEqual(
            vmBad.availability,
            .releaseAvailable(
                latestTag: "v0.4.0",
                notesSummary: "Highlights",
                hasAppAsset: true,
                hasWheelAsset: true
            )
        )

        await vmBad.syncUpdateNow()
        if case .failed(let message) = vmBad.availability {
            XCTAssertTrue(message.contains("SHA-256"), "Unexpected error: \(message)")
        } else {
            XCTFail("Expected checksum mismatch failure, got \(vmBad.availability)")
        }
        XCTAssertFalse(runner.invocations.contains { $0.arguments.prefix(2) == ["-m", "pip"] })

        let goodFetcher = StubHTTPFetcher(routes: [
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data(validSums.utf8), 200),
            wheelURL: (wheelBytes, 200),
            zipURL: (zipBytes, 200),
        ])
        let vmGood = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.0",
            environment: env,
            commandRunner: runner,
            httpFetcher: goodFetcher,
            stagingRootOverride: tempRoot.appendingPathComponent("staging-ok", isDirectory: true)
        )

        await vmGood.checkForUpdates()
        await vmGood.syncUpdateNow()
        XCTAssertEqual(vmGood.availability, .upToDate(currentVersion: "0.4.0"))
        XCTAssertEqual(vmGood.alignment, .aligned(version: "0.4.0"))
        XCTAssertTrue(vmGood.canRelaunchApp)
    }

    @MainActor
    func testUpdateViewModelLocalRepoSync() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-localrepo-\(UUID().uuidString)", isDirectory: true)
        let repoDir = tempRoot.appendingPathComponent("repo", isDirectory: true)
        let scriptsDir = repoDir.appendingPathComponent("scripts", isDirectory: true)
        let binDir = repoDir.appendingPathComponent("bin", isDirectory: true)
        try FileManager.default.createDirectory(at: scriptsDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: binDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        try "[project]\nname = \"codex-agy-sync\"\nversion = \"0.3.0\"\n"
            .write(to: repoDir.appendingPathComponent("pyproject.toml"), atomically: true, encoding: .utf8)
        let buildScript = scriptsDir.appendingPathComponent("build_menubar_app.sh")
        try "#!/bin/sh\nexit 0\n".write(to: buildScript, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: buildScript.path)

        let cliBin = binDir.appendingPathComponent("codex-agy-sync")
        try "#!/usr/bin/env python3\n".write(to: cliBin, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cliBin.path)

        let runner = StubCommandRunner { exe, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.3.0\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = #"{"executable":"/usr/bin/python3","in_venv":true,"editable":true,"repo_path":null}"#
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            if exe.lastPathComponent == "build_menubar_app.sh",
               let outIdx = args.firstIndex(of: "--output-dir"),
               outIdx + 1 < args.count
            {
                let outApp = URL(fileURLWithPath: args[outIdx + 1])
                    .appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
                try FileManager.default.createDirectory(at: outApp, withIntermediateDirectories: true)
                return CommandExecutionResult(exitCode: 0, stdout: Data(outApp.path.utf8), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        let vm = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.3.0",
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.skipSelfReplaceEnvKey: "1",
            ],
            commandRunner: runner,
            httpFetcher: StubHTTPFetcher(routes: [:]),
            stagingRootOverride: tempRoot.appendingPathComponent("staging", isDirectory: true)
        )

        await vm.probeEnvironment()
        XCTAssertEqual(vm.alignment, .aligned(version: "0.3.0"))
        XCTAssertEqual(vm.detectedRepoPath, repoDir.resolvingSymlinksInPath().path)
        XCTAssertEqual(vm.detectedRepoVersion, "0.3.0")

        await vm.syncFromLocalRepo()
        XCTAssertEqual(vm.availability, .upToDate(currentVersion: "0.3.0"))
        XCTAssertTrue(vm.canRelaunchApp)
        XCTAssertTrue(runner.invocations.contains { $0.arguments.contains("-e") })
        XCTAssertTrue(runner.invocations.contains { $0.executablePath.hasSuffix("build_menubar_app.sh") })
    }

    func testAssetPathTraversalSanitizationStalePruningAndReleaseAssetFiltering() throws {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-pathguard-\(UUID().uuidString)", isDirectory: true)
        let stagingDir = tempRoot.appendingPathComponent("session", isDirectory: true)
        try FileManager.default.createDirectory(at: stagingDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        let sanitized = try UpdatePathGuard.stagedAssetFileURL(for: "../../evil.whl", in: stagingDir)
        XCTAssertEqual(sanitized.lastPathComponent, "evil.whl")
        XCTAssertEqual(sanitized.deletingLastPathComponent(), stagingDir.standardizedFileURL)

        for invalid in ["", "   ", ".", "..", "/", "bad\0name.whl"] {
            XCTAssertThrowsError(try UpdatePathGuard.stagedAssetFileURL(for: invalid, in: stagingDir)) { error in
                guard case UpdateServiceError.invalidAssetName = error else {
                    return XCTFail("Expected invalidAssetName for \(invalid.debugDescription), got \(error)")
                }
            }
        }

        let updatesRoot = tempRoot.appendingPathComponent("updates", isDirectory: true)
        let staleChild = updatesRoot.appendingPathComponent("stale-dir", isDirectory: true)
        let freshChild = updatesRoot.appendingPathComponent("fresh-dir", isDirectory: true)
        try FileManager.default.createDirectory(at: staleChild, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: freshChild, withIntermediateDirectories: true)
        let oldDate = Date().addingTimeInterval(-90_000)
        try FileManager.default.setAttributes([.modificationDate: oldDate], ofItemAtPath: staleChild.path)

        UpdatePathGuard.pruneStaleStagingDirectories(in: updatesRoot, maxAgeSeconds: 86_400, now: Date())
        XCTAssertFalse(FileManager.default.fileExists(atPath: staleChild.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: freshChild.path))

        let releaseJSON = """
        {
          "tag_name": "v0.4.2",
          "html_url": "https://github.com/semantic-craft/codex-agy-sync/releases/tag/v0.4.2",
          "body": "Hardened release decoding",
          "assets": [
            {"name": "SHA256SUMS.asc", "browser_download_url": "https://example.com/SHA256SUMS.asc", "size": 64},
            {"name": "CodexAgyHandoff-macos.zip.sha256", "browser_download_url": "https://example.com/app.sha256", "size": 96},
            {"name": "other_pkg-0.4.2-py3-none-any.whl", "browser_download_url": "https://example.com/other.whl", "size": 64},
            {"name": "codex_agy_sync-0.4.2-py3-none-any.whl.sig", "browser_download_url": "https://example.com/wheel.sig", "size": 64},
            {"name": "codex_agy_sync-0.4.2-py3-none-any.whl", "browser_download_url": "https://example.com/wheel.whl", "size": 128}
          ]
        }
        """
        let decoded = try GitHubReleaseInfo.decode(from: Data(releaseJSON.utf8))
        XCTAssertEqual(decoded.checksumsAsset?.name, "CodexAgyHandoff-macos.zip.sha256")
        XCTAssertEqual(decoded.wheelAsset?.name, "codex_agy_sync-0.4.2-py3-none-any.whl")
    }

    @MainActor
    func testTmpRepoPathRejectedAcrossDiscoveryValidationAndSync() async {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        XCTAssertFalse(UpdateViewModel.isValidSourceRepo(URL(fileURLWithPath: "/tmp/codex-agy-sync")))
        XCTAssertFalse(UpdateViewModel.isValidSourceRepo(URL(fileURLWithPath: "/private/tmp/codex-agy-sync")))
        XCTAssertNil(UpdateViewModel.parsePyprojectVersion(in: URL(fileURLWithPath: "/tmp/codex-agy-sync")))

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        prefs.setLocalRepoPathForSync("/tmp/codex-agy-sync")

        let runner = StubCommandRunner { _, _ in
            CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }
        let vm = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.3.0",
            environment: [
                "CODEX_AGY_SYNC_BIN": "/nonexistent/codex-agy-sync",
                "PATH": "/nonexistent",
            ],
            commandRunner: runner,
            httpFetcher: StubHTTPFetcher(routes: [:])
        )

        await vm.probeEnvironment()
        XCTAssertNil(vm.effectiveRepoPath)
        XCTAssertNil(vm.detectedRepoPath)

        await vm.syncFromLocalRepo()
        if case .failed(let msg) = vm.availability {
            XCTAssertTrue(msg.contains("/tmp"), "Expected /tmp rejection, got \(msg)")
        } else {
            XCTFail("Expected /tmp local repo sync failure, got \(vm.availability)")
        }
        XCTAssertTrue(runner.invocations.isEmpty)
    }

    @MainActor
    func testTargetBundleWritabilityCheckedBeforePipMutation() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-writable-\(UUID().uuidString)", isDirectory: true)
        let repoDir = tempRoot.appendingPathComponent("repo", isDirectory: true)
        let scriptsDir = repoDir.appendingPathComponent("scripts", isDirectory: true)
        let cliBin = tempRoot.appendingPathComponent("codex-agy-sync")
        let readOnlyParent = tempRoot.appendingPathComponent("readonly-apps", isDirectory: true)
        let appInReadOnly = readOnlyParent.appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)

        try FileManager.default.createDirectory(at: scriptsDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: appInReadOnly, withIntermediateDirectories: true)
        defer {
            try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: readOnlyParent.path)
            try? FileManager.default.removeItem(at: tempRoot)
        }

        try "[project]\nname = \"codex-agy-sync\"\nversion = \"0.3.1\"\n"
            .write(to: repoDir.appendingPathComponent("pyproject.toml"), atomically: true, encoding: .utf8)
        let buildScript = scriptsDir.appendingPathComponent("build_menubar_app.sh")
        try "#!/bin/sh\nexit 0\n".write(to: buildScript, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: buildScript.path)
        try "#!/usr/bin/env python3\n".write(to: cliBin, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cliBin.path)

        let runner = StubCommandRunner { _, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.3.0\n".utf8), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        prefs.setLocalRepoPathForSync(repoDir.path)

        let vmNilBundle = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.3.0",
            currentAppBundleURL: nil,
            environment: ["CODEX_AGY_SYNC_BIN": cliBin.path],
            commandRunner: runner,
            httpFetcher: StubHTTPFetcher(routes: [:]),
            stagingRootOverride: tempRoot.appendingPathComponent("staging-nil", isDirectory: true)
        )
        await vmNilBundle.syncFromLocalRepo()
        if case .failed(let msg) = vmNilBundle.availability {
            XCTAssertTrue(msg.contains("无法写入应用安装目录"), "Unexpected error: \(msg)")
        } else {
            XCTFail("Expected targetBundleNotWritable when currentAppBundleURL is nil")
        }
        XCTAssertFalse(vmNilBundle.canRelaunchApp)
        XCTAssertFalse(runner.invocations.contains { $0.arguments.prefix(2) == ["-m", "pip"] })

        if geteuid() != 0 {
            try FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: readOnlyParent.path)
            let vmReadOnly = UpdateViewModel(
                preferencesStore: prefs,
                appVersion: "0.3.0",
                currentAppBundleURL: appInReadOnly,
                environment: ["CODEX_AGY_SYNC_BIN": cliBin.path],
                commandRunner: runner,
                httpFetcher: StubHTTPFetcher(routes: [:]),
                stagingRootOverride: tempRoot.appendingPathComponent("staging-ro", isDirectory: true)
            )
            await vmReadOnly.syncFromLocalRepo()
            if case .failed(let msg) = vmReadOnly.availability {
                XCTAssertTrue(msg.contains("无法写入应用安装目录"), "Unexpected error: \(msg)")
            } else {
                XCTFail("Expected targetBundleNotWritable for chmod 0555 parent dir")
            }
            XCTAssertFalse(vmReadOnly.canRelaunchApp)
            XCTAssertFalse(runner.invocations.contains { $0.arguments.prefix(2) == ["-m", "pip"] })
        }
    }

    @MainActor
    func testFailedCodesignAbortsBeforePipInstallInReleaseAndLocalSync() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-codesign-\(UUID().uuidString)", isDirectory: true)
        let repoDir = tempRoot.appendingPathComponent("repo", isDirectory: true)
        let scriptsDir = repoDir.appendingPathComponent("scripts", isDirectory: true)
        let cliBin = tempRoot.appendingPathComponent("codex-agy-sync")
        try FileManager.default.createDirectory(at: scriptsDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        try "[project]\nname = \"codex-agy-sync\"\nversion = \"0.4.0\"\n"
            .write(to: repoDir.appendingPathComponent("pyproject.toml"), atomically: true, encoding: .utf8)
        let buildScript = scriptsDir.appendingPathComponent("build_menubar_app.sh")
        try "#!/bin/sh\nexit 0\n".write(to: buildScript, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: buildScript.path)
        try "#!/usr/bin/env python3\n".write(to: cliBin, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cliBin.path)

        let wheelBytes = Data("wheel-v0.4.0".utf8)
        let zipBytes = Data("zip-v0.4.0".utf8)
        let sums = """
        \(SHA256Verifier.sha256Hex(of: wheelBytes))  codex_agy_sync-0.4.0-py3-none-any.whl
        \(SHA256Verifier.sha256Hex(of: zipBytes))  CodexAgyHandoff-macos.zip
        """
        let apiURL = URL(string: "http://127.0.0.1:29999/releases/latest")!
        let sumsURL = URL(string: "http://127.0.0.1:29999/SHA256SUMS")!
        let wheelURL = URL(string: "http://127.0.0.1:29999/codex_agy_sync-0.4.0-py3-none-any.whl")!
        let zipURL = URL(string: "http://127.0.0.1:29999/CodexAgyHandoff-macos.zip")!
        let releaseJSON = """
        {
          "tag_name": "v0.4.0",
          "body": "Codesign gate test",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "\(sumsURL.absoluteString)", "size": 128},
            {"name": "codex_agy_sync-0.4.0-py3-none-any.whl", "browser_download_url": "\(wheelURL.absoluteString)", "size": \(wheelBytes.count)},
            {"name": "CodexAgyHandoff-macos.zip", "browser_download_url": "\(zipURL.absoluteString)", "size": \(zipBytes.count)}
          ]
        }
        """

        let runner = StubCommandRunner { exe, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.3.0\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = #"{"executable":"/usr/bin/python3","in_venv":true,"editable":false,"repo_path":null,"supports_break_system_packages":false}"#
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            if exe.path == "/usr/bin/ditto", let destPath = args.last {
                let appDir = URL(fileURLWithPath: destPath).appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
                try FileManager.default.createDirectory(at: appDir, withIntermediateDirectories: true)
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            if exe.lastPathComponent == "build_menubar_app.sh",
               let outIdx = args.firstIndex(of: "--output-dir"),
               outIdx + 1 < args.count
            {
                let outApp = URL(fileURLWithPath: args[outIdx + 1]).appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
                try FileManager.default.createDirectory(at: outApp, withIntermediateDirectories: true)
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            if exe.path == "/usr/bin/codesign" {
                return CommandExecutionResult(exitCode: 1, stdout: Data(), stderr: Data("invalid signature".utf8))
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }

        let fetcher = StubHTTPFetcher(routes: [
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data(sums.utf8), 200),
            wheelURL: (wheelBytes, 200),
            zipURL: (zipBytes, 200),
        ])
        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        let env = [
            "CODEX_AGY_SYNC_BIN": cliBin.path,
            UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            UpdateViewModel.skipSelfReplaceEnvKey: "1",
        ]

        let vmRelease = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.3.0",
            environment: env,
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: tempRoot.appendingPathComponent("staging-rel", isDirectory: true)
        )
        await vmRelease.checkForUpdates()
        await vmRelease.syncUpdateNow()
        if case .failed(let msg) = vmRelease.availability {
            XCTAssertTrue(msg.contains("签名校验未通过"), "Unexpected release codesign error: \(msg)")
        } else {
            XCTFail("Expected codesign failure in release sync")
        }
        XCTAssertFalse(runner.invocations.contains { $0.arguments.prefix(2) == ["-m", "pip"] })

        prefs.setLocalRepoPathForSync(repoDir.path)
        let vmLocal = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.3.0",
            environment: env,
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: tempRoot.appendingPathComponent("staging-loc", isDirectory: true)
        )
        await vmLocal.syncFromLocalRepo()
        if case .failed(let msg) = vmLocal.availability {
            XCTAssertTrue(msg.contains("签名校验未通过"), "Unexpected local codesign error: \(msg)")
        } else {
            XCTFail("Expected codesign failure in local repo sync")
        }
        XCTAssertFalse(runner.invocations.contains { $0.arguments.prefix(2) == ["-m", "pip"] })
    }

    @MainActor
    func testPendingRelaunchVersionKeepsAlignedAndCliUnavailableNeverReportsUpToDate() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-relaunch-\(UUID().uuidString)", isDirectory: true)
        let cliBin = tempRoot.appendingPathComponent("codex-agy-sync")
        try FileManager.default.createDirectory(at: tempRoot, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }
        try "#!/usr/bin/env python3\n".write(to: cliBin, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cliBin.path)

        let verLock = NSLock()
        nonisolated(unsafe) var cliVer = "0.3.0"

        let wheelBytes = Data("wheel-040".utf8)
        let zipBytes = Data("zip-040".utf8)
        let sums = """
        \(SHA256Verifier.sha256Hex(of: wheelBytes))  codex_agy_sync-0.4.0-py3-none-any.whl
        \(SHA256Verifier.sha256Hex(of: zipBytes))  CodexAgyHandoff-macos.zip
        """
        let apiURL = URL(string: "http://127.0.0.1:30001/releases/latest")!
        let sumsURL = URL(string: "http://127.0.0.1:30001/SHA256SUMS")!
        let wheelURL = URL(string: "http://127.0.0.1:30001/codex_agy_sync-0.4.0-py3-none-any.whl")!
        let zipURL = URL(string: "http://127.0.0.1:30001/CodexAgyHandoff-macos.zip")!
        let releaseJSON = """
        {
          "tag_name": "v0.4.0",
          "body": "Upgrade test",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "\(sumsURL.absoluteString)", "size": 128},
            {"name": "codex_agy_sync-0.4.0-py3-none-any.whl", "browser_download_url": "\(wheelURL.absoluteString)", "size": \(wheelBytes.count)},
            {"name": "CodexAgyHandoff-macos.zip", "browser_download_url": "\(zipURL.absoluteString)", "size": \(zipBytes.count)}
          ]
        }
        """

        let runner = StubCommandRunner { exe, args in
            if args == ["--version"] {
                let v = verLock.withLock { cliVer }
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync \(v)\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = #"{"executable":"/usr/bin/python3","in_venv":true,"editable":false,"repo_path":null,"supports_break_system_packages":false}"#
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            if exe.path == "/usr/bin/ditto", let destPath = args.last {
                let appDir = URL(fileURLWithPath: destPath).appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
                try FileManager.default.createDirectory(at: appDir, withIntermediateDirectories: true)
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            if args.prefix(3) == ["-m", "pip", "install"] {
                verLock.withLock { cliVer = "0.4.0" }
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }
        let fetcher = StubHTTPFetcher(routes: [
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data(sums.utf8), 200),
            wheelURL: (wheelBytes, 200),
            zipURL: (zipBytes, 200),
        ])
        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())

        let vmUpgrade = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.3.0",
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
                UpdateViewModel.skipSelfReplaceEnvKey: "1",
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: tempRoot.appendingPathComponent("staging-up", isDirectory: true)
        )
        await vmUpgrade.checkForUpdates()
        await vmUpgrade.syncUpdateNow()
        XCTAssertEqual(vmUpgrade.pendingRelaunchVersion, "0.4.0")
        XCTAssertEqual(vmUpgrade.effectiveAppVersion, "0.4.0")
        XCTAssertEqual(vmUpgrade.alignment, .aligned(version: "0.4.0"))
        XCTAssertEqual(vmUpgrade.availability, .upToDate(currentVersion: "0.4.0"))

        let vmMissingCLI = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.0",
            environment: [
                "CODEX_AGY_SYNC_BIN": tempRoot.appendingPathComponent("missing-cli").path,
                "PATH": "/nonexistent",
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: tempRoot.appendingPathComponent("staging-miss", isDirectory: true)
        )
        await vmMissingCLI.checkForUpdates()
        guard case .cliUnavailable = vmMissingCLI.alignment else {
            return XCTFail("Expected .cliUnavailable alignment, got \(vmMissingCLI.alignment)")
        }
        if case .upToDate = vmMissingCLI.availability {
            XCTFail("checkForUpdates must never report .upToDate when CLI is unavailable")
        }
        if case .releaseAvailable(let tag, _, _, let hasWheel) = vmMissingCLI.availability {
            XCTAssertEqual(tag, "v0.4.0")
            XCTAssertTrue(hasWheel)
        } else {
            XCTFail("Expected .releaseAvailable to install missing CLI, got \(vmMissingCLI.availability)")
        }
    }

    @MainActor
    func testPipBreakSystemPackagesConditionalAndShebangPythonResolution() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-shebang-\(UUID().uuidString)", isDirectory: true)
        let spacedVenvBin = tempRoot.appendingPathComponent("pipx venvs/codex sync/bin", isDirectory: true)
        let envPathBin = tempRoot.appendingPathComponent("env-path-bin", isDirectory: true)
        try FileManager.default.createDirectory(at: spacedVenvBin, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: envPathBin, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        let spacedPython = spacedVenvBin.appendingPathComponent("python")
        try "#!/bin/sh\nexit 0\n".write(to: spacedPython, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: spacedPython.path)

        let pipxWrapper = tempRoot.appendingPathComponent("codex-agy-sync-pipx")
        let pipxScript = """
        #!/bin/sh
        '''exec' '\(spacedPython.path)' "$0" "$@"
        ' '''
        import sys
        """
        try pipxScript.write(to: pipxWrapper, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: pipxWrapper.path)

        let resolvedPipxPython = UpdateViewModel.findPythonExecutable(
            for: pipxWrapper,
            childEnv: ["PATH": "/usr/bin:/bin"]
        )
        XCTAssertEqual(resolvedPipxPython?.path, spacedPython.path)

        let customEnvPython3 = envPathBin.appendingPathComponent("python3")
        try "#!/bin/sh\nexit 0\n".write(to: customEnvPython3, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: customEnvPython3.path)

        let envShebangCLI = tempRoot.appendingPathComponent("codex-agy-sync-env")
        try "#!/usr/bin/env python3\nimport sys\n".write(to: envShebangCLI, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: envShebangCLI.path)

        let resolvedEnvPython = UpdateViewModel.findPythonExecutable(
            for: envShebangCLI,
            childEnv: ["PATH": "\(envPathBin.path):/usr/bin"]
        )
        XCTAssertEqual(resolvedEnvPython?.path, customEnvPython3.path)

        let wheelBytes = Data("wheel-bsp".utf8)
        let sums = "\(SHA256Verifier.sha256Hex(of: wheelBytes))  codex_agy_sync-0.4.0-py3-none-any.whl\n"
        let apiURL = URL(string: "http://127.0.0.1:30002/releases/latest")!
        let sumsURL = URL(string: "http://127.0.0.1:30002/SHA256SUMS")!
        let wheelURL = URL(string: "http://127.0.0.1:30002/codex_agy_sync-0.4.0-py3-none-any.whl")!
        let releaseJSON = """
        {
          "tag_name": "v0.4.0",
          "body": "Pip flag test",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "\(sumsURL.absoluteString)", "size": 64},
            {"name": "codex_agy_sync-0.4.0-py3-none-any.whl", "browser_download_url": "\(wheelURL.absoluteString)", "size": \(wheelBytes.count)}
          ]
        }
        """
        let fetcher = StubHTTPFetcher(routes: [
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data(sums.utf8), 200),
            wheelURL: (wheelBytes, 200),
        ])
        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())

        for supportsBSP in [false, true] {
            let runner = StubCommandRunner { _, args in
                if args == ["--version"] {
                    return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.3.0\n".utf8), stderr: Data())
                }
                if args.first == "-c" {
                    let pyOut = """
                    {"executable":"\(customEnvPython3.path)","in_venv":false,"editable":false,"repo_path":null,"supports_break_system_packages":\(supportsBSP)}
                    """
                    return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
                }
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            let vm = UpdateViewModel(
                preferencesStore: prefs,
                appVersion: "0.3.0",
                environment: [
                    "CODEX_AGY_SYNC_BIN": envShebangCLI.path,
                    "PATH": "\(envPathBin.path):/usr/bin",
                    UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
                    UpdateViewModel.skipSelfReplaceEnvKey: "1",
                ],
                commandRunner: runner,
                httpFetcher: fetcher,
                stagingRootOverride: tempRoot.appendingPathComponent("staging-bsp-\(supportsBSP)", isDirectory: true)
            )
            await vm.checkForUpdates()
            await vm.syncUpdateNow()
            let pipCall = try XCTUnwrap(runner.invocations.first { $0.arguments.prefix(3) == ["-m", "pip", "install"] })
            XCTAssertTrue(pipCall.arguments.contains("--user"))
            XCTAssertEqual(pipCall.arguments.contains("--break-system-packages"), supportsBSP)
        }
    }

    @MainActor
    func testAppLanguageResolutionDefaultsLegacyDecodeMenusAndIcon() throws {
        XCTAssertEqual(AppLanguage.zhHans.resolve(preferredLanguages: ["en-US"]), .zhHans)
        XCTAssertEqual(AppLanguage.en.resolve(preferredLanguages: ["zh-Hans-CN"]), .en)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["zh-Hans-CN"]), .zhHans)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["zh-CN"]), .zhHans)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["zh-SG"]), .zhHans)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["zh"]), .zhHans)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["zh-Hant-TW"]), .zhHans)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["en-US"]), .en)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["en-GB"]), .en)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["ja-JP"]), .en)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: []), .en)

        XCTAssertEqual(AppLanguage.system.displayName(in: .zhHans), "跟随系统")
        XCTAssertEqual(AppLanguage.system.displayName(in: .en), "Follow System")
        XCTAssertEqual(AppLanguage.zhHans.displayName(in: .en), "简体中文")
        XCTAssertEqual(AppLanguage.en.displayName(in: .zhHans), "English")

        let legacyJSON = """
        {
          "showAttentionDotOnStatusIcon": false,
          "hotKeyBehavior": "toggle_panel",
          "recentFeedLimit": 4
        }
        """
        let decodedLegacy = try JSONDecoder().decode(AppPreferences.self, from: Data(legacyJSON.utf8))
        XCTAssertEqual(decodedLegacy.language, .zhHans)
        XCTAssertEqual(decodedLegacy.autoUpdatePolicy, .checkOnly)
        XCTAssertFalse(decodedLegacy.showAttentionDotOnStatusIcon)
        XCTAssertEqual(decodedLegacy.hotKeyBehavior, .togglePanelOnly)
        XCTAssertEqual(decodedLegacy.recentFeedLimit, 4)

        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let store = PreferencesStore(
            defaults: defaults,
            loginItemManager: NoOpLoginItemManager(),
            preferredLanguagesProvider: { ["en-US"] }
        )
        XCTAssertEqual(store.preferences.language, .zhHans)
        XCTAssertEqual(store.preferences.autoUpdatePolicy, .checkOnly)
        XCTAssertEqual(store.effectiveLanguage, .zhHans)

        store.setLanguage(.system)
        XCTAssertEqual(store.effectiveLanguage, .en)
        store.setAutoUpdatePolicy(.autoStage)

        let reloaded = PreferencesStore(
            defaults: defaults,
            loginItemManager: NoOpLoginItemManager(),
            preferredLanguagesProvider: { ["en-US"] }
        )
        XCTAssertEqual(reloaded.preferences.language, .system)
        XCTAssertEqual(reloaded.preferences.autoUpdatePolicy, .autoStage)
        XCTAssertEqual(reloaded.effectiveLanguage, .en)

        XCTAssertEqual(HotKeyBehavior.instantPrimaryRescue.displayName(in: .zhHans), "展开并自动接力首个对话")
        XCTAssertEqual(HotKeyBehavior.instantPrimaryRescue.displayName(in: .en), "Open & Continue Top Session")
        XCTAssertFalse(HotKeyBehavior.togglePanelOnly.helperDescription(in: .en).isEmpty)
        XCTAssertFalse(HotKeyBehavior.instantPrimaryRescue.helperDescription(in: .zhHans).isEmpty)

        XCTAssertEqual(MenuBarIconRenderer.image(showAttentionDot: false, language: .en).accessibilityDescription, "Session Handoff")
        XCTAssertEqual(MenuBarIconRenderer.image(showAttentionDot: false, language: .zhHans).accessibilityDescription, "对话接力")

        let zhMenu = AppMenuBuilder.buildMainMenu(language: .zhHans, target: nil, settingsAction: Selector(("openSettings:")))
        let enMenu = AppMenuBuilder.buildMainMenu(language: .en, target: nil, settingsAction: Selector(("openSettings:")))
        XCTAssertEqual(zhMenu.items.first?.submenu?.items.first?.title, "设置…")
        XCTAssertEqual(enMenu.items.first?.submenu?.items.first?.title, "Settings…")
        XCTAssertEqual(enMenu.items.last?.submenu?.title, "Edit")
    }

    @MainActor
    func testSettingsWindowMatchCapturesByIdentifierRegardlessOfLocalizedTitle() {
        let settingsWin = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 300),
            styleMask: [.titled],
            backing: .buffered,
            defer: false
        )
        settingsWin.identifier = SettingsWindowIdentity.identifier
        settingsWin.title = AppCopy.text(.settingsWindowTitle, in: .en)

        let unrelatedWin = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 300, height: 200),
            styleMask: [.titled],
            backing: .buffered,
            defer: false
        )
        unrelatedWin.title = AppCopy.text(.settingsWindowTitle, in: .zhHans)

        XCTAssertTrue(
            SettingsWindowMatch.shouldCaptureKeyEvent(
                isRecording: true,
                hostWindow: settingsWin,
                keyWindow: settingsWin,
                eventWindow: settingsWin
            )
        )
        settingsWin.title = AppCopy.text(.settingsWindowTitle, in: .zhHans)
        XCTAssertTrue(
            SettingsWindowMatch.shouldCaptureKeyEvent(
                isRecording: true,
                hostWindow: settingsWin,
                keyWindow: settingsWin,
                eventWindow: settingsWin
            )
        )
        XCTAssertFalse(
            SettingsWindowMatch.shouldCaptureKeyEvent(
                isRecording: false,
                hostWindow: settingsWin,
                keyWindow: settingsWin,
                eventWindow: settingsWin
            )
        )
        XCTAssertFalse(
            SettingsWindowMatch.shouldCaptureKeyEvent(
                isRecording: true,
                hostWindow: settingsWin,
                keyWindow: settingsWin,
                eventWindow: unrelatedWin
            )
        )
        XCTAssertFalse(
            SettingsWindowMatch.shouldCaptureKeyEvent(
                isRecording: true,
                hostWindow: nil,
                keyWindow: unrelatedWin,
                eventWindow: unrelatedWin
            )
        )
    }

    @MainActor
    func testAutoUpdatePolicyCooldownFailureBackoffAndCheckpointIsolation() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-policy-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: tempRoot, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        let fakeCLI = tempRoot.appendingPathComponent("codex-agy-sync")
        try "#!/bin/sh\nexit 0\n".write(to: fakeCLI, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: fakeCLI.path)

        let apiURL = URL(string: "http://127.0.0.1:30011/releases/latest")!
        let releaseJSON = """
        {
          "tag_name": "v0.5.0",
          "body": "Cooldown and backoff test",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "http://127.0.0.1:30011/SHA256SUMS", "size": 32},
            {"name": "codex_agy_sync-0.5.0-py3-none-any.whl", "browser_download_url": "http://127.0.0.1:30011/pkg.whl", "size": 32}
          ]
        }
        """

        let runner = StubCommandRunner { _, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.4.1\n".utf8), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }
        let fetcher = StubHTTPFetcher(routes: [apiURL: (Data(releaseJSON.utf8), 503)])

        let clockLock = NSLock()
        nonisolated(unsafe) var currentEpoch: TimeInterval = 1_794_800_000.0
        let nowProvider: @Sendable () -> Date = {
            clockLock.withLock { Date(timeIntervalSince1970: currentEpoch) }
        }

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        var prefEvents = 0
        let sub = prefs.$preferences.dropFirst().sink { _ in prefEvents += 1 }
        defer { sub.cancel() }

        let vm = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": fakeCLI.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: tempRoot.appendingPathComponent("updates", isDirectory: true),
            nowProvider: nowProvider
        )

        prefs.setAutoUpdatePolicy(.off)
        XCTAssertEqual(prefEvents, 1)
        await vm.performOpportunisticCheckIfNeeded(trigger: .appLaunched)
        XCTAssertEqual(fetcher.requestedURLs.count, 0)

        prefs.setAutoUpdatePolicy(.checkOnly)
        XCTAssertEqual(prefEvents, 2)
        await vm.performOpportunisticCheckIfNeeded(trigger: .appLaunched)
        XCTAssertEqual(fetcher.requestedURLs.count, 1)
        XCTAssertEqual(prefEvents, 2)
        XCTAssertEqual(vm.availability, .idle)
        XCTAssertNotNil(vm.lastBackgroundCheckError)

        clockLock.withLock { currentEpoch += 1_800 }
        await vm.performOpportunisticCheckIfNeeded(trigger: .panelOpened)
        XCTAssertEqual(fetcher.requestedURLs.count, 1)

        fetcher.setRoutes([apiURL: (Data(releaseJSON.utf8), 200)])
        clockLock.withLock { currentEpoch += 1_801 }
        await vm.performOpportunisticCheckIfNeeded(trigger: .settingsOpened)
        XCTAssertEqual(fetcher.requestedURLs.count, 2)
        XCTAssertNil(vm.lastBackgroundCheckError)
        XCTAssertEqual(vm.lastCheckedEpoch, clockLock.withLock { currentEpoch })
        XCTAssertEqual(vm.popoverNotice?.action, .openUpdatesSettings(tag: "v0.5.0"))
        XCTAssertEqual(vm.popoverNotice?.actionTitle, "查看更新")

        prefs.setLanguage(.en)
        XCTAssertEqual(vm.popoverNotice?.actionTitle, "View Update")
        XCTAssertTrue(vm.statusMessage?.contains("New version v0.5.0") == true)
        XCTAssertTrue(vm.lastCheckedDisplayText?.hasPrefix("Last checked:") == true)
        XCTAssertEqual(vm.alignment.badgeTitle(in: .en), "Aligned")

        prefs.setLanguage(.zhHans)
        XCTAssertEqual(vm.popoverNotice?.actionTitle, "查看更新")
        XCTAssertTrue(vm.lastCheckedDisplayText?.hasPrefix("上次检查：") == true)

        clockLock.withLock { currentEpoch += 3_600 }
        await vm.performOpportunisticCheckIfNeeded(trigger: .panelOpened)
        XCTAssertEqual(fetcher.requestedURLs.count, 2)

        clockLock.withLock { currentEpoch += 82_800 }
        await vm.performOpportunisticCheckIfNeeded(trigger: .panelOpened)
        XCTAssertEqual(fetcher.requestedURLs.count, 3)
        XCTAssertEqual(prefEvents, 4)
    }

    @MainActor
    func testAutoStageLocalRepoGuardrailPersistenceAndDirectoryPruning() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-autostage-\(UUID().uuidString)", isDirectory: true)
        let repoDir = tempRoot.appendingPathComponent("repo", isDirectory: true)
        let scriptsDir = repoDir.appendingPathComponent("scripts", isDirectory: true)
        let cliBin = tempRoot.appendingPathComponent("codex-agy-sync")
        let updatesRoot = tempRoot.appendingPathComponent("updates", isDirectory: true)
        try FileManager.default.createDirectory(at: scriptsDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        try "[project]\nname = \"codex-agy-sync\"\nversion = \"0.4.1\"\n"
            .write(to: repoDir.appendingPathComponent("pyproject.toml"), atomically: true, encoding: .utf8)
        let buildScript = scriptsDir.appendingPathComponent("build_menubar_app.sh")
        try "#!/bin/sh\nexit 0\n".write(to: buildScript, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: buildScript.path)
        try "#!/usr/bin/env python3\n".write(to: cliBin, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cliBin.path)

        let wheelBytes = Data("staged-wheel-050".utf8)
        let zipBytes = Data("staged-zip-050".utf8)
        let sums = """
        \(SHA256Verifier.sha256Hex(of: wheelBytes))  codex_agy_sync-0.5.0-py3-none-any.whl
        \(SHA256Verifier.sha256Hex(of: zipBytes))  CodexAgyHandoff-macos.zip
        """
        let apiURL = URL(string: "http://127.0.0.1:30012/releases/latest")!
        let sumsURL = URL(string: "http://127.0.0.1:30012/SHA256SUMS")!
        let wheelURL = URL(string: "http://127.0.0.1:30012/codex_agy_sync-0.5.0-py3-none-any.whl")!
        let zipURL = URL(string: "http://127.0.0.1:30012/CodexAgyHandoff-macos.zip")!
        let releaseJSON = """
        {
          "tag_name": "v0.5.0",
          "body": "Auto-stage test",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "\(sumsURL.absoluteString)", "size": 128},
            {"name": "codex_agy_sync-0.5.0-py3-none-any.whl", "browser_download_url": "\(wheelURL.absoluteString)", "size": \(wheelBytes.count)},
            {"name": "CodexAgyHandoff-macos.zip", "browser_download_url": "\(zipURL.absoluteString)", "size": \(zipBytes.count)}
          ]
        }
        """

        let runner = StubCommandRunner { exe, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.4.1\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = #"{"executable":"/usr/bin/python3","in_venv":true,"editable":false,"repo_path":null,"supports_break_system_packages":false}"#
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            if exe.path == "/usr/bin/ditto", let destPath = args.last {
                let appDir = URL(fileURLWithPath: destPath).appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
                try FileManager.default.createDirectory(at: appDir, withIntermediateDirectories: true)
                try "v0.5.0".write(to: appDir.appendingPathComponent("version.txt"), atomically: true, encoding: .utf8)
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }
        let fetcher = StubHTTPFetcher(routes: [
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data(sums.utf8), 200),
            wheelURL: (wheelBytes, 200),
            zipURL: (zipBytes, 200),
        ])

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        prefs.setAutoUpdatePolicy(.autoStage)
        prefs.setLocalRepoPathForSync(repoDir.path)

        let vmLocal = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
                UpdateViewModel.skipSelfReplaceEnvKey: "1",
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: updatesRoot
        )
        await vmLocal.performOpportunisticCheckIfNeeded(trigger: .appLaunched)
        XCTAssertNil(vmLocal.stagedManifest)
        XCTAssertEqual(fetcher.requestedURLs, [apiURL])
        XCTAssertFalse(runner.invocations.contains { $0.arguments.prefix(2) == ["-m", "pip"] })

        prefs.setLocalRepoPathForSync(nil)
        defaults.removeObject(forKey: UpdateViewModel.checkpointDefaultsKey)

        let legacyStale = updatesRoot.appendingPathComponent("legacy-tmp", isDirectory: true)
        let existingStagedOld = updatesRoot.appendingPathComponent("staged/0.4.9", isDirectory: true)
        try FileManager.default.createDirectory(at: legacyStale, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: existingStagedOld, withIntermediateDirectories: true)
        let oldDate = Date().addingTimeInterval(-100_000)
        try FileManager.default.setAttributes([.modificationDate: oldDate], ofItemAtPath: legacyStale.path)
        try FileManager.default.setAttributes([.modificationDate: oldDate], ofItemAtPath: (updatesRoot.appendingPathComponent("staged")).path)

        let vmStage = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
                UpdateViewModel.skipSelfReplaceEnvKey: "1",
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: updatesRoot
        )
        await vmStage.performOpportunisticCheckIfNeeded(trigger: .panelOpened)

        XCTAssertNotNil(vmStage.stagedManifest)
        XCTAssertEqual(vmStage.stagedManifest?.version, "0.5.0")
        XCTAssertEqual(vmStage.popoverNotice?.action, .installStagedAndRelaunch(tag: "v0.5.0"))
        XCTAssertFalse(runner.invocations.contains { $0.arguments.prefix(2) == ["-m", "pip"] })
        XCTAssertFalse(FileManager.default.fileExists(atPath: legacyStale.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: existingStagedOld.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: updatesRoot.appendingPathComponent("staged/0.5.0/stage-manifest.json").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: updatesRoot.appendingPathComponent("staged/0.5.0/CodexAgyHandoff-macos.zip").path))

        let vmReloaded = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
                UpdateViewModel.skipSelfReplaceEnvKey: "1",
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: updatesRoot
        )
        await vmReloaded.probeEnvironment()
        XCTAssertEqual(vmReloaded.stagedManifest?.version, "0.5.0")
        XCTAssertEqual(vmReloaded.popoverNotice?.action, .installStagedAndRelaunch(tag: "v0.5.0"))
    }

    @MainActor
    func testInstallStagedAndRelaunchRespectsCanMutateGuardAndRelaunchHooks() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-install-staged-\(UUID().uuidString)", isDirectory: true)
        let cliBin = tempRoot.appendingPathComponent("codex-agy-sync")
        let updatesRoot = tempRoot.appendingPathComponent("updates", isDirectory: true)
        let installedApp = tempRoot.appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
        try FileManager.default.createDirectory(at: installedApp, withIntermediateDirectories: true)
        try "v0.4.1".write(to: installedApp.appendingPathComponent("version.txt"), atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        try "#!/usr/bin/env python3\n".write(to: cliBin, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cliBin.path)

        let wheelBytes = Data("staged-wheel-050".utf8)
        let zipBytes = Data("staged-zip-050".utf8)
        let sums = """
        \(SHA256Verifier.sha256Hex(of: wheelBytes))  codex_agy_sync-0.5.0-py3-none-any.whl
        \(SHA256Verifier.sha256Hex(of: zipBytes))  CodexAgyHandoff-macos.zip
        """
        let apiURL = URL(string: "http://127.0.0.1:30013/releases/latest")!
        let sumsURL = URL(string: "http://127.0.0.1:30013/SHA256SUMS")!
        let wheelURL = URL(string: "http://127.0.0.1:30013/codex_agy_sync-0.5.0-py3-none-any.whl")!
        let zipURL = URL(string: "http://127.0.0.1:30013/CodexAgyHandoff-macos.zip")!
        let releaseJSON = """
        {
          "tag_name": "v0.5.0",
          "body": "Install staged test",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "\(sumsURL.absoluteString)", "size": 128},
            {"name": "codex_agy_sync-0.5.0-py3-none-any.whl", "browser_download_url": "\(wheelURL.absoluteString)", "size": \(wheelBytes.count)},
            {"name": "CodexAgyHandoff-macos.zip", "browser_download_url": "\(zipURL.absoluteString)", "size": \(zipBytes.count)}
          ]
        }
        """

        let cliVerLock = NSLock()
        nonisolated(unsafe) var cliVer = "0.4.1"
        let runner = StubCommandRunner { exe, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync \(cliVerLock.withLock { cliVer })\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = #"{"executable":"/usr/bin/python3","in_venv":true,"editable":false,"repo_path":null,"supports_break_system_packages":false}"#
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            if exe.path == "/usr/bin/ditto", let destPath = args.last {
                let appDir = URL(fileURLWithPath: destPath).appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
                try FileManager.default.createDirectory(at: appDir, withIntermediateDirectories: true)
                try "v0.5.0".write(to: appDir.appendingPathComponent("version.txt"), atomically: true, encoding: .utf8)
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            if args.prefix(3) == ["-m", "pip", "install"] {
                cliVerLock.withLock { cliVer = "0.5.0" }
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }
        let fetcher = StubHTTPFetcher(routes: [
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data(sums.utf8), 200),
            wheelURL: (wheelBytes, 200),
            zipURL: (zipBytes, 200),
        ])

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        prefs.setAutoUpdatePolicy(.autoStage)

        @MainActor
        final class GuardAndHookState {
            var allowMutate = false
            var events: [String] = []
        }
        let hookState = GuardAndHookState()
        let relaunchMock = StubRelaunchExecutor {
            hookState.events.append("relaunch")
        }

        let vm = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            currentAppBundleURL: installedApp,
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            relaunchExecutor: relaunchMock,
            stagingRootOverride: updatesRoot,
            canMutateEnvironment: { hookState.allowMutate },
            onPrepareForRelaunch: { hookState.events.append("prepare") }
        )

        await vm.performOpportunisticCheckIfNeeded(trigger: .appLaunched)
        XCTAssertNotNil(vm.stagedManifest)
        let fetchCountAfterStage = fetcher.requestedURLs.count

        await vm.installStagedAndRelaunch()
        XCTAssertEqual(vm.statusMessage, AppCopy.text(.updatesErrHandoffInProgress, in: .zhHans))
        XCTAssertFalse(runner.invocations.contains { $0.arguments.prefix(2) == ["-m", "pip"] })
        XCTAssertTrue(hookState.events.isEmpty)
        XCTAssertEqual(try String(contentsOf: installedApp.appendingPathComponent("version.txt"), encoding: .utf8), "v0.4.1")

        await vm.probeEnvironment()
        XCTAssertNotNil(vm.stagedManifest)

        await vm.installStagedAndRelaunch()
        XCTAssertEqual(vm.statusMessage, AppCopy.text(.updatesErrHandoffInProgress, in: .zhHans))

        hookState.allowMutate = true
        await vm.installStagedAndRelaunch()
        XCTAssertEqual(fetcher.requestedURLs.count, fetchCountAfterStage)
        XCTAssertTrue(runner.invocations.contains { $0.arguments.prefix(3) == ["-m", "pip", "install"] })
        XCTAssertEqual(try String(contentsOf: installedApp.appendingPathComponent("version.txt"), encoding: .utf8), "v0.5.0")
        XCTAssertEqual(hookState.events, ["prepare", "relaunch"])
        XCTAssertEqual(relaunchMock.relaunchedURLs, [installedApp])
        XCTAssertTrue(vm.canRelaunchApp)
        XCTAssertEqual(vm.pendingRelaunchVersion, "0.5.0")
        XCTAssertEqual(vm.alignment, .aligned(version: "0.5.0"))
        XCTAssertFalse(FileManager.default.fileExists(atPath: updatesRoot.appendingPathComponent("staged/0.5.0").path))
    }

    @MainActor
    func testAppPreferencesForwardCompatibleDecodingAndLocaleDidChangeNotification() throws {
        let legacyOrFutureJSON = """
        {
          "showAttentionDotOnStatusIcon": false,
          "hotKey": {
            "isEnabled": true,
            "keyCode": 40,
            "carbonModifiers": 4608,
            "displayString": "⌃⇧K"
          },
          "hotKeyBehavior": "future_instant_warp",
          "activateTargetAppAfterHandoff": false,
          "recentFeedLimit": 8,
          "customCLIPath": "/opt/tools/codex-agy-sync",
          "localRepoPathForSync": "/repos/codex-agy-sync",
          "language": "fr-CA",
          "autoUpdatePolicy": "future_auto_apply"
        }
        """
        let decoded = try JSONDecoder().decode(AppPreferences.self, from: Data(legacyOrFutureJSON.utf8))
        XCTAssertEqual(decoded.hotKeyBehavior, .instantPrimaryRescue)
        XCTAssertEqual(decoded.language, .zhHans)
        XCTAssertEqual(decoded.autoUpdatePolicy, .checkOnly)
        XCTAssertFalse(decoded.showAttentionDotOnStatusIcon)
        XCTAssertFalse(decoded.activateTargetAppAfterHandoff)
        XCTAssertEqual(decoded.recentFeedLimit, 8)
        XCTAssertEqual(decoded.customCLIPath, "/opt/tools/codex-agy-sync")
        XCTAssertEqual(decoded.localRepoPathForSync, "/repos/codex-agy-sync")
        XCTAssertEqual(decoded.hotKey.displayString, "⌃⇧K")

        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["zh-MO"]), .zhHans)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["zh-MO-Hant"]), .zhHans)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["zh-Hant-HK", "en-US"]), .en)
        XCTAssertEqual(AppLanguage.system.resolve(preferredLanguages: ["zh-Hant-MO", "en-US"]), .en)

        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let localeLock = NSLock()
        nonisolated(unsafe) var systemLanguages = ["en-US"]
        let store = PreferencesStore(
            defaults: defaults,
            loginItemManager: NoOpLoginItemManager(),
            preferredLanguagesProvider: {
                localeLock.withLock { systemLanguages }
            }
        )
        store.setLanguage(.system)
        XCTAssertEqual(store.effectiveLanguage, .en)

        localeLock.withLock { systemLanguages = ["zh-Hans-CN"] }
        NotificationCenter.default.post(name: NSLocale.currentLocaleDidChangeNotification, object: nil)
        XCTAssertEqual(store.effectiveLanguage, .zhHans)
    }

    @MainActor
    func testAutoStageFailureEnforcesOneHourBackoffWithoutThrashing() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-autostage-backoff-\(UUID().uuidString)", isDirectory: true)
        let cliBin = tempRoot.appendingPathComponent("codex-agy-sync")
        try FileManager.default.createDirectory(at: tempRoot, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        try "#!/bin/sh\nexit 0\n".write(to: cliBin, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cliBin.path)

        let wheelBytes = Data("wheel-backoff-050".utf8)
        let sums = "\(SHA256Verifier.sha256Hex(of: wheelBytes))  codex_agy_sync-0.5.0-py3-none-any.whl\n"
        let apiURL = URL(string: "http://127.0.0.1:30101/releases/latest")!
        let sumsURL = URL(string: "http://127.0.0.1:30101/SHA256SUMS")!
        let wheelURL = URL(string: "http://127.0.0.1:30101/codex_agy_sync-0.5.0-py3-none-any.whl")!
        let releaseJSON = """
        {
          "tag_name": "v0.5.0",
          "body": "",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "\(sumsURL.absoluteString)", "size": 64},
            {"name": "codex_agy_sync-0.5.0-py3-none-any.whl", "browser_download_url": "\(wheelURL.absoluteString)", "size": \(wheelBytes.count)}
          ]
        }
        """

        let runner = StubCommandRunner { _, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.4.1\n".utf8), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }
        let fetcher = StubHTTPFetcher(routes: [
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data("unavailable".utf8), 503),
            wheelURL: (wheelBytes, 200),
        ])

        let clockLock = NSLock()
        nonisolated(unsafe) var epoch: TimeInterval = 1_795_000_000.0
        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        prefs.setAutoUpdatePolicy(.autoStage)

        let vm = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: tempRoot.appendingPathComponent("updates", isDirectory: true),
            nowProvider: { clockLock.withLock { Date(timeIntervalSince1970: epoch) } }
        )

        await vm.performOpportunisticCheckIfNeeded(trigger: .appLaunched)
        XCTAssertNil(vm.stagedManifest)
        XCTAssertNotNil(vm.lastBackgroundCheckError)
        let cpAfterFail = UpdateViewModel.loadCheckpoint(from: defaults)
        XCTAssertNil(cpAfterFail.lastSuccessfulCheckEpoch)
        XCTAssertEqual(cpAfterFail.lastAttemptEpoch, 1_795_000_000.0)
        let hitsAfterFail = fetcher.requestedURLs.count

        clockLock.withLock { epoch += 600 }
        await vm.performOpportunisticCheckIfNeeded(trigger: .panelOpened)
        await vm.performOpportunisticCheckIfNeeded(trigger: .settingsOpened)
        XCTAssertEqual(fetcher.requestedURLs.count, hitsAfterFail)

        fetcher.setRoutes([
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data(sums.utf8), 200),
            wheelURL: (wheelBytes, 200),
        ])
        clockLock.withLock { epoch += 3_001 }
        await vm.performOpportunisticCheckIfNeeded(trigger: .panelOpened)
        XCTAssertEqual(vm.stagedManifest?.version, "0.5.0")
        XCTAssertEqual(vm.stagedManifest?.notesSummary, "")
        XCTAssertEqual(
            vm.availability,
            .stagedReady(
                tagName: "v0.5.0",
                version: "0.5.0",
                notesSummary: AppCopy.text(.updatesDefaultNotesSummary, in: .zhHans),
                hasAppAsset: false,
                hasWheelAsset: true
            )
        )
        prefs.setLanguage(.en)
        XCTAssertEqual(
            vm.availability,
            .stagedReady(
                tagName: "v0.5.0",
                version: "0.5.0",
                notesSummary: AppCopy.text(.updatesDefaultNotesSummary, in: .en),
                hasAppAsset: false,
                hasWheelAsset: true
            )
        )

        prefs.setAutoUpdatePolicy(.checkOnly)
        let policyFetcher = StubHTTPFetcher(routes: [
            apiURL: (Data(releaseJSON.utf8), 200),
            sumsURL: (Data("unavailable".utf8), 503),
            wheelURL: (wheelBytes, 200),
        ])
        let vmPolicyFail = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: runner,
            httpFetcher: policyFetcher,
            stagingRootOverride: tempRoot.appendingPathComponent("updates-policy-fail", isDirectory: true),
            nowProvider: { clockLock.withLock { Date(timeIntervalSince1970: epoch) } }
        )
        await vmPolicyFail.checkForUpdates()
        XCTAssertNotNil(UpdateViewModel.loadCheckpoint(from: defaults).lastSuccessfulCheckEpoch)
        prefs.setAutoUpdatePolicy(.autoStage)
        await vmPolicyFail.handlePolicyChanged(to: .autoStage)
        let cpPolicyFail = UpdateViewModel.loadCheckpoint(from: defaults)
        XCTAssertNil(cpPolicyFail.lastSuccessfulCheckEpoch)
        XCTAssertEqual(cpPolicyFail.lastAttemptEpoch, clockLock.withLock { epoch })
        let policyHitsAfterFail = policyFetcher.requestedURLs.count
        clockLock.withLock { epoch += 600 }
        await vmPolicyFail.performOpportunisticCheckIfNeeded(trigger: .panelOpened)
        XCTAssertEqual(policyFetcher.requestedURLs.count, policyHitsAfterFail)
    }

    @MainActor
    func testReconcilePersistedStagedVersionPrecedenceRepoGuardAndFailureRecovery() async throws {
        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-reconcile-\(UUID().uuidString)", isDirectory: true)
        let repoDir = tempRoot.appendingPathComponent("repo", isDirectory: true)
        let scriptsDir = repoDir.appendingPathComponent("scripts", isDirectory: true)
        let cliBin = tempRoot.appendingPathComponent("codex-agy-sync")
        let updatesRoot = tempRoot.appendingPathComponent("updates", isDirectory: true)
        let staged050 = updatesRoot.appendingPathComponent("staged/0.5.0", isDirectory: true)
        try FileManager.default.createDirectory(at: scriptsDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: staged050, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        try "[project]\nname = \"codex-agy-sync\"\nversion = \"0.4.1\"\n"
            .write(to: repoDir.appendingPathComponent("pyproject.toml"), atomically: true, encoding: .utf8)
        let buildScript = scriptsDir.appendingPathComponent("build_menubar_app.sh")
        try "#!/bin/sh\nexit 0\n".write(to: buildScript, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: buildScript.path)
        try "#!/bin/sh\nexit 0\n".write(to: cliBin, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cliBin.path)

        let wheelBytes = Data("wheel-reconcile-050".utf8)
        let wheelName = "codex_agy_sync-0.5.0-py3-none-any.whl"
        try wheelBytes.write(to: staged050.appendingPathComponent(wheelName))
        let legacyManifest = StagedUpdateManifest(
            tagName: "v0.5.0",
            version: "0.5.0",
            notesSummary: AppCopy.text(.updatesDefaultNotesSummary, in: .zhHans),
            wheelFileName: wheelName,
            wheelSHA256: SHA256Verifier.sha256Hex(of: wheelBytes),
            appBundleName: nil,
            stagedAtEpoch: 1_795_000_000
        )
        try JSONEncoder().encode(legacyManifest).write(to: staged050.appendingPathComponent("stage-manifest.json"))

        let apiURL = URL(string: "http://127.0.0.1:30102/releases/latest")!
        let release060JSON = """
        {
          "tag_name": "v0.6.0",
          "body": "Newer v0.6.0 release",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "http://127.0.0.1:30102/SHA256SUMS", "size": 64},
            {"name": "codex_agy_sync-0.6.0-py3-none-any.whl", "browser_download_url": "http://127.0.0.1:30102/pkg.whl", "size": 64}
          ]
        }
        """

        let pipFailLock = NSLock()
        nonisolated(unsafe) var shouldFailPip = true
        let runner = StubCommandRunner { _, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.4.1\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = #"{"executable":"/usr/bin/python3","in_venv":true,"editable":false,"repo_path":null}"#
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            if args.prefix(3) == ["-m", "pip", "install"], pipFailLock.withLock({ shouldFailPip }) {
                return CommandExecutionResult(exitCode: 1, stdout: Data(), stderr: Data("pip transient failure".utf8))
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }
        let fetcher = StubHTTPFetcher(routes: [apiURL: (Data(release060JSON.utf8), 200)])

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        prefs.setLanguage(.en)
        let vm = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: updatesRoot
        )

        await vm.probeEnvironment()
        XCTAssertEqual(vm.stagedManifest?.version, "0.5.0")
        XCTAssertEqual(
            vm.availability,
            .stagedReady(
                tagName: "v0.5.0",
                version: "0.5.0",
                notesSummary: AppCopy.text(.updatesDefaultNotesSummary, in: .en),
                hasAppAsset: false,
                hasWheelAsset: true
            )
        )

        await vm.checkForUpdates()
        XCTAssertNil(vm.stagedManifest)
        XCTAssertEqual(
            vm.availability,
            .releaseAvailable(
                latestTag: "v0.6.0",
                notesSummary: "Newer v0.6.0 release",
                hasAppAsset: false,
                hasWheelAsset: true
            )
        )

        await vm.probeEnvironment()
        XCTAssertNil(vm.stagedManifest)
        if case .releaseAvailable(let latestTag, _, _, _) = vm.availability {
            XCTAssertEqual(latestTag, "v0.6.0")
        } else {
            XCTFail("Expected .releaseAvailable(v0.6.0) not to downgrade to staged 0.5.0, got \(vm.availability)")
        }

        let vmRecover = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: runner,
            httpFetcher: fetcher,
            stagingRootOverride: updatesRoot
        )
        await vmRecover.probeEnvironment()
        XCTAssertEqual(vmRecover.stagedManifest?.version, "0.5.0")

        prefs.setLocalRepoPathForSync(repoDir.path)
        await vmRecover.probeEnvironment()
        XCTAssertNil(vmRecover.stagedManifest)
        XCTAssertEqual(vmRecover.availability, .idle)

        prefs.setLocalRepoPathForSync(nil)
        await vmRecover.probeEnvironment()
        XCTAssertEqual(vmRecover.stagedManifest?.version, "0.5.0")

        await vmRecover.installStagedAndRelaunch()
        guard case .failed = vmRecover.availability else {
            return XCTFail("Expected .failed after pip failure, got \(vmRecover.availability)")
        }
        await vmRecover.probeEnvironment(recoverStagedFromFailure: false)
        guard case .failed = vmRecover.availability else {
            return XCTFail("Expected .failed without recoverStagedFromFailure, got \(vmRecover.availability)")
        }
        await vmRecover.probeEnvironment(recoverStagedFromFailure: true)
        XCTAssertEqual(vmRecover.stagedManifest?.version, "0.5.0")

        pipFailLock.withLock { shouldFailPip = false }
        try Data("corrupted-wheel".utf8).write(to: staged050.appendingPathComponent(wheelName))
        await vmRecover.installStagedAndRelaunch()
        guard case .failed = vmRecover.availability else {
            return XCTFail("Expected .failed for invalid staged wheel")
        }
        await vmRecover.probeEnvironment(recoverStagedFromFailure: true)
        XCTAssertNil(vmRecover.stagedManifest)
    }

    @MainActor
    func testConcurrentProbeGenerationManualSupersedeDeletedStagedAndMidFlightPolicyAbort() async throws {
        final class GatedCommandRunner: SystemCommandRunning, @unchecked Sendable {
            private let lock = NSLock()
            private var gates: [CheckedContinuation<CommandExecutionResult, Never>] = []
            private var waitEntered: [CheckedContinuation<Void, Never>] = []
            private var callIndex = 0
            private let onCall: @Sendable (Int, URL, [String]) -> CommandExecutionResult?

            init(onCall: @escaping @Sendable (Int, URL, [String]) -> CommandExecutionResult?) {
                self.onCall = onCall
            }

            func waitForNextGate() async {
                await withCheckedContinuation { cont in
                    lock.withLock {
                        if !gates.isEmpty {
                            cont.resume()
                        } else {
                            waitEntered.append(cont)
                        }
                    }
                }
            }

            func releaseGate(with result: CommandExecutionResult) {
                let cont = lock.withLock { gates.isEmpty ? nil : gates.removeFirst() }
                cont?.resume(returning: result)
            }

            func run(
                executableURL: URL,
                arguments: [String],
                environment: [String: String],
                timeoutSeconds: TimeInterval
            ) async throws -> CommandExecutionResult {
                let idx = lock.withLock { () -> Int in
                    callIndex += 1
                    return callIndex
                }
                if let immediate = onCall(idx, executableURL, arguments) {
                    return immediate
                }
                return await withCheckedContinuation { (cont: CheckedContinuation<CommandExecutionResult, Never>) in
                    let waiters = lock.withLock { () -> [CheckedContinuation<Void, Never>] in
                        gates.append(cont)
                        let current = waitEntered
                        waitEntered.removeAll()
                        return current
                    }
                    for waiter in waiters {
                        waiter.resume()
                    }
                }
            }
        }

        final class GatedHTTPFetcher: ReleaseHTTPFetching, @unchecked Sendable {
            private let lock = NSLock()
            private var routes: [URL: (Data, Int)]
            private var gatedPathSuffix: String?
            private var gateCont: CheckedContinuation<(data: Data, statusCode: Int), Never>?
            private var enterCont: CheckedContinuation<Void, Never>?

            init(routes: [URL: (Data, Int)]) {
                self.routes = routes
            }

            func setRoutes(_ newRoutes: [URL: (Data, Int)]) {
                lock.withLock { routes = newRoutes }
            }

            func gateNextRequest(matchingSuffix suffix: String) {
                lock.withLock { gatedPathSuffix = suffix }
            }

            func waitForGatedRequest() async {
                await withCheckedContinuation { cont in
                    lock.withLock {
                        if gateCont != nil {
                            cont.resume()
                        } else {
                            enterCont = cont
                        }
                    }
                }
            }

            func resumeGatedRequest(with result: (Data, Int)) {
                let cont = lock.withLock { () -> CheckedContinuation<(data: Data, statusCode: Int), Never>? in
                    let c = gateCont
                    gateCont = nil
                    return c
                }
                cont?.resume(returning: (data: result.0, statusCode: result.1))
            }

            func fetchData(
                from url: URL,
                headers: [String: String],
                timeoutSeconds: TimeInterval
            ) async throws -> (data: Data, statusCode: Int) {
                let shouldGate = lock.withLock { () -> Bool in
                    if let suffix = gatedPathSuffix, url.path.hasSuffix(suffix) {
                        gatedPathSuffix = nil
                        return true
                    }
                    return false
                }
                if shouldGate {
                    return await withCheckedContinuation { (cont: CheckedContinuation<(data: Data, statusCode: Int), Never>) in
                        let enter = lock.withLock { () -> CheckedContinuation<Void, Never>? in
                            gateCont = cont
                            let c = enterCont
                            enterCont = nil
                            return c
                        }
                        enter?.resume()
                    }
                }
                return lock.withLock { routes[url] } ?? (Data(), 404)
            }
        }

        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-concurrency-\(UUID().uuidString)", isDirectory: true)
        let cli1 = tempRoot.appendingPathComponent("cli1/codex-agy-sync")
        let cli2 = tempRoot.appendingPathComponent("cli2/codex-agy-sync")
        let updatesRoot = tempRoot.appendingPathComponent("updates", isDirectory: true)
        for cli in [cli1, cli2] {
            try FileManager.default.createDirectory(at: cli.deletingLastPathComponent(), withIntermediateDirectories: true)
            try "#!/bin/sh\nexit 0\n".write(to: cli, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)
        }
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        prefs.setCustomCLIPath(cli1.path)

        let gatedRunner = GatedCommandRunner { _, exe, args in
            if args == ["--version"], exe.path == cli1.path {
                return nil
            }
            if args == ["--version"], exe.path == cli2.path {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.4.1\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = #"{"executable":"/usr/bin/python3","in_venv":true,"editable":false,"repo_path":null}"#
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }

        let vmProbe = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: ["PATH": "/nonexistent"],
            commandRunner: gatedRunner,
            httpFetcher: StubHTTPFetcher(routes: [:]),
            stagingRootOverride: updatesRoot
        )
        let probe1Task = Task { await vmProbe.probeEnvironment() }
        await gatedRunner.waitForNextGate()
        prefs.setCustomCLIPath(cli2.path)
        await vmProbe.probeEnvironment()
        XCTAssertEqual(vmProbe.alignment, .aligned(version: "0.4.1"))
        gatedRunner.releaseGate(
            with: CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.2.0\n".utf8), stderr: Data())
        )
        await probe1Task.value
        XCTAssertEqual(vmProbe.alignment, .aligned(version: "0.4.1"))
        XCTAssertEqual(vmProbe.resolvedCLIPath, cli2.path)

        let wheel050 = Data("wheel-050".utf8)
        let wheel050Name = "codex_agy_sync-0.5.0-py3-none-any.whl"
        let staged050Dir = updatesRoot.appendingPathComponent("staged/0.5.0", isDirectory: true)
        try FileManager.default.createDirectory(at: staged050Dir, withIntermediateDirectories: true)
        try wheel050.write(to: staged050Dir.appendingPathComponent(wheel050Name))
        let manifest050 = StagedUpdateManifest(
            tagName: "v0.5.0",
            version: "0.5.0",
            notesSummary: "",
            wheelFileName: wheel050Name,
            wheelSHA256: SHA256Verifier.sha256Hex(of: wheel050),
            appBundleName: nil,
            stagedAtEpoch: 1_795_000_000
        )
        try JSONEncoder().encode(manifest050).write(to: staged050Dir.appendingPathComponent("stage-manifest.json"))

        let apiURL = URL(string: "http://127.0.0.1:30103/releases/latest")!
        let sumsURL = URL(string: "http://127.0.0.1:30103/SHA256SUMS")!
        let wheelURL = URL(string: "http://127.0.0.1:30103/codex_agy_sync-0.5.0-py3-none-any.whl")!
        let release060JSON = """
        {
          "tag_name": "v0.6.0",
          "body": "Supersede test",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "\(sumsURL.absoluteString)", "size": 64},
            {"name": "codex_agy_sync-0.6.0-py3-none-any.whl", "browser_download_url": "\(wheelURL.absoluteString)", "size": 32}
          ]
        }
        """
        let release050JSON = """
        {
          "tag_name": "v0.5.0",
          "body": "Mid-flight abort test",
          "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "\(sumsURL.absoluteString)", "size": 64},
            {"name": "codex_agy_sync-0.5.0-py3-none-any.whl", "browser_download_url": "\(wheelURL.absoluteString)", "size": \(wheel050.count)}
          ]
        }
        """
        let sums050 = "\(SHA256Verifier.sha256Hex(of: wheel050))  \(wheel050Name)\n"

        let gatedFetcher = GatedHTTPFetcher(routes: [
            apiURL: (Data(release060JSON.utf8), 200),
            sumsURL: (Data(sums050.utf8), 200),
            wheelURL: (wheel050, 200),
        ])
        let cliVerLock = NSLock()
        nonisolated(unsafe) var cliVer = "0.4.1"
        let simpleRunner = StubCommandRunner { _, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync \(cliVerLock.withLock { cliVer })\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = #"{"executable":"/usr/bin/python3","in_venv":true,"editable":false,"repo_path":null}"#
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            if args.prefix(3) == ["-m", "pip", "install"] {
                cliVerLock.withLock { cliVer = "0.5.0" }
                return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }

        prefs.setAutoUpdatePolicy(.checkOnly)
        let vmManualCheck = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cli2.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: simpleRunner,
            httpFetcher: gatedFetcher,
            stagingRootOverride: updatesRoot
        )
        await vmManualCheck.probeEnvironment()
        XCTAssertEqual(vmManualCheck.stagedManifest?.version, "0.5.0")

        gatedFetcher.gateNextRequest(matchingSuffix: "/releases/latest")
        let oppCheckTask = Task { await vmManualCheck.performOpportunisticCheckIfNeeded(trigger: .panelOpened) }
        await gatedFetcher.waitForGatedRequest()

        await vmManualCheck.checkForUpdates()
        if case .releaseAvailable(let tag, _, _, _) = vmManualCheck.availability {
            XCTAssertEqual(tag, "v0.6.0")
        } else {
            XCTFail("Expected manual checkForUpdates during in-flight opportunistic check to succeed")
        }

        gatedFetcher.resumeGatedRequest(with: (Data(), 503))
        await oppCheckTask.value
        if case .releaseAvailable(let tag, _, _, _) = vmManualCheck.availability {
            XCTAssertEqual(tag, "v0.6.0")
        } else {
            XCTFail("Superseded opportunistic failure overwrote manual check result: \(vmManualCheck.availability)")
        }

        defaults.removeObject(forKey: UpdateViewModel.checkpointDefaultsKey)
        let vmManualInstall = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cli2.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: simpleRunner,
            httpFetcher: gatedFetcher,
            stagingRootOverride: updatesRoot
        )
        await vmManualInstall.probeEnvironment()
        XCTAssertEqual(vmManualInstall.stagedManifest?.version, "0.5.0")

        gatedFetcher.gateNextRequest(matchingSuffix: "/releases/latest")
        let oppInstallTask = Task { await vmManualInstall.performOpportunisticCheckIfNeeded(trigger: .panelOpened) }
        await gatedFetcher.waitForGatedRequest()

        await vmManualInstall.installStagedAndRelaunch()
        XCTAssertEqual(vmManualInstall.availability, .upToDate(currentVersion: "0.5.0"))

        gatedFetcher.resumeGatedRequest(with: (Data(release060JSON.utf8), 200))
        await oppInstallTask.value
        XCTAssertEqual(vmManualInstall.availability, .upToDate(currentVersion: "0.5.0"))

        cliVerLock.withLock { cliVer = "0.4.1" }
        try FileManager.default.createDirectory(at: staged050Dir, withIntermediateDirectories: true)
        try wheel050.write(to: staged050Dir.appendingPathComponent(wheel050Name))
        try JSONEncoder().encode(manifest050).write(to: staged050Dir.appendingPathComponent("stage-manifest.json"))
        defaults.removeObject(forKey: UpdateViewModel.checkpointDefaultsKey)

        let vmDeletedStaged = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cli2.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: simpleRunner,
            httpFetcher: gatedFetcher,
            stagingRootOverride: updatesRoot
        )
        await vmDeletedStaged.probeEnvironment()
        XCTAssertEqual(vmDeletedStaged.stagedManifest?.version, "0.5.0")

        try FileManager.default.removeItem(at: staged050Dir)
        gatedFetcher.setRoutes([apiURL: (Data(), 503)])
        await vmDeletedStaged.performOpportunisticCheckIfNeeded(trigger: .panelOpened)
        XCTAssertNil(vmDeletedStaged.stagedManifest)
        XCTAssertEqual(vmDeletedStaged.availability, .idle)

        defaults.removeObject(forKey: UpdateViewModel.checkpointDefaultsKey)
        prefs.setAutoUpdatePolicy(.autoStage)
        gatedFetcher.setRoutes([
            apiURL: (Data(release050JSON.utf8), 200),
            sumsURL: (Data(sums050.utf8), 200),
            wheelURL: (wheel050, 200),
        ])
        gatedFetcher.gateNextRequest(matchingSuffix: wheel050Name)

        let vmAbortStage = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            environment: [
                "CODEX_AGY_SYNC_BIN": cli2.path,
                UpdateViewModel.releaseAPIEnvKey: apiURL.absoluteString,
            ],
            commandRunner: simpleRunner,
            httpFetcher: gatedFetcher,
            stagingRootOverride: updatesRoot
        )
        let abortTask = Task { await vmAbortStage.performOpportunisticCheckIfNeeded(trigger: .panelOpened) }
        await gatedFetcher.waitForGatedRequest()
        XCTAssertFalse(vmAbortStage.isInstallingUpdate)
        prefs.setAutoUpdatePolicy(.checkOnly)
        gatedFetcher.resumeGatedRequest(with: (wheel050, 200))
        await abortTask.value

        XCTAssertNil(vmAbortStage.stagedManifest)
        XCTAssertFalse(FileManager.default.fileExists(atPath: staged050Dir.path))
        let tmpChildren = (try? FileManager.default.contentsOfDirectory(atPath: updatesRoot.appendingPathComponent("tmp").path)) ?? []
        XCTAssertTrue(tmpChildren.isEmpty)
        if case .releaseAvailable(let tag, _, _, _) = vmAbortStage.availability {
            XCTAssertEqual(tag, "v0.5.0")
        } else {
            XCTFail("Expected .releaseAvailable after mid-flight policy downgrade, got \(vmAbortStage.availability)")
        }

        defaults.removeObject(forKey: UpdateViewModel.checkpointDefaultsKey)
        prefs.setAutoUpdatePolicy(.autoStage)
        gatedFetcher.gateNextRequest(matchingSuffix: wheel050Name)
        let cancelTask = Task { await vmAbortStage.performOpportunisticCheckIfNeeded(trigger: .panelOpened) }
        await gatedFetcher.waitForGatedRequest()
        cancelTask.cancel()
        gatedFetcher.resumeGatedRequest(with: (wheel050, 200))
        await cancelTask.value
        XCTAssertNil(vmAbortStage.stagedManifest)
        XCTAssertFalse(FileManager.default.fileExists(atPath: staged050Dir.path))
    }

    @MainActor
    func testSkipSelfReplaceWithNonNilBundleOnRelaunchFailedAndCLIFallbackBeforeProbe() async throws {
        final class FailingRelaunchExecutor: RelaunchExecuting, @unchecked Sendable {
            struct Failure: LocalizedError {
                var errorDescription: String? { "open failed" }
            }

            @MainActor
            func relaunch(bundleURL: URL) throws {
                throw Failure()
            }
        }

        let (defaults, suiteName) = makeIsolatedDefaults()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-agy-skipreplace-\(UUID().uuidString)", isDirectory: true)
        let cliBin = tempRoot.appendingPathComponent("codex-agy-sync")
        let installedApp = tempRoot.appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
        let updatesRoot = tempRoot.appendingPathComponent("updates", isDirectory: true)
        let staged050 = updatesRoot.appendingPathComponent("staged/0.5.0", isDirectory: true)
        let extractedApp = staged050.appendingPathComponent("extracted/CodexAgyHandoff.app", isDirectory: true)
        try FileManager.default.createDirectory(at: installedApp, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: extractedApp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        let shebangPython = tempRoot.appendingPathComponent("python3")
        try "#!/bin/sh\nexit 0\n".write(to: shebangPython, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: shebangPython.path)
        try "#!\(shebangPython.path)\n".write(to: cliBin, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cliBin.path)

        let wheelBytes = Data("wheel-skipreplace".utf8)
        let wheelName = "codex_agy_sync-0.5.0-py3-none-any.whl"
        try wheelBytes.write(to: staged050.appendingPathComponent(wheelName))
        let manifest = StagedUpdateManifest(
            tagName: "v0.5.0",
            version: "0.5.0",
            notesSummary: "",
            wheelFileName: wheelName,
            wheelSHA256: SHA256Verifier.sha256Hex(of: wheelBytes),
            appBundleName: "CodexAgyHandoff.app",
            stagedAtEpoch: 1_795_000_000
        )
        try JSONEncoder().encode(manifest).write(to: staged050.appendingPathComponent("stage-manifest.json"))

        let runner = StubCommandRunner { _, args in
            if args == ["--version"] {
                return CommandExecutionResult(exitCode: 0, stdout: Data("codex-agy-sync 0.5.0\n".utf8), stderr: Data())
            }
            if args.first == "-c" {
                let pyOut = """
                {"executable":"\(shebangPython.path)","in_venv":true,"editable":false,"repo_path":null}
                """
                return CommandExecutionResult(exitCode: 0, stdout: Data(pyOut.utf8), stderr: Data())
            }
            return CommandExecutionResult(exitCode: 0, stdout: Data(), stderr: Data())
        }

        let relaunchMock = StubRelaunchExecutor()
        let prefs = PreferencesStore(defaults: defaults, loginItemManager: NoOpLoginItemManager())
        let vm = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            currentAppBundleURL: installedApp,
            environment: [
                "CODEX_AGY_SYNC_BIN": cliBin.path,
                "PATH": "/nonexistent",
                UpdateViewModel.skipSelfReplaceEnvKey: "1",
            ],
            commandRunner: runner,
            httpFetcher: StubHTTPFetcher(routes: [:]),
            relaunchExecutor: relaunchMock,
            stagingRootOverride: updatesRoot
        )

        await vm.installStagedAndRelaunch()
        XCTAssertTrue(relaunchMock.relaunchedURLs.isEmpty)
        XCTAssertTrue(vm.canRelaunchApp)
        XCTAssertEqual(vm.pendingRelaunchVersion, "0.5.0")
        XCTAssertTrue(runner.invocations.contains { $0.executablePath == shebangPython.path && $0.arguments.first == "-c" })

        var relaunchFailedCalls = 0
        let vmFailRelaunch = UpdateViewModel(
            preferencesStore: prefs,
            appVersion: "0.4.1",
            currentAppBundleURL: installedApp,
            environment: ["CODEX_AGY_SYNC_BIN": cliBin.path],
            commandRunner: runner,
            httpFetcher: StubHTTPFetcher(routes: [:]),
            relaunchExecutor: FailingRelaunchExecutor(),
            stagingRootOverride: updatesRoot,
            onRelaunchFailed: { relaunchFailedCalls += 1 }
        )
        XCTAssertFalse(vmFailRelaunch.relaunchApp())
        XCTAssertEqual(relaunchFailedCalls, 1)
        if case .failed(let msg) = vmFailRelaunch.availability {
            XCTAssertTrue(msg.contains("open failed"))
        } else {
            XCTFail("Expected .failed after relaunch failure")
        }
    }
}
