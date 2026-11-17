import Foundation

enum ReleaseNoticeReason: Equatable, Sendable {
    case newerVersion
    case cliMissing
    case versionMismatch
}

enum LocalSyncReason: Equatable, Sendable {
    case cliMissing
    case versionMismatch
    case remoteCheckUnavailable(UpdateFailureReason)
}

enum UpToDateMessage: Equatable, Sendable {
    case alignedWithRemote(tag: String)
    case localRepoSynced(version: String)
    case releaseSyncedWithApp(tag: String)
    case releaseSyncedCLIOnly(tag: String)
}

enum UpdateProgressStep: Equatable, Sendable {
    case buildingLocalApp
    case upgradingLocalCLI
    case downloadingChecksums
    case downloadingWheel(fileName: String)
    case downloadingAppArchive(fileName: String)
    case verifyingAppArchive
    case installingCLI
    case stagingRelease(tagName: String)
    case applyingStagedUpdate(tagName: String)

    func localizedText(in language: ResolvedLanguage) -> String {
        switch self {
        case .buildingLocalApp:
            return AppCopy.text(.updatesStepBuildingLocal, in: language)
        case .upgradingLocalCLI:
            return AppCopy.text(.updatesStepUpgradingLocalCLI, in: language)
        case .downloadingChecksums:
            return AppCopy.text(.updatesStepDownloadingChecksums, in: language)
        case .downloadingWheel(let fileName):
            return AppCopy.stepDownloadingWheel(fileName, in: language)
        case .downloadingAppArchive(let fileName):
            return AppCopy.stepDownloadingApp(fileName, in: language)
        case .verifyingAppArchive:
            return AppCopy.text(.updatesStepVerifyingApp, in: language)
        case .installingCLI:
            return AppCopy.text(.updatesStepInstallingCLI, in: language)
        case .stagingRelease(let tagName):
            return AppCopy.stepStagingRelease(tagName, in: language)
        case .applyingStagedUpdate(let tagName):
            return AppCopy.stepApplyingStaged(tagName, in: language)
        }
    }
}

enum UpdateFailureReason: Equatable, Sendable {
    case service(UpdateServiceError)
    case cliUnavailableMissingAsset(tag: String)
    case mismatchMissingAsset(tag: String)
    case bundlePathNotFound
    case relaunchFailed(String)
    case handoffInProgress
    case stagedArtifactInvalid(String)
    case message(String)

    static func from(_ error: Error) -> UpdateFailureReason {
        if let serviceErr = error as? UpdateServiceError {
            return .service(serviceErr)
        }
        if let localized = error as? LocalizedError, let text = localized.errorDescription {
            return .message(text)
        }
        return .message(error.localizedDescription)
    }

    func localizedText(in language: ResolvedLanguage) -> String {
        switch self {
        case .service(let serviceError):
            return serviceError.localizedMessage(in: language)
        case .cliUnavailableMissingAsset(let tag):
            return AppCopy.updateFailureCliUnavailableMissingAsset(tag: tag, in: language)
        case .mismatchMissingAsset(let tag):
            return AppCopy.updateFailureMismatchMissingAsset(tag: tag, in: language)
        case .bundlePathNotFound:
            return AppCopy.text(.updatesErrBundlePathMissing, in: language)
        case .relaunchFailed(let detail):
            return AppCopy.updateFailureRelaunchFailed(detail, in: language)
        case .handoffInProgress:
            return AppCopy.text(.updatesErrHandoffInProgress, in: language)
        case .stagedArtifactInvalid(let detail):
            return AppCopy.updateFailureStagedArtifactInvalid(detail, in: language)
        case .message(let raw):
            return raw
        }
    }
}

struct UpdateFailureReasonError: Error {
    let reason: UpdateFailureReason
    init(_ reason: UpdateFailureReason) {
        self.reason = reason
    }
}

enum SemanticUpdateState: Equatable, Sendable {
    case idle
    case checking
    case upToDate(currentVersion: String, message: UpToDateMessage?)
    case releaseAvailable(release: GitHubReleaseInfo, reason: ReleaseNoticeReason)
    case stagedReady(manifest: StagedUpdateManifest, stagingDir: URL)
    case localSyncReady(repoPath: String, repoVersion: String, reason: LocalSyncReason)
    case updating(step: UpdateProgressStep)
    case failed(UpdateFailureReason)
}

struct UpdateProjectedViewState: Equatable {
    let availability: UpdateAvailability
    let statusMessage: String?
    let stagedManifest: StagedUpdateManifest?
    let lastCheckedDisplayText: String?
    let lastBackgroundCheckError: String?
    let popoverNotice: PopoverUpdateNotice?
}

@MainActor
enum UpdateStateProjector {
    static func project(
        semanticState: SemanticUpdateState,
        lastCheckedEpoch: TimeInterval?,
        lastBackgroundErrorReason: UpdateFailureReason?,
        canRelaunchApp: Bool,
        pendingRelaunchVersion: String?,
        effectiveAppVersion: String,
        now: Date,
        in lang: ResolvedLanguage
    ) -> UpdateProjectedViewState {
        let lastCheckedDisplayText: String
        if let epoch = lastCheckedEpoch {
            let formatted = AppDateFormatting.formatCheckTimestamp(epoch, now: now, in: lang)
            lastCheckedDisplayText = AppCopy.lastCheckedReadout(formatted, in: lang)
        } else {
            lastCheckedDisplayText = AppCopy.lastCheckedReadout(nil, in: lang)
        }

        let lastBackgroundCheckError: String? = lastBackgroundErrorReason.map {
            AppCopy.backgroundCheckErrorReadout($0.localizedText(in: lang), in: lang)
        }

        let availability: UpdateAvailability
        let statusMessage: String?
        let stagedManifest: StagedUpdateManifest?

        switch semanticState {
        case .idle:
            availability = .idle
            statusMessage = nil
            stagedManifest = nil

        case .checking:
            availability = .checking
            statusMessage = nil
            stagedManifest = nil

        case .upToDate(let currentVersion, let message):
            availability = .upToDate(currentVersion: currentVersion)
            stagedManifest = nil
            switch message {
            case nil:
                statusMessage = nil
            case .alignedWithRemote(let tag):
                statusMessage = AppCopy.updatesStatusAlignedWithRemote(tag: tag, in: lang)
            case .localRepoSynced(let version):
                statusMessage = AppCopy.updatesStatusLocalRepoSynced(version: version, in: lang)
            case .releaseSyncedWithApp(let tag):
                statusMessage = AppCopy.updatesStatusReleaseSyncedWithApp(tag: tag, in: lang)
            case .releaseSyncedCLIOnly(let tag):
                statusMessage = AppCopy.updatesStatusReleaseSyncedCLIOnly(tag: tag, in: lang)
            }

        case .releaseAvailable(let release, let reason):
            stagedManifest = nil
            let hasApp = release.appZipAsset != nil
            let hasWheel = release.wheelAsset != nil
            let summary: String
            switch reason {
            case .newerVersion:
                summary = release.notesSummary(in: lang)
                statusMessage = AppCopy.updatesStatusNewRelease(tag: release.tagName, summary: summary, in: lang)
            case .cliMissing:
                summary = AppCopy.updatesSummaryInstallCLI(tag: release.tagName, in: lang)
                statusMessage = AppCopy.updatesStatusCLIMissing(tag: release.tagName, in: lang)
            case .versionMismatch:
                summary = AppCopy.updatesSummaryAlignVersions(tag: release.tagName, in: lang)
                statusMessage = AppCopy.updatesStatusVersionMismatch(tag: release.tagName, in: lang)
            }
            availability = .releaseAvailable(
                latestTag: release.tagName,
                notesSummary: summary,
                hasAppAsset: hasApp,
                hasWheelAsset: hasWheel
            )

        case .stagedReady(let manifest, _):
            stagedManifest = manifest
            let legacyDefaultNotes = AppCopy.text(.updatesDefaultNotesSummary, in: .zhHans)
            let notes = (manifest.notesSummary.isEmpty || manifest.notesSummary == legacyDefaultNotes)
                ? AppCopy.text(.updatesDefaultNotesSummary, in: lang)
                : manifest.notesSummary
            availability = .stagedReady(
                tagName: manifest.tagName,
                version: manifest.version,
                notesSummary: notes,
                hasAppAsset: manifest.appBundleName != nil,
                hasWheelAsset: manifest.wheelFileName != nil
            )
            statusMessage = AppCopy.updatesStatusStagedReady(tag: manifest.tagName, in: lang)

        case .localSyncReady(let repoPath, let repoVersion, let reason):
            stagedManifest = nil
            availability = .localSyncReady(repoPath: repoPath, repoVersion: repoVersion)
            switch reason {
            case .cliMissing:
                statusMessage = AppCopy.updatesStatusLocalSyncCLIMissing(version: repoVersion, in: lang)
            case .versionMismatch:
                statusMessage = AppCopy.updatesStatusLocalSyncMismatch(version: repoVersion, in: lang)
            case .remoteCheckUnavailable(let failure):
                statusMessage = AppCopy.updatesStatusLocalSyncRemoteUnavailable(
                    detail: failure.localizedText(in: lang),
                    repoVersion: repoVersion,
                    in: lang
                )
            }

        case .updating(let step):
            stagedManifest = nil
            availability = .updating(stepDescription: step.localizedText(in: lang))
            statusMessage = nil

        case .failed(let reason):
            stagedManifest = nil
            let detail = reason.localizedText(in: lang)
            availability = .failed(detail)
            statusMessage = detail
        }

        let notice = buildPopoverNotice(
            semanticState: semanticState,
            canRelaunchApp: canRelaunchApp,
            pendingRelaunchVersion: pendingRelaunchVersion,
            effectiveAppVersion: effectiveAppVersion,
            in: lang
        )

        return UpdateProjectedViewState(
            availability: availability,
            statusMessage: statusMessage,
            stagedManifest: stagedManifest,
            lastCheckedDisplayText: lastCheckedDisplayText,
            lastBackgroundCheckError: lastBackgroundCheckError,
            popoverNotice: notice
        )
    }

    static func buildPopoverNotice(
        semanticState: SemanticUpdateState,
        canRelaunchApp: Bool,
        pendingRelaunchVersion: String?,
        effectiveAppVersion: String,
        in language: ResolvedLanguage
    ) -> PopoverUpdateNotice? {
        if canRelaunchApp {
            let ver = pendingRelaunchVersion ?? effectiveAppVersion
            return PopoverUpdateNotice(
                title: AppCopy.noticeRelaunchTitle(version: ver, in: language),
                actionTitle: AppCopy.text(.updatesNoticeRelaunchAction, in: language),
                action: .relaunchInstalled(version: ver)
            )
        }
        switch semanticState {
        case .stagedReady(let manifest, _):
            return PopoverUpdateNotice(
                title: AppCopy.noticeStagedTitle(tagName: manifest.tagName, in: language),
                actionTitle: AppCopy.text(.updatesNoticeInstallStagedAction, in: language),
                action: .installStagedAndRelaunch(tag: manifest.tagName)
            )
        case .releaseAvailable(let release, _):
            return PopoverUpdateNotice(
                title: AppCopy.noticeReleaseAvailableTitle(tagName: release.tagName, in: language),
                actionTitle: AppCopy.text(.updatesNoticeOpenSettingsAction, in: language),
                action: .openUpdatesSettings(tag: release.tagName)
            )
        case .localSyncReady(_, let repoVersion, _):
            return PopoverUpdateNotice(
                title: AppCopy.noticeLocalSyncTitle(version: repoVersion, in: language),
                actionTitle: AppCopy.text(.updatesNoticeOpenSettingsAction, in: language),
                action: .openUpdatesSettings(tag: "v\(repoVersion)")
            )
        case .idle, .checking, .upToDate, .updating, .failed:
            return nil
        }
    }
}
