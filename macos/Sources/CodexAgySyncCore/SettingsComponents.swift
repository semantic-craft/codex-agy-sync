import SwiftUI

struct SettingsCard<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            content
        }
        .padding(13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 11)
                .fill(Color.primary.opacity(0.035))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 11)
                .stroke(Color.primary.opacity(0.09), lineWidth: 0.8)
        )
    }
}

struct SettingsActionButton: View {
    let title: String
    let systemImage: String
    let disabled: Bool
    let action: () -> Void

    init(
        _ title: String,
        systemImage: String,
        disabled: Bool = false,
        action: @escaping () -> Void
    ) {
        self.title = title
        self.systemImage = systemImage
        self.disabled = disabled
        self.action = action
    }

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: systemImage)
                    .font(.system(size: 11, weight: .medium))
                Text(title)
                    .font(.system(size: 11, weight: .semibold))
            }
            .foregroundStyle(.primary)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 7)
                    .fill(Color.primary.opacity(0.08))
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(FlatSurfaceButtonStyle())
        .focusEffectDisabled()
        .disabled(disabled)
        .accessibilityLabel(title)
        .accessibilityAction { action() }
    }
}

struct SettingsPrimaryActionButton: View {
    let title: String
    let systemImage: String
    let tint: Color
    let disabled: Bool
    let action: () -> Void

    init(
        _ title: String,
        systemImage: String,
        tint: Color = .accentColor,
        disabled: Bool,
        action: @escaping () -> Void
    ) {
        self.title = title
        self.systemImage = systemImage
        self.tint = tint
        self.disabled = disabled
        self.action = action
    }

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: systemImage)
                    .font(.system(size: 11, weight: .semibold))
                Text(title)
                    .font(.system(size: 11, weight: .semibold))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 7)
                    .fill(disabled ? tint.opacity(0.45) : tint)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(FlatSurfaceButtonStyle())
        .focusEffectDisabled()
        .disabled(disabled)
        .accessibilityLabel(title)
        .accessibilityAction { action() }
    }
}

struct SettingsSmallInlineButton: View {
    let title: String
    let action: () -> Void

    init(_ title: String, action: @escaping () -> Void) {
        self.title = title
        self.action = action
    }

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.primary)
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: 5)
                        .fill(Color.primary.opacity(0.08))
                )
                .contentShape(Rectangle())
        }
        .buttonStyle(FlatSurfaceButtonStyle())
        .focusEffectDisabled()
        .accessibilityLabel(title)
        .accessibilityAction { action() }
    }
}
