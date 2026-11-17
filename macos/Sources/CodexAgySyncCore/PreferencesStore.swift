import AppKit
import Carbon.HIToolbox
import CoreFoundation
import Foundation
import Observation
import os
import ServiceManagement

public enum AutoUpdatePolicy: String, Codable, CaseIterable, Sendable, Identifiable {
    case off = "off"
    case checkOnly = "check_only"
    case autoStage = "auto_stage"

    public var id: String { rawValue }

    public func displayName(in language: ResolvedLanguage) -> String {
        switch self {
        case .off:
            return AppCopy.text(.updatesPolicyOffLabel, in: language)
        case .checkOnly:
            return AppCopy.text(.updatesPolicyCheckOnlyLabel, in: language)
        case .autoStage:
            return AppCopy.text(.updatesPolicyAutoStageLabel, in: language)
        }
    }

    public func helperDescription(in language: ResolvedLanguage) -> String {
        switch self {
        case .off:
            return AppCopy.text(.updatesPolicyOffHelper, in: language)
        case .checkOnly:
            return AppCopy.text(.updatesPolicyCheckOnlyHelper, in: language)
        case .autoStage:
            return AppCopy.text(.updatesPolicyAutoStageHelper, in: language)
        }
    }
}

public enum HotKeyBehavior: String, Codable, CaseIterable, Sendable, Identifiable {
    case togglePanelOnly = "toggle_panel"
    case instantPrimaryRescue = "instant_rescue"

    public var id: String { rawValue }

    public func displayName(in language: ResolvedLanguage) -> String {
        switch self {
        case .togglePanelOnly:
            return AppCopy.text(.hotKeyTogglePanelLabel, in: language)
        case .instantPrimaryRescue:
            return AppCopy.text(.hotKeyInstantContinueLabel, in: language)
        }
    }

    public func helperDescription(in language: ResolvedLanguage) -> String {
        switch self {
        case .togglePanelOnly:
            return AppCopy.text(.hotKeyTogglePanelHelper, in: language)
        case .instantPrimaryRescue:
            return AppCopy.text(.hotKeyInstantContinueHelper, in: language)
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

    public static func normalizedEventFlags(_ flags: NSEvent.ModifierFlags) -> NSEvent.ModifierFlags {
        flags
            .intersection(.deviceIndependentFlagsMask)
            .subtracting([.capsLock, .numericPad, .function])
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
    private static let legacyActivateTargetKey = "activateTargetAppAfter" + "Hand" + "off"

    private struct DynamicCodingKey: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
        static func named(_ value: String) -> Self { Self(stringValue: value)! }
    }

    private enum CodingKeys: String, CodingKey {
        case showAttentionDotOnStatusIcon
        case hotKey
        case hotKeyBehavior
        case activateTargetAppAfterSync
        case recentFeedLimit
        case customCLIPath
        case localRepoPathForSync
        case language
        case autoUpdatePolicy
    }

    public var showAttentionDotOnStatusIcon: Bool
    public var hotKey: HotKeyShortcut
    public var hotKeyBehavior: HotKeyBehavior
    public var activateTargetAppAfterSync: Bool
    public var recentFeedLimit: Int
    public var customCLIPath: String?
    public var localRepoPathForSync: String?
    public var language: AppLanguage
    public var autoUpdatePolicy: AutoUpdatePolicy

    public init(
        showAttentionDotOnStatusIcon: Bool = true,
        hotKey: HotKeyShortcut = .defaultShortcut,
        hotKeyBehavior: HotKeyBehavior = .instantPrimaryRescue,
        activateTargetAppAfterSync: Bool = true,
        recentFeedLimit: Int = 6,
        customCLIPath: String? = nil,
        localRepoPathForSync: String? = nil,
        language: AppLanguage = .zhHans,
        autoUpdatePolicy: AutoUpdatePolicy = .checkOnly
    ) {
        self.showAttentionDotOnStatusIcon = showAttentionDotOnStatusIcon
        self.hotKey = hotKey
        self.hotKeyBehavior = hotKeyBehavior
        self.activateTargetAppAfterSync = activateTargetAppAfterSync
        self.recentFeedLimit = Self.clampFeedLimit(recentFeedLimit)
        self.customCLIPath = Self.normalizedOptionalPath(customCLIPath)
        self.localRepoPathForSync = Self.normalizedOptionalPath(localRepoPathForSync)
        self.language = language
        self.autoUpdatePolicy = autoUpdatePolicy
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let dynamic = try decoder.container(keyedBy: DynamicCodingKey.self)
        self.showAttentionDotOnStatusIcon = try container.decodeIfPresent(Bool.self, forKey: .showAttentionDotOnStatusIcon) ?? true
        self.hotKey = try container.decodeIfPresent(HotKeyShortcut.self, forKey: .hotKey) ?? .defaultShortcut
        let rawHotKeyBehavior = try container.decodeIfPresent(String.self, forKey: .hotKeyBehavior)
        self.hotKeyBehavior = rawHotKeyBehavior.flatMap(HotKeyBehavior.init(rawValue:)) ?? .instantPrimaryRescue
        let decodedActivate = try container.decodeIfPresent(Bool.self, forKey: .activateTargetAppAfterSync)
            ?? dynamic.decodeIfPresent(Bool.self, forKey: .named(Self.legacyActivateTargetKey))
        self.activateTargetAppAfterSync = decodedActivate ?? true
        let rawLimit = try container.decodeIfPresent(Int.self, forKey: .recentFeedLimit) ?? 6
        self.recentFeedLimit = Self.clampFeedLimit(rawLimit)
        let rawCLI = try container.decodeIfPresent(String.self, forKey: .customCLIPath)
        self.customCLIPath = Self.normalizedOptionalPath(rawCLI)
        let rawRepo = try container.decodeIfPresent(String.self, forKey: .localRepoPathForSync)
        self.localRepoPathForSync = Self.normalizedOptionalPath(rawRepo)
        let rawLanguage = try container.decodeIfPresent(String.self, forKey: .language)
        self.language = rawLanguage.flatMap(AppLanguage.init(rawValue:)) ?? .zhHans
        let rawAutoUpdatePolicy = try container.decodeIfPresent(String.self, forKey: .autoUpdatePolicy)
        self.autoUpdatePolicy = rawAutoUpdatePolicy.flatMap(AutoUpdatePolicy.init(rawValue:)) ?? .checkOnly
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(showAttentionDotOnStatusIcon, forKey: .showAttentionDotOnStatusIcon)
        try container.encode(hotKey, forKey: .hotKey)
        try container.encode(hotKeyBehavior, forKey: .hotKeyBehavior)
        try container.encode(activateTargetAppAfterSync, forKey: .activateTargetAppAfterSync)
        try container.encode(recentFeedLimit, forKey: .recentFeedLimit)
        try container.encodeIfPresent(customCLIPath, forKey: .customCLIPath)
        try container.encodeIfPresent(localRepoPathForSync, forKey: .localRepoPathForSync)
        try container.encode(language, forKey: .language)
        try container.encode(autoUpdatePolicy, forKey: .autoUpdatePolicy)
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

public struct SMAppServiceLoginItemManager: LoginItemManaging, Sendable {
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

public final class NoOpLoginItemManager: LoginItemManaging, Sendable {
    private let enabledState: OSAllocatedUnfairLock<Bool>

    public init(initiallyEnabled: Bool = false) {
        self.enabledState = OSAllocatedUnfairLock(initialState: initiallyEnabled)
    }

    public var isEnabled: Bool {
        enabledState.withLock { $0 }
    }

    public func setEnabled(_ enabled: Bool) throws {
        enabledState.withLock {
            $0 = enabled
        }
    }
}

@MainActor
@Observable
public final class PreferencesStore {
    public nonisolated static let preferencesDefaultsKey = "appPreferences"
    public nonisolated static let prefsSuiteEnvKey = "MENUBAR_PREFS_SUITE"
    public nonisolated static let disableLoginItemEnvKey = "MENUBAR_DISABLE_LOGIN_ITEM"
    public nonisolated static let appBundleIdentifier = "dev.semanticcraft.codex-agy-sync"
    public nonisolated static let legacyAppName = ["Codex", "Agy", "Hand" + "off"].joined()
    public nonisolated static let legacyPreferencesSuiteName =
        "dev.semanticcraft." + ["codex", "agy", "hand" + "off"].joined(separator: "-")

    public nonisolated static var isProductionAppBundleProcess: Bool {
        Bundle.main.bundleIdentifier == appBundleIdentifier
            && NSClassFromString("XCTestCase") == nil
            && ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil
    }

    public private(set) var preferences: AppPreferences
    public private(set) var effectiveLanguage: ResolvedLanguage
    public private(set) var launchAtLogin: Bool
    public private(set) var loginItemError: String?
    public private(set) var isRecordingShortcut: Bool = false
    public private(set) var hotKeyRegistrationFailed: Bool = false

    public let defaults: UserDefaults
    public let usesHostStandardDefaults: Bool
    private let skipsImplicitStandardPersistence: Bool
    private let loginItemManager: any LoginItemManaging
    private let preferredLanguagesProvider: @Sendable () -> [String]
    @ObservationIgnored private nonisolated let localeTaskLock = OSAllocatedUnfairLock<Task<Void, Never>?>(initialState: nil)

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
        legacyDefaults: UserDefaults? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        loginItemManager: (any LoginItemManaging)? = nil,
        preferredLanguagesProvider: @escaping @Sendable () -> [String] = { Locale.preferredLanguages }
    ) {
        let resolvedDefaults = defaults ?? Self.resolveDefaults(environment: environment)
        let resolvedLoginManager = loginItemManager ?? Self.defaultLoginItemManager(environment: environment)
        self.defaults = resolvedDefaults
        self.loginItemManager = resolvedLoginManager
        self.preferredLanguagesProvider = preferredLanguagesProvider

        let explicitSuite = (environment[Self.prefsSuiteEnvKey] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let isImplicitStandard = (defaults == nil && explicitSuite.isEmpty)
        let usesHostStandardDefaults = (isImplicitStandard && legacyDefaults == nil && Self.isProductionAppBundleProcess)
        let skipsImplicitStandardPersistence = (isImplicitStandard && !Self.isProductionAppBundleProcess)
        self.usesHostStandardDefaults = usesHostStandardDefaults
        self.skipsImplicitStandardPersistence = skipsImplicitStandardPersistence

        let fallbackDefaults: UserDefaults?
        if let legacyDefaults {
            fallbackDefaults = legacyDefaults
        } else if usesHostStandardDefaults {
            fallbackDefaults = UserDefaults(suiteName: Self.legacyPreferencesSuiteName)
        } else {
            fallbackDefaults = nil
        }

        let loaded: AppPreferences
        if skipsImplicitStandardPersistence, legacyDefaults == nil {
            loaded = AppPreferences()
        } else {
            loaded = Self.loadPreferencesWithLegacyFallback(
                from: resolvedDefaults,
                legacyDefaults: fallbackDefaults
            )
        }

        self.preferences = loaded
        self.effectiveLanguage = loaded.language.resolve(preferredLanguages: preferredLanguagesProvider())
        self.launchAtLogin = resolvedLoginManager.isEnabled
        self.loginItemError = nil
        persist(loaded)
        if usesHostStandardDefaults {
            _ = CFPreferencesAppSynchronize(Self.appBundleIdentifier as CFString)
            _ = CFPreferencesSynchronize(
                Self.appBundleIdentifier as CFString,
                kCFPreferencesCurrentUser,
                kCFPreferencesAnyHost
            )
            UpdatePathGuard.cleanupLegacyUpdateResidue(removeLegacyPreferencesDomains: true)
        }

        let task = Task { @MainActor [weak self] in
            for await _ in NotificationCenter.default.notifications(named: NSLocale.currentLocaleDidChangeNotification) {
                guard !Task.isCancelled, let self else { break }
                self.refreshEffectiveLanguage()
            }
        }
        localeTaskLock.withLock { $0 = task }
    }

    deinit {
        localeTaskLock.withLock {
            $0?.cancel()
            $0 = nil
        }
    }

    public func update(_ transform: (inout AppPreferences) -> Void) {
        var next = preferences
        transform(&next)
        next = AppPreferences(
            showAttentionDotOnStatusIcon: next.showAttentionDotOnStatusIcon,
            hotKey: next.hotKey,
            hotKeyBehavior: next.hotKeyBehavior,
            activateTargetAppAfterSync: next.activateTargetAppAfterSync,
            recentFeedLimit: next.recentFeedLimit,
            customCLIPath: next.customCLIPath,
            localRepoPathForSync: next.localRepoPathForSync,
            language: next.language,
            autoUpdatePolicy: next.autoUpdatePolicy
        )
        guard next != preferences else { return }
        preferences = next
        persist(next)
        refreshEffectiveLanguage()
    }

    public func refreshEffectiveLanguage() {
        let resolved = preferences.language.resolve(preferredLanguages: preferredLanguagesProvider())
        if effectiveLanguage != resolved {
            effectiveLanguage = resolved
        }
    }

    public func setLanguage(_ language: AppLanguage) {
        update { $0.language = language }
    }

    public func setAutoUpdatePolicy(_ policy: AutoUpdatePolicy) {
        update { $0.autoUpdatePolicy = policy }
    }

    public func setShowAttentionDot(_ enabled: Bool) {
        update { $0.showAttentionDotOnStatusIcon = enabled }
    }

    public func setHotKey(_ shortcut: HotKeyShortcut) {
        if !shortcut.isEnabled {
            isRecordingShortcut = false
            hotKeyRegistrationFailed = false
        }
        update { $0.hotKey = shortcut }
    }

    public func setShortcutRecording(_ recording: Bool) {
        guard isRecordingShortcut != recording else { return }
        isRecordingShortcut = recording
    }

    public func setHotKeyRegistrationFailed(_ failed: Bool) {
        guard hotKeyRegistrationFailed != failed else { return }
        hotKeyRegistrationFailed = failed
    }

    public func resetHotKeyToDefault() {
        update { $0.hotKey = .defaultShortcut }
    }

    public func setHotKeyBehavior(_ behavior: HotKeyBehavior) {
        update { $0.hotKeyBehavior = behavior }
    }

    public func setActivateTargetAppAfterSync(_ enabled: Bool) {
        update { $0.activateTargetAppAfterSync = enabled }
    }

    public func setRecentFeedLimit(_ limit: Int) {
        update { $0.recentFeedLimit = limit }
    }

    public func setCustomCLIPath(_ path: String?) {
        update { $0.customCLIPath = path }
    }

    public func resetCustomCLIPath() {
        update { $0.customCLIPath = nil }
        guard !skipsImplicitStandardPersistence else { return }
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

    static func loadPreferencesWithLegacyFallback(
        from primaryDefaults: UserDefaults,
        legacyDefaults: UserDefaults?
    ) -> AppPreferences {
        let hasPrimaryPrefs = primaryDefaults.object(forKey: preferencesDefaultsKey) != nil
        let hasPrimaryCLI = primaryDefaults.object(forKey: SubprocessCLIClient.cliExecutableDefaultsKey) != nil
        let sourceDefaults: UserDefaults
        if !hasPrimaryPrefs,
           !hasPrimaryCLI,
           let legacyDefaults,
           legacyDefaults.object(forKey: preferencesDefaultsKey) != nil
               || legacyDefaults.object(forKey: SubprocessCLIClient.cliExecutableDefaultsKey) != nil
        {
            sourceDefaults = legacyDefaults
        } else {
            sourceDefaults = primaryDefaults
        }

        var loaded = loadPreferences(from: sourceDefaults)
        if loaded.customCLIPath == nil,
           let legacyCLI = sourceDefaults.string(forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
        {
            let trimmed = legacyCLI.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                loaded.customCLIPath = (trimmed as NSString).expandingTildeInPath
            }
        }
        return loaded
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
        guard !skipsImplicitStandardPersistence else { return }
        if let encoded = try? JSONEncoder().encode(value),
           let jsonObject = try? JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        {
            defaults.set(jsonObject, forKey: Self.preferencesDefaultsKey)
        }
        if let cliPath = value.customCLIPath, !cliPath.isEmpty {
            defaults.set(cliPath, forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
        } else {
            defaults.removeObject(forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
        }
    }
}
