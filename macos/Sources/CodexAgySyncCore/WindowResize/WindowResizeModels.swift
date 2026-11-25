import Carbon.HIToolbox
import CoreGraphics
import Foundation
import Observation

public struct WindowResizePreferences: Codable, Equatable, Sendable {
    private enum CodingKeys: String, CodingKey {
        case isEnabled
        case nearestCornerResize
        case excludedBundleIds
    }

    public var isEnabled: Bool
    public var nearestCornerResize: Bool
    public var excludedBundleIds: [String]

    public init(
        isEnabled: Bool = false,
        nearestCornerResize: Bool = false,
        excludedBundleIds: [String] = []
    ) {
        self.isEnabled = isEnabled
        self.nearestCornerResize = nearestCornerResize
        self.excludedBundleIds = ExcludedAppMatcher.normalize(excludedBundleIds)
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.isEnabled = try container.decodeIfPresent(Bool.self, forKey: .isEnabled) ?? false
        self.nearestCornerResize = try container.decodeIfPresent(Bool.self, forKey: .nearestCornerResize) ?? false
        let rawExcluded = try container.decodeIfPresent([String].self, forKey: .excludedBundleIds) ?? []
        self.excludedBundleIds = ExcludedAppMatcher.normalize(rawExcluded)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(isEnabled, forKey: .isEnabled)
        try container.encode(nearestCornerResize, forKey: .nearestCornerResize)
        try container.encode(ExcludedAppMatcher.normalize(excludedBundleIds), forKey: .excludedBundleIds)
    }
}

public enum ResizeAnchorCorner: String, CaseIterable, Equatable, Sendable {
    case topLeft
    case topRight
    case bottomLeft
    case bottomRight
}

public enum WindowPlacementAction: String, CaseIterable, Equatable, Sendable {
    case tileLeft
    case tileRight
    case tileTop
    case tileBottom
    case fill
    case center
    case restorePrevious

    public init?(keyCode: UInt16) {
        switch Int(keyCode) {
        case kVK_LeftArrow:
            self = .tileLeft
        case kVK_RightArrow:
            self = .tileRight
        case kVK_UpArrow:
            self = .tileTop
        case kVK_DownArrow:
            self = .tileBottom
        case kVK_ANSI_F:
            self = .fill
        case kVK_ANSI_C:
            self = .center
        case kVK_ANSI_R:
            self = .restorePrevious
        default:
            return nil
        }
    }

    public var keyCode: UInt16 {
        switch self {
        case .tileLeft:
            return UInt16(kVK_LeftArrow)
        case .tileRight:
            return UInt16(kVK_RightArrow)
        case .tileTop:
            return UInt16(kVK_UpArrow)
        case .tileBottom:
            return UInt16(kVK_DownArrow)
        case .fill:
            return UInt16(kVK_ANSI_F)
        case .center:
            return UInt16(kVK_ANSI_C)
        case .restorePrevious:
            return UInt16(kVK_ANSI_R)
        }
    }

    public var isArrowKey: Bool {
        switch self {
        case .tileLeft, .tileRight, .tileTop, .tileBottom:
            return true
        case .fill, .center, .restorePrevious:
            return false
        }
    }

    public var shortcutLabel: String {
        switch self {
        case .tileLeft:
            return "fn⌃←"
        case .tileRight:
            return "fn⌃→"
        case .tileTop:
            return "fn⌃↑"
        case .tileBottom:
            return "fn⌃↓"
        case .fill:
            return "fn⌃F"
        case .center:
            return "fn⌃C"
        case .restorePrevious:
            return "fn⌃R"
        }
    }
}

public enum SplitScreenGestureAction: String, CaseIterable, Equatable, Sendable {
    case splitLeft
    case splitRight
    case enterFullScreen
    case exitFullScreenOrSplit

    public var symbolName: String {
        switch self {
        case .splitLeft:
            return "rectangle.lefthalf.inset.filled"
        case .splitRight:
            return "rectangle.righthalf.inset.filled"
        case .enterFullScreen:
            return "arrow.up.left.and.arrow.down.right"
        case .exitFullScreenOrSplit:
            return "arrow.down.right.and.arrow.up.left"
        }
    }

    public func localizedTitle(in language: ResolvedLanguage) -> String {
        switch self {
        case .splitLeft:
            return AppCopy.text(.windowResizeHUDSplitLeft, in: language)
        case .splitRight:
            return AppCopy.text(.windowResizeHUDSplitRight, in: language)
        case .enterFullScreen:
            return AppCopy.text(.windowResizeHUDEnterFullScreen, in: language)
        case .exitFullScreenOrSplit:
            return AppCopy.text(.windowResizeHUDExitFullScreen, in: language)
        }
    }
}

public struct WindowTargetSnapshot: Equatable, Sendable {
    public let windowId: UInt64
    public let pid: pid_t
    public let bundleId: String?
    public let frame: CGRect
    public let isMovable: Bool
    public let isResizable: Bool
    public let isFullScreen: Bool

    public init(
        windowId: UInt64,
        pid: pid_t,
        bundleId: String?,
        frame: CGRect,
        isMovable: Bool = true,
        isResizable: Bool = true,
        isFullScreen: Bool = false
    ) {
        self.windowId = windowId
        self.pid = pid
        self.bundleId = bundleId
        self.frame = frame
        self.isMovable = isMovable
        self.isResizable = isResizable
        self.isFullScreen = isFullScreen
    }

    public func updatingFrame(_ newFrame: CGRect) -> WindowTargetSnapshot {
        WindowTargetSnapshot(
            windowId: windowId,
            pid: pid,
            bundleId: bundleId,
            frame: newFrame,
            isMovable: isMovable,
            isResizable: isResizable,
            isFullScreen: isFullScreen
        )
    }
}

public enum WindowResizeActionResult: Equatable, Sendable {
    case applied
    case unsupportedByTargetApp(detail: String)
    case unsupportedBySystemState(detail: String)
    case permissionDenied
    case excludedApp

    public func localizedDiagnostic(in language: ResolvedLanguage) -> String? {
        switch self {
        case .applied, .excludedApp:
            return nil
        case .unsupportedByTargetApp(let detail):
            return AppCopy.windowResizeDiagnosticUnsupportedApp(detail, in: language)
        case .unsupportedBySystemState(let detail):
            return AppCopy.windowResizeDiagnosticUnsupportedSystem(detail, in: language)
        case .permissionDenied:
            return AppCopy.text(.windowResizeDiagnosticPermissionDenied, in: language)
        }
    }
}

public struct WindowResizeHUDState: Equatable, Sendable {
    public static let hidden = WindowResizeHUDState(isVisible: false, action: nil, anchorPoint: .zero)

    public let isVisible: Bool
    public let action: SplitScreenGestureAction?
    public let anchorPoint: CGPoint

    public init(
        isVisible: Bool,
        action: SplitScreenGestureAction?,
        anchorPoint: CGPoint
    ) {
        self.isVisible = isVisible
        self.action = action
        self.anchorPoint = anchorPoint
    }
}

public enum WindowResizeShortcutCatalog {
    public static func bindings() -> [ShortcutBindingDescriptor] {
        WindowPlacementAction.allCases.map { action in
            ShortcutBindingDescriptor(
                owner: .windowResize,
                label: action.shortcutLabel,
                keyCode: action.keyCode,
                modifiers: [.fn, .control]
            )
        }
    }
}

@MainActor
@Observable
public final class WindowResizeStatusStore {
    public private(set) var lastResult: WindowResizeActionResult?

    public init(lastResult: WindowResizeActionResult? = nil) {
        self.lastResult = lastResult
    }

    public func record(_ result: WindowResizeActionResult) {
        switch result {
        case .applied:
            lastResult = nil
        case .excludedApp:
            break
        case .unsupportedByTargetApp, .unsupportedBySystemState, .permissionDenied:
            lastResult = result
        }
    }

    public func clearDiagnostic() {
        lastResult = nil
    }

    public func diagnosticMessage(in language: ResolvedLanguage) -> String? {
        lastResult?.localizedDiagnostic(in: language)
    }
}
