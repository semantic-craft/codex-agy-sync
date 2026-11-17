import AppKit
import Foundation
import Observation

@MainActor
@Observable
public final class UpdateViewModel {
    public nonisolated static let defaultReleaseAPIURL = "https://api.github.com/repos/semantic-craft/codex-agy-sync/releases/latest"
    public nonisolated static let releaseAPIEnvKey = "MENUBAR_RELEASE_API_URL"
    public nonisolated static let skipSelfReplaceEnvKey = "MENUBAR_SKIP_SELF_REPLACE"
    public nonisolated static let checkpointDefaultsKey = UpdateStagingStore.checkpointDefaultsKey
    public nonisolated static let successCooldownSeconds: TimeInterval = UpdateCheckCheckpoint.successCooldownSeconds
    public nonisolated static let failureBackoffSeconds: TimeInterval = UpdateCheckCheckpoint.failureBackoffSeconds

    private nonisolated static let legacyBundleSuffix = ["codex", "agy", "hand" + "off"].joined(separator: "-")

    private struct ProbeSignature: Equatable {
        let customCLIPath: String?
        let localRepoPath: String?
        let recoverStagedFromFailure: Bool
    }

    private var rawAlignment: VersionAlignmentState
    private var semanticState: SemanticUpdateState = .idle
    private var lastBackgroundErrorReason: UpdateFailureReason?

    public private(set) var resolvedCLIPath: String?
    public private(set) var cliSource: CLIResolutionSource = .missing
    public private(set) var detectedRepoPath: String?
    public private(set) var detectedRepoVersion: String?
    private var validatedCustomRepo: ValidatedRepoResult?
    public private(set) var latestRelease: GitHubReleaseInfo?
    public private(set) var lastCheckedEpoch: TimeInterval?
    public private(set) var canRelaunchApp: Bool = false
    public private(set) var pendingRelaunchVersion: String?
    public private(set) var currentAppBundleURL: URL?

    public let appVersion: String
    public let currentBundleIdentifier: String
    public let preferencesStore: PreferencesStore

    private let environment: [String: String]
    private let commandRunner: any SystemCommandRunning
    private let stagingStore: UpdateStagingStore
    private let releaseClient: ReleaseUpdateClient
    private let relaunchExecutor: any RelaunchExecuting
    private let nowProvider: @Sendable () -> Date
    @ObservationIgnored private let canMutateEnvironment: (@MainActor () -> Bool)?
    @ObservationIgnored public var onPrepareForRelaunch: (@MainActor () -> Void)?
    @ObservationIgnored public var onRelaunchFailed: (@MainActor () -> Void)?

    @ObservationIgnored private var hasCompletedInitialProbe = false
    @ObservationIgnored private var activeProbeTask: Task<Void, Never>?
    @ObservationIgnored private var activeProbeSignature: ProbeSignature?
    @ObservationIgnored private var opportunisticTask: Task<Void, Never>?
    @ObservationIgnored private var surfaceRefreshTask: Task<Void, Never>?
    @ObservationIgnored private var isManualActionInFlight = false
    @ObservationIgnored private var probeGeneration: UInt64 = 0
    @ObservationIgnored private var opportunisticGeneration: UInt64 = 0

    public var alignment: VersionAlignmentState {
        if case .cliUnavailable(let appVer, _) = rawAlignment, cliSource == .missing {
            return .cliUnavailable(
                appVersion: appVer,
                reason: AppCopy.text(.cliMissingError, in: preferencesStore.effectiveLanguage)
            )
        }
        return rawAlignment
    }

    public var availability: UpdateAvailability {
        UpdateStateProjector.projectAvailabilityAndStatus(
            semanticState: semanticState,
            in: preferencesStore.effectiveLanguage
        ).availability
    }

    public var statusMessage: String? {
        UpdateStateProjector.projectAvailabilityAndStatus(
            semanticState: semanticState,
            in: preferencesStore.effectiveLanguage
        ).statusMessage
    }

    public var stagedManifest: StagedUpdateManifest? {
        UpdateStateProjector.projectAvailabilityAndStatus(
            semanticState: semanticState,
            in: preferencesStore.effectiveLanguage
        ).stagedManifest
    }

    public var lastCheckedDisplayText: String? {
        UpdateStateProjector.formatLastChecked(
            epoch: lastCheckedEpoch,
            now: nowProvider(),
            in: preferencesStore.effectiveLanguage
        )
    }

    public var lastBackgroundCheckError: String? {
        UpdateStateProjector.formatBackgroundError(
            reason: lastBackgroundErrorReason,
            in: preferencesStore.effectiveLanguage
        )
    }

    public var popoverNotice: PopoverUpdateNotice? {
        UpdateStateProjector.buildPopoverNotice(
            semanticState: semanticState,
            canRelaunchApp: canRelaunchApp,
            pendingRelaunchVersion: pendingRelaunchVersion,
            effectiveAppVersion: effectiveAppVersion,
            in: preferencesStore.effectiveLanguage
        )
    }

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
        if let custom = preferencesStore.preferences.localRepoPathForSync {
            if validatedCustomRepo?.rawInput == custom, let validPath = validatedCustomRepo?.validatedPath {
                return validPath
            }
            return detectedRepoPath
        }
        return detectedRepoPath
    }

    public var effectiveRepoVersion: String? {
        if let custom = preferencesStore.preferences.localRepoPathForSync,
           validatedCustomRepo?.rawInput == custom,
           validatedCustomRepo?.validatedPath != nil
        {
            return validatedCustomRepo?.version ?? detectedRepoVersion
        }
        if detectedRepoPath != nil {
            return detectedRepoVersion
        }
        return nil
    }

    private var skipsSelfReplace: Bool {
        environment[Self.skipSelfReplaceEnvKey] == "1"
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
            ?? "0.4.3"
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
        let store = UpdateStagingStore(stagingRootOverride: stagingRootOverride, commandRunner: commandRunner)
        self.stagingStore = store
        self.releaseClient = ReleaseUpdateClient(httpFetcher: httpFetcher, commandRunner: commandRunner, stagingStore: store)
        self.relaunchExecutor = relaunchExecutor
        self.nowProvider = nowProvider
        self.canMutateEnvironment = canMutateEnvironment
        self.onPrepareForRelaunch = onPrepareForRelaunch
        self.onRelaunchFailed = onRelaunchFailed
        self.rawAlignment = .cliUnavailable(
            appVersion: resolvedAppVer,
            reason: AppCopy.text(.updatesNotProbed, in: preferencesStore.effectiveLanguage)
        )

        let checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
        self.lastCheckedEpoch = checkpoint.lastSuccessfulCheckEpoch
    }

    public func probeIfNeeded() async {
        if let existingTask = activeProbeTask {
            await existingTask.value
            return
        }
        guard !hasCompletedInitialProbe else { return }
        await probeEnvironment()
    }

    public func refreshOnSurfaceOpen(
        trigger: UpdateTrigger,
        recoverStagedFromFailure: Bool = true
    ) async {
        surfaceRefreshTask?.cancel()
        let task = Task { @MainActor [weak self] in
            guard let self else { return }
            await self.probeEnvironment(recoverStagedFromFailure: recoverStagedFromFailure)
            guard !Task.isCancelled else { return }
            if self.preferencesStore.preferences.autoUpdatePolicy != .off {
                await self.performOpportunisticCheckIfNeeded(
                    trigger: trigger,
                    skipInitialProbe: true
                )
            }
        }
        surfaceRefreshTask = task
        await withTaskCancellationHandler {
            await task.value
        } onCancel: {
            task.cancel()
        }
        if surfaceRefreshTask == task {
            surfaceRefreshTask = nil
        }
    }

    public func probeEnvironment(recoverStagedFromFailure: Bool = false) async {
        hasCompletedInitialProbe = true
        let sig = currentProbeSignature(recoverStagedFromFailure: recoverStagedFromFailure)
        if let existingTask = activeProbeTask, activeProbeSignature == sig {
            await withTaskCancellationHandler {
                await existingTask.value
            } onCancel: {
                existingTask.cancel()
            }
            return
        }

        activeProbeTask?.cancel()
        probeGeneration &+= 1
        let myProbeGen = probeGeneration
        let task = Task { @MainActor [weak self] in
            guard let self else { return }
            await self.executeProbe(
                signature: sig,
                recoverStagedFromFailure: recoverStagedFromFailure,
                generation: myProbeGen
            )
        }
        activeProbeTask = task
        activeProbeSignature = sig
        await withTaskCancellationHandler {
            await task.value
        } onCancel: {
            task.cancel()
        }
        if probeGeneration == myProbeGen {
            activeProbeTask = nil
            activeProbeSignature = nil
        }
    }

    private func currentProbeSignature(recoverStagedFromFailure: Bool) -> ProbeSignature {
        let customCLIPath = preferencesStore.preferences.customCLIPath
            ?? preferencesStore.defaults.string(forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
        return ProbeSignature(
            customCLIPath: customCLIPath,
            localRepoPath: preferencesStore.preferences.localRepoPathForSync,
            recoverStagedFromFailure: recoverStagedFromFailure
        )
    }

    private func executeProbe(
        signature: ProbeSignature,
        recoverStagedFromFailure: Bool,
        generation myProbeGen: UInt64
    ) async {
        let probed = await UpdateEnvironmentProber.shared.probeCLIAndRepo(
            environment: environment,
            customCLIPath: signature.customCLIPath,
            localRepoPathForSync: signature.localRepoPath
        )
        guard !Task.isCancelled, myProbeGen == probeGeneration else { return }

        self.cliSource = probed.cliSource
        self.resolvedCLIPath = probed.cliURL?.path
        self.validatedCustomRepo = probed.validatedCustomRepo
        self.detectedRepoPath = probed.discoveredRepo?.path
        self.detectedRepoVersion = probed.discoveredRepo?.version

        guard let cliURL = probed.cliURL else {
            self.rawAlignment = .cliUnavailable(
                appVersion: effectiveAppVersion,
                reason: AppCopy.text(.cliMissingError, in: preferencesStore.effectiveLanguage)
            )
            reconcilePersistedStagedRelease(recoverFromFailure: recoverStagedFromFailure)
            return
        }

        let nextAlignment: VersionAlignmentState
        do {
            let result = try await commandRunner.run(
                executableURL: cliURL,
                arguments: ["--version"],
                environment: probed.childEnv,
                timeoutSeconds: 8.0
            )
            guard !Task.isCancelled, myProbeGen == probeGeneration else { return }
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
            guard !Task.isCancelled, myProbeGen == probeGeneration else { return }
            nextAlignment = .cliUnavailable(appVersion: effectiveAppVersion, reason: error.localizedDescription)
        }
        self.rawAlignment = nextAlignment

        await discoverEditableRepoIfNeeded(
            fromCLI: cliURL,
            childEnv: probed.childEnv,
            probeGenerationSnapshot: myProbeGen
        )
        guard !Task.isCancelled, myProbeGen == probeGeneration else { return }
        reconcilePersistedStagedRelease(recoverFromFailure: recoverStagedFromFailure)
    }

    public func checkForUpdates() async {
        guard !availability.isBusy, !isManualActionInFlight else { return }
        isManualActionInFlight = true
        defer { isManualActionInFlight = false }
        cancelOpportunisticCheck()
        await performReleaseCheck(trigger: .manual)
    }

    public func performOpportunisticCheckIfNeeded(
        trigger: UpdateTrigger,
        skipInitialProbe: Bool = false
    ) async {
        await performReleaseCheck(trigger: trigger, skipInitialProbe: skipInitialProbe)
    }

    public func handlePolicyChanged(to policy: AutoUpdatePolicy) async {
        guard policy != .off else { return }
        await refreshCustomRepoValidationIfStale()
        guard !availability.isBusy, !isManualActionInFlight, opportunisticTask == nil else { return }
        if policy == .autoStage,
           !canRelaunchApp,
           pendingRelaunchVersion == nil,
           effectiveRepoPath == nil,
           case .releaseAvailable(let release, let reason) = semanticState
        {
            opportunisticGeneration &+= 1
            let myOppGen = opportunisticGeneration
            let task = Task { @MainActor [weak self] in
                guard let self else { return }
                var checkpoint = Self.loadCheckpoint(from: self.preferencesStore.defaults)
                checkpoint.lastAttemptEpoch = self.nowProvider().timeIntervalSince1970
                Self.saveCheckpoint(checkpoint, to: self.preferencesStore.defaults)
                await self.stageReleaseIfEligible(
                    release,
                    reason: reason,
                    isOpportunistic: true,
                    opportunisticGenerationSnapshot: myOppGen
                )
            }
            opportunisticTask = task
            await withTaskCancellationHandler {
                await task.value
            } onCancel: {
                task.cancel()
            }
            if myOppGen == opportunisticGeneration {
                opportunisticTask = nil
            }
            return
        }
        await performOpportunisticCheckIfNeeded(trigger: .policyChanged)
    }

    public func syncUpdateNow() async {
        guard !availability.isBusy, !isManualActionInFlight else { return }
        isManualActionInFlight = true
        defer { isManualActionInFlight = false }
        cancelOpportunisticCheck()
        await performManualSyncUpdateNow()
    }

    public func installStagedAndRelaunch() async {
        guard !availability.isBusy, !isManualActionInFlight else { return }
        isManualActionInFlight = true
        defer { isManualActionInFlight = false }
        cancelOpportunisticCheck()

        guard canMutateEnvironment?() ?? true else {
            semanticState = .failed(.syncInProgress)
            return
        }
        await refreshCustomRepoValidationIfStale()
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

    public func syncFromLocalRepo() async {
        guard !availability.isBusy, !isManualActionInFlight else { return }
        isManualActionInFlight = true
        defer { isManualActionInFlight = false }
        cancelOpportunisticCheck()

        guard canMutateEnvironment?() ?? true else {
            semanticState = .failed(.syncInProgress)
            return
        }
        await refreshCustomRepoValidationIfStale()
        let candidate = effectiveRepoPath ?? preferencesStore.preferences.localRepoPathForSync
        guard let repoPath = candidate else {
            semanticState = .failed(.service(.invalidLocalRepo("unconfigured")))
            return
        }
        await performLocalRepoSync(repoPath: repoPath)
    }

    public func clearLocalRepoPath() async {
        preferencesStore.setLocalRepoPathForSync(nil)
        validatedCustomRepo = nil
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
            semanticState = .failed(.bundlePathNotFound)
            return false
        }
        onPrepareForRelaunch?()
        do {
            try relaunchExecutor.relaunch(bundleURL: bundleURL)
            return true
        } catch {
            onRelaunchFailed?()
            semanticState = .failed(.relaunchFailed(error.localizedDescription))
            return false
        }
    }

    private func cancelOpportunisticCheck() {
        opportunisticGeneration &+= 1
        opportunisticTask?.cancel()
        opportunisticTask = nil
    }

    private func refreshCustomRepoValidationIfStale() async {
        guard let rawRepo = preferencesStore.preferences.localRepoPathForSync else {
            validatedCustomRepo = nil
            return
        }
        if validatedCustomRepo?.rawInput == rawRepo {
            return
        }
        validatedCustomRepo = await UpdateEnvironmentProber.shared.validateCustomRepo(rawRepo)
    }

    private func performReleaseCheck(
        trigger: UpdateTrigger,
        skipInitialProbe: Bool = false
    ) async {
        let isOpportunistic = trigger.isOpportunistic

        if isOpportunistic {
            guard !availability.isBusy, !isManualActionInFlight, opportunisticTask == nil else { return }
            let policy = preferencesStore.preferences.autoUpdatePolicy
            guard policy != .off, !canRelaunchApp, pendingRelaunchVersion == nil else { return }

            let now = nowProvider().timeIntervalSince1970
            let checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
            guard Self.hasCompletedCooldown(now: now, checkpoint: checkpoint) else { return }

            opportunisticGeneration &+= 1
            let myOppGen = opportunisticGeneration
            let task = Task { @MainActor [weak self] in
                guard let self else { return }
                await self.executeReleaseCheckBody(
                    isOpportunistic: true,
                    skipInitialProbe: skipInitialProbe,
                    opportunisticGenerationSnapshot: myOppGen
                )
            }
            opportunisticTask = task
            await withTaskCancellationHandler {
                await task.value
            } onCancel: {
                task.cancel()
            }
            if myOppGen == opportunisticGeneration {
                opportunisticTask = nil
            }
            return
        }

        await executeReleaseCheckBody(
            isOpportunistic: false,
            skipInitialProbe: skipInitialProbe,
            opportunisticGenerationSnapshot: nil
        )
    }

    private func executeReleaseCheckBody(
        isOpportunistic: Bool,
        skipInitialProbe: Bool,
        opportunisticGenerationSnapshot capturedOppGen: UInt64?
    ) async {
        if !skipInitialProbe {
            await probeEnvironment()
        } else {
            await refreshCustomRepoValidationIfStale()
        }

        if let myOppGen = capturedOppGen {
            guard !Task.isCancelled,
                  myOppGen == opportunisticGeneration,
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
            semanticState = .checking
        }

        let rawURL = environment[Self.releaseAPIEnvKey] ?? Self.defaultReleaseAPIURL
        guard let url = URL(string: rawURL) else {
            handleCheckError(
                UpdateServiceError.invalidReleaseURL(rawURL),
                isOpportunistic: isOpportunistic,
                priorState: priorState
            )
            return
        }

        var checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
        checkpoint.lastAttemptEpoch = nowProvider().timeIntervalSince1970
        Self.saveCheckpoint(checkpoint, to: preferencesStore.defaults)

        do {
            let release = try await releaseClient.fetchLatestRelease(from: url, appVersion: appVersion)
            if let myOppGen = capturedOppGen {
                guard !Task.isCancelled, myOppGen == opportunisticGeneration else { return }
            }
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
                guard !Task.isCancelled, myOppGen == opportunisticGeneration else { return }
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
            switch rawAlignment {
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

        switch rawAlignment {
        case .cliUnavailable:
            if let repoPath = effectiveRepoPath, let repoVer = effectiveRepoVersion {
                semanticState = .localSyncReady(repoPath: repoPath, repoVersion: repoVer, reason: .cliMissing)
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
                semanticState = .failed(.cliUnavailableMissingAsset(tag: release.tagName))
            }

        case .mismatched:
            if let repoPath = effectiveRepoPath, let repoVer = effectiveRepoVersion {
                semanticState = .localSyncReady(repoPath: repoPath, repoVersion: repoVer, reason: .versionMismatch)
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
                semanticState = .failed(.mismatchMissingAsset(tag: release.tagName))
            }

        case .aligned:
            stagingStore.clearPersistedStagedDirectory()
            semanticState = .upToDate(currentVersion: currentAppVer, message: .alignedWithRemote(tag: release.tagName))
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
           stagingStore.verifyStagedFilesPresent(manifest: manifest, in: stagingDir)
        {
            semanticState = .stagedReady(manifest: manifest, stagingDir: stagingDir)
            return
        }

        semanticState = .releaseAvailable(release: release, reason: reason)

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
            semanticState = .localSyncReady(
                repoPath: repoPath,
                repoVersion: repoVer,
                reason: .remoteCheckUnavailable(failure)
            )
        } else {
            semanticState = .failed(failure)
        }
    }

    private func restoreQuietStateAfterOpportunistic(_ priorState: SemanticUpdateState) {
        switch priorState {
        case .stagedReady, .releaseAvailable, .upToDate, .localSyncReady:
            semanticState = priorState
        case .idle, .checking, .updating, .failed:
            semanticState = .idle
        }
    }

    private func performManualSyncUpdateNow() async {
        guard canMutateEnvironment?() ?? true else {
            semanticState = .failed(.syncInProgress)
            return
        }
        await refreshCustomRepoValidationIfStale()

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
            await performReleaseCheck(trigger: .manual)
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

        semanticState = .failed(.service(.missingUpdateAssets))
    }

    private func performInstallStaged(manifest: StagedUpdateManifest, stagedDir: URL) async {
        let childEnv = await UpdateEnvironmentProber.shared.mergedEnvironment(from: environment)
        let lang = preferencesStore.effectiveLanguage
        semanticState = .updating(step: .applyingStagedUpdate(tagName: manifest.tagName))

        do {
            let validated = try await stagingStore.prepareValidatedStagedAssets(
                for: manifest,
                in: stagedDir,
                targetBundleURL: currentAppBundleURL,
                skipSelfReplace: skipsSelfReplace,
                childEnv: childEnv,
                language: lang
            )

            if let wheelURL = validated.verifiedWheelURL {
                let cliURL = await resolveCLIExecutableURLOffMain()
                let pyEnv = try await LocalRepoEnvironment.resolvePythonEnvironment(
                    for: cliURL,
                    childEnv: childEnv,
                    commandRunner: commandRunner
                )
                guard canMutateEnvironment?() ?? true else {
                    throw UpdateFailureReasonError(.syncInProgress)
                }
                try await releaseClient.installWheel(at: wheelURL, pythonEnv: pyEnv, childEnv: childEnv)
            }

            if let stagedAppURL = validated.stagedAppURL {
                try replaceRunningBundleIfPermitted(with: stagedAppURL)
                pendingRelaunchVersion = manifest.version
                canRelaunchApp = true
            }

            stagingStore.clearPersistedStagedDirectory()
            await probeEnvironment()

            let upToDateKind: UpToDateMessage = validated.stagedAppURL != nil
                ? .releaseSyncedWithApp(tag: manifest.tagName)
                : .releaseSyncedCLIOnly(tag: manifest.tagName)
            semanticState = .upToDate(currentVersion: manifest.version, message: upToDateKind)

            if validated.stagedAppURL != nil, !skipsSelfReplace {
                _ = relaunchApp()
            }
        } catch let wrapped as UpdateFailureReasonError {
            if case .stagedArtifactInvalid = wrapped.reason {
                stagingStore.clearPersistedStagedDirectory()
            }
            await probeEnvironment()
            semanticState = .failed(wrapped.reason)
        } catch {
            await probeEnvironment()
            semanticState = .failed(.from(error))
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

        let childEnv = await UpdateEnvironmentProber.shared.mergedEnvironment(from: environment)
        let normalizedVer = SemanticVersion.parse(release.version)?.description ?? release.version
        let lang = preferencesStore.effectiveLanguage

        do {
            if release.appZipAsset != nil {
                try UpdateStagingStore.validateReplacementTargetIfNeeded(
                    targetBundleURL: currentAppBundleURL,
                    skipSelfReplace: skipsSelfReplace,
                    language: lang
                )
            }
            let tmpDir = try stagingStore.makeStagingSessionDirectory()
            defer { try? FileManager.default.removeItem(at: tmpDir) }

            semanticState = .updating(step: .stagingRelease(tagName: release.tagName))
            let prepared = try await releaseClient.downloadAndVerifyReleaseAssets(
                release,
                checksumsAsset: checksumsAsset,
                into: tmpDir,
                appVersion: appVersion,
                childEnv: childEnv,
                language: lang,
                onStep: { _ in }
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
            let versionDir = try stagingStore.persistStagedDirectory(from: tmpDir, manifest: manifest)
            semanticState = .stagedReady(manifest: manifest, stagingDir: versionDir)
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
                semanticState = .releaseAvailable(release: release, reason: reason)
            } else {
                semanticState = .failed(.from(error))
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
                semanticState = .localSyncReady(repoPath: repoPath, repoVersion: repoVer, reason: .cliMissing)
                return
            case .versionMismatch:
                semanticState = .localSyncReady(repoPath: repoPath, repoVersion: repoVer, reason: .versionMismatch)
                return
            case .newerVersion:
                break
            }
        }
        semanticState = .releaseAvailable(release: release, reason: reason)
    }

    private func performLocalRepoSync(repoPath: String) async {
        guard canMutateEnvironment?() ?? true else {
            semanticState = .failed(.syncInProgress)
            return
        }
        let rawRepoURL = URL(fileURLWithPath: (repoPath as NSString).expandingTildeInPath, isDirectory: true)
        let childEnv = await UpdateEnvironmentProber.shared.mergedEnvironment(from: environment)
        let lang = preferencesStore.effectiveLanguage

        do {
            let repoCheck = await UpdateEnvironmentProber.shared.validateRepoURL(rawRepoURL)
            guard let repoURL = repoCheck.validatedURL else {
                throw UpdateServiceError.invalidLocalRepo(repoPath)
            }
            try UpdateStagingStore.validateReplacementTargetIfNeeded(
                targetBundleURL: currentAppBundleURL,
                skipSelfReplace: skipsSelfReplace,
                language: lang
            )

            let stagingDir = try stagingStore.makeStagingSessionDirectory()
            defer { try? FileManager.default.removeItem(at: stagingDir) }

            semanticState = .updating(step: .buildingLocalApp)
            let cliURL = await resolveCLIExecutableURLOffMain()
            let pyEnv = try await LocalRepoEnvironment.resolvePythonEnvironment(
                for: cliURL,
                childEnv: childEnv,
                commandRunner: commandRunner
            )

            let targetBundleIdentifier = currentBundleIdentifier.lowercased().hasSuffix(Self.legacyBundleSuffix)
                ? "dev.semanticcraft.codex-agy-sync"
                : currentBundleIdentifier
            let builtAppURL = try await releaseClient.buildAndVerifyLocalApp(
                repoURL: repoURL,
                stagingDir: stagingDir,
                targetBundleIdentifier: targetBundleIdentifier,
                childEnv: childEnv,
                language: lang
            )

            semanticState = .updating(step: .upgradingLocalCLI)
            guard canMutateEnvironment?() ?? true else {
                throw UpdateFailureReasonError(.syncInProgress)
            }
            try await releaseClient.installLocalCLI(repoURL: repoURL, pythonEnv: pyEnv, childEnv: childEnv)

            try replaceRunningBundleIfPermitted(with: builtAppURL)

            let rawTargetVer = repoCheck.version ?? appVersion
            let targetVer = SemanticVersion.parse(rawTargetVer)?.description ?? rawTargetVer
            pendingRelaunchVersion = targetVer
            canRelaunchApp = true
            stagingStore.clearPersistedStagedDirectory()
            await probeEnvironment()
            semanticState = .upToDate(currentVersion: targetVer, message: .localRepoSynced(version: targetVer))
        } catch let wrapped as UpdateFailureReasonError {
            await probeEnvironment()
            semanticState = .failed(wrapped.reason)
        } catch {
            await probeEnvironment()
            semanticState = .failed(.from(error))
        }
    }

    private func performReleaseSync(_ release: GitHubReleaseInfo) async {
        guard canMutateEnvironment?() ?? true else {
            semanticState = .failed(.syncInProgress)
            return
        }
        guard let checksumsAsset = release.checksumsAsset else {
            semanticState = .failed(.service(.missingChecksumsAsset))
            return
        }
        guard release.wheelAsset != nil || release.appZipAsset != nil else {
            semanticState = .failed(.service(.missingUpdateAssets))
            return
        }

        let childEnv = await UpdateEnvironmentProber.shared.mergedEnvironment(from: environment)
        let lang = preferencesStore.effectiveLanguage

        do {
            if release.appZipAsset != nil {
                try UpdateStagingStore.validateReplacementTargetIfNeeded(
                    targetBundleURL: currentAppBundleURL,
                    skipSelfReplace: skipsSelfReplace,
                    language: lang
                )
            }
            let stagingDir = try stagingStore.makeStagingSessionDirectory()
            defer { try? FileManager.default.removeItem(at: stagingDir) }

            semanticState = .updating(step: .downloadingChecksums)
            let resolvedPyEnv: PythonRuntimeEnvironment?
            if release.wheelAsset != nil {
                let cliURL = await resolveCLIExecutableURLOffMain()
                resolvedPyEnv = try await LocalRepoEnvironment.resolvePythonEnvironment(
                    for: cliURL,
                    childEnv: childEnv,
                    commandRunner: commandRunner
                )
            } else {
                resolvedPyEnv = nil
            }

            let prepared = try await releaseClient.downloadAndVerifyReleaseAssets(
                release,
                checksumsAsset: checksumsAsset,
                into: stagingDir,
                appVersion: appVersion,
                childEnv: childEnv,
                language: lang,
                onStep: { [weak self] step in
                    self?.semanticState = .updating(step: step)
                }
            )

            if let wheelPath = prepared.wheelURL, let pyEnv = resolvedPyEnv {
                semanticState = .updating(step: .installingCLI)
                guard canMutateEnvironment?() ?? true else {
                    throw UpdateFailureReasonError(.syncInProgress)
                }
                try await releaseClient.installWheel(at: wheelPath, pythonEnv: pyEnv, childEnv: childEnv)
            }

            let normalizedReleaseVer = SemanticVersion.parse(release.version)?.description ?? release.version
            if let extractedApp = prepared.extractedAppURL {
                try replaceRunningBundleIfPermitted(with: extractedApp)
                pendingRelaunchVersion = normalizedReleaseVer
                canRelaunchApp = true
            }

            stagingStore.clearPersistedStagedDirectory()
            await probeEnvironment()
            let upToDateKind: UpToDateMessage = prepared.extractedAppURL != nil
                ? .releaseSyncedWithApp(tag: release.tagName)
                : .releaseSyncedCLIOnly(tag: release.tagName)
            semanticState = .upToDate(currentVersion: normalizedReleaseVer, message: upToDateKind)
        } catch let wrapped as UpdateFailureReasonError {
            await probeEnvironment()
            semanticState = .failed(wrapped.reason)
        } catch {
            await probeEnvironment()
            semanticState = .failed(.from(error))
        }
    }

    private func replaceRunningBundleIfPermitted(with stagedAppURL: URL) throws {
        guard canMutateEnvironment?() ?? true else {
            throw UpdateFailureReasonError(.syncInProgress)
        }
        currentAppBundleURL = try UpdateStagingStore.replaceRunningBundleIfPermitted(
            with: stagedAppURL,
            currentAppBundleURL: currentAppBundleURL,
            skipSelfReplace: skipsSelfReplace,
            language: preferencesStore.effectiveLanguage
        )
    }

    private func reconcilePersistedStagedRelease(recoverFromFailure: Bool = false) {
        if canRelaunchApp || pendingRelaunchVersion != nil {
            stagingStore.clearPersistedStagedDirectory()
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

        if let best = stagingStore.bestPersistedStagedCandidate(
            effectiveAppVersion: effectiveAppVersion,
            alignment: rawAlignment
        ) {
            switch semanticState {
            case .releaseAvailable(let knownRelease, _):
                if Self.isVersion(best.manifest.version, atLeast: knownRelease.version) {
                    semanticState = .stagedReady(manifest: best.manifest, stagingDir: best.dir)
                }
            case .idle, .stagedReady, .upToDate, .failed(.syncInProgress):
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
            if case .stagedReady = semanticState {
                semanticState = .idle
            }
        }
    }

    private func resolveCLIExecutableURLOffMain() async -> URL? {
        if let resolvedCLIPath {
            return URL(fileURLWithPath: resolvedCLIPath)
        }
        let customCLIPath = preferencesStore.preferences.customCLIPath
            ?? preferencesStore.defaults.string(forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
        return await UpdateEnvironmentProber.shared.probeCLIAndRepo(
            environment: environment,
            customCLIPath: customCLIPath,
            localRepoPathForSync: nil
        ).cliURL
    }

    private func discoverEditableRepoIfNeeded(
        fromCLI cliURL: URL,
        childEnv: [String: String],
        probeGenerationSnapshot: UInt64
    ) async {
        guard detectedRepoPath == nil else { return }

        if let pyInfo = try? await LocalRepoEnvironment.resolvePythonEnvironment(
            for: cliURL,
            childEnv: childEnv,
            commandRunner: commandRunner
        ),
           !Task.isCancelled,
           probeGenerationSnapshot == probeGeneration,
           let repoPath = pyInfo.repoPath
        {
            let repoURL = URL(fileURLWithPath: repoPath, isDirectory: true)
            let check = await UpdateEnvironmentProber.shared.validateRepoURL(repoURL)
            if let validated = check.validatedURL, probeGenerationSnapshot == probeGeneration {
                detectedRepoPath = validated.path
                detectedRepoVersion = check.version
            }
        }
    }

    private static func isVersion(_ lhs: String, atLeast rhs: String) -> Bool {
        if let lSem = SemanticVersion.parse(lhs), let rSem = SemanticVersion.parse(rhs) {
            return lSem >= rSem
        }
        return lhs == rhs
    }
}
