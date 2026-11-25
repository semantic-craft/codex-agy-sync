import Carbon.HIToolbox
import CoreGraphics
import Foundation
import os
import XCTest
@testable import CodexAgySyncCore

private final class MockPermissionService: DesktopPermissionChecking, @unchecked Sendable {
    private struct State {
        var permissionState: DesktopPermissionState
        var currentStateCallCount: Int = 0
        var accessibilityPromptCount: Int = 0
        var inputMonitoringPromptCount: Int = 0
        var openAccessibilityCount: Int = 0
        var openInputMonitoringCount: Int = 0
    }

    private let lock: OSAllocatedUnfairLock<State>

    init(
        accessibilityGranted: Bool = false,
        inputMonitoringGranted: Bool = false
    ) {
        self.lock = OSAllocatedUnfairLock(
            initialState: State(
                permissionState: DesktopPermissionState(
                    accessibilityGranted: accessibilityGranted,
                    inputMonitoringGranted: inputMonitoringGranted
                )
            )
        )
    }

    func setPermissions(accessibility: Bool, inputMonitoring: Bool) {
        lock.withLock {
            $0.permissionState = DesktopPermissionState(
                accessibilityGranted: accessibility,
                inputMonitoringGranted: inputMonitoring
            )
        }
    }

    var accessibilityPromptCount: Int { lock.withLock { $0.accessibilityPromptCount } }
    var inputMonitoringPromptCount: Int { lock.withLock { $0.inputMonitoringPromptCount } }
    var openAccessibilityCount: Int { lock.withLock { $0.openAccessibilityCount } }
    var openInputMonitoringCount: Int { lock.withLock { $0.openInputMonitoringCount } }

    func currentState() -> DesktopPermissionState {
        lock.withLock {
            $0.currentStateCallCount += 1
            return $0.permissionState
        }
    }

    func requestAccessibilityPrompt() -> Bool {
        lock.withLock {
            $0.accessibilityPromptCount += 1
            return $0.permissionState.accessibilityGranted
        }
    }

    func requestInputMonitoringPrompt() -> Bool {
        lock.withLock {
            $0.inputMonitoringPromptCount += 1
            return $0.permissionState.inputMonitoringGranted
        }
    }

    func openAccessibilitySettings() -> Bool {
        lock.withLock {
            $0.openAccessibilityCount += 1
            return true
        }
    }

    func openInputMonitoringSettings() -> Bool {
        lock.withLock {
            $0.openInputMonitoringCount += 1
            return true
        }
    }
}

@MainActor
private final class MockCarbonHotKeyBackend: CarbonHotKeyBackend {
    var failingSlots: Set<HotKeySlotID> = []
    private(set) var registered: [HotKeySlotID: HotKeyShortcut] = [:]
    private(set) var handlers: [HotKeySlotID: @MainActor @Sendable () -> Void] = [:]
    private(set) var registerCalls: [(HotKeySlotID, HotKeyShortcut)] = []
    private(set) var unregisterCalls: [HotKeySlotID] = []
    private(set) var unregisterAllCount: Int = 0

    func register(
        slot: HotKeySlotID,
        shortcut: HotKeyShortcut,
        handler: @escaping @MainActor @Sendable () -> Void
    ) -> Bool {
        registerCalls.append((slot, shortcut))
        if !shortcut.isEnabled {
            unregister(slot: slot)
            return true
        }
        if failingSlots.contains(slot) {
            registered.removeValue(forKey: slot)
            handlers.removeValue(forKey: slot)
            return false
        }
        registered[slot] = shortcut
        handlers[slot] = handler
        return true
    }

    func unregister(slot: HotKeySlotID) {
        unregisterCalls.append(slot)
        registered.removeValue(forKey: slot)
        handlers.removeValue(forKey: slot)
    }

    func unregisterAll() {
        unregisterAllCount += 1
        registered.removeAll()
        handlers.removeAll()
    }

    func trigger(slot: HotKeySlotID) {
        handlers[slot]?()
    }
}

@MainActor
private final class MockEventTapTransport: EventTapTransport {
    var shouldSucceedStart: Bool = true
    private(set) var isRunning: Bool = false
    private(set) var currentMask: CGEventMask = 0
    private(set) var startMasks: [CGEventMask] = []
    private(set) var stopCount: Int = 0
    private var eventCallback: (@Sendable (RoutedInputEvent, InputEventContext) -> InputRoutingDecision)?
    private var resetCallback: (@Sendable () -> Void)?

    func start(
        mask: CGEventMask,
        onEvent: @escaping @Sendable (RoutedInputEvent, InputEventContext) -> InputRoutingDecision,
        onReset: @escaping @Sendable () -> Void
    ) -> Bool {
        startMasks.append(mask)
        guard shouldSucceedStart, mask != 0 else {
            isRunning = false
            currentMask = 0
            eventCallback = nil
            resetCallback = nil
            return false
        }
        isRunning = true
        currentMask = mask
        eventCallback = onEvent
        resetCallback = onReset
        return true
    }

    func stop() {
        stopCount += 1
        isRunning = false
        currentMask = 0
        eventCallback = nil
        resetCallback = nil
    }

    func simulateEvent(
        _ event: RoutedInputEvent,
        context: InputEventContext = InputEventContext()
    ) -> InputRoutingDecision {
        eventCallback?(event, context) ?? .passThrough
    }

    func simulateTapReset() {
        resetCallback?()
    }
}

private struct MockSymbolicHotKeyProvider: SystemSymbolicHotKeyProviding {
    let keys: [SystemSymbolicHotKey]

    func fetchSymbolicHotKeys() -> [SystemSymbolicHotKey] {
        keys
    }
}

private final class RecordingConsumer: DesktopInputConsumer, @unchecked Sendable {
    private struct State {
        var isActive: Bool
        var requiredEvents: EventTapRequirement
        var decision: InputRoutingDecision
        var receivedEvents: [RoutedInputEvent] = []
        var receivedContexts: [InputEventContext] = []
        var resetCount: Int = 0
    }

    let slot: DesktopFeatureSlot
    let priority: Int
    private let lock: OSAllocatedUnfairLock<State>

    init(
        slot: DesktopFeatureSlot,
        priority: Int,
        requiredEvents: EventTapRequirement,
        isActive: Bool = true,
        decision: InputRoutingDecision = .passThrough
    ) {
        self.slot = slot
        self.priority = priority
        self.lock = OSAllocatedUnfairLock(
            initialState: State(
                isActive: isActive,
                requiredEvents: requiredEvents,
                decision: decision
            )
        )
    }

    var requiredEvents: EventTapRequirement {
        lock.withLock { $0.requiredEvents }
    }

    var isActive: Bool {
        get { lock.withLock { $0.isActive } }
        set { lock.withLock { $0.isActive = newValue } }
    }

    var decision: InputRoutingDecision {
        get { lock.withLock { $0.decision } }
        set { lock.withLock { $0.decision = newValue } }
    }

    var receivedEvents: [RoutedInputEvent] {
        lock.withLock { $0.receivedEvents }
    }

    var receivedContexts: [InputEventContext] {
        lock.withLock { $0.receivedContexts }
    }

    var resetCount: Int {
        lock.withLock { $0.resetCount }
    }

    func setRequiredEvents(_ events: EventTapRequirement) {
        lock.withLock { $0.requiredEvents = events }
    }

    func handle(_ event: RoutedInputEvent, context: InputEventContext) -> InputRoutingDecision {
        lock.withLock {
            $0.receivedEvents.append(event)
            $0.receivedContexts.append(context)
            return $0.decision
        }
    }

    func handleTapReset() {
        lock.withLock {
            $0.resetCount += 1
        }
    }
}

@MainActor
final class DesktopInputHubTests: XCTestCase {
    func testDefaultOffZeroEventTapAndZeroPermissionPrompts() {
        let permissions = MockPermissionService(accessibilityGranted: false, inputMonitoringGranted: false)
        let carbon = MockCarbonHotKeyBackend()
        let transport = MockEventTapTransport()

        let hub = DesktopInputHub(
            environment: [:],
            carbonBackend: carbon,
            eventTapTransport: transport,
            permissionService: permissions,
            symbolicHotKeyProvider: MockSymbolicHotKeyProvider(keys: [])
        )

        XCTAssertEqual(hub.eventTapStatus, .inactive)
        XCTAssertFalse(transport.isRunning)
        XCTAssertTrue(transport.startMasks.isEmpty)
        XCTAssertEqual(permissions.accessibilityPromptCount, 0)
        XCTAssertEqual(permissions.inputMonitoringPromptCount, 0)

        let inactiveConsumer = RecordingConsumer(
            slot: .windowResize,
            priority: 10,
            requiredEvents: [.flagsChanged, .mouseMoved],
            isActive: false
        )
        hub.registerConsumer(inactiveConsumer)

        XCTAssertEqual(hub.eventTapStatus, .inactive)
        XCTAssertFalse(transport.isRunning)
        XCTAssertTrue(transport.startMasks.isEmpty)
        XCTAssertEqual(permissions.accessibilityPromptCount, 0)
        XCTAssertEqual(permissions.inputMonitoringPromptCount, 0)
    }

    func testRecordingSuspendsCarbonAndPassesThroughTapEvents() {
        let permissions = MockPermissionService(accessibilityGranted: true, inputMonitoringGranted: true)
        let carbon = MockCarbonHotKeyBackend()
        let transport = MockEventTapTransport()

        let hub = DesktopInputHub(
            environment: [:],
            carbonBackend: carbon,
            eventTapTransport: transport,
            permissionService: permissions,
            symbolicHotKeyProvider: MockSymbolicHotKeyProvider(keys: [])
        )

        var triggerCount = 0
        let initialShortcut = HotKeyShortcut.defaultShortcut
        XCTAssertTrue(
            hub.registerCarbonHotKey(
                slot: .syncPanel,
                featureSlot: .syncPanel,
                shortcut: initialShortcut
            ) {
                triggerCount += 1
            }
        )
        XCTAssertEqual(carbon.registered[.syncPanel], initialShortcut)

        let consumer = RecordingConsumer(
            slot: .cleanPaste,
            priority: 10,
            requiredEvents: [.keyDown],
            isActive: true,
            decision: .consume
        )
        hub.registerConsumer(consumer)
        XCTAssertEqual(hub.eventTapStatus, .active)

        let keyEvent = RoutedInputEvent(
            kind: .keyDown(keyCode: UInt16(kVK_ANSI_V), modifiers: [.command, .shift], isRepeat: false),
            timestamp: 1.0
        )

        XCTAssertEqual(transport.simulateEvent(keyEvent), .consume)
        XCTAssertEqual(consumer.receivedEvents.count, 1)

        hub.setShortcutRecording(true)
        XCTAssertTrue(hub.isRecordingShortcut)
        XCTAssertTrue(hub.dispatcher.isRecordingShortcut)
        XCTAssertTrue(carbon.registered.isEmpty)

        XCTAssertEqual(transport.simulateEvent(keyEvent), .passThrough)
        XCTAssertEqual(consumer.receivedEvents.count, 1)

        let updatedShortcut = HotKeyShortcut(
            isEnabled: true,
            keyCode: UInt32(kVK_ANSI_K),
            carbonModifiers: HotKeyShortcut.carbonCmdKey | HotKeyShortcut.carbonOptionKey,
            displayString: "⌥⌘K"
        )
        XCTAssertTrue(hub.reconfigureCarbonHotKey(slot: .syncPanel, shortcut: updatedShortcut))
        XCTAssertTrue(carbon.registered.isEmpty)

        hub.setShortcutRecording(false)
        XCTAssertFalse(hub.isRecordingShortcut)
        XCTAssertFalse(hub.dispatcher.isRecordingShortcut)
        XCTAssertEqual(carbon.registered[.syncPanel], updatedShortcut)

        carbon.trigger(slot: .syncPanel)
        XCTAssertEqual(triggerCount, 1)

        XCTAssertEqual(transport.simulateEvent(keyEvent), .consume)
        XCTAssertEqual(consumer.receivedEvents.count, 2)
    }

    func testPrioritySingleConsumeAndPassThroughOrdering() {
        let dispatcher = EventTapDispatcher()

        let lowPriority = RecordingConsumer(
            slot: .appHotKeys,
            priority: 30,
            requiredEvents: [.keyDown],
            isActive: true,
            decision: .consume
        )
        let highPriority = RecordingConsumer(
            slot: .cleanPaste,
            priority: 10,
            requiredEvents: [.keyDown],
            isActive: true,
            decision: .consume
        )
        let inactiveHighest = RecordingConsumer(
            slot: .windowResize,
            priority: 1,
            requiredEvents: [.keyDown],
            isActive: false,
            decision: .consume
        )
        let mouseOnly = RecordingConsumer(
            slot: .relaySlashCommand,
            priority: 5,
            requiredEvents: [.mouseMoved],
            isActive: true,
            decision: .consume
        )

        dispatcher.register(lowPriority)
        dispatcher.register(highPriority)
        dispatcher.register(inactiveHighest)
        dispatcher.register(mouseOnly)

        let keyEvent = RoutedInputEvent(
            kind: .keyDown(keyCode: UInt16(kVK_ANSI_A), modifiers: [.command], isRepeat: false)
        )
        let firstDecision = dispatcher.dispatch(keyEvent, context: InputEventContext())
        XCTAssertEqual(firstDecision, .consume)
        XCTAssertEqual(inactiveHighest.receivedEvents.count, 0)
        XCTAssertEqual(mouseOnly.receivedEvents.count, 0)
        XCTAssertEqual(highPriority.receivedEvents.count, 1)
        XCTAssertEqual(lowPriority.receivedEvents.count, 0)

        highPriority.decision = .passThrough
        let secondDecision = dispatcher.dispatch(keyEvent, context: InputEventContext())
        XCTAssertEqual(secondDecision, .consume)
        XCTAssertEqual(highPriority.receivedEvents.count, 2)
        XCTAssertEqual(lowPriority.receivedEvents.count, 1)

        lowPriority.decision = .passThrough
        let thirdDecision = dispatcher.dispatch(keyEvent, context: InputEventContext())
        XCTAssertEqual(thirdDecision, .passThrough)
        XCTAssertEqual(highPriority.receivedEvents.count, 3)
        XCTAssertEqual(lowPriority.receivedEvents.count, 2)
    }

    func testSecureInputPassesThroughKeyboardEventsOnly() {
        let dispatcher = EventTapDispatcher()

        let keyboardConsumer = RecordingConsumer(
            slot: .cleanPaste,
            priority: 10,
            requiredEvents: [.keyDown, .keyUp, .flagsChanged],
            isActive: true,
            decision: .consume
        )
        let mouseConsumer = RecordingConsumer(
            slot: .windowResize,
            priority: 20,
            requiredEvents: [.mouseMoved, .leftMouse],
            isActive: true,
            decision: .consume
        )

        dispatcher.register(keyboardConsumer)
        dispatcher.register(mouseConsumer)

        let secureContext = InputEventContext(
            frontmostBundleId: "com.apple.SecurityAgent",
            isSecureInputActive: true,
            isRecordingShortcut: false
        )

        XCTAssertEqual(
            dispatcher.dispatch(
                RoutedInputEvent(kind: .keyDown(keyCode: UInt16(kVK_ANSI_V), modifiers: [.command], isRepeat: false)),
                context: secureContext
            ),
            .passThrough
        )
        XCTAssertEqual(
            dispatcher.dispatch(
                RoutedInputEvent(kind: .keyUp(keyCode: UInt16(kVK_ANSI_V), modifiers: [.command])),
                context: secureContext
            ),
            .passThrough
        )
        XCTAssertEqual(
            dispatcher.dispatch(
                RoutedInputEvent(kind: .flagsChanged(modifiers: [.command])),
                context: secureContext
            ),
            .passThrough
        )
        XCTAssertTrue(keyboardConsumer.receivedEvents.isEmpty)

        XCTAssertEqual(
            dispatcher.dispatch(
                RoutedInputEvent(kind: .mouseMoved(location: CGPoint(x: 100, y: 200), modifiers: [.control])),
                context: secureContext
            ),
            .consume
        )
        XCTAssertEqual(mouseConsumer.receivedEvents.count, 1)
    }

    func testMissingPermissionDegradesTapSafelyWhileCarbonSyncPanelStillWorks() {
        let permissions = MockPermissionService(accessibilityGranted: true, inputMonitoringGranted: false)
        let carbon = MockCarbonHotKeyBackend()
        let transport = MockEventTapTransport()

        let hub = DesktopInputHub(
            environment: [:],
            carbonBackend: carbon,
            eventTapTransport: transport,
            permissionService: permissions,
            symbolicHotKeyProvider: MockSymbolicHotKeyProvider(keys: [])
        )

        var syncPanelTriggered = 0
        XCTAssertTrue(
            hub.registerCarbonHotKey(
                slot: .syncPanel,
                featureSlot: .syncPanel,
                shortcut: .defaultShortcut
            ) {
                syncPanelTriggered += 1
            }
        )

        let consumer = RecordingConsumer(
            slot: .windowResize,
            priority: 10,
            requiredEvents: [.flagsChanged, .mouseMoved],
            isActive: true
        )
        hub.registerConsumer(consumer)

        XCTAssertEqual(hub.eventTapStatus, .pausedMissingPermission)
        XCTAssertFalse(transport.isRunning)
        XCTAssertTrue(transport.startMasks.isEmpty)
        XCTAssertEqual(permissions.accessibilityPromptCount, 0)
        XCTAssertEqual(permissions.inputMonitoringPromptCount, 0)

        carbon.trigger(slot: .syncPanel)
        XCTAssertEqual(syncPanelTriggered, 1)

        permissions.setPermissions(accessibility: true, inputMonitoring: true)
        let refreshed = hub.refreshPermissions()
        XCTAssertTrue(refreshed.hasEventTapAccess)
        XCTAssertEqual(hub.eventTapStatus, .active)
        XCTAssertTrue(transport.isRunning)
        XCTAssertEqual(transport.startMasks.count, 1)
    }

    func testEnvironmentDisableGuardDisablesCarbonAndEventTap() {
        let permissions = MockPermissionService(accessibilityGranted: true, inputMonitoringGranted: true)
        let carbon = MockCarbonHotKeyBackend()
        let transport = MockEventTapTransport()

        let hub = DesktopInputHub(
            environment: [DesktopInputHub.disableGlobalHotKeyEnvKey: "1"],
            carbonBackend: carbon,
            eventTapTransport: transport,
            permissionService: permissions,
            symbolicHotKeyProvider: MockSymbolicHotKeyProvider(keys: [])
        )

        XCTAssertTrue(hub.isGlobalInputDisabledByEnvironment)
        XCTAssertTrue(
            hub.registerCarbonHotKey(
                slot: .syncPanel,
                featureSlot: .syncPanel,
                shortcut: .defaultShortcut
            ) {}
        )
        XCTAssertTrue(carbon.registered.isEmpty)
        XCTAssertTrue(carbon.registerCalls.isEmpty)
        XCTAssertTrue(hub.failedCarbonSlots.isEmpty)

        let consumer = RecordingConsumer(
            slot: .cleanPaste,
            priority: 10,
            requiredEvents: [.keyDown],
            isActive: true
        )
        hub.registerConsumer(consumer)

        XCTAssertEqual(hub.eventTapStatus, .disabledByEnvironment)
        XCTAssertFalse(transport.isRunning)
        XCTAssertTrue(transport.startMasks.isEmpty)
    }

    func testTapResetBroadcastAndSingleTapMaskReconfiguration() {
        let permissions = MockPermissionService(accessibilityGranted: true, inputMonitoringGranted: true)
        let carbon = MockCarbonHotKeyBackend()
        let transport = MockEventTapTransport()

        let hub = DesktopInputHub(
            environment: [:],
            carbonBackend: carbon,
            eventTapTransport: transport,
            permissionService: permissions,
            symbolicHotKeyProvider: MockSymbolicHotKeyProvider(keys: [])
        )

        let resizeConsumer = RecordingConsumer(
            slot: .windowResize,
            priority: 10,
            requiredEvents: [.flagsChanged, .mouseMoved],
            isActive: true
        )
        let pasteConsumer = RecordingConsumer(
            slot: .cleanPaste,
            priority: 20,
            requiredEvents: [.keyDown],
            isActive: false
        )

        hub.registerConsumer(resizeConsumer)
        hub.registerConsumer(pasteConsumer)

        let initialMask = EventTapRequirement([.flagsChanged, .mouseMoved]).cgEventMask
        XCTAssertEqual(hub.eventTapStatus, .active)
        XCTAssertEqual(transport.currentMask, initialMask)
        XCTAssertEqual(transport.startMasks, [initialMask])

        XCTAssertEqual(hub.syncTapState(), .active)
        XCTAssertEqual(transport.startMasks.count, 1)

        pasteConsumer.isActive = true
        XCTAssertEqual(hub.syncTapState(), .active)
        let unionMask = EventTapRequirement([.flagsChanged, .mouseMoved, .keyDown]).cgEventMask
        XCTAssertEqual(transport.currentMask, unionMask)
        XCTAssertEqual(transport.startMasks, [initialMask, unionMask])

        transport.simulateTapReset()
        XCTAssertEqual(resizeConsumer.resetCount, 1)
        XCTAssertEqual(pasteConsumer.resetCount, 1)

        resizeConsumer.isActive = false
        pasteConsumer.isActive = false
        XCTAssertEqual(hub.syncTapState(), .inactive)
        XCTAssertFalse(transport.isRunning)
        XCTAssertEqual(transport.stopCount, 1)

        transport.shouldSucceedStart = false
        resizeConsumer.isActive = true
        XCTAssertEqual(hub.syncTapState(), .creationFailed)
        XCTAssertFalse(transport.isRunning)
    }

    func testExcludedAppMatcherNormalizationAndMatching() {
        let normalized = ExcludedAppMatcher.normalize([
            "  com.Apple.Terminal  ",
            "",
            "   ",
            "com.apple.terminal",
            "COM.MICROSOFT.VSCode",
            "com.jetbrains.intellij\n",
            "com.microsoft.vscode",
        ])

        XCTAssertEqual(
            normalized,
            [
                "com.apple.terminal",
                "com.microsoft.vscode",
                "com.jetbrains.intellij",
            ]
        )

        XCTAssertTrue(ExcludedAppMatcher.isExcluded(bundleId: "com.apple.Terminal", in: normalized))
        XCTAssertTrue(ExcludedAppMatcher.isExcluded(bundleId: "  COM.JETBRAINS.INTELLIJ ", in: normalized))
        XCTAssertFalse(ExcludedAppMatcher.isExcluded(bundleId: "com.apple.Safari", in: normalized))
        XCTAssertFalse(ExcludedAppMatcher.isExcluded(bundleId: nil, in: normalized))
        XCTAssertFalse(ExcludedAppMatcher.isExcluded(bundleId: "   ", in: normalized))

        let nonAppURL = URL(fileURLWithPath: "/nonexistent/path/NotAnApp.app")
        XCTAssertNil(RunningAppCatalog.descriptor(forAppURL: nonAppURL))
    }

    func testShortcutConflictDetectorAllKindsAndHubIntegration() {
        let sharedCombo = ShortcutBindingDescriptor(
            owner: .syncPanel,
            label: "⌃⌥H",
            keyCode: UInt16(kVK_ANSI_H),
            modifiers: [.control, .option]
        )
        let duplicateCombo = ShortcutBindingDescriptor(
            owner: .appHotKeys,
            label: "App H",
            keyCode: UInt16(kVK_ANSI_H),
            modifiers: [.control, .option]
        )
        let tilingCombo = ShortcutBindingDescriptor(
            owner: .windowResize,
            label: "fn⌃←",
            keyCode: UInt16(kVK_LeftArrow),
            modifiers: [.fn, .control]
        )
        let nonTilingFnCombo = ShortcutBindingDescriptor(
            owner: .appHotKeys,
            label: "fn⌃H",
            keyCode: UInt16(kVK_ANSI_H),
            modifiers: [.fn, .control]
        )

        let symbolicHotKeys = [
            SystemSymbolicHotKey(
                symbolicId: 64,
                keyCode: UInt16(kVK_ANSI_H),
                modifiers: [.control, .option],
                enabled: true
            ),
            SystemSymbolicHotKey(
                symbolicId: 65,
                keyCode: UInt16(kVK_LeftArrow),
                modifiers: [.fn, .control],
                enabled: false
            ),
        ]

        let conflicts = ShortcutConflictDetector.detect(
            bindings: [sharedCombo, duplicateCombo, tilingCombo, nonTilingFnCombo],
            failedCarbonSlots: [.syncPanel],
            symbolicHotKeys: symbolicHotKeys,
            checkNativeTilingOverlap: true
        )

        XCTAssertEqual(
            conflicts.filter { $0.kind == .internalDuplicate }.map(\.owner),
            [.syncPanel, .appHotKeys]
        )
        XCTAssertEqual(
            conflicts.filter { $0.kind == .carbonRegistrationFailed }.map(\.owner),
            [.syncPanel]
        )
        XCTAssertEqual(
            conflicts.filter { $0.kind == .systemSymbolicHotKey }.map(\.owner),
            [.syncPanel, .appHotKeys]
        )
        XCTAssertEqual(
            conflicts.filter { $0.kind == .macOSNativeTilingOverlap }.map(\.owner),
            [.windowResize]
        )

        let conflictsWithoutTiling = ShortcutConflictDetector.detect(
            bindings: [tilingCombo],
            checkNativeTilingOverlap: false
        )
        XCTAssertTrue(conflictsWithoutTiling.isEmpty)

        let permissions = MockPermissionService(accessibilityGranted: true, inputMonitoringGranted: true)
        let carbon = MockCarbonHotKeyBackend()
        carbon.failingSlots = [.syncPanel]
        let transport = MockEventTapTransport()

        let hub = DesktopInputHub(
            environment: [:],
            carbonBackend: carbon,
            eventTapTransport: transport,
            permissionService: permissions,
            symbolicHotKeyProvider: MockSymbolicHotKeyProvider(keys: symbolicHotKeys)
        )

        XCTAssertFalse(
            hub.registerCarbonHotKey(
                slot: .syncPanel,
                featureSlot: .syncPanel,
                shortcut: .defaultShortcut
            ) {}
        )
        XCTAssertEqual(hub.failedCarbonSlots, [.syncPanel])

        let hubConflicts = hub.detectConflicts(
            extraBindings: [duplicateCombo, tilingCombo],
            checkNativeTilingOverlap: true
        )
        XCTAssertTrue(hubConflicts.contains { $0.owner == .syncPanel && $0.kind == .carbonRegistrationFailed })
        XCTAssertTrue(hubConflicts.contains { $0.owner == .syncPanel && $0.kind == .internalDuplicate })
        XCTAssertTrue(hubConflicts.contains { $0.owner == .syncPanel && $0.kind == .systemSymbolicHotKey })
        XCTAssertTrue(hubConflicts.contains { $0.owner == .windowResize && $0.kind == .macOSNativeTilingOverlap })
    }

    func testCarbonHotKeyRelaunchSuspendResumeAndStopAll() {
        let permissions = MockPermissionService(accessibilityGranted: true, inputMonitoringGranted: true)
        let carbon = MockCarbonHotKeyBackend()
        let transport = MockEventTapTransport()

        let hub = DesktopInputHub(
            environment: [:],
            carbonBackend: carbon,
            eventTapTransport: transport,
            permissionService: permissions,
            symbolicHotKeyProvider: MockSymbolicHotKeyProvider(keys: [])
        )

        let secondSlot = HotKeySlotID(rawValue: "cleanPaste", carbonID: 2)
        let pasteShortcut = HotKeyShortcut(
            isEnabled: true,
            keyCode: UInt32(kVK_ANSI_V),
            carbonModifiers: HotKeyShortcut.carbonCmdKey | HotKeyShortcut.carbonOptionKey | HotKeyShortcut.carbonShiftKey,
            displayString: "⌥⇧⌘V"
        )

        var syncHits = 0
        var pasteHits = 0
        XCTAssertTrue(
            hub.registerCarbonHotKey(slot: .syncPanel, featureSlot: .syncPanel, shortcut: .defaultShortcut) {
                syncHits += 1
            }
        )
        XCTAssertTrue(
            hub.registerCarbonHotKey(slot: secondSlot, featureSlot: .cleanPaste, shortcut: pasteShortcut) {
                pasteHits += 1
            }
        )

        let consumer = RecordingConsumer(
            slot: .windowResize,
            priority: 10,
            requiredEvents: [.flagsChanged],
            isActive: true
        )
        hub.registerConsumer(consumer)
        XCTAssertEqual(hub.eventTapStatus, .active)
        XCTAssertEqual(carbon.registered.count, 2)

        hub.suspendForRelaunch()
        XCTAssertTrue(hub.isSuspendedForRelaunch)
        XCTAssertTrue(carbon.registered.isEmpty)
        XCTAssertEqual(hub.eventTapStatus, .inactive)
        XCTAssertFalse(transport.isRunning)

        hub.resumeAfterFailedRelaunch()
        XCTAssertFalse(hub.isSuspendedForRelaunch)
        XCTAssertEqual(carbon.registered[.syncPanel], .defaultShortcut)
        XCTAssertEqual(carbon.registered[secondSlot], pasteShortcut)
        XCTAssertEqual(hub.eventTapStatus, .active)
        XCTAssertTrue(transport.isRunning)

        carbon.trigger(slot: .syncPanel)
        carbon.trigger(slot: secondSlot)
        XCTAssertEqual(syncHits, 1)
        XCTAssertEqual(pasteHits, 1)

        hub.stopAll()
        XCTAssertTrue(carbon.registered.isEmpty)
        XCTAssertEqual(hub.eventTapStatus, .inactive)
        XCTAssertFalse(transport.isRunning)
    }

    func testInputModifierSetConversionsAndCodableRoundTrip() throws {
        let allModifiers: InputModifierSet = [.fn, .control, .option, .shift, .command]
        let roundTrippedCG = InputModifierSet(cgEventFlags: allModifiers.cgEventFlags)
        XCTAssertEqual(roundTrippedCG, allModifiers)

        let carbonOnly: InputModifierSet = [.control, .option, .shift, .command]
        let roundTrippedCarbon = InputModifierSet(carbonModifiers: carbonOnly.carbonModifiers)
        XCTAssertEqual(roundTrippedCarbon, carbonOnly)

        let encoded = try JSONEncoder().encode(allModifiers)
        let decoded = try JSONDecoder().decode(InputModifierSet.self, from: encoded)
        XCTAssertEqual(decoded, allModifiers)

        let leftMouseMask = EventTapRequirement.leftMouse.cgEventMask
        XCTAssertNotEqual(leftMouseMask & (1 << CGEventType.leftMouseDown.rawValue), 0)
        XCTAssertNotEqual(leftMouseMask & (1 << CGEventType.leftMouseDragged.rawValue), 0)
        XCTAssertNotEqual(leftMouseMask & (1 << CGEventType.leftMouseUp.rawValue), 0)
    }
}
