import CoreGraphics
import Foundation
import os

public final class WindowResizeConsumer: DesktopInputConsumer, @unchecked Sendable {
    private enum PendingGeometryOperation: Sendable {
        case move(windowId: UInt64, origin: CGPoint)
        case resize(windowId: UInt64, frame: CGRect, corner: ResizeAnchorCorner)
    }

    private struct MachineState {
        var stateMachine: WindowResizeStateMachine
        var onHUDStateChange: (@MainActor @Sendable (WindowResizeHUDState) -> Void)?
        var onDiagnosticResult: (@MainActor @Sendable (WindowResizeActionResult) -> Void)?
    }

    private struct GeometryState {
        var pending: PendingGeometryOperation?
        var drainScheduled: Bool = false
    }

    public let slot: DesktopFeatureSlot = .windowResize
    public let priority: Int = 10
    public let requiredEvents: EventTapRequirement = [.flagsChanged, .keyDown, .mouseMoved, .leftMouse]

    private let driver: any WindowSystemDriving
    private let excludingProcessId: pid_t
    private let lock: OSAllocatedUnfairLock<MachineState>
    private let geometryLock = OSAllocatedUnfairLock(initialState: GeometryState())
    private let actionQueue = DispatchQueue(
        label: "dev.semanticcraft.codex-agy-sync.window-resize",
        qos: .utility
    )

    public init(
        preferences: WindowResizePreferences = WindowResizePreferences(),
        driver: any WindowSystemDriving = SystemAXWindowDriver(),
        excludingProcessId: pid_t = ProcessInfo.processInfo.processIdentifier,
        onHUDStateChange: (@MainActor @Sendable (WindowResizeHUDState) -> Void)? = nil,
        onDiagnosticResult: (@MainActor @Sendable (WindowResizeActionResult) -> Void)? = nil
    ) {
        self.driver = driver
        self.excludingProcessId = excludingProcessId
        self.lock = OSAllocatedUnfairLock(
            initialState: MachineState(
                stateMachine: WindowResizeStateMachine(preferences: preferences),
                onHUDStateChange: onHUDStateChange,
                onDiagnosticResult: onDiagnosticResult
            )
        )
    }

    public var isActive: Bool {
        lock.withLock { $0.stateMachine.preferences.isEnabled }
    }

    public var preferences: WindowResizePreferences {
        lock.withLock { $0.stateMachine.preferences }
    }

    public var currentSessionState: WindowResizeSessionState {
        lock.withLock { $0.stateMachine.state }
    }

    public func setHandlers(
        onHUDStateChange: (@MainActor @Sendable (WindowResizeHUDState) -> Void)? = nil,
        onDiagnosticResult: (@MainActor @Sendable (WindowResizeActionResult) -> Void)? = nil
    ) {
        lock.withLock { state in
            state.onHUDStateChange = onHUDStateChange
            state.onDiagnosticResult = onDiagnosticResult
        }
    }

    public func updatePreferences(_ newPreferences: WindowResizePreferences) {
        let (effects, hudHandler, diagnosticHandler) = lock.withLock { state in
            let cleanup = state.stateMachine.updatePreferences(newPreferences)
            return (cleanup, state.onHUDStateChange, state.onDiagnosticResult)
        }
        if !effects.isEmpty {
            execute(effects: effects, hudHandler: hudHandler, diagnosticHandler: diagnosticHandler)
        }
    }

    public func handle(_ event: RoutedInputEvent, context: InputEventContext) -> InputRoutingDecision {
        let driver = self.driver
        let excludedPid = self.excludingProcessId
        let (result, hudHandler, diagnosticHandler) = lock.withLock { state in
            let step = state.stateMachine.handle(
                event,
                context: context,
                resolveTarget: { point in
                    driver.windowAtPointer(point, excludingProcessId: excludedPid)
                },
                currentFrame: { windowId in
                    driver.currentFrame(for: windowId)
                }
            )
            return (step, state.onHUDStateChange, state.onDiagnosticResult)
        }
        if !result.effects.isEmpty {
            execute(effects: result.effects, hudHandler: hudHandler, diagnosticHandler: diagnosticHandler)
        }
        return result.decision
    }

    public func handleTapReset() {
        let (effects, hudHandler, diagnosticHandler) = lock.withLock { state in
            let cleanup = state.stateMachine.reset()
            return (cleanup, state.onHUDStateChange, state.onDiagnosticResult)
        }
        if !effects.isEmpty {
            execute(effects: effects, hudHandler: hudHandler, diagnosticHandler: diagnosticHandler)
        }
    }

    public func drainActionQueueForTesting() {
        actionQueue.sync {}
    }

    private func execute(
        effects: [WindowResizeEffect],
        hudHandler: (@MainActor @Sendable (WindowResizeHUDState) -> Void)?,
        diagnosticHandler: (@MainActor @Sendable (WindowResizeActionResult) -> Void)?
    ) {
        let driver = self.driver

        for effect in effects {
            switch effect {
            case .beginEnhancedUIGuard(let pid):
                actionQueue.async {
                    driver.beginEnhancedUIGuard(pid: pid)
                }

            case .endEnhancedUIGuard(let pid):
                actionQueue.async {
                    driver.endEnhancedUIGuard(pid: pid)
                }

            case .applyMove(let windowId, let origin):
                enqueueGeometryOperation(.move(windowId: windowId, origin: origin))

            case .applyResize(let windowId, let frame, let corner):
                enqueueGeometryOperation(.resize(windowId: windowId, frame: frame, corner: corner))

            case .executeKeyboardPlacement(let action):
                actionQueue.async {
                    let result = driver.performNativePlacement(action)
                    if let diagnosticHandler {
                        Task { @MainActor in
                            diagnosticHandler(result)
                        }
                    }
                }

            case .executeSplitGesture(let windowId, let action):
                actionQueue.async {
                    let result = driver.performSplitOrFullScreen(action, windowId: windowId)
                    if let diagnosticHandler {
                        Task { @MainActor in
                            diagnosticHandler(result)
                        }
                    }
                }

            case .updateHUD(let hudState):
                if let hudHandler {
                    Task { @MainActor in
                        hudHandler(hudState)
                    }
                }
            }
        }
    }

    private func enqueueGeometryOperation(_ operation: PendingGeometryOperation) {
        let shouldSchedule = geometryLock.withLock { state -> Bool in
            state.pending = operation
            if state.drainScheduled {
                return false
            }
            state.drainScheduled = true
            return true
        }
        guard shouldSchedule else { return }

        let driver = self.driver
        let geometryLock = self.geometryLock
        actionQueue.async {
            while true {
                let nextOperation = geometryLock.withLock { state -> PendingGeometryOperation? in
                    if let pending = state.pending {
                        state.pending = nil
                        return pending
                    }
                    state.drainScheduled = false
                    return nil
                }
                guard let nextOperation else { break }
                switch nextOperation {
                case .move(let windowId, let origin):
                    _ = driver.moveWindow(windowId: windowId, to: origin)
                case .resize(let windowId, let frame, let corner):
                    _ = driver.resizeWindow(windowId: windowId, to: frame, corner: corner)
                }
            }
        }
    }
}
