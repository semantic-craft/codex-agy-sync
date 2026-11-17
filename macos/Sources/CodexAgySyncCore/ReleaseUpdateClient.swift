import Foundation

struct PreparedReleaseAssets: Sendable {
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
        guard let appZipAsset = release.appZipAsset else {
            throw UpdateServiceError.missingUpdateAssets
        }

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

        let zipDest = try UpdatePathGuard.stagedAssetFileURL(for: appZipAsset.name, in: stagingDir)
        let safeZipName = zipDest.lastPathComponent
        await onStep(.downloadingAppArchive(fileName: safeZipName))
        let zipStatus = try await httpFetcher.downloadFile(
            from: appZipAsset.browserDownloadURL,
            headers: ["User-Agent": "CodexAgySync/\(appVersion)"],
            timeoutSeconds: 60.0,
            to: zipDest
        )
        guard (200..<300).contains(zipStatus) else {
            throw UpdateServiceError.httpError(statusCode: zipStatus)
        }
        do {
            _ = try SHA256Verifier.verify(
                fileURL: zipDest,
                assetName: safeZipName,
                checksums: checksums
            )
        } catch {
            try? FileManager.default.removeItem(at: zipDest)
            throw error
        }

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

        return PreparedReleaseAssets(
            zipURL: zipDest,
            extractedAppURL: extractedApp
        )
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
}
