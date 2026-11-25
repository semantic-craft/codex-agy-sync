import AppKit
import CodexAgySyncCore
import SwiftUI

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    private let window: NSWindow
    private let hostingController: NSHostingController<SettingsView>
    private let preferencesStore: PreferencesStore
    private let updateViewModel: UpdateViewModel
    private let automationViewModel: AutomationViewModel
    private let inputHub: DesktopInputHub?
    private let navigation: SettingsNavigationModel
    private var hasCenteredOnce = false
    private var languageObserverToken: ObservationToken?

    init(
        preferencesStore: PreferencesStore,
        updateViewModel: UpdateViewModel,
        automationViewModel: AutomationViewModel,
        inputHub: DesktopInputHub? = nil,
        windowResizeStatusStore: WindowResizeStatusStore? = nil,
        onDidChangeCLI: (() -> Void)? = nil
    ) {
        self.preferencesStore = preferencesStore
        self.updateViewModel = updateViewModel
        self.automationViewModel = automationViewModel
        self.inputHub = inputHub
        let navigation = SettingsNavigationModel(selectedTab: .general)
        self.navigation = navigation
        let rootView = SettingsView(
            preferencesStore: preferencesStore,
            updateViewModel: updateViewModel,
            automationViewModel: automationViewModel,
            navigation: navigation,
            inputHub: inputHub,
            windowResizeStatusStore: windowResizeStatusStore,
            onDidChangeCLI: onDidChangeCLI
        )
        let hosting = NSHostingController(rootView: rootView)
        hosting.sizingOptions = [.preferredContentSize]
        self.hostingController = hosting

        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 600, height: 500),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        win.identifier = SettingsWindowIdentity.identifier
        win.title = AppCopy.text(.settingsWindowTitle, in: preferencesStore.effectiveLanguage)
        win.isReleasedWhenClosed = false
        win.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        win.contentViewController = hosting
        self.window = win

        super.init()
        self.window.delegate = self

        self.languageObserverToken = observeContinuously {
            preferencesStore.effectiveLanguage
        } onChange: { [weak self] lang in
            self?.window.title = AppCopy.text(.settingsWindowTitle, in: lang)
        }
    }

    func show(initialTab: SettingsTab = .general) {
        let wasVisible = window.isVisible
        navigation.selectedTab = initialTab
        if NSApp.activationPolicy() != .regular {
            _ = NSApp.setActivationPolicy(.regular)
        }
        preferencesStore.refreshLaunchAtLoginStatus()
        _ = inputHub?.refreshPermissions()
        automationViewModel.refresh()
        Task { @MainActor [weak updateViewModel = self.updateViewModel] in
            await updateViewModel?.refreshOnSurfaceOpen(
                trigger: .settingsOpened,
                recoverStagedFromFailure: true
            )
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
            Task { @MainActor [weak window = self.window] in
                NSApp.activate()
                window?.makeKeyAndOrderFront(nil)
                window?.makeFirstResponder(nil)
            }
        }
    }

    func windowWillClose(_ notification: Notification) {
        preferencesStore.setShortcutRecording(false)
        Task { @MainActor in
            _ = NSApp.setActivationPolicy(.accessory)
        }
    }

    func windowDidBecomeKey(_ notification: Notification) {
        _ = inputHub?.refreshPermissions()
        automationViewModel.refresh()
    }

    func windowDidResignKey(_ notification: Notification) {
        preferencesStore.setShortcutRecording(false)
    }
}
