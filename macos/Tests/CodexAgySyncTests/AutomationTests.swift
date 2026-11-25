import Foundation
import os
import XCTest
@testable import CodexAgySyncCore

private struct AutomationTestSandbox {
    let rootURL: URL
    let homeURL: URL
    let binDirURL: URL
    let agentsSkillsRootURL: URL
    let claudeSkillsRootURL: URL
    let helpersV1URL: URL
    let helpersV2URL: URL

    static func create(label: String) throws -> AutomationTestSandbox {
        let repoRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let rootURL = repoRoot
            .appendingPathComponent("out", isDirectory: true)
            .appendingPathComponent("test-automation-sandboxes", isDirectory: true)
            .appendingPathComponent("\(label)-\(UUID().uuidString)", isDirectory: true)
        let homeURL = rootURL.appendingPathComponent("home", isDirectory: true)
        let binDirURL = homeURL.appendingPathComponent(".local/bin", isDirectory: true)
        let agentsSkillsRootURL = homeURL.appendingPathComponent(".agents/skills", isDirectory: true)
        let claudeSkillsRootURL = homeURL.appendingPathComponent(".claude/skills", isDirectory: true)

        let helpersV1URL = rootURL
            .appendingPathComponent("bundle-v1/Contents/Helpers/codex-agy-sync", isDirectory: false)
        let helpersV2URL = rootURL
            .appendingPathComponent("bundle-v2/Contents/Helpers/codex-agy-sync", isDirectory: false)

        let fm = FileManager.default
        try fm.createDirectory(at: homeURL, withIntermediateDirectories: true)
        try createFakeExecutable(at: helpersV1URL, version: "0.4.2")
        try createFakeExecutable(at: helpersV2URL, version: "0.4.3")

        return AutomationTestSandbox(
            rootURL: rootURL,
            homeURL: homeURL,
            binDirURL: binDirURL,
            agentsSkillsRootURL: agentsSkillsRootURL,
            claudeSkillsRootURL: claudeSkillsRootURL,
            helpersV1URL: helpersV1URL,
            helpersV2URL: helpersV2URL
        )
    }

    private static func createFakeExecutable(at url: URL, version: String) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let script = "#!/bin/sh\necho \"codex-agy-sync \(version)\"\n"
        try script.write(to: url, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
    }

    func cleanup() {
        let fm = FileManager.default
        try? fm.removeItem(at: rootURL)
        let parent = rootURL.deletingLastPathComponent()
        if let remaining = try? fm.contentsOfDirectory(atPath: parent.path), remaining.isEmpty {
            try? fm.removeItem(at: parent)
        }
    }
}

final class AutomationTests: XCTestCase {
    func testCLIFreshInstallUpdateAndUninstall() throws {
        let sandbox = try AutomationTestSandbox.create(label: "cli-lifecycle")
        defer { sandbox.cleanup() }

        let activeTarget = OSAllocatedUnfairLock<URL?>(initialState: sandbox.helpersV1URL)
        let service = CLIInstallService(
            installDirectoryURL: sandbox.binDirURL,
            bundledCLIResolver: { activeTarget.withLock { $0 } },
            environment: ["HOME": sandbox.homeURL.path, "PATH": "/usr/bin:/bin"]
        )

        let initial = service.inspect()
        XCTAssertEqual(initial.status, .notInstalled)
        XCTAssertFalse(initial.isInstallDirectoryOnPATH)
        XCTAssertEqual(initial.bundledCLIPath, sandbox.helpersV1URL.standardizedFileURL.path)

        let installed = try service.installOrUpdate()
        XCTAssertEqual(installed.status, .installed(targetPath: sandbox.helpersV1URL.standardizedFileURL.path))
        let rawDestV1 = try FileManager.default.destinationOfSymbolicLink(atPath: service.installURL.path)
        XCTAssertEqual(rawDestV1, sandbox.helpersV1URL.standardizedFileURL.path)

        let idempotent = try service.installOrUpdate()
        XCTAssertEqual(idempotent.status, .installed(targetPath: sandbox.helpersV1URL.standardizedFileURL.path))

        activeTarget.withLock { $0 = sandbox.helpersV2URL }
        let outdated = service.inspect()
        XCTAssertEqual(
            outdated.status,
            .outdatedSymlink(
                currentTarget: sandbox.helpersV1URL.standardizedFileURL.path,
                expectedTarget: sandbox.helpersV2URL.standardizedFileURL.path
            )
        )

        let updated = try service.installOrUpdate()
        XCTAssertEqual(updated.status, .installed(targetPath: sandbox.helpersV2URL.standardizedFileURL.path))
        let rawDestV2 = try FileManager.default.destinationOfSymbolicLink(atPath: service.installURL.path)
        XCTAssertEqual(rawDestV2, sandbox.helpersV2URL.standardizedFileURL.path)

        let uninstalled = try service.uninstall()
        XCTAssertEqual(uninstalled.status, .notInstalled)
        XCTAssertFalse(FileManager.default.fileExists(atPath: service.installURL.path))

        let secondUninstall = try service.uninstall()
        XCTAssertEqual(secondUninstall.status, .notInstalled)
    }

    func testCLIConflictNonManagedFileAndForeignSymlinkRefused() throws {
        let sandbox = try AutomationTestSandbox.create(label: "cli-conflict")
        defer { sandbox.cleanup() }

        let fm = FileManager.default
        try fm.createDirectory(at: sandbox.binDirURL, withIntermediateDirectories: true)

        let service = CLIInstallService(
            installDirectoryURL: sandbox.binDirURL,
            bundledCLIResolver: { sandbox.helpersV2URL },
            environment: ["HOME": sandbox.homeURL.path, "PATH": sandbox.binDirURL.path]
        )

        let customContent = "#!/bin/sh\necho custom-user-binary\n"
        try customContent.write(to: service.installURL, atomically: true, encoding: .utf8)

        let fileConflict = service.inspect()
        XCTAssertEqual(fileConflict.status, .conflictNonManaged(existingPath: service.installURL.standardizedFileURL.path))

        XCTAssertThrowsError(try service.installOrUpdate()) { error in
            XCTAssertEqual(error as? CLIInstallError, .conflictNonManaged(service.installURL.standardizedFileURL.path))
        }
        XCTAssertThrowsError(try service.uninstall()) { error in
            XCTAssertEqual(error as? CLIInstallError, .conflictNonManaged(service.installURL.standardizedFileURL.path))
        }
        XCTAssertEqual(try String(contentsOf: service.installURL, encoding: .utf8), customContent)

        try fm.removeItem(at: service.installURL)
        let foreignTool = sandbox.rootURL.appendingPathComponent("foreign-bin/codex-agy-sync")
        try fm.createDirectory(at: foreignTool.deletingLastPathComponent(), withIntermediateDirectories: true)
        try "foreign".write(to: foreignTool, atomically: true, encoding: .utf8)
        try fm.createSymbolicLink(atPath: service.installURL.path, withDestinationPath: foreignTool.path)

        let symlinkConflict = service.inspect()
        XCTAssertEqual(symlinkConflict.status, .conflictNonManaged(existingPath: service.installURL.standardizedFileURL.path))
        XCTAssertThrowsError(try service.installOrUpdate()) { error in
            XCTAssertEqual(error as? CLIInstallError, .conflictNonManaged(service.installURL.standardizedFileURL.path))
        }
        XCTAssertThrowsError(try service.uninstall()) { error in
            XCTAssertEqual(error as? CLIInstallError, .conflictNonManaged(service.installURL.standardizedFileURL.path))
        }
        XCTAssertEqual(try fm.destinationOfSymbolicLink(atPath: service.installURL.path), foreignTool.path)
    }

    func testCLIBundledMissingAndPATHDetection() throws {
        let sandbox = try AutomationTestSandbox.create(label: "cli-path")
        defer { sandbox.cleanup() }

        let missingService = CLIInstallService(
            installDirectoryURL: sandbox.binDirURL,
            bundledCLIResolver: { nil },
            environment: ["HOME": sandbox.homeURL.path, "PATH": "/usr/bin:~/.local/bin/"]
        )
        let state = missingService.inspect()
        XCTAssertEqual(state.status, .bundledCLIMissing)
        XCTAssertTrue(state.isInstallDirectoryOnPATH)
        XCTAssertThrowsError(try missingService.installOrUpdate()) { error in
            XCTAssertEqual(error as? CLIInstallError, .bundledCLIMissing)
        }

        XCTAssertTrue(
            CLIInstallService.isDirectoryOnPATH(
                sandbox.binDirURL.path,
                environment: ["HOME": sandbox.homeURL.path, "PATH": "/usr/local/bin:~/.local/bin"]
            )
        )
        XCTAssertFalse(
            CLIInstallService.isDirectoryOnPATH(
                sandbox.binDirURL.path,
                environment: ["HOME": sandbox.homeURL.path, "PATH": "/usr/bin:/bin"]
            )
        )
    }

    func testAgentSkillFreshInstallAndValidYAMLFrontmatter() throws {
        let sandbox = try AutomationTestSandbox.create(label: "skill-fresh")
        defer { sandbox.cleanup() }

        let service = AgentSkillInstallService(
            canonicalSkillsRootURL: sandbox.agentsSkillsRootURL,
            clientLinkRootURLs: [sandbox.claudeSkillsRootURL],
            renderContext: AgentSkillRenderContext(appVersion: "0.4.3")
        )

        XCTAssertEqual(service.inspect().status, .notInstalled)

        let installed = try service.installFresh()
        XCTAssertEqual(installed.status, .installedUpToDate)

        let skillMDURL = service.canonicalDirectoryURL.appendingPathComponent("SKILL.md")
        let contractURL = service.canonicalDirectoryURL.appendingPathComponent("references/contract.md")
        let troubleshootingURL = service.canonicalDirectoryURL.appendingPathComponent("references/troubleshooting.md")

        let skillMD = try String(contentsOf: skillMDURL, encoding: .utf8)
        let contractMD = try String(contentsOf: contractURL, encoding: .utf8)
        let troubleshootingMD = try String(contentsOf: troubleshootingURL, encoding: .utf8)

        XCTAssertTrue(skillMD.hasPrefix("---\nname: codex-agy-sync\ndescription: "))
        XCTAssertTrue(skillMD.contains("\n---\n"))
        XCTAssertTrue(skillMD.contains("~/.local/bin/codex-agy-sync"))
        XCTAssertTrue(skillMD.contains("jq"))
        XCTAssertTrue(skillMD.contains("references/contract.md"))
        XCTAssertTrue(skillMD.contains("references/troubleshooting.md"))

        XCTAssertTrue(contractMD.contains("menubar-feed"))
        XCTAssertTrue(contractMD.contains("sync-workspace"))
        XCTAssertTrue(contractMD.contains("pairing_mode"))

        XCTAssertTrue(troubleshootingMD.contains("codex-agy-sync status --workspace ."))
        XCTAssertTrue(troubleshootingMD.contains("unpaired_workspace"))
        XCTAssertTrue(troubleshootingMD.contains("sync_busy_lock"))
        XCTAssertTrue(troubleshootingMD.contains("--reconcile --dry-run"))

        let claudeLinkURL = sandbox.claudeSkillsRootURL.appendingPathComponent("codex-agy-sync")
        let dest = try FileManager.default.destinationOfSymbolicLink(atPath: claudeLinkURL.path)
        XCTAssertEqual(dest, service.canonicalDirectoryURL.standardizedFileURL.path)
    }

    @MainActor
    func testAgentSkillLocalEditDiffReviewPreventsSilentOverwriteUntilConfirmed() throws {
        let sandbox = try AutomationTestSandbox.create(label: "skill-diff")
        defer { sandbox.cleanup() }

        let cliService = CLIInstallService(
            installDirectoryURL: sandbox.binDirURL,
            bundledCLIResolver: { sandbox.helpersV2URL },
            environment: ["HOME": sandbox.homeURL.path, "PATH": sandbox.binDirURL.path]
        )
        let skillService = AgentSkillInstallService(
            canonicalSkillsRootURL: sandbox.agentsSkillsRootURL,
            clientLinkRootURLs: [sandbox.claudeSkillsRootURL],
            renderContext: AgentSkillRenderContext(appVersion: "0.4.3")
        )
        let viewModel = AutomationViewModel(
            cliService: cliService,
            skillService: skillService,
            clipboardCopier: { _ in },
            languageProvider: { .en }
        )

        XCTAssertTrue(viewModel.showsPreInstallIntro)
        viewModel.installCLIAndSkill()
        XCTAssertFalse(viewModel.showsPreInstallIntro)
        XCTAssertEqual(viewModel.skillState.status, .installedUpToDate)
        XCTAssertNil(viewModel.pendingDiffReview)

        let skillMDURL = skillService.canonicalDirectoryURL.appendingPathComponent("SKILL.md")
        let original = try String(contentsOf: skillMDURL, encoding: .utf8)
        let edited = original.replacingOccurrences(
            of: "Generated by CodexAgySync v0.4.3.",
            with: "Custom local header line."
        ) + "\nExtra local note.\n"
        try edited.write(to: skillMDURL, atomically: true, encoding: .utf8)

        let inspected = skillService.inspect()
        guard case .updateAvailable(let diffs) = inspected.status else {
            return XCTFail("Expected .updateAvailable, got \(inspected.status)")
        }
        XCTAssertEqual(diffs.map(\.relativePath), ["SKILL.md"])
        let diff = try XCTUnwrap(diffs.first)
        XCTAssertTrue(diff.lines.contains { $0.kind == .removed && $0.text == "Custom local header line." })
        XCTAssertTrue(diff.lines.contains { $0.kind == .added && $0.text == "Generated by CodexAgySync v0.4.3." })
        XCTAssertTrue(diff.lines.contains { $0.kind == .removed && $0.text == "Extra local note." })

        viewModel.installCLIAndSkill()
        XCTAssertNotNil(viewModel.pendingDiffReview)
        XCTAssertEqual(viewModel.pendingDiffReview?.diffs.count, 1)
        XCTAssertEqual(try String(contentsOf: skillMDURL, encoding: .utf8), edited)

        viewModel.dismissDiffReview()
        XCTAssertNil(viewModel.pendingDiffReview)
        XCTAssertEqual(try String(contentsOf: skillMDURL, encoding: .utf8), edited)

        viewModel.installOrReviewSkill()
        XCTAssertNotNil(viewModel.pendingDiffReview)

        viewModel.confirmDiffReviewAndApply()
        XCTAssertNil(viewModel.pendingDiffReview)
        XCTAssertEqual(viewModel.skillState.status, .installedUpToDate)
        XCTAssertEqual(try String(contentsOf: skillMDURL, encoding: .utf8), original)
    }

    @MainActor
    func testAgentSkillBrokenClientLinksDetectionAndRepair() throws {
        let sandbox = try AutomationTestSandbox.create(label: "skill-repair")
        defer { sandbox.cleanup() }

        let cliService = CLIInstallService(
            installDirectoryURL: sandbox.binDirURL,
            bundledCLIResolver: { sandbox.helpersV2URL },
            environment: ["HOME": sandbox.homeURL.path, "PATH": sandbox.binDirURL.path]
        )
        let skillService = AgentSkillInstallService(
            canonicalSkillsRootURL: sandbox.agentsSkillsRootURL,
            clientLinkRootURLs: [sandbox.claudeSkillsRootURL],
            renderContext: AgentSkillRenderContext(appVersion: "0.4.3")
        )
        let viewModel = AutomationViewModel(
            cliService: cliService,
            skillService: skillService,
            clipboardCopier: { _ in },
            languageProvider: { .en }
        )

        _ = try skillService.installFresh()
        let claudeLinkURL = sandbox.claudeSkillsRootURL.appendingPathComponent("codex-agy-sync")
        try FileManager.default.removeItem(at: claudeLinkURL)

        viewModel.refresh()
        XCTAssertEqual(
            viewModel.skillState.status,
            .installedBrokenLinks(brokenPaths: [claudeLinkURL.standardizedFileURL.path])
        )

        viewModel.repairSkillLinks()
        XCTAssertEqual(viewModel.skillState.status, .installedUpToDate)
        let restoredDest = try FileManager.default.destinationOfSymbolicLink(atPath: claudeLinkURL.path)
        XCTAssertEqual(restoredDest, skillService.canonicalDirectoryURL.standardizedFileURL.path)

        try FileManager.default.removeItem(at: claudeLinkURL)
        let staleTarget = sandbox.rootURL.appendingPathComponent("old-home/.agents/skills/codex-agy-sync").path
        try FileManager.default.createSymbolicLink(atPath: claudeLinkURL.path, withDestinationPath: staleTarget)

        let brokenAgain = skillService.inspect()
        XCTAssertEqual(
            brokenAgain.status,
            .installedBrokenLinks(brokenPaths: [claudeLinkURL.standardizedFileURL.path])
        )
        let repaired = try skillService.repairLinks()
        XCTAssertEqual(repaired.status, .installedUpToDate)
    }

    func testAgentSkillExternallyManagedSymlinkNeverOverwrittenOrDeleted() throws {
        let sandbox = try AutomationTestSandbox.create(label: "skill-external")
        defer { sandbox.cleanup() }

        let fm = FileManager.default
        let externalSkillDir = sandbox.rootURL.appendingPathComponent("managed-store/codex-agy-sync", isDirectory: true)
        try fm.createDirectory(at: externalSkillDir, withIntermediateDirectories: true)
        let sentinelFile = externalSkillDir.appendingPathComponent("SKILL.md")
        let sentinelText = "managed-by-skills-toml\n"
        try sentinelText.write(to: sentinelFile, atomically: true, encoding: .utf8)

        try fm.createDirectory(at: sandbox.agentsSkillsRootURL, withIntermediateDirectories: true)
        let canonicalURL = sandbox.agentsSkillsRootURL.appendingPathComponent("codex-agy-sync")
        try fm.createSymbolicLink(atPath: canonicalURL.path, withDestinationPath: externalSkillDir.path)

        let service = AgentSkillInstallService(
            canonicalSkillsRootURL: sandbox.agentsSkillsRootURL,
            clientLinkRootURLs: [sandbox.claudeSkillsRootURL],
            renderContext: AgentSkillRenderContext(appVersion: "0.4.3")
        )

        let inspected = service.inspect()
        XCTAssertEqual(
            inspected.status,
            .externallyManaged(path: canonicalURL.standardizedFileURL.path, destination: externalSkillDir.path)
        )

        XCTAssertThrowsError(try service.installFresh()) { error in
            XCTAssertEqual(error as? AgentSkillInstallError, .externallyManaged(canonicalURL.standardizedFileURL.path))
        }
        XCTAssertThrowsError(try service.applyUpdateAfterDiffReview()) { error in
            XCTAssertEqual(error as? AgentSkillInstallError, .externallyManaged(canonicalURL.standardizedFileURL.path))
        }
        XCTAssertThrowsError(try service.repairLinks()) { error in
            XCTAssertEqual(error as? AgentSkillInstallError, .externallyManaged(canonicalURL.standardizedFileURL.path))
        }
        XCTAssertThrowsError(try service.uninstall()) { error in
            XCTAssertEqual(error as? AgentSkillInstallError, .externallyManaged(canonicalURL.standardizedFileURL.path))
        }

        XCTAssertEqual(try fm.destinationOfSymbolicLink(atPath: canonicalURL.path), externalSkillDir.path)
        XCTAssertEqual(try String(contentsOf: sentinelFile, encoding: .utf8), sentinelText)
    }

    func testAgentSkillConflictNonManagedAndSelectiveUninstall() throws {
        let sandbox = try AutomationTestSandbox.create(label: "skill-conflict-uninstall")
        defer { sandbox.cleanup() }

        let fm = FileManager.default
        let claudeLinkURL = sandbox.claudeSkillsRootURL.appendingPathComponent("codex-agy-sync", isDirectory: true)
        try fm.createDirectory(at: claudeLinkURL, withIntermediateDirectories: true)

        let service = AgentSkillInstallService(
            canonicalSkillsRootURL: sandbox.agentsSkillsRootURL,
            clientLinkRootURLs: [sandbox.claudeSkillsRootURL],
            renderContext: AgentSkillRenderContext(appVersion: "0.4.3")
        )

        XCTAssertEqual(
            service.inspect().status,
            .conflictNonManaged(path: claudeLinkURL.standardizedFileURL.path)
        )
        XCTAssertThrowsError(try service.installFresh()) { error in
            XCTAssertEqual(error as? AgentSkillInstallError, .conflictNonManaged(claudeLinkURL.standardizedFileURL.path))
        }

        try fm.removeItem(at: claudeLinkURL)
        _ = try service.installFresh()

        let userNoteURL = service.canonicalDirectoryURL.appendingPathComponent("custom-notes.md")
        try "keep me".write(to: userNoteURL, atomically: true, encoding: .utf8)

        _ = try service.uninstall()
        XCTAssertTrue(fm.fileExists(atPath: userNoteURL.path))
        XCTAssertFalse(fm.fileExists(atPath: service.canonicalDirectoryURL.appendingPathComponent("SKILL.md").path))
        XCTAssertFalse(fm.fileExists(atPath: service.canonicalDirectoryURL.appendingPathComponent("references").path))
        XCTAssertFalse(fm.fileExists(atPath: claudeLinkURL.path))

        try fm.removeItem(at: userNoteURL)
        _ = try service.installFresh()
        let fullyRemoved = try service.uninstall()
        XCTAssertEqual(fullyRemoved.status, .notInstalled)
        XCTAssertFalse(fm.fileExists(atPath: service.canonicalDirectoryURL.path))
    }

    @MainActor
    func testAutomationViewModelStateTransitionsAndCopyCLIPath() async throws {
        let sandbox = try AutomationTestSandbox.create(label: "vm-transitions")
        defer { sandbox.cleanup() }

        let copiedValues = OSAllocatedUnfairLock<[String]>(initialState: [])
        let cliService = CLIInstallService(
            installDirectoryURL: sandbox.binDirURL,
            bundledCLIResolver: { sandbox.helpersV2URL },
            environment: ["HOME": sandbox.homeURL.path, "PATH": sandbox.binDirURL.path]
        )
        let skillService = AgentSkillInstallService(
            canonicalSkillsRootURL: sandbox.agentsSkillsRootURL,
            clientLinkRootURLs: [sandbox.claudeSkillsRootURL],
            renderContext: AgentSkillRenderContext(appVersion: "0.4.3")
        )
        let viewModel = AutomationViewModel(
            cliService: cliService,
            skillService: skillService,
            clipboardCopier: { text in
                copiedValues.withLock { $0.append(text) }
            },
            languageProvider: { .zhHans },
            copiedResetDurationNanoseconds: 50_000_000
        )

        XCTAssertTrue(viewModel.showsPreInstallIntro)
        XCTAssertFalse(viewModel.justCopiedPath)

        viewModel.installCLIAndSkill()
        XCTAssertFalse(viewModel.showsPreInstallIntro)
        XCTAssertEqual(viewModel.cliState.status, .installed(targetPath: sandbox.helpersV2URL.standardizedFileURL.path))
        XCTAssertEqual(viewModel.skillState.status, .installedUpToDate)
        XCTAssertEqual(viewModel.notice?.kind, .success)

        viewModel.copyCLIPath()
        XCTAssertTrue(viewModel.justCopiedPath)
        XCTAssertEqual(copiedValues.withLock { $0 }, [cliService.installURL.standardizedFileURL.path])

        try await Task.sleep(nanoseconds: 120_000_000)
        XCTAssertFalse(viewModel.justCopiedPath)

        viewModel.uninstallCLI()
        XCTAssertEqual(viewModel.cliState.status, .notInstalled)
        XCTAssertFalse(viewModel.showsPreInstallIntro)

        viewModel.uninstallSkill()
        XCTAssertEqual(viewModel.skillState.status, .notInstalled)
        XCTAssertTrue(viewModel.showsPreInstallIntro)
    }

    @MainActor
    func testSettingsTabIncludesAutomationAndBilingualCopy() {
        let isolated = makeIsolatedTestDefaultsSuite(label: "automation-tab")
        defer { isolated.cleanup() }

        XCTAssertTrue(SettingsTab.allCases.contains(.automation))
        XCTAssertEqual(SettingsTab.automation.iconName, "terminal")
        XCTAssertEqual(SettingsTab.automation.title(in: .zhHans), "自动化")
        XCTAssertEqual(SettingsTab.automation.title(in: .en), "Automation")

        let prefs = PreferencesStore(
            defaults: isolated.defaults,
            loginItemManager: NoOpLoginItemManager()
        )
        let updateVM = UpdateViewModel(preferencesStore: prefs, appVersion: "0.4.3")
        _ = SettingsView(
            preferencesStore: prefs,
            updateViewModel: updateVM,
            initialTab: .automation
        )
    }
}
