import Carbon
import Foundation

@MainActor
public protocol CarbonHotKeyBackend: AnyObject {
    @discardableResult
    func register(
        slot: HotKeySlotID,
        shortcut: HotKeyShortcut,
        handler: @escaping @MainActor @Sendable () -> Void
    ) -> Bool
    func unregister(slot: HotKeySlotID)
    func unregisterAll()
}

@MainActor
private final class CarbonHotKeyCallbackState {
    private struct RegisteredSlot {
        let ref: EventHotKeyRef
        let keyCode: UInt32
        let carbonModifiers: UInt32
        var handler: @MainActor @Sendable () -> Void
    }

    nonisolated let signature: OSType = 0x4341_484B

    private var handlerRef: EventHandlerRef?
    private var retainedContext: Unmanaged<CarbonHotKeyCallbackState>?
    private var slots: [UInt32: RegisteredSlot] = [:]

    func installHandlerIfNeeded() -> Bool {
        guard handlerRef == nil else { return true }
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        let retained = Unmanaged.passRetained(self)
        var installedHandler: EventHandlerRef?
        let installStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            carbonHotKeyEventCallback,
            1,
            &eventType,
            retained.toOpaque(),
            &installedHandler
        )
        guard installStatus == noErr, let installedHandler else {
            retained.release()
            return false
        }
        handlerRef = installedHandler
        retainedContext = retained
        return true
    }

    func register(
        slot: HotKeySlotID,
        shortcut: HotKeyShortcut,
        handler: @escaping @MainActor @Sendable () -> Void
    ) -> Bool {
        if !shortcut.isEnabled {
            unregister(slot: slot)
            return true
        }

        if var existing = slots[slot.carbonID],
           existing.keyCode == shortcut.keyCode,
           existing.carbonModifiers == shortcut.carbonModifiers
        {
            existing.handler = handler
            slots[slot.carbonID] = existing
            return true
        }

        unregisterHotKeyOnly(carbonID: slot.carbonID)

        guard installHandlerIfNeeded() else {
            return false
        }

        let hotKeyID = EventHotKeyID(signature: signature, id: slot.carbonID)
        var registeredRef: EventHotKeyRef?
        let registerStatus = RegisterEventHotKey(
            shortcut.keyCode,
            shortcut.carbonModifiers,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &registeredRef
        )
        guard registerStatus == noErr, let registeredRef else {
            removeHandlerIfIdle()
            return false
        }

        slots[slot.carbonID] = RegisteredSlot(
            ref: registeredRef,
            keyCode: shortcut.keyCode,
            carbonModifiers: shortcut.carbonModifiers,
            handler: handler
        )
        return true
    }

    func unregister(slot: HotKeySlotID) {
        unregisterHotKeyOnly(carbonID: slot.carbonID)
        removeHandlerIfIdle()
    }

    func unregisterAll() {
        let refs = slots.values.map(\.ref)
        slots.removeAll()
        for ref in refs {
            UnregisterEventHotKey(ref)
        }
        removeHandlerNow()
    }

    func invoke(carbonID: UInt32) {
        slots[carbonID]?.handler()
    }

    private func unregisterHotKeyOnly(carbonID: UInt32) {
        if let existing = slots.removeValue(forKey: carbonID) {
            UnregisterEventHotKey(existing.ref)
        }
    }

    private func removeHandlerIfIdle() {
        guard slots.isEmpty else { return }
        removeHandlerNow()
    }

    private func removeHandlerNow() {
        if let existingHandler = handlerRef {
            handlerRef = nil
            RemoveEventHandler(existingHandler)
        }
        if let retained = retainedContext {
            retainedContext = nil
            retained.release()
        }
    }
}

private func carbonHotKeyEventCallback(
    _ nextHandler: EventHandlerCallRef?,
    _ event: EventRef?,
    _ userData: UnsafeMutableRawPointer?
) -> OSStatus {
    guard let event, let userData else {
        return OSStatus(eventNotHandledErr)
    }
    var hotKeyID = EventHotKeyID()
    let status = GetEventParameter(
        event,
        EventParamName(kEventParamDirectObject),
        EventParamType(typeEventHotKeyID),
        nil,
        MemoryLayout<EventHotKeyID>.size,
        nil,
        &hotKeyID
    )
    guard status == noErr else {
        return status
    }
    let state = Unmanaged<CarbonHotKeyCallbackState>.fromOpaque(userData).takeUnretainedValue()
    guard hotKeyID.signature == state.signature else {
        return OSStatus(eventNotHandledErr)
    }
    let triggeredID = hotKeyID.id
    Task { @MainActor in
        state.invoke(carbonID: triggeredID)
    }
    return noErr
}

@MainActor
public final class SystemCarbonHotKeyBackend: CarbonHotKeyBackend {
    private nonisolated let callbackState: CarbonHotKeyCallbackState

    public init() {
        self.callbackState = CarbonHotKeyCallbackState()
    }

    deinit {
        let state = callbackState
        Task { @MainActor in
            state.unregisterAll()
        }
    }

    @discardableResult
    public func register(
        slot: HotKeySlotID,
        shortcut: HotKeyShortcut,
        handler: @escaping @MainActor @Sendable () -> Void
    ) -> Bool {
        callbackState.register(slot: slot, shortcut: shortcut, handler: handler)
    }

    public func unregister(slot: HotKeySlotID) {
        callbackState.unregister(slot: slot)
    }

    public func unregisterAll() {
        callbackState.unregisterAll()
    }
}
