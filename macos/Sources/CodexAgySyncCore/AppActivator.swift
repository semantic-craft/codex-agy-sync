import AppKit
import Foundation

public protocol AppActivating: Sendable {
    @discardableResult
    func activateTarget(host: String) async -> Bool
}

private struct HostAppSpec: Sendable {
    let bundleIdentifier: String
    let fallbackPaths: [String]
}

@MainActor
public final class WorkspaceAppActivator: AppActivating {
    private static let hostSpecs: [String: HostAppSpec] = [
        "codex": HostAppSpec(
            bundleIdentifier: "com.openai.codex",
            fallbackPaths: ["/Applications/ChatGPT.app", "/Applications/Codex.app"]
        ),
        "antigravity": HostAppSpec(
            bundleIdentifier: "com.google.antigravity",
            fallbackPaths: ["/Applications/Antigravity.app"]
        ),
    ]

    private let environment: [String: String]
    public private(set) var recordedActivations: [String] = []

    public init(environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.environment = environment
    }

    @discardableResult
    public func activateTarget(host: String) async -> Bool {
        let normalized = host.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard let spec = Self.hostSpecs[normalized] else {
            return false
        }

        if environment["MENUBAR_SKIP_APP_ACTIVATION"] == "1" {
            recordedActivations.append(normalized)
            return true
        }

        let workspace = NSWorkspace.shared
        let resolvedURL = workspace.urlForApplication(withBundleIdentifier: spec.bundleIdentifier)
            ?? spec.fallbackPaths
                .map { URL(fileURLWithPath: $0) }
                .first { FileManager.default.fileExists(atPath: $0.path) }

        guard let targetURL = resolvedURL else {
            return false
        }

        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        do {
            _ = try await workspace.openApplication(at: targetURL, configuration: config)
            recordedActivations.append(normalized)
            return true
        } catch {
            return false
        }
    }
}
