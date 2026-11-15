import Carbon
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
    private let callbackBox: HotKeyCallbackBox

    init(onTrigger: @escaping @MainActor @Sendable () -> Void) {
        self.callbackBox = HotKeyCallbackBox(onTrigger: onTrigger)
    }

    @discardableResult
    func register() -> Bool {
        guard hotKeyRef == nil else { return true }

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

        let hotKeyID = EventHotKeyID(
            signature: callbackBox.signature,
            id: callbackBox.identifier
        )
        var registeredRef: EventHotKeyRef?
        let registerStatus = RegisterEventHotKey(
            UInt32(kVK_ANSI_H),
            UInt32(controlKey | optionKey),
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &registeredRef
        )
        guard registerStatus == noErr, let registeredRef else {
            RemoveEventHandler(installedHandler)
            return false
        }

        self.handlerRef = installedHandler
        self.hotKeyRef = registeredRef
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
    }
}
