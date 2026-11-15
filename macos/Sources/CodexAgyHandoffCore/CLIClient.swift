import Darwin
import Foundation

public enum CLIResolutionSource: String, Codable, Equatable, Sendable {
    case envOverride = "env_override"
    case customPath = "custom_path"
    case autoDiscovered = "auto_discovered"
    case missing = "missing"

    public var displayName: String {
        switch self {
        case .envOverride:
            return "环境变量"
        case .customPath:
            return "手动指定"
        case .autoDiscovered:
            return "自动发现"
        case .missing:
            return "未找到"
        }
    }
}

public protocol CLIRunnerProtocol: Sendable {
    func fetchFeed(limit: Int) async throws -> MenubarFeedSnapshot
    func executeHandoff(workspacePath: String, sourceId: String) async throws -> HandoffReceiptPayload
    func reconcilePending(workspacePath: String?) async throws -> ReconcileSummaryPayload
}

public extension CLIRunnerProtocol {
    func reconcilePending() async throws -> ReconcileSummaryPayload {
        try await reconcilePending(workspacePath: nil)
    }
}

public enum CLIClientError: LocalizedError, Sendable, Equatable {
    case cliMissing(searchedLocations: [String])
    case timedOut(command: [String], seconds: TimeInterval)
    case structuredError(CLIErrorPayload, exitCode: Int32)
    case nonZeroExit(exitCode: Int32, stderr: String)
    case decodingFailed(String)
    case launchFailed(String)

    public var isMissingExecutable: Bool {
        if case .cliMissing = self {
            return true
        }
        return false
    }

    public var errorDescription: String? {
        switch self {
        case .cliMissing:
            return "未找到 codex-agy-sync 命令行工具，请点击“选择工具…”指定路径。"
        case .timedOut(_, let seconds):
            return "命令行工具响应超时（超过 \(Int(seconds)) 秒），请稍后重试。"
        case .structuredError(let payload, _):
            if let cause = payload.cause, !cause.isEmpty {
                return "\(payload.message)（\(cause)）"
            }
            return payload.message
        case .nonZeroExit(let code, let stderr):
            let trimmed = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? "命令执行返回错误码 \(code)" : "命令执行返回错误码 \(code)：\(trimmed)"
        case .decodingFailed(let detail):
            return "无法解析工具输出：\(detail)"
        case .launchFailed(let detail):
            return "无法启动命令行工具：\(detail)"
        }
    }
}

private final class LockedBox<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: Value

    init(_ initial: Value) {
        self.storage = initial
    }

    func set(_ value: Value) {
        lock.lock()
        storage = value
        lock.unlock()
    }

    func get() -> Value {
        lock.lock()
        let copy = storage
        lock.unlock()
        return copy
    }
}

public final class SubprocessCLIClient: CLIRunnerProtocol, @unchecked Sendable {
    public static let cliExecutableDefaultsKey = "cliExecutablePath"

    private let defaultsSuiteName: String?
    private let environment: [String: String]
    private let timeoutSeconds: TimeInterval
    private let executableOverride: URL?

    public init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaultsSuiteName: String? = nil,
        timeoutSeconds: TimeInterval = 15.0,
        executableOverride: URL? = nil
    ) {
        self.environment = environment
        self.defaultsSuiteName = defaultsSuiteName
        self.timeoutSeconds = timeoutSeconds
        self.executableOverride = executableOverride
    }

    public static func userPythonBinDirectories(
        homeDirectory: URL? = nil,
        fileManager: FileManager = .default
    ) -> [String] {
        let home = homeDirectory ?? fileManager.homeDirectoryForCurrentUser
        let pyRoot = home.appendingPathComponent("Library/Python", isDirectory: true)
        guard let children = try? fileManager.contentsOfDirectory(
            at: pyRoot,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) else {
            return []
        }

        let sortedDirs = children
            .filter { (try? $0.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true }
            .sorted { lhs, rhs in
                let lv = lhs.lastPathComponent.split(separator: ".").map { Int($0) ?? -1 }
                let rv = rhs.lastPathComponent.split(separator: ".").map { Int($0) ?? -1 }
                for idx in 0..<max(lv.count, rv.count) {
                    let a = idx < lv.count ? lv[idx] : 0
                    let b = idx < rv.count ? rv[idx] : 0
                    if a != b { return a > b }
                }
                return lhs.lastPathComponent > rhs.lastPathComponent
            }

        return sortedDirs.map { $0.appendingPathComponent("bin", isDirectory: true).path }
    }

    public static func candidateExecutableEntries(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil
    ) -> [(path: String, source: CLIResolutionSource)] {
        var entries: [(path: String, source: CLIResolutionSource)] = []
        var seen = Set<String>()
        let home = homeDirectory ?? fileManager.homeDirectoryForCurrentUser

        func appendCandidate(_ raw: String?, source: CLIResolutionSource) {
            guard let raw else { return }
            let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { return }
            let expanded = (trimmed as NSString).expandingTildeInPath
            if seen.insert(expanded).inserted {
                entries.append((expanded, source))
            }
        }

        if let rawOverride = environment["CODEX_AGY_SYNC_BIN"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !rawOverride.isEmpty
        {
            let expanded = (rawOverride as NSString).expandingTildeInPath
            return [(expanded, .envOverride)]
        }

        appendCandidate(defaults.string(forKey: cliExecutableDefaultsKey), source: .customPath)

        if let pathEnv = environment["PATH"], !pathEnv.isEmpty {
            for dir in pathEnv.split(separator: ":") {
                let path = URL(fileURLWithPath: String(dir))
                    .appendingPathComponent("codex-agy-sync")
                    .path
                appendCandidate(path, source: .autoDiscovered)
            }
        }

        appendCandidate(
            home.appendingPathComponent(".local/bin/codex-agy-sync").path,
            source: .autoDiscovered
        )
        for pyBin in userPythonBinDirectories(homeDirectory: home, fileManager: fileManager) {
            appendCandidate(
                URL(fileURLWithPath: pyBin).appendingPathComponent("codex-agy-sync").path,
                source: .autoDiscovered
            )
        }
        appendCandidate("/opt/homebrew/bin/codex-agy-sync", source: .autoDiscovered)
        appendCandidate("/usr/local/bin/codex-agy-sync", source: .autoDiscovered)

        return entries
    }

    public static func candidateExecutablePaths(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil
    ) -> [String] {
        candidateExecutableEntries(
            environment: environment,
            defaults: defaults,
            fileManager: fileManager,
            homeDirectory: homeDirectory
        ).map(\.path)
    }

    public static func resolveExecutableWithSource(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil
    ) -> (url: URL?, source: CLIResolutionSource, searched: [String]) {
        let entries = candidateExecutableEntries(
            environment: environment,
            defaults: defaults,
            fileManager: fileManager,
            homeDirectory: homeDirectory
        )
        let searched = entries.map(\.path)
        for entry in entries where fileManager.isExecutableFile(atPath: entry.path) {
            return (URL(fileURLWithPath: entry.path), entry.source, searched)
        }
        return (nil, .missing, searched)
    }

    public static func resolveExecutableURL(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil
    ) -> (url: URL?, searched: [String]) {
        let resolved = resolveExecutableWithSource(
            environment: environment,
            defaults: defaults,
            fileManager: fileManager,
            homeDirectory: homeDirectory
        )
        return (resolved.url, resolved.searched)
    }

    public static func mergedEnvironment(
        from environment: [String: String],
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil
    ) -> [String: String] {
        var env = environment
        let home = homeDirectory ?? fileManager.homeDirectoryForCurrentUser
        let homeLocalBin = home.appendingPathComponent(".local/bin").path
        let pyBins = userPythonBinDirectories(homeDirectory: home, fileManager: fileManager)
        let extraPathEntries = [homeLocalBin] + pyBins + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        var existing = (env["PATH"] ?? "").split(separator: ":").map(String.init)
        for entry in extraPathEntries where !existing.contains(entry) {
            existing.append(entry)
        }
        env["PATH"] = existing.joined(separator: ":")
        return env
    }

    public func fetchFeed(limit: Int = 6) async throws -> MenubarFeedSnapshot {
        try await runAndDecode(
            MenubarFeedSnapshot.self,
            arguments: ["menubar-feed", "--limit", String(limit)]
        )
    }

    public func executeHandoff(
        workspacePath: String,
        sourceId: String
    ) async throws -> HandoffReceiptPayload {
        try await runAndDecode(
            HandoffReceiptPayload.self,
            arguments: [
                "handoff",
                "--workspace",
                workspacePath,
                "--source-id",
                sourceId,
                "--auto-pair",
                "--include-interrupted",
            ]
        )
    }

    public func reconcilePending(workspacePath: String?) async throws -> ReconcileSummaryPayload {
        var args = ["handoff"]
        if let workspacePath, !workspacePath.isEmpty {
            args.append(contentsOf: ["--workspace", workspacePath])
        }
        args.append("--reconcile")
        return try await runAndDecode(ReconcileSummaryPayload.self, arguments: args)
    }

    private func currentDefaults() -> UserDefaults {
        PreferencesStore.resolveDefaults(suiteName: defaultsSuiteName, environment: environment)
    }

    private func runAndDecode<T: Decodable & Sendable>(
        _ type: T.Type,
        arguments: [String]
    ) async throws -> T {
        let executableURL: URL
        if let executableOverride {
            guard FileManager.default.isExecutableFile(atPath: executableOverride.path) else {
                throw CLIClientError.cliMissing(searchedLocations: [executableOverride.path])
            }
            executableURL = executableOverride
        } else {
            let resolved = Self.resolveExecutableURL(
                environment: environment,
                defaults: currentDefaults()
            )
            guard let url = resolved.url else {
                throw CLIClientError.cliMissing(searchedLocations: resolved.searched)
            }
            executableURL = url
        }

        let childEnv = Self.mergedEnvironment(from: environment)
        let timeout = timeoutSeconds

        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let output = try Self.runProcessSync(
                        executableURL: executableURL,
                        arguments: arguments,
                        environment: childEnv,
                        timeoutSeconds: timeout
                    )
                    if output.exitCode != 0 {
                        if let payload = Self.decodeCLIError(from: output.stderr)
                            ?? Self.decodeCLIError(from: output.stdout)
                        {
                            continuation.resume(
                                throwing: CLIClientError.structuredError(payload, exitCode: output.exitCode)
                            )
                            return
                        }
                        let stderrText = String(data: output.stderr, encoding: .utf8) ?? ""
                        continuation.resume(
                            throwing: CLIClientError.nonZeroExit(exitCode: output.exitCode, stderr: stderrText)
                        )
                        return
                    }
                    let decoded = try JSONDecoder().decode(T.self, from: output.stdout)
                    continuation.resume(returning: decoded)
                } catch let err as CLIClientError {
                    continuation.resume(throwing: err)
                } catch let err as DecodingError {
                    continuation.resume(throwing: CLIClientError.decodingFailed(String(describing: err)))
                } catch {
                    continuation.resume(throwing: CLIClientError.launchFailed(error.localizedDescription))
                }
            }
        }
    }

    public static func runProcessSync(
        executableURL: URL,
        arguments: [String],
        environment: [String: String],
        timeoutSeconds: TimeInterval
    ) throws -> (exitCode: Int32, stdout: Data, stderr: Data) {
        let process = Process()
        process.executableURL = executableURL
        process.arguments = arguments
        process.environment = environment
        process.standardInput = FileHandle.nullDevice

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in
            finished.signal()
        }

        let stdoutBox = LockedBox(Data())
        let stderrBox = LockedBox(Data())
        let ioGroup = DispatchGroup()

        do {
            try process.run()
        } catch {
            throw CLIClientError.launchFailed(error.localizedDescription)
        }

        // Drain stdout and stderr concurrently so large responses cannot fill pipe buffers and deadlock.
        ioGroup.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            stdoutBox.set(stdoutPipe.fileHandleForReading.readDataToEndOfFile())
            ioGroup.leave()
        }
        ioGroup.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            stderrBox.set(stderrPipe.fileHandleForReading.readDataToEndOfFile())
            ioGroup.leave()
        }

        if finished.wait(timeout: .now() + timeoutSeconds) == .timedOut {
            process.terminate()
            if finished.wait(timeout: .now() + 2.0) == .timedOut || process.isRunning {
                kill(process.processIdentifier, SIGKILL)
                _ = finished.wait(timeout: .now() + 1.0)
            }
            _ = ioGroup.wait(timeout: .now() + 1.0)
            throw CLIClientError.timedOut(command: arguments, seconds: timeoutSeconds)
        }

        ioGroup.wait()
        return (process.terminationStatus, stdoutBox.get(), stderrBox.get())
    }

    static func decodeCLIError(from data: Data) -> CLIErrorPayload? {
        guard !data.isEmpty else { return nil }
        let decoder = JSONDecoder()
        if let direct = try? decoder.decode(CLIErrorPayload.self, from: data) {
            return direct
        }
        guard let text = String(data: data, encoding: .utf8) else { return nil }
        for line in text.split(whereSeparator: \.isNewline).reversed() {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.hasPrefix("{"), let lineData = trimmed.data(using: .utf8),
               let parsed = try? decoder.decode(CLIErrorPayload.self, from: lineData)
            {
                return parsed
            }
        }
        return nil
    }
}
