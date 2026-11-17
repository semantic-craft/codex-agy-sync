import Foundation

struct ProbedEnvironmentContext: Sendable {
    let childEnv: [String: String]
    let cliURL: URL?
    let cliSource: CLIResolutionSource
    let validatedCustomRepo: ValidatedRepoResult?
    let discoveredRepo: (path: String, version: String?)?
}

struct ValidatedRepoResult: Sendable, Equatable {
    let rawInput: String
    let validatedPath: String?
    let version: String?
}

actor UpdateEnvironmentProber {
    static let shared = UpdateEnvironmentProber()

    func mergedEnvironment(from environment: [String: String]) -> [String: String] {
        SubprocessCLIClient.mergedEnvironment(from: environment)
    }

    func validateCustomRepo(_ rawPath: String) -> ValidatedRepoResult {
        let expanded = (rawPath as NSString).expandingTildeInPath
        let customURL = URL(fileURLWithPath: expanded, isDirectory: true)
        if let validated = try? UpdatePathGuard.validateNonTemporaryURL(customURL),
           LocalRepoEnvironment.isValidSourceRepo(validated)
        {
            return ValidatedRepoResult(
                rawInput: rawPath,
                validatedPath: validated.path,
                version: LocalRepoEnvironment.parsePyprojectVersion(in: validated)
            )
        }
        return ValidatedRepoResult(rawInput: rawPath, validatedPath: nil, version: nil)
    }

    func validateRepoURL(_ rawURL: URL) -> (validatedURL: URL?, version: String?) {
        guard let validated = try? UpdatePathGuard.validateNonTemporaryURL(rawURL),
              LocalRepoEnvironment.isValidSourceRepo(validated)
        else {
            return (nil, nil)
        }
        return (validated, LocalRepoEnvironment.parsePyprojectVersion(in: validated))
    }

    func probeCLIAndRepo(
        environment: [String: String],
        customCLIPath: String?,
        localRepoPathForSync: String?
    ) -> ProbedEnvironmentContext {
        let pyBins = SubprocessCLIClient.userPythonBinDirectories()
        let childEnv = SubprocessCLIClient.mergedEnvironment(
            from: environment,
            pythonBinDirectories: pyBins
        )
        let resolved = SubprocessCLIClient.resolveExecutableWithSource(
            environment: environment,
            customCLIPath: customCLIPath,
            pythonBinDirectories: pyBins
        )
        let validatedCustom = localRepoPathForSync.map { validateCustomRepo($0) }
        let discovered = LocalRepoEnvironment.discoverCandidateRepo(
            customRepoPath: localRepoPathForSync,
            cliURL: resolved.url
        )
        return ProbedEnvironmentContext(
            childEnv: childEnv,
            cliURL: resolved.url,
            cliSource: resolved.source,
            validatedCustomRepo: validatedCustom,
            discoveredRepo: discovered
        )
    }
}
