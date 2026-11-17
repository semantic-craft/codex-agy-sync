import AppKit
import SwiftUI

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

struct PaletteSearchField: NSViewRepresentable {
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

struct PopoverSearchHeader: View {
    @Bindable var store: SessionSyncStore

    private var lang: ResolvedLanguage {
        store.preferencesStore.effectiveLanguage
    }

    var body: some View {
        HStack(spacing: 8) {
            Group {
                if store.isLoading {
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
                text: $store.searchQuery,
                placeholder: AppCopy.text(.searchPlaceholder, in: lang),
                accessibilityLabel: AppCopy.text(.searchAccessibility, in: lang)
            )
            .frame(maxWidth: .infinity, minHeight: 18)

            if !store.searchQuery.isEmpty {
                Button {
                    store.searchQuery = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 12))
                        .foregroundStyle(.tertiary)
                        .padding(2)
                        .contentShape(Rectangle())
                }
                .buttonStyle(FlatSurfaceButtonStyle())
                .focusEffectDisabled()
                .accessibilityLabel(AppCopy.text(.clearSearchAccessibility, in: lang))
                .accessibilityAction {
                    store.searchQuery = ""
                }
            }

            if store.preferencesStore.preferences.hotKey.isEnabled {
                KeycapBadge(text: store.preferencesStore.preferences.hotKey.displayString)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }
}
