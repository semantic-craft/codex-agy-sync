import AppKit
import Carbon
import CoreGraphics
import Foundation
import os

public final class EventTapDispatcher: @unchecked Sendable {
    private struct State {
        var consumers: [any DesktopInputConsumer] = []
        var isRecordingShortcut: Bool = false
    }

    private let lock = OSAllocatedUnfairLock(initialState: State())

    public init() {}

    public var isRecordingShortcut: Bool {
        lock.withLock { $0.isRecordingShortcut }
    }

    public func setRecordingShortcut(_ isRecording: Bool) {
        lock.withLock {
            $0.isRecordingShortcut = isRecording
        }
    }

    public func register(_ consumer: any DesktopInputConsumer) {
        lock.withLock { state in
            state.consumers.removeAll { $0 === consumer }
            state.consumers.append(consumer)
            state.consumers.sort { lhs, rhs in
                if lhs.priority != rhs.priority {
                    return lhs.priority < rhs.priority
                }
                return lhs.slot.rawValue < rhs.slot.rawValue
            }
        }
    }

    public func unregister(_ consumer: any DesktopInputConsumer) {
        lock.withLock { state in
            state.consumers.removeAll { $0 === consumer }
        }
    }

    public func unregister(slot: DesktopFeatureSlot) {
        lock.withLock { state in
            state.consumers.removeAll { $0.slot == slot }
        }
    }

    public func activeEventRequirement() -> EventTapRequirement {
        let snapshot = lock.withLock { $0.consumers }
        var combined: EventTapRequirement = []
        for consumer in snapshot where consumer.isActive {
            combined.formUnion(consumer.requiredEvents)
        }
        return combined
    }

    public func dispatch(_ event: RoutedInputEvent, context: InputEventContext) -> InputRoutingDecision {
        let (snapshot, recordingFlag) = lock.withLock { ($0.consumers, $0.isRecordingShortcut) }
        if context.isRecordingShortcut || recordingFlag {
            return .passThrough
        }
        if event.kind.isKeyboardEvent, context.isSecureInputActive {
            return .passThrough
        }
        let requiredForEvent = event.kind.tapRequirement
        for consumer in snapshot {
            guard consumer.isActive, consumer.requiredEvents.contains(requiredForEvent) else {
                continue
            }
            if consumer.handle(event, context: context) == .consume {
                return .consume
            }
        }
        return .passThrough
    }

    public func notifyTapReset() {
        let snapshot = lock.withLock { $0.consumers }
        for consumer in snapshot {
            consumer.handleTapReset()
        }
    }
}

@MainActor
public protocol EventTapTransport: AnyObject {
    var isRunning: Bool { get }
    var currentMask: CGEventMask { get }
    @discardableResult
    func start(
        mask: CGEventMask,
        onEvent: @escaping @Sendable (RoutedInputEvent, InputEventContext) -> InputRoutingDecision,
        onReset: @escaping @Sendable () -> Void
    ) -> Bool
    func stop()
}

private final class TapCallbackContext: @unchecked Sendable {
    private struct PortBox: @unchecked Sendable {
        var port: CFMachPort?
    }

    let onEvent: @Sendable (RoutedInputEvent, InputEventContext) -> InputRoutingDecision
    let onReset: @Sendable () -> Void
    private let tapPortLock = OSAllocatedUnfairLock(initialState: PortBox(port: nil))

    init(
        onEvent: @escaping @Sendable (RoutedInputEvent, InputEventContext) -> InputRoutingDecision,
        onReset: @escaping @Sendable () -> Void
    ) {
        self.onEvent = onEvent
        self.onReset = onReset
    }

    var tapPort: CFMachPort? {
        get { tapPortLock.withLock { $0 }.port }
        set {
            let box = PortBox(port: newValue)
            tapPortLock.withLock { $0 = box }
        }
    }
}

private func cgEventTapCallback(
    _ proxy: CGEventTapProxy,
    _ type: CGEventType,
    _ event: CGEvent,
    _ userInfo: UnsafeMutableRawPointer?
) -> Unmanaged<CGEvent>? {
    guard let userInfo else {
        return Unmanaged.passUnretained(event)
    }
    let callbackContext = Unmanaged<TapCallbackContext>.fromOpaque(userInfo).takeUnretainedValue()
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        if let port = callbackContext.tapPort {
            CGEvent.tapEnable(tap: port, enable: true)
        }
        callbackContext.onReset()
        return Unmanaged.passUnretained(event)
    }
    guard let routedEvent = SystemCGEventTapTransport.routedEvent(from: event, type: type) else {
        return Unmanaged.passUnretained(event)
    }
    let context = InputEventContext(
        frontmostBundleId: NSWorkspace.shared.frontmostApplication?.bundleIdentifier,
        isSecureInputActive: IsSecureEventInputEnabled(),
        isRecordingShortcut: false
    )
    let decision = callbackContext.onEvent(routedEvent, context)
    return decision == .consume ? nil : Unmanaged.passUnretained(event)
}

@MainActor
private final class CGEventTapLifecycleBox {
    private var tapPort: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var retainedContext: Unmanaged<TapCallbackContext>?
    private var activeCallbackContext: TapCallbackContext?
    private(set) var currentMask: CGEventMask = 0

    var isRunning: Bool {
        tapPort != nil
    }

    func start(
        mask: CGEventMask,
        onEvent: @escaping @Sendable (RoutedInputEvent, InputEventContext) -> InputRoutingDecision,
        onReset: @escaping @Sendable () -> Void
    ) -> Bool {
        stop()
        guard mask != 0 else { return false }

        let callbackContext = TapCallbackContext(onEvent: onEvent, onReset: onReset)
        let retained = Unmanaged.passRetained(callbackContext)
        guard let port = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
            eventsOfInterest: mask,
            callback: cgEventTapCallback,
            userInfo: retained.toOpaque()
        ) else {
            retained.release()
            return false
        }

        guard let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, port, 0) else {
            CFMachPortInvalidate(port)
            retained.release()
            return false
        }

        callbackContext.tapPort = port
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: port, enable: true)

        tapPort = port
        runLoopSource = source
        retainedContext = retained
        activeCallbackContext = callbackContext
        currentMask = mask
        return true
    }

    func stop() {
        if let port = tapPort {
            tapPort = nil
            CGEvent.tapEnable(tap: port, enable: false)
            CFMachPortInvalidate(port)
        }
        if let source = runLoopSource {
            runLoopSource = nil
            CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
        }
        activeCallbackContext?.tapPort = nil
        activeCallbackContext = nil
        if let retained = retainedContext {
            retainedContext = nil
            retained.release()
        }
        currentMask = 0
    }
}

@MainActor
public final class SystemCGEventTapTransport: EventTapTransport {
    private nonisolated let lifecycleBox: CGEventTapLifecycleBox

    public init() {
        self.lifecycleBox = CGEventTapLifecycleBox()
    }

    deinit {
        let box = lifecycleBox
        Task { @MainActor in
            box.stop()
        }
    }

    public var isRunning: Bool {
        lifecycleBox.isRunning
    }

    public var currentMask: CGEventMask {
        lifecycleBox.currentMask
    }

    @discardableResult
    public func start(
        mask: CGEventMask,
        onEvent: @escaping @Sendable (RoutedInputEvent, InputEventContext) -> InputRoutingDecision,
        onReset: @escaping @Sendable () -> Void
    ) -> Bool {
        lifecycleBox.start(mask: mask, onEvent: onEvent, onReset: onReset)
    }

    public func stop() {
        lifecycleBox.stop()
    }

    nonisolated static func routedEvent(from event: CGEvent, type: CGEventType) -> RoutedInputEvent? {
        let modifiers = InputModifierSet(cgEventFlags: event.flags)
        let timestamp = TimeInterval(event.timestamp) / 1_000_000_000.0
        switch type {
        case .flagsChanged:
            return RoutedInputEvent(
                kind: .flagsChanged(modifiers: modifiers),
                timestamp: timestamp
            )
        case .keyDown:
            let keyCode = UInt16(truncatingIfNeeded: event.getIntegerValueField(.keyboardEventKeycode))
            let isRepeat = event.getIntegerValueField(.keyboardEventAutorepeat) != 0
            return RoutedInputEvent(
                kind: .keyDown(keyCode: keyCode, modifiers: modifiers, isRepeat: isRepeat),
                timestamp: timestamp
            )
        case .keyUp:
            let keyCode = UInt16(truncatingIfNeeded: event.getIntegerValueField(.keyboardEventKeycode))
            return RoutedInputEvent(
                kind: .keyUp(keyCode: keyCode, modifiers: modifiers),
                timestamp: timestamp
            )
        case .mouseMoved:
            return RoutedInputEvent(
                kind: .mouseMoved(location: event.location, modifiers: modifiers),
                timestamp: timestamp
            )
        case .leftMouseDown:
            return RoutedInputEvent(
                kind: .leftMouseDown(location: event.location, modifiers: modifiers),
                timestamp: timestamp
            )
        case .leftMouseDragged:
            return RoutedInputEvent(
                kind: .leftMouseDragged(location: event.location, modifiers: modifiers),
                timestamp: timestamp
            )
        case .leftMouseUp:
            return RoutedInputEvent(
                kind: .leftMouseUp(location: event.location, modifiers: modifiers),
                timestamp: timestamp
            )
        default:
            return nil
        }
    }
}

@MainActor
public final class SharedEventTapRouter {
    public let dispatcher: EventTapDispatcher
    private let transport: any EventTapTransport
    private let permissionService: any DesktopPermissionChecking
    public let disabledByEnvironment: Bool

    public private(set) var status: EventTapBackendStatus = .inactive
    public private(set) var lastPermissionState: DesktopPermissionState

    public init(
        dispatcher: EventTapDispatcher = EventTapDispatcher(),
        transport: any EventTapTransport = SystemCGEventTapTransport(),
        permissionService: any DesktopPermissionChecking = SystemDesktopPermissionService(),
        disabledByEnvironment: Bool = false
    ) {
        self.dispatcher = dispatcher
        self.transport = transport
        self.permissionService = permissionService
        self.disabledByEnvironment = disabledByEnvironment
        self.lastPermissionState = DesktopPermissionState(
            accessibilityGranted: false,
            inputMonitoringGranted: false
        )
    }

    @discardableResult
    public func syncState() -> EventTapBackendStatus {
        let requirement = dispatcher.activeEventRequirement()
        let desiredMask = requirement.cgEventMask
        guard desiredMask != 0 else {
            if transport.isRunning {
                transport.stop()
            }
            status = .inactive
            return status
        }

        if disabledByEnvironment {
            if transport.isRunning {
                transport.stop()
            }
            status = .disabledByEnvironment
            return status
        }

        let permissions = permissionService.currentState()
        lastPermissionState = permissions
        guard permissions.hasEventTapAccess else {
            if transport.isRunning {
                transport.stop()
            }
            status = .pausedMissingPermission
            return status
        }

        if transport.isRunning, transport.currentMask == desiredMask {
            status = .active
            return status
        }

        let activeDispatcher = dispatcher
        let started = transport.start(
            mask: desiredMask,
            onEvent: { event, context in
                activeDispatcher.dispatch(event, context: context)
            },
            onReset: {
                activeDispatcher.notifyTapReset()
            }
        )
        status = started ? .active : .creationFailed
        return status
    }

    public func stop() {
        if transport.isRunning {
            transport.stop()
        }
        status = .inactive
    }
}
