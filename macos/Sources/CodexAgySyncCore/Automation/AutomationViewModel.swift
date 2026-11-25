import AppKit
import Foundation
import Observation
import os

public enum AutomationNoticeKind: Equatable, Sendable {
    case info
    case success
    case warning
    case error
}

public struct AutomationNotice: Equatable, Sendable {
    public let kind: AutomationNoticeKind
    public let message: String

    public init(kind: AutomationNoticeKind, message: String) {
        self.kind = kind
        self.message = message
    }
}

@MainActor
@Observable
public final class AutomationViewModel {
    public let cliService: CLIInstallService
    public let skillService: AgentSkillInstallService
    @ObservationIgnored private let clipboardCopier: @Sendable (String) -> Void
    @ObservationIgnored private let languageProvider: @MainActor () -> ResolvedLanguage
    @ObservationIgnored private let copiedResetDurationNanoseconds: UInt64
    @ObservationIgnored private nonisolated let copiedResetTaskLock = OSAllocatedUnfairLock<Task<Void, Never>?>(initialState: nil)

    public private(set) var cliState: CLIInstallState
    public private(set) var skillState: AgentSkillInstallState
    public var pendingDiffReview: AgentSkillDiffReview?
    public private(set) var notice: AutomationNotice?
    public private(set) var justCopiedPath: Bool = false

    public var showsPreInstallIntro: Bool {
        cliState.status == .notInstalled && skillState.status == .notInstalled
    }

    public init(
        cliService: CLIInstallService = CLIInstallService(),
        skillService: AgentSkillInstallService = AgentSkillInstallService(),
        clipboardCopier: @escaping @Sendable (String) -> Void = { text in
            MainActor.assumeIsolated {
                let pb = NSPasteboard.general
                pb.clearContents()
                pb.setString(text, forType: .string)
            }
        },
        languageProvider: @escaping @MainActor () -> ResolvedLanguage = { .zhHans },
        copiedResetDurationNanoseconds: UInt64 = 2_000_000_000
    ) {
        self.cliService = cliService
        self.skillService = skillService
        self.clipboardCopier = clipboardCopier
        self.languageProvider = languageProvider
        self.copiedResetDurationNanoseconds = copiedResetDurationNanoseconds
        self.cliState = cliService.inspect()
        self.skillState = skillService.inspect()
    }

    public convenience init(
        preferencesStore: PreferencesStore,
        appVersion: String,
        bundleURL: URL = Bundle.main.bundleURL,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        let cliService = CLIInstallService(
            bundledCLIResolver: {
                CLIInstallService.defaultBundledCLIURL(bundleURL: bundleURL)
            },
            environment: environment
        )
        let skillService = AgentSkillInstallService(
            renderContext: AgentSkillRenderContext(appVersion: appVersion)
        )
        self.init(
            cliService: cliService,
            skillService: skillService,
            languageProvider: { [weak preferencesStore] in
                preferencesStore?.effectiveLanguage ?? .zhHans
            }
        )
    }

    deinit {
        copiedResetTaskLock.withLock {
            $0?.cancel()
            $0 = nil
        }
    }

    private var lang: ResolvedLanguage {
        languageProvider()
    }

    public func refresh() {
        cliState = cliService.inspect()
        skillState = skillService.inspect()
        if case .updateAvailable = skillState.status {
            return
        }
        pendingDiffReview = nil
    }

    public func dismissNotice() {
        notice = nil
    }

    public func installCLIAndSkill() {
        var cliError: CLIInstallError?
        let currentCLI = cliService.inspect()
        switch currentCLI.status {
        case .notInstalled, .outdatedSymlink:
            do {
                cliState = try cliService.installOrUpdate()
            } catch let error as CLIInstallError {
                cliError = error
                cliState = cliService.inspect()
            } catch {
                cliError = .filesystemError(error.localizedDescription)
                cliState = cliService.inspect()
            }
        case .bundledCLIMissing:
            cliState = currentCLI
            cliError = .bundledCLIMissing
        case .conflictNonManaged(let path):
            cliState = currentCLI
            cliError = .conflictNonManaged(path)
        case .installed:
            cliState = currentCLI
        }

        do {
            let nextSkill = try skillService.installFresh()
            skillState = nextSkill
            if case .updateAvailable(let diffs) = nextSkill.status {
                pendingDiffReview = AgentSkillDiffReview(diffs: diffs)
                if let cliError {
                    notice = makeCLINotice(for: cliError)
                } else {
                    notice = AutomationNotice(
                        kind: .info,
                        message: AppCopy.text(.automationNoticeReviewPending, in: lang)
                    )
                }
                return
            }
            pendingDiffReview = nil
            if let cliError {
                notice = makeCLINotice(for: cliError)
            } else {
                notice = AutomationNotice(
                    kind: .success,
                    message: AppCopy.text(.automationNoticeBothInstalled, in: lang)
                )
            }
        } catch let error as AgentSkillInstallError {
            skillState = skillService.inspect()
            notice = makeSkillNotice(for: error)
        } catch {
            skillState = skillService.inspect()
            notice = AutomationNotice(
                kind: .error,
                message: AppCopy.automationNoticeFilesystemError(error.localizedDescription, in: lang)
            )
        }
    }

    public func installOrUpdateCLI() {
        do {
            cliState = try cliService.installOrUpdate()
            notice = AutomationNotice(
                kind: .success,
                message: AppCopy.text(.automationNoticeCLIInstalled, in: lang)
            )
        } catch let error as CLIInstallError {
            cliState = cliService.inspect()
            notice = makeCLINotice(for: error)
        } catch {
            cliState = cliService.inspect()
            notice = AutomationNotice(
                kind: .error,
                message: AppCopy.automationNoticeFilesystemError(error.localizedDescription, in: lang)
            )
        }
    }

    public func uninstallCLI() {
        do {
            cliState = try cliService.uninstall()
            notice = AutomationNotice(
                kind: .info,
                message: AppCopy.text(.automationNoticeCLIUninstalled, in: lang)
            )
        } catch let error as CLIInstallError {
            cliState = cliService.inspect()
            notice = makeCLINotice(for: error)
        } catch {
            cliState = cliService.inspect()
            notice = AutomationNotice(
                kind: .error,
                message: AppCopy.automationNoticeFilesystemError(error.localizedDescription, in: lang)
            )
        }
    }

    public func copyCLIPath() {
        clipboardCopier(cliState.installPath)
        justCopiedPath = true
        let delay = copiedResetDurationNanoseconds
        copiedResetTaskLock.withLock { existing in
            existing?.cancel()
            existing = Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: delay)
                guard !Task.isCancelled else { return }
                self?.justCopiedPath = false
            }
        }
    }

    public func installOrReviewSkill() {
        do {
            let next = try skillService.installFresh()
            skillState = next
            if case .updateAvailable(let diffs) = next.status {
                pendingDiffReview = AgentSkillDiffReview(diffs: diffs)
                notice = AutomationNotice(
                    kind: .info,
                    message: AppCopy.text(.automationNoticeReviewPending, in: lang)
                )
            } else {
                pendingDiffReview = nil
                notice = AutomationNotice(
                    kind: .success,
                    message: AppCopy.text(.automationNoticeSkillInstalled, in: lang)
                )
            }
        } catch let error as AgentSkillInstallError {
            skillState = skillService.inspect()
            notice = makeSkillNotice(for: error)
        } catch {
            skillState = skillService.inspect()
            notice = AutomationNotice(
                kind: .error,
                message: AppCopy.automationNoticeFilesystemError(error.localizedDescription, in: lang)
            )
        }
    }

    public func confirmDiffReviewAndApply() {
        do {
            skillState = try skillService.applyUpdateAfterDiffReview()
            pendingDiffReview = nil
            notice = AutomationNotice(
                kind: .success,
                message: AppCopy.text(.automationNoticeSkillUpdated, in: lang)
            )
        } catch let error as AgentSkillInstallError {
            skillState = skillService.inspect()
            pendingDiffReview = nil
            notice = makeSkillNotice(for: error)
        } catch {
            skillState = skillService.inspect()
            pendingDiffReview = nil
            notice = AutomationNotice(
                kind: .error,
                message: AppCopy.automationNoticeFilesystemError(error.localizedDescription, in: lang)
            )
        }
    }

    public func dismissDiffReview() {
        pendingDiffReview = nil
    }

    public func repairSkillLinks() {
        do {
            skillState = try skillService.repairLinks()
            notice = AutomationNotice(
                kind: .success,
                message: AppCopy.text(.automationNoticeSkillLinksRepaired, in: lang)
            )
        } catch let error as AgentSkillInstallError {
            skillState = skillService.inspect()
            notice = makeSkillNotice(for: error)
        } catch {
            skillState = skillService.inspect()
            notice = AutomationNotice(
                kind: .error,
                message: AppCopy.automationNoticeFilesystemError(error.localizedDescription, in: lang)
            )
        }
    }

    public func uninstallSkill() {
        do {
            skillState = try skillService.uninstall()
            pendingDiffReview = nil
            notice = AutomationNotice(
                kind: .info,
                message: AppCopy.text(.automationNoticeSkillUninstalled, in: lang)
            )
        } catch let error as AgentSkillInstallError {
            skillState = skillService.inspect()
            notice = makeSkillNotice(for: error)
        } catch {
            skillState = skillService.inspect()
            notice = AutomationNotice(
                kind: .error,
                message: AppCopy.automationNoticeFilesystemError(error.localizedDescription, in: lang)
            )
        }
    }

    private func makeCLINotice(for error: CLIInstallError) -> AutomationNotice {
        switch error {
        case .bundledCLIMissing:
            return AutomationNotice(
                kind: .warning,
                message: AppCopy.text(.automationCLIBundledMissingHint, in: lang)
            )
        case .conflictNonManaged(let path):
            return AutomationNotice(
                kind: .warning,
                message: AppCopy.automationCLIConflictDetail(path: path, in: lang)
            )
        case .filesystemError(let detail):
            return AutomationNotice(
                kind: .error,
                message: AppCopy.automationNoticeFilesystemError(detail, in: lang)
            )
        }
    }

    private func makeSkillNotice(for error: AgentSkillInstallError) -> AutomationNotice {
        switch error {
        case .externallyManaged(let path):
            let destination: String
            if case .externallyManaged(_, let dest) = skillState.status {
                destination = dest
            } else {
                destination = ""
            }
            return AutomationNotice(
                kind: .warning,
                message: AppCopy.automationSkillExternalDetail(
                    path: path,
                    destination: destination,
                    in: lang
                )
            )
        case .conflictNonManaged(let path):
            return AutomationNotice(
                kind: .warning,
                message: AppCopy.automationSkillConflictDetail(path: path, in: lang)
            )
        case .filesystemError(let detail):
            return AutomationNotice(
                kind: .error,
                message: AppCopy.automationNoticeFilesystemError(detail, in: lang)
            )
        }
    }
}
