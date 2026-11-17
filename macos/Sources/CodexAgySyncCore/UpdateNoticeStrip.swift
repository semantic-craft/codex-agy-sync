import SwiftUI

struct UpdateNoticeStrip: View {
    let updateViewModel: UpdateViewModel
    let onOpenSettings: ((SettingsTab) -> Void)?

    var body: some View {
        if let notice = updateViewModel.popoverNotice {
            HairlineDivider()
            noticeRow(notice)
        }
    }

    private func noticeRow(_ notice: PopoverUpdateNotice) -> some View {
        let accentTint: Color = switch notice.action {
        case .openUpdatesSettings:
            .accentColor
        case .installStagedAndRelaunch, .relaunchInstalled:
            .green
        }
        let iconName: String = switch notice.action {
        case .openUpdatesSettings:
            "arrow.down.circle.fill"
        case .installStagedAndRelaunch:
            "checkmark.circle.fill"
        case .relaunchInstalled:
            "arrow.clockwise.circle.fill"
        }
        let isBusy = updateViewModel.availability.isBusy

        return HStack(spacing: 8) {
            Image(systemName: iconName)
                .font(.system(size: 11.5, weight: .semibold))
                .foregroundStyle(accentTint)

            Text(notice.title)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)

            Button {
                trigger(notice.action)
            } label: {
                Text(notice.actionTitle)
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3.5)
                    .background(
                        RoundedRectangle(cornerRadius: 5, style: .continuous)
                            .fill(isBusy ? accentTint.opacity(0.45) : accentTint)
                    )
                    .contentShape(Rectangle())
            }
            .buttonStyle(FlatSurfaceButtonStyle())
            .focusEffectDisabled()
            .disabled(isBusy)
            .accessibilityLabel(notice.actionTitle)
            .accessibilityAction {
                guard !isBusy else { return }
                trigger(notice.action)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(accentTint.opacity(0.08))
    }

    private func trigger(_ action: PopoverUpdateAction) {
        switch action {
        case .openUpdatesSettings:
            onOpenSettings?(.updates)
        case .installStagedAndRelaunch:
            Task { await updateViewModel.installStagedAndRelaunch() }
        case .relaunchInstalled:
            _ = updateViewModel.relaunchApp()
        }
    }
}
