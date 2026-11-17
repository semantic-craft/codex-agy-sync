import Foundation

struct PythonProbePayload: Decodable, Sendable {
    let executable: String
    let in_venv: Bool
    let editable: Bool
    let repo_path: String?
    let supports_break_system_packages: Bool?
}

struct PythonRuntimeEnvironment: Sendable {
    let pythonURL: URL
    let inVenv: Bool
    let editable: Bool
    let repoPath: String?
    let supportsBreakSystemPackages: Bool
}

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
            let resolvedCLI = cliURL.resolvingSymlinksInPath()
            let ancestorRepo = resolvedCLI.deletingLastPathComponent().deletingLastPathComponent()
            if let validated = try? UpdatePathGuard.validateNonTemporaryURL(ancestorRepo),
               isValidSourceRepo(validated)
            {
                return (validated.path, parsePyprojectVersion(in: validated))
            }
        }
        return nil
    }

    static func resolvePythonEnvironment(
        for cliURL: URL?,
        childEnv: [String: String],
        commandRunner: any SystemCommandRunning
    ) async throws -> PythonRuntimeEnvironment {
        guard let candidatePython = findPythonExecutable(for: cliURL, childEnv: childEnv) else {
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

    static func findPythonExecutable(
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
