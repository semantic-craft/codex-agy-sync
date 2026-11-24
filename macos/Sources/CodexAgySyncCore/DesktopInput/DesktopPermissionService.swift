import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

public struct DesktopPermissionState: Equatable, Sendable {
    public let accessibilityGranted: Bool
    public let inputMonitoringGranted: Bool

    public var hasEventTapAccess: Bool {
        accessibilityGranted && inputMonitoringGranted
    }

    public init(accessibilityGranted: Bool, inputMonitoringGranted: Bool) {
        self.accessibilityGranted = accessibilityGranted
        self.inputMonitoringGranted = inputMonitoringGranted
    }
}

public protocol DesktopPermissionChecking: Sendable {
    func currentState() -> DesktopPermissionState
    @discardableResult func requestAccessibilityPrompt() -> Bool
    @discardableResult func requestInputMonitoringPrompt() -> Bool
    @discardableResult func openAccessibilitySettings() -> Bool
    @discardableResult func openInputMonitoringSettings() -> Bool
}

public struct SystemDesktopPermissionService: DesktopPermissionChecking, Sendable {
    private static let accessibilityPromptOptionKey = "AXTrustedCheckOptionPrompt"
    private static let accessibilitySettingsURL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    private static let inputMonitoringSettingsURL = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"

    public init() {}

    public func currentState() -> DesktopPermissionState {
        DesktopPermissionState(
            accessibilityGranted: AXIsProcessTrusted(),
            inputMonitoringGranted: CGPreflightListenEventAccess()
        )
    }

    @discardableResult
    public func requestAccessibilityPrompt() -> Bool {
        let options = [Self.accessibilityPromptOptionKey: true] as CFDictionary
        return AXIsProcessTrustedWithOptions(options)
    }

    @discardableResult
    public func requestInputMonitoringPrompt() -> Bool {
        CGRequestListenEventAccess()
    }

    @discardableResult
    public func openAccessibilitySettings() -> Bool {
        openSystemSettings(urlString: Self.accessibilitySettingsURL)
    }

    @discardableResult
    public func openInputMonitoringSettings() -> Bool {
        openSystemSettings(urlString: Self.inputMonitoringSettingsURL)
    }

    private func openSystemSettings(urlString: String) -> Bool {
        guard let url = URL(string: urlString) else { return false }
        return NSWorkspace.shared.open(url)
    }
}
