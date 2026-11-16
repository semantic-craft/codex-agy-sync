import AppKit
import CodexAgySyncCore
import Combine
import SwiftUI

private final class HandoffPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

@MainActor
final class StatusBarController: NSObject, NSWindowDelegate {
    private static let panelWidth: CGFloat = 392

    private let viewModel: HandoffViewModel
    private let updateViewModel: UpdateViewModel
    private let environment: [String: String]
    private let statusItem: NSStatusItem
    private let panel: HandoffPanel
    private let hostingController: NSHostingController<PopoverView>
    private var settingsController: SettingsWindowController?
    private var hotKey: GlobalHotKey?
    private var keyMonitor: Any?
    private var layoutRefreshScheduled = false
    private var panelLayoutRefreshScheduled = false
    private var lastShowAttentionDot: Bool?
    private var lastLanguage: ResolvedLanguage?
    private var lastToolTip: String?
    private var cancellables = Set<AnyCancellable>()

    private var pinPanel: Bool {
        environment["MENUBAR_PIN_PANEL"] == "1"
    }

    init(
        viewModel: HandoffViewModel,
        updateViewModel: UpdateViewModel? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.viewModel = viewModel
        let resolvedUpdateVM = updateViewModel ?? UpdateViewModel(
            preferencesStore: viewModel.preferencesStore,
            environment: environment,
            canMutateEnvironment: { [weak viewModel] in
                viewModel?.busyItemId == nil
            }
        )
        self.updateViewModel = resolvedUpdateVM
        self.environment = environment
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        self.hostingController = NSHostingController(
            rootView: PopoverView(viewModel: viewModel, updateViewModel: resolvedUpdateVM)
        )
        self.hostingController.sizingOptions = [.preferredContentSize]
        self.hostingController.safeAreaRegions = []

        let panel = HandoffPanel(
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

        self.updateViewModel.onPrepareForRelaunch = { [weak self] in
            self?.hotKey?.unregister()
        }
        self.updateViewModel.onRelaunchFailed = { [weak self] in
            self?.applyHotKeyPreferences()
        }
        if viewModel.canMutateEnvironment == nil {
            viewModel.canMutateEnvironment = { [weak resolvedUpdateVM] in
                !(resolvedUpdateVM?.isInstallingUpdate ?? false)
            }
        }
        self.hostingController.rootView = PopoverView(
            viewModel: viewModel,
            updateViewModel: resolvedUpdateVM,
            onOpenSettings: { [weak self] tab in
                self?.openSettings(initialTab: tab)
            }
        )
        self.panel.delegate = self
        configureStatusButton()

        self.viewModel.onDidHandoffAndActivate = { [weak self] in
            self?.hidePanel(force: false)
        }

        Publishers.MergeMany(
            viewModel.$snapshot.map { _ in () }.eraseToAnyPublisher(),
            viewModel.$isLoading.map { _ in () }.eraseToAnyPublisher(),
            viewModel.$bannerEvent.map { _ in () }.eraseToAnyPublisher(),
            viewModel.$cliMissing.map { _ in () }.eraseToAnyPublisher(),
            resolvedUpdateVM.$popoverNotice.map { _ in () }.eraseToAnyPublisher(),
            viewModel.preferencesStore.$effectiveLanguage.map { _ in () }.eraseToAnyPublisher()
        )
        .sink { [weak self] _ in
            self?.scheduleChromeRefresh()
        }
        .store(in: &cancellables)

        viewModel.$searchQuery
            .sink { [weak self] _ in
                self?.schedulePanelLayoutRefresh()
            }
            .store(in: &cancellables)

        Publishers.CombineLatest(
            viewModel.preferencesStore.$preferences,
            viewModel.preferencesStore.$isRecordingShortcut
        )
        .sink { [weak self] _, _ in
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.updateStatusButtonAppearance()
                self.applyHotKeyPreferences()
                if self.panel.isVisible {
                    self.positionPanel()
                }
            }
        }
        .store(in: &cancellables)
    }

    private func applyHotKeyPreferences() {
        guard environment["MENUBAR_DISABLE_GLOBAL_HOTKEY"] != "1", let hk = hotKey else {
            return
        }
        if viewModel.preferencesStore.isRecordingShortcut {
            hk.suspend()
        } else {
            let ok = hk.reconfigure(viewModel.preferencesStore.preferences.hotKey)
            viewModel.preferencesStore.setHotKeyRegistrationFailed(!ok)
        }
    }

    private func scheduleChromeRefresh() {
        guard !layoutRefreshScheduled else { return }
        layoutRefreshScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.layoutRefreshScheduled = false
            self.updateStatusButtonAppearance()
            if self.panel.isVisible {
                self.positionPanel()
            }
        }
    }

    private func schedulePanelLayoutRefresh() {
        guard !panelLayoutRefreshScheduled else { return }
        panelLayoutRefreshScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.panelLayoutRefreshScheduled = false
            if self.panel.isVisible {
                self.positionPanel()
            }
        }
    }

    func start() {
        installKeyMonitorIfNeeded()

        if environment["MENUBAR_DISABLE_GLOBAL_HOTKEY"] != "1" {
            let hk = GlobalHotKey { [weak self] in
                guard let self else { return }
                switch self.viewModel.preferencesStore.preferences.hotKeyBehavior {
                case .togglePanelOnly:
                    self.togglePanel(nil)
                case .instantPrimaryRescue:
                    self.showPanel(refreshIfNeeded: false)
                    Task { @MainActor in
                        await self.viewModel.refreshAndPerformPrimaryAction()
                    }
                }
            }
            let ok = hk.register(shortcut: viewModel.preferencesStore.preferences.hotKey)
            viewModel.preferencesStore.setHotKeyRegistrationFailed(!ok)
            self.hotKey = hk
        }

        if environment["MENUBAR_AUTO_OPEN_PANEL"] == "1" {
            DispatchQueue.main.async { [weak self] in
                self?.showPanel()
            }
        } else {
            Task { @MainActor in
                await viewModel.refresh()
            }
        }

        Task { @MainActor [weak self] in
            guard let self else { return }
            if self.viewModel.preferencesStore.preferences.autoUpdatePolicy == .off {
                await self.updateViewModel.probeIfNeeded()
            } else {
                await self.updateViewModel.performOpportunisticCheckIfNeeded(trigger: .appLaunched)
            }
        }
    }

    func stop() {
        hotKey?.unregister()
        hotKey = nil
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
        cancellables.removeAll()
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
                preferencesStore: viewModel.preferencesStore,
                updateViewModel: updateViewModel,
                onDidChangeCLI: { [weak self] in
                    Task { @MainActor in
                        await self?.viewModel.refresh()
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
        panel.isFloatingPanel = true
        panel.level = .floating
        positionPanel()
        NSRunningApplication.current.activate(options: [.activateAllWindows])
        NSApp.activate()
        panel.makeKeyAndOrderFront(nil)
        DispatchQueue.main.async { [weak self] in
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
            Task { @MainActor in
                await viewModel.refresh()
            }
        }
        Task { @MainActor [weak self] in
            guard let self else { return }
            await self.updateViewModel.probeEnvironment(recoverStagedFromFailure: true)
            await self.updateViewModel.performOpportunisticCheckIfNeeded(trigger: .panelOpened)
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
                await self?.viewModel.refresh()
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
            viewModel.moveSelectionDown()
            return true
        case 126:
            viewModel.moveSelectionUp()
            return true
        case 36, 76:
            Task { @MainActor [weak self] in
                await self?.viewModel.performSelectedAction()
            }
            return true
        case 53:
            if !viewModel.searchQuery.isEmpty {
                viewModel.searchQuery = ""
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
        let prefs = viewModel.preferencesStore.preferences
        let lang = viewModel.preferencesStore.effectiveLanguage
        let showDot = prefs.showAttentionDotOnStatusIcon && viewModel.needsAttentionBadge
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
