import Foundation

struct UpdateStagingStore: Sendable {
    static let stagedManifestFileName = "stage-manifest.json"
    static let checkpointDefaultsKey = "updateCheckCheckpoint"

    let stagingRootOverride: URL?
    let commandRunner: any SystemCommandRunning

    init(
        stagingRootOverride: URL? = nil,
        commandRunner: any SystemCommandRunning = DefaultSystemCommandRunner()
    ) {
        self.stagingRootOverride = stagingRootOverride
        self.commandRunner = commandRunner
    }

    static func loadCheckpoint(from defaults: UserDefaults) -> UpdateCheckCheckpoint {
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

    static func saveCheckpoint(_ checkpoint: UpdateCheckCheckpoint, to defaults: UserDefaults) {
        if let encoded = try? JSONEncoder().encode(checkpoint) {
            defaults.set(encoded, forKey: checkpointDefaultsKey)
        }
    }

    static func hasCompletedCooldown(
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

    func updatesRootURL() throws -> URL {
        if let stagingRootOverride {
            return try UpdatePathGuard.validateNonTemporaryURL(stagingRootOverride)
        }
        return try UpdatePathGuard.defaultStagingRootURL()
    }

    func stagedVersionsRootURL() throws -> URL {
        let root = try updatesRootURL()
        return try UpdatePathGuard.validateNonTemporaryURL(
            root.appendingPathComponent("staged", isDirectory: true)
        )
    }

    func tmpStagingRootURL() throws -> URL {
        let root = try updatesRootURL()
        return try UpdatePathGuard.validateNonTemporaryURL(
            root.appendingPathComponent("tmp", isDirectory: true)
        )
    }

    func makeStagingSessionDirectory() throws -> URL {
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

    func clearPersistedStagedDirectory() {
        guard let stagedRoot = try? stagedVersionsRootURL() else { return }
        UpdatePathGuard.pruneStagedVersions(in: stagedRoot, keepingVersion: nil)
    }

    func persistStagedDirectory(
        from tmpDir: URL,
        manifest: StagedUpdateManifest
    ) throws -> URL {
        let fm = FileManager.default
        let manifestURL = tmpDir.appendingPathComponent(Self.stagedManifestFileName, isDirectory: false)
        let encoded = try JSONEncoder().encode(manifest)
        try encoded.write(to: manifestURL, options: .atomic)

        let stagedRoot = try stagedVersionsRootURL()
        try fm.createDirectory(at: stagedRoot, withIntermediateDirectories: true)
        let versionDir = try UpdatePathGuard.stagedAssetFileURL(for: manifest.version, in: stagedRoot)
        if fm.fileExists(atPath: versionDir.path) {
            _ = try fm.replaceItemAt(versionDir, withItemAt: tmpDir)
        } else {
            try fm.moveItem(at: tmpDir, to: versionDir)
        }
        UpdatePathGuard.pruneStagedVersions(in: stagedRoot, keepingVersion: versionDir.lastPathComponent)
        return versionDir
    }

    func verifyStagedFilesPresent(manifest: StagedUpdateManifest, in stagingDir: URL) -> Bool {
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
                  Self.isRealAppBundleDirectory(appURL, inside: extractedRoot, fileManager: fm)
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

    func isStagedManifestApplicable(
        _ manifest: StagedUpdateManifest,
        effectiveAppVersion: String,
        alignment: VersionAlignmentState
    ) -> Bool {
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

    func bestPersistedStagedCandidate(
        effectiveAppVersion: String,
        alignment: VersionAlignmentState
    ) -> (manifest: StagedUpdateManifest, dir: URL)? {
        let fm = FileManager.default
        guard let stagedRoot = try? stagedVersionsRootURL(),
              let children = try? fm.contentsOfDirectory(
                  at: stagedRoot,
                  includingPropertiesForKeys: [.isDirectoryKey],
                  options: [.skipsHiddenFiles]
              )
        else {
            return nil
        }

        var bestCandidate: (manifest: StagedUpdateManifest, dir: URL, semver: SemanticVersion?)?
        for child in children {
            let values = try? child.resourceValues(forKeys: [.isDirectoryKey])
            guard values?.isDirectory == true else { continue }
            let manifestURL = child.appendingPathComponent(Self.stagedManifestFileName, isDirectory: false)
            guard let data = try? Data(contentsOf: manifestURL),
                  let manifest = try? JSONDecoder().decode(StagedUpdateManifest.self, from: data),
                  verifyStagedFilesPresent(manifest: manifest, in: child),
                  isStagedManifestApplicable(
                      manifest,
                      effectiveAppVersion: effectiveAppVersion,
                      alignment: alignment
                  )
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
            return (best.manifest, best.dir)
        }
        UpdatePathGuard.pruneStagedVersions(in: stagedRoot, keepingVersion: nil)
        return nil
    }

    func prepareValidatedStagedAssets(
        for manifest: StagedUpdateManifest,
        in stagedDir: URL,
        targetBundleURL: URL?,
        skipSelfReplace: Bool,
        childEnv: [String: String],
        language: ResolvedLanguage
    ) async throws -> (stagedAppURL: URL?, verifiedWheelURL: URL?) {
        let validatedStageDir = try UpdatePathGuard.validateNonTemporaryURL(stagedDir)
        let manifestURL = validatedStageDir.appendingPathComponent(Self.stagedManifestFileName, isDirectory: false)
        guard FileManager.default.fileExists(atPath: manifestURL.path) else {
            throw UpdateFailureReasonError(.stagedArtifactInvalid("missing manifest"))
        }

        var stagedAppURL: URL?
        if let appBundleName = manifest.appBundleName {
            try Self.validateReplacementTargetIfNeeded(
                targetBundleURL: targetBundleURL,
                skipSelfReplace: skipSelfReplace,
                language: language
            )
            let extractedRoot = validatedStageDir.appendingPathComponent("extracted", isDirectory: true)
            let candidateApp: URL
            do {
                candidateApp = try UpdatePathGuard.stagedAssetFileURL(for: appBundleName, in: extractedRoot)
            } catch {
                throw UpdateFailureReasonError(.stagedArtifactInvalid(error.localizedDescription))
            }
            guard Self.isRealAppBundleDirectory(candidateApp, inside: extractedRoot) else {
                throw UpdateFailureReasonError(.stagedArtifactInvalid("invalid app bundle directory \(appBundleName)"))
            }
            do {
                try await verifyCodesign(
                    of: candidateApp,
                    inside: extractedRoot,
                    childEnv: childEnv,
                    language: language
                )
            } catch {
                throw UpdateFailureReasonError(.stagedArtifactInvalid(error.localizedDescription))
            }
            stagedAppURL = candidateApp
        }

        var verifiedWheelURL: URL?
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
            verifiedWheelURL = candidateWheel
        }

        return (stagedAppURL, verifiedWheelURL)
    }

    func verifyCodesign(
        of appURL: URL,
        inside parentDir: URL? = nil,
        childEnv: [String: String],
        language: ResolvedLanguage
    ) async throws {
        guard Self.isRealAppBundleDirectory(appURL, inside: parentDir) else {
            let missingMsg = AppCopy.updatesMissingAppBundle(appURL.lastPathComponent, in: language)
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

    static func isRealAppBundleDirectory(
        _ candidate: URL,
        inside parentDir: URL? = nil,
        fileManager: FileManager = .default
    ) -> Bool {
        guard fileManager.fileExists(atPath: candidate.path) else {
            return false
        }
        var probe = candidate
        probe.removeAllCachedResourceValues()
        guard let values = try? probe.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
              values.isDirectory == true,
              values.isSymbolicLink != true
        else {
            return false
        }
        if let parentDir {
            let resolvedCandidateParent = candidate.resolvingSymlinksInPath().deletingLastPathComponent().standardizedFileURL
            let resolvedParent = parentDir.resolvingSymlinksInPath().standardizedFileURL
            guard resolvedCandidateParent == resolvedParent else {
                return false
            }
        }
        return true
    }

    static func resolveExtractedAppBundle(
        in extractDir: URL,
        fileManager: FileManager = .default
    ) -> URL {
        let preferred = extractDir.appendingPathComponent("CodexAgySync.app", isDirectory: true)
        if isRealAppBundleDirectory(preferred, inside: extractDir, fileManager: fileManager) {
            return preferred
        }
        if (try? fileManager.attributesOfItem(atPath: preferred.path)) != nil {
            return extractDir.appendingPathComponent(".invalid-CodexAgySync.app", isDirectory: true)
        }
        if let children = try? fileManager.contentsOfDirectory(
            at: extractDir,
            includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey],
            options: [.skipsHiddenFiles]
        ) {
            let apps = children.filter {
                $0.pathExtension.lowercased() == "app"
                    && isRealAppBundleDirectory($0, inside: extractDir, fileManager: fileManager)
            }
            if apps.count == 1 {
                return apps[0]
            }
        }
        return preferred
    }

    static func validateReplacementTargetIfNeeded(
        targetBundleURL: URL?,
        skipSelfReplace: Bool,
        language: ResolvedLanguage,
        fileManager: FileManager = .default
    ) throws {
        if skipSelfReplace {
            return
        }
        guard let targetBundleURL else {
            throw UpdateServiceError.targetBundleNotWritable(
                AppCopy.text(.updatesErrNoWritableBundleFallback, in: language)
            )
        }
        let validatedTarget = try UpdatePathGuard.validateNonTemporaryURL(targetBundleURL)
        let parentDir = validatedTarget.deletingLastPathComponent()
        guard fileManager.fileExists(atPath: parentDir.path), fileManager.isWritableFile(atPath: parentDir.path) else {
            throw UpdateServiceError.targetBundleNotWritable(parentDir.path)
        }
        if fileManager.fileExists(atPath: validatedTarget.path), !fileManager.isWritableFile(atPath: validatedTarget.path) {
            throw UpdateServiceError.targetBundleNotWritable(validatedTarget.path)
        }
    }

    static func replaceRunningBundleIfPermitted(
        with stagedAppURL: URL,
        currentAppBundleURL: URL?,
        skipSelfReplace: Bool,
        language: ResolvedLanguage,
        fileManager: FileManager = .default
    ) throws -> URL? {
        if skipSelfReplace {
            return currentAppBundleURL
        }
        try validateReplacementTargetIfNeeded(
            targetBundleURL: currentAppBundleURL,
            skipSelfReplace: false,
            language: language,
            fileManager: fileManager
        )
        guard let targetBundleURL = currentAppBundleURL else {
            throw UpdateServiceError.targetBundleNotWritable(
                AppCopy.text(.updatesErrNoWritableBundleFallback, in: language)
            )
        }
        let stagedBundleName = stagedAppURL.lastPathComponent
        var effectiveBundleURL = targetBundleURL
        if fileManager.fileExists(atPath: targetBundleURL.path) {
            if let resultingURL = try fileManager.replaceItemAt(targetBundleURL, withItemAt: stagedAppURL) {
                effectiveBundleURL = resultingURL
            }
        } else {
            try fileManager.moveItem(at: stagedAppURL, to: targetBundleURL)
        }
        if effectiveBundleURL.lastPathComponent != "CodexAgySync.app",
           stagedBundleName == "CodexAgySync.app"
        {
            let renamedURL = effectiveBundleURL
                .deletingLastPathComponent()
                .appendingPathComponent("CodexAgySync.app", isDirectory: true)
            if fileManager.fileExists(atPath: renamedURL.path) {
                if let replaced = try? fileManager.replaceItemAt(renamedURL, withItemAt: effectiveBundleURL) {
                    effectiveBundleURL = replaced
                }
            } else if (try? fileManager.moveItem(at: effectiveBundleURL, to: renamedURL)) != nil {
                effectiveBundleURL = renamedURL
            }
        }
        return effectiveBundleURL
    }
}
