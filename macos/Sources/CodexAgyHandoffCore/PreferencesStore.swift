import AppKit
import Carbon.HIToolbox
import Foundation
import ServiceManagement

public enum MenuBarIconStyle: String, Codable, CaseIterable, Sendable, Identifiable {
    case relayBridge = "relay_bridge"
    case pulseBolt = "pulse_bolt"
    case twinArrows = "twin_arrows"
    case syncOrbit = "sync_orbit"

    public var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .relayBridge:
            return "接力双轨"
        case .pulseBolt:
            return "切端闪电"
        case .twinArrows:
            return "双向穿梭"
        case .syncOrbit:
            return "同频双环"
        }
    }

    public var subtitle: String {
        switch self {
        case .relayBridge:
            return "双竖轨与S弯接力桥"
        case .pulseBolt:
            return "极速切端与无缝续接"
        case .twinArrows:
            return "双端互导对向箭头"
        case .syncOrbit:
            return "双宿主交叠同频环"
        }
    }

    public var accessibilityDescription: String {
        "对话接力（\(displayName)）"
    }
}

public enum HotKeyBehavior: String, Codable, CaseIterable, Sendable, Identifiable {
    case togglePanelOnly = "toggle_panel"
    case instantPrimaryRescue = "instant_rescue"

    public var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .togglePanelOnly:
            return "仅展开面板"
        case .instantPrimaryRescue:
            return "展开并立即接力首卡"
        }
    }
}

public struct HotKeyShortcut: Codable, Equatable, Sendable {
    public static let carbonCmdKey = UInt32(cmdKey)
    public static let carbonShiftKey = UInt32(shiftKey)
    public static let carbonOptionKey = UInt32(optionKey)
    public static let carbonControlKey = UInt32(controlKey)

    public var isEnabled: Bool
    public var keyCode: UInt32
    public var carbonModifiers: UInt32
    public var displayString: String

    public static let defaultShortcut = HotKeyShortcut(
        isEnabled: true,
        keyCode: UInt32(kVK_ANSI_H),
        carbonModifiers: UInt32(controlKey | optionKey),
        displayString: "⌃⌥H"
    )

    public init(
        isEnabled: Bool = true,
        keyCode: UInt32 = UInt32(kVK_ANSI_H),
        carbonModifiers: UInt32 = UInt32(controlKey | optionKey),
        displayString: String = "⌃⌥H"
    ) {
        self.isEnabled = isEnabled
        self.keyCode = keyCode
        self.carbonModifiers = carbonModifiers
        self.displayString = displayString
    }

    public static func carbonModifiers(from flags: NSEvent.ModifierFlags) -> UInt32 {
        var mask: UInt32 = 0
        if flags.contains(.control) { mask |= carbonControlKey }
        if flags.contains(.option) { mask |= carbonOptionKey }
        if flags.contains(.shift) { mask |= carbonShiftKey }
        if flags.contains(.command) { mask |= carbonCmdKey }
        return mask
    }

    public static func modifierPrefix(for carbonModifiers: UInt32) -> String {
        var out = ""
        if (carbonModifiers & carbonControlKey) != 0 { out.append("⌃") }
        if (carbonModifiers & carbonOptionKey) != 0 { out.append("⌥") }
        if (carbonModifiers & carbonShiftKey) != 0 { out.append("⇧") }
        if (carbonModifiers & carbonCmdKey) != 0 { out.append("⌘") }
        return out
    }

    public static func keyLabel(keyCode: UInt16, charactersIgnoringModifiers: String? = nil) -> String? {
        if modifierOnlyKeyCodes.contains(Int(keyCode)) {
            return nil
        }
        if let special = specialKeyLabels[Int(keyCode)] {
            return special
        }
        if let ansi = ansiKeyLabels[Int(keyCode)] {
            return ansi
        }
        if let chars = charactersIgnoringModifiers?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .uppercased(),
           chars.count == 1
        {
            return chars
        }
        return nil
    }

    public static func from(
        keyCode: UInt16,
        modifierFlags: NSEvent.ModifierFlags,
        charactersIgnoringModifiers: String? = nil,
        isEnabled: Bool = true
    ) -> HotKeyShortcut? {
        let relevant = modifierFlags.intersection([.control, .option, .shift, .command])
        let hasPrimaryModifier = !relevant.intersection([.control, .option, .command]).isEmpty
        guard hasPrimaryModifier else { return nil }
        guard let label = keyLabel(keyCode: keyCode, charactersIgnoringModifiers: charactersIgnoringModifiers) else {
            return nil
        }
        let carbon = carbonModifiers(from: relevant)
        let text = modifierPrefix(for: carbon) + label
        return HotKeyShortcut(
            isEnabled: isEnabled,
            keyCode: UInt32(keyCode),
            carbonModifiers: carbon,
            displayString: text
        )
    }

    private static let modifierOnlyKeyCodes: Set<Int> = [
        kVK_Command, kVK_RightCommand,
        kVK_Shift, kVK_RightShift,
        kVK_Option, kVK_RightOption,
        kVK_Control, kVK_RightControl,
        kVK_CapsLock, kVK_Function,
    ]

    private static let specialKeyLabels: [Int: String] = [
        kVK_Space: "Space",
        kVK_Return: "↩",
        kVK_Tab: "⇥",
        kVK_Delete: "⌫",
        kVK_ForwardDelete: "⌦",
        kVK_LeftArrow: "←",
        kVK_RightArrow: "→",
        kVK_UpArrow: "↑",
        kVK_DownArrow: "↓",
        kVK_F1: "F1", kVK_F2: "F2", kVK_F3: "F3", kVK_F4: "F4",
        kVK_F5: "F5", kVK_F6: "F6", kVK_F7: "F7", kVK_F8: "F8",
        kVK_F9: "F9", kVK_F10: "F10", kVK_F11: "F11", kVK_F12: "F12",
    ]

    private static let ansiKeyLabels: [Int: String] = [
        kVK_ANSI_A: "A", kVK_ANSI_B: "B", kVK_ANSI_C: "C", kVK_ANSI_D: "D",
        kVK_ANSI_E: "E", kVK_ANSI_F: "F", kVK_ANSI_G: "G", kVK_ANSI_H: "H",
        kVK_ANSI_I: "I", kVK_ANSI_J: "J", kVK_ANSI_K: "K", kVK_ANSI_L: "L",
        kVK_ANSI_M: "M", kVK_ANSI_N: "N", kVK_ANSI_O: "O", kVK_ANSI_P: "P",
        kVK_ANSI_Q: "Q", kVK_ANSI_R: "R", kVK_ANSI_S: "S", kVK_ANSI_T: "T",
        kVK_ANSI_U: "U", kVK_ANSI_V: "V", kVK_ANSI_W: "W", kVK_ANSI_X: "X",
        kVK_ANSI_Y: "Y", kVK_ANSI_Z: "Z",
        kVK_ANSI_0: "0", kVK_ANSI_1: "1", kVK_ANSI_2: "2", kVK_ANSI_3: "3",
        kVK_ANSI_4: "4", kVK_ANSI_5: "5", kVK_ANSI_6: "6", kVK_ANSI_7: "7",
        kVK_ANSI_8: "8", kVK_ANSI_9: "9",
        kVK_ANSI_Grave: "`", kVK_ANSI_Minus: "-", kVK_ANSI_Equal: "=",
        kVK_ANSI_LeftBracket: "[", kVK_ANSI_RightBracket: "]",
        kVK_ANSI_Backslash: "\\", kVK_ANSI_Semicolon: ";", kVK_ANSI_Quote: "'",
        kVK_ANSI_Comma: ",", kVK_ANSI_Period: ".", kVK_ANSI_Slash: "/",
    ]
}

public struct AppPreferences: Codable, Equatable, Sendable {
    public static let supportedFeedLimits: [Int] = [4, 6, 8]

    public var menuBarIconStyle: MenuBarIconStyle
    public var showAttentionDotOnStatusIcon: Bool
    public var hotKey: HotKeyShortcut
    public var hotKeyBehavior: HotKeyBehavior
    public var activateTargetAppAfterHandoff: Bool
    public var recentFeedLimit: Int
    public var customCLIPath: String?
    public var localRepoPathForSync: String?

    public init(
        menuBarIconStyle: MenuBarIconStyle = .relayBridge,
        showAttentionDotOnStatusIcon: Bool = true,
        hotKey: HotKeyShortcut = .defaultShortcut,
        hotKeyBehavior: HotKeyBehavior = .instantPrimaryRescue,
        activateTargetAppAfterHandoff: Bool = true,
        recentFeedLimit: Int = 6,
        customCLIPath: String? = nil,
        localRepoPathForSync: String? = nil
    ) {
        self.menuBarIconStyle = menuBarIconStyle
        self.showAttentionDotOnStatusIcon = showAttentionDotOnStatusIcon
        self.hotKey = hotKey
        self.hotKeyBehavior = hotKeyBehavior
        self.activateTargetAppAfterHandoff = activateTargetAppAfterHandoff
        self.recentFeedLimit = Self.clampFeedLimit(recentFeedLimit)
        self.customCLIPath = Self.normalizedOptionalPath(customCLIPath)
        self.localRepoPathForSync = Self.normalizedOptionalPath(localRepoPathForSync)
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.menuBarIconStyle = try container.decodeIfPresent(MenuBarIconStyle.self, forKey: .menuBarIconStyle) ?? .relayBridge
        self.showAttentionDotOnStatusIcon = try container.decodeIfPresent(Bool.self, forKey: .showAttentionDotOnStatusIcon) ?? true
        self.hotKey = try container.decodeIfPresent(HotKeyShortcut.self, forKey: .hotKey) ?? .defaultShortcut
        self.hotKeyBehavior = try container.decodeIfPresent(HotKeyBehavior.self, forKey: .hotKeyBehavior) ?? .instantPrimaryRescue
        self.activateTargetAppAfterHandoff = try container.decodeIfPresent(Bool.self, forKey: .activateTargetAppAfterHandoff) ?? true
        let rawLimit = try container.decodeIfPresent(Int.self, forKey: .recentFeedLimit) ?? 6
        self.recentFeedLimit = Self.clampFeedLimit(rawLimit)
        let rawCLI = try container.decodeIfPresent(String.self, forKey: .customCLIPath)
        self.customCLIPath = Self.normalizedOptionalPath(rawCLI)
        let rawRepo = try container.decodeIfPresent(String.self, forKey: .localRepoPathForSync)
        self.localRepoPathForSync = Self.normalizedOptionalPath(rawRepo)
    }

    private static func clampFeedLimit(_ value: Int) -> Int {
        min(max(value, 1), 50)
    }

    private static func normalizedOptionalPath(_ raw: String?) -> String? {
        guard let raw else { return nil }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return (trimmed as NSString).expandingTildeInPath
    }
}

public protocol LoginItemManaging: Sendable {
    var isEnabled: Bool { get }
    func setEnabled(_ enabled: Bool) throws
}

public final class SMAppServiceLoginItemManager: LoginItemManaging, @unchecked Sendable {
    public init() {}

    public var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    public func setEnabled(_ enabled: Bool) throws {
        if enabled {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
    }
}

public final class NoOpLoginItemManager: LoginItemManaging, @unchecked Sendable {
    private let lock = NSLock()
    private var enabledState: Bool

    public init(initiallyEnabled: Bool = false) {
        self.enabledState = initiallyEnabled
    }

    public var isEnabled: Bool {
        lock.withLock { enabledState }
    }

    public func setEnabled(_ enabled: Bool) throws {
        lock.withLock {
            enabledState = enabled
        }
    }
}

@MainActor
public final class PreferencesStore: ObservableObject {
    public nonisolated static let preferencesDefaultsKey = "appPreferences"
    public nonisolated static let menuBarIconStyleDefaultsKey = "menuBarIconStyle"
    public nonisolated static let prefsSuiteEnvKey = "MENUBAR_PREFS_SUITE"
    public nonisolated static let disableLoginItemEnvKey = "MENUBAR_DISABLE_LOGIN_ITEM"

    @Published public private(set) var preferences: AppPreferences
    @Published public private(set) var launchAtLogin: Bool
    @Published public private(set) var loginItemError: String?

    public let defaults: UserDefaults
    private let loginItemManager: any LoginItemManaging

    public nonisolated static func resolveDefaults(
        suiteName: String? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> UserDefaults {
        let candidate = suiteName ?? environment[prefsSuiteEnvKey]
        if let candidate {
            let trimmed = candidate.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty, let custom = UserDefaults(suiteName: trimmed) {
                return custom
            }
        }
        return .standard
    }

    public nonisolated static func defaultLoginItemManager(
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> any LoginItemManaging {
        if environment[disableLoginItemEnvKey] == "1" {
            return NoOpLoginItemManager()
        }
        return SMAppServiceLoginItemManager()
    }

    public init(
        defaults: UserDefaults? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        loginItemManager: (any LoginItemManaging)? = nil
    ) {
        let resolvedDefaults = defaults ?? Self.resolveDefaults(environment: environment)
        let resolvedLoginManager = loginItemManager ?? Self.defaultLoginItemManager(environment: environment)
        self.defaults = resolvedDefaults
        self.loginItemManager = resolvedLoginManager

        var loaded = Self.loadPreferences(from: resolvedDefaults)
        if loaded.customCLIPath == nil,
           let legacyCLI = resolvedDefaults.string(forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
        {
            let trimmed = legacyCLI.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                loaded.customCLIPath = (trimmed as NSString).expandingTildeInPath
            }
        }

        self.preferences = loaded
        self.launchAtLogin = resolvedLoginManager.isEnabled
        self.loginItemError = nil
        persist(loaded)
    }

    public func update(_ transform: (inout AppPreferences) -> Void) {
        var next = preferences
        transform(&next)
        next = AppPreferences(
            menuBarIconStyle: next.menuBarIconStyle,
            showAttentionDotOnStatusIcon: next.showAttentionDotOnStatusIcon,
            hotKey: next.hotKey,
            hotKeyBehavior: next.hotKeyBehavior,
            activateTargetAppAfterHandoff: next.activateTargetAppAfterHandoff,
            recentFeedLimit: next.recentFeedLimit,
            customCLIPath: next.customCLIPath,
            localRepoPathForSync: next.localRepoPathForSync
        )
        guard next != preferences else { return }
        preferences = next
        persist(next)
    }

    public func setMenuBarIconStyle(_ style: MenuBarIconStyle) {
        update { $0.menuBarIconStyle = style }
    }

    public func setShowAttentionDot(_ enabled: Bool) {
        update { $0.showAttentionDotOnStatusIcon = enabled }
    }

    public func setHotKey(_ shortcut: HotKeyShortcut) {
        update { $0.hotKey = shortcut }
    }

    public func resetHotKeyToDefault() {
        update { $0.hotKey = .defaultShortcut }
    }

    public func setHotKeyBehavior(_ behavior: HotKeyBehavior) {
        update { $0.hotKeyBehavior = behavior }
    }

    public func setActivateTargetAppAfterHandoff(_ enabled: Bool) {
        update { $0.activateTargetAppAfterHandoff = enabled }
    }

    public func setRecentFeedLimit(_ limit: Int) {
        update { $0.recentFeedLimit = limit }
    }

    public func setCustomCLIPath(_ path: String?) {
        update { $0.customCLIPath = path }
    }

    public func resetCustomCLIPath() {
        update { $0.customCLIPath = nil }
        defaults.removeObject(forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
    }

    public func setLocalRepoPathForSync(_ path: String?) {
        update { $0.localRepoPathForSync = path }
    }

    public func setLaunchAtLogin(_ enabled: Bool) {
        do {
            try loginItemManager.setEnabled(enabled)
            launchAtLogin = loginItemManager.isEnabled
            loginItemError = nil
        } catch {
            launchAtLogin = loginItemManager.isEnabled
            loginItemError = error.localizedDescription
        }
    }

    public func refreshLaunchAtLoginStatus() {
        launchAtLogin = loginItemManager.isEnabled
    }

    private static func loadPreferences(from defaults: UserDefaults) -> AppPreferences {
        if let dict = defaults.dictionary(forKey: preferencesDefaultsKey),
           let data = try? JSONSerialization.data(withJSONObject: dict),
           let decoded = try? JSONDecoder().decode(AppPreferences.self, from: data)
        {
            return decoded
        }
        if let rawData = defaults.data(forKey: preferencesDefaultsKey),
           let decoded = try? JSONDecoder().decode(AppPreferences.self, from: rawData)
        {
            return decoded
        }
        return AppPreferences()
    }

    private func persist(_ value: AppPreferences) {
        if let encoded = try? JSONEncoder().encode(value),
           let jsonObject = try? JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        {
            defaults.set(jsonObject, forKey: Self.preferencesDefaultsKey)
        }
        defaults.set(value.menuBarIconStyle.rawValue, forKey: Self.menuBarIconStyleDefaultsKey)
        if let cliPath = value.customCLIPath, !cliPath.isEmpty {
            defaults.set(cliPath, forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
        } else {
            defaults.removeObject(forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
        }
    }
}
