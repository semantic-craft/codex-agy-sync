import Darwin
import Foundation

public enum CLIResolutionSource: String, Codable, Equatable, Sendable {
    case envOverride = "env_override"
    case customPath = "custom_path"
    case bundled = "bundled"
    case autoDiscovered = "auto_discovered"
    case missing = "missing"

    public func displayName(in language: ResolvedLanguage) -> String {
        switch self {
        case .envOverride:
            return AppCopy.text(.cliSourceEnv, in: language)
        case .customPath:
            return AppCopy.text(.cliSourceCustom, in: language)
        case .bundled:
            return AppCopy.text(.cliSourceBundled, in: language)
        case .autoDiscovered:
            return AppCopy.text(.cliSourceAuto, in: language)
        case .missing:
            return AppCopy.text(.cliSourceMissing, in: language)
        }
    }
}

public protocol CLIRunnerProtocol: Sendable {
    func fetchFeed(limit: Int) async throws -> MenubarFeedSnapshot
    func executeSync(workspacePath: String, sourceId: String) async throws -> SyncReceiptPayload
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
        localizedMessage(in: AppLanguage.system.resolve())
    }

    public func localizedMessage(in language: ResolvedLanguage) -> String {
        switch self {
        case .cliMissing:
            return AppCopy.text(.cliMissingError, in: language)
        case .timedOut(_, let seconds):
            return AppCopy.cliTimedOut(seconds: seconds, in: language)
        case .structuredError(let payload, _):
            return AppCopy.cliStructuredError(message: payload.message, cause: payload.cause, in: language)
        case .nonZeroExit(let code, let stderr):
            return AppCopy.cliNonZeroExit(code: code, stderr: stderr, in: language)
        case .decodingFailed(let detail):
            return AppCopy.cliDecodingFailed(detail, in: language)
        case .launchFailed(let detail):
            return AppCopy.cliLaunchFailed(detail, in: language)
        }
    }
}

private actor CLIEnvironmentScanner {
    static let shared = CLIEnvironmentScanner()

    func resolveExecutionContext(
        executableOverride: URL?,
        environment: [String: String],
        customCLIPath: String?,
        bundleURL: URL
    ) throws -> (executableURL: URL, childEnv: [String: String]) {
        let executableURL: URL
        if let executableOverride {
            guard FileManager.default.isExecutableFile(atPath: executableOverride.path) else {
                throw CLIClientError.cliMissing(searchedLocations: [executableOverride.path])
            }
            executableURL = executableOverride
        } else {
            let resolved = SubprocessCLIClient.resolveExecutableURL(
                environment: environment,
                customCLIPath: customCLIPath,
                bundleURL: bundleURL
            )
            guard let url = resolved.url else {
                throw CLIClientError.cliMissing(searchedLocations: resolved.searched)
            }
            executableURL = url
        }
        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)
        return (executableURL, childEnv)
    }
}

private final class AsyncProcessCoordinator: @unchecked Sendable {
    private let queue = DispatchQueue(label: "dev.semanticcraft.codex-agy-sync.process")
    private let process = Process()
    private let stdoutPipe = Pipe()
    private let stderrPipe = Pipe()
    private let arguments: [String]
    private let timeoutSeconds: TimeInterval
    private let drainGraceSeconds: TimeInterval

    private var continuation: CheckedContinuation<(exitCode: Int32, stdout: Data, stderr: Data), Error>?
    private var timeoutTimer: DispatchSourceTimer?
    private var drainTimer: DispatchSourceTimer?
    private var stdoutData = Data()
    private var stderrData = Data()
    private var stdoutDone = false
    private var stderrDone = false
    private var terminated = false
    private var exitCode: Int32 = 0
    private var resumed = false
    private var cancelled = false

    init(
        executableURL: URL,
        arguments: [String],
        environment: [String: String],
        timeoutSeconds: TimeInterval,
        drainGraceSeconds: TimeInterval = 1.0
    ) {
        self.arguments = arguments
        self.timeoutSeconds = timeoutSeconds
        self.drainGraceSeconds = drainGraceSeconds
        SubprocessCLIClient.configureProcess(
            process,
            executableURL: executableURL,
            arguments: arguments,
            environment: environment,
            standardOutput: stdoutPipe,
            standardError: stderrPipe
        )
    }

    func start(
        continuation: CheckedContinuation<(exitCode: Int32, stdout: Data, stderr: Data), Error>
    ) {
        queue.async {
            if self.cancelled {
                guard !self.resumed else { return }
                self.resumed = true
                self.cleanup()
                continuation.resume(throwing: CancellationError())
                return
            }
            self.continuation = continuation
            let outHandle = self.stdoutPipe.fileHandleForReading
            let errHandle = self.stderrPipe.fileHandleForReading

            outHandle.readabilityHandler = { handle in
                let chunk = handle.availableData
                if chunk.isEmpty {
                    handle.readabilityHandler = nil
                    self.queue.async {
                        self.stdoutDone = true
                        self.tryComplete()
                    }
                } else {
                    self.queue.async {
                        if !self.resumed {
                            self.stdoutData.append(chunk)
                        }
                    }
                }
            }

            errHandle.readabilityHandler = { handle in
                let chunk = handle.availableData
                if chunk.isEmpty {
                    handle.readabilityHandler = nil
                    self.queue.async {
                        self.stderrDone = true
                        self.tryComplete()
                    }
                } else {
                    self.queue.async {
                        if !self.resumed {
                            self.stderrData.append(chunk)
                        }
                    }
                }
            }

            self.process.terminationHandler = { proc in
                let status = proc.terminationStatus
                self.queue.async {
                    self.exitCode = status
                    self.terminated = true
                    self.tryComplete()
                    if !self.resumed && (!self.stdoutDone || !self.stderrDone) && self.drainTimer == nil {
                        let drain = DispatchSource.makeTimerSource(queue: self.queue)
                        let drainNs = max(1, Int((self.drainGraceSeconds * 1_000_000_000).rounded()))
                        drain.schedule(deadline: .now() + .nanoseconds(drainNs))
                        drain.setEventHandler {
                            guard !self.resumed else { return }
                            self.stdoutPipe.fileHandleForReading.readabilityHandler = nil
                            self.stderrPipe.fileHandleForReading.readabilityHandler = nil
                            self.stdoutDone = true
                            self.stderrDone = true
                            self.tryComplete()
                        }
                        self.drainTimer = drain
                        drain.resume()
                    }
                }
            }

            let timer = DispatchSource.makeTimerSource(queue: self.queue)
            let timeoutNs = max(1, Int((self.timeoutSeconds * 1_000_000_000).rounded()))
            timer.schedule(deadline: .now() + .nanoseconds(timeoutNs))
            timer.setEventHandler {
                self.handleTimeout()
            }
            self.timeoutTimer = timer
            timer.resume()

            do {
                try self.process.run()
            } catch {
                self.failLaunch(error)
            }
        }
    }

    private func cleanup() {
        timeoutTimer?.cancel()
        timeoutTimer = nil
        drainTimer?.cancel()
        drainTimer = nil
        stdoutPipe.fileHandleForReading.readabilityHandler = nil
        stderrPipe.fileHandleForReading.readabilityHandler = nil
        process.terminationHandler = nil
    }

    private func tryComplete() {
        guard !resumed, terminated, stdoutDone, stderrDone else { return }
        resumed = true
        cleanup()
        let result = (exitCode: exitCode, stdout: stdoutData, stderr: stderrData)
        let cont = continuation
        continuation = nil
        cont?.resume(returning: result)
    }

    private func failLaunch(_ error: Error) {
        guard !resumed else { return }
        resumed = true
        cleanup()
        let cont = continuation
        continuation = nil
        cont?.resume(throwing: CLIClientError.launchFailed(error.localizedDescription))
    }

    func cancel() {
        queue.async {
            self.handleCancel()
        }
    }

    private func handleCancel() {
        cancelled = true
        guard let cont = continuation, !resumed else { return }
        resumed = true
        continuation = nil
        cleanup()
        terminateProcessWithEscalation()
        cont.resume(throwing: CancellationError())
    }

    private func handleTimeout() {
        guard !resumed else { return }
        resumed = true
        cleanup()
        terminateProcessWithEscalation()
        let cont = continuation
        continuation = nil
        cont?.resume(throwing: CLIClientError.timedOut(command: arguments, seconds: timeoutSeconds))
    }

    private func terminateProcessWithEscalation() {
        let proc = process
        let pid = proc.processIdentifier
        if pid > 0 && proc.isRunning {
            proc.terminate()
            queue.asyncAfter(deadline: .now() + 2.0) {
                if pid > 0 && proc.isRunning {
                    kill(pid, SIGKILL)
                }
            }
        }
    }
}

public struct SubprocessCLIClient: CLIRunnerProtocol, Sendable {
    public static let cliExecutableDefaultsKey = "cliExecutablePath"

    private let defaultsSuiteName: String?
    private let environment: [String: String]
    private let timeoutSeconds: TimeInterval
    private let executableOverride: URL?
    private let bundleURL: URL

    public init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaultsSuiteName: String? = nil,
        timeoutSeconds: TimeInterval = 15.0,
        executableOverride: URL? = nil,
        bundleURL: URL = Bundle.main.bundleURL
    ) {
        self.environment = environment
        self.defaultsSuiteName = defaultsSuiteName
        self.timeoutSeconds = timeoutSeconds
        self.executableOverride = executableOverride
        self.bundleURL = bundleURL
    }

    public static func bundledExecutablePath(bundleURL: URL = Bundle.main.bundleURL) -> String? {
        guard bundleURL.pathExtension.lowercased() == "app" else {
            return nil
        }
        return bundleURL
            .appendingPathComponent("Contents", isDirectory: true)
            .appendingPathComponent("Helpers", isDirectory: true)
            .appendingPathComponent("codex-agy-sync", isDirectory: false)
            .path
    }

    public static func candidateExecutableEntries(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil,
        bundleURL: URL = Bundle.main.bundleURL
    ) -> [(path: String, source: CLIResolutionSource)] {
        candidateExecutableEntries(
            environment: environment,
            customCLIPath: defaults.string(forKey: cliExecutableDefaultsKey),
            fileManager: fileManager,
            homeDirectory: homeDirectory,
            bundleURL: bundleURL
        )
    }

    public static func candidateExecutableEntries(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        customCLIPath: String?,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil,
        bundleURL: URL = Bundle.main.bundleURL
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

        appendCandidate(customCLIPath, source: .customPath)
        appendCandidate(bundledExecutablePath(bundleURL: bundleURL), source: .bundled)

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
        appendCandidate("/opt/homebrew/bin/codex-agy-sync", source: .autoDiscovered)
        appendCandidate("/usr/local/bin/codex-agy-sync", source: .autoDiscovered)

        return entries
    }

    public static func candidateExecutablePaths(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil,
        bundleURL: URL = Bundle.main.bundleURL
    ) -> [String] {
        candidateExecutableEntries(
            environment: environment,
            defaults: defaults,
            fileManager: fileManager,
            homeDirectory: homeDirectory,
            bundleURL: bundleURL
        ).map(\.path)
    }

    public static func resolveExecutableWithSource(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil,
        bundleURL: URL = Bundle.main.bundleURL
    ) -> (url: URL?, source: CLIResolutionSource, searched: [String]) {
        resolveExecutableWithSource(
            environment: environment,
            customCLIPath: defaults.string(forKey: cliExecutableDefaultsKey),
            fileManager: fileManager,
            homeDirectory: homeDirectory,
            bundleURL: bundleURL
        )
    }

    public static func resolveExecutableWithSource(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        customCLIPath: String?,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil,
        bundleURL: URL = Bundle.main.bundleURL
    ) -> (url: URL?, source: CLIResolutionSource, searched: [String]) {
        let entries = candidateExecutableEntries(
            environment: environment,
            customCLIPath: customCLIPath,
            fileManager: fileManager,
            homeDirectory: homeDirectory,
            bundleURL: bundleURL
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
        homeDirectory: URL? = nil,
        bundleURL: URL = Bundle.main.bundleURL
    ) -> (url: URL?, searched: [String]) {
        let resolved = resolveExecutableWithSource(
            environment: environment,
            defaults: defaults,
            fileManager: fileManager,
            homeDirectory: homeDirectory,
            bundleURL: bundleURL
        )
        return (resolved.url, resolved.searched)
    }

    public static func resolveExecutableURL(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        customCLIPath: String?,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil,
        bundleURL: URL = Bundle.main.bundleURL
    ) -> (url: URL?, searched: [String]) {
        let resolved = resolveExecutableWithSource(
            environment: environment,
            customCLIPath: customCLIPath,
            fileManager: fileManager,
            homeDirectory: homeDirectory,
            bundleURL: bundleURL
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
        let extraPathEntries = [homeLocalBin, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
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

    public func executeSync(
        workspacePath: String,
        sourceId: String
    ) async throws -> SyncReceiptPayload {
        try await runAndDecode(
            SyncReceiptPayload.self,
            arguments: [
                "sync",
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
        var args = ["sync"]
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
        let customCLIPath = currentDefaults().string(forKey: Self.cliExecutableDefaultsKey)
        let context = try await CLIEnvironmentScanner.shared.resolveExecutionContext(
            executableOverride: executableOverride,
            environment: environment,
            customCLIPath: customCLIPath,
            bundleURL: bundleURL
        )
        let output = try await Self.runProcessAsync(
            executableURL: context.executableURL,
            arguments: arguments,
            environment: context.childEnv,
            timeoutSeconds: timeoutSeconds
        )
        if output.exitCode != 0 {
            if let payload = Self.decodeCLIError(from: output.stderr)
                ?? Self.decodeCLIError(from: output.stdout)
            {
                throw CLIClientError.structuredError(payload, exitCode: output.exitCode)
            }
            let stderrText = String(data: output.stderr, encoding: .utf8) ?? ""
            throw CLIClientError.nonZeroExit(exitCode: output.exitCode, stderr: stderrText)
        }
        do {
            return try JSONDecoder().decode(T.self, from: output.stdout)
        } catch let err as DecodingError {
            throw CLIClientError.decodingFailed(String(describing: err))
        } catch {
            throw CLIClientError.launchFailed(error.localizedDescription)
        }
    }

    static func configureProcess(
        _ process: Process,
        executableURL: URL,
        arguments: [String],
        environment: [String: String]? = nil,
        standardInput: Any? = FileHandle.nullDevice,
        standardOutput: Any? = FileHandle.nullDevice,
        standardError: Any? = FileHandle.nullDevice
    ) {
        process.executableURL = executableURL
        process.arguments = arguments
        if let environment {
            process.environment = environment
        }
        process.standardInput = standardInput
        process.standardOutput = standardOutput
        process.standardError = standardError
    }

    public static func launchDetachedProcess(
        executableURL: URL,
        arguments: [String],
        environment: [String: String]? = nil
    ) throws {
        let process = Process()
        configureProcess(
            process,
            executableURL: executableURL,
            arguments: arguments,
            environment: environment
        )
        do {
            try process.run()
        } catch {
            throw CLIClientError.launchFailed(error.localizedDescription)
        }
    }

    public static func runProcessAsync(
        executableURL: URL,
        arguments: [String],
        environment: [String: String],
        timeoutSeconds: TimeInterval,
        drainGraceSeconds: TimeInterval = 1.0
    ) async throws -> (exitCode: Int32, stdout: Data, stderr: Data) {
        let coordinator = AsyncProcessCoordinator(
            executableURL: executableURL,
            arguments: arguments,
            environment: environment,
            timeoutSeconds: timeoutSeconds,
            drainGraceSeconds: drainGraceSeconds
        )
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                coordinator.start(continuation: continuation)
            }
        } onCancel: {
            coordinator.cancel()
        }
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
