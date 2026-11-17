import AppKit
import SwiftUI

public struct PopoverView: View {
    private let store: SessionSyncStore
    private let updateViewModel: UpdateViewModel
    private let onOpenSettings: ((SettingsTab) -> Void)?

    public init(
        store: SessionSyncStore,
        updateViewModel: UpdateViewModel,
        onOpenSettings: ((SettingsTab) -> Void)? = nil
    ) {
        self.store = store
        self.updateViewModel = updateViewModel
        self.onOpenSettings = onOpenSettings
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            PopoverSearchHeader(store: store)
            HairlineDivider()
            PopoverBannerSection(store: store)
            PopoverCommandStreamSection(store: store)
            UpdateNoticeStrip(
                updateViewModel: updateViewModel,
                onOpenSettings: onOpenSettings
            )
            PopoverActionBarFooter(
                store: store,
                onOpenSettings: onOpenSettings
            )
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
}

private struct PopoverBannerSection: View {
    let store: SessionSyncStore

    private var lang: ResolvedLanguage {
        store.preferencesStore.effectiveLanguage
    }

    var body: some View {
        if store.banner != nil || store.cliMissing {
            VStack(alignment: .leading, spacing: 6) {
                if let banner = store.banner {
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
                            store.dismissBanner()
                        } label: {
                            Image(systemName: "xmark")
                                .font(.system(size: 10, weight: .medium))
                                .foregroundStyle(.secondary)
                                .padding(3)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(FlatSurfaceButtonStyle())
                        .focusEffectDisabled()
                        .accessibilityLabel(AppCopy.dismissBannerAccessibility(banner.text, in: lang))
                        .accessibilityAction {
                            store.dismissBanner()
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

                if store.cliMissing {
                    let chooseTitle = AppCopy.text(.chooseCLIButton, in: lang)
                    Button {
                        pickCLIExecutable()
                    } label: {
                        Text(chooseTitle)
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
                    .accessibilityLabel(chooseTitle)
                    .accessibilityAction {
                        pickCLIExecutable()
                    }
                }
            }
            .padding(.horizontal, 8)
            .padding(.top, 8)
            .padding(.bottom, 2)
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

    private func pickCLIExecutable() {
        let panel = NSOpenPanel()
        panel.title = AppCopy.text(.chooseCLIPanelTitle, in: lang)
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        NSApp.activate()
        if panel.runModal() == .OK, let url = panel.url {
            store.setCustomCLIPath(url.path)
        }
    }
}

private struct PopoverCommandStreamSection: View {
    let store: SessionSyncStore

    private var lang: ResolvedLanguage {
        store.preferencesStore.effectiveLanguage
    }

    var body: some View {
        let currentSections = store.sections
        let nowEpoch = store.feedReferenceEpoch

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
                            let isRowBusy = store.busyItemId == item.id
                                || (item.state == .pendingReconcile && store.busyItemId == item.workspacePath)
                            PaletteSessionRow(
                                item: item,
                                nowEpoch: nowEpoch,
                                language: lang,
                                isSelected: store.selectedItemId == item.id,
                                isBusy: isRowBusy,
                                isDisabled: store.busyItemId != nil,
                                onSelect: { store.selectItem(item.id) },
                                onTrigger: { triggerRowAction(item) }
                            )
                        }
                    }
                }
            }
            .padding(6)
        } else if store.snapshot == nil && store.isLoading {
            let loadingTitle = AppCopy.text(.loadingSessions, in: lang)
            emptyStateCard(label: loadingTitle) {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                    Text(loadingTitle)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                }
            }
        } else if !store.searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let emptyTitle = AppCopy.text(.searchEmptyTitle, in: lang)
            let emptyHint = AppCopy.text(.searchEmptyHint, in: lang)
            emptyStateCard(label: "\(emptyTitle) · \(emptyHint)") {
                VStack(spacing: 4) {
                    Text(emptyTitle)
                        .font(.system(size: 12.5, weight: .medium))
                        .foregroundStyle(.primary)
                    Text(emptyHint)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
        } else {
            let emptyTitle = AppCopy.text(.emptyFeedTitle, in: lang)
            let emptyHint = AppCopy.text(.emptyFeedHint, in: lang)
            emptyStateCard(label: emptyTitle) {
                VStack(spacing: 4) {
                    Text(emptyTitle)
                        .font(.system(size: 12.5, weight: .medium))
                        .foregroundStyle(.primary)
                    Text(emptyHint)
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

    private func triggerRowAction(_ item: MenubarSessionItem) {
        guard store.busyItemId == nil, item.presentation(language: lang).isActionable else { return }
        store.selectItem(item.id)
        Task {
            if item.state == .pendingReconcile {
                await store.reconcile(workspacePath: item.workspacePath, itemId: item.id)
            } else {
                await store.sync(item)
            }
        }
    }
}
