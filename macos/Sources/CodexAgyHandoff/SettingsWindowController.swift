import AppKit
import CodexAgyHandoffCore
import SwiftUI

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    private let window: NSWindow
    private let hostingController: NSHostingController<SettingsView>
    private let preferencesStore: PreferencesStore
    private let updateViewModel: UpdateViewModel
    private var hasCenteredOnce = false

    init(
        preferencesStore: PreferencesStore,
        updateViewModel: UpdateViewModel,
        onDidChangeCLI: (() -> Void)? = nil
    ) {
        self.preferencesStore = preferencesStore
        self.updateViewModel = updateViewModel
        let rootView = SettingsView(
            preferencesStore: preferencesStore,
            updateViewModel: updateViewModel,
            onDidChangeCLI: onDidChangeCLI
        )
        let hosting = NSHostingController(rootView: rootView)
        hosting.sizingOptions = [.preferredContentSize]
        self.hostingController = hosting

        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 460),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        win.title = "CodexAgyHandoff 设置"
        win.isReleasedWhenClosed = false
        win.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        win.contentViewController = hosting
        self.window = win

        super.init()
        self.window.delegate = self
    }

    func show() {
        let wasVisible = window.isVisible
        if NSApp.activationPolicy() != .regular {
            _ = NSApp.setActivationPolicy(.regular)
        }
        preferencesStore.refreshLaunchAtLoginStatus()
        Task { @MainActor [weak updateViewModel = self.updateViewModel] in
            await updateViewModel?.probeEnvironment()
        }
        if !hasCenteredOnce {
            window.center()
            hasCenteredOnce = true
        }
        window.orderFrontRegardless()
        NSRunningApplication.current.activate(options: [.activateAllWindows])
        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
        if !wasVisible {
            window.makeFirstResponder(nil)
            DispatchQueue.main.async { [weak window = self.window] in
                NSApp.activate()
                window?.makeKeyAndOrderFront(nil)
                window?.makeFirstResponder(nil)
            }
        }
    }

    func windowWillClose(_ notification: Notification) {
        preferencesStore.setShortcutRecording(false)
        DispatchQueue.main.async {
            _ = NSApp.setActivationPolicy(.accessory)
        }
    }

    func windowDidResignKey(_ notification: Notification) {
        preferencesStore.setShortcutRecording(false)
    }
}
