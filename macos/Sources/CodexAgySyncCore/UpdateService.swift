import AppKit
import Combine
import Foundation

private enum ReleaseNoticeReason: Equatable {
    case newerVersion
    case cliMissing
    case versionMismatch
}

private enum LocalSyncReason: Equatable {
    case cliMissing
    case versionMismatch
    case remoteCheckUnavailable(UpdateFailureReason)
}

private enum UpToDateMessage: Equatable {
    case alignedWithRemote(tag: String)
    case localRepoSynced(version: String)
    case releaseSyncedWithApp(tag: String)
    case releaseSyncedCLIOnly(tag: String)
}

private enum UpdateProgressStep: Equatable {
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

private enum UpdateFailureReason: Equatable {
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

private enum SemanticUpdateState: Equatable {
    case idle
    case checking
    case upToDate(currentVersion: String, message: UpToDateMessage?)
    case releaseAvailable(release: GitHubReleaseInfo, reason: ReleaseNoticeReason)
    case stagedReady(manifest: StagedUpdateManifest, stagingDir: URL)
    case localSyncReady(repoPath: String, repoVersion: String, reason: LocalSyncReason)
    case updating(step: UpdateProgressStep)
    case failed(UpdateFailureReason)
}

@MainActor
public final class UpdateViewModel: ObservableObject {
    public nonisolated static let defaultReleaseAPIURL = "https://api.github.com/repos/semantic-craft/codex-agy-sync/releases/latest"
    public nonisolated static let releaseAPIEnvKey = "MENUBAR_RELEASE_API_URL"
    public nonisolated static let skipSelfReplaceEnvKey = "MENUBAR_SKIP_SELF_REPLACE"
    public nonisolated static let checkpointDefaultsKey = "updateCheckCheckpoint"
    public nonisolated static let successCooldownSeconds: TimeInterval = UpdateCheckCheckpoint.successCooldownSeconds
    public nonisolated static let failureBackoffSeconds: TimeInterval = UpdateCheckCheckpoint.failureBackoffSeconds

    private static let stagedManifestFileName = "stage-manifest.json"

    @Published public private(set) var alignment: VersionAlignmentState
    @Published public private(set) var availability: UpdateAvailability = .idle
    @Published public private(set) var resolvedCLIPath: String?
    @Published public private(set) var cliSource: CLIResolutionSource = .missing
    @Published public private(set) var detectedRepoPath: String?
    @Published public private(set) var detectedRepoVersion: String?
    @Published public private(set) var latestRelease: GitHubReleaseInfo?
    @Published public private(set) var statusMessage: String?
    @Published public private(set) var popoverNotice: PopoverUpdateNotice?
    @Published public private(set) var lastCheckedEpoch: TimeInterval?
    @Published public private(set) var lastCheckedDisplayText: String?
    @Published public private(set) var lastBackgroundCheckError: String?
    @Published public private(set) var stagedManifest: StagedUpdateManifest?
    @Published public private(set) var canRelaunchApp: Bool = false
    @Published public private(set) var pendingRelaunchVersion: String?

    public let appVersion: String
    public let currentBundleIdentifier: String
    public let currentAppBundleURL: URL?
    public let preferencesStore: PreferencesStore

    private let environment: [String: String]
    private let commandRunner: any SystemCommandRunning
    private let httpFetcher: any ReleaseHTTPFetching
    private let relaunchExecutor: any RelaunchExecuting
    private let stagingRootOverride: URL?
    private let nowProvider: @Sendable () -> Date
    private let canMutateEnvironment: (@MainActor () -> Bool)?
    public var onPrepareForRelaunch: (@MainActor () -> Void)?
    public var onRelaunchFailed: (@MainActor () -> Void)?

    private var semanticState: SemanticUpdateState = .idle
    private var lastBackgroundErrorReason: UpdateFailureReason?
    private var hasCompletedInitialProbe = false
    private var isOpportunisticCheckInFlight = false
    private var isManualActionInFlight = false
    private var probeGeneration: UInt64 = 0
    private var opportunisticGeneration: UInt64 = 0
    private var cancellables = Set<AnyCancellable>()

    public var isInstallingUpdate: Bool {
        if case .updating(let step) = semanticState {
            if case .stagingRelease = step {
                return false
            }
            return true
        }
        return false
    }

    public var effectiveAppVersion: String {
        pendingRelaunchVersion ?? (SemanticVersion.parse(appVersion)?.description ?? appVersion)
    }

    public var effectiveRepoPath: String? {
        if let custom = preferencesStore.preferences.localRepoPathForSync,
           Self.isValidSourceRepo(URL(fileURLWithPath: custom))
        {
            return custom
        }
        return detectedRepoPath
    }

    public var effectiveRepoVersion: String? {
        if let repoPath = effectiveRepoPath {
            return Self.parsePyprojectVersion(in: URL(fileURLWithPath: repoPath)) ?? detectedRepoVersion
        }
        return nil
    }

    public init(
        preferencesStore: PreferencesStore,
        appVersion: String? = nil,
        currentBundleIdentifier: String? = nil,
        currentAppBundleURL: URL? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        commandRunner: any SystemCommandRunning = DefaultSystemCommandRunner(),
        httpFetcher: any ReleaseHTTPFetching = URLSessionReleaseHTTPFetcher(),
        relaunchExecutor: any RelaunchExecuting = DefaultRelaunchExecutor(),
        stagingRootOverride: URL? = nil,
        nowProvider: @escaping @Sendable () -> Date = Date.init,
        canMutateEnvironment: (@MainActor () -> Bool)? = nil,
        onPrepareForRelaunch: (@MainActor () -> Void)? = nil,
        onRelaunchFailed: (@MainActor () -> Void)? = nil
    ) {
        let resolvedAppVer = appVersion
            ?? (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String)
            ?? "0.4.1"
        self.appVersion = resolvedAppVer
        self.currentBundleIdentifier = currentBundleIdentifier
            ?? Bundle.main.bundleIdentifier
            ?? "dev.semanticcraft.codex-agy-sync"
        if let currentAppBundleURL {
            self.currentAppBundleURL = currentAppBundleURL
        } else if Bundle.main.bundleURL.pathExtension == "app" {
            self.currentAppBundleURL = Bundle.main.bundleURL
        } else {
            self.currentAppBundleURL = nil
        }
        self.preferencesStore = preferencesStore
        self.environment = environment
        self.commandRunner = commandRunner
        self.httpFetcher = httpFetcher
        self.relaunchExecutor = relaunchExecutor
        self.stagingRootOverride = stagingRootOverride
        self.nowProvider = nowProvider
        self.canMutateEnvironment = canMutateEnvironment
        self.onPrepareForRelaunch = onPrepareForRelaunch
        self.onRelaunchFailed = onRelaunchFailed
        self.alignment = .cliUnavailable(
            appVersion: resolvedAppVer,
            reason: AppCopy.text(.updatesNotProbed, in: preferencesStore.effectiveLanguage)
        )

        let checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
        self.lastCheckedEpoch = checkpoint.lastSuccessfulCheckEpoch

        preferencesStore.$effectiveLanguage
            .dropFirst()
            .sink { [weak self] nextLanguage in
                self?.projectPublishedState(in: nextLanguage)
            }
            .store(in: &cancellables)

        projectPublishedState()
    }

    public func probeIfNeeded() async {
        guard !hasCompletedInitialProbe else { return }
        hasCompletedInitialProbe = true
        await probeEnvironment()
    }

    public func probeEnvironment(recoverStagedFromFailure: Bool = false) async {
        hasCompletedInitialProbe = true
        probeGeneration &+= 1
        let myProbeGen = probeGeneration
        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)
        let resolved = SubprocessCLIClient.resolveExecutableWithSource(
            environment: environment,
            defaults: preferencesStore.defaults
        )
        self.cliSource = resolved.source
        self.resolvedCLIPath = resolved.url?.path

        guard let cliURL = resolved.url else {
            self.alignment = .cliUnavailable(
                appVersion: effectiveAppVersion,
                reason: AppCopy.text(.cliMissingError, in: preferencesStore.effectiveLanguage)
            )
            updateRepoDiscovery(fromCLI: nil)
            reconcilePersistedStagedRelease(recoverFromFailure: recoverStagedFromFailure)
            projectPublishedState()
            return
        }

        let nextAlignment: VersionAlignmentState
        do {
            let result = try await commandRunner.run(
                executableURL: cliURL,
                arguments: ["--version"],
                environment: childEnv,
                timeoutSeconds: 8.0
            )
            guard myProbeGen == probeGeneration else { return }
            let combined = (result.stdoutString + "\n" + result.stderrString)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if result.exitCode == 0, let parsed = SemanticVersion.parse(combined) {
                let cliVer = parsed.description
                let appTargetVer = effectiveAppVersion
                if cliVer == appTargetVer {
                    nextAlignment = .aligned(version: appTargetVer)
                } else {
                    nextAlignment = .mismatched(appVersion: appTargetVer, cliVersion: cliVer)
                }
            } else {
                let reason = combined.isEmpty ? "exit \(result.exitCode)" : combined
                nextAlignment = .cliUnavailable(appVersion: effectiveAppVersion, reason: reason)
            }
        } catch {
            guard myProbeGen == probeGeneration else { return }
            nextAlignment = .cliUnavailable(appVersion: effectiveAppVersion, reason: error.localizedDescription)
        }
        self.alignment = nextAlignment

        await discoverEditableRepoIfNeeded(
            fromCLI: cliURL,
            childEnv: childEnv,
            probeGenerationSnapshot: myProbeGen
        )
        guard myProbeGen == probeGeneration else { return }
        reconcilePersistedStagedRelease(recoverFromFailure: recoverStagedFromFailure)
        projectPublishedState()
    }

    public func checkForUpdates() async {
        guard !availability.isBusy, !isManualActionInFlight else { return }
        isManualActionInFlight = true
        defer { isManualActionInFlight = false }
        opportunisticGeneration &+= 1
        isOpportunisticCheckInFlight = false
        await performManualReleaseCheck()
    }

    public func performOpportunisticCheckIfNeeded(trigger: UpdateTrigger) async {
        await performReleaseCheck(trigger: trigger)
    }

    public func handlePolicyChanged(to policy: AutoUpdatePolicy) async {
        guard policy != .off else { return }
        guard !availability.isBusy, !isManualActionInFlight, !isOpportunisticCheckInFlight else { return }
        if policy == .autoStage,
           !canRelaunchApp,
           pendingRelaunchVersion == nil,
           effectiveRepoPath == nil,
           case .releaseAvailable(let release, let reason) = semanticState
        {
            isOpportunisticCheckInFlight = true
            let myOppGen = opportunisticGeneration
            defer {
                if myOppGen == opportunisticGeneration {
                    isOpportunisticCheckInFlight = false
                }
            }
            var checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
            checkpoint.lastAttemptEpoch = nowProvider().timeIntervalSince1970
            Self.saveCheckpoint(checkpoint, to: preferencesStore.defaults)
            await stageReleaseIfEligible(
                release,
                reason: reason,
                isOpportunistic: true,
                opportunisticGenerationSnapshot: myOppGen
            )
            return
        }
        await performOpportunisticCheckIfNeeded(trigger: .policyChanged)
    }

    private func performManualReleaseCheck() async {
        await performReleaseCheck(trigger: .manual)
    }

    private func performReleaseCheck(trigger: UpdateTrigger) async {
        let isOpportunistic = trigger.isOpportunistic
        var capturedOppGen: UInt64?

        if isOpportunistic {
            guard !availability.isBusy, !isManualActionInFlight, !isOpportunisticCheckInFlight else { return }
            let policy = preferencesStore.preferences.autoUpdatePolicy
            guard policy != .off else { return }
            guard !canRelaunchApp, pendingRelaunchVersion == nil else { return }

            let now = nowProvider().timeIntervalSince1970
            let checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
            guard Self.hasCompletedCooldown(now: now, checkpoint: checkpoint) else {
                return
            }

            isOpportunisticCheckInFlight = true
            capturedOppGen = opportunisticGeneration
        }

        defer {
            if let myOppGen = capturedOppGen, myOppGen == opportunisticGeneration {
                isOpportunisticCheckInFlight = false
            }
        }

        await probeEnvironment()
        if let myOppGen = capturedOppGen {
            guard myOppGen == opportunisticGeneration,
                  !availability.isBusy,
                  !isManualActionInFlight,
                  preferencesStore.preferences.autoUpdatePolicy != .off,
                  !canRelaunchApp,
                  pendingRelaunchVersion == nil
            else {
                return
            }
        }

        let priorState = semanticState

        if isOpportunistic, case .stagedReady = priorState {
        } else {
            setSemanticState(.checking)
        }

        let rawURL = environment[Self.releaseAPIEnvKey] ?? Self.defaultReleaseAPIURL
        guard let url = URL(string: rawURL) else {
            let err = UpdateServiceError.invalidReleaseURL(rawURL)
            handleCheckError(err, isOpportunistic: isOpportunistic, priorState: priorState)
            return
        }

        var checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
        checkpoint.lastAttemptEpoch = nowProvider().timeIntervalSince1970
        Self.saveCheckpoint(checkpoint, to: preferencesStore.defaults)

        do {
            let (data, statusCode) = try await httpFetcher.fetchData(
                from: url,
                headers: [
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "CodexAgySync/\(appVersion)",
                ],
                timeoutSeconds: 10.0
            )
            if let myOppGen = capturedOppGen {
                guard myOppGen == opportunisticGeneration else { return }
            }
            guard (200..<300).contains(statusCode) else {
                throw UpdateServiceError.httpError(statusCode: statusCode)
            }
            let release = try GitHubReleaseInfo.decode(from: data)
            self.latestRelease = release

            let successEpoch = nowProvider().timeIntervalSince1970
            checkpoint.lastSuccessfulCheckEpoch = successEpoch
            checkpoint.lastAttemptEpoch = successEpoch
            Self.saveCheckpoint(checkpoint, to: preferencesStore.defaults)
            self.lastCheckedEpoch = successEpoch
            self.lastBackgroundErrorReason = nil

            await evaluateDecodedRelease(
                release,
                isOpportunistic: isOpportunistic,
                priorState: priorState,
                opportunisticGenerationSnapshot: capturedOppGen
            )
        } catch {
            if let myOppGen = capturedOppGen {
                guard myOppGen == opportunisticGeneration else { return }
            }
            handleCheckError(error, isOpportunistic: isOpportunistic, priorState: priorState)
        }
    }

    private func evaluateDecodedRelease(
        _ release: GitHubReleaseInfo,
        isOpportunistic: Bool,
        priorState: SemanticUpdateState,
        opportunisticGenerationSnapshot: UInt64?
    ) async {
        let currentAppVer = effectiveAppVersion
        let remoteSemver = SemanticVersion.parse(release.version)
        let appSemver = SemanticVersion.parse(currentAppVer)
        let cliSemver: SemanticVersion? = {
            switch alignment {
            case .aligned(let ver):
                return SemanticVersion.parse(ver)
            case .mismatched(_, let cliVer):
                return SemanticVersion.parse(cliVer)
            case .cliUnavailable:
                return nil
            }
        }()

        let remoteIsNewerThanApp = (remoteSemver != nil && appSemver != nil)
            ? (remoteSemver! > appSemver!)
            : (release.version != currentAppVer)
        let remoteIsNewerThanCLI = (remoteSemver != nil && cliSemver != nil)
            ? (remoteSemver! > cliSemver!)
            : false

        if remoteIsNewerThanApp || remoteIsNewerThanCLI {
            await handleReleaseAvailable(
                release,
                reason: .newerVersion,
                isOpportunistic: isOpportunistic,
                priorState: priorState,
                opportunisticGenerationSnapshot: opportunisticGenerationSnapshot
            )
            return
        }

        switch alignment {
        case .cliUnavailable:
            if let repoPath = effectiveRepoPath, let repoVer = effectiveRepoVersion {
                setSemanticState(.localSyncReady(repoPath: repoPath, repoVersion: repoVer, reason: .cliMissing))
            } else if release.wheelAsset != nil {
                await handleReleaseAvailable(
                    release,
                    reason: .cliMissing,
                    isOpportunistic: isOpportunistic,
                    priorState: priorState,
                    opportunisticGenerationSnapshot: opportunisticGenerationSnapshot
                )
            } else if isOpportunistic {
                lastBackgroundErrorReason = .cliUnavailableMissingAsset(tag: release.tagName)
                restoreQuietStateAfterOpportunistic(priorState)
            } else {
                setSemanticState(.failed(.cliUnavailableMissingAsset(tag: release.tagName)))
            }

        case .mismatched:
            if let repoPath = effectiveRepoPath, let repoVer = effectiveRepoVersion {
                setSemanticState(.localSyncReady(repoPath: repoPath, repoVersion: repoVer, reason: .versionMismatch))
            } else if release.wheelAsset != nil || release.appZipAsset != nil {
                await handleReleaseAvailable(
                    release,
                    reason: .versionMismatch,
                    isOpportunistic: isOpportunistic,
                    priorState: priorState,
                    opportunisticGenerationSnapshot: opportunisticGenerationSnapshot
                )
            } else if isOpportunistic {
                lastBackgroundErrorReason = .mismatchMissingAsset(tag: release.tagName)
                restoreQuietStateAfterOpportunistic(priorState)
            } else {
                setSemanticState(.failed(.mismatchMissingAsset(tag: release.tagName)))
            }

        case .aligned:
            clearPersistedStagedDirectory()
            setSemanticState(.upToDate(currentVersion: currentAppVer, message: .alignedWithRemote(tag: release.tagName)))
        }
    }

    private func handleReleaseAvailable(
        _ release: GitHubReleaseInfo,
        reason: ReleaseNoticeReason,
        isOpportunistic: Bool,
        priorState: SemanticUpdateState,
        opportunisticGenerationSnapshot: UInt64?
    ) async {
        let normalizedVer = SemanticVersion.parse(release.version)?.description ?? release.version
        if case .stagedReady(let manifest, let stagingDir) = priorState,
           manifest.version == normalizedVer,
           verifyStagedFilesPresent(manifest: manifest, in: stagingDir)
        {
            setSemanticState(.stagedReady(manifest: manifest, stagingDir: stagingDir))
            return
        }

        setSemanticState(.releaseAvailable(release: release, reason: reason))

        if isOpportunistic,
           preferencesStore.preferences.autoUpdatePolicy == .autoStage,
           effectiveRepoPath == nil
        {
            await stageReleaseIfEligible(
                release,
                reason: reason,
                isOpportunistic: true,
                opportunisticGenerationSnapshot: opportunisticGenerationSnapshot
            )
        }
    }

    private func handleCheckError(
        _ error: Error,
        isOpportunistic: Bool,
        priorState: SemanticUpdateState
    ) {
        let failure = UpdateFailureReason.from(error)
        if isOpportunistic {
            lastBackgroundErrorReason = failure
            restoreQuietStateAfterOpportunistic(priorState)
            return
        }

        if let repoPath = effectiveRepoPath, let repoVer = effectiveRepoVersion {
            setSemanticState(
                .localSyncReady(
                    repoPath: repoPath,
                    repoVersion: repoVer,
                    reason: .remoteCheckUnavailable(failure)
                )
            )
        } else {
            setSemanticState(.failed(failure))
        }
    }

    private func restoreQuietStateAfterOpportunistic(_ priorState: SemanticUpdateState) {
        switch priorState {
        case .stagedReady, .releaseAvailable, .upToDate, .localSyncReady:
            setSemanticState(priorState)
        case .idle, .checking, .updating, .failed:
            setSemanticState(.idle)
        }
    }

    public func syncUpdateNow() async {
        guard !availability.isBusy, !isManualActionInFlight else { return }
        isManualActionInFlight = true
        defer { isManualActionInFlight = false }
        opportunisticGeneration &+= 1
        isOpportunisticCheckInFlight = false
        await performManualSyncUpdateNow()
    }

    private func performManualSyncUpdateNow() async {
        guard canMutateEnvironment?() ?? true else {
            setSemanticState(.failed(.handoffInProgress))
            return
        }

        if case .stagedReady(let manifest, let stagedDir) = semanticState {
            await performInstallStaged(manifest: manifest, stagedDir: stagedDir)
            return
        }
        reconcilePersistedStagedRelease(recoverFromFailure: true)
        if case .stagedReady(let manifest, let stagedDir) = semanticState {
            await performInstallStaged(manifest: manifest, stagedDir: stagedDir)
            return
        }

        if latestRelease == nil && effectiveRepoPath == nil {
            await performManualReleaseCheck()
            if case .failed = semanticState { return }
            if case .stagedReady(let manifest, let stagedDir) = semanticState {
                await performInstallStaged(manifest: manifest, stagedDir: stagedDir)
                return
            }
        }

        if let release = latestRelease,
           release.checksumsAsset != nil,
           release.wheelAsset != nil || release.appZipAsset != nil
        {
            let remoteSem = SemanticVersion.parse(release.version)
            let localRepoSem = effectiveRepoVersion.flatMap(SemanticVersion.parse)
            let preferRelease = effectiveRepoPath == nil
                || localRepoSem == nil
                || (remoteSem != nil && remoteSem! > localRepoSem!)
            if preferRelease {
                await performReleaseSync(release)
                return
            }
        }

        if let repoPath = effectiveRepoPath {
            await performLocalRepoSync(repoPath: repoPath)
            return
        }

        if let release = latestRelease {
            await performReleaseSync(release)
            return
        }

        setSemanticState(.failed(.service(.missingUpdateAssets)))
    }

    public func installStagedAndRelaunch() async {
        guard !availability.isBusy, !isManualActionInFlight else { return }
        isManualActionInFlight = true
        defer { isManualActionInFlight = false }
        opportunisticGeneration &+= 1
        isOpportunisticCheckInFlight = false

        guard canMutateEnvironment?() ?? true else {
            setSemanticState(.failed(.handoffInProgress))
            return
        }
        if case .stagedReady = semanticState {
        } else {
            reconcilePersistedStagedRelease(recoverFromFailure: true)
        }
        guard case .stagedReady(let manifest, let stagedDir) = semanticState else {
            await performManualSyncUpdateNow()
            return
        }
        await performInstallStaged(manifest: manifest, stagedDir: stagedDir)
    }

    private func performInstallStaged(manifest: StagedUpdateManifest, stagedDir: URL) async {
        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)
        setSemanticState(.updating(step: .applyingStagedUpdate(tagName: manifest.tagName)))

        do {
            let validatedStageDir = try UpdatePathGuard.validateNonTemporaryURL(stagedDir)
            let manifestURL = validatedStageDir.appendingPathComponent(Self.stagedManifestFileName, isDirectory: false)
            guard FileManager.default.fileExists(atPath: manifestURL.path) else {
                throw UpdateFailureReasonError(.stagedArtifactInvalid("missing manifest"))
            }

            var stagedAppURL: URL?
            if let appBundleName = manifest.appBundleName {
                try validateReplacementTargetIfNeeded()
                let extractedRoot = validatedStageDir.appendingPathComponent("extracted", isDirectory: true)
                let candidateApp: URL
                do {
                    candidateApp = try UpdatePathGuard.stagedAssetFileURL(for: appBundleName, in: extractedRoot)
                } catch {
                    throw UpdateFailureReasonError(.stagedArtifactInvalid(error.localizedDescription))
                }
                do {
                    try await verifyCodesign(of: candidateApp, childEnv: childEnv)
                } catch {
                    throw UpdateFailureReasonError(.stagedArtifactInvalid(error.localizedDescription))
                }
                stagedAppURL = candidateApp
            }

            var verifiedWheelURL: URL?
            var resolvedPyEnv: PythonRuntimeEnvironment?
            if let wheelFileName = manifest.wheelFileName {
                let candidateWheel = try UpdatePathGuard.stagedAssetFileURL(for: wheelFileName, in: validatedStageDir)
                guard FileManager.default.fileExists(atPath: candidateWheel.path) else {
                    throw UpdateFailureReasonError(.stagedArtifactInvalid("missing wheel \(wheelFileName)"))
                }
                let wheelData = try Data(contentsOf: candidateWheel)
                guard let expectedSHA = manifest.wheelSHA256?.lowercased(),
                      SHA256Verifier.sha256Hex(of: wheelData) == expectedSHA
                else {
                    throw UpdateFailureReasonError(.stagedArtifactInvalid("SHA-256 mismatch for \(wheelFileName)"))
                }
                let cliURL = resolvedCLIExecutableURL()
                resolvedPyEnv = try await resolvePythonEnvironment(for: cliURL, childEnv: childEnv)
                verifiedWheelURL = candidateWheel
            }

            if let wheelURL = verifiedWheelURL, let pyEnv = resolvedPyEnv {
                try await installWheel(at: wheelURL, pythonEnv: pyEnv, childEnv: childEnv)
            }

            if let stagedAppURL {
                try replaceRunningBundleIfPermitted(with: stagedAppURL)
                pendingRelaunchVersion = manifest.version
                canRelaunchApp = true
            }

            clearPersistedStagedDirectory()
            await probeEnvironment()

            let upToDateKind: UpToDateMessage = stagedAppURL != nil
                ? .releaseSyncedWithApp(tag: manifest.tagName)
                : .releaseSyncedCLIOnly(tag: manifest.tagName)
            setSemanticState(.upToDate(currentVersion: manifest.version, message: upToDateKind))

            if stagedAppURL != nil, environment[Self.skipSelfReplaceEnvKey] != "1" {
                _ = relaunchApp()
            }
        } catch let wrapped as UpdateFailureReasonError {
            if case .stagedArtifactInvalid = wrapped.reason {
                clearPersistedStagedDirectory()
            }
            await probeEnvironment()
            setSemanticState(.failed(wrapped.reason))
        } catch {
            await probeEnvironment()
            setSemanticState(.failed(.from(error)))
        }
    }

    public func syncFromLocalRepo() async {
        guard !availability.isBusy, !isManualActionInFlight else { return }
        isManualActionInFlight = true
        defer { isManualActionInFlight = false }
        opportunisticGeneration &+= 1
        isOpportunisticCheckInFlight = false

        guard canMutateEnvironment?() ?? true else {
            setSemanticState(.failed(.handoffInProgress))
            return
        }
        let candidate = effectiveRepoPath ?? preferencesStore.preferences.localRepoPathForSync
        guard let repoPath = candidate else {
            setSemanticState(.failed(.service(.invalidLocalRepo("unconfigured"))))
            return
        }
        await performLocalRepoSync(repoPath: repoPath)
    }

    public func clearLocalRepoPath() async {
        preferencesStore.setLocalRepoPathForSync(nil)
        detectedRepoPath = nil
        detectedRepoVersion = nil
        await probeEnvironment()
    }

    public func openStateDirectory() {
        let dirURL = Self.resolveSyncStateDirectoryURL(environment: environment)
        try? FileManager.default.createDirectory(at: dirURL, withIntermediateDirectories: true)
        let handoffsDir = dirURL.appendingPathComponent("handoffs", isDirectory: true)
        let target = FileManager.default.fileExists(atPath: handoffsDir.path) ? handoffsDir : dirURL
        NSWorkspace.shared.open(target)
    }

    @discardableResult
    public func relaunchApp() -> Bool {
        guard let bundleURL = currentAppBundleURL,
              FileManager.default.fileExists(atPath: bundleURL.path)
        else {
            setSemanticState(.failed(.bundlePathNotFound))
            return false
        }
        onPrepareForRelaunch?()
        do {
            try relaunchExecutor.relaunch(bundleURL: bundleURL)
            return true
        } catch {
            onRelaunchFailed?()
            setSemanticState(.failed(.relaunchFailed(error.localizedDescription)))
            return false
        }
    }

    private func stageReleaseIfEligible(
        _ release: GitHubReleaseInfo,
        reason: ReleaseNoticeReason,
        isOpportunistic: Bool,
        opportunisticGenerationSnapshot: UInt64? = nil
    ) async {
        guard effectiveRepoPath == nil else { return }
        guard let checksumsAsset = release.checksumsAsset,
              release.wheelAsset != nil || release.appZipAsset != nil
        else {
            return
        }

        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)
        let normalizedVer = SemanticVersion.parse(release.version)?.description ?? release.version

        do {
            if release.appZipAsset != nil {
                try validateReplacementTargetIfNeeded()
            }
            let tmpDir = try makeStagingSessionDirectory()
            defer { try? FileManager.default.removeItem(at: tmpDir) }

            setSemanticState(.updating(step: .stagingRelease(tagName: release.tagName)))
            let prepared = try await downloadAndVerifyReleaseAssets(
                release,
                checksumsAsset: checksumsAsset,
                into: tmpDir,
                childEnv: childEnv,
                reportDetailedSteps: false
            )
            if let zipURL = prepared.zipURL {
                try? FileManager.default.removeItem(at: zipURL)
            }

            if let myOppGen = opportunisticGenerationSnapshot, myOppGen != opportunisticGeneration {
                return
            }

            guard !Task.isCancelled,
                  preferencesStore.preferences.autoUpdatePolicy == .autoStage,
                  effectiveRepoPath == nil
            else {
                restoreNonStagedStateAfterAbortedStaging(release: release, reason: reason)
                return
            }

            let manifest = StagedUpdateManifest(
                tagName: release.tagName,
                version: normalizedVer,
                notesSummary: release.rawNotesSummaryForPersistence ?? "",
                wheelFileName: prepared.wheelURL?.lastPathComponent,
                wheelSHA256: prepared.wheelSHA256,
                appBundleName: prepared.extractedAppURL?.lastPathComponent,
                stagedAtEpoch: nowProvider().timeIntervalSince1970
            )
            let manifestURL = tmpDir.appendingPathComponent(Self.stagedManifestFileName, isDirectory: false)
            let encoded = try JSONEncoder().encode(manifest)
            try encoded.write(to: manifestURL, options: .atomic)

            let stagedRoot = try stagedVersionsRootURL()
            try FileManager.default.createDirectory(at: stagedRoot, withIntermediateDirectories: true)
            let versionDir = try UpdatePathGuard.stagedAssetFileURL(for: normalizedVer, in: stagedRoot)
            if FileManager.default.fileExists(atPath: versionDir.path) {
                _ = try FileManager.default.replaceItemAt(versionDir, withItemAt: tmpDir)
            } else {
                try FileManager.default.moveItem(at: tmpDir, to: versionDir)
            }
            UpdatePathGuard.pruneStagedVersions(in: stagedRoot, keepingVersion: versionDir.lastPathComponent)
            setSemanticState(.stagedReady(manifest: manifest, stagingDir: versionDir))
        } catch {
            if let myOppGen = opportunisticGenerationSnapshot, myOppGen != opportunisticGeneration {
                return
            }
            if Task.isCancelled {
                restoreNonStagedStateAfterAbortedStaging(release: release, reason: reason)
                return
            }
            if isOpportunistic {
                lastBackgroundErrorReason = .from(error)
                var checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
                checkpoint.lastSuccessfulCheckEpoch = nil
                checkpoint.lastAttemptEpoch = nowProvider().timeIntervalSince1970
                Self.saveCheckpoint(checkpoint, to: preferencesStore.defaults)
                lastCheckedEpoch = nil
                setSemanticState(.releaseAvailable(release: release, reason: reason))
            } else {
                setSemanticState(.failed(.from(error)))
            }
        }
    }

    private func restoreNonStagedStateAfterAbortedStaging(
        release: GitHubReleaseInfo,
        reason: ReleaseNoticeReason
    ) {
        if let repoPath = effectiveRepoPath, let repoVer = effectiveRepoVersion {
            switch reason {
            case .cliMissing:
                setSemanticState(.localSyncReady(repoPath: repoPath, repoVersion: repoVer, reason: .cliMissing))
                return
            case .versionMismatch:
                setSemanticState(.localSyncReady(repoPath: repoPath, repoVersion: repoVer, reason: .versionMismatch))
                return
            case .newerVersion:
                break
            }
        }
        setSemanticState(.releaseAvailable(release: release, reason: reason))
    }

    public static func resolveSyncStateDirectoryURL(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default
    ) -> URL {
        if let explicit = environment["CODEX_AGY_SYNC_STATE_DIR"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !explicit.isEmpty
        {
            return URL(fileURLWithPath: (explicit as NSString).expandingTildeInPath, isDirectory: true)
        }
        let geminiRoot: URL
        if let rawGemini = environment["GEMINI_HOME"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !rawGemini.isEmpty
        {
            geminiRoot = URL(fileURLWithPath: (rawGemini as NSString).expandingTildeInPath, isDirectory: true)
        } else {
            geminiRoot = fileManager.homeDirectoryForCurrentUser.appendingPathComponent(".gemini", isDirectory: true)
        }
        return geminiRoot.appendingPathComponent("codex-agy-sync", isDirectory: true)
    }

    public static func isValidSourceRepo(_ repoURL: URL, fileManager: FileManager = .default) -> Bool {
        guard let validatedURL = try? UpdatePathGuard.validateNonTemporaryURL(repoURL) else {
            return false
        }
        let pyproject = validatedURL.appendingPathComponent("pyproject.toml").path
        let buildScript = validatedURL.appendingPathComponent("scripts/build_menubar_app.sh").path
        return fileManager.fileExists(atPath: pyproject) && fileManager.isExecutableFile(atPath: buildScript)
    }

    private static let pyprojectVersionRegex = try! NSRegularExpression(
        pattern: #"(?m)^version\s*=\s*"([^"]+)""#
    )

    public static func parsePyprojectVersion(in repoURL: URL) -> String? {
        guard let validatedURL = try? UpdatePathGuard.validateNonTemporaryURL(repoURL) else {
            return nil
        }
        let pyprojectURL = validatedURL.appendingPathComponent("pyproject.toml")
        guard let text = try? String(contentsOf: pyprojectURL, encoding: .utf8) else {
            return nil
        }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        guard let match = pyprojectVersionRegex.firstMatch(in: text, range: range),
              let verRange = Range(match.range(at: 1), in: text)
        else {
            return nil
        }
        return String(text[verRange])
    }

    private struct UpdateFailureReasonError: Error {
        let reason: UpdateFailureReason
        init(_ reason: UpdateFailureReason) {
            self.reason = reason
        }
    }

    private struct PreparedReleaseAssets {
        let wheelURL: URL?
        let wheelSHA256: String?
        let zipURL: URL?
        let extractedAppURL: URL?
    }

    private func setSemanticState(_ next: SemanticUpdateState) {
        semanticState = next
        projectPublishedState()
    }

    private func projectPublishedState(in languageOverride: ResolvedLanguage? = nil) {
        let lang = languageOverride ?? preferencesStore.effectiveLanguage

        if case .cliUnavailable(let appVer, _) = alignment, cliSource == .missing {
            alignment = .cliUnavailable(
                appVersion: appVer,
                reason: AppCopy.text(.cliMissingError, in: lang)
            )
        }

        if let epoch = lastCheckedEpoch {
            let formatted = AppDateFormatting.formatCheckTimestamp(epoch, now: nowProvider(), in: lang)
            lastCheckedDisplayText = AppCopy.lastCheckedReadout(formatted, in: lang)
        } else {
            lastCheckedDisplayText = AppCopy.lastCheckedReadout(nil, in: lang)
        }

        if let bgErr = lastBackgroundErrorReason {
            lastBackgroundCheckError = AppCopy.backgroundCheckErrorReadout(bgErr.localizedText(in: lang), in: lang)
        } else {
            lastBackgroundCheckError = nil
        }

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

        popoverNotice = buildPopoverNotice(in: lang)
    }

    private func buildPopoverNotice(in language: ResolvedLanguage) -> PopoverUpdateNotice? {
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

    public static func loadCheckpoint(from defaults: UserDefaults) -> UpdateCheckCheckpoint {
        if let rawData = defaults.data(forKey: checkpointDefaultsKey),
           let decoded = try? JSONDecoder().decode(UpdateCheckCheckpoint.self, from: rawData)
        {
            return decoded
        }
        if let dict = defaults.dictionary(forKey: checkpointDefaultsKey),
           let data = try? JSONSerialization.data(withJSONObject: dict),
           let decoded = try? JSONDecoder().decode(UpdateCheckCheckpoint.self, from: data)
        {
            return decoded
        }
        return UpdateCheckCheckpoint()
    }

    public static func saveCheckpoint(_ checkpoint: UpdateCheckCheckpoint, to defaults: UserDefaults) {
        if let encoded = try? JSONEncoder().encode(checkpoint) {
            defaults.set(encoded, forKey: checkpointDefaultsKey)
        }
    }

    public static func hasCompletedCooldown(
        now: TimeInterval,
        checkpoint: UpdateCheckCheckpoint
    ) -> Bool {
        if let lastSuccess = checkpoint.lastSuccessfulCheckEpoch,
           now - lastSuccess < UpdateCheckCheckpoint.successCooldownSeconds
        {
            return false
        }
        if let lastAttempt = checkpoint.lastAttemptEpoch,
           now - lastAttempt < UpdateCheckCheckpoint.failureBackoffSeconds
        {
            return false
        }
        return true
    }

    private func resolvedCLIExecutableURL() -> URL? {
        if let resolvedCLIPath {
            return URL(fileURLWithPath: resolvedCLIPath)
        }
        return SubprocessCLIClient.resolveExecutableWithSource(
            environment: environment,
            defaults: preferencesStore.defaults
        ).url
    }

    private static func isVersion(_ lhs: String, atLeast rhs: String) -> Bool {
        if let lSem = SemanticVersion.parse(lhs), let rSem = SemanticVersion.parse(rhs) {
            return lSem >= rSem
        }
        return lhs == rhs
    }

    private func updatesRootURL() throws -> URL {
        if let stagingRootOverride {
            return try UpdatePathGuard.validateNonTemporaryURL(stagingRootOverride)
        }
        return try UpdatePathGuard.defaultStagingRootURL()
    }

    private func stagedVersionsRootURL() throws -> URL {
        let root = try updatesRootURL()
        return try UpdatePathGuard.validateNonTemporaryURL(
            root.appendingPathComponent("staged", isDirectory: true)
        )
    }

    private func tmpStagingRootURL() throws -> URL {
        let root = try updatesRootURL()
        return try UpdatePathGuard.validateNonTemporaryURL(
            root.appendingPathComponent("tmp", isDirectory: true)
        )
    }

    private func makeStagingSessionDirectory() throws -> URL {
        let fm = FileManager.default
        let updatesRoot = try updatesRootURL()
        try fm.createDirectory(at: updatesRoot, withIntermediateDirectories: true)
        UpdatePathGuard.pruneStaleStagingDirectories(
            in: updatesRoot,
            excludingNames: ["tmp", "staged"],
            fileManager: fm
        )
        let tmpRoot = try tmpStagingRootURL()
        try fm.createDirectory(at: tmpRoot, withIntermediateDirectories: true)
        UpdatePathGuard.pruneStaleStagingDirectories(in: tmpRoot, fileManager: fm)
        let sessionDir = tmpRoot.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let validated = try UpdatePathGuard.validateNonTemporaryURL(sessionDir)
        try fm.createDirectory(at: validated, withIntermediateDirectories: true)
        return validated
    }

    private func clearPersistedStagedDirectory() {
        guard let stagedRoot = try? stagedVersionsRootURL() else { return }
        UpdatePathGuard.pruneStagedVersions(in: stagedRoot, keepingVersion: nil)
    }

    private func verifyStagedFilesPresent(manifest: StagedUpdateManifest, in stagingDir: URL) -> Bool {
        let fm = FileManager.default
        guard let validatedDir = try? UpdatePathGuard.validateNonTemporaryURL(stagingDir) else {
            return false
        }
        let manifestURL = validatedDir.appendingPathComponent(Self.stagedManifestFileName, isDirectory: false)
        guard fm.fileExists(atPath: manifestURL.path) else {
            return false
        }
        if let appBundleName = manifest.appBundleName {
            let extractedRoot = validatedDir.appendingPathComponent("extracted", isDirectory: true)
            guard let appURL = try? UpdatePathGuard.stagedAssetFileURL(for: appBundleName, in: extractedRoot),
                  fm.fileExists(atPath: appURL.path)
            else {
                return false
            }
        }
        if let wheelName = manifest.wheelFileName {
            guard let wheelURL = try? UpdatePathGuard.stagedAssetFileURL(for: wheelName, in: validatedDir),
                  let wheelData = try? Data(contentsOf: wheelURL),
                  let expectedSHA = manifest.wheelSHA256?.lowercased(),
                  SHA256Verifier.sha256Hex(of: wheelData) == expectedSHA
            else {
                return false
            }
        }
        return true
    }

    private func isStagedManifestApplicable(_ manifest: StagedUpdateManifest) -> Bool {
        let stagedSem = SemanticVersion.parse(manifest.version)
        let appSem = SemanticVersion.parse(effectiveAppVersion)
        let cliSem: SemanticVersion? = {
            switch alignment {
            case .aligned(let ver):
                return SemanticVersion.parse(ver)
            case .mismatched(_, let cliVer):
                return SemanticVersion.parse(cliVer)
            case .cliUnavailable:
                return nil
            }
        }()

        let isNewerThanApp = (stagedSem != nil && appSem != nil)
            ? (stagedSem! > appSem!)
            : (manifest.version != effectiveAppVersion)
        if isNewerThanApp {
            return true
        }
        let appMatchesOrOlder = (stagedSem != nil && appSem != nil)
            ? (stagedSem! >= appSem!)
            : (manifest.version == effectiveAppVersion)
        guard appMatchesOrOlder, manifest.wheelFileName != nil else {
            return false
        }
        guard let cliSem else {
            return true
        }
        if let stagedSem {
            return stagedSem > cliSem
        }
        return manifest.version != cliSem.description
    }

    private func reconcilePersistedStagedRelease(recoverFromFailure: Bool = false) {
        let fm = FileManager.default
        if canRelaunchApp || pendingRelaunchVersion != nil {
            clearPersistedStagedDirectory()
            return
        }
        switch semanticState {
        case .checking, .updating:
            return
        default:
            break
        }
        if effectiveRepoPath != nil {
            if case .stagedReady = semanticState {
                semanticState = .idle
            }
            return
        }

        guard let stagedRoot = try? stagedVersionsRootURL(),
              let children = try? fm.contentsOfDirectory(
                  at: stagedRoot,
                  includingPropertiesForKeys: [.isDirectoryKey],
                  options: [.skipsHiddenFiles]
              )
        else {
            if case .stagedReady = semanticState {
                semanticState = .idle
            }
            return
        }

        var bestCandidate: (manifest: StagedUpdateManifest, dir: URL, semver: SemanticVersion?)?
        for child in children {
            let values = try? child.resourceValues(forKeys: [.isDirectoryKey])
            guard values?.isDirectory == true else { continue }
            let manifestURL = child.appendingPathComponent(Self.stagedManifestFileName, isDirectory: false)
            guard let data = try? Data(contentsOf: manifestURL),
                  let manifest = try? JSONDecoder().decode(StagedUpdateManifest.self, from: data),
                  verifyStagedFilesPresent(manifest: manifest, in: child),
                  isStagedManifestApplicable(manifest)
            else {
                try? fm.removeItem(at: child)
                continue
            }
            let sem = SemanticVersion.parse(manifest.version)
            if let existing = bestCandidate {
                let isBetter: Bool
                if let lhs = sem, let rhs = existing.semver, lhs != rhs {
                    isBetter = lhs > rhs
                } else {
                    isBetter = manifest.stagedAtEpoch > existing.manifest.stagedAtEpoch
                }
                if isBetter {
                    bestCandidate = (manifest, child, sem)
                }
            } else {
                bestCandidate = (manifest, child, sem)
            }
        }

        if let best = bestCandidate {
            UpdatePathGuard.pruneStagedVersions(in: stagedRoot, keepingVersion: best.dir.lastPathComponent)
            switch semanticState {
            case .releaseAvailable(let knownRelease, _):
                if Self.isVersion(best.manifest.version, atLeast: knownRelease.version) {
                    semanticState = .stagedReady(manifest: best.manifest, stagingDir: best.dir)
                }
            case .idle, .stagedReady, .upToDate, .failed(.handoffInProgress):
                semanticState = .stagedReady(manifest: best.manifest, stagingDir: best.dir)
            case .failed(let reason) where recoverFromFailure:
                if case .stagedArtifactInvalid = reason {
                    break
                }
                semanticState = .stagedReady(manifest: best.manifest, stagingDir: best.dir)
            default:
                break
            }
        } else {
            UpdatePathGuard.pruneStagedVersions(in: stagedRoot, keepingVersion: nil)
            if case .stagedReady = semanticState {
                semanticState = .idle
            }
        }
    }

    private func downloadAndVerifyReleaseAssets(
        _ release: GitHubReleaseInfo,
        checksumsAsset: ReleaseAssetInfo,
        into stagingDir: URL,
        childEnv: [String: String],
        reportDetailedSteps: Bool
    ) async throws -> PreparedReleaseAssets {
        if reportDetailedSteps {
            setSemanticState(.updating(step: .downloadingChecksums))
        }
        let (checksumsData, checksumsStatus) = try await httpFetcher.fetchData(
            from: checksumsAsset.browserDownloadURL,
            headers: ["User-Agent": "CodexAgySync/\(appVersion)"],
            timeoutSeconds: 15.0
        )
        guard (200..<300).contains(checksumsStatus),
              let checksumsText = String(data: checksumsData, encoding: .utf8)
        else {
            throw UpdateServiceError.httpError(statusCode: checksumsStatus)
        }
        let checksums = SHA256Verifier.parseChecksums(checksumsText)

        var verifiedWheelURL: URL?
        var wheelSHA256: String?
        if let wheelAsset = release.wheelAsset {
            let wheelDest = try UpdatePathGuard.stagedAssetFileURL(for: wheelAsset.name, in: stagingDir)
            let safeWheelName = wheelDest.lastPathComponent
            if reportDetailedSteps {
                setSemanticState(.updating(step: .downloadingWheel(fileName: safeWheelName)))
            }
            let (wheelData, wheelStatus) = try await httpFetcher.fetchData(
                from: wheelAsset.browserDownloadURL,
                headers: ["User-Agent": "CodexAgySync/\(appVersion)"],
                timeoutSeconds: 30.0
            )
            guard (200..<300).contains(wheelStatus) else {
                throw UpdateServiceError.httpError(statusCode: wheelStatus)
            }
            try SHA256Verifier.verify(data: wheelData, assetName: safeWheelName, checksums: checksums)
            try wheelData.write(to: wheelDest, options: .atomic)
            verifiedWheelURL = wheelDest
            wheelSHA256 = SHA256Verifier.sha256Hex(of: wheelData)
        }

        var verifiedZipURL: URL?
        var verifiedExtractedAppURL: URL?
        if let appZipAsset = release.appZipAsset {
            let zipDest = try UpdatePathGuard.stagedAssetFileURL(for: appZipAsset.name, in: stagingDir)
            let safeZipName = zipDest.lastPathComponent
            if reportDetailedSteps {
                setSemanticState(.updating(step: .downloadingAppArchive(fileName: safeZipName)))
            }
            let (zipData, zipStatus) = try await httpFetcher.fetchData(
                from: appZipAsset.browserDownloadURL,
                headers: ["User-Agent": "CodexAgySync/\(appVersion)"],
                timeoutSeconds: 60.0
            )
            guard (200..<300).contains(zipStatus) else {
                throw UpdateServiceError.httpError(statusCode: zipStatus)
            }
            try SHA256Verifier.verify(data: zipData, assetName: safeZipName, checksums: checksums)
            try zipData.write(to: zipDest, options: .atomic)
            verifiedZipURL = zipDest

            if reportDetailedSteps {
                setSemanticState(.updating(step: .verifyingAppArchive))
            }
            let extractDir = stagingDir.appendingPathComponent("extracted", isDirectory: true)
            try FileManager.default.createDirectory(at: extractDir, withIntermediateDirectories: true)
            let dittoOut = try await commandRunner.run(
                executableURL: URL(fileURLWithPath: "/usr/bin/ditto"),
                arguments: ["-x", "-k", zipDest.path, extractDir.path],
                environment: childEnv,
                timeoutSeconds: 30.0
            )
            if dittoOut.exitCode != 0 {
                throw UpdateServiceError.commandFailed(
                    step: "ditto",
                    detail: dittoOut.stderrString.trimmingCharacters(in: .whitespacesAndNewlines)
                )
            }
            let extractedApp = Self.resolveExtractedAppBundle(in: extractDir)
            try await verifyCodesign(of: extractedApp, childEnv: childEnv)
            verifiedExtractedAppURL = extractedApp
        }

        return PreparedReleaseAssets(
            wheelURL: verifiedWheelURL,
            wheelSHA256: wheelSHA256,
            zipURL: verifiedZipURL,
            extractedAppURL: verifiedExtractedAppURL
        )
    }

    private func installWheel(
        at wheelURL: URL,
        pythonEnv: PythonRuntimeEnvironment,
        childEnv: [String: String]
    ) async throws {
        guard canMutateEnvironment?() ?? true else {
            throw UpdateFailureReasonError(.handoffInProgress)
        }
        var pipArgs = ["-m", "pip", "install", "--upgrade"]
        if !pythonEnv.inVenv {
            pipArgs.append("--user")
            if pythonEnv.supportsBreakSystemPackages {
                pipArgs.append("--break-system-packages")
            }
        }
        pipArgs.append(wheelURL.path)
        let pipOut = try await commandRunner.run(
            executableURL: pythonEnv.pythonURL,
            arguments: pipArgs,
            environment: childEnv,
            timeoutSeconds: 90.0
        )
        if pipOut.exitCode != 0 {
            throw UpdateServiceError.commandFailed(
                step: "pip install",
                detail: pipOut.stderrString.trimmingCharacters(in: .whitespacesAndNewlines)
            )
        }
    }

    private func performLocalRepoSync(repoPath: String) async {
        guard canMutateEnvironment?() ?? true else {
            setSemanticState(.failed(.handoffInProgress))
            return
        }
        let rawRepoURL = URL(fileURLWithPath: (repoPath as NSString).expandingTildeInPath, isDirectory: true)
        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)

        do {
            let repoURL = try UpdatePathGuard.validateNonTemporaryURL(rawRepoURL)
            guard Self.isValidSourceRepo(repoURL) else {
                throw UpdateServiceError.invalidLocalRepo(repoPath)
            }
            try validateReplacementTargetIfNeeded()

            let stagingDir = try makeStagingSessionDirectory()
            defer { try? FileManager.default.removeItem(at: stagingDir) }

            setSemanticState(.updating(step: .buildingLocalApp))
            let cliURL = resolvedCLIExecutableURL()
            let pyEnv = try await resolvePythonEnvironment(for: cliURL, childEnv: childEnv)

            let appStageDir = stagingDir.appendingPathComponent("app-out", isDirectory: true)
            try FileManager.default.createDirectory(at: appStageDir, withIntermediateDirectories: true)
            let buildScript = repoURL.appendingPathComponent("scripts/build_menubar_app.sh")
            let buildResult = try await commandRunner.run(
                executableURL: buildScript,
                arguments: [
                    "--output-dir", appStageDir.path,
                    "--bundle-id", currentBundleIdentifier,
                ],
                environment: childEnv,
                timeoutSeconds: 180.0
            )
            if buildResult.exitCode != 0 {
                throw UpdateServiceError.commandFailed(
                    step: "build_menubar_app.sh",
                    detail: buildResult.stderrString.trimmingCharacters(in: .whitespacesAndNewlines)
                )
            }

            let builtAppURL = appStageDir.appendingPathComponent("CodexAgySync.app", isDirectory: true)
            try await verifyCodesign(of: builtAppURL, childEnv: childEnv)

            setSemanticState(.updating(step: .upgradingLocalCLI))
            guard canMutateEnvironment?() ?? true else {
                throw UpdateFailureReasonError(.handoffInProgress)
            }
            var pipArgs = ["-m", "pip", "install", "--no-build-isolation"]
            if !pyEnv.inVenv {
                pipArgs.append("--user")
                if pyEnv.supportsBreakSystemPackages {
                    pipArgs.append("--break-system-packages")
                }
            }
            if pyEnv.editable {
                pipArgs.append(contentsOf: ["--no-deps", "--force-reinstall", "-e", repoURL.path])
            } else {
                pipArgs.append(contentsOf: ["--no-deps", "--upgrade", repoURL.path])
            }

            let pipResult = try await commandRunner.run(
                executableURL: pyEnv.pythonURL,
                arguments: pipArgs,
                environment: childEnv,
                timeoutSeconds: 90.0
            )
            if pipResult.exitCode != 0 {
                throw UpdateServiceError.commandFailed(
                    step: "pip install",
                    detail: pipResult.stderrString.trimmingCharacters(in: .whitespacesAndNewlines)
                )
            }

            try replaceRunningBundleIfPermitted(with: builtAppURL)

            let rawTargetVer = Self.parsePyprojectVersion(in: repoURL) ?? appVersion
            let targetVer = SemanticVersion.parse(rawTargetVer)?.description ?? rawTargetVer
            pendingRelaunchVersion = targetVer
            canRelaunchApp = true
            clearPersistedStagedDirectory()
            await probeEnvironment()
            setSemanticState(.upToDate(currentVersion: targetVer, message: .localRepoSynced(version: targetVer)))
        } catch let wrapped as UpdateFailureReasonError {
            await probeEnvironment()
            setSemanticState(.failed(wrapped.reason))
        } catch {
            await probeEnvironment()
            setSemanticState(.failed(.from(error)))
        }
    }

    private func performReleaseSync(_ release: GitHubReleaseInfo) async {
        guard canMutateEnvironment?() ?? true else {
            setSemanticState(.failed(.handoffInProgress))
            return
        }
        guard let checksumsAsset = release.checksumsAsset else {
            setSemanticState(.failed(.service(.missingChecksumsAsset)))
            return
        }
        guard release.wheelAsset != nil || release.appZipAsset != nil else {
            setSemanticState(.failed(.service(.missingUpdateAssets)))
            return
        }

        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)

        do {
            if release.appZipAsset != nil {
                try validateReplacementTargetIfNeeded()
            }
            let stagingDir = try makeStagingSessionDirectory()
            defer { try? FileManager.default.removeItem(at: stagingDir) }

            setSemanticState(.updating(step: .downloadingChecksums))
            let resolvedPyEnv: PythonRuntimeEnvironment?
            if release.wheelAsset != nil {
                let cliURL = resolvedCLIExecutableURL()
                resolvedPyEnv = try await resolvePythonEnvironment(for: cliURL, childEnv: childEnv)
            } else {
                resolvedPyEnv = nil
            }

            let prepared = try await downloadAndVerifyReleaseAssets(
                release,
                checksumsAsset: checksumsAsset,
                into: stagingDir,
                childEnv: childEnv,
                reportDetailedSteps: true
            )

            if let wheelPath = prepared.wheelURL, let pyEnv = resolvedPyEnv {
                setSemanticState(.updating(step: .installingCLI))
                try await installWheel(at: wheelPath, pythonEnv: pyEnv, childEnv: childEnv)
            }

            let normalizedReleaseVer = SemanticVersion.parse(release.version)?.description ?? release.version
            if let extractedApp = prepared.extractedAppURL {
                try replaceRunningBundleIfPermitted(with: extractedApp)
                pendingRelaunchVersion = normalizedReleaseVer
                canRelaunchApp = true
            }

            clearPersistedStagedDirectory()
            await probeEnvironment()
            let upToDateKind: UpToDateMessage = prepared.extractedAppURL != nil
                ? .releaseSyncedWithApp(tag: release.tagName)
                : .releaseSyncedCLIOnly(tag: release.tagName)
            setSemanticState(.upToDate(currentVersion: normalizedReleaseVer, message: upToDateKind))
        } catch let wrapped as UpdateFailureReasonError {
            await probeEnvironment()
            setSemanticState(.failed(wrapped.reason))
        } catch {
            await probeEnvironment()
            setSemanticState(.failed(.from(error)))
        }
    }

    nonisolated static func resolveExtractedAppBundle(
        in extractDir: URL,
        fileManager: FileManager = .default
    ) -> URL {
        let preferred = extractDir.appendingPathComponent("CodexAgySync.app", isDirectory: true)
        if fileManager.fileExists(atPath: preferred.path) {
            return preferred
        }
        if let children = try? fileManager.contentsOfDirectory(
            at: extractDir,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) {
            let apps = children.filter { $0.pathExtension.lowercased() == "app" }
            if apps.count == 1 {
                return apps[0]
            }
        }
        return preferred
    }

    private func verifyCodesign(of appURL: URL, childEnv: [String: String]) async throws {
        guard FileManager.default.fileExists(atPath: appURL.path) else {
            let missingMsg = AppCopy.updatesMissingAppBundle(
                appURL.lastPathComponent,
                in: preferencesStore.effectiveLanguage
            )
            throw UpdateServiceError.codesignVerificationFailed(missingMsg)
        }
        let result = try await commandRunner.run(
            executableURL: URL(fileURLWithPath: "/usr/bin/codesign"),
            arguments: ["--verify", "--deep", "--strict", appURL.path],
            environment: childEnv,
            timeoutSeconds: 20.0
        )
        if result.exitCode != 0 {
            throw UpdateServiceError.codesignVerificationFailed(
                result.stderrString.trimmingCharacters(in: .whitespacesAndNewlines)
            )
        }
    }

    private func validateReplacementTargetIfNeeded() throws {
        if environment[Self.skipSelfReplaceEnvKey] == "1" {
            return
        }
        guard let targetBundleURL = currentAppBundleURL else {
            throw UpdateServiceError.targetBundleNotWritable(
                AppCopy.text(.updatesErrNoWritableBundleFallback, in: preferencesStore.effectiveLanguage)
            )
        }
        let validatedTarget = try UpdatePathGuard.validateNonTemporaryURL(targetBundleURL)
        let fm = FileManager.default
        let parentDir = validatedTarget.deletingLastPathComponent()
        guard fm.fileExists(atPath: parentDir.path), fm.isWritableFile(atPath: parentDir.path) else {
            throw UpdateServiceError.targetBundleNotWritable(parentDir.path)
        }
        if fm.fileExists(atPath: validatedTarget.path), !fm.isWritableFile(atPath: validatedTarget.path) {
            throw UpdateServiceError.targetBundleNotWritable(validatedTarget.path)
        }
    }

    private func replaceRunningBundleIfPermitted(with stagedAppURL: URL) throws {
        guard canMutateEnvironment?() ?? true else {
            throw UpdateFailureReasonError(.handoffInProgress)
        }
        if environment[Self.skipSelfReplaceEnvKey] == "1" {
            return
        }
        try validateReplacementTargetIfNeeded()
        guard let targetBundleURL = currentAppBundleURL else {
            throw UpdateServiceError.targetBundleNotWritable(
                AppCopy.text(.updatesErrNoWritableBundleFallback, in: preferencesStore.effectiveLanguage)
            )
        }
        let fm = FileManager.default
        if fm.fileExists(atPath: targetBundleURL.path) {
            _ = try fm.replaceItemAt(targetBundleURL, withItemAt: stagedAppURL)
        } else {
            try fm.moveItem(at: stagedAppURL, to: targetBundleURL)
        }
    }

    private struct PythonProbePayload: Decodable {
        let executable: String
        let in_venv: Bool
        let editable: Bool
        let repo_path: String?
        let supports_break_system_packages: Bool?
    }

    private struct PythonRuntimeEnvironment {
        let pythonURL: URL
        let inVenv: Bool
        let editable: Bool
        let repoPath: String?
        let supportsBreakSystemPackages: Bool
    }

    private func resolvePythonEnvironment(
        for cliURL: URL?,
        childEnv: [String: String]
    ) async throws -> PythonRuntimeEnvironment {
        guard let candidatePython = Self.findPythonExecutable(for: cliURL, childEnv: childEnv) else {
            throw UpdateServiceError.pythonInterpreterMissing
        }
        let probeCode = """
        import importlib.metadata as m, json, pathlib, subprocess, sys, urllib.parse
        editable = False
        repo = None
        try:
            dist = m.distribution("codex-agy-sync")
            raw = dist.read_text("direct_url.json")
            if raw:
                info = json.loads(raw)
                editable = bool(info.get("dir_info", {}).get("editable"))
                url = str(info.get("url") or "")
                if url.startswith("file://"):
                    p = pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
                    if (p / "pyproject.toml").is_file() and (p / "scripts" / "build_menubar_app.sh").is_file():
                        repo = str(p)
        except Exception:
            pass
        in_venv = sys.prefix != sys.base_prefix
        supports_bsp = False
        if not in_venv:
            try:
                help_proc = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                supports_bsp = "--break-system-packages" in (help_proc.stdout or "")
            except Exception:
                supports_bsp = False
        print(json.dumps({
            "executable": sys.executable,
            "in_venv": in_venv,
            "editable": editable,
            "repo_path": repo,
            "supports_break_system_packages": supports_bsp,
        }))
        """
        let out = try await commandRunner.run(
            executableURL: candidatePython,
            arguments: ["-c", probeCode],
            environment: childEnv,
            timeoutSeconds: 10.0
        )
        if out.exitCode == 0,
           let parsed = try? JSONDecoder().decode(PythonProbePayload.self, from: out.stdout)
        {
            return PythonRuntimeEnvironment(
                pythonURL: URL(fileURLWithPath: parsed.executable),
                inVenv: parsed.in_venv,
                editable: parsed.editable,
                repoPath: parsed.repo_path,
                supportsBreakSystemPackages: parsed.supports_break_system_packages ?? false
            )
        }
        return PythonRuntimeEnvironment(
            pythonURL: candidatePython,
            inVenv: false,
            editable: false,
            repoPath: nil,
            supportsBreakSystemPackages: false
        )
    }

    private func updateRepoDiscovery(fromCLI cliURL: URL?) {
        if let customRepo = preferencesStore.preferences.localRepoPathForSync {
            let customURL = URL(fileURLWithPath: customRepo, isDirectory: true)
            if let validated = try? UpdatePathGuard.validateNonTemporaryURL(customURL),
               Self.isValidSourceRepo(validated)
            {
                detectedRepoPath = validated.path
                detectedRepoVersion = Self.parsePyprojectVersion(in: validated)
                return
            }
        }
        if let cliURL {
            let resolvedCLI = cliURL.resolvingSymlinksInPath()
            let ancestorRepo = resolvedCLI.deletingLastPathComponent().deletingLastPathComponent()
            if let validated = try? UpdatePathGuard.validateNonTemporaryURL(ancestorRepo),
               Self.isValidSourceRepo(validated)
            {
                detectedRepoPath = validated.path
                detectedRepoVersion = Self.parsePyprojectVersion(in: validated)
                return
            }
        }
        detectedRepoPath = nil
        detectedRepoVersion = nil
    }

    private func discoverEditableRepoIfNeeded(
        fromCLI cliURL: URL,
        childEnv: [String: String],
        probeGenerationSnapshot: UInt64
    ) async {
        updateRepoDiscovery(fromCLI: cliURL)
        guard detectedRepoPath == nil else { return }

        if let pyInfo = try? await resolvePythonEnvironment(for: cliURL, childEnv: childEnv),
           probeGenerationSnapshot == probeGeneration,
           let repoPath = pyInfo.repoPath
        {
            let repoURL = URL(fileURLWithPath: repoPath, isDirectory: true)
            if let validated = try? UpdatePathGuard.validateNonTemporaryURL(repoURL),
               Self.isValidSourceRepo(validated)
            {
                detectedRepoPath = validated.path
                detectedRepoVersion = Self.parsePyprojectVersion(in: validated)
            }
        }
    }

    public static func findPythonExecutable(
        for cliURL: URL?,
        childEnv: [String: String],
        fileManager: FileManager = .default
    ) -> URL? {
        let pathEntries = (childEnv["PATH"] ?? "").split(separator: ":").map(String.init)

        if let cliURL,
           let handle = try? FileHandle(forReadingFrom: cliURL),
           let headData = try? handle.read(upToCount: 1024),
           let headText = String(data: headData, encoding: .utf8)
        {
            try? handle.close()
            let lines = headText.split(whereSeparator: \.isNewline).map(String.init)
            if let firstLine = lines.first, firstLine.hasPrefix("#!") {
                let shebang = String(firstLine.dropFirst(2)).trimmingCharacters(in: .whitespaces)
                let tokens = shebang.split(separator: " ").map(String.init)

                if isPythonExecutableName(shebang), fileManager.isExecutableFile(atPath: shebang) {
                    return URL(fileURLWithPath: shebang)
                }
                if let first = tokens.first {
                    if first.hasSuffix("/env") {
                        if let target = tokens.dropFirst().first(where: { !$0.hasPrefix("-") && !$0.contains("=") }) {
                            if target.hasPrefix("/"),
                               isPythonExecutableName(target),
                               fileManager.isExecutableFile(atPath: target)
                            {
                                return URL(fileURLWithPath: target)
                            }
                            if isPythonExecutableName(target) {
                                for dir in pathEntries {
                                    let candidate = URL(fileURLWithPath: dir).appendingPathComponent(target).path
                                    if fileManager.isExecutableFile(atPath: candidate) {
                                        return URL(fileURLWithPath: candidate)
                                    }
                                }
                            }
                            if let wrapped = extractWrappedPython(from: Array(lines.dropFirst().prefix(4)), fileManager: fileManager) {
                                return wrapped
                            }
                        }
                    } else if isPythonExecutableName(first), fileManager.isExecutableFile(atPath: first) {
                        return URL(fileURLWithPath: first)
                    } else if let wrapped = extractWrappedPython(from: Array(lines.dropFirst().prefix(4)), fileManager: fileManager) {
                        return wrapped
                    }
                }
            }
        }

        for dir in pathEntries {
            let candidate = URL(fileURLWithPath: dir).appendingPathComponent("python3").path
            if fileManager.isExecutableFile(atPath: candidate) {
                return URL(fileURLWithPath: candidate)
            }
        }
        for fallback in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
            where fileManager.isExecutableFile(atPath: fallback)
        {
            return URL(fileURLWithPath: fallback)
        }
        return nil
    }

    private static func isPythonExecutableName(_ pathOrName: String) -> Bool {
        let base = URL(fileURLWithPath: pathOrName).lastPathComponent.lowercased()
        return base.range(of: #"^python([0-9]+(\.[0-9]+)*)?$"#, options: .regularExpression) != nil
    }

    private static func extractWrappedPython(
        from lines: [String],
        fileManager: FileManager
    ) -> URL? {
        guard let quoteRegex = try? NSRegularExpression(pattern: #"'([^']+)'|"([^"]+)""#) else {
            return nil
        }
        for line in lines {
            let nsRange = NSRange(line.startIndex..<line.endIndex, in: line)
            for match in quoteRegex.matches(in: line, range: nsRange) {
                let captureRange = match.range(at: 1).location != NSNotFound
                    ? match.range(at: 1)
                    : match.range(at: 2)
                if let swiftRange = Range(captureRange, in: line) {
                    let candidate = String(line[swiftRange])
                    if candidate.hasPrefix("/"),
                       isPythonExecutableName(candidate),
                       fileManager.isExecutableFile(atPath: candidate)
                    {
                        return URL(fileURLWithPath: candidate)
                    }
                }
            }
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("exec ") {
                let parts = trimmed.dropFirst(5).split(separator: " ").map(String.init)
                if let rawExec = parts.first,
                   rawExec.hasPrefix("/"),
                   isPythonExecutableName(rawExec),
                   fileManager.isExecutableFile(atPath: rawExec)
                {
                    return URL(fileURLWithPath: rawExec)
                }
            }
        }
        return nil
    }
}
