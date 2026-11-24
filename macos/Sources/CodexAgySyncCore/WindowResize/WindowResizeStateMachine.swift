import CoreGraphics
import Foundation

public enum ArmedMode: Equatable, Sendable {
    case move
    case resize
}

public enum WindowResizeSessionState: Equatable, Sendable {
    case idle
    case armed(mode: ArmedMode, startPointer: CGPoint, suppressLeftButton: Bool)
    case moving(target: WindowTargetSnapshot, startPointer: CGPoint, initialOrigin: CGPoint, suppressLeftButton: Bool)
    case resizing(
        target: WindowTargetSnapshot,
        corner: ResizeAnchorCorner,
        startPointer: CGPoint,
        initialFrame: CGRect,
        suppressLeftButton: Bool
    )
    case splitGesture(
        target: WindowTargetSnapshot,
        startPointer: CGPoint,
        pendingAction: SplitScreenGestureAction?,
        suppressLeftButton: Bool
    )
}

public enum WindowResizeEffect: Equatable, Sendable {
    case beginEnhancedUIGuard(pid: pid_t)
    case endEnhancedUIGuard(pid: pid_t)
    case applyMove(windowId: UInt64, origin: CGPoint)
    case applyResize(windowId: UInt64, frame: CGRect, corner: ResizeAnchorCorner)
    case executeKeyboardPlacement(action: WindowPlacementAction)
    case executeSplitGesture(windowId: UInt64, action: SplitScreenGestureAction)
    case updateHUD(WindowResizeHUDState)
}

public struct WindowResizeStepResult: Equatable, Sendable {
    public let decision: InputRoutingDecision
    public let effects: [WindowResizeEffect]

    public init(decision: InputRoutingDecision, effects: [WindowResizeEffect] = []) {
        self.decision = decision
        self.effects = effects
    }
}

public struct WindowResizeStateMachine: Sendable {
    public static let defaultArmedDeadzone: CGFloat = 4
    public static let defaultSplitDeadzone: CGFloat = WindowGeometry.defaultSplitDeadzone
    private static let escapeKeyCode: UInt16 = 53
    private static let moveModifiers: InputModifierSet = [.fn, .control]
    private static let resizeModifiers: InputModifierSet = [.fn, .control, .option]
    private static let splitModifiers: InputModifierSet = [.fn, .control, .option, .command]

    public private(set) var state: WindowResizeSessionState = .idle
    public var preferences: WindowResizePreferences
    public let armedDeadzone: CGFloat
    public let splitDeadzone: CGFloat

    private var lastPointer: CGPoint?
    private var lastObservedFlags: InputModifierSet?
    private var guardedPid: pid_t?
    private var hudVisible: Bool = false
    private var suppressSplitUntilRelease: Bool = false
    private var suppressArmedUntilModifierChange: Bool = false
    private var consumeNextLeftMouseUp: Bool = false

    public init(
        preferences: WindowResizePreferences = WindowResizePreferences(),
        armedDeadzone: CGFloat = defaultArmedDeadzone,
        splitDeadzone: CGFloat = defaultSplitDeadzone
    ) {
        self.preferences = preferences
        self.armedDeadzone = max(0, armedDeadzone)
        self.splitDeadzone = max(0, splitDeadzone)
    }

    public mutating func updatePreferences(_ newPreferences: WindowResizePreferences) -> [WindowResizeEffect] {
        preferences = newPreferences
        if !newPreferences.isEnabled {
            return reset()
        }
        return []
    }

    public mutating func reset() -> [WindowResizeEffect] {
        var effects: [WindowResizeEffect] = []
        endGuardIfNeeded(into: &effects)
        hideHUDIfNeeded(into: &effects)
        state = .idle
        lastObservedFlags = nil
        suppressSplitUntilRelease = false
        suppressArmedUntilModifierChange = false
        consumeNextLeftMouseUp = false
        return effects
    }

    public mutating func handle(
        _ event: RoutedInputEvent,
        context: InputEventContext,
        resolveTarget: (CGPoint) -> WindowTargetSnapshot?,
        currentFrame: (UInt64) -> CGRect?
    ) -> WindowResizeStepResult {
        guard preferences.isEnabled else {
            let cleanup = state == .idle && guardedPid == nil && !hudVisible ? [] : reset()
            return WindowResizeStepResult(decision: .passThrough, effects: cleanup)
        }

        switch event.kind {
        case .flagsChanged(let modifiers):
            return handleFlagsChanged(
                modifiers: modifiers,
                context: context,
                resolveTarget: resolveTarget,
                currentFrame: currentFrame
            )
        case .keyDown(let keyCode, let modifiers, let isRepeat):
            return handleKeyDown(
                keyCode: keyCode,
                modifiers: modifiers,
                isRepeat: isRepeat,
                context: context
            )
        case .keyUp:
            return WindowResizeStepResult(decision: .passThrough)
        case .mouseMoved(let location, let modifiers):
            return handlePointerMoved(
                location: location,
                modifiers: modifiers,
                isLeftDrag: false,
                context: context,
                resolveTarget: resolveTarget,
                currentFrame: currentFrame
            )
        case .leftMouseDown(let location, let modifiers):
            return handleLeftMouseDown(
                location: location,
                modifiers: modifiers,
                context: context,
                resolveTarget: resolveTarget
            )
        case .leftMouseDragged(let location, let modifiers):
            return handlePointerMoved(
                location: location,
                modifiers: modifiers,
                isLeftDrag: true,
                context: context,
                resolveTarget: resolveTarget,
                currentFrame: currentFrame
            )
        case .leftMouseUp(let location, _):
            return handleLeftMouseUp(location: location)
        }
    }

    private mutating func handleKeyDown(
        keyCode: UInt16,
        modifiers: InputModifierSet,
        isRepeat: Bool,
        context: InputEventContext
    ) -> WindowResizeStepResult {
        var effects: [WindowResizeEffect] = []

        if case .splitGesture(_, _, _, let suppressLeftButton) = state, keyCode == Self.escapeKeyCode {
            hideHUDIfNeeded(into: &effects)
            state = .idle
            suppressSplitUntilRelease = true
            if suppressLeftButton {
                consumeNextLeftMouseUp = true
            }
            return WindowResizeStepResult(decision: .consume, effects: effects)
        }

        guard modifiers == Self.moveModifiers, let action = WindowPlacementAction(keyCode: keyCode) else {
            return WindowResizeStepResult(decision: .passThrough)
        }

        if action.isArrowKey,
           let observedFlags = lastObservedFlags,
           observedFlags.contains(.control),
           !observedFlags.contains(.fn)
        {
            return WindowResizeStepResult(decision: .passThrough)
        }

        if ExcludedAppMatcher.isExcluded(
            bundleId: context.frontmostBundleId,
            in: preferences.excludedBundleIds
        ) {
            if case .armed = state {
                state = .idle
            }
            return WindowResizeStepResult(decision: .passThrough)
        }

        if case .armed = state {
            state = .idle
        }
        suppressArmedUntilModifierChange = true

        if isRepeat {
            return WindowResizeStepResult(decision: .consume)
        }

        effects.append(.executeKeyboardPlacement(action: action))
        return WindowResizeStepResult(decision: .consume, effects: effects)
    }

    private mutating func handleFlagsChanged(
        modifiers: InputModifierSet,
        context: InputEventContext,
        resolveTarget: (CGPoint) -> WindowTargetSnapshot?,
        currentFrame: (UInt64) -> CGRect?
    ) -> WindowResizeStepResult {
        var effects: [WindowResizeEffect] = []
        lastObservedFlags = modifiers

        if modifiers != Self.splitModifiers {
            suppressSplitUntilRelease = false
        }
        if modifiers != Self.moveModifiers {
            suppressArmedUntilModifierChange = false
        }

        switch state {
        case .idle:
            if modifiers == Self.moveModifiers, !suppressArmedUntilModifierChange, let pointer = lastPointer {
                state = .armed(mode: .move, startPointer: pointer, suppressLeftButton: false)
            } else if modifiers == Self.resizeModifiers, let pointer = lastPointer {
                state = .armed(mode: .resize, startPointer: pointer, suppressLeftButton: false)
            } else if modifiers == Self.splitModifiers, !suppressSplitUntilRelease, let pointer = lastPointer {
                _ = tryBeginSplitGesture(
                    at: pointer,
                    suppressLeftButton: false,
                    context: context,
                    resolveTarget: resolveTarget,
                    effects: &effects
                )
            }

        case .armed(_, let startPointer, let suppressLeftButton):
            if modifiers == Self.moveModifiers, !suppressArmedUntilModifierChange {
                state = .armed(mode: .move, startPointer: lastPointer ?? startPointer, suppressLeftButton: suppressLeftButton)
            } else if modifiers == Self.resizeModifiers {
                state = .armed(mode: .resize, startPointer: lastPointer ?? startPointer, suppressLeftButton: suppressLeftButton)
            } else if modifiers == Self.splitModifiers, !suppressSplitUntilRelease {
                state = .idle
                _ = tryBeginSplitGesture(
                    at: lastPointer ?? startPointer,
                    suppressLeftButton: suppressLeftButton,
                    context: context,
                    resolveTarget: resolveTarget,
                    effects: &effects
                )
            } else {
                state = .idle
                if suppressLeftButton {
                    consumeNextLeftMouseUp = true
                }
            }

        case .moving(let target, _, _, let suppressLeftButton):
            if modifiers == Self.moveModifiers {
                break
            } else if modifiers == Self.resizeModifiers {
                transitionMoveToResize(
                    target: target,
                    pointer: lastPointer ?? CGPoint(x: target.frame.midX, y: target.frame.midY),
                    suppressLeftButton: suppressLeftButton,
                    currentFrame: currentFrame,
                    effects: &effects
                )
            } else if modifiers == Self.splitModifiers, !suppressSplitUntilRelease {
                endGuardIfNeeded(into: &effects)
                state = .idle
                if let pointer = lastPointer {
                    _ = tryBeginSplitGesture(
                        at: pointer,
                        suppressLeftButton: suppressLeftButton,
                        context: context,
                        resolveTarget: resolveTarget,
                        effects: &effects
                    )
                }
            } else {
                endGuardIfNeeded(into: &effects)
                state = .idle
                if suppressLeftButton {
                    consumeNextLeftMouseUp = true
                }
            }

        case .resizing(let target, _, _, _, let suppressLeftButton):
            if modifiers == Self.resizeModifiers {
                break
            } else if modifiers == Self.moveModifiers {
                transitionResizeToMove(
                    target: target,
                    pointer: lastPointer ?? target.frame.origin,
                    suppressLeftButton: suppressLeftButton,
                    currentFrame: currentFrame,
                    effects: &effects
                )
            } else if modifiers == Self.splitModifiers, !suppressSplitUntilRelease {
                endGuardIfNeeded(into: &effects)
                state = .idle
                if let pointer = lastPointer {
                    _ = tryBeginSplitGesture(
                        at: pointer,
                        suppressLeftButton: suppressLeftButton,
                        context: context,
                        resolveTarget: resolveTarget,
                        effects: &effects
                    )
                }
            } else {
                endGuardIfNeeded(into: &effects)
                state = .idle
                if suppressLeftButton {
                    consumeNextLeftMouseUp = true
                }
            }

        case .splitGesture(let target, _, let pendingAction, let suppressLeftButton):
            if modifiers == Self.splitModifiers {
                break
            }
            finishSplitGesture(
                target: target,
                pendingAction: pendingAction,
                effects: &effects
            )
            if modifiers == Self.moveModifiers {
                suppressArmedUntilModifierChange = true
            }
            if suppressLeftButton {
                consumeNextLeftMouseUp = true
            }
        }

        return WindowResizeStepResult(decision: .passThrough, effects: effects)
    }

    private mutating func handleLeftMouseDown(
        location: CGPoint,
        modifiers: InputModifierSet,
        context: InputEventContext,
        resolveTarget: (CGPoint) -> WindowTargetSnapshot?
    ) -> WindowResizeStepResult {
        var effects: [WindowResizeEffect] = []
        lastPointer = location
        consumeNextLeftMouseUp = false

        if modifiers == Self.moveModifiers {
            if case .moving(let target, let startPointer, let initialOrigin, _) = state {
                state = .moving(
                    target: target,
                    startPointer: startPointer,
                    initialOrigin: initialOrigin,
                    suppressLeftButton: true
                )
                return WindowResizeStepResult(decision: .consume)
            }
            let started = tryBeginMove(
                at: location,
                currentPointer: location,
                suppressLeftButton: true,
                applyImmediately: false,
                context: context,
                resolveTarget: resolveTarget,
                effects: &effects
            )
            return WindowResizeStepResult(decision: started ? .consume : .passThrough, effects: effects)
        }

        if modifiers == Self.resizeModifiers {
            if case .resizing(let target, let corner, let startPointer, let initialFrame, _) = state {
                state = .resizing(
                    target: target,
                    corner: corner,
                    startPointer: startPointer,
                    initialFrame: initialFrame,
                    suppressLeftButton: true
                )
                return WindowResizeStepResult(decision: .consume)
            }
            let started = tryBeginResize(
                at: location,
                currentPointer: location,
                suppressLeftButton: true,
                applyImmediately: false,
                context: context,
                resolveTarget: resolveTarget,
                effects: &effects
            )
            return WindowResizeStepResult(decision: started ? .consume : .passThrough, effects: effects)
        }

        if modifiers == Self.splitModifiers, !suppressSplitUntilRelease {
            if case .splitGesture(let target, _, _, _) = state {
                state = .splitGesture(
                    target: target,
                    startPointer: location,
                    pendingAction: nil,
                    suppressLeftButton: true
                )
                emitHUD(
                    WindowResizeHUDState(isVisible: true, action: nil, anchorPoint: location),
                    into: &effects
                )
                return WindowResizeStepResult(decision: .consume, effects: effects)
            }
            let started = tryBeginSplitGesture(
                at: location,
                suppressLeftButton: true,
                context: context,
                resolveTarget: resolveTarget,
                effects: &effects
            )
            return WindowResizeStepResult(decision: started ? .consume : .passThrough, effects: effects)
        }

        return WindowResizeStepResult(decision: .passThrough)
    }

    private mutating func handlePointerMoved(
        location: CGPoint,
        modifiers: InputModifierSet,
        isLeftDrag: Bool,
        context: InputEventContext,
        resolveTarget: (CGPoint) -> WindowTargetSnapshot?,
        currentFrame: (UInt64) -> CGRect?
    ) -> WindowResizeStepResult {
        var effects: [WindowResizeEffect] = []
        lastPointer = location

        switch state {
        case .idle:
            guard !isLeftDrag else {
                return WindowResizeStepResult(decision: .passThrough)
            }
            if modifiers == Self.moveModifiers, !suppressArmedUntilModifierChange {
                state = .armed(mode: .move, startPointer: location, suppressLeftButton: false)
            } else if modifiers == Self.resizeModifiers {
                state = .armed(mode: .resize, startPointer: location, suppressLeftButton: false)
            } else if modifiers == Self.splitModifiers, !suppressSplitUntilRelease {
                _ = tryBeginSplitGesture(
                    at: location,
                    suppressLeftButton: false,
                    context: context,
                    resolveTarget: resolveTarget,
                    effects: &effects
                )
            }
            return WindowResizeStepResult(decision: .passThrough, effects: effects)

        case .armed(_, let startPointer, let suppressLeftButton):
            if modifiers == Self.splitModifiers, !suppressSplitUntilRelease {
                state = .idle
                _ = tryBeginSplitGesture(
                    at: startPointer,
                    suppressLeftButton: suppressLeftButton,
                    context: context,
                    resolveTarget: resolveTarget,
                    effects: &effects
                )
                if case .splitGesture = state {
                    updateSplitGesturePointer(location: location, effects: &effects)
                }
                let decision: InputRoutingDecision = (isLeftDrag && suppressLeftButton) ? .consume : .passThrough
                return WindowResizeStepResult(decision: decision, effects: effects)
            }

            let targetMode: ArmedMode
            if modifiers == Self.moveModifiers, !suppressArmedUntilModifierChange {
                targetMode = .move
            } else if modifiers == Self.resizeModifiers {
                targetMode = .resize
            } else {
                state = .idle
                return WindowResizeStepResult(decision: .passThrough)
            }

            let distance = hypot(location.x - startPointer.x, location.y - startPointer.y)
            if distance < armedDeadzone {
                state = .armed(mode: targetMode, startPointer: startPointer, suppressLeftButton: suppressLeftButton)
                let decision: InputRoutingDecision = (isLeftDrag && suppressLeftButton) ? .consume : .passThrough
                return WindowResizeStepResult(decision: decision)
            }

            switch targetMode {
            case .move:
                _ = tryBeginMove(
                    at: startPointer,
                    currentPointer: location,
                    suppressLeftButton: suppressLeftButton,
                    applyImmediately: true,
                    context: context,
                    resolveTarget: resolveTarget,
                    effects: &effects
                )
            case .resize:
                _ = tryBeginResize(
                    at: startPointer,
                    currentPointer: location,
                    suppressLeftButton: suppressLeftButton,
                    applyImmediately: true,
                    context: context,
                    resolveTarget: resolveTarget,
                    effects: &effects
                )
            }

            let decision: InputRoutingDecision = (isLeftDrag && suppressLeftButton) ? .consume : .passThrough
            return WindowResizeStepResult(decision: decision, effects: effects)

        case .moving(let target, let startPointer, let initialOrigin, let suppressLeftButton):
            if modifiers == Self.resizeModifiers {
                transitionMoveToResize(
                    target: target,
                    pointer: location,
                    suppressLeftButton: suppressLeftButton,
                    currentFrame: currentFrame,
                    effects: &effects
                )
                let decision: InputRoutingDecision = (isLeftDrag && suppressLeftButton) ? .consume : .passThrough
                return WindowResizeStepResult(decision: decision, effects: effects)
            }
            guard modifiers == Self.moveModifiers else {
                endGuardIfNeeded(into: &effects)
                state = .idle
                if suppressLeftButton {
                    consumeNextLeftMouseUp = true
                }
                return WindowResizeStepResult(decision: .passThrough, effects: effects)
            }
            let nextOrigin = WindowGeometry.movedOrigin(
                initialOrigin: initialOrigin,
                startPointer: startPointer,
                currentPointer: location
            )
            let updatedFrame = CGRect(origin: nextOrigin, size: target.frame.size)
            state = .moving(
                target: target.updatingFrame(updatedFrame),
                startPointer: startPointer,
                initialOrigin: initialOrigin,
                suppressLeftButton: suppressLeftButton
            )
            effects.append(.applyMove(windowId: target.windowId, origin: nextOrigin))
            let decision: InputRoutingDecision = (isLeftDrag && suppressLeftButton) ? .consume : .passThrough
            return WindowResizeStepResult(decision: decision, effects: effects)

        case .resizing(let target, let corner, let startPointer, let initialFrame, let suppressLeftButton):
            if modifiers == Self.moveModifiers {
                transitionResizeToMove(
                    target: target,
                    pointer: location,
                    suppressLeftButton: suppressLeftButton,
                    currentFrame: currentFrame,
                    effects: &effects
                )
                let decision: InputRoutingDecision = (isLeftDrag && suppressLeftButton) ? .consume : .passThrough
                return WindowResizeStepResult(decision: decision, effects: effects)
            }
            guard modifiers == Self.resizeModifiers else {
                endGuardIfNeeded(into: &effects)
                state = .idle
                if suppressLeftButton {
                    consumeNextLeftMouseUp = true
                }
                return WindowResizeStepResult(decision: .passThrough, effects: effects)
            }
            let nextFrame = WindowGeometry.resizedFrame(
                initialFrame: initialFrame,
                corner: corner,
                startPointer: startPointer,
                currentPointer: location
            )
            state = .resizing(
                target: target.updatingFrame(nextFrame),
                corner: corner,
                startPointer: startPointer,
                initialFrame: initialFrame,
                suppressLeftButton: suppressLeftButton
            )
            effects.append(.applyResize(windowId: target.windowId, frame: nextFrame, corner: corner))
            let decision: InputRoutingDecision = (isLeftDrag && suppressLeftButton) ? .consume : .passThrough
            return WindowResizeStepResult(decision: decision, effects: effects)

        case .splitGesture(let target, _, let pendingAction, let suppressLeftButton):
            guard modifiers == Self.splitModifiers else {
                finishSplitGesture(
                    target: target,
                    pendingAction: pendingAction,
                    effects: &effects
                )
                if suppressLeftButton {
                    consumeNextLeftMouseUp = true
                }
                return WindowResizeStepResult(decision: .passThrough, effects: effects)
            }
            updateSplitGesturePointer(location: location, effects: &effects)
            let decision: InputRoutingDecision = (isLeftDrag && suppressLeftButton) ? .consume : .passThrough
            return WindowResizeStepResult(decision: decision, effects: effects)
        }
    }

    private mutating func handleLeftMouseUp(location: CGPoint) -> WindowResizeStepResult {
        lastPointer = location
        var effects: [WindowResizeEffect] = []

        if consumeNextLeftMouseUp {
            consumeNextLeftMouseUp = false
            return WindowResizeStepResult(decision: .consume)
        }

        switch state {
        case .moving(_, _, _, let suppressLeftButton) where suppressLeftButton:
            endGuardIfNeeded(into: &effects)
            state = .idle
            return WindowResizeStepResult(decision: .consume, effects: effects)
        case .resizing(_, _, _, _, let suppressLeftButton) where suppressLeftButton:
            endGuardIfNeeded(into: &effects)
            state = .idle
            return WindowResizeStepResult(decision: .consume, effects: effects)
        case .splitGesture(let target, _, let pendingAction, let suppressLeftButton) where suppressLeftButton:
            finishSplitGesture(
                target: target,
                pendingAction: pendingAction,
                effects: &effects
            )
            suppressSplitUntilRelease = true
            return WindowResizeStepResult(decision: .consume, effects: effects)
        case .armed(_, _, let suppressLeftButton) where suppressLeftButton:
            state = .idle
            return WindowResizeStepResult(decision: .consume)
        default:
            return WindowResizeStepResult(decision: .passThrough)
        }
    }

    private mutating func tryBeginMove(
        at anchorPointer: CGPoint,
        currentPointer: CGPoint,
        suppressLeftButton: Bool,
        applyImmediately: Bool,
        context: InputEventContext,
        resolveTarget: (CGPoint) -> WindowTargetSnapshot?,
        effects: inout [WindowResizeEffect]
    ) -> Bool {
        guard let target = resolveEligibleTarget(
            at: anchorPointer,
            fallbackPoint: currentPointer,
            context: context,
            resolveTarget: resolveTarget
        ), target.isMovable else {
            state = .idle
            return false
        }
        beginGuardIfNeeded(for: target.pid, into: &effects)
        let initialOrigin = target.frame.origin
        var activeTarget = target
        if applyImmediately {
            let nextOrigin = WindowGeometry.movedOrigin(
                initialOrigin: initialOrigin,
                startPointer: anchorPointer,
                currentPointer: currentPointer
            )
            activeTarget = target.updatingFrame(CGRect(origin: nextOrigin, size: target.frame.size))
            effects.append(.applyMove(windowId: target.windowId, origin: nextOrigin))
        }
        state = .moving(
            target: activeTarget,
            startPointer: anchorPointer,
            initialOrigin: initialOrigin,
            suppressLeftButton: suppressLeftButton
        )
        return true
    }

    private mutating func tryBeginResize(
        at anchorPointer: CGPoint,
        currentPointer: CGPoint,
        suppressLeftButton: Bool,
        applyImmediately: Bool,
        context: InputEventContext,
        resolveTarget: (CGPoint) -> WindowTargetSnapshot?,
        effects: inout [WindowResizeEffect]
    ) -> Bool {
        guard let target = resolveEligibleTarget(
            at: anchorPointer,
            fallbackPoint: currentPointer,
            context: context,
            resolveTarget: resolveTarget
        ) else {
            state = .idle
            return false
        }
        let corner: ResizeAnchorCorner = preferences.nearestCornerResize
            ? WindowGeometry.nearestCorner(for: anchorPointer, in: target.frame)
            : .bottomRight
        guard canResize(target: target, corner: corner) else {
            state = .idle
            return false
        }
        beginGuardIfNeeded(for: target.pid, into: &effects)
        let initialFrame = target.frame
        var activeTarget = target
        if applyImmediately {
            let nextFrame = WindowGeometry.resizedFrame(
                initialFrame: initialFrame,
                corner: corner,
                startPointer: anchorPointer,
                currentPointer: currentPointer
            )
            activeTarget = target.updatingFrame(nextFrame)
            effects.append(.applyResize(windowId: target.windowId, frame: nextFrame, corner: corner))
        }
        state = .resizing(
            target: activeTarget,
            corner: corner,
            startPointer: anchorPointer,
            initialFrame: initialFrame,
            suppressLeftButton: suppressLeftButton
        )
        return true
    }

    private mutating func tryBeginSplitGesture(
        at anchorPointer: CGPoint,
        suppressLeftButton: Bool,
        context: InputEventContext,
        resolveTarget: (CGPoint) -> WindowTargetSnapshot?,
        effects: inout [WindowResizeEffect]
    ) -> Bool {
        guard let target = resolveEligibleTarget(
            at: anchorPointer,
            fallbackPoint: nil,
            context: context,
            resolveTarget: resolveTarget
        ) else {
            state = .idle
            return false
        }
        state = .splitGesture(
            target: target,
            startPointer: anchorPointer,
            pendingAction: nil,
            suppressLeftButton: suppressLeftButton
        )
        emitHUD(
            WindowResizeHUDState(isVisible: true, action: nil, anchorPoint: anchorPointer),
            into: &effects
        )
        return true
    }

    private mutating func updateSplitGesturePointer(
        location: CGPoint,
        effects: inout [WindowResizeEffect]
    ) {
        guard case .splitGesture(let target, let startPointer, let previousAction, let suppressLeftButton) = state else {
            return
        }
        let nextAction = WindowGeometry.splitGestureAction(
            startPointer: startPointer,
            currentPointer: location,
            deadzone: splitDeadzone
        )
        state = .splitGesture(
            target: target,
            startPointer: startPointer,
            pendingAction: nextAction,
            suppressLeftButton: suppressLeftButton
        )
        if nextAction != previousAction {
            emitHUD(
                WindowResizeHUDState(isVisible: true, action: nextAction, anchorPoint: startPointer),
                into: &effects
            )
        }
    }

    private mutating func finishSplitGesture(
        target: WindowTargetSnapshot,
        pendingAction: SplitScreenGestureAction?,
        effects: inout [WindowResizeEffect]
    ) {
        hideHUDIfNeeded(into: &effects)
        state = .idle
        if let action = pendingAction {
            effects.append(.executeSplitGesture(windowId: target.windowId, action: action))
        }
    }

    private mutating func transitionMoveToResize(
        target: WindowTargetSnapshot,
        pointer: CGPoint,
        suppressLeftButton: Bool,
        currentFrame: (UInt64) -> CGRect?,
        effects: inout [WindowResizeEffect]
    ) {
        let liveFrame = currentFrame(target.windowId) ?? target.frame
        let corner: ResizeAnchorCorner = preferences.nearestCornerResize
            ? WindowGeometry.nearestCorner(for: pointer, in: liveFrame)
            : .bottomRight
        guard canResize(target: target, corner: corner) else {
            endGuardIfNeeded(into: &effects)
            state = .idle
            if suppressLeftButton {
                consumeNextLeftMouseUp = true
            }
            return
        }
        state = .resizing(
            target: target.updatingFrame(liveFrame),
            corner: corner,
            startPointer: pointer,
            initialFrame: liveFrame,
            suppressLeftButton: suppressLeftButton
        )
    }

    private mutating func transitionResizeToMove(
        target: WindowTargetSnapshot,
        pointer: CGPoint,
        suppressLeftButton: Bool,
        currentFrame: (UInt64) -> CGRect?,
        effects: inout [WindowResizeEffect]
    ) {
        let liveFrame = currentFrame(target.windowId) ?? target.frame
        guard target.isMovable else {
            endGuardIfNeeded(into: &effects)
            state = .idle
            if suppressLeftButton {
                consumeNextLeftMouseUp = true
            }
            return
        }
        state = .moving(
            target: target.updatingFrame(liveFrame),
            startPointer: pointer,
            initialOrigin: liveFrame.origin,
            suppressLeftButton: suppressLeftButton
        )
    }

    private func resolveEligibleTarget(
        at primaryPoint: CGPoint,
        fallbackPoint: CGPoint?,
        context: InputEventContext,
        resolveTarget: (CGPoint) -> WindowTargetSnapshot?
    ) -> WindowTargetSnapshot? {
        let resolved = resolveTarget(primaryPoint) ?? fallbackPoint.flatMap(resolveTarget)
        guard let target = resolved else {
            return nil
        }
        let effectiveBundleId = target.bundleId ?? context.frontmostBundleId
        if ExcludedAppMatcher.isExcluded(bundleId: effectiveBundleId, in: preferences.excludedBundleIds) {
            return nil
        }
        return target
    }

    private func canResize(target: WindowTargetSnapshot, corner: ResizeAnchorCorner) -> Bool {
        guard target.isResizable else { return false }
        if corner != .bottomRight && !target.isMovable {
            return false
        }
        return true
    }

    private mutating func beginGuardIfNeeded(for pid: pid_t, into effects: inout [WindowResizeEffect]) {
        if guardedPid == pid {
            return
        }
        if let previousPid = guardedPid {
            effects.append(.endEnhancedUIGuard(pid: previousPid))
        }
        guardedPid = pid
        effects.append(.beginEnhancedUIGuard(pid: pid))
    }

    private mutating func endGuardIfNeeded(into effects: inout [WindowResizeEffect]) {
        guard let previousPid = guardedPid else { return }
        guardedPid = nil
        effects.append(.endEnhancedUIGuard(pid: previousPid))
    }

    private mutating func emitHUD(_ hudState: WindowResizeHUDState, into effects: inout [WindowResizeEffect]) {
        hudVisible = hudState.isVisible
        effects.append(.updateHUD(hudState))
    }

    private mutating func hideHUDIfNeeded(into effects: inout [WindowResizeEffect]) {
        guard hudVisible else { return }
        hudVisible = false
        effects.append(.updateHUD(.hidden))
    }
}
