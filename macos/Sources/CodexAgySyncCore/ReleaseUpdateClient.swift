import Foundation

struct PreparedReleaseAssets: Sendable {
    let wheelURL: URL?
    let wheelSHA256: String?
    let zipURL: URL?
    let extractedAppURL: URL?
}

struct ReleaseUpdateClient: Sendable {
    let httpFetcher: any ReleaseHTTPFetching
    let commandRunner: any SystemCommandRunning
    let stagingStore: UpdateStagingStore

    init(
        httpFetcher: any ReleaseHTTPFetching,
        commandRunner: any SystemCommandRunning,
        stagingStore: UpdateStagingStore
    ) {
        self.httpFetcher = httpFetcher
        self.commandRunner = commandRunner
        self.stagingStore = stagingStore
    }

    func fetchLatestRelease(
        from url: URL,
        appVersion: String
    ) async throws -> GitHubReleaseInfo {
        let (data, statusCode) = try await httpFetcher.fetchData(
            from: url,
            headers: [
                "Accept": "application/vnd.github+json",
                "User-Agent": "CodexAgySync/\(appVersion)",
            ],
            timeoutSeconds: 10.0
        )
        guard (200..<300).contains(statusCode) else {
            throw UpdateServiceError.httpError(statusCode: statusCode)
        }
        return try GitHubReleaseInfo.decode(from: data)
    }

    func downloadAndVerifyReleaseAssets(
        _ release: GitHubReleaseInfo,
        checksumsAsset: ReleaseAssetInfo,
        into stagingDir: URL,
        appVersion: String,
        childEnv: [String: String],
        language: ResolvedLanguage,
        onStep: @MainActor (UpdateProgressStep) -> Void
    ) async throws -> PreparedReleaseAssets {
        await onStep(.downloadingChecksums)
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
            await onStep(.downloadingWheel(fileName: safeWheelName))
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
            await onStep(.downloadingAppArchive(fileName: safeZipName))
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

            await onStep(.verifyingAppArchive)
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
            let extractedApp = UpdateStagingStore.resolveExtractedAppBundle(in: extractDir)
            try await stagingStore.verifyCodesign(
                of: extractedApp,
                inside: extractDir,
                childEnv: childEnv,
                language: language
            )
            verifiedExtractedAppURL = extractedApp
        }

        return PreparedReleaseAssets(
            wheelURL: verifiedWheelURL,
            wheelSHA256: wheelSHA256,
            zipURL: verifiedZipURL,
            extractedAppURL: verifiedExtractedAppURL
        )
    }

    func installWheel(
        at wheelURL: URL,
        pythonEnv: PythonRuntimeEnvironment,
        childEnv: [String: String]
    ) async throws {
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

    func buildAndVerifyLocalApp(
        repoURL: URL,
        stagingDir: URL,
        targetBundleIdentifier: String,
        childEnv: [String: String],
        language: ResolvedLanguage
    ) async throws -> URL {
        let appStageDir = stagingDir.appendingPathComponent("app-out", isDirectory: true)
        try FileManager.default.createDirectory(at: appStageDir, withIntermediateDirectories: true)
        let buildScript = repoURL.appendingPathComponent("scripts/build_menubar_app.sh")
        let buildResult = try await commandRunner.run(
            executableURL: buildScript,
            arguments: [
                "--output-dir", appStageDir.path,
                "--bundle-id", targetBundleIdentifier,
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
        let builtAppURL = UpdateStagingStore.resolveExtractedAppBundle(in: appStageDir)
        try await stagingStore.verifyCodesign(
            of: builtAppURL,
            inside: appStageDir,
            childEnv: childEnv,
            language: language
        )
        return builtAppURL
    }

    func installLocalCLI(
        repoURL: URL,
        pythonEnv: PythonRuntimeEnvironment,
        childEnv: [String: String]
    ) async throws {
        var pipArgs = ["-m", "pip", "install", "--no-build-isolation"]
        if !pythonEnv.inVenv {
            pipArgs.append("--user")
            if pythonEnv.supportsBreakSystemPackages {
                pipArgs.append("--break-system-packages")
            }
        }
        if pythonEnv.editable {
            pipArgs.append(contentsOf: ["--no-deps", "--force-reinstall", "-e", repoURL.path])
        } else {
            pipArgs.append(contentsOf: ["--no-deps", "--upgrade", repoURL.path])
        }

        let pipResult = try await commandRunner.run(
            executableURL: pythonEnv.pythonURL,
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
    }
}
