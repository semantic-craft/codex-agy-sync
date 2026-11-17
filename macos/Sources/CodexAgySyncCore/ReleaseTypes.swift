import Foundation

public enum VersionAlignmentState: Equatable, Sendable {
    case aligned(version: String)
    case mismatched(appVersion: String, cliVersion: String)
    case cliUnavailable(appVersion: String, reason: String)

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
    case releaseAvailable(latestTag: String, notesSummary: String, hasAppAsset: Bool)
    case stagedReady(tagName: String, version: String, notesSummary: String, hasAppAsset: Bool)
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
    public let appBundleName: String?
    public let stagedAtEpoch: TimeInterval

    public init(
        tagName: String,
        version: String,
        notesSummary: String,
        appBundleName: String?,
        stagedAtEpoch: TimeInterval
    ) {
        self.tagName = tagName
        self.version = version
        self.notesSummary = notesSummary
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
    public let appZipAsset: ReleaseAssetInfo?
    public let checksumsAsset: ReleaseAssetInfo?

    var rawNotesSummaryForPersistence: String? {
        rawNotesSummary
    }

    public func notesSummary(in language: ResolvedLanguage) -> String {
        rawNotesSummary ?? AppCopy.text(.updatesDefaultNotesSummary, in: language)
    }

    public init(
        tagName: String,
        version: String,
        htmlURL: URL?,
        notesSummary: String,
        appZipAsset: ReleaseAssetInfo?,
        checksumsAsset: ReleaseAssetInfo?
    ) {
        self.tagName = tagName
        self.version = version
        self.htmlURL = htmlURL
        let trimmed = notesSummary.trimmingCharacters(in: .whitespacesAndNewlines)
        self.rawNotesSummary = trimmed.isEmpty ? nil : trimmed
        self.appZipAsset = appZipAsset
        self.checksumsAsset = checksumsAsset
    }

    private init(
        tagName: String,
        version: String,
        htmlURL: URL?,
        rawNotesSummary: String?,
        appZipAsset: ReleaseAssetInfo?,
        checksumsAsset: ReleaseAssetInfo?
    ) {
        self.tagName = tagName
        self.version = version
        self.htmlURL = htmlURL
        self.rawNotesSummary = rawNotesSummary
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
    case commandFailed(step: String, detail: String)
    case codesignVerificationFailed(String)
    case updateInstallationInProgress

    public var errorDescription: String? {
        localizedMessage(in: AppLanguage.system.resolve())
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
        case .commandFailed(let step, let detail):
            return AppCopy.updateErrCommandFailed(step: step, detail: detail, in: language)
        case .codesignVerificationFailed(let detail):
            return AppCopy.updateErrCodesignFailed(detail, in: language)
        case .updateInstallationInProgress:
            return AppCopy.text(.updatesErrInstallationInProgress, in: language)
        }
    }
}
