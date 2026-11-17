import SwiftUI

struct PaletteSessionRow: View {
    let item: MenubarSessionItem
    let nowEpoch: Double
    let language: ResolvedLanguage
    let isSelected: Bool
    let isBusy: Bool
    let isDisabled: Bool
    let onSelect: () -> Void
    let onTrigger: () -> Void

    @State private var isHovered = false

    var body: some View {
        let presentation = item.presentation(nowEpoch: nowEpoch, language: language)
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
