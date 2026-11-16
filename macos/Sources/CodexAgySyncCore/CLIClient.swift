import Darwin
import Foundation

public enum CLIResolutionSource: String, Codable, Equatable, Sendable {
    case envOverride = "env_override"
    case customPath = "custom_path"
    case autoDiscovered = "auto_discovered"
    case missing = "missing"

    public var displayName: String {
        displayName(in: .zhHans)
    }

    public func displayName(in language: ResolvedLanguage) -> String {
        switch self {
        case .envOverride:
            return AppCopy.text(.cliSourceEnv, in: language)
        case .customPath:
            return AppCopy.text(.cliSourceCustom, in: language)
        case .autoDiscovered:
            return AppCopy.text(.cliSourceAuto, in: language)
        case .missing:
            return AppCopy.text(.cliSourceMissing, in: language)
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
        localizedMessage(in: .zhHans)
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
        process.executableURL = executableURL
        process.arguments = arguments
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
    }

    func start(
        continuation: CheckedContinuation<(exitCode: Int32, stdout: Data, stderr: Data), Error>
    ) {
        queue.async {
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

    private func handleTimeout() {
        guard !resumed else { return }
        resumed = true
        cleanup()
        let proc = process
        let pid = proc.processIdentifier
        proc.terminate()
        queue.asyncAfter(deadline: .now() + 2.0) {
            if pid > 0 && proc.isRunning {
                kill(pid, SIGKILL)
            }
        }
        let cont = continuation
        continuation = nil
        cont?.resume(throwing: CLIClientError.timedOut(command: arguments, seconds: timeoutSeconds))
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
        homeDirectory: URL? = nil,
        pythonBinDirectories: [String]? = nil
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
        let pyBins = pythonBinDirectories ?? userPythonBinDirectories(homeDirectory: home, fileManager: fileManager)
        for pyBin in pyBins {
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
        homeDirectory: URL? = nil,
        pythonBinDirectories: [String]? = nil
    ) -> [String] {
        candidateExecutableEntries(
            environment: environment,
            defaults: defaults,
            fileManager: fileManager,
            homeDirectory: homeDirectory,
            pythonBinDirectories: pythonBinDirectories
        ).map(\.path)
    }

    public static func resolveExecutableWithSource(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil,
        pythonBinDirectories: [String]? = nil
    ) -> (url: URL?, source: CLIResolutionSource, searched: [String]) {
        let entries = candidateExecutableEntries(
            environment: environment,
            defaults: defaults,
            fileManager: fileManager,
            homeDirectory: homeDirectory,
            pythonBinDirectories: pythonBinDirectories
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
        pythonBinDirectories: [String]? = nil
    ) -> (url: URL?, searched: [String]) {
        let resolved = resolveExecutableWithSource(
            environment: environment,
            defaults: defaults,
            fileManager: fileManager,
            homeDirectory: homeDirectory,
            pythonBinDirectories: pythonBinDirectories
        )
        return (resolved.url, resolved.searched)
    }

    public static func mergedEnvironment(
        from environment: [String: String],
        fileManager: FileManager = .default,
        homeDirectory: URL? = nil,
        pythonBinDirectories: [String]? = nil
    ) -> [String: String] {
        var env = environment
        let home = homeDirectory ?? fileManager.homeDirectoryForCurrentUser
        let homeLocalBin = home.appendingPathComponent(".local/bin").path
        let pyBins = pythonBinDirectories ?? userPythonBinDirectories(homeDirectory: home, fileManager: fileManager)
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
        let pyBins: [String]
        let executableURL: URL
        if let executableOverride {
            guard FileManager.default.isExecutableFile(atPath: executableOverride.path) else {
                throw CLIClientError.cliMissing(searchedLocations: [executableOverride.path])
            }
            executableURL = executableOverride
            pyBins = Self.userPythonBinDirectories()
        } else {
            pyBins = Self.userPythonBinDirectories()
            let resolved = Self.resolveExecutableURL(
                environment: environment,
                defaults: currentDefaults(),
                pythonBinDirectories: pyBins
            )
            guard let url = resolved.url else {
                throw CLIClientError.cliMissing(searchedLocations: resolved.searched)
            }
            executableURL = url
        }

        let childEnv = Self.mergedEnvironment(from: environment, pythonBinDirectories: pyBins)
        let output = try await Self.runProcessAsync(
            executableURL: executableURL,
            arguments: arguments,
            environment: childEnv,
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
        return try await withCheckedThrowingContinuation { continuation in
            coordinator.start(continuation: continuation)
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
                let pid = process.processIdentifier
                if pid > 0 && process.isRunning {
                    kill(pid, SIGKILL)
                }
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
