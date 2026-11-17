import AppKit
import Foundation

public protocol RelaunchExecuting: Sendable {
    @MainActor func relaunch(bundleURL: URL) throws
}

public struct DefaultRelaunchExecutor: RelaunchExecuting, Sendable {
    public init() {}

    @MainActor
    public func relaunch(bundleURL: URL) throws {
        try SubprocessCLIClient.launchDetachedProcess(
            executableURL: URL(fileURLWithPath: "/usr/bin/open"),
            arguments: ["-n", bundleURL.path]
        )
        NSApp.terminate(nil)
    }
}

public struct CommandExecutionResult: Equatable, Sendable {
    public let exitCode: Int32
    public let stdout: Data
    public let stderr: Data

    public init(exitCode: Int32, stdout: Data, stderr: Data) {
        self.exitCode = exitCode
        self.stdout = stdout
        self.stderr = stderr
    }

    public var stdoutString: String {
        String(data: stdout, encoding: .utf8) ?? ""
    }

    public var stderrString: String {
        String(data: stderr, encoding: .utf8) ?? ""
    }
}

public protocol SystemCommandRunning: Sendable {
    func run(
        executableURL: URL,
        arguments: [String],
        environment: [String: String],
        timeoutSeconds: TimeInterval
    ) async throws -> CommandExecutionResult
}

public struct DefaultSystemCommandRunner: SystemCommandRunning, Sendable {
    public init() {}

    public func run(
        executableURL: URL,
        arguments: [String],
        environment: [String: String],
        timeoutSeconds: TimeInterval
    ) async throws -> CommandExecutionResult {
        let out = try await SubprocessCLIClient.runProcessAsync(
            executableURL: executableURL,
            arguments: arguments,
            environment: environment,
            timeoutSeconds: timeoutSeconds
        )
        return CommandExecutionResult(
            exitCode: out.exitCode,
            stdout: out.stdout,
            stderr: out.stderr
        )
    }
}

public protocol ReleaseHTTPFetching: Sendable {
    func fetchData(
        from url: URL,
        headers: [String: String],
        timeoutSeconds: TimeInterval
    ) async throws -> (data: Data, statusCode: Int)

    func downloadFile(
        from url: URL,
        headers: [String: String],
        timeoutSeconds: TimeInterval,
        to destinationURL: URL
    ) async throws -> Int
}

public extension ReleaseHTTPFetching {
    func downloadFile(
        from url: URL,
        headers: [String: String],
        timeoutSeconds: TimeInterval,
        to destinationURL: URL
    ) async throws -> Int {
        let (data, statusCode) = try await fetchData(
            from: url,
            headers: headers,
            timeoutSeconds: timeoutSeconds
        )
        if (200..<300).contains(statusCode) {
            try data.write(to: destinationURL, options: .atomic)
        }
        return statusCode
    }
}

public struct URLSessionReleaseHTTPFetcher: ReleaseHTTPFetching, Sendable {
    private static let sharedSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 180
        return URLSession(configuration: config)
    }()

    public init() {}

    public func fetchData(
        from url: URL,
        headers: [String: String],
        timeoutSeconds: TimeInterval
    ) async throws -> (data: Data, statusCode: Int) {
        var request = URLRequest(url: url, timeoutInterval: timeoutSeconds)
        for (key, value) in headers {
            request.setValue(value, forHTTPHeaderField: key)
        }
        let (data, response) = try await Self.sharedSession.data(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 200
        return (data, code)
    }

    public func downloadFile(
        from url: URL,
        headers: [String: String],
        timeoutSeconds: TimeInterval,
        to destinationURL: URL
    ) async throws -> Int {
        var request = URLRequest(url: url, timeoutInterval: timeoutSeconds)
        for (key, value) in headers {
            request.setValue(value, forHTTPHeaderField: key)
        }
        let (tempURL, response) = try await Self.sharedSession.download(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 200
        guard (200..<300).contains(code) else {
            try? FileManager.default.removeItem(at: tempURL)
            return code
        }
        let fm = FileManager.default
        if fm.fileExists(atPath: destinationURL.path) {
            _ = try fm.replaceItemAt(destinationURL, withItemAt: tempURL)
        } else {
            try fm.moveItem(at: tempURL, to: destinationURL)
        }
        return code
    }
}
