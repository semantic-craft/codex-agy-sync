import AppKit
import CodexAgyHandoffCore
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
    private let environment: [String: String]
    private let statusItem: NSStatusItem
    private let panel: HandoffPanel
    private let hostingController: NSHostingController<PopoverView>
    private var settingsController: SettingsWindowController?
    private var hotKey: GlobalHotKey?
    private var keyMonitor: Any?
    private var cancellables = Set<AnyCancellable>()

    private var pinPanel: Bool {
        environment["MENUBAR_PIN_PANEL"] == "1"
    }

    init(
        viewModel: HandoffViewModel,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.viewModel = viewModel
        self.environment = environment
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        self.hostingController = NSHostingController(rootView: PopoverView(viewModel: viewModel))
        self.hostingController.sizingOptions = [.preferredContentSize]
        self.hostingController.safeAreaRegions = []

        let panel = HandoffPanel(
            contentRect: NSRect(x: 0, y: 0, width: Self.panelWidth, height: 320),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.title = "CodexAgyHandoff"
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

        self.hostingController.rootView = PopoverView(
            viewModel: viewModel,
            onOpenSettings: { [weak self] in
                self?.openSettings()
            }
        )
        self.panel.delegate = self
        configureStatusButton()

        self.viewModel.onDidHandoffAndActivate = { [weak self] in
            self?.hidePanel(force: false)
        }

        viewModel.objectWillChange
            .sink { [weak self] _ in
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    self.updateStatusButtonAppearance()
                    if self.panel.isVisible {
                        self.positionPanel()
                    }
                }
            }
            .store(in: &cancellables)

        viewModel.preferencesStore.$preferences
            .sink { [weak self] prefs in
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    self.updateStatusButtonAppearance()
                    if self.environment["MENUBAR_DISABLE_GLOBAL_HOTKEY"] != "1" {
                        _ = self.hotKey?.reconfigure(prefs.hotKey)
                    }
                    if self.panel.isVisible {
                        self.positionPanel()
                    }
                }
            }
            .store(in: &cancellables)
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
                    self.showPanel()
                    Task { @MainActor in
                        await self.viewModel.performPrimaryAction()
                    }
                }
            }
            _ = hk.register(shortcut: viewModel.preferencesStore.preferences.hotKey)
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

    func openSettings() {
        if pinPanel {
            panel.level = .normal
        } else {
            hidePanel(force: false)
        }
        if settingsController == nil {
            let updateVM = UpdateViewModel(
                preferencesStore: viewModel.preferencesStore,
                environment: environment
            )
            settingsController = SettingsWindowController(
                preferencesStore: viewModel.preferencesStore,
                updateViewModel: updateVM,
                onDidChangeCLI: { [weak self] in
                    Task { @MainActor in
                        await self?.viewModel.refresh()
                    }
                }
            )
        }
        settingsController?.show()
    }

    @objc
    private func togglePanel(_ sender: Any?) {
        if panel.isVisible {
            hidePanel(force: true)
        } else {
            showPanel()
        }
    }

    func showPanel() {
        panel.level = .floating
        positionPanel()
        NSRunningApplication.current.activate(options: [.activateAllWindows])
        NSApp.activate(ignoringOtherApps: true)
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
        Task { @MainActor in
            await viewModel.refresh()
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
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
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
        let showDot = prefs.showAttentionDotOnStatusIcon && viewModel.needsAttentionBadge
        let icon = MenuBarIconRenderer.image(
            for: prefs.menuBarIconStyle,
            showAttentionDot: showDot
        )
        icon.accessibilityDescription = "对话接力"
        button.image = icon
        let shortcutSuffix = prefs.hotKey.isEnabled ? " (\(prefs.hotKey.displayString))" : ""
        button.toolTip = "对话接力\(shortcutSuffix)"
        button.setAccessibilityLabel("对话接力")
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

        panel.setFrame(
            NSRect(x: originX, y: originY, width: panelWidth, height: panelHeight),
            display: true
        )
    }
}
