import AppKit
import Combine
import Foundation

@MainActor
public final class UpdateViewModel: ObservableObject {
    public nonisolated static let defaultReleaseAPIURL = "https://api.github.com/repos/semantic-craft/codex-agy-sync/releases/latest"
    public nonisolated static let releaseAPIEnvKey = "MENUBAR_RELEASE_API_URL"
    public nonisolated static let skipSelfReplaceEnvKey = "MENUBAR_SKIP_SELF_REPLACE"
    public nonisolated static let checkpointDefaultsKey = UpdateStagingStore.checkpointDefaultsKey
    public nonisolated static let successCooldownSeconds: TimeInterval = UpdateCheckCheckpoint.successCooldownSeconds
    public nonisolated static let failureBackoffSeconds: TimeInterval = UpdateCheckCheckpoint.failureBackoffSeconds

    private nonisolated static let legacyBundleSuffix = ["codex", "agy", "hand" + "off"].joined(separator: "-")

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
    public private(set) var currentAppBundleURL: URL?
    public let preferencesStore: PreferencesStore

    private let environment: [String: String]
    private let commandRunner: any SystemCommandRunning
    private let stagingStore: UpdateStagingStore
    private let releaseClient: ReleaseUpdateClient
    private let relaunchExecutor: any RelaunchExecuting
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
        await performReleaseCheck(trigger: .manual)
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

    public func syncUpdateNow() async {
        guard !availability.isBusy, !isManualActionInFlight else { return }
        isManualActionInFlight = true
        defer { isManualActionInFlight = false }
        opportunisticGeneration &+= 1
        isOpportunisticCheckInFlight = false
        await performManualSyncUpdateNow()
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

    private func performReleaseCheck(trigger: UpdateTrigger) async {
        let isOpportunistic = trigger.isOpportunistic
        var capturedOppGen: UInt64?

        if isOpportunistic {
            guard !availability.isBusy, !isManualActionInFlight, !isOpportunisticCheckInFlight else { return }
            let policy = preferencesStore.preferences.autoUpdatePolicy
            guard policy != .off, !canRelaunchApp, pendingRelaunchVersion == nil else { return }

            let now = nowProvider().timeIntervalSince1970
            let checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
            guard Self.hasCompletedCooldown(now: now, checkpoint: checkpoint) else { return }

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
            handleCheckError(UpdateServiceError.invalidReleaseURL(rawURL), isOpportunistic: isOpportunistic, priorState: priorState)
            return
        }

        var checkpoint = Self.loadCheckpoint(from: preferencesStore.defaults)
        checkpoint.lastAttemptEpoch = nowProvider().timeIntervalSince1970
        Self.saveCheckpoint(checkpoint, to: preferencesStore.defaults)

        do {
            let release = try await releaseClient.fetchLatestRelease(from: url, appVersion: appVersion)
            if let myOppGen = capturedOppGen {
                guard myOppGen == opportunisticGeneration else { return }
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
            stagingStore.clearPersistedStagedDirectory()
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
           stagingStore.verifyStagedFilesPresent(manifest: manifest, in: stagingDir)
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

        setSemanticState(.failed(.service(.missingUpdateAssets)))
    }

    private func performInstallStaged(manifest: StagedUpdateManifest, stagedDir: URL) async {
        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)
        let lang = preferencesStore.effectiveLanguage
        setSemanticState(.updating(step: .applyingStagedUpdate(tagName: manifest.tagName)))

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
                let cliURL = resolvedCLIExecutableURL()
                let pyEnv = try await LocalRepoEnvironment.resolvePythonEnvironment(
                    for: cliURL,
                    childEnv: childEnv,
                    commandRunner: commandRunner
                )
                guard canMutateEnvironment?() ?? true else {
                    throw UpdateFailureReasonError(.handoffInProgress)
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
            setSemanticState(.upToDate(currentVersion: manifest.version, message: upToDateKind))

            if validated.stagedAppURL != nil, !skipsSelfReplace {
                _ = relaunchApp()
            }
        } catch let wrapped as UpdateFailureReasonError {
            if case .stagedArtifactInvalid = wrapped.reason {
                stagingStore.clearPersistedStagedDirectory()
            }
            await probeEnvironment()
            setSemanticState(.failed(wrapped.reason))
        } catch {
            await probeEnvironment()
            setSemanticState(.failed(.from(error)))
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

            setSemanticState(.updating(step: .stagingRelease(tagName: release.tagName)))
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

    private func performLocalRepoSync(repoPath: String) async {
        guard canMutateEnvironment?() ?? true else {
            setSemanticState(.failed(.handoffInProgress))
            return
        }
        let rawRepoURL = URL(fileURLWithPath: (repoPath as NSString).expandingTildeInPath, isDirectory: true)
        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)
        let lang = preferencesStore.effectiveLanguage

        do {
            let repoURL = try UpdatePathGuard.validateNonTemporaryURL(rawRepoURL)
            guard Self.isValidSourceRepo(repoURL) else {
                throw UpdateServiceError.invalidLocalRepo(repoPath)
            }
            try UpdateStagingStore.validateReplacementTargetIfNeeded(
                targetBundleURL: currentAppBundleURL,
                skipSelfReplace: skipsSelfReplace,
                language: lang
            )

            let stagingDir = try stagingStore.makeStagingSessionDirectory()
            defer { try? FileManager.default.removeItem(at: stagingDir) }

            setSemanticState(.updating(step: .buildingLocalApp))
            let cliURL = resolvedCLIExecutableURL()
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

            setSemanticState(.updating(step: .upgradingLocalCLI))
            guard canMutateEnvironment?() ?? true else {
                throw UpdateFailureReasonError(.handoffInProgress)
            }
            try await releaseClient.installLocalCLI(repoURL: repoURL, pythonEnv: pyEnv, childEnv: childEnv)

            try replaceRunningBundleIfPermitted(with: builtAppURL)

            let rawTargetVer = Self.parsePyprojectVersion(in: repoURL) ?? appVersion
            let targetVer = SemanticVersion.parse(rawTargetVer)?.description ?? rawTargetVer
            pendingRelaunchVersion = targetVer
            canRelaunchApp = true
            stagingStore.clearPersistedStagedDirectory()
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

            setSemanticState(.updating(step: .downloadingChecksums))
            let resolvedPyEnv: PythonRuntimeEnvironment?
            if release.wheelAsset != nil {
                let cliURL = resolvedCLIExecutableURL()
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
                    self?.setSemanticState(.updating(step: step))
                }
            )

            if let wheelPath = prepared.wheelURL, let pyEnv = resolvedPyEnv {
                setSemanticState(.updating(step: .installingCLI))
                guard canMutateEnvironment?() ?? true else {
                    throw UpdateFailureReasonError(.handoffInProgress)
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
            setSemanticState(.upToDate(currentVersion: normalizedReleaseVer, message: upToDateKind))
        } catch let wrapped as UpdateFailureReasonError {
            await probeEnvironment()
            setSemanticState(.failed(wrapped.reason))
        } catch {
            await probeEnvironment()
            setSemanticState(.failed(.from(error)))
        }
    }

    private func replaceRunningBundleIfPermitted(with stagedAppURL: URL) throws {
        guard canMutateEnvironment?() ?? true else {
            throw UpdateFailureReasonError(.handoffInProgress)
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
            alignment: alignment
        ) {
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
            if case .stagedReady = semanticState {
                semanticState = .idle
            }
        }
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

        let projected = UpdateStateProjector.project(
            semanticState: semanticState,
            lastCheckedEpoch: lastCheckedEpoch,
            lastBackgroundErrorReason: lastBackgroundErrorReason,
            canRelaunchApp: canRelaunchApp,
            pendingRelaunchVersion: pendingRelaunchVersion,
            effectiveAppVersion: effectiveAppVersion,
            now: nowProvider(),
            in: lang
        )
        lastCheckedDisplayText = projected.lastCheckedDisplayText
        lastBackgroundCheckError = projected.lastBackgroundCheckError
        availability = projected.availability
        statusMessage = projected.statusMessage
        stagedManifest = projected.stagedManifest
        popoverNotice = projected.popoverNotice
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

    private func updateRepoDiscovery(fromCLI cliURL: URL?) {
        if let found = LocalRepoEnvironment.discoverCandidateRepo(
            customRepoPath: preferencesStore.preferences.localRepoPathForSync,
            cliURL: cliURL
        ) {
            detectedRepoPath = found.path
            detectedRepoVersion = found.version
            return
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

        if let pyInfo = try? await LocalRepoEnvironment.resolvePythonEnvironment(
            for: cliURL,
            childEnv: childEnv,
            commandRunner: commandRunner
        ),
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

    private static func isVersion(_ lhs: String, atLeast rhs: String) -> Bool {
        if let lSem = SemanticVersion.parse(lhs), let rSem = SemanticVersion.parse(rhs) {
            return lSem >= rSem
        }
        return lhs == rhs
    }

    public static func resolveSyncStateDirectoryURL(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default
    ) -> URL {
        LocalRepoEnvironment.resolveSyncStateDirectoryURL(environment: environment, fileManager: fileManager)
    }

    public static func isValidSourceRepo(_ repoURL: URL, fileManager: FileManager = .default) -> Bool {
        LocalRepoEnvironment.isValidSourceRepo(repoURL, fileManager: fileManager)
    }

    public static func parsePyprojectVersion(in repoURL: URL) -> String? {
        LocalRepoEnvironment.parsePyprojectVersion(in: repoURL)
    }

    public static func loadCheckpoint(from defaults: UserDefaults) -> UpdateCheckCheckpoint {
        UpdateStagingStore.loadCheckpoint(from: defaults)
    }

    public static func saveCheckpoint(_ checkpoint: UpdateCheckCheckpoint, to defaults: UserDefaults) {
        UpdateStagingStore.saveCheckpoint(checkpoint, to: defaults)
    }

    public static func hasCompletedCooldown(
        now: TimeInterval,
        checkpoint: UpdateCheckCheckpoint
    ) -> Bool {
        UpdateStagingStore.hasCompletedCooldown(now: now, checkpoint: checkpoint)
    }

    nonisolated static func isRealAppBundleDirectory(
        _ candidate: URL,
        inside parentDir: URL? = nil,
        fileManager: FileManager = .default
    ) -> Bool {
        UpdateStagingStore.isRealAppBundleDirectory(candidate, inside: parentDir, fileManager: fileManager)
    }

    nonisolated static func resolveExtractedAppBundle(
        in extractDir: URL,
        fileManager: FileManager = .default
    ) -> URL {
        UpdateStagingStore.resolveExtractedAppBundle(in: extractDir, fileManager: fileManager)
    }

    public static func findPythonExecutable(
        for cliURL: URL?,
        childEnv: [String: String],
        fileManager: FileManager = .default
    ) -> URL? {
        LocalRepoEnvironment.findPythonExecutable(for: cliURL, childEnv: childEnv, fileManager: fileManager)
    }
}
