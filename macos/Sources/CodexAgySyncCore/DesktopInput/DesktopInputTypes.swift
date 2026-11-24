import Carbon.HIToolbox
import CoreGraphics
import Foundation

public enum DesktopFeatureSlot: String, CaseIterable, Hashable, Sendable {
    case syncPanel
    case windowResize
    case cleanPaste
    case appHotKeys
    case relaySlashCommand
}

public struct HotKeySlotID: Hashable, Sendable {
    public let rawValue: String
    public let carbonID: UInt32

    public static let syncPanel = HotKeySlotID(rawValue: "syncPanel", carbonID: 1)

    public init(rawValue: String, carbonID: UInt32) {
        self.rawValue = rawValue
        self.carbonID = carbonID
    }
}

public struct InputModifierSet: OptionSet, Sendable, Hashable, Codable {
    public let rawValue: UInt32

    public static let fn = InputModifierSet(rawValue: 1 << 0)
    public static let control = InputModifierSet(rawValue: 1 << 1)
    public static let option = InputModifierSet(rawValue: 1 << 2)
    public static let shift = InputModifierSet(rawValue: 1 << 3)
    public static let command = InputModifierSet(rawValue: 1 << 4)

    public init(rawValue: UInt32) {
        self.rawValue = rawValue
    }

    public init(cgEventFlags flags: CGEventFlags) {
        var result: InputModifierSet = []
        if flags.contains(.maskSecondaryFn) { result.insert(.fn) }
        if flags.contains(.maskControl) { result.insert(.control) }
        if flags.contains(.maskAlternate) { result.insert(.option) }
        if flags.contains(.maskShift) { result.insert(.shift) }
        if flags.contains(.maskCommand) { result.insert(.command) }
        self = result
    }

    public var cgEventFlags: CGEventFlags {
        var flags: CGEventFlags = []
        if contains(.fn) { flags.insert(.maskSecondaryFn) }
        if contains(.control) { flags.insert(.maskControl) }
        if contains(.option) { flags.insert(.maskAlternate) }
        if contains(.shift) { flags.insert(.maskShift) }
        if contains(.command) { flags.insert(.maskCommand) }
        return flags
    }

    public init(carbonModifiers modifiers: UInt32) {
        var result: InputModifierSet = []
        if (modifiers & UInt32(controlKey)) != 0 { result.insert(.control) }
        if (modifiers & UInt32(optionKey)) != 0 { result.insert(.option) }
        if (modifiers & UInt32(shiftKey)) != 0 { result.insert(.shift) }
        if (modifiers & UInt32(cmdKey)) != 0 { result.insert(.command) }
        self = result
    }

    public var carbonModifiers: UInt32 {
        var mask: UInt32 = 0
        if contains(.control) { mask |= UInt32(controlKey) }
        if contains(.option) { mask |= UInt32(optionKey) }
        if contains(.shift) { mask |= UInt32(shiftKey) }
        if contains(.command) { mask |= UInt32(cmdKey) }
        return mask
    }
}

public enum RoutedInputEventKind: Equatable, Sendable {
    case flagsChanged(modifiers: InputModifierSet)
    case keyDown(keyCode: UInt16, modifiers: InputModifierSet, isRepeat: Bool)
    case keyUp(keyCode: UInt16, modifiers: InputModifierSet)
    case mouseMoved(location: CGPoint, modifiers: InputModifierSet)
    case leftMouseDown(location: CGPoint, modifiers: InputModifierSet)
    case leftMouseDragged(location: CGPoint, modifiers: InputModifierSet)
    case leftMouseUp(location: CGPoint, modifiers: InputModifierSet)

    public var isKeyboardEvent: Bool {
        switch self {
        case .flagsChanged, .keyDown, .keyUp:
            return true
        case .mouseMoved, .leftMouseDown, .leftMouseDragged, .leftMouseUp:
            return false
        }
    }

    public var tapRequirement: EventTapRequirement {
        switch self {
        case .flagsChanged:
            return .flagsChanged
        case .keyDown:
            return .keyDown
        case .keyUp:
            return .keyUp
        case .mouseMoved:
            return .mouseMoved
        case .leftMouseDown, .leftMouseDragged, .leftMouseUp:
            return .leftMouse
        }
    }
}

public struct RoutedInputEvent: Equatable, Sendable {
    public let kind: RoutedInputEventKind
    public let timestamp: TimeInterval

    public init(kind: RoutedInputEventKind, timestamp: TimeInterval = 0) {
        self.kind = kind
        self.timestamp = timestamp
    }
}

public struct InputEventContext: Equatable, Sendable {
    public let frontmostBundleId: String?
    public let isSecureInputActive: Bool
    public let isRecordingShortcut: Bool

    public init(
        frontmostBundleId: String? = nil,
        isSecureInputActive: Bool = false,
        isRecordingShortcut: Bool = false
    ) {
        self.frontmostBundleId = frontmostBundleId
        self.isSecureInputActive = isSecureInputActive
        self.isRecordingShortcut = isRecordingShortcut
    }
}

public enum InputRoutingDecision: Equatable, Sendable {
    case passThrough
    case consume
}

public struct EventTapRequirement: OptionSet, Sendable, Hashable {
    public let rawValue: UInt8

    public static let flagsChanged = EventTapRequirement(rawValue: 1 << 0)
    public static let keyDown = EventTapRequirement(rawValue: 1 << 1)
    public static let keyUp = EventTapRequirement(rawValue: 1 << 2)
    public static let mouseMoved = EventTapRequirement(rawValue: 1 << 3)
    public static let leftMouse = EventTapRequirement(rawValue: 1 << 4)

    public init(rawValue: UInt8) {
        self.rawValue = rawValue
    }

    public var cgEventMask: CGEventMask {
        var mask: CGEventMask = 0
        if contains(.flagsChanged) {
            mask |= (1 << CGEventType.flagsChanged.rawValue)
        }
        if contains(.keyDown) {
            mask |= (1 << CGEventType.keyDown.rawValue)
        }
        if contains(.keyUp) {
            mask |= (1 << CGEventType.keyUp.rawValue)
        }
        if contains(.mouseMoved) {
            mask |= (1 << CGEventType.mouseMoved.rawValue)
        }
        if contains(.leftMouse) {
            mask |= (1 << CGEventType.leftMouseDown.rawValue)
            mask |= (1 << CGEventType.leftMouseDragged.rawValue)
            mask |= (1 << CGEventType.leftMouseUp.rawValue)
        }
        return mask
    }
}

public protocol DesktopInputConsumer: AnyObject, Sendable {
    var slot: DesktopFeatureSlot { get }
    var priority: Int { get }
    var requiredEvents: EventTapRequirement { get }
    var isActive: Bool { get }
    func handle(_ event: RoutedInputEvent, context: InputEventContext) -> InputRoutingDecision
    func handleTapReset()
}

public enum EventTapBackendStatus: Equatable, Sendable {
    case inactive
    case active
    case disabledByEnvironment
    case pausedMissingPermission
    case creationFailed
}

public enum ShortcutConflictKind: String, Equatable, Sendable {
    case internalDuplicate
    case carbonRegistrationFailed
    case systemSymbolicHotKey
    case macOSNativeTilingOverlap
}

public struct ShortcutConflict: Equatable, Sendable {
    public let owner: DesktopFeatureSlot
    public let kind: ShortcutConflictKind
    public let detail: String

    public init(owner: DesktopFeatureSlot, kind: ShortcutConflictKind, detail: String) {
        self.owner = owner
        self.kind = kind
        self.detail = detail
    }
}
