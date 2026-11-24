import AppKit
import CodexAgySyncCore
import SwiftUI

private final class SyncPalettePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
private final class PopoverRouter {
    var openSettings: ((SettingsTab) -> Void)?
}

private struct ChromeObservationKey: Equatable {
    let agyActive: Bool?
    let codexActive: Bool?
    let heroId: String?
    let heroState: MenubarCandidateState?
    let isLoading: Bool
    let bannerEvent: SyncBannerEvent?
    let cliMissing: Bool
    let popoverNotice: PopoverUpdateNotice?
    let effectiveLanguage: ResolvedLanguage
}

private struct LayoutObservationKey: Equatable {
    let searchQuery: String
    let sections: [PaletteSection]
}

private struct PreferencesObservationKey: Equatable {
    let preferences: AppPreferences
    let isRecordingShortcut: Bool
}

@MainActor
final class StatusBarController: NSObject, NSWindowDelegate {
    private static let panelWidth: CGFloat = 392

    private let store: SessionSyncStore
    private let updateViewModel: UpdateViewModel
    private let environment: [String: String]
    private let statusItem: NSStatusItem
    private let panel: SyncPalettePanel
    private let hostingController: NSHostingController<PopoverView>
    private let router = PopoverRouter()
    private var settingsController: SettingsWindowController?
    private let inputHub: DesktopInputHub
    private var hasStarted: Bool = false
    private var keyMonitor: Any?
    private var lastShowAttentionDot: Bool?
    private var lastLanguage: ResolvedLanguage?
    private var lastToolTip: String?
    private var observationTokens: [ObservationToken] = []

    private var pinPanel: Bool {
        environment["MENUBAR_PIN_PANEL"] == "1"
    }

    init(
        store: SessionSyncStore,
        updateViewModel: UpdateViewModel? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.store = store
        let resolvedUpdateVM = updateViewModel ?? UpdateViewModel(
            preferencesStore: store.preferencesStore,
            environment: environment,
            canMutateEnvironment: { [weak store] in
                store?.busyItemId == nil
            }
        )
        self.updateViewModel = resolvedUpdateVM
        self.environment = environment
        self.inputHub = DesktopInputHub(environment: environment)
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)

        let router = self.router
        self.hostingController = NSHostingController(
            rootView: PopoverView(
                store: store,
                updateViewModel: resolvedUpdateVM,
                onOpenSettings: { tab in
                    router.openSettings?(tab)
                }
            )
        )
        self.hostingController.sizingOptions = []
        self.hostingController.safeAreaRegions = []

        let panel = SyncPalettePanel(
            contentRect: NSRect(x: 0, y: 0, width: Self.panelWidth, height: 320),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.title = "CodexAgySync"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.isReleasedWhenClosed = false
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.standardWindowButton(.closeButton)?.isHidden = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        panel.contentViewController = self.hostingController
        self.panel = panel

        super.init()

        self.router.openSettings = { [weak self] tab in
            self?.openSettings(initialTab: tab)
        }
        self.updateViewModel.onPrepareForRelaunch = { [weak self] in
            self?.inputHub.suspendForRelaunch()
        }
        self.updateViewModel.onRelaunchFailed = { [weak self] in
            guard let self else { return }
            self.inputHub.resumeAfterFailedRelaunch()
            self.store.preferencesStore.setHotKeyRegistrationFailed(
                self.inputHub.failedCarbonSlots.contains(.syncPanel)
            )
        }
        if store.canMutateEnvironment == nil {
            store.canMutateEnvironment = { [weak resolvedUpdateVM] in
                !(resolvedUpdateVM?.isInstallingUpdate ?? false)
            }
        }
        self.panel.delegate = self
        configureStatusButton()

        self.store.onDidSyncAndActivate = { [weak self] in
            self?.hidePanel(force: false)
        }

        let chromeToken = observeContinuously {
            ChromeObservationKey(
                agyActive: store.snapshot?.agyDaemonActive,
                codexActive: store.snapshot?.codexDaemonActive,
                heroId: store.snapshot?.hero?.id,
                heroState: store.snapshot?.hero?.state,
                isLoading: store.isLoading,
                bannerEvent: store.bannerEvent,
                cliMissing: store.cliMissing,
                popoverNotice: resolvedUpdateVM.popoverNotice,
                effectiveLanguage: store.preferencesStore.effectiveLanguage
            )
        } onChange: { [weak self] _ in
            guard let self else { return }
            self.updateStatusButtonAppearance()
            if self.panel.isVisible {
                self.positionPanel()
            }
        }

        let layoutToken = observeContinuously {
            LayoutObservationKey(
                searchQuery: store.searchQuery,
                sections: store.sections
            )
        } onChange: { [weak self] _ in
            guard let self, self.panel.isVisible else { return }
            self.positionPanel()
        }

        let prefsToken = observeContinuously {
            PreferencesObservationKey(
                preferences: store.preferencesStore.preferences,
                isRecordingShortcut: store.preferencesStore.isRecordingShortcut
            )
        } onChange: { [weak self] _ in
            guard let self else { return }
            self.updateStatusButtonAppearance()
            self.applyHotKeyPreferences()
            if self.panel.isVisible {
                self.positionPanel()
            }
        }

        observationTokens = [chromeToken, layoutToken, prefsToken]
    }

    private func applyHotKeyPreferences() {
        guard hasStarted else { return }
        if store.preferencesStore.isRecordingShortcut {
            inputHub.setShortcutRecording(true)
            return
        }
        registerSyncPanelHotKey()
        if inputHub.isRecordingShortcut {
            inputHub.setShortcutRecording(false)
        }
        store.preferencesStore.setHotKeyRegistrationFailed(
            inputHub.failedCarbonSlots.contains(.syncPanel)
        )
    }

    private func registerSyncPanelHotKey() {
        inputHub.registerCarbonHotKey(
            slot: .syncPanel,
            featureSlot: .syncPanel,
            shortcut: store.preferencesStore.preferences.hotKey
        ) { [weak self] in
            guard let self else { return }
            switch self.store.preferencesStore.preferences.hotKeyBehavior {
            case .togglePanelOnly:
                self.togglePanel(nil)
            case .instantPrimaryRescue:
                self.showPanel(refreshIfNeeded: false)
                Task { @MainActor in
                    await self.store.refreshAndPerformPrimaryAction()
                }
            }
        }
    }

    func start() {
        installKeyMonitorIfNeeded()
        hasStarted = true
        applyHotKeyPreferences()

        if environment["MENUBAR_AUTO_OPEN_PANEL"] == "1" {
            Task { @MainActor [weak self] in
                self?.showPanel()
            }
        } else {
            Task { @MainActor [weak self] in
                await self?.store.refresh()
            }
        }

        Task { @MainActor [weak self] in
            guard let self else { return }
            if self.store.preferencesStore.preferences.autoUpdatePolicy == .off {
                await self.updateViewModel.probeIfNeeded()
            } else {
                await self.updateViewModel.refreshOnSurfaceOpen(
                    trigger: .appLaunched,
                    recoverStagedFromFailure: false
                )
            }
        }
    }

    func stop() {
        hasStarted = false
        inputHub.stopAll()
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
        observationTokens.forEach { $0.cancel() }
        observationTokens.removeAll()
    }

    func openSettings(initialTab: SettingsTab = .general) {
        if pinPanel {
            panel.isFloatingPanel = false
            panel.level = .normal
        } else {
            hidePanel(force: false)
        }
        if settingsController == nil {
            settingsController = SettingsWindowController(
                preferencesStore: store.preferencesStore,
                updateViewModel: updateViewModel,
                onDidChangeCLI: { [weak self] in
                    Task { @MainActor in
                        await self?.store.refresh()
                    }
                }
            )
        }
        settingsController?.show(initialTab: initialTab)
    }

    @objc
    private func togglePanel(_ sender: Any?) {
        if panel.isVisible {
            hidePanel(force: true)
        } else {
            showPanel()
        }
    }

    func showPanel(refreshIfNeeded: Bool = true) {
        store.updateFeedReferenceTimestamp()
        panel.isFloatingPanel = true
        panel.level = .floating
        positionPanel()
        NSRunningApplication.current.activate(options: [.activateAllWindows])
        NSApp.activate()
        panel.makeKeyAndOrderFront(nil)
        Task { @MainActor [weak self] in
            guard let self, self.panel.isVisible else { return }
            self.positionPanel()
            if let searchField = Self.findSearchTextField(in: self.panel.contentView) {
                if self.panel.firstResponder !== searchField.currentEditor() {
                    self.panel.makeFirstResponder(searchField)
                }
                searchField.currentEditor()?.selectedRange = NSRange(
                    location: searchField.stringValue.utf16.count,
                    length: 0
                )
            }
        }
        if refreshIfNeeded {
            Task { @MainActor [weak self] in
                await self?.store.refresh()
            }
        }
        Task { @MainActor [weak self] in
            guard let self else { return }
            await self.updateViewModel.refreshOnSurfaceOpen(
                trigger: .panelOpened,
                recoverStagedFromFailure: true
            )
        }
    }

    private static func findSearchTextField(in view: NSView?) -> NSTextField? {
        guard let view else { return nil }
        if let textField = view as? NSTextField, textField.isEditable {
            return textField
        }
        for subview in view.subviews {
            if let found = findSearchTextField(in: subview) {
                return found
            }
        }
        return nil
    }

    func hidePanel(force: Bool = false) {
        if !force && pinPanel {
            return
        }
        panel.orderOut(nil)
    }

    func windowDidResignKey(_ notification: Notification) {
        hidePanel(force: false)
    }

    private func installKeyMonitorIfNeeded() {
        guard keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            let eventWindowNumber = event.windowNumber
            let flags = HotKeyShortcut.normalizedEventFlags(event.modifierFlags)
            let keyChar = event.charactersIgnoringModifiers?.lowercased()
            let keyCode = event.keyCode
            let consumed = MainActor.assumeIsolated {
                self?.handleKeyDown(
                    eventWindowNumber: eventWindowNumber,
                    flags: flags,
                    keyChar: keyChar,
                    keyCode: keyCode
                ) ?? false
            }
            return consumed ? nil : event
        }
    }

    private func handleKeyDown(
        eventWindowNumber: Int,
        flags: NSEvent.ModifierFlags,
        keyChar: String?,
        keyCode: UInt16
    ) -> Bool {
        guard panel.isVisible, panel.windowNumber == eventWindowNumber else {
            return false
        }

        if flags == .command, keyChar == "r" {
            Task { @MainActor [weak self] in
                await self?.store.refresh()
            }
            return true
        }

        if flags == .command, keyChar == "," {
            openSettings()
            return true
        }

        if flags == .command, keyChar == "q" {
            NSApplication.shared.terminate(nil)
            return true
        }

        let hasMarkedText = (panel.firstResponder as? NSTextView)?.hasMarkedText() ?? false
        guard !hasMarkedText else {
            return false
        }

        guard flags.intersection([.command, .control, .option]).isEmpty else {
            return false
        }

        switch keyCode {
        case 125:
            store.moveSelectionDown()
            return true
        case 126:
            store.moveSelectionUp()
            return true
        case 36, 76:
            Task { @MainActor [weak self] in
                await self?.store.performSelectedAction()
            }
            return true
        case 53:
            if !store.searchQuery.isEmpty {
                store.searchQuery = ""
            } else {
                hidePanel(force: true)
            }
            return true
        default:
            return false
        }
    }

    private func configureStatusButton() {
        guard let button = statusItem.button else { return }
        button.target = self
        button.action = #selector(togglePanel(_:))
        updateStatusButtonAppearance()
    }

    private func updateStatusButtonAppearance() {
        guard let button = statusItem.button else { return }
        let prefs = store.preferencesStore.preferences
        let lang = store.preferencesStore.effectiveLanguage
        let showDot = prefs.showAttentionDotOnStatusIcon && store.needsAttentionBadge
        if button.image == nil || lastShowAttentionDot != showDot || lastLanguage != lang {
            let icon = MenuBarIconRenderer.image(showAttentionDot: showDot, language: lang)
            let label = AppCopy.text(.statusItemLabel, in: lang)
            icon.accessibilityDescription = label
            button.image = icon
            button.setAccessibilityLabel(label)
            lastShowAttentionDot = showDot
            lastLanguage = lang
        }
        let shortcutSuffix = prefs.hotKey.isEnabled ? " (\(prefs.hotKey.displayString))" : ""
        let newToolTip = AppCopy.statusToolTip(shortcutSuffix: shortcutSuffix, in: lang)
        if lastToolTip != newToolTip {
            button.toolTip = newToolTip
            lastToolTip = newToolTip
        }
    }

    private func positionPanel() {
        let panelWidth = Self.panelWidth
        hostingController.view.layoutSubtreeIfNeeded()
        let fitting = hostingController.sizeThatFits(in: NSSize(width: panelWidth, height: 680))
        let panelHeight = max(116, min(ceil(fitting.height), 680))

        let fallbackScreen = NSScreen.main ?? NSScreen.screens.first
        let visibleFallback = fallbackScreen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)

        var originX = visibleFallback.maxX - panelWidth - 12
        var originY = visibleFallback.maxY - panelHeight - 8

        if let button = statusItem.button,
           let buttonWindow = button.window
        {
            let rectInWindow = button.convert(button.bounds, to: nil)
            let screenRect = buttonWindow.convertToScreen(rectInWindow)
            if let matchingScreen = NSScreen.screens.first(where: { $0.frame.intersects(screenRect) }),
               screenRect.width > 0,
               screenRect.height > 0
            {
                let visible = matchingScreen.visibleFrame
                let centeredX = screenRect.midX - (panelWidth / 2)
                originX = min(max(centeredX, visible.minX + 8), visible.maxX - panelWidth - 8)
                originY = max(visible.minY + 8, screenRect.minY - panelHeight - 6)
            }
        }

        let targetFrame = NSRect(x: originX, y: originY, width: panelWidth, height: panelHeight)
        if panel.frame != targetFrame {
            panel.setFrame(targetFrame, display: true)
        }
    }
}
