import Carbon
import CodexAgySyncCore
import Foundation

@MainActor
private final class HotKeyCallbackBox {
    nonisolated let signature: OSType = 0x4341_484B // 'CAHK'
    nonisolated let identifier: UInt32 = 1
    nonisolated let onTrigger: @MainActor @Sendable () -> Void

    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private var retainedContext: Unmanaged<HotKeyCallbackBox>?

    init(onTrigger: @escaping @MainActor @Sendable () -> Void) {
        self.onTrigger = onTrigger
    }

    var hasRegisteredHotKey: Bool {
        hotKeyRef != nil
    }

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
            globalHotKeyEventCallback,
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

    func setHotKey(_ ref: EventHotKeyRef) {
        hotKeyRef = ref
    }

    func unregisterHotKeyOnly() {
        if let existing = hotKeyRef {
            hotKeyRef = nil
            UnregisterEventHotKey(existing)
        }
    }

    func unregisterAll() {
        unregisterHotKeyOnly()
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

private func globalHotKeyEventCallback(
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
    let box = Unmanaged<HotKeyCallbackBox>.fromOpaque(userData).takeUnretainedValue()
    guard hotKeyID.signature == box.signature, hotKeyID.id == box.identifier else {
        return OSStatus(eventNotHandledErr)
    }
    Task { @MainActor in
        box.onTrigger()
    }
    return noErr
}

@MainActor
final class GlobalHotKey {
    private var activeShortcut: HotKeyShortcut?
    private(set) var lastRegistrationFailed: Bool = false
    private nonisolated let callbackBox: HotKeyCallbackBox

    init(onTrigger: @escaping @MainActor @Sendable () -> Void) {
        self.callbackBox = HotKeyCallbackBox(onTrigger: onTrigger)
    }

    deinit {
        let box = callbackBox
        Task { @MainActor in
            box.unregisterAll()
        }
    }

    @discardableResult
    func register(shortcut: HotKeyShortcut = .defaultShortcut) -> Bool {
        reconfigure(shortcut)
    }

    @discardableResult
    func reconfigure(_ shortcut: HotKeyShortcut) -> Bool {
        if !shortcut.isEnabled {
            unregister()
            activeShortcut = shortcut
            lastRegistrationFailed = false
            return true
        }
        if activeShortcut == shortcut, callbackBox.hasRegisteredHotKey {
            lastRegistrationFailed = false
            return true
        }

        callbackBox.unregisterHotKeyOnly()

        guard callbackBox.installHandlerIfNeeded() else {
            activeShortcut = nil
            lastRegistrationFailed = true
            return false
        }

        let hotKeyID = EventHotKeyID(
            signature: callbackBox.signature,
            id: callbackBox.identifier
        )
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
            activeShortcut = nil
            lastRegistrationFailed = true
            return false
        }

        callbackBox.setHotKey(registeredRef)
        activeShortcut = shortcut
        lastRegistrationFailed = false
        return true
    }

    func suspend() {
        callbackBox.unregisterHotKeyOnly()
        activeShortcut = nil
    }

    func unregister() {
        callbackBox.unregisterAll()
        activeShortcut = nil
    }
}
