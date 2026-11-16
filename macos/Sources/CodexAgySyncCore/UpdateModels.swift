import AppKit
import CoreFoundation
import CryptoKit
import Foundation

public enum VersionAlignmentState: Equatable, Sendable {
    case aligned(version: String)
    case mismatched(appVersion: String, cliVersion: String)
    case cliUnavailable(appVersion: String, reason: String)

    public var badgeTitle: String {
        badgeTitle(in: .zhHans)
    }

    public func badgeTitle(in language: ResolvedLanguage) -> String {
        switch self {
        case .aligned:
            return AppCopy.text(.updatesAlignmentAligned, in: language)
        case .mismatched:
            return AppCopy.text(.updatesAlignmentMismatched, in: language)
        case .cliUnavailable:
            return AppCopy.text(.updatesAlignmentUnavailable, in: language)
        }
    }

    public var cliVersionLabel: String {
        cliVersionLabel(in: .zhHans)
    }

    public func cliVersionLabel(in language: ResolvedLanguage) -> String {
        switch self {
        case .aligned(let version):
            return "v\(version)"
        case .mismatched(_, let cliVersion):
            return "v\(cliVersion)"
        case .cliUnavailable:
            return AppCopy.text(.updatesCLINotReady, in: language)
        }
    }
}

public enum UpdateAvailability: Equatable, Sendable {
    case idle
    case checking
    case upToDate(currentVersion: String)
    case releaseAvailable(latestTag: String, notesSummary: String, hasAppAsset: Bool, hasWheelAsset: Bool)
    case stagedReady(tagName: String, version: String, notesSummary: String, hasAppAsset: Bool, hasWheelAsset: Bool)
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

public enum UpdateTrigger: Equatable, Sendable {
    case manual
    case appLaunched
    case panelOpened
    case settingsOpened
    case policyChanged

    public var isOpportunistic: Bool {
        self != .manual
    }
}

public struct UpdateCheckCheckpoint: Codable, Equatable, Sendable {
    public var lastSuccessfulCheckEpoch: TimeInterval?
    public var lastAttemptEpoch: TimeInterval?

    public static let successCooldownSeconds: TimeInterval = 86_400
    public static let failureBackoffSeconds: TimeInterval = 3_600

    public init(lastSuccessfulCheckEpoch: TimeInterval? = nil, lastAttemptEpoch: TimeInterval? = nil) {
        self.lastSuccessfulCheckEpoch = lastSuccessfulCheckEpoch
        self.lastAttemptEpoch = lastAttemptEpoch
    }
}

public struct StagedUpdateManifest: Codable, Equatable, Sendable {
    public let tagName: String
    public let version: String
    public let notesSummary: String
    public let wheelFileName: String?
    public let wheelSHA256: String?
    public let appBundleName: String?
    public let stagedAtEpoch: TimeInterval

    public init(
        tagName: String,
        version: String,
        notesSummary: String,
        wheelFileName: String?,
        wheelSHA256: String?,
        appBundleName: String?,
        stagedAtEpoch: TimeInterval
    ) {
        self.tagName = tagName
        self.version = version
        self.notesSummary = notesSummary
        self.wheelFileName = wheelFileName
        self.wheelSHA256 = wheelSHA256
        self.appBundleName = appBundleName
        self.stagedAtEpoch = stagedAtEpoch
    }
}

public enum PopoverUpdateAction: Equatable, Sendable {
    case openUpdatesSettings(tag: String)
    case installStagedAndRelaunch(tag: String)
    case relaunchInstalled(version: String)
}

public struct PopoverUpdateNotice: Equatable, Sendable {
    public let title: String
    public let actionTitle: String
    public let action: PopoverUpdateAction

    public init(title: String, actionTitle: String, action: PopoverUpdateAction) {
        self.title = title
        self.actionTitle = actionTitle
        self.action = action
    }
}

public protocol RelaunchExecuting: Sendable {
    @MainActor func relaunch(bundleURL: URL) throws
}

public final class DefaultRelaunchExecutor: RelaunchExecuting, @unchecked Sendable {
    public init() {}

    @MainActor
    public func relaunch(bundleURL: URL) throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-n", bundleURL.path]
        try proc.run()
        NSApp.terminate(nil)
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
    private let rawNotesSummary: String?
    public let wheelAsset: ReleaseAssetInfo?
    public let appZipAsset: ReleaseAssetInfo?
    public let checksumsAsset: ReleaseAssetInfo?

    var rawNotesSummaryForPersistence: String? {
        rawNotesSummary
    }

    public var notesSummary: String {
        notesSummary(in: .zhHans)
    }

    public func notesSummary(in language: ResolvedLanguage) -> String {
        rawNotesSummary ?? AppCopy.text(.updatesDefaultNotesSummary, in: language)
    }

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
        let trimmed = notesSummary.trimmingCharacters(in: .whitespacesAndNewlines)
        self.rawNotesSummary = trimmed.isEmpty ? nil : trimmed
        self.wheelAsset = wheelAsset
        self.appZipAsset = appZipAsset
        self.checksumsAsset = checksumsAsset
    }

    private init(
        tagName: String,
        version: String,
        htmlURL: URL?,
        rawNotesSummary: String?,
        wheelAsset: ReleaseAssetInfo?,
        appZipAsset: ReleaseAssetInfo?,
        checksumsAsset: ReleaseAssetInfo?
    ) {
        self.tagName = tagName
        self.version = version
        self.htmlURL = htmlURL
        self.rawNotesSummary = rawNotesSummary
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
        let appZip = assets.first { $0.name == "CodexAgySync-macos.zip" }
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
            rawNotesSummary: summary,
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

    private static func summarizeNotes(_ rawBody: String) -> String? {
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
        return nil
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
    case updateInstallationInProgress

    public var errorDescription: String? {
        localizedMessage(in: .zhHans)
    }

    public func localizedMessage(in language: ResolvedLanguage) -> String {
        switch self {
        case .forbiddenTemporaryPath(let path):
            return AppCopy.updateErrForbiddenTempPath(path, in: language)
        case .invalidAssetName(let name):
            return AppCopy.updateErrInvalidAssetName(name, in: language)
        case .invalidReleaseURL(let url):
            return AppCopy.updateErrInvalidReleaseURL(url, in: language)
        case .httpError(let code):
            return AppCopy.updateErrHttpStatus(code, in: language)
        case .missingChecksumsAsset:
            return AppCopy.text(.updatesErrMissingChecksums, in: language)
        case .missingUpdateAssets:
            return AppCopy.text(.updatesErrMissingAssets, in: language)
        case .missingChecksum(let name):
            return AppCopy.updateErrMissingChecksum(assetName: name, in: language)
        case .checksumMismatch(let name, let expected, let actual):
            return AppCopy.updateErrChecksumMismatch(
                assetName: name,
                expected: expected,
                actual: actual,
                in: language
            )
        case .invalidLocalRepo(let path):
            return AppCopy.updateErrInvalidLocalRepo(path, in: language)
        case .targetBundleNotWritable(let detail):
            return AppCopy.updateErrTargetBundleNotWritable(detail, in: language)
        case .pythonInterpreterMissing:
            return AppCopy.text(.updatesErrPythonMissing, in: language)
        case .commandFailed(let step, let detail):
            return AppCopy.updateErrCommandFailed(step: step, detail: detail, in: language)
        case .codesignVerificationFailed(let detail):
            return AppCopy.updateErrCodesignFailed(detail, in: language)
        case .updateInstallationInProgress:
            return AppCopy.text(.updatesErrInstallationInProgress, in: language)
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
            .appendingPathComponent("CodexAgySync", isDirectory: true)
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
        excludingNames: Set<String> = [],
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
            if excludingNames.contains(canonicalChild.lastPathComponent) {
                continue
            }
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

    public static func pruneStagedVersions(
        in stagedRoot: URL,
        keepingVersion keptName: String?,
        fileManager: FileManager = .default
    ) {
        guard let validatedRoot = try? validateNonTemporaryURL(stagedRoot) else { return }
        let canonicalRoot = validatedRoot.standardizedFileURL
        guard let children = try? fileManager.contentsOfDirectory(
            at: canonicalRoot,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) else {
            return
        }
        for child in children {
            let canonicalChild = child.standardizedFileURL
            guard canonicalChild.deletingLastPathComponent() == canonicalRoot else { continue }
            if let keptName, canonicalChild.lastPathComponent == keptName {
                continue
            }
            try? fileManager.removeItem(at: canonicalChild)
        }
    }

    public static func cleanupLegacyUpdateResidue(
        appSupportDirectory: URL? = nil,
        cachesDirectory: URL? = nil,
        preferencesDirectory: URL? = nil,
        removeLegacyPreferencesDomains: Bool = false,
        fileManager: FileManager = .default
    ) {
        let legacyAppName = PreferencesStore.legacyAppName
        let legacyBundleID = PreferencesStore.legacyPreferencesSuiteName

        if let targetAppSupport = resolveCleanupDirectory(
            explicit: appSupportDirectory,
            defaultDirectory: fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
        ) {
            let legacySupportDir = targetAppSupport.appendingPathComponent(legacyAppName, isDirectory: true)
            try? fileManager.removeItem(at: legacySupportDir)
        }

        if let targetCaches = resolveCleanupDirectory(
            explicit: cachesDirectory,
            defaultDirectory: fileManager.urls(for: .cachesDirectory, in: .userDomainMask).first
        ) {
            let legacyCacheBundleDir = targetCaches.appendingPathComponent(legacyBundleID, isDirectory: true)
            try? fileManager.removeItem(at: legacyCacheBundleDir)
            let legacyCacheAppDir = targetCaches.appendingPathComponent(legacyAppName, isDirectory: true)
            try? fileManager.removeItem(at: legacyCacheAppDir)
        }

        if removeLegacyPreferencesDomains,
           let targetPrefsDir = resolveCleanupDirectory(
               explicit: preferencesDirectory,
               defaultDirectory: fileManager.homeDirectoryForCurrentUser.appendingPathComponent(
                   "Library/Preferences",
                   isDirectory: true
               )
           )
        {
            if preferencesDirectory == nil {
                for domain in [legacyBundleID, legacyAppName] {
                    let plistURL = targetPrefsDir.appendingPathComponent("\(domain).plist", isDirectory: false)
                    let hasPlistItem = (try? fileManager.attributesOfItem(atPath: plistURL.path)) != nil
                    let anyKeys = (CFPreferencesCopyKeyList(
                        domain as CFString,
                        kCFPreferencesCurrentUser,
                        kCFPreferencesAnyHost
                    ) as? [String]) ?? []
                    let hostKeys = (CFPreferencesCopyKeyList(
                        domain as CFString,
                        kCFPreferencesCurrentUser,
                        kCFPreferencesCurrentHost
                    ) as? [String]) ?? []

                    if !anyKeys.isEmpty || !hostKeys.isEmpty {
                        if !anyKeys.isEmpty {
                            CFPreferencesSetMultiple(
                                nil,
                                anyKeys as CFArray,
                                domain as CFString,
                                kCFPreferencesCurrentUser,
                                kCFPreferencesAnyHost
                            )
                        }
                        if !hostKeys.isEmpty {
                            CFPreferencesSetMultiple(
                                nil,
                                hostKeys as CFArray,
                                domain as CFString,
                                kCFPreferencesCurrentUser,
                                kCFPreferencesCurrentHost
                            )
                        }
                        _ = CFPreferencesSynchronize(
                            domain as CFString,
                            kCFPreferencesCurrentUser,
                            kCFPreferencesAnyHost
                        )
                        _ = CFPreferencesAppSynchronize(domain as CFString)
                        try? fileManager.removeItem(at: plistURL)
                        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 8.0) {
                            try? FileManager.default.removeItem(at: plistURL)
                        }
                    } else if hasPlistItem {
                        try? fileManager.removeItem(at: plistURL)
                    }
                }
            } else {
                let legacyBundlePlist = targetPrefsDir.appendingPathComponent("\(legacyBundleID).plist", isDirectory: false)
                let legacyAppPlist = targetPrefsDir.appendingPathComponent("\(legacyAppName).plist", isDirectory: false)
                try? fileManager.removeItem(at: legacyBundlePlist)
                try? fileManager.removeItem(at: legacyAppPlist)
            }
        }
    }

    private static func resolveCleanupDirectory(
        explicit: URL?,
        defaultDirectory: @autoclosure () -> URL?
    ) -> URL? {
        let candidate: URL
        if let explicit {
            candidate = explicit.standardizedFileURL
        } else {
            guard let hostDefault = defaultDirectory(),
                  let validated = try? validateNonTemporaryURL(hostDefault)
            else {
                return nil
            }
            candidate = validated.standardizedFileURL
        }
        let path = candidate.path
        guard !path.isEmpty, path != "/" else {
            return nil
        }
        return candidate
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
