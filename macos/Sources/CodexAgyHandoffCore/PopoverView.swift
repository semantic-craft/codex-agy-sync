import AppKit
import SwiftUI

struct FlatSurfaceButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.84 : 1.0)
    }
}

private struct PopoverVibrancyBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .popover
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = .popover
        nsView.blendingMode = .behindWindow
        nsView.state = .active
    }
}

final class PaletteSearchTextFieldCell: NSTextFieldCell {
    override func setAccessibilityValue(_ value: Any?) {
        if let field = controlView as? PaletteSearchTextField {
            field.applyAccessibilityValue(value)
            return
        }
        super.setAccessibilityValue(value)
    }

    override func isAccessibilitySelectorAllowed(_ selector: Selector) -> Bool {
        if selector == #selector(setAccessibilityValue(_:)) {
            return true
        }
        return super.isAccessibilitySelectorAllowed(selector)
    }
}

final class PaletteSearchTextField: NSTextField {
    override class var cellClass: AnyClass? {
        get { PaletteSearchTextFieldCell.self }
        set {}
    }

    override var needsPanelToBecomeKey: Bool { true }

    var onTextChange: ((String) -> Void)?

    func applyAccessibilityValue(_ value: Any?) {
        let updated = (value as? String) ?? ""
        stringValue = updated
        if let editor = currentEditor() {
            editor.string = updated
            editor.selectedRange = NSRange(location: updated.utf16.count, length: 0)
        }
        onTextChange?(updated)
    }

    override func setAccessibilityValue(_ value: Any?) {
        applyAccessibilityValue(value)
    }

    override func isAccessibilitySelectorAllowed(_ selector: Selector) -> Bool {
        if selector == #selector(setAccessibilityValue(_:)) {
            return true
        }
        return super.isAccessibilitySelectorAllowed(selector)
    }

    override func textDidChange(_ notification: Notification) {
        super.textDidChange(notification)
        onTextChange?(stringValue)
    }
}

private struct PaletteSearchField: NSViewRepresentable {
    @Binding var text: String
    let placeholder: String
    let accessibilityLabel: String

    func makeCoordinator() -> Coordinator {
        Coordinator(text: $text)
    }

    func makeNSView(context: Context) -> PaletteSearchTextField {
        let field = PaletteSearchTextField(frame: .zero)
        field.isEditable = true
        field.isSelectable = true
        field.isEnabled = true
        field.isBordered = false
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.font = .systemFont(ofSize: 13)
        field.textColor = .labelColor
        field.placeholderString = placeholder
        field.usesSingleLineMode = true
        field.maximumNumberOfLines = 1
        field.lineBreakMode = .byTruncatingTail
        field.cell?.wraps = false
        field.cell?.isScrollable = true
        field.setContentHuggingPriority(.defaultLow, for: .horizontal)
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        field.setContentHuggingPriority(.required, for: .vertical)
        field.setAccessibilityLabel(accessibilityLabel)
        field.delegate = context.coordinator
        field.onTextChange = { [weak coordinator = context.coordinator] updated in
            coordinator?.propagate(updated)
        }
        field.stringValue = text
        return field
    }

    func updateNSView(_ nsView: PaletteSearchTextField, context: Context) {
        context.coordinator.text = $text
        nsView.placeholderString = placeholder
        nsView.setAccessibilityLabel(accessibilityLabel)
        nsView.onTextChange = { [weak coordinator = context.coordinator] updated in
            coordinator?.propagate(updated)
        }
        let hasMarked = (nsView.currentEditor() as? NSTextView)?.hasMarkedText() ?? false
        if nsView.stringValue != text && !hasMarked {
            nsView.stringValue = text
            if let editor = nsView.currentEditor() {
                editor.string = text
                editor.selectedRange = NSRange(location: text.utf16.count, length: 0)
            }
        }
    }

    func sizeThatFits(
        _ proposal: ProposedViewSize,
        nsView: PaletteSearchTextField,
        context: Context
    ) -> CGSize {
        let fittingHeight = max(17, ceil(nsView.intrinsicContentSize.height))
        return CGSize(width: proposal.width ?? 180, height: fittingHeight)
    }

    @MainActor
    final class Coordinator: NSObject, NSTextFieldDelegate {
        var text: Binding<String>

        init(text: Binding<String>) {
            self.text = text
        }

        func propagate(_ updated: String) {
            if text.wrappedValue != updated {
                text.wrappedValue = updated
            }
        }

        func controlTextDidChange(_ obj: Notification) {
            guard let field = obj.object as? NSTextField else { return }
            propagate(field.stringValue)
        }
    }
}

private struct KeycapBadge: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: .medium, design: .monospaced))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 5)
            .padding(.vertical, 1.5)
            .background(
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .fill(Color.primary.opacity(0.06))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5)
            )
    }
}

private struct PaletteSessionRow: View {
    let item: MenubarSessionItem
    let nowEpoch: Double
    let isSelected: Bool
    let isBusy: Bool
    let isDisabled: Bool
    let onSelect: () -> Void
    let onTrigger: () -> Void

    @State private var isHovered = false

    var body: some View {
        let presentation = item.presentation(nowEpoch: nowEpoch)
        if presentation.isActionable {
            Button(action: onTrigger) {
                rowContent(presentation: presentation)
            }
            .buttonStyle(FlatSurfaceButtonStyle())
            .focusEffectDisabled()
            .disabled(isDisabled)
            .onHover { hovering in
                isHovered = hovering
                if hovering {
                    onSelect()
                }
            }
            .accessibilityLabel(presentation.accessibilityLabel)
            .accessibilityAction { onTrigger() }
        } else {
            rowContent(presentation: presentation)
                .onHover { hovering in
                    isHovered = hovering
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel(presentation.accessibilityLabel)
        }
    }

    private func rowContent(presentation: MenubarRowPresentation) -> some View {
        HStack(spacing: 10) {
            monogramBadge(presentation: presentation)

            VStack(alignment: .leading, spacing: 2) {
                Text(item.title)
                    .font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(presentation.isActionable ? .primary : .secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)

                Text(presentation.subtitle)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            trailingAccessory(presentation: presentation)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6.5)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(rowBackgroundFill(isActionable: presentation.isActionable))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(
                    isSelected && presentation.isActionable
                        ? Color.accentColor.opacity(0.28)
                        : Color.clear,
                    lineWidth: 0.5
                )
        )
        .contentShape(Rectangle())
    }

    private func rowBackgroundFill(isActionable: Bool) -> Color {
        if isSelected && isActionable {
            return Color.accentColor.opacity(0.16)
        }
        if isHovered && isActionable {
            return Color.primary.opacity(0.05)
        }
        return .clear
    }

    private func monogramBadge(presentation: MenubarRowPresentation) -> some View {
        let tint: Color = presentation.monogram == "A" ? .purple : .blue
        return ZStack(alignment: .bottomTrailing) {
            Text(presentation.monogram)
                .font(.system(size: 11.5, weight: .bold, design: .rounded))
                .foregroundStyle(tint)
                .frame(width: 24, height: 24)
                .background(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(tint.opacity(0.14))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .strokeBorder(tint.opacity(0.22), lineWidth: 0.5)
                )

            if item.state == .interruptedStopped {
                Circle()
                    .fill(Color.orange)
                    .frame(width: 6, height: 6)
                    .overlay(
                        Circle()
                            .strokeBorder(Color(nsColor: .windowBackgroundColor), lineWidth: 1)
                    )
                    .offset(x: 1.5, y: 1.5)
            }
        }
    }

    @ViewBuilder
    private func trailingAccessory(presentation: MenubarRowPresentation) -> some View {
        if isBusy {
            ProgressView()
                .controlSize(.small)
                .scaleEffect(0.75)
        } else if isSelected && presentation.isActionable {
            HStack(spacing: 4) {
                Text(presentation.selectedActionTitle)
                    .font(.system(size: 11.5, weight: .semibold))
                    .foregroundStyle(Color.accentColor)
                KeycapBadge(text: "↵")
            }
        } else {
            Text(presentation.unselectedTrailingTitle)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
        }
    }
}

public struct PopoverView: View {
    @ObservedObject private var viewModel: HandoffViewModel
    @ObservedObject private var preferencesStore: PreferencesStore
    private let onOpenSettings: (() -> Void)?

    public init(viewModel: HandoffViewModel, onOpenSettings: (() -> Void)? = nil) {
        self.viewModel = viewModel
        self.preferencesStore = viewModel.preferencesStore
        self.onOpenSettings = onOpenSettings
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            searchHeader
            hairlineDivider
            if viewModel.banner != nil || viewModel.cliMissing {
                bannerSection
            }
            commandStreamSection
            actionBarFooter
        }
        .frame(width: 392)
        .background(PopoverVibrancyBackground())
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.14), lineWidth: 0.5)
        )
        .ignoresSafeArea()
    }

    private var searchHeader: some View {
        HStack(spacing: 8) {
            Group {
                if viewModel.isLoading {
                    ProgressView()
                        .controlSize(.small)
                        .scaleEffect(0.72)
                } else {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: 15, height: 15)

            PaletteSearchField(
                text: $viewModel.searchQuery,
                placeholder: "筛选项目或对话标题…",
                accessibilityLabel: "筛选项目或对话标题"
            )
            .frame(maxWidth: .infinity, minHeight: 18)

            if !viewModel.searchQuery.isEmpty {
                Button {
                    viewModel.searchQuery = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 12))
                        .foregroundStyle(.tertiary)
                        .padding(2)
                        .contentShape(Rectangle())
                }
                .buttonStyle(FlatSurfaceButtonStyle())
                .focusEffectDisabled()
                .accessibilityLabel("清空筛选")
                .accessibilityAction {
                    viewModel.searchQuery = ""
                }
            }

            if preferencesStore.preferences.hotKey.isEnabled {
                KeycapBadge(text: preferencesStore.preferences.hotKey.displayString)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    private var hairlineDivider: some View {
        Rectangle()
            .fill(Color.primary.opacity(0.08))
            .frame(height: 0.5)
    }

    private var bannerSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let banner = viewModel.banner {
                let tint = bannerTint(for: banner.kind)
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: bannerIcon(for: banner.kind))
                        .font(.system(size: 12))
                        .foregroundStyle(tint)
                        .padding(.top, 1)

                    Text(banner.text)
                        .font(.system(size: 11.5))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    Button {
                        viewModel.dismissBanner()
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(.secondary)
                            .padding(3)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(FlatSurfaceButtonStyle())
                    .focusEffectDisabled()
                    .accessibilityLabel("关闭提示：\(banner.text)")
                    .accessibilityAction {
                        viewModel.dismissBanner()
                    }
                }
                .padding(8)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(tint.opacity(0.10))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(tint.opacity(0.26), lineWidth: 0.5)
                )
            }

            if viewModel.cliMissing {
                Button {
                    pickCLIExecutable()
                } label: {
                    Text("选择工具…")
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .fill(Color.primary.opacity(0.08))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .strokeBorder(Color.primary.opacity(0.15), lineWidth: 0.5)
                        )
                        .contentShape(Rectangle())
                }
                .buttonStyle(FlatSurfaceButtonStyle())
                .focusEffectDisabled()
                .accessibilityLabel("选择工具…")
                .accessibilityAction {
                    pickCLIExecutable()
                }
            }
        }
        .padding(.horizontal, 8)
        .padding(.top, 8)
        .padding(.bottom, 2)
    }

    @ViewBuilder
    private var commandStreamSection: some View {
        let currentSections = viewModel.sections
        let nowEpoch = Date().timeIntervalSince1970

        if !currentSections.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(currentSections) { section in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(section.title)
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 8)
                            .padding(.top, 4)
                            .padding(.bottom, 2)

                        ForEach(section.items) { item in
                            let isRowBusy = viewModel.busyItemId == item.id
                                || (item.state == .pendingReconcile && viewModel.busyItemId == item.workspacePath)
                            PaletteSessionRow(
                                item: item,
                                nowEpoch: nowEpoch,
                                isSelected: viewModel.selectedItemId == item.id,
                                isBusy: isRowBusy,
                                isDisabled: viewModel.busyItemId != nil,
                                onSelect: { viewModel.selectItem(item.id) },
                                onTrigger: { triggerRowAction(item) }
                            )
                        }
                    }
                }
            }
            .padding(6)
        } else if viewModel.snapshot == nil && viewModel.isLoading {
            emptyStateCard(label: "正在读取最近对话…") {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                    Text("正在读取最近对话…")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                }
            }
        } else if !viewModel.searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            emptyStateCard(label: "未找到匹配的对话 · 换个项目名或关键词试试，按 Esc 清空筛选") {
                VStack(spacing: 4) {
                    Text("未找到匹配的对话")
                        .font(.system(size: 12.5, weight: .medium))
                        .foregroundStyle(.primary)
                    Text("换个项目名或关键词试试，按 Esc 清空筛选")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
        } else {
            emptyStateCard(label: "暂无可继续的对话") {
                VStack(spacing: 4) {
                    Text("暂无可继续的对话")
                        .font(.system(size: 12.5, weight: .medium))
                        .foregroundStyle(.primary)
                    Text("在 Codex 或 Antigravity 开始对话后会显示在这里")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func emptyStateCard<Content: View>(
        label: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        content()
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 16)
            .padding(.vertical, 26)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(label)
    }

    private var actionBarFooter: some View {
        let canContinue = (viewModel.selectedItem?.presentation().isActionable ?? false)
            && viewModel.busyItemId == nil

        return VStack(spacing: 0) {
            hairlineDivider
            HStack(spacing: 5) {
                daemonStatusIndicator

                Spacer(minLength: 4)

                footerChip(
                    title: "继续",
                    shortcut: "↵",
                    accessibilityLabel: "执行当前选中操作",
                    isEnabled: canContinue
                ) {
                    Task { await viewModel.performSelectedAction() }
                }

                footerChip(
                    title: "刷新",
                    shortcut: "⌘R",
                    accessibilityLabel: "刷新对话列表",
                    isEnabled: !viewModel.isLoading
                ) {
                    Task { await viewModel.refresh() }
                }

                footerChip(
                    title: "设置",
                    shortcut: "⌘,",
                    accessibilityLabel: "打开设置",
                    isEnabled: true
                ) {
                    onOpenSettings?()
                }

                footerChip(
                    title: "退出",
                    shortcut: "⌘Q",
                    accessibilityLabel: "退出应用",
                    isEnabled: true
                ) {
                    NSApplication.shared.terminate(nil)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(Color.primary.opacity(0.03))
        }
    }

    private var daemonStatusIndicator: some View {
        let agyActive = viewModel.snapshot?.agyDaemonActive ?? false
        let codexActive = viewModel.snapshot?.codexDaemonActive ?? false
        let dotColor: Color
        let statusText: String

        switch (agyActive, codexActive) {
        case (true, true):
            dotColor = .green
            statusText = "双端在线"
        case (true, false):
            dotColor = .orange
            statusText = "仅 AGY 在线"
        case (false, true):
            dotColor = .orange
            statusText = "仅 Codex 在线"
        case (false, false):
            dotColor = Color.gray.opacity(0.55)
            statusText = "双端未运行"
        }

        return HStack(spacing: 5) {
            Circle()
                .fill(dotColor)
                .frame(width: 6, height: 6)
            Text(statusText)
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .fixedSize(horizontal: true, vertical: false)
    }

    private func footerChip(
        title: String,
        shortcut: String,
        accessibilityLabel: String,
        isEnabled: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 3) {
                Text(title)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(isEnabled ? .primary : .tertiary)
                KeycapBadge(text: shortcut)
                    .opacity(isEnabled ? 1.0 : 0.55)
            }
            .padding(.horizontal, 5)
            .padding(.vertical, 2.5)
            .fixedSize()
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(Color.primary.opacity(0.04))
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(FlatSurfaceButtonStyle())
        .focusEffectDisabled()
        .disabled(!isEnabled)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityAction {
            guard isEnabled else { return }
            action()
        }
    }

    private func bannerIcon(for kind: StatusBanner.Kind) -> String {
        switch kind {
        case .success:
            return "checkmark.circle.fill"
        case .info:
            return "info.circle.fill"
        case .error:
            return "exclamationmark.triangle.fill"
        }
    }

    private func bannerTint(for kind: StatusBanner.Kind) -> Color {
        switch kind {
        case .success:
            return .green
        case .info:
            return .accentColor
        case .error:
            return .red
        }
    }

    private func triggerRowAction(_ item: MenubarSessionItem) {
        guard viewModel.busyItemId == nil, item.presentation().isActionable else { return }
        viewModel.selectItem(item.id)
        Task {
            if item.state == .pendingReconcile {
                await viewModel.reconcile(workspacePath: item.workspacePath, itemId: item.id)
            } else {
                await viewModel.handoff(item)
            }
        }
    }

    private func pickCLIExecutable() {
        let panel = NSOpenPanel()
        panel.title = "选择 codex-agy-sync 命令行工具"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            viewModel.setCustomCLIPath(url.path)
        }
    }
}
