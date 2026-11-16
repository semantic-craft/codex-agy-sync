import AppKit
import CodexAgyHandoffCore
import Combine
import SwiftUI

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    private let window: NSWindow
    private let hostingController: NSHostingController<SettingsView>
    private let preferencesStore: PreferencesStore
    private let updateViewModel: UpdateViewModel
    private let navigation: SettingsNavigationModel
    private var hasCenteredOnce = false
    private var cancellables = Set<AnyCancellable>()

    init(
        preferencesStore: PreferencesStore,
        updateViewModel: UpdateViewModel,
        onDidChangeCLI: (() -> Void)? = nil
    ) {
        self.preferencesStore = preferencesStore
        self.updateViewModel = updateViewModel
        let navigation = SettingsNavigationModel(selectedTab: .general)
        self.navigation = navigation
        let rootView = SettingsView(
            preferencesStore: preferencesStore,
            updateViewModel: updateViewModel,
            navigation: navigation,
            onDidChangeCLI: onDidChangeCLI
        )
        let hosting = NSHostingController(rootView: rootView)
        hosting.sizingOptions = [.preferredContentSize]
        self.hostingController = hosting

        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 500),
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

        preferencesStore.$effectiveLanguage
            .dropFirst()
            .sink { [weak self] lang in
                self?.window.title = AppCopy.text(.settingsWindowTitle, in: lang)
            }
            .store(in: &cancellables)
    }

    func show(initialTab: SettingsTab = .general) {
        let wasVisible = window.isVisible
        navigation.selectedTab = initialTab
        if NSApp.activationPolicy() != .regular {
            _ = NSApp.setActivationPolicy(.regular)
        }
        preferencesStore.refreshLaunchAtLoginStatus()
        Task { @MainActor [weak updateViewModel = self.updateViewModel] in
            await updateViewModel?.probeEnvironment(recoverStagedFromFailure: true)
            await updateViewModel?.performOpportunisticCheckIfNeeded(trigger: .settingsOpened)
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
