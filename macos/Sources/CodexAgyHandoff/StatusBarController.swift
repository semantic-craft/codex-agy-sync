import AppKit
import CodexAgyHandoffCore
import SwiftUI

private final class HandoffPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

@MainActor
final class StatusBarController: NSObject, NSWindowDelegate {
    private let viewModel: HandoffViewModel
    private let environment: [String: String]
    private let statusItem: NSStatusItem
    private let panel: HandoffPanel
    private let hostingController: NSHostingController<PopoverView>
    private var hotKey: GlobalHotKey?

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

        let panel = HandoffPanel(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 420),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.title = "CodexAgyHandoff"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
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

        self.panel.delegate = self
        configureStatusButton()

        self.viewModel.onDidHandoffAndActivate = { [weak self] in
            self?.hidePanel(force: false)
        }
    }

    func start() {
        if environment["MENUBAR_DISABLE_GLOBAL_HOTKEY"] != "1" {
            let hk = GlobalHotKey { [weak self] in
                guard let self else { return }
                self.showPanel()
                Task { @MainActor in
                    await self.viewModel.performPrimaryAction()
                }
            }
            _ = hk.register()
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
        positionPanel()
        NSRunningApplication.current.activate(options: [.activateAllWindows])
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(nil)
        Task { @MainActor in
            await viewModel.refresh()
        }
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

    private func configureStatusButton() {
        guard let button = statusItem.button else { return }
        let symbol = NSImage(
            systemSymbolName: "arrow.trianglehead.2.clockwise.rotate.90",
            accessibilityDescription: "会话接力"
        ) ?? NSImage(
            systemSymbolName: "arrow.left.arrow.right.circle",
            accessibilityDescription: "会话接力"
        )
        symbol?.isTemplate = true
        button.image = symbol
        button.toolTip = "会话接力 (⌃⌥H)"
        button.setAccessibilityLabel("会话接力")
        button.target = self
        button.action = #selector(togglePanel(_:))
    }

    private func positionPanel() {
        let fitting = hostingController.sizeThatFits(in: NSSize(width: 360, height: 680))
        let panelWidth: CGFloat = 360
        let panelHeight = max(240, min(fitting.height, 680))

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
