import AppKit
import SwiftUI

struct PopoverActionBarFooter: View {
    let store: SessionSyncStore
    let onOpenSettings: ((SettingsTab) -> Void)?

    private var lang: ResolvedLanguage {
        store.preferencesStore.effectiveLanguage
    }

    var body: some View {
        let canContinue = (store.selectedItem?.presentation(language: lang).isActionable ?? false)
            && store.busyItemId == nil

        VStack(spacing: 0) {
            HairlineDivider()
            HStack(spacing: 5) {
                DaemonStatusIndicator(
                    agyActive: store.snapshot?.agyDaemonActive ?? false,
                    codexActive: store.snapshot?.codexDaemonActive ?? false,
                    language: lang
                )

                Spacer(minLength: 4)

                footerChip(
                    title: AppCopy.text(.footerContinue, in: lang),
                    shortcut: "↵",
                    accessibilityLabel: AppCopy.text(.footerContinueAX, in: lang),
                    isEnabled: canContinue
                ) {
                    Task { await store.performSelectedAction() }
                }

                footerChip(
                    title: AppCopy.text(.footerRefresh, in: lang),
                    shortcut: "⌘R",
                    accessibilityLabel: AppCopy.text(.footerRefreshAX, in: lang),
                    isEnabled: !store.isLoading
                ) {
                    Task { await store.refresh() }
                }

                footerChip(
                    title: AppCopy.text(.footerSettings, in: lang),
                    shortcut: "⌘,",
                    accessibilityLabel: AppCopy.text(.footerSettingsAX, in: lang),
                    isEnabled: true
                ) {
                    onOpenSettings?(.general)
                }

                footerChip(
                    title: AppCopy.text(.footerQuit, in: lang),
                    shortcut: "⌘Q",
                    accessibilityLabel: AppCopy.text(.footerQuitAX, in: lang),
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
}

private struct DaemonStatusIndicator: View {
    let agyActive: Bool
    let codexActive: Bool
    let language: ResolvedLanguage

    var body: some View {
        let (dotColor, statusText): (Color, String) = switch (agyActive, codexActive) {
        case (true, true):
            (.green, AppCopy.text(.daemonBothOnline, in: language))
        case (true, false):
            (.orange, AppCopy.text(.daemonAGYOnly, in: language))
        case (false, true):
            (.orange, AppCopy.text(.daemonCodexOnly, in: language))
        case (false, false):
            (Color.gray.opacity(0.55), AppCopy.text(.daemonBothOffline, in: language))
        }

        HStack(spacing: 5) {
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
}
