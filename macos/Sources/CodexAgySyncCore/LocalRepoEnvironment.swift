import Foundation

enum LocalRepoEnvironment {
    private static let pyprojectVersionRegex = try! NSRegularExpression(
        pattern: #"(?m)^version\s*=\s*"([^"]+)""#
    )

    static func resolveSyncStateDirectoryURL(
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

    static func isValidSourceRepo(_ repoURL: URL, fileManager: FileManager = .default) -> Bool {
        guard let validatedURL = try? UpdatePathGuard.validateNonTemporaryURL(repoURL) else {
            return false
        }
        let pyproject = validatedURL.appendingPathComponent("pyproject.toml").path
        let buildScript = validatedURL.appendingPathComponent("scripts/build_menubar_app.sh").path
        return fileManager.fileExists(atPath: pyproject) && fileManager.isExecutableFile(atPath: buildScript)
    }

    static func parsePyprojectVersion(in repoURL: URL) -> String? {
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

    static func discoverCandidateRepo(
        customRepoPath: String?,
        cliURL: URL?
    ) -> (path: String, version: String?)? {
        if let customRepoPath {
            let customURL = URL(fileURLWithPath: customRepoPath, isDirectory: true)
            if let validated = try? UpdatePathGuard.validateNonTemporaryURL(customURL),
               isValidSourceRepo(validated)
            {
                return (validated.path, parsePyprojectVersion(in: validated))
            }
        }
        if let cliURL {
            var cursor = cliURL.resolvingSymlinksInPath().deletingLastPathComponent()
            for _ in 0..<4 {
                if let validated = try? UpdatePathGuard.validateNonTemporaryURL(cursor),
                   isValidSourceRepo(validated)
                {
                    return (validated.path, parsePyprojectVersion(in: validated))
                }
                let parent = cursor.deletingLastPathComponent()
                if parent.path == cursor.path {
                    break
                }
                cursor = parent
            }
        }
        return nil
    }
}
