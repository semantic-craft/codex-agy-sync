import CoreFoundation
import CryptoKit
import Foundation

public enum SHA256Verifier {
    public static func sha256Hex(of data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    public static func sha256Hex(fileURL: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: fileURL)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let chunk = try handle.read(upToCount: 65_536) ?? Data()
            if chunk.isEmpty { break }
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
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

    @discardableResult
    public static func verify(fileURL: URL, assetName: String, checksums: [String: String]) throws -> String {
        guard let expected = checksums[assetName]?.lowercased() else {
            throw UpdateServiceError.missingChecksum(assetName: assetName)
        }
        let actual = try sha256Hex(fileURL: fileURL)
        guard actual == expected else {
            throw UpdateServiceError.checksumMismatch(
                assetName: assetName,
                expected: expected,
                actual: actual
            )
        }
        return actual
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
        fileManager: FileManager = .default,
        hasLegacyKeys: ((String) -> Bool)? = nil,
        clearLegacyDomain: ((String) -> Void)? = nil,
        delayedReunlink: (@Sendable (URL, @escaping @Sendable () -> Void) -> Void)? = nil
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
            let useTwoPassCleanup = preferencesDirectory == nil
                || hasLegacyKeys != nil
                || clearLegacyDomain != nil
                || delayedReunlink != nil
            if useTwoPassCleanup {
                let checkKeys = hasLegacyKeys ?? { domain in
                    preferencesDirectory == nil ? defaultHasLegacyKeys(domain: domain) : false
                }
                let clearDomain = clearLegacyDomain ?? { domain in
                    if preferencesDirectory == nil {
                        defaultClearLegacyDomain(domain: domain)
                    }
                }
                let scheduleReunlink = delayedReunlink ?? defaultDelayedReunlink(plistURL:perform:)
                for domain in [legacyBundleID, legacyAppName] {
                    let plistURL = targetPrefsDir.appendingPathComponent("\(domain).plist", isDirectory: false)
                    let hasPlistItem = (try? fileManager.attributesOfItem(atPath: plistURL.path)) != nil
                    if checkKeys(domain) {
                        clearDomain(domain)
                        try? fileManager.removeItem(at: plistURL)
                        scheduleReunlink(plistURL) {
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

    private static func defaultHasLegacyKeys(domain: String) -> Bool {
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
        return !anyKeys.isEmpty || !hostKeys.isEmpty
    }

    private static func defaultClearLegacyDomain(domain: String) {
        let anyKeys = (CFPreferencesCopyKeyList(
            domain as CFString,
            kCFPreferencesCurrentUser,
            kCFPreferencesAnyHost
        ) as? [String]) ?? []
        if !anyKeys.isEmpty {
            CFPreferencesSetMultiple(
                nil,
                anyKeys as CFArray,
                domain as CFString,
                kCFPreferencesCurrentUser,
                kCFPreferencesAnyHost
            )
        }
        let hostKeys = (CFPreferencesCopyKeyList(
            domain as CFString,
            kCFPreferencesCurrentUser,
            kCFPreferencesCurrentHost
        ) as? [String]) ?? []
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
    }

    private static func defaultDelayedReunlink(
        plistURL _: URL,
        perform: @escaping @Sendable () -> Void
    ) {
        Task.detached(priority: .utility) {
            try? await Task.sleep(for: .seconds(8))
            perform()
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
