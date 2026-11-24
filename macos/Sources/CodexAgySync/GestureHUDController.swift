import AppKit
import CodexAgySyncCore
import CoreGraphics
import Observation
import SwiftUI

@MainActor
@Observable
private final class GestureHUDModel {
    var action: SplitScreenGestureAction?
    var language: ResolvedLanguage = .zhHans
}

private struct GestureHUDView: View {
    let model: GestureHUDModel

    private var iconName: String {
        model.action?.symbolName ?? "arrow.up.and.down.and.arrow.left.and.right"
    }

    private var title: String {
        if let action = model.action {
            return action.localizedTitle(in: model.language)
        }
        return AppCopy.text(.windowResizeHUDNeutralPrompt, in: model.language)
    }

    private var subtitle: String {
        AppCopy.text(.windowResizeHUDCancelHint, in: model.language)
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: iconName)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(model.action == nil ? Color.secondary : Color.accentColor)
                .frame(width: 24, height: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 12.5, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text(subtitle)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(.regularMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(Color.primary.opacity(0.14), lineWidth: 0.8)
        )
        .fixedSize()
    }
}

private final class NonActivatingHUDPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class GestureHUDController {
    private let model = GestureHUDModel()
    private let panel: NonActivatingHUDPanel
    private let hostingController: NSHostingController<GestureHUDView>

    init() {
        let hosting = NSHostingController(rootView: GestureHUDView(model: model))
        hosting.sizingOptions = [.preferredContentSize]
        self.hostingController = hosting

        let panel = NonActivatingHUDPanel(
            contentRect: NSRect(x: 0, y: 0, width: 220, height: 54),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.ignoresMouseEvents = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.contentViewController = hosting
        self.panel = panel
    }

    func update(state: WindowResizeHUDState, language: ResolvedLanguage) {
        guard state.isVisible else {
            hide()
            return
        }
        model.action = state.action
        model.language = language
        positionPanel(nearQuartzPoint: state.anchorPoint)
        panel.orderFrontRegardless()
    }

    func hide() {
        if panel.isVisible {
            panel.orderOut(nil)
        }
        model.action = nil
    }

    private func positionPanel(nearQuartzPoint quartzPoint: CGPoint) {
        hostingController.view.layoutSubtreeIfNeeded()
        let fitting = hostingController.view.fittingSize
        let width = max(180, ceil(fitting.width))
        let height = max(46, ceil(fitting.height))

        let primaryBounds = CGDisplayBounds(CGMainDisplayID())
        let appKitPoint = CGPoint(
            x: quartzPoint.x,
            y: primaryBounds.height - quartzPoint.y
        )

        let screen = NSScreen.screens.first(where: { $0.frame.contains(appKitPoint) })
            ?? NSScreen.main
            ?? NSScreen.screens.first
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)

        let desiredX = appKitPoint.x - (width / 2)
        let desiredY = appKitPoint.y + 24
        let clampedX = min(max(desiredX, visible.minX + 12), visible.maxX - width - 12)
        let clampedY = min(max(desiredY, visible.minY + 12), visible.maxY - height - 12)

        panel.setFrame(NSRect(x: clampedX, y: clampedY, width: width, height: height), display: true)
    }
}
