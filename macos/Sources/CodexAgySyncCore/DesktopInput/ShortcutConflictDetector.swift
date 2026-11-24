import Carbon
import CoreGraphics
import Foundation

public struct ShortcutBindingDescriptor: Equatable, Sendable {
    public let owner: DesktopFeatureSlot
    public let label: String
    public let keyCode: UInt16
    public let modifiers: InputModifierSet

    public init(
        owner: DesktopFeatureSlot,
        label: String,
        keyCode: UInt16,
        modifiers: InputModifierSet
    ) {
        self.owner = owner
        self.label = label
        self.keyCode = keyCode
        self.modifiers = modifiers
    }
}

public struct SystemSymbolicHotKey: Equatable, Sendable {
    public let symbolicId: Int
    public let keyCode: UInt16
    public let modifiers: InputModifierSet
    public let enabled: Bool

    public init(
        symbolicId: Int,
        keyCode: UInt16,
        modifiers: InputModifierSet,
        enabled: Bool
    ) {
        self.symbolicId = symbolicId
        self.keyCode = keyCode
        self.modifiers = modifiers
        self.enabled = enabled
    }
}

public protocol SystemSymbolicHotKeyProviding: Sendable {
    func fetchSymbolicHotKeys() -> [SystemSymbolicHotKey]
}

public struct CarbonSymbolicHotKeyProvider: SystemSymbolicHotKeyProviding, Sendable {
    private static let codeKey = "kHISymbolicHotKeyCode"
    private static let modifiersKey = "kHISymbolicHotKeyModifiers"
    private static let enabledKey = "kHISymbolicHotKeyEnabled"
    private static let idKey = "kHISymbolicHotKeyID"

    public init() {}

    public func fetchSymbolicHotKeys() -> [SystemSymbolicHotKey] {
        var unmanagedArray: Unmanaged<CFArray>?
        let status = CopySymbolicHotKeys(&unmanagedArray)
        guard status == noErr, let cfArray = unmanagedArray?.takeRetainedValue() else {
            return []
        }
        guard let entries = cfArray as? [[String: Any]] else {
            return []
        }
        var result: [SystemSymbolicHotKey] = []
        result.reserveCapacity(entries.count)
        for (index, entry) in entries.enumerated() {
            guard let rawCode = (entry[Self.codeKey] as? NSNumber)?.intValue,
                  rawCode >= 0,
                  rawCode < Int(UInt16.max),
                  let rawModifiers = (entry[Self.modifiersKey] as? NSNumber)?.uint64Value
            else {
                continue
            }
            let enabled = (entry[Self.enabledKey] as? NSNumber)?.boolValue ?? false
            let symbolicId = (entry[Self.idKey] as? NSNumber)?.intValue ?? index
            let modifiers = InputModifierSet(cgEventFlags: CGEventFlags(rawValue: rawModifiers))
            result.append(
                SystemSymbolicHotKey(
                    symbolicId: symbolicId,
                    keyCode: UInt16(rawCode),
                    modifiers: modifiers,
                    enabled: enabled
                )
            )
        }
        return result
    }
}

public enum ShortcutConflictDetector {
    private struct ShortcutComboKey: Hashable {
        let keyCode: UInt16
        let modifiers: InputModifierSet
    }

    private static let nativeTilingPrimaryKeyCodes: Set<UInt16> = [
        UInt16(kVK_LeftArrow),
        UInt16(kVK_RightArrow),
        UInt16(kVK_UpArrow),
        UInt16(kVK_DownArrow),
        UInt16(kVK_Return),
        UInt16(kVK_ANSI_C),
        UInt16(kVK_ANSI_F),
        UInt16(kVK_ANSI_R),
    ]

    private static let nativeTilingShiftedKeyCodes: Set<UInt16> = [
        UInt16(kVK_LeftArrow),
        UInt16(kVK_RightArrow),
        UInt16(kVK_UpArrow),
        UInt16(kVK_DownArrow),
    ]

    public static func detect(
        bindings: [ShortcutBindingDescriptor],
        failedCarbonSlots: Set<DesktopFeatureSlot> = [],
        symbolicHotKeys: [SystemSymbolicHotKey] = [],
        checkNativeTilingOverlap: Bool = false
    ) -> [ShortcutConflict] {
        var conflicts: [ShortcutConflict] = []

        var groupedByCombo: [ShortcutComboKey: [ShortcutBindingDescriptor]] = [:]
        var comboOrder: [ShortcutComboKey] = []
        for binding in bindings {
            let key = ShortcutComboKey(keyCode: binding.keyCode, modifiers: binding.modifiers)
            if groupedByCombo[key] == nil {
                comboOrder.append(key)
            }
            groupedByCombo[key, default: []].append(binding)
        }

        for key in comboOrder {
            guard let group = groupedByCombo[key], group.count > 1 else { continue }
            for (index, binding) in group.enumerated() {
                let peers = group.enumerated()
                    .filter { $0.offset != index }
                    .map { "\($0.element.owner.rawValue):\($0.element.label)" }
                    .joined(separator: ", ")
                conflicts.append(
                    ShortcutConflict(
                        owner: binding.owner,
                        kind: .internalDuplicate,
                        detail: "\(binding.label) <-> \(peers)"
                    )
                )
            }
        }

        let sortedFailedSlots = failedCarbonSlots.sorted { $0.rawValue < $1.rawValue }
        for slot in sortedFailedSlots {
            let label = bindings.first(where: { $0.owner == slot })?.label ?? slot.rawValue
            conflicts.append(
                ShortcutConflict(
                    owner: slot,
                    kind: .carbonRegistrationFailed,
                    detail: label
                )
            )
        }

        let enabledSymbolicByCombo: [ShortcutComboKey: [SystemSymbolicHotKey]] = Dictionary(
            grouping: symbolicHotKeys.filter(\.enabled),
            by: { ShortcutComboKey(keyCode: $0.keyCode, modifiers: $0.modifiers) }
        )

        for binding in bindings {
            let key = ShortcutComboKey(keyCode: binding.keyCode, modifiers: binding.modifiers)
            if let matches = enabledSymbolicByCombo[key], let firstMatch = matches.first {
                conflicts.append(
                    ShortcutConflict(
                        owner: binding.owner,
                        kind: .systemSymbolicHotKey,
                        detail: "\(binding.label) (#\(firstMatch.symbolicId))"
                    )
                )
            }
            if checkNativeTilingOverlap, overlapsMacOSNativeTiling(binding) {
                conflicts.append(
                    ShortcutConflict(
                        owner: binding.owner,
                        kind: .macOSNativeTilingOverlap,
                        detail: binding.label
                    )
                )
            }
        }

        return conflicts
    }

    private static func overlapsMacOSNativeTiling(_ binding: ShortcutBindingDescriptor) -> Bool {
        if binding.modifiers == [.fn, .control] {
            return nativeTilingPrimaryKeyCodes.contains(binding.keyCode)
        }
        if binding.modifiers == [.fn, .control, .shift] {
            return nativeTilingShiftedKeyCodes.contains(binding.keyCode)
        }
        return false
    }
}
