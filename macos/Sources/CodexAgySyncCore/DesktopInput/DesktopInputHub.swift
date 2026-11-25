import Foundation
import Observation

@MainActor
@Observable
public final class DesktopInputHub {
    public nonisolated static let disableGlobalHotKeyEnvKey = "MENUBAR_DISABLE_GLOBAL_HOTKEY"

    private struct CarbonSlotEntry {
        let slot: HotKeySlotID
        let featureSlot: DesktopFeatureSlot
        let label: String
        let shortcut: HotKeyShortcut
        let handler: @MainActor @Sendable () -> Void
    }

    public private(set) var permissionState: DesktopPermissionState
    public private(set) var eventTapStatus: EventTapBackendStatus = .inactive
    public private(set) var failedCarbonSlots: Set<HotKeySlotID> = []
    public private(set) var isRecordingShortcut: Bool = false
    public private(set) var isSuspendedForRelaunch: Bool = false
    public let isGlobalInputDisabledByEnvironment: Bool

    @ObservationIgnored private let carbonBackend: any CarbonHotKeyBackend
    @ObservationIgnored private let eventTapRouter: SharedEventTapRouter
    @ObservationIgnored private let permissionService: any DesktopPermissionChecking
    @ObservationIgnored private let symbolicHotKeyProvider: any SystemSymbolicHotKeyProviding
    @ObservationIgnored private var carbonSlots: [HotKeySlotID: CarbonSlotEntry] = [:]

    public var dispatcher: EventTapDispatcher {
        eventTapRouter.dispatcher
    }

    public init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        carbonBackend: (any CarbonHotKeyBackend)? = nil,
        eventTapTransport: (any EventTapTransport)? = nil,
        permissionService: (any DesktopPermissionChecking)? = nil,
        symbolicHotKeyProvider: (any SystemSymbolicHotKeyProviding)? = nil
    ) {
        let disabled = environment[Self.disableGlobalHotKeyEnvKey] == "1"
        let resolvedPermissionService = permissionService ?? SystemDesktopPermissionService()
        let resolvedCarbonBackend = carbonBackend ?? SystemCarbonHotKeyBackend()
        let resolvedTransport = eventTapTransport ?? SystemCGEventTapTransport()
        let resolvedSymbolicProvider = symbolicHotKeyProvider ?? CarbonSymbolicHotKeyProvider()

        self.isGlobalInputDisabledByEnvironment = disabled
        self.carbonBackend = resolvedCarbonBackend
        self.permissionService = resolvedPermissionService
        self.symbolicHotKeyProvider = resolvedSymbolicProvider
        self.eventTapRouter = SharedEventTapRouter(
            transport: resolvedTransport,
            permissionService: resolvedPermissionService,
            disabledByEnvironment: disabled
        )
        self.permissionState = resolvedPermissionService.currentState()
    }

    @discardableResult
    public func registerCarbonHotKey(
        slot: HotKeySlotID,
        featureSlot: DesktopFeatureSlot = .syncPanel,
        label: String? = nil,
        shortcut: HotKeyShortcut,
        handler: @escaping @MainActor @Sendable () -> Void
    ) -> Bool {
        let entry = CarbonSlotEntry(
            slot: slot,
            featureSlot: featureSlot,
            label: label ?? shortcut.displayString,
            shortcut: shortcut,
            handler: handler
        )
        carbonSlots[slot] = entry

        if isGlobalInputDisabledByEnvironment {
            carbonBackend.unregister(slot: slot)
            failedCarbonSlots.remove(slot)
            return true
        }

        if isRecordingShortcut || isSuspendedForRelaunch {
            carbonBackend.unregister(slot: slot)
            failedCarbonSlots.remove(slot)
            return true
        }

        return applyCarbonEntry(entry)
    }

    @discardableResult
    public func reconfigureCarbonHotKey(
        slot: HotKeySlotID,
        shortcut: HotKeyShortcut,
        label: String? = nil
    ) -> Bool {
        guard let existing = carbonSlots[slot] else {
            return false
        }
        return registerCarbonHotKey(
            slot: slot,
            featureSlot: existing.featureSlot,
            label: label ?? shortcut.displayString,
            shortcut: shortcut,
            handler: existing.handler
        )
    }

    public func unregisterCarbonHotKey(slot: HotKeySlotID) {
        carbonSlots.removeValue(forKey: slot)
        failedCarbonSlots.remove(slot)
        carbonBackend.unregister(slot: slot)
    }

    public func registerConsumer(_ consumer: any DesktopInputConsumer) {
        eventTapRouter.dispatcher.register(consumer)
        syncTapState()
    }

    public func unregisterConsumer(_ consumer: any DesktopInputConsumer) {
        eventTapRouter.dispatcher.unregister(consumer)
        syncTapState()
    }

    public func unregisterConsumers(for slot: DesktopFeatureSlot) {
        eventTapRouter.dispatcher.unregister(slot: slot)
        syncTapState()
    }

    @discardableResult
    public func syncTapState() -> EventTapBackendStatus {
        if isSuspendedForRelaunch {
            eventTapRouter.stop()
            eventTapStatus = .inactive
            return eventTapStatus
        }
        let status = eventTapRouter.syncState()
        eventTapStatus = status
        if status == .active || status == .pausedMissingPermission || status == .creationFailed {
            permissionState = eventTapRouter.lastPermissionState
        }
        return status
    }

    public func setShortcutRecording(_ recording: Bool) {
        guard isRecordingShortcut != recording else { return }
        isRecordingShortcut = recording
        eventTapRouter.dispatcher.setRecordingShortcut(recording)
        if recording {
            carbonBackend.unregisterAll()
        } else {
            resumeCarbonHotKeysIfActive()
        }
    }

    @discardableResult
    public func refreshPermissions() -> DesktopPermissionState {
        let state = permissionService.currentState()
        permissionState = state
        syncTapState()
        return state
    }

    @discardableResult
    public func requestAccessibilityPrompt() -> Bool {
        let granted = permissionService.requestAccessibilityPrompt()
        refreshPermissions()
        return granted
    }

    @discardableResult
    public func requestInputMonitoringPrompt() -> Bool {
        let granted = permissionService.requestInputMonitoringPrompt()
        refreshPermissions()
        return granted
    }

    @discardableResult
    public func openAccessibilitySettings() -> Bool {
        permissionService.openAccessibilitySettings()
    }

    @discardableResult
    public func openInputMonitoringSettings() -> Bool {
        permissionService.openInputMonitoringSettings()
    }

    public func suspendForRelaunch() {
        isSuspendedForRelaunch = true
        carbonBackend.unregisterAll()
        eventTapRouter.stop()
        eventTapStatus = .inactive
    }

    public func resumeAfterFailedRelaunch() {
        guard isSuspendedForRelaunch else { return }
        isSuspendedForRelaunch = false
        resumeCarbonHotKeysIfActive()
        syncTapState()
    }

    public func stopAll() {
        isSuspendedForRelaunch = false
        isRecordingShortcut = false
        eventTapRouter.dispatcher.setRecordingShortcut(false)
        carbonSlots.removeAll()
        failedCarbonSlots.removeAll()
        carbonBackend.unregisterAll()
        eventTapRouter.stop()
        eventTapStatus = .inactive
    }

    public func detectConflicts(
        extraBindings: [ShortcutBindingDescriptor] = [],
        checkNativeTilingOverlap: Bool = false
    ) -> [ShortcutConflict] {
        var bindings: [ShortcutBindingDescriptor] = carbonSlots.values
            .sorted { $0.slot.carbonID < $1.slot.carbonID }
            .filter { $0.shortcut.isEnabled }
            .map { entry in
                ShortcutBindingDescriptor(
                    owner: entry.featureSlot,
                    label: entry.label,
                    keyCode: UInt16(truncatingIfNeeded: entry.shortcut.keyCode),
                    modifiers: InputModifierSet(carbonModifiers: entry.shortcut.carbonModifiers)
                )
            }
        bindings.append(contentsOf: extraBindings)

        let failedFeatureSlots: Set<DesktopFeatureSlot> = Set(
            failedCarbonSlots.compactMap { slot in
                carbonSlots[slot]?.featureSlot ?? (slot == .syncPanel ? .syncPanel : nil)
            }
        )
        let symbolicHotKeys = symbolicHotKeyProvider.fetchSymbolicHotKeys()
        return ShortcutConflictDetector.detect(
            bindings: bindings,
            failedCarbonSlots: failedFeatureSlots,
            symbolicHotKeys: symbolicHotKeys,
            checkNativeTilingOverlap: checkNativeTilingOverlap
        )
    }

    private func resumeCarbonHotKeysIfActive() {
        guard !isGlobalInputDisabledByEnvironment, !isRecordingShortcut, !isSuspendedForRelaunch else {
            return
        }
        for entry in carbonSlots.values.sorted(by: { $0.slot.carbonID < $1.slot.carbonID }) {
            _ = applyCarbonEntry(entry)
        }
    }

    @discardableResult
    private func applyCarbonEntry(_ entry: CarbonSlotEntry) -> Bool {
        if !entry.shortcut.isEnabled {
            carbonBackend.unregister(slot: entry.slot)
            failedCarbonSlots.remove(entry.slot)
            return true
        }
        let ok = carbonBackend.register(
            slot: entry.slot,
            shortcut: entry.shortcut,
            handler: entry.handler
        )
        if ok {
            failedCarbonSlots.remove(entry.slot)
        } else {
            failedCarbonSlots.insert(entry.slot)
        }
        return ok
    }
}
