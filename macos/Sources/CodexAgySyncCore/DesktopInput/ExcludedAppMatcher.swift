import AppKit
import Foundation

public enum ExcludedAppMatcher {
    public static func normalize(_ bundleIds: [String]) -> [String] {
        var seen: Set<String> = []
        var result: [String] = []
        result.reserveCapacity(bundleIds.count)
        for raw in bundleIds {
            let normalized = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            guard !normalized.isEmpty, seen.insert(normalized).inserted else {
                continue
            }
            result.append(normalized)
        }
        return result
    }

    public static func isExcluded(bundleId: String?, in excludedBundleIds: [String]) -> Bool {
        guard let bundleId else { return false }
        let candidate = bundleId.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !candidate.isEmpty else { return false }
        for raw in excludedBundleIds {
            let normalized = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if !normalized.isEmpty, normalized == candidate {
                return true
            }
        }
        return false
    }
}

public struct InstalledAppDescriptor: Identifiable, Equatable, Sendable {
    public let bundleId: String
    public let displayName: String
    public let appURL: URL?

    public var id: String { bundleId }

    public init(bundleId: String, displayName: String, appURL: URL? = nil) {
        self.bundleId = bundleId
        self.displayName = displayName
        self.appURL = appURL
    }
}

public enum RunningAppCatalog {
    public static func regularRunningApps() -> [InstalledAppDescriptor] {
        var seen: Set<String> = []
        var apps: [InstalledAppDescriptor] = []
        for app in NSWorkspace.shared.runningApplications {
            guard app.activationPolicy == .regular else { continue }
            guard let rawBundleId = app.bundleIdentifier?
                .trimmingCharacters(in: .whitespacesAndNewlines),
                !rawBundleId.isEmpty
            else {
                continue
            }
            let dedupeKey = rawBundleId.lowercased()
            guard seen.insert(dedupeKey).inserted else { continue }
            let url = app.bundleURL
            let name = app.localizedName?
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .nilIfEmpty
                ?? url?.deletingPathExtension().lastPathComponent.nilIfEmpty
                ?? rawBundleId
            apps.append(InstalledAppDescriptor(bundleId: rawBundleId, displayName: name, appURL: url))
        }
        return apps.sorted { lhs, rhs in
            let order = lhs.displayName.localizedCaseInsensitiveCompare(rhs.displayName)
            if order != .orderedSame {
                return order == .orderedAscending
            }
            return lhs.bundleId.localizedCaseInsensitiveCompare(rhs.bundleId) == .orderedAscending
        }
    }

    public static func descriptor(forAppURL appURL: URL) -> InstalledAppDescriptor? {
        guard let bundle = Bundle(url: appURL),
              let rawBundleId = bundle.bundleIdentifier?
                .trimmingCharacters(in: .whitespacesAndNewlines),
              !rawBundleId.isEmpty
        else {
            return nil
        }
        let info = bundle.infoDictionary
        let displayName = (info?["CFBundleDisplayName"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .nilIfEmpty
            ?? (info?["CFBundleName"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .nilIfEmpty
            ?? appURL.deletingPathExtension().lastPathComponent.nilIfEmpty
            ?? rawBundleId
        return InstalledAppDescriptor(
            bundleId: rawBundleId,
            displayName: displayName,
            appURL: appURL
        )
    }
}

private extension String {
    var nilIfEmpty: String? {
        isEmpty ? nil : self
    }
}
