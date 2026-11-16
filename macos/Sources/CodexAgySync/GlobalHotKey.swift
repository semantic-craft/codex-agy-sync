import Carbon
import CodexAgySyncCore
import Foundation

private final class HotKeyCallbackBox: @unchecked Sendable {
    let signature: OSType = 0x4341_484B // 'CAHK'
    let identifier: UInt32 = 1
    let onTrigger: @MainActor @Sendable () -> Void

    private let lock = NSLock()
    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?

    init(onTrigger: @escaping @MainActor @Sendable () -> Void) {
        self.onTrigger = onTrigger
    }

    var hasRegisteredHotKey: Bool {
        lock.withLock { hotKeyRef != nil }
    }

    var hasInstalledHandler: Bool {
        lock.withLock { handlerRef != nil }
    }

    func setHandler(_ ref: EventHandlerRef) {
        lock.withLock { handlerRef = ref }
    }

    func setHotKey(_ ref: EventHotKeyRef) {
        lock.withLock { hotKeyRef = ref }
    }

    func unregisterHotKeyOnly() {
        let existing: EventHotKeyRef? = lock.withLock {
            let ref = hotKeyRef
            hotKeyRef = nil
            return ref
        }
        if let existing {
            UnregisterEventHotKey(existing)
        }
    }

    func unregisterAll() {
        let (existingHotKey, existingHandler): (EventHotKeyRef?, EventHandlerRef?) = lock.withLock {
            let hk = hotKeyRef
            let eh = handlerRef
            hotKeyRef = nil
            handlerRef = nil
            return (hk, eh)
        }
        if let existingHotKey {
            UnregisterEventHotKey(existingHotKey)
        }
        if let existingHandler {
            RemoveEventHandler(existingHandler)
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
        callbackBox.unregisterAll()
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

        if !callbackBox.hasInstalledHandler {
            var eventType = EventTypeSpec(
                eventClass: OSType(kEventClassKeyboard),
                eventKind: UInt32(kEventHotKeyPressed)
            )
            let userData = Unmanaged.passUnretained(callbackBox).toOpaque()
            var installedHandler: EventHandlerRef?
            let installStatus = InstallEventHandler(
                GetApplicationEventTarget(),
                globalHotKeyEventCallback,
                1,
                &eventType,
                userData,
                &installedHandler
            )
            guard installStatus == noErr, let installedHandler else {
                activeShortcut = nil
                lastRegistrationFailed = true
                return false
            }
            callbackBox.setHandler(installedHandler)
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
