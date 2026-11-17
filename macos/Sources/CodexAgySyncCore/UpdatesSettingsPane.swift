import AppKit
import SwiftUI

struct UpdatesSettingsPane: View {
    let preferencesStore: PreferencesStore
    let updateViewModel: UpdateViewModel
    let onDidChangeCLI: (() -> Void)?

    private var lang: ResolvedLanguage {
        preferencesStore.effectiveLanguage
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SettingsCard {
                HStack(spacing: 8) {
                    Text(AppCopy.text(.updatesAlignmentCardTitle, in: lang))
                        .font(.system(size: 13, weight: .semibold))
                    Spacer()
                    alignmentBadge
                }

                HStack(spacing: 12) {
                    let appBoxTitle = AppCopy.text(.updatesAppBoxTitle, in: lang)
                    let appValue = updateViewModel.pendingRelaunchVersion != nil
                        ? "v\(updateViewModel.effectiveAppVersion)\(AppCopy.text(.updatesPendingRelaunchSuffix, in: lang))"
                        : "v\(updateViewModel.effectiveAppVersion)"
                    versionBox(title: appBoxTitle, value: appValue)
                    Image(systemName: "arrow.left.arrow.right")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.secondary)
                    versionBox(
                        title: AppCopy.text(.updatesCLIBoxTitle, in: lang),
                        value: updateViewModel.alignment.cliVersionLabel(in: lang)
                    )
                }

                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text(AppCopy.text(.updatesCLIPathLabel, in: lang))
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                        Text(updateViewModel.cliSource.displayName(in: lang))
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(
                                Capsule()
                                    .fill(Color.primary.opacity(0.08))
                            )
                    }
                    Text(updateViewModel.resolvedCLIPath ?? AppCopy.text(.updatesCLINotFoundPath, in: lang))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if updateViewModel.cliSource == .envOverride {
                        Text(AppCopy.text(.updatesCLIEnvHint, in: lang))
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                }

                HStack(spacing: 8) {
                    SettingsActionButton(AppCopy.text(.updatesChooseCLI, in: lang), systemImage: "folder") {
                        chooseCustomCLI()
                    }
                    if preferencesStore.preferences.customCLIPath != nil {
                        SettingsActionButton(
                            AppCopy.text(.updatesRestoreAutoDiscovery, in: lang),
                            systemImage: "arrow.uturn.backward"
                        ) {
                            preferencesStore.resetCustomCLIPath()
                            onDidChangeCLI?()
                            Task { await updateViewModel.probeEnvironment() }
                        }
                    }
                    Spacer()
                    SettingsActionButton(AppCopy.text(.updatesOpenStateDir, in: lang), systemImage: "externaldrive") {
                        updateViewModel.openStateDirectory()
                    }
                }
            }

            SettingsCard {
                HStack(spacing: 8) {
                    Text(AppCopy.text(.updatesPolicyCardTitle, in: lang))
                        .font(.system(size: 13, weight: .semibold))
                    Spacer()
                    if updateViewModel.availability.isBusy {
                        ProgressView()
                            .controlSize(.small)
                            .scaleEffect(0.8)
                    }
                }

                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Text(AppCopy.text(.updatesPolicyLabel, in: lang))
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text(updateViewModel.lastCheckedDisplayText ?? AppCopy.lastCheckedReadout(nil, in: lang))
                            .font(.system(size: 10.5))
                            .foregroundStyle(.secondary)
                    }

                    HStack(spacing: 6) {
                        ForEach(AutoUpdatePolicy.allCases) { policy in
                            let active = preferencesStore.preferences.autoUpdatePolicy == policy
                            let label = policy.displayName(in: lang)
                            Button {
                                preferencesStore.setAutoUpdatePolicy(policy)
                                Task { await updateViewModel.handlePolicyChanged(to: policy) }
                            } label: {
                                Text(label)
                                    .font(.system(size: 11, weight: .semibold))
                                    .lineLimit(1)
                                    .minimumScaleFactor(0.9)
                                    .foregroundStyle(active ? Color.white : Color.primary)
                                    .frame(maxWidth: .infinity)
                                    .padding(.vertical, 6)
                                    .padding(.horizontal, 6)
                                    .background(
                                        RoundedRectangle(cornerRadius: 7)
                                            .fill(active ? Color.accentColor : Color.primary.opacity(0.06))
                                    )
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(FlatSurfaceButtonStyle())
                            .focusEffectDisabled()
                            .accessibilityLabel(AppCopy.autoUpdatePolicyAccessibility(label, in: lang))
                            .accessibilityAction {
                                preferencesStore.setAutoUpdatePolicy(policy)
                                Task { await updateViewModel.handlePolicyChanged(to: policy) }
                            }
                        }
                    }

                    Text(preferencesStore.preferences.autoUpdatePolicy.helperDescription(in: lang))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    if let bgError = updateViewModel.lastBackgroundCheckError, !bgError.isEmpty {
                        Text(bgError)
                            .font(.system(size: 10.5))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                Divider()

                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text(AppCopy.text(.updatesLocalRepoLabel, in: lang))
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                        if let repoVer = updateViewModel.effectiveRepoVersion {
                            Text("v\(repoVer)")
                                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(Color.primary.opacity(0.07)))
                        }
                        Spacer()
                        SettingsSmallInlineButton(AppCopy.text(.updatesChooseLocalRepo, in: lang)) {
                            chooseLocalSourceRepo()
                        }
                        if preferencesStore.preferences.localRepoPathForSync != nil {
                            SettingsSmallInlineButton(AppCopy.text(.updatesClearLocalRepo, in: lang)) {
                                Task { await updateViewModel.clearLocalRepoPath() }
                            }
                        }
                    }

                    Text(updateViewModel.effectiveRepoPath ?? AppCopy.text(.updatesNoLocalRepoText, in: lang))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                if let statusMsg = statusReadoutText, !statusMsg.isEmpty {
                    Text(statusMsg)
                        .font(.system(size: 11))
                        .foregroundStyle(isFailedAvailability ? .red : .primary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: 7)
                                .fill((isFailedAvailability ? Color.red : Color.primary).opacity(0.06))
                        )
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel(AppCopy.statusReadoutAccessibility(statusMsg, in: lang))
                }

                HStack(spacing: 8) {
                    SettingsActionButton(
                        AppCopy.text(.updatesCheckNow, in: lang),
                        systemImage: "magnifyingglass",
                        disabled: updateViewModel.availability.isBusy
                    ) {
                        Task { await updateViewModel.checkForUpdates() }
                    }

                    if let staged = updateViewModel.stagedManifest {
                        SettingsPrimaryActionButton(
                            AppCopy.installStagedButtonTitle(tagName: staged.tagName, in: lang),
                            systemImage: "arrow.down.circle.fill",
                            tint: .green,
                            disabled: updateViewModel.availability.isBusy
                        ) {
                            Task { await updateViewModel.installStagedAndRelaunch() }
                        }
                    } else {
                        SettingsPrimaryActionButton(
                            AppCopy.text(.updatesSyncNow, in: lang),
                            systemImage: "arrow.triangle.2.circlepath",
                            disabled: updateViewModel.availability.isBusy
                        ) {
                            Task { await updateViewModel.syncUpdateNow() }
                        }
                    }

                    if updateViewModel.canRelaunchApp {
                        SettingsPrimaryActionButton(
                            AppCopy.text(.updatesRelaunchNow, in: lang),
                            systemImage: "power.circle.fill",
                            tint: .green,
                            disabled: false
                        ) {
                            _ = updateViewModel.relaunchApp()
                        }
                    }

                    Spacer()
                }
            }
        }
        .task {
            await updateViewModel.probeIfNeeded()
        }
    }

    private var statusReadoutText: String? {
        if case .updating(let step) = updateViewModel.availability {
            return step
        }
        return updateViewModel.statusMessage
    }

    private var isFailedAvailability: Bool {
        if case .failed = updateViewModel.availability {
            return true
        }
        return false
    }

    private var alignmentBadge: some View {
        let color: Color = switch updateViewModel.alignment {
        case .aligned:
            .green
        case .mismatched:
            .orange
        case .cliUnavailable:
            .red
        }
        let title = updateViewModel.alignment.badgeTitle(in: lang)

        return HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
            Text(title)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(color)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(
            Capsule()
                .fill(color.opacity(0.12))
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel(AppCopy.alignmentBadgeAccessibility(title, in: lang))
    }

    private func versionBox(title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 14, weight: .semibold, design: .monospaced))
                .foregroundStyle(.primary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(9)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.primary.opacity(0.045))
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel(AppCopy.versionBoxAccessibility(title: title, value: value, in: lang))
    }

    private func chooseCustomCLI() {
        let panel = NSOpenPanel()
        panel.title = AppCopy.text(.chooseCLIPanelTitle, in: lang)
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        NSApp.activate()
        if panel.runModal() == .OK, let url = panel.url {
            preferencesStore.setCustomCLIPath(url.path)
            onDidChangeCLI?()
            Task { await updateViewModel.probeEnvironment() }
        }
    }

    private func chooseLocalSourceRepo() {
        let panel = NSOpenPanel()
        panel.title = AppCopy.text(.updatesChooseLocalRepoPanelTitle, in: lang)
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        NSApp.activate()
        if panel.runModal() == .OK, let url = panel.url {
            preferencesStore.setLocalRepoPathForSync(url.path)
            Task { await updateViewModel.probeEnvironment() }
        }
    }
}
