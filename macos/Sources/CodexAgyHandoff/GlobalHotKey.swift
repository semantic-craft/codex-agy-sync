import Carbon
import CodexAgyHandoffCore
import Foundation

private final class HotKeyCallbackBox: @unchecked Sendable {
    let signature: OSType = 0x4341_484B // 'CAHK'
    let identifier: UInt32 = 1
    let onTrigger: @MainActor @Sendable () -> Void

    init(onTrigger: @escaping @MainActor @Sendable () -> Void) {
        self.onTrigger = onTrigger
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
    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private var activeShortcut: HotKeyShortcut?
    private let callbackBox: HotKeyCallbackBox

    init(onTrigger: @escaping @MainActor @Sendable () -> Void) {
        self.callbackBox = HotKeyCallbackBox(onTrigger: onTrigger)
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
            return true
        }
        if activeShortcut == shortcut, hotKeyRef != nil {
            return true
        }

        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }

        if handlerRef == nil {
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
                return false
            }
            self.handlerRef = installedHandler
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
            return false
        }

        self.hotKeyRef = registeredRef
        self.activeShortcut = shortcut
        return true
    }

    func unregister() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }
        if let handlerRef {
            RemoveEventHandler(handlerRef)
            self.handlerRef = nil
        }
        self.activeShortcut = nil
    }
}
