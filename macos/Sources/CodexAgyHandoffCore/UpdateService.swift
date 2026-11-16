import AppKit
import CryptoKit
import Foundation

public enum VersionAlignmentState: Equatable, Sendable {
    case aligned(version: String)
    case mismatched(appVersion: String, cliVersion: String)
    case cliUnavailable(appVersion: String, reason: String)

    public var badgeTitle: String {
        switch self {
        case .aligned:
            return "双端已同频"
        case .mismatched:
            return "版本未对齐"
        case .cliUnavailable:
            return "CLI 未就绪"
        }
    }

    public var cliVersionLabel: String {
        switch self {
        case .aligned(let version):
            return "v\(version)"
        case .mismatched(_, let cliVersion):
            return "v\(cliVersion)"
        case .cliUnavailable:
            return "未就绪"
        }
    }
}

public enum UpdateAvailability: Equatable, Sendable {
    case idle
    case checking
    case upToDate(currentVersion: String)
    case releaseAvailable(latestTag: String, notesSummary: String, hasAppAsset: Bool, hasWheelAsset: Bool)
    case localSyncReady(repoPath: String, repoVersion: String)
    case updating(stepDescription: String)
    case failed(String)

    public var isBusy: Bool {
        switch self {
        case .checking, .updating:
            return true
        default:
            return false
        }
    }
}

public struct SemanticVersion: Comparable, Equatable, Sendable, CustomStringConvertible {
    public let major: Int
    public let minor: Int
    public let patch: Int
    public let prerelease: String?

    public var description: String {
        if let prerelease, !prerelease.isEmpty {
            return "\(major).\(minor).\(patch)-\(prerelease)"
        }
        return "\(major).\(minor).\(patch)"
    }

    public init(major: Int, minor: Int, patch: Int, prerelease: String? = nil) {
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = prerelease
    }

    private static let versionRegex = try! NSRegularExpression(
        pattern: #"(?:^|[^0-9])v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z.-]+))?"#
    )

    public static func parse(_ raw: String) -> SemanticVersion? {
        let nsRange = NSRange(raw.startIndex..<raw.endIndex, in: raw)
        guard let match = versionRegex.firstMatch(in: raw, range: nsRange),
              let r1 = Range(match.range(at: 1), in: raw),
              let r2 = Range(match.range(at: 2), in: raw),
              let r3 = Range(match.range(at: 3), in: raw),
              let major = Int(raw[r1]),
              let minor = Int(raw[r2]),
              let patch = Int(raw[r3])
        else {
            return nil
        }
        var pre: String?
        if match.range(at: 4).location != NSNotFound,
           let r4 = Range(match.range(at: 4), in: raw)
        {
            pre = String(raw[r4])
        }
        return SemanticVersion(major: major, minor: minor, patch: patch, prerelease: pre)
    }

    public static func < (lhs: SemanticVersion, rhs: SemanticVersion) -> Bool {
        if lhs.major != rhs.major { return lhs.major < rhs.major }
        if lhs.minor != rhs.minor { return lhs.minor < rhs.minor }
        if lhs.patch != rhs.patch { return lhs.patch < rhs.patch }
        switch (lhs.prerelease, rhs.prerelease) {
        case (nil, nil):
            return false
        case (.some, nil):
            return true
        case (nil, .some):
            return false
        case (.some(let lp), .some(let rp)):
            return lp < rp
        }
    }
}

public struct ReleaseAssetInfo: Codable, Equatable, Sendable {
    public let name: String
    public let browserDownloadURL: URL
    public let size: Int

    public init(name: String, browserDownloadURL: URL, size: Int = 0) {
        self.name = name
        self.browserDownloadURL = browserDownloadURL
        self.size = size
    }

    public enum CodingKeys: String, CodingKey {
        case name
        case browserDownloadURL = "browser_download_url"
        case size
    }
}

public struct GitHubReleaseInfo: Equatable, Sendable {
    public let tagName: String
    public let version: String
    public let htmlURL: URL?
    public let notesSummary: String
    public let wheelAsset: ReleaseAssetInfo?
    public let appZipAsset: ReleaseAssetInfo?
    public let checksumsAsset: ReleaseAssetInfo?

    public init(
        tagName: String,
        version: String,
        htmlURL: URL?,
        notesSummary: String,
        wheelAsset: ReleaseAssetInfo?,
        appZipAsset: ReleaseAssetInfo?,
        checksumsAsset: ReleaseAssetInfo?
    ) {
        self.tagName = tagName
        self.version = version
        self.htmlURL = htmlURL
        self.notesSummary = notesSummary
        self.wheelAsset = wheelAsset
        self.appZipAsset = appZipAsset
        self.checksumsAsset = checksumsAsset
    }

    private struct RawReleasePayload: Decodable {
        let tag_name: String
        let html_url: String?
        let body: String?
        let assets: [ReleaseAssetInfo]?
    }

    public static func decode(from data: Data) throws -> GitHubReleaseInfo {
        let raw = try JSONDecoder().decode(RawReleasePayload.self, from: data)
        let normalizedVersion = SemanticVersion.parse(raw.tag_name)?.description
            ?? raw.tag_name.trimmingCharacters(in: CharacterSet(charactersIn: "vV \t\r\n"))
        let assets = raw.assets ?? []
        let wheel = assets.first {
            !isSignatureAssetName($0.name)
                && $0.name.hasPrefix("codex_agy_sync-")
                && $0.name.hasSuffix(".whl")
        }
        let appZip = assets.first { $0.name == "CodexAgyHandoff-macos.zip" }
            ?? assets.first { !isSignatureAssetName($0.name) && $0.name.lowercased().hasSuffix("-macos.zip") }
        let checksums = assets.first { $0.name == "SHA256SUMS" }
            ?? assets.first { !isSignatureAssetName($0.name) && $0.name.uppercased().contains("SHA256SUM") }
            ?? assets.first { !isSignatureAssetName($0.name) && $0.name.lowercased().hasSuffix(".sha256") }

        let summary = Self.summarizeNotes(raw.body ?? "")
        let url = raw.html_url.flatMap(URL.init(string:))
        return GitHubReleaseInfo(
            tagName: raw.tag_name,
            version: normalizedVersion,
            htmlURL: url,
            notesSummary: summary,
            wheelAsset: wheel,
            appZipAsset: appZip,
            checksumsAsset: checksums
        )
    }

    private static func isSignatureAssetName(_ name: String) -> Bool {
        let lower = name.lowercased()
        return lower.hasSuffix(".asc")
            || lower.hasSuffix(".sig")
            || lower.hasSuffix(".minisig")
            || lower.hasSuffix(".pem")
    }

    private static func summarizeNotes(_ rawBody: String) -> String {
        for line in rawBody.split(whereSeparator: \.isNewline) {
            var cleaned = line.trimmingCharacters(in: .whitespacesAndNewlines)
            while cleaned.hasPrefix("#") || cleaned.hasPrefix("-") || cleaned.hasPrefix("*") {
                cleaned.removeFirst()
                cleaned = cleaned.trimmingCharacters(in: .whitespaces)
            }
            if !cleaned.isEmpty {
                if cleaned.count > 120 {
                    return String(cleaned.prefix(117)) + "..."
                }
                return cleaned
            }
        }
        return "包含最新双端接力与菜单栏改进"
    }
}

public enum UpdateServiceError: LocalizedError, Equatable, Sendable {
    case forbiddenTemporaryPath(String)
    case invalidAssetName(String)
    case invalidReleaseURL(String)
    case httpError(statusCode: Int)
    case missingChecksumsAsset
    case missingUpdateAssets
    case missingChecksum(assetName: String)
    case checksumMismatch(assetName: String, expected: String, actual: String)
    case invalidLocalRepo(String)
    case targetBundleNotWritable(String)
    case pythonInterpreterMissing
    case commandFailed(step: String, detail: String)
    case codesignVerificationFailed(String)

    public var errorDescription: String? {
        switch self {
        case .forbiddenTemporaryPath(let path):
            return "拒绝使用 /tmp 临时目录：\(path)"
        case .invalidAssetName(let name):
            return "拒绝不安全的更新资产文件名：\(name)"
        case .invalidReleaseURL(let url):
            return "无效的更新检查地址：\(url)"
        case .httpError(let code):
            return "请求远端发布信息失败（HTTP \(code)）。"
        case .missingChecksumsAsset:
            return "远端 Release 缺少 SHA256SUMS 校验文件，已中止更新。"
        case .missingUpdateAssets:
            return "远端 Release 未包含可用的安装包资产（.whl 或 CodexAgyHandoff-macos.zip）。"
        case .missingChecksum(let name):
            return "SHA256SUMS 中缺少资产校验项：\(name)"
        case .checksumMismatch(let name, let expected, let actual):
            return "\(name) SHA-256 校验不匹配（预期 \(expected.prefix(12))…，实际 \(actual.prefix(12))…）。"
        case .invalidLocalRepo(let path):
            return "无效的本地源码仓目录（需包含 pyproject.toml 与 scripts/build_menubar_app.sh）：\(path)"
        case .targetBundleNotWritable(let detail):
            return "无法写入应用安装目录（\(detail)），请将应用移至可写目录后重试。"
        case .pythonInterpreterMissing:
            return "未找到可用的 Python 3 解释器以更新 CLI。"
        case .commandFailed(let step, let detail):
            return detail.isEmpty ? "\(step) 执行失败。" : "\(step) 失败：\(detail)"
        case .codesignVerificationFailed(let detail):
            return "新构建的应用签名校验未通过：\(detail)"
        }
    }
}

public enum SHA256Verifier {
    public static func sha256Hex(of data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    public static func parseChecksums(_ text: String) -> [String: String] {
        var result: [String: String] = [:]
        for rawLine in text.split(whereSeparator: \.isNewline) {
            let trimmed = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty || trimmed.hasPrefix("#") {
                continue
            }
            let parts = trimmed.split(maxSplits: 1, omittingEmptySubsequences: true) {
                $0 == " " || $0 == "\t"
            }
            guard parts.count == 2 else { continue }
            let digest = String(parts[0]).lowercased()
            guard digest.count == 64, digest.allSatisfy({ $0.isHexDigit }) else { continue }
            var namePart = String(parts[1]).trimmingCharacters(in: .whitespaces)
            if namePart.hasPrefix("*") {
                namePart.removeFirst()
            }
            let fileName = URL(fileURLWithPath: namePart).lastPathComponent
            if !fileName.isEmpty {
                result[fileName] = digest
            }
        }
        return result
    }

    public static func verify(data: Data, assetName: String, checksums: [String: String]) throws {
        guard let expected = checksums[assetName]?.lowercased() else {
            throw UpdateServiceError.missingChecksum(assetName: assetName)
        }
        let actual = sha256Hex(of: data)
        guard actual == expected else {
            throw UpdateServiceError.checksumMismatch(
                assetName: assetName,
                expected: expected,
                actual: actual
            )
        }
    }
}

public enum UpdatePathGuard {
    public static func validateNonTemporaryURL(_ url: URL) throws -> URL {
        let raw = (url.path as NSString).standardizingPath
        if isTmpPath(raw) {
            throw UpdateServiceError.forbiddenTemporaryPath(raw)
        }
        let resolved = resolveRealAncestorPath(raw)
        if isTmpPath(resolved) {
            throw UpdateServiceError.forbiddenTemporaryPath(resolved)
        }
        return URL(fileURLWithPath: resolved, isDirectory: url.hasDirectoryPath)
    }

    public static func defaultStagingRootURL(fileManager: FileManager = .default) throws -> URL {
        let appSupport = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
        let root = appSupport
            .appendingPathComponent("CodexAgyHandoff", isDirectory: true)
            .appendingPathComponent("updates", isDirectory: true)
        return try validateNonTemporaryURL(root)
    }

    public static func stagedAssetFileURL(for rawAssetName: String, in stagingDir: URL) throws -> URL {
        let trimmed = rawAssetName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !trimmed.contains("\0") else {
            throw UpdateServiceError.invalidAssetName(rawAssetName)
        }
        let normalized = trimmed.replacingOccurrences(of: "\\", with: "/")
        guard !normalized.hasSuffix("/"),
              let rawTail = normalized.split(separator: "/", omittingEmptySubsequences: true).last.map(String.init),
              !rawTail.isEmpty,
              rawTail != ".",
              rawTail != ".."
        else {
            throw UpdateServiceError.invalidAssetName(rawAssetName)
        }
        let safeName = URL(fileURLWithPath: "/" + rawTail).lastPathComponent
        guard !safeName.isEmpty,
              safeName != ".",
              safeName != "..",
              safeName != "/",
              !safeName.contains("/")
        else {
            throw UpdateServiceError.invalidAssetName(rawAssetName)
        }
        let canonicalStaging = try validateNonTemporaryURL(stagingDir).standardizedFileURL
        let dest = canonicalStaging.appendingPathComponent(safeName, isDirectory: false).standardizedFileURL
        guard dest.deletingLastPathComponent() == canonicalStaging else {
            throw UpdateServiceError.invalidAssetName(rawAssetName)
        }
        return dest
    }

    public static func pruneStaleStagingDirectories(
        in rootURL: URL,
        maxAgeSeconds: TimeInterval = 86_400,
        now: Date = Date(),
        fileManager: FileManager = .default
    ) {
        guard let validatedRoot = try? validateNonTemporaryURL(rootURL) else { return }
        let canonicalRoot = validatedRoot.standardizedFileURL
        guard let children = try? fileManager.contentsOfDirectory(
            at: canonicalRoot,
            includingPropertiesForKeys: [.isDirectoryKey, .contentModificationDateKey, .creationDateKey],
            options: [.skipsHiddenFiles]
        ) else {
            return
        }
        for child in children {
            let canonicalChild = child.standardizedFileURL
            guard canonicalChild.deletingLastPathComponent() == canonicalRoot else { continue }
            guard let values = try? child.resourceValues(
                forKeys: [.isDirectoryKey, .contentModificationDateKey, .creationDateKey]
            ), values.isDirectory == true else {
                continue
            }
            let stamp = values.contentModificationDate ?? values.creationDate
            if let stamp, now.timeIntervalSince(stamp) > maxAgeSeconds {
                try? fileManager.removeItem(at: canonicalChild)
            }
        }
    }

    private static func isTmpPath(_ path: String) -> Bool {
        path == "/tmp"
            || path.hasPrefix("/tmp/")
            || path == "/private/tmp"
            || path.hasPrefix("/private/tmp/")
    }

    private static func resolveRealAncestorPath(_ absolutePath: String) -> String {
        let fm = FileManager.default
        var probe = URL(fileURLWithPath: absolutePath)
        var trailing: [String] = []
        while !fm.fileExists(atPath: probe.path) && probe.path != "/" {
            trailing.append(probe.lastPathComponent)
            probe.deleteLastPathComponent()
        }
        var resolved = probe.resolvingSymlinksInPath()
        for component in trailing.reversed() {
            resolved.appendPathComponent(component)
        }
        return (resolved.path as NSString).standardizingPath
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

public final class DefaultSystemCommandRunner: SystemCommandRunning, @unchecked Sendable {
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
}

public final class URLSessionReleaseHTTPFetcher: ReleaseHTTPFetching, @unchecked Sendable {
    public init() {}

    public func fetchData(
        from url: URL,
        headers: [String: String],
        timeoutSeconds: TimeInterval
    ) async throws -> (data: Data, statusCode: Int) {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeoutSeconds
        config.timeoutIntervalForResource = max(timeoutSeconds * 3, 30)
        let session = URLSession(configuration: config)
        defer { session.finishTasksAndInvalidate() }

        var request = URLRequest(url: url, timeoutInterval: timeoutSeconds)
        for (key, value) in headers {
            request.setValue(value, forHTTPHeaderField: key)
        }

        let (data, response) = try await session.data(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 200
        return (data, code)
    }
}

@MainActor
public final class UpdateViewModel: ObservableObject {
    public nonisolated static let defaultReleaseAPIURL = "https://api.github.com/repos/semantic-craft/codex-agy-sync/releases/latest"
    public nonisolated static let releaseAPIEnvKey = "MENUBAR_RELEASE_API_URL"
    public nonisolated static let skipSelfReplaceEnvKey = "MENUBAR_SKIP_SELF_REPLACE"

    @Published public private(set) var alignment: VersionAlignmentState
    @Published public private(set) var availability: UpdateAvailability = .idle
    @Published public private(set) var resolvedCLIPath: String?
    @Published public private(set) var cliSource: CLIResolutionSource = .missing
    @Published public private(set) var detectedRepoPath: String?
    @Published public private(set) var detectedRepoVersion: String?
    @Published public private(set) var latestRelease: GitHubReleaseInfo?
    @Published public private(set) var statusMessage: String?
    @Published public private(set) var canRelaunchApp: Bool = false
    @Published public private(set) var pendingRelaunchVersion: String?

    public let appVersion: String
    public let currentBundleIdentifier: String
    public let currentAppBundleURL: URL?
    public let preferencesStore: PreferencesStore

    private let environment: [String: String]
    private let commandRunner: any SystemCommandRunning
    private let httpFetcher: any ReleaseHTTPFetching
    private let stagingRootOverride: URL?
    private var hasCompletedInitialProbe = false

    public var effectiveAppVersion: String {
        pendingRelaunchVersion ?? (SemanticVersion.parse(appVersion)?.description ?? appVersion)
    }

    public var effectiveRepoPath: String? {
        if let custom = preferencesStore.preferences.localRepoPathForSync,
           Self.isValidSourceRepo(URL(fileURLWithPath: custom))
        {
            return custom
        }
        return detectedRepoPath
    }

    public var effectiveRepoVersion: String? {
        if let repoPath = effectiveRepoPath {
            return Self.parsePyprojectVersion(in: URL(fileURLWithPath: repoPath)) ?? detectedRepoVersion
        }
        return nil
    }

    public init(
        preferencesStore: PreferencesStore,
        appVersion: String? = nil,
        currentBundleIdentifier: String? = nil,
        currentAppBundleURL: URL? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        commandRunner: any SystemCommandRunning = DefaultSystemCommandRunner(),
        httpFetcher: any ReleaseHTTPFetching = URLSessionReleaseHTTPFetcher(),
        stagingRootOverride: URL? = nil
    ) {
        let resolvedAppVer = appVersion
            ?? (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String)
            ?? "0.4.1"
        self.appVersion = resolvedAppVer
        self.currentBundleIdentifier = currentBundleIdentifier
            ?? Bundle.main.bundleIdentifier
            ?? "dev.semanticcraft.codex-agy-handoff"
        if let currentAppBundleURL {
            self.currentAppBundleURL = currentAppBundleURL
        } else if Bundle.main.bundleURL.pathExtension == "app" {
            self.currentAppBundleURL = Bundle.main.bundleURL
        } else {
            self.currentAppBundleURL = nil
        }
        self.preferencesStore = preferencesStore
        self.environment = environment
        self.commandRunner = commandRunner
        self.httpFetcher = httpFetcher
        self.stagingRootOverride = stagingRootOverride
        self.alignment = .cliUnavailable(appVersion: resolvedAppVer, reason: "尚未探测")
    }

    public func probeIfNeeded() async {
        guard !hasCompletedInitialProbe else { return }
        hasCompletedInitialProbe = true
        await probeEnvironment()
    }

    public func probeEnvironment() async {
        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)
        let resolved = SubprocessCLIClient.resolveExecutableWithSource(
            environment: environment,
            defaults: preferencesStore.defaults
        )
        self.cliSource = resolved.source
        self.resolvedCLIPath = resolved.url?.path

        guard let cliURL = resolved.url else {
            self.alignment = .cliUnavailable(appVersion: effectiveAppVersion, reason: "未找到 codex-agy-sync 可执行文件")
            updateRepoDiscovery(fromCLI: nil)
            return
        }

        do {
            let result = try await commandRunner.run(
                executableURL: cliURL,
                arguments: ["--version"],
                environment: childEnv,
                timeoutSeconds: 8.0
            )
            let combined = (result.stdoutString + "\n" + result.stderrString)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if result.exitCode == 0, let parsed = SemanticVersion.parse(combined) {
                let cliVer = parsed.description
                let appTargetVer = effectiveAppVersion
                if cliVer == appTargetVer {
                    self.alignment = .aligned(version: appTargetVer)
                } else {
                    self.alignment = .mismatched(appVersion: appTargetVer, cliVersion: cliVer)
                }
            } else {
                let reason = combined.isEmpty ? "退出码 \(result.exitCode)" : combined
                self.alignment = .cliUnavailable(appVersion: effectiveAppVersion, reason: reason)
            }
        } catch {
            self.alignment = .cliUnavailable(appVersion: effectiveAppVersion, reason: error.localizedDescription)
        }

        await discoverEditableRepoIfNeeded(fromCLI: cliURL, childEnv: childEnv)
    }

    public func checkForUpdates() async {
        guard !availability.isBusy else { return }
        availability = .checking
        statusMessage = nil

        await probeEnvironment()

        let rawURL = environment[Self.releaseAPIEnvKey] ?? Self.defaultReleaseAPIURL
        guard let url = URL(string: rawURL) else {
            let err = UpdateServiceError.invalidReleaseURL(rawURL)
            availability = .failed(err.localizedDescription)
            statusMessage = err.localizedDescription
            return
        }

        do {
            let (data, statusCode) = try await httpFetcher.fetchData(
                from: url,
                headers: [
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "CodexAgyHandoff/\(appVersion)",
                ],
                timeoutSeconds: 10.0
            )
            guard (200..<300).contains(statusCode) else {
                throw UpdateServiceError.httpError(statusCode: statusCode)
            }
            let release = try GitHubReleaseInfo.decode(from: data)
            self.latestRelease = release

            let currentAppVer = effectiveAppVersion
            let remoteSemver = SemanticVersion.parse(release.version)
            let appSemver = SemanticVersion.parse(currentAppVer)
            let cliSemver: SemanticVersion? = {
                switch alignment {
                case .aligned(let ver):
                    return SemanticVersion.parse(ver)
                case .mismatched(_, let cliVer):
                    return SemanticVersion.parse(cliVer)
                case .cliUnavailable:
                    return nil
                }
            }()

            let remoteIsNewerThanApp = (remoteSemver != nil && appSemver != nil) ? (remoteSemver! > appSemver!) : (release.version != currentAppVer)
            let remoteIsNewerThanCLI = (remoteSemver != nil && cliSemver != nil) ? (remoteSemver! > cliSemver!) : false

            if remoteIsNewerThanApp || remoteIsNewerThanCLI {
                availability = .releaseAvailable(
                    latestTag: release.tagName,
                    notesSummary: release.notesSummary,
                    hasAppAsset: release.appZipAsset != nil,
                    hasWheelAsset: release.wheelAsset != nil
                )
                statusMessage = "发现新版本 \(release.tagName)：\(release.notesSummary)"
                return
            }

            switch alignment {
            case .cliUnavailable:
                if let repoPath = effectiveRepoPath, let repoVer = effectiveRepoVersion {
                    availability = .localSyncReady(repoPath: repoPath, repoVersion: repoVer)
                    statusMessage = "CLI 尚未就绪，可从本地源码仓（v\(repoVer)）安装并同步。"
                } else if release.wheelAsset != nil {
                    availability = .releaseAvailable(
                        latestTag: release.tagName,
                        notesSummary: "安装 CLI 并同步对齐至 \(release.tagName)",
                        hasAppAsset: release.appZipAsset != nil,
                        hasWheelAsset: true
                    )
                    statusMessage = "CLI 尚未就绪，可点击同步更新安装 \(release.tagName)。"
                } else {
                    let msg = "CLI 尚未就绪，且远端 Release（\(release.tagName)）未包含 CLI 安装包。"
                    availability = .failed(msg)
                    statusMessage = msg
                }
                return

            case .mismatched:
                if let repoPath = effectiveRepoPath, let repoVer = effectiveRepoVersion {
                    availability = .localSyncReady(repoPath: repoPath, repoVersion: repoVer)
                    statusMessage = "检测到双端版本不一致，可从本地源码仓（v\(repoVer)）或远端 Release 对齐。"
                } else if release.wheelAsset != nil || release.appZipAsset != nil {
                    availability = .releaseAvailable(
                        latestTag: release.tagName,
                        notesSummary: "重新同步对齐 App 与 CLI 至 \(release.tagName)",
                        hasAppAsset: release.appZipAsset != nil,
                        hasWheelAsset: release.wheelAsset != nil
                    )
                    statusMessage = "双端版本未对齐，可点击同步更新对齐至 \(release.tagName)。"
                } else {
                    let msg = "双端版本未对齐，且远端 Release（\(release.tagName)）未包含对齐所需安装包。"
                    availability = .failed(msg)
                    statusMessage = msg
                }
                return

            case .aligned:
                availability = .upToDate(currentVersion: currentAppVer)
                statusMessage = "当前 App 与远端最新 Release（\(release.tagName)）一致。"
            }
        } catch {
            if let repoPath = effectiveRepoPath, let repoVer = effectiveRepoVersion {
                availability = .localSyncReady(repoPath: repoPath, repoVersion: repoVer)
                statusMessage = "远端检查不可用（\(error.localizedDescription)）；可直接从本地源码仓 v\(repoVer) 同步构建。"
            } else {
                availability = .failed(error.localizedDescription)
                statusMessage = error.localizedDescription
            }
        }
    }

    public func syncUpdateNow() async {
        guard !availability.isBusy else { return }

        if latestRelease == nil && effectiveRepoPath == nil {
            await checkForUpdates()
            if case .failed = availability { return }
        }

        if let release = latestRelease,
           release.checksumsAsset != nil,
           release.wheelAsset != nil || release.appZipAsset != nil
        {
            let remoteSem = SemanticVersion.parse(release.version)
            let localRepoSem = effectiveRepoVersion.flatMap(SemanticVersion.parse)
            let preferRelease = effectiveRepoPath == nil
                || localRepoSem == nil
                || (remoteSem != nil && remoteSem! > localRepoSem!)
            if preferRelease {
                await performReleaseSync(release)
                return
            }
        }

        if let repoPath = effectiveRepoPath {
            await performLocalRepoSync(repoPath: repoPath)
            return
        }

        if let release = latestRelease {
            await performReleaseSync(release)
            return
        }

        let err = UpdateServiceError.missingUpdateAssets
        availability = .failed(err.localizedDescription)
        statusMessage = err.localizedDescription
    }

    public func syncFromLocalRepo() async {
        guard !availability.isBusy else { return }
        let candidate = effectiveRepoPath ?? preferencesStore.preferences.localRepoPathForSync
        guard let repoPath = candidate else {
            let err = UpdateServiceError.invalidLocalRepo("未配置")
            availability = .failed(err.localizedDescription)
            statusMessage = err.localizedDescription
            return
        }
        await performLocalRepoSync(repoPath: repoPath)
    }

    public func clearLocalRepoPath() async {
        preferencesStore.setLocalRepoPathForSync(nil)
        detectedRepoPath = nil
        detectedRepoVersion = nil
        await probeEnvironment()
    }

    public func openStateDirectory() {
        let dirURL = Self.resolveSyncStateDirectoryURL(environment: environment)
        try? FileManager.default.createDirectory(at: dirURL, withIntermediateDirectories: true)
        let handoffsDir = dirURL.appendingPathComponent("handoffs", isDirectory: true)
        let target = FileManager.default.fileExists(atPath: handoffsDir.path) ? handoffsDir : dirURL
        NSWorkspace.shared.open(target)
    }

    public func relaunchApp() {
        guard let bundleURL = currentAppBundleURL,
              FileManager.default.fileExists(atPath: bundleURL.path)
        else {
            let msg = "未找到可重启的应用包路径。"
            availability = .failed(msg)
            statusMessage = msg
            return
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-n", bundleURL.path]
        do {
            try proc.run()
            NSApp.terminate(nil)
        } catch {
            let msg = "重启应用失败：\(error.localizedDescription)"
            availability = .failed(msg)
            statusMessage = msg
        }
    }

    public static func resolveSyncStateDirectoryURL(
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

    public static func isValidSourceRepo(_ repoURL: URL, fileManager: FileManager = .default) -> Bool {
        guard let validatedURL = try? UpdatePathGuard.validateNonTemporaryURL(repoURL) else {
            return false
        }
        let pyproject = validatedURL.appendingPathComponent("pyproject.toml").path
        let buildScript = validatedURL.appendingPathComponent("scripts/build_menubar_app.sh").path
        return fileManager.fileExists(atPath: pyproject) && fileManager.isExecutableFile(atPath: buildScript)
    }

    private static let pyprojectVersionRegex = try! NSRegularExpression(
        pattern: #"(?m)^version\s*=\s*"([^"]+)""#
    )

    public static func parsePyprojectVersion(in repoURL: URL) -> String? {
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

    private func makeStagingSessionDirectory() throws -> URL {
        let fm = FileManager.default
        let baseRoot: URL
        if let stagingRootOverride {
            baseRoot = try UpdatePathGuard.validateNonTemporaryURL(stagingRootOverride)
        } else {
            baseRoot = try UpdatePathGuard.defaultStagingRootURL(fileManager: fm)
        }
        try fm.createDirectory(at: baseRoot, withIntermediateDirectories: true)
        UpdatePathGuard.pruneStaleStagingDirectories(in: baseRoot, fileManager: fm)
        let sessionDir = baseRoot.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let validated = try UpdatePathGuard.validateNonTemporaryURL(sessionDir)
        try fm.createDirectory(at: validated, withIntermediateDirectories: true)
        return validated
    }

    private func performLocalRepoSync(repoPath: String) async {
        let rawRepoURL = URL(fileURLWithPath: (repoPath as NSString).expandingTildeInPath, isDirectory: true)
        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)

        do {
            let repoURL = try UpdatePathGuard.validateNonTemporaryURL(rawRepoURL)
            guard Self.isValidSourceRepo(repoURL) else {
                throw UpdateServiceError.invalidLocalRepo(repoPath)
            }
            try validateReplacementTargetIfNeeded()

            let cliURL = resolvedCLIPath.map { URL(fileURLWithPath: $0) }
            let pyEnv = try await resolvePythonEnvironment(for: cliURL, childEnv: childEnv)

            let stagingDir = try makeStagingSessionDirectory()
            defer { try? FileManager.default.removeItem(at: stagingDir) }

            availability = .updating(stepDescription: "正在从源码仓构建并签名 CodexAgyHandoff.app…")
            let appStageDir = stagingDir.appendingPathComponent("app-out", isDirectory: true)
            try FileManager.default.createDirectory(at: appStageDir, withIntermediateDirectories: true)
            let buildScript = repoURL.appendingPathComponent("scripts/build_menubar_app.sh")
            let buildResult = try await commandRunner.run(
                executableURL: buildScript,
                arguments: [
                    "--output-dir", appStageDir.path,
                    "--bundle-id", currentBundleIdentifier,
                ],
                environment: childEnv,
                timeoutSeconds: 180.0
            )
            if buildResult.exitCode != 0 {
                throw UpdateServiceError.commandFailed(
                    step: "构建菜单栏应用",
                    detail: buildResult.stderrString.trimmingCharacters(in: .whitespacesAndNewlines)
                )
            }

            let builtAppURL = appStageDir.appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
            try await verifyCodesign(of: builtAppURL, childEnv: childEnv)

            availability = .updating(stepDescription: "正在升级 Python CLI 环境…")
            var pipArgs = ["-m", "pip", "install", "--no-build-isolation"]
            if !pyEnv.inVenv {
                pipArgs.append("--user")
                if pyEnv.supportsBreakSystemPackages {
                    pipArgs.append("--break-system-packages")
                }
            }
            if pyEnv.editable {
                pipArgs.append(contentsOf: ["--no-deps", "--force-reinstall", "-e", repoURL.path])
            } else {
                pipArgs.append(contentsOf: ["--no-deps", "--upgrade", repoURL.path])
            }

            let pipResult = try await commandRunner.run(
                executableURL: pyEnv.pythonURL,
                arguments: pipArgs,
                environment: childEnv,
                timeoutSeconds: 90.0
            )
            if pipResult.exitCode != 0 {
                throw UpdateServiceError.commandFailed(
                    step: "pip 升级 CLI",
                    detail: pipResult.stderrString.trimmingCharacters(in: .whitespacesAndNewlines)
                )
            }

            try replaceRunningBundleIfPermitted(with: builtAppURL)

            let rawTargetVer = Self.parsePyprojectVersion(in: repoURL) ?? appVersion
            let targetVer = SemanticVersion.parse(rawTargetVer)?.description ?? rawTargetVer
            pendingRelaunchVersion = targetVer
            canRelaunchApp = true
            await probeEnvironment()
            availability = .upToDate(currentVersion: targetVer)
            statusMessage = "已从本地源码仓同步更新至 v\(targetVer)，点击“立即重启应用”加载新壳层。"
        } catch {
            let errText = error.localizedDescription
            await probeEnvironment()
            availability = .failed(errText)
            statusMessage = errText
        }
    }

    private func performReleaseSync(_ release: GitHubReleaseInfo) async {
        guard let checksumsAsset = release.checksumsAsset else {
            let err = UpdateServiceError.missingChecksumsAsset
            availability = .failed(err.localizedDescription)
            statusMessage = err.localizedDescription
            return
        }
        guard release.wheelAsset != nil || release.appZipAsset != nil else {
            let err = UpdateServiceError.missingUpdateAssets
            availability = .failed(err.localizedDescription)
            statusMessage = err.localizedDescription
            return
        }

        let childEnv = SubprocessCLIClient.mergedEnvironment(from: environment)

        do {
            if release.appZipAsset != nil {
                try validateReplacementTargetIfNeeded()
            }
            let resolvedPyEnv: PythonRuntimeEnvironment?
            if release.wheelAsset != nil {
                let cliURL = resolvedCLIPath.map { URL(fileURLWithPath: $0) }
                resolvedPyEnv = try await resolvePythonEnvironment(for: cliURL, childEnv: childEnv)
            } else {
                resolvedPyEnv = nil
            }

            let stagingDir = try makeStagingSessionDirectory()
            defer { try? FileManager.default.removeItem(at: stagingDir) }

            availability = .updating(stepDescription: "正在下载并校验 SHA256SUMS…")
            let (checksumsData, checksumsStatus) = try await httpFetcher.fetchData(
                from: checksumsAsset.browserDownloadURL,
                headers: ["User-Agent": "CodexAgyHandoff/\(appVersion)"],
                timeoutSeconds: 15.0
            )
            guard (200..<300).contains(checksumsStatus),
                  let checksumsText = String(data: checksumsData, encoding: .utf8)
            else {
                throw UpdateServiceError.httpError(statusCode: checksumsStatus)
            }
            let checksums = SHA256Verifier.parseChecksums(checksumsText)

            var verifiedWheelURL: URL?
            if let wheelAsset = release.wheelAsset {
                let wheelDest = try UpdatePathGuard.stagedAssetFileURL(for: wheelAsset.name, in: stagingDir)
                let safeWheelName = wheelDest.lastPathComponent
                availability = .updating(stepDescription: "正在下载并校验 CLI 安装包（\(safeWheelName)）…")
                let (wheelData, wheelStatus) = try await httpFetcher.fetchData(
                    from: wheelAsset.browserDownloadURL,
                    headers: ["User-Agent": "CodexAgyHandoff/\(appVersion)"],
                    timeoutSeconds: 30.0
                )
                guard (200..<300).contains(wheelStatus) else {
                    throw UpdateServiceError.httpError(statusCode: wheelStatus)
                }
                try SHA256Verifier.verify(data: wheelData, assetName: safeWheelName, checksums: checksums)
                try wheelData.write(to: wheelDest, options: .atomic)
                verifiedWheelURL = wheelDest
            }

            var verifiedExtractedAppURL: URL?
            if let appZipAsset = release.appZipAsset {
                let zipDest = try UpdatePathGuard.stagedAssetFileURL(for: appZipAsset.name, in: stagingDir)
                let safeZipName = zipDest.lastPathComponent
                availability = .updating(stepDescription: "正在下载并校验应用包（\(safeZipName)）…")
                let (zipData, zipStatus) = try await httpFetcher.fetchData(
                    from: appZipAsset.browserDownloadURL,
                    headers: ["User-Agent": "CodexAgyHandoff/\(appVersion)"],
                    timeoutSeconds: 60.0
                )
                guard (200..<300).contains(zipStatus) else {
                    throw UpdateServiceError.httpError(statusCode: zipStatus)
                }
                try SHA256Verifier.verify(data: zipData, assetName: safeZipName, checksums: checksums)
                try zipData.write(to: zipDest, options: .atomic)

                availability = .updating(stepDescription: "正在解包并校验应用签名…")
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
                        step: "解压应用包",
                        detail: dittoOut.stderrString.trimmingCharacters(in: .whitespacesAndNewlines)
                    )
                }
                let extractedApp = extractDir.appendingPathComponent("CodexAgyHandoff.app", isDirectory: true)
                try await verifyCodesign(of: extractedApp, childEnv: childEnv)
                verifiedExtractedAppURL = extractedApp
            }

            if let wheelPath = verifiedWheelURL, let pyEnv = resolvedPyEnv {
                availability = .updating(stepDescription: "正在升级 codex-agy-sync CLI…")
                var pipArgs = ["-m", "pip", "install", "--upgrade"]
                if !pyEnv.inVenv {
                    pipArgs.append("--user")
                    if pyEnv.supportsBreakSystemPackages {
                        pipArgs.append("--break-system-packages")
                    }
                }
                pipArgs.append(wheelPath.path)
                let pipOut = try await commandRunner.run(
                    executableURL: pyEnv.pythonURL,
                    arguments: pipArgs,
                    environment: childEnv,
                    timeoutSeconds: 90.0
                )
                if pipOut.exitCode != 0 {
                    throw UpdateServiceError.commandFailed(
                        step: "pip 安装 wheel",
                        detail: pipOut.stderrString.trimmingCharacters(in: .whitespacesAndNewlines)
                    )
                }
            }

            let normalizedReleaseVer = SemanticVersion.parse(release.version)?.description ?? release.version
            if let extractedApp = verifiedExtractedAppURL {
                try replaceRunningBundleIfPermitted(with: extractedApp)
                pendingRelaunchVersion = normalizedReleaseVer
                canRelaunchApp = true
            }

            await probeEnvironment()
            availability = .upToDate(currentVersion: normalizedReleaseVer)
            if verifiedExtractedAppURL != nil {
                statusMessage = "已同步升级至 \(release.tagName)，点击“立即重启应用”加载新应用壳层。"
            } else {
                statusMessage = "已校验并升级 CLI 至 \(release.tagName)（该 Release 未附带 .app 压缩包）。"
            }
        } catch {
            let errText = error.localizedDescription
            await probeEnvironment()
            availability = .failed(errText)
            statusMessage = errText
        }
    }

    private func verifyCodesign(of appURL: URL, childEnv: [String: String]) async throws {
        guard FileManager.default.fileExists(atPath: appURL.path) else {
            throw UpdateServiceError.codesignVerificationFailed("未找到 \(appURL.lastPathComponent)")
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

    private func validateReplacementTargetIfNeeded() throws {
        if environment[Self.skipSelfReplaceEnvKey] == "1" {
            return
        }
        guard let targetBundleURL = currentAppBundleURL else {
            throw UpdateServiceError.targetBundleNotWritable("当前未从 .app 应用包启动")
        }
        let validatedTarget = try UpdatePathGuard.validateNonTemporaryURL(targetBundleURL)
        let fm = FileManager.default
        let parentDir = validatedTarget.deletingLastPathComponent()
        guard fm.fileExists(atPath: parentDir.path), fm.isWritableFile(atPath: parentDir.path) else {
            throw UpdateServiceError.targetBundleNotWritable(parentDir.path)
        }
        if fm.fileExists(atPath: validatedTarget.path), !fm.isWritableFile(atPath: validatedTarget.path) {
            throw UpdateServiceError.targetBundleNotWritable(validatedTarget.path)
        }
    }

    private func replaceRunningBundleIfPermitted(with stagedAppURL: URL) throws {
        if environment[Self.skipSelfReplaceEnvKey] == "1" {
            return
        }
        try validateReplacementTargetIfNeeded()
        guard let targetBundleURL = currentAppBundleURL else {
            throw UpdateServiceError.targetBundleNotWritable("当前未从 .app 应用包启动")
        }
        let fm = FileManager.default
        if fm.fileExists(atPath: targetBundleURL.path) {
            _ = try fm.replaceItemAt(targetBundleURL, withItemAt: stagedAppURL)
        } else {
            try fm.moveItem(at: stagedAppURL, to: targetBundleURL)
        }
    }

    private struct PythonProbePayload: Decodable {
        let executable: String
        let in_venv: Bool
        let editable: Bool
        let repo_path: String?
        let supports_break_system_packages: Bool?
    }

    private struct PythonRuntimeEnvironment {
        let pythonURL: URL
        let inVenv: Bool
        let editable: Bool
        let repoPath: String?
        let supportsBreakSystemPackages: Bool
    }

    private func resolvePythonEnvironment(
        for cliURL: URL?,
        childEnv: [String: String]
    ) async throws -> PythonRuntimeEnvironment {
        guard let candidatePython = Self.findPythonExecutable(for: cliURL, childEnv: childEnv) else {
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

    private func updateRepoDiscovery(fromCLI cliURL: URL?) {
        if let customRepo = preferencesStore.preferences.localRepoPathForSync {
            let customURL = URL(fileURLWithPath: customRepo, isDirectory: true)
            if let validated = try? UpdatePathGuard.validateNonTemporaryURL(customURL),
               Self.isValidSourceRepo(validated)
            {
                detectedRepoPath = validated.path
                detectedRepoVersion = Self.parsePyprojectVersion(in: validated)
                return
            }
        }
        if let cliURL {
            let resolvedCLI = cliURL.resolvingSymlinksInPath()
            let ancestorRepo = resolvedCLI.deletingLastPathComponent().deletingLastPathComponent()
            if let validated = try? UpdatePathGuard.validateNonTemporaryURL(ancestorRepo),
               Self.isValidSourceRepo(validated)
            {
                detectedRepoPath = validated.path
                detectedRepoVersion = Self.parsePyprojectVersion(in: validated)
                return
            }
        }
        detectedRepoPath = nil
        detectedRepoVersion = nil
    }

    private func discoverEditableRepoIfNeeded(fromCLI cliURL: URL, childEnv: [String: String]) async {
        updateRepoDiscovery(fromCLI: cliURL)
        guard detectedRepoPath == nil else { return }

        if let pyInfo = try? await resolvePythonEnvironment(for: cliURL, childEnv: childEnv),
           let repoPath = pyInfo.repoPath
        {
            let repoURL = URL(fileURLWithPath: repoPath, isDirectory: true)
            if let validated = try? UpdatePathGuard.validateNonTemporaryURL(repoURL),
               Self.isValidSourceRepo(validated)
            {
                detectedRepoPath = validated.path
                detectedRepoVersion = Self.parsePyprojectVersion(in: validated)
            }
        }
    }

    public static func findPythonExecutable(
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
