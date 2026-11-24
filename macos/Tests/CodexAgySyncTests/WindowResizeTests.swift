import Carbon.HIToolbox
import CoreGraphics
import Foundation
import os
import XCTest
@testable import CodexAgySyncCore

private final class StubPermissionChecker: DesktopPermissionChecking, @unchecked Sendable {
    private struct State {
        var permissionState: DesktopPermissionState
        var accessibilityPromptCount: Int = 0
        var inputMonitoringPromptCount: Int = 0
    }

    private let lock: OSAllocatedUnfairLock<State>

    init(accessibility: Bool, inputMonitoring: Bool) {
        self.lock = OSAllocatedUnfairLock(
            initialState: State(
                permissionState: DesktopPermissionState(
                    accessibilityGranted: accessibility,
                    inputMonitoringGranted: inputMonitoring
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

    var promptCount: Int {
        lock.withLock { $0.accessibilityPromptCount + $0.inputMonitoringPromptCount }
    }

    func currentState() -> DesktopPermissionState {
        lock.withLock { $0.permissionState }
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

    func openAccessibilitySettings() -> Bool { true }
    func openInputMonitoringSettings() -> Bool { true }
}

@MainActor
private final class StubCarbonBackend: CarbonHotKeyBackend {
    private(set) var handlers: [HotKeySlotID: @MainActor @Sendable () -> Void] = [:]

    func register(
        slot: HotKeySlotID,
        shortcut: HotKeyShortcut,
        handler: @escaping @MainActor @Sendable () -> Void
    ) -> Bool {
        if shortcut.isEnabled {
            handlers[slot] = handler
        } else {
            handlers.removeValue(forKey: slot)
        }
        return true
    }

    func unregister(slot: HotKeySlotID) {
        handlers.removeValue(forKey: slot)
    }

    func unregisterAll() {
        handlers.removeAll()
    }

    func trigger(slot: HotKeySlotID) {
        handlers[slot]?()
    }
}

@MainActor
private final class StubEventTapTransport: EventTapTransport {
    private(set) var isRunning: Bool = false
    private(set) var currentMask: CGEventMask = 0
    private(set) var startMasks: [CGEventMask] = []
    private var eventHandler: (@Sendable (RoutedInputEvent, InputEventContext) -> InputRoutingDecision)?
    private var resetHandler: (@Sendable () -> Void)?

    func start(
        mask: CGEventMask,
        onEvent: @escaping @Sendable (RoutedInputEvent, InputEventContext) -> InputRoutingDecision,
        onReset: @escaping @Sendable () -> Void
    ) -> Bool {
        startMasks.append(mask)
        guard mask != 0 else { return false }
        isRunning = true
        currentMask = mask
        eventHandler = onEvent
        resetHandler = onReset
        return true
    }

    func stop() {
        isRunning = false
        currentMask = 0
        eventHandler = nil
        resetHandler = nil
    }

    func send(
        _ event: RoutedInputEvent,
        context: InputEventContext = InputEventContext()
    ) -> InputRoutingDecision {
        eventHandler?(event, context) ?? .passThrough
    }

    func triggerReset() {
        resetHandler?()
    }
}

private struct EmptySymbolicHotKeyProvider: SystemSymbolicHotKeyProviding {
    func fetchSymbolicHotKeys() -> [SystemSymbolicHotKey] { [] }
}

private final class FakeWindowDriver: WindowSystemDriving, @unchecked Sendable {
    private struct State {
        var target: WindowTargetSnapshot?
        var frames: [UInt64: CGRect] = [:]
        var guardEvents: [String] = []
        var moves: [(UInt64, CGPoint)] = []
        var resizes: [(UInt64, CGRect, ResizeAnchorCorner)] = []
        var placements: [WindowPlacementAction] = []
        var splitGestures: [(UInt64, SplitScreenGestureAction)] = []
        var placementResult: WindowResizeActionResult = .applied
        var splitResult: WindowResizeActionResult = .applied
    }

    private let lock = OSAllocatedUnfairLock(initialState: State())

    func setTarget(_ snapshot: WindowTargetSnapshot?) {
        lock.withLock { state in
            state.target = snapshot
            if let snapshot {
                state.frames[snapshot.windowId] = snapshot.frame
            }
        }
    }

    func setPlacementResult(_ result: WindowResizeActionResult) {
        lock.withLock { $0.placementResult = result }
    }

    func setSplitResult(_ result: WindowResizeActionResult) {
        lock.withLock { $0.splitResult = result }
    }

    var guardEvents: [String] { lock.withLock { $0.guardEvents } }
    var moves: [(UInt64, CGPoint)] { lock.withLock { $0.moves } }
    var resizes: [(UInt64, CGRect, ResizeAnchorCorner)] { lock.withLock { $0.resizes } }
    var placements: [WindowPlacementAction] { lock.withLock { $0.placements } }
    var splitGestures: [(UInt64, SplitScreenGestureAction)] { lock.withLock { $0.splitGestures } }

    func windowAtPointer(_ point: CGPoint, excludingProcessId: pid_t) -> WindowTargetSnapshot? {
        lock.withLock { state in
            guard let target = state.target, target.pid != excludingProcessId else {
                return nil
            }
            return target
        }
    }

    func currentFrame(for windowId: UInt64) -> CGRect? {
        lock.withLock { $0.frames[windowId] }
    }

    func beginEnhancedUIGuard(pid: pid_t) {
        lock.withLock { $0.guardEvents.append("begin:\(pid)") }
    }

    func endEnhancedUIGuard(pid: pid_t) {
        lock.withLock { $0.guardEvents.append("end:\(pid)") }
    }

    func moveWindow(windowId: UInt64, to origin: CGPoint) -> Bool {
        lock.withLock { state in
            state.moves.append((windowId, origin))
            if let existing = state.frames[windowId] {
                state.frames[windowId] = CGRect(origin: origin, size: existing.size)
            }
            return true
        }
    }

    func resizeWindow(windowId: UInt64, to frame: CGRect, corner: ResizeAnchorCorner) -> Bool {
        lock.withLock { state in
            state.resizes.append((windowId, frame, corner))
            state.frames[windowId] = frame
            return true
        }
    }

    func performNativePlacement(_ action: WindowPlacementAction) -> WindowResizeActionResult {
        lock.withLock { state in
            state.placements.append(action)
            return state.placementResult
        }
    }

    func performSplitOrFullScreen(
        _ action: SplitScreenGestureAction,
        windowId: UInt64
    ) -> WindowResizeActionResult {
        lock.withLock { state in
            state.splitGestures.append((windowId, action))
            return state.splitResult
        }
    }
}

final class WindowResizeTests: XCTestCase {
    @MainActor
    func testWindowResizePreferencesDefaultOffAndLegacyDecoding() throws {
        let defaultsPreferences = AppPreferences()
        XCTAssertFalse(defaultsPreferences.windowResize.isEnabled)
        XCTAssertFalse(defaultsPreferences.windowResize.nearestCornerResize)
        XCTAssertTrue(defaultsPreferences.windowResize.excludedBundleIds.isEmpty)

        let legacyJSON = """
        {
          "showAttentionDotOnStatusIcon": true,
          "hotKeyBehavior": "toggle_panel",
          "activateTargetAppAfterSync": false,
          "recentFeedLimit": 6,
          "language": "en",
          "autoUpdatePolicy": "off"
        }
        """
        let decodedLegacy = try JSONDecoder().decode(AppPreferences.self, from: Data(legacyJSON.utf8))
        XCTAssertFalse(decodedLegacy.windowResize.isEnabled)
        XCTAssertFalse(decodedLegacy.windowResize.nearestCornerResize)
        XCTAssertTrue(decodedLegacy.windowResize.excludedBundleIds.isEmpty)
        XCTAssertEqual(decodedLegacy.language, .en)

        let isolated = makeIsolatedTestDefaultsSuite(label: "window-resize-prefs")
        defer { isolated.cleanup() }

        let store = PreferencesStore(defaults: isolated.defaults, loginItemManager: NoOpLoginItemManager())
        XCTAssertFalse(store.preferences.windowResize.isEnabled)

        store.setWindowResizeEnabled(true)
        store.setWindowResizeNearestCorner(true)
        store.setWindowResizeExcludedBundleIds(["  com.Apple.Terminal ", "com.apple.terminal", "com.microsoft.VSCode"])
        store.addWindowResizeExcludedBundleId("com.apple.Safari")
        store.removeWindowResizeExcludedBundleId("COM.APPLE.TERMINAL")

        let reloaded = PreferencesStore(defaults: isolated.defaults, loginItemManager: NoOpLoginItemManager())
        XCTAssertTrue(reloaded.preferences.windowResize.isEnabled)
        XCTAssertTrue(reloaded.preferences.windowResize.nearestCornerResize)
        XCTAssertEqual(
            reloaded.preferences.windowResize.excludedBundleIds,
            ["com.microsoft.vscode", "com.apple.safari"]
        )
    }

    func testWindowGeometryMoveResizeClampingActualSizeAdjustmentAndSplitDirections() {
        let moved = WindowGeometry.movedOrigin(
            initialOrigin: CGPoint(x: 100, y: 200),
            startPointer: CGPoint(x: 150, y: 240),
            currentPointer: CGPoint(x: 185, y: 215)
        )
        XCTAssertEqual(moved, CGPoint(x: 135, y: 175))

        let frame = CGRect(x: 100, y: 100, width: 400, height: 300)
        XCTAssertEqual(WindowGeometry.nearestCorner(for: CGPoint(x: 150, y: 150), in: frame), .topLeft)
        XCTAssertEqual(WindowGeometry.nearestCorner(for: CGPoint(x: 450, y: 150), in: frame), .topRight)
        XCTAssertEqual(WindowGeometry.nearestCorner(for: CGPoint(x: 150, y: 350), in: frame), .bottomLeft)
        XCTAssertEqual(WindowGeometry.nearestCorner(for: CGPoint(x: 450, y: 350), in: frame), .bottomRight)

        let bottomRightGrow = WindowGeometry.resizedFrame(
            initialFrame: frame,
            corner: .bottomRight,
            startPointer: CGPoint(x: 480, y: 380),
            currentPointer: CGPoint(x: 530, y: 420)
        )
        XCTAssertEqual(bottomRightGrow, CGRect(x: 100, y: 100, width: 450, height: 340))

        let topLeftClamp = WindowGeometry.resizedFrame(
            initialFrame: frame,
            corner: .topLeft,
            startPointer: CGPoint(x: 100, y: 100),
            currentPointer: CGPoint(x: 600, y: 500),
            minimumSize: CGSize(width: 120, height: 80)
        )
        XCTAssertEqual(topLeftClamp.size, CGSize(width: 120, height: 80))
        XCTAssertEqual(topLeftClamp.maxX, frame.maxX)
        XCTAssertEqual(topLeftClamp.maxY, frame.maxY)

        let bottomLeftClamp = WindowGeometry.resizedFrame(
            initialFrame: frame,
            corner: .bottomLeft,
            startPointer: CGPoint(x: 100, y: 400),
            currentPointer: CGPoint(x: 550, y: 100),
            minimumSize: CGSize(width: 120, height: 80)
        )
        XCTAssertEqual(bottomLeftClamp.size, CGSize(width: 120, height: 80))
        XCTAssertEqual(bottomLeftClamp.maxX, frame.maxX)
        XCTAssertEqual(bottomLeftClamp.minY, frame.minY)

        let topRightClamp = WindowGeometry.resizedFrame(
            initialFrame: frame,
            corner: .topRight,
            startPointer: CGPoint(x: 500, y: 100),
            currentPointer: CGPoint(x: 0, y: 500),
            minimumSize: CGSize(width: 120, height: 80)
        )
        XCTAssertEqual(topRightClamp.size, CGSize(width: 120, height: 80))
        XCTAssertEqual(topRightClamp.minX, frame.minX)
        XCTAssertEqual(topRightClamp.maxY, frame.maxY)

        let desired = CGRect(x: 200, y: 180, width: 200, height: 150)
        let enforcedSize = CGSize(width: 260, height: 210)
        XCTAssertEqual(
            WindowGeometry.adjustOriginAfterActualSize(desiredFrame: desired, actualSize: enforcedSize, corner: .topLeft),
            CGPoint(x: desired.maxX - 260, y: desired.maxY - 210)
        )
        XCTAssertEqual(
            WindowGeometry.adjustOriginAfterActualSize(desiredFrame: desired, actualSize: enforcedSize, corner: .topRight),
            CGPoint(x: desired.minX, y: desired.maxY - 210)
        )
        XCTAssertEqual(
            WindowGeometry.adjustOriginAfterActualSize(desiredFrame: desired, actualSize: enforcedSize, corner: .bottomLeft),
            CGPoint(x: desired.maxX - 260, y: desired.minY)
        )
        XCTAssertEqual(
            WindowGeometry.adjustOriginAfterActualSize(desiredFrame: desired, actualSize: enforcedSize, corner: .bottomRight),
            desired.origin
        )

        let origin = CGPoint(x: 500, y: 500)
        XCTAssertNil(WindowGeometry.splitGestureAction(startPointer: origin, currentPointer: CGPoint(x: 535, y: 520)))
        XCTAssertNil(WindowGeometry.splitGestureAction(startPointer: origin, currentPointer: CGPoint(x: 500, y: 465)))
        XCTAssertEqual(
            WindowGeometry.splitGestureAction(startPointer: origin, currentPointer: CGPoint(x: 460, y: 510)),
            .splitLeft
        )
        XCTAssertEqual(
            WindowGeometry.splitGestureAction(startPointer: origin, currentPointer: CGPoint(x: 540, y: 505)),
            .splitRight
        )
        XCTAssertEqual(
            WindowGeometry.splitGestureAction(startPointer: origin, currentPointer: CGPoint(x: 510, y: 460)),
            .enterFullScreen
        )
        XCTAssertEqual(
            WindowGeometry.splitGestureAction(startPointer: origin, currentPointer: CGPoint(x: 505, y: 545)),
            .exitFullScreenOrSplit
        )
    }

    func testStateMachinePointerMoveDeadzoneAndRelease() {
        let target = WindowTargetSnapshot(
            windowId: 101,
            pid: 4242,
            bundleId: "com.apple.Notes",
            frame: CGRect(x: 200, y: 150, width: 600, height: 400)
        )
        var machine = WindowResizeStateMachine(
            preferences: WindowResizePreferences(isEnabled: true)
        )
        let context = InputEventContext(frontmostBundleId: "com.apple.Safari")

        let initialHover = machine.handle(
            RoutedInputEvent(kind: .mouseMoved(location: CGPoint(x: 300, y: 250), modifiers: [])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(initialHover.decision, .passThrough)
        XCTAssertTrue(initialHover.effects.isEmpty)
        XCTAssertEqual(machine.state, .idle)

        let armed = machine.handle(
            RoutedInputEvent(kind: .flagsChanged(modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(armed.decision, .passThrough)
        XCTAssertTrue(armed.effects.isEmpty)
        XCTAssertEqual(
            machine.state,
            .armed(mode: .move, startPointer: CGPoint(x: 300, y: 250), suppressLeftButton: false)
        )

        let jitter = machine.handle(
            RoutedInputEvent(kind: .mouseMoved(location: CGPoint(x: 302, y: 251), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertTrue(jitter.effects.isEmpty)
        XCTAssertEqual(
            machine.state,
            .armed(mode: .move, startPointer: CGPoint(x: 300, y: 250), suppressLeftButton: false)
        )

        let promoted = machine.handle(
            RoutedInputEvent(kind: .mouseMoved(location: CGPoint(x: 320, y: 275), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(promoted.decision, .passThrough)
        XCTAssertEqual(
            promoted.effects,
            [
                .beginEnhancedUIGuard(pid: 4242),
                .applyMove(windowId: 101, origin: CGPoint(x: 220, y: 175)),
            ]
        )

        let released = machine.handle(
            RoutedInputEvent(kind: .flagsChanged(modifiers: [])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(released.effects, [.endEnhancedUIGuard(pid: 4242)])
        XCTAssertEqual(machine.state, .idle)
    }

    func testStateMachineLeftClickDragSuppressesClickWithoutActivating() {
        let target = WindowTargetSnapshot(
            windowId: 202,
            pid: 5150,
            bundleId: "com.apple.TextEdit",
            frame: CGRect(x: 100, y: 100, width: 500, height: 350)
        )
        var machine = WindowResizeStateMachine(
            preferences: WindowResizePreferences(isEnabled: true)
        )
        let context = InputEventContext(frontmostBundleId: "com.apple.Finder")

        let down = machine.handle(
            RoutedInputEvent(kind: .leftMouseDown(location: CGPoint(x: 200, y: 200), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(down.decision, .consume)
        XCTAssertEqual(down.effects, [.beginEnhancedUIGuard(pid: 5150)])

        let drag = machine.handle(
            RoutedInputEvent(kind: .leftMouseDragged(location: CGPoint(x: 245, y: 230), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(drag.decision, .consume)
        XCTAssertEqual(drag.effects, [.applyMove(windowId: 202, origin: CGPoint(x: 145, y: 130))])

        let up = machine.handle(
            RoutedInputEvent(kind: .leftMouseUp(location: CGPoint(x: 245, y: 230), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(up.decision, .consume)
        XCTAssertEqual(up.effects, [.endEnhancedUIGuard(pid: 5150)])
        XCTAssertEqual(machine.state, .idle)
    }

    func testStateMachineMidDragOptionToggleAndNearestCornerSelection() {
        let initialFrame = CGRect(x: 100, y: 100, width: 400, height: 300)
        var liveFrame = initialFrame
        let target = WindowTargetSnapshot(
            windowId: 303,
            pid: 6060,
            bundleId: "com.apple.Notes",
            frame: initialFrame
        )
        let context = InputEventContext(frontmostBundleId: "com.apple.Finder")

        var nearestOffMachine = WindowResizeStateMachine(
            preferences: WindowResizePreferences(isEnabled: true, nearestCornerResize: false)
        )
        _ = nearestOffMachine.handle(
            RoutedInputEvent(kind: .leftMouseDown(location: CGPoint(x: 120, y: 120), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in liveFrame }
        )
        let moved = nearestOffMachine.handle(
            RoutedInputEvent(kind: .leftMouseDragged(location: CGPoint(x: 170, y: 140), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in liveFrame }
        )
        XCTAssertEqual(moved.effects, [.applyMove(windowId: 303, origin: CGPoint(x: 150, y: 120))])
        liveFrame = CGRect(x: 150, y: 120, width: 400, height: 300)

        let switchToResize = nearestOffMachine.handle(
            RoutedInputEvent(kind: .flagsChanged(modifiers: [.fn, .control, .option])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in liveFrame }
        )
        XCTAssertTrue(switchToResize.effects.isEmpty)
        if case .resizing(_, let corner, let startPointer, let anchorFrame, _) = nearestOffMachine.state {
            XCTAssertEqual(corner, .bottomRight)
            XCTAssertEqual(startPointer, CGPoint(x: 170, y: 140))
            XCTAssertEqual(anchorFrame, liveFrame)
        } else {
            XCTFail("Expected resizing state after pressing Option mid-drag")
        }

        let resized = nearestOffMachine.handle(
            RoutedInputEvent(kind: .leftMouseDragged(location: CGPoint(x: 210, y: 190), modifiers: [.fn, .control, .option])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in liveFrame }
        )
        let expectedResizedFrame = CGRect(x: 150, y: 120, width: 440, height: 350)
        XCTAssertEqual(
            resized.effects,
            [.applyResize(windowId: 303, frame: expectedResizedFrame, corner: .bottomRight)]
        )
        liveFrame = expectedResizedFrame

        let switchBackToMove = nearestOffMachine.handle(
            RoutedInputEvent(kind: .flagsChanged(modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in liveFrame }
        )
        XCTAssertTrue(switchBackToMove.effects.isEmpty)
        if case .moving(_, let startPointer, let initialOrigin, _) = nearestOffMachine.state {
            XCTAssertEqual(startPointer, CGPoint(x: 210, y: 190))
            XCTAssertEqual(initialOrigin, liveFrame.origin)
        } else {
            XCTFail("Expected moving state after releasing Option mid-drag")
        }

        var nearestOnMachine = WindowResizeStateMachine(
            preferences: WindowResizePreferences(isEnabled: true, nearestCornerResize: true)
        )
        let start = nearestOnMachine.handle(
            RoutedInputEvent(kind: .leftMouseDown(location: CGPoint(x: 120, y: 120), modifiers: [.fn, .control, .option])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in initialFrame }
        )
        XCTAssertEqual(start.decision, .consume)
        if case .resizing(_, let corner, _, _, _) = nearestOnMachine.state {
            XCTAssertEqual(corner, .topLeft)
        } else {
            XCTFail("Expected nearest corner .topLeft when nearestCornerResize is enabled")
        }
    }

    func testStateMachineKeyboardPlacementCancelsArmedIgnoresRepeatsAndExcluded() {
        var machine = WindowResizeStateMachine(
            preferences: WindowResizePreferences(
                isEnabled: true,
                excludedBundleIds: ["com.apple.Terminal"]
            )
        )
        let normalContext = InputEventContext(frontmostBundleId: "com.apple.Safari")
        let excludedContext = InputEventContext(frontmostBundleId: "com.apple.Terminal")

        _ = machine.handle(
            RoutedInputEvent(kind: .mouseMoved(location: CGPoint(x: 300, y: 300), modifiers: [.fn, .control])),
            context: normalContext,
            resolveTarget: { _ in nil },
            currentFrame: { _ in nil }
        )
        XCTAssertEqual(
            machine.state,
            .armed(mode: .move, startPointer: CGPoint(x: 300, y: 300), suppressLeftButton: false)
        )

        let expectedActions: [(UInt16, WindowPlacementAction)] = [
            (UInt16(kVK_LeftArrow), .tileLeft),
            (UInt16(kVK_RightArrow), .tileRight),
            (UInt16(kVK_UpArrow), .tileTop),
            (UInt16(kVK_DownArrow), .tileBottom),
            (UInt16(kVK_ANSI_F), .fill),
            (UInt16(kVK_ANSI_C), .center),
            (UInt16(kVK_ANSI_R), .restorePrevious),
        ]

        for (keyCode, expected) in expectedActions {
            let step = machine.handle(
                RoutedInputEvent(kind: .keyDown(keyCode: keyCode, modifiers: [.fn, .control], isRepeat: false)),
                context: normalContext,
                resolveTarget: { _ in nil },
                currentFrame: { _ in nil }
            )
            XCTAssertEqual(step.decision, .consume)
            XCTAssertEqual(step.effects, [.executeKeyboardPlacement(action: expected)])
            XCTAssertEqual(machine.state, .idle)
        }

        let repeated = machine.handle(
            RoutedInputEvent(kind: .keyDown(keyCode: UInt16(kVK_ANSI_F), modifiers: [.fn, .control], isRepeat: true)),
            context: normalContext,
            resolveTarget: { _ in nil },
            currentFrame: { _ in nil }
        )
        XCTAssertEqual(repeated.decision, .consume)
        XCTAssertTrue(repeated.effects.isEmpty)

        let excluded = machine.handle(
            RoutedInputEvent(kind: .keyDown(keyCode: UInt16(kVK_ANSI_F), modifiers: [.fn, .control], isRepeat: false)),
            context: excludedContext,
            resolveTarget: { _ in nil },
            currentFrame: { _ in nil }
        )
        XCTAssertEqual(excluded.decision, .passThrough)
        XCTAssertTrue(excluded.effects.isEmpty)

        _ = machine.handle(
            RoutedInputEvent(kind: .flagsChanged(modifiers: [.control])),
            context: normalContext,
            resolveTarget: { _ in nil },
            currentFrame: { _ in nil }
        )
        let ctrlArrowWithoutPhysicalFn = machine.handle(
            RoutedInputEvent(kind: .keyDown(keyCode: UInt16(kVK_LeftArrow), modifiers: [.fn, .control], isRepeat: false)),
            context: normalContext,
            resolveTarget: { _ in nil },
            currentFrame: { _ in nil }
        )
        XCTAssertEqual(ctrlArrowWithoutPhysicalFn.decision, .passThrough)
        XCTAssertTrue(ctrlArrowWithoutPhysicalFn.effects.isEmpty)
    }

    func testStateMachineSplitGestureHUDCancelEscAndExecuteOnlyOnRelease() {
        let target = WindowTargetSnapshot(
            windowId: 777,
            pid: 9001,
            bundleId: "com.apple.Safari",
            frame: CGRect(x: 100, y: 100, width: 900, height: 600)
        )
        let splitMods: InputModifierSet = [.fn, .control, .option, .command]
        let context = InputEventContext(frontmostBundleId: "com.apple.Safari")
        var machine = WindowResizeStateMachine(
            preferences: WindowResizePreferences(isEnabled: true)
        )

        let start = machine.handle(
            RoutedInputEvent(kind: .mouseMoved(location: CGPoint(x: 400, y: 400), modifiers: splitMods)),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(
            start.effects,
            [.updateHUD(WindowResizeHUDState(isVisible: true, action: nil, anchorPoint: CGPoint(x: 400, y: 400)))]
        )

        let dragLeft = machine.handle(
            RoutedInputEvent(kind: .mouseMoved(location: CGPoint(x: 340, y: 400), modifiers: splitMods)),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(
            dragLeft.effects,
            [.updateHUD(WindowResizeHUDState(isVisible: true, action: .splitLeft, anchorPoint: CGPoint(x: 400, y: 400)))]
        )

        let escCancel = machine.handle(
            RoutedInputEvent(kind: .keyDown(keyCode: 53, modifiers: splitMods, isRepeat: false)),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(escCancel.decision, .consume)
        XCTAssertEqual(escCancel.effects, [.updateHUD(.hidden)])
        XCTAssertEqual(machine.state, .idle)

        let releaseAfterEsc = machine.handle(
            RoutedInputEvent(kind: .flagsChanged(modifiers: [])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertTrue(releaseAfterEsc.effects.isEmpty)

        _ = machine.handle(
            RoutedInputEvent(kind: .mouseMoved(location: CGPoint(x: 400, y: 400), modifiers: splitMods)),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        _ = machine.handle(
            RoutedInputEvent(kind: .mouseMoved(location: CGPoint(x: 400, y: 340), modifiers: splitMods)),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        let releaseFullScreen = machine.handle(
            RoutedInputEvent(kind: .flagsChanged(modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(
            releaseFullScreen.effects,
            [
                .updateHUD(.hidden),
                .executeSplitGesture(windowId: 777, action: .enterFullScreen),
            ]
        )
        XCTAssertEqual(machine.state, .idle)

        _ = machine.handle(
            RoutedInputEvent(kind: .flagsChanged(modifiers: [])),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        let leftDownSplit = machine.handle(
            RoutedInputEvent(kind: .leftMouseDown(location: CGPoint(x: 400, y: 400), modifiers: splitMods)),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(leftDownSplit.decision, .consume)
        let dragDownExit = machine.handle(
            RoutedInputEvent(kind: .leftMouseDragged(location: CGPoint(x: 400, y: 460), modifiers: splitMods)),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(dragDownExit.decision, .consume)
        XCTAssertEqual(
            dragDownExit.effects,
            [.updateHUD(WindowResizeHUDState(isVisible: true, action: .exitFullScreenOrSplit, anchorPoint: CGPoint(x: 400, y: 400)))]
        )
        let leftUpApply = machine.handle(
            RoutedInputEvent(kind: .leftMouseUp(location: CGPoint(x: 400, y: 460), modifiers: splitMods)),
            context: context,
            resolveTarget: { _ in target },
            currentFrame: { _ in target.frame }
        )
        XCTAssertEqual(leftUpApply.decision, .consume)
        XCTAssertEqual(
            leftUpApply.effects,
            [
                .updateHUD(.hidden),
                .executeSplitGesture(windowId: 777, action: .exitFullScreenOrSplit),
            ]
        )
    }

    func testStateMachineExcludedAppsNonMovableNonResizableAndReset() {
        let excludedTarget = WindowTargetSnapshot(
            windowId: 11,
            pid: 1001,
            bundleId: "com.apple.Terminal",
            frame: CGRect(x: 50, y: 50, width: 500, height: 400)
        )
        let fixedTarget = WindowTargetSnapshot(
            windowId: 12,
            pid: 1002,
            bundleId: "com.apple.Calculator",
            frame: CGRect(x: 50, y: 50, width: 240, height: 320),
            isMovable: false,
            isResizable: false
        )
        var machine = WindowResizeStateMachine(
            preferences: WindowResizePreferences(
                isEnabled: true,
                excludedBundleIds: ["com.apple.terminal"]
            )
        )
        let context = InputEventContext(frontmostBundleId: "com.apple.Safari")

        let excludedMove = machine.handle(
            RoutedInputEvent(kind: .leftMouseDown(location: CGPoint(x: 100, y: 100), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in excludedTarget },
            currentFrame: { _ in excludedTarget.frame }
        )
        XCTAssertEqual(excludedMove.decision, .passThrough)
        XCTAssertTrue(excludedMove.effects.isEmpty)
        XCTAssertEqual(machine.state, .idle)

        let fixedMove = machine.handle(
            RoutedInputEvent(kind: .leftMouseDown(location: CGPoint(x: 100, y: 100), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in fixedTarget },
            currentFrame: { _ in fixedTarget.frame }
        )
        XCTAssertEqual(fixedMove.decision, .passThrough)
        XCTAssertTrue(fixedMove.effects.isEmpty)
        XCTAssertEqual(machine.state, .idle)

        let fixedResize = machine.handle(
            RoutedInputEvent(kind: .leftMouseDown(location: CGPoint(x: 100, y: 100), modifiers: [.fn, .control, .option])),
            context: context,
            resolveTarget: { _ in fixedTarget },
            currentFrame: { _ in fixedTarget.frame }
        )
        XCTAssertEqual(fixedResize.decision, .passThrough)
        XCTAssertTrue(fixedResize.effects.isEmpty)
        XCTAssertEqual(machine.state, .idle)

        let movableTarget = WindowTargetSnapshot(
            windowId: 13,
            pid: 1003,
            bundleId: "com.apple.Notes",
            frame: CGRect(x: 50, y: 50, width: 500, height: 400)
        )
        _ = machine.handle(
            RoutedInputEvent(kind: .leftMouseDown(location: CGPoint(x: 100, y: 100), modifiers: [.fn, .control])),
            context: context,
            resolveTarget: { _ in movableTarget },
            currentFrame: { _ in movableTarget.frame }
        )
        let resetEffects = machine.reset()
        XCTAssertEqual(resetEffects, [.endEnhancedUIGuard(pid: 1003)])
        XCTAssertEqual(machine.state, .idle)
    }

    @MainActor
    func testConsumerWithDesktopInputHubDefaultOffPermissionsAndDiagnostics() async {
        let permissions = StubPermissionChecker(accessibility: false, inputMonitoring: false)
        let carbon = StubCarbonBackend()
        let transport = StubEventTapTransport()
        let driver = FakeWindowDriver()
        let target = WindowTargetSnapshot(
            windowId: 99,
            pid: 7777,
            bundleId: "com.apple.Notes",
            frame: CGRect(x: 100, y: 100, width: 500, height: 400)
        )
        driver.setTarget(target)
        driver.setPlacementResult(.unsupportedByTargetApp(detail: "No Move & Resize menu"))

        let statusStore = WindowResizeStatusStore()
        var receivedHUDStates: [WindowResizeHUDState] = []

        let consumer = WindowResizeConsumer(
            preferences: WindowResizePreferences(),
            driver: driver,
            excludingProcessId: 99999,
            onHUDStateChange: { state in
                receivedHUDStates.append(state)
            },
            onDiagnosticResult: { result in
                statusStore.record(result)
            }
        )

        let hub = DesktopInputHub(
            environment: [:],
            carbonBackend: carbon,
            eventTapTransport: transport,
            permissionService: permissions,
            symbolicHotKeyProvider: EmptySymbolicHotKeyProvider()
        )

        var syncPanelHits = 0
        XCTAssertTrue(
            hub.registerCarbonHotKey(slot: .syncPanel, featureSlot: .syncPanel, shortcut: .defaultShortcut) {
                syncPanelHits += 1
            }
        )
        hub.registerConsumer(consumer)

        XCTAssertFalse(consumer.isActive)
        XCTAssertEqual(hub.eventTapStatus, .inactive)
        XCTAssertFalse(transport.isRunning)
        XCTAssertTrue(transport.startMasks.isEmpty)
        XCTAssertEqual(permissions.promptCount, 0)

        consumer.updatePreferences(WindowResizePreferences(isEnabled: true))
        XCTAssertTrue(consumer.isActive)
        XCTAssertEqual(hub.syncTapState(), .pausedMissingPermission)
        XCTAssertFalse(transport.isRunning)
        XCTAssertEqual(permissions.promptCount, 0)

        carbon.trigger(slot: .syncPanel)
        XCTAssertEqual(syncPanelHits, 1)

        permissions.setPermissions(accessibility: true, inputMonitoring: true)
        _ = hub.refreshPermissions()
        XCTAssertEqual(hub.eventTapStatus, .active)
        XCTAssertTrue(transport.isRunning)

        let tileDecision = transport.send(
            RoutedInputEvent(kind: .keyDown(keyCode: UInt16(kVK_ANSI_F), modifiers: [.fn, .control], isRepeat: false)),
            context: InputEventContext(frontmostBundleId: "com.apple.Notes")
        )
        XCTAssertEqual(tileDecision, .consume)
        consumer.drainActionQueueForTesting()
        await Task.yield()
        await Task.yield()

        XCTAssertEqual(driver.placements, [.fill])
        XCTAssertEqual(
            statusStore.lastResult,
            .unsupportedByTargetApp(detail: "No Move & Resize menu")
        )
        XCTAssertNotNil(statusStore.diagnosticMessage(in: .en))

        let dragDown = transport.send(
            RoutedInputEvent(kind: .leftMouseDown(location: CGPoint(x: 200, y: 200), modifiers: [.fn, .control])),
            context: InputEventContext(frontmostBundleId: "com.apple.Notes")
        )
        XCTAssertEqual(dragDown, .consume)
        let dragMove = transport.send(
            RoutedInputEvent(kind: .leftMouseDragged(location: CGPoint(x: 250, y: 230), modifiers: [.fn, .control])),
            context: InputEventContext(frontmostBundleId: "com.apple.Notes")
        )
        XCTAssertEqual(dragMove, .consume)
        transport.triggerReset()
        consumer.drainActionQueueForTesting()

        XCTAssertEqual(driver.guardEvents, ["begin:7777", "end:7777"])
        XCTAssertEqual(driver.moves.last?.1, CGPoint(x: 150, y: 130))
        XCTAssertEqual(consumer.currentSessionState, .idle)

        let splitStart = transport.send(
            RoutedInputEvent(
                kind: .mouseMoved(location: CGPoint(x: 300, y: 300), modifiers: [.fn, .control, .option, .command])
            ),
            context: InputEventContext(frontmostBundleId: "com.apple.Notes")
        )
        XCTAssertEqual(splitStart, .passThrough)
        transport.triggerReset()
        await Task.yield()
        await Task.yield()
        XCTAssertEqual(receivedHUDStates.last, .hidden)

        let conflicts = hub.detectConflicts(
            extraBindings: WindowResizeShortcutCatalog.bindings(),
            checkNativeTilingOverlap: true
        )
        XCTAssertEqual(
            conflicts.filter { $0.owner == .windowResize && $0.kind == .macOSNativeTilingOverlap }.count,
            7
        )
    }
}
