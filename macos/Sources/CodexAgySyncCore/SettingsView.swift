import AppKit
import Carbon.HIToolbox
import SwiftUI

public enum SettingsTab: String, CaseIterable, Identifiable, Sendable {
    case general
    case updates

    public var id: String { rawValue }

    public var title: String {
        title(in: .zhHans)
    }

    public func title(in language: ResolvedLanguage) -> String {
        switch self {
        case .general:
            return AppCopy.text(.settingsTabGeneral, in: language)
        case .updates:
            return AppCopy.text(.settingsTabUpdates, in: language)
        }
    }

    public var iconName: String {
        switch self {
        case .general:
            return "slider.horizontal.3"
        case .updates:
            return "arrow.triangle.2.circlepath.circle"
        }
    }

    public var accessibilityLabel: String {
        accessibilityLabel(in: .zhHans)
    }

    public func accessibilityLabel(in language: ResolvedLanguage) -> String {
        AppCopy.settingsTabAccessibility(title(in: language), in: language)
    }
}

@MainActor
public final class SettingsNavigationModel: ObservableObject {
    @Published public var selectedTab: SettingsTab

    public init(selectedTab: SettingsTab = .general) {
        self.selectedTab = selectedTab
    }
}

@MainActor
private final class WeakWindowBox {
    weak var window: NSWindow?
}

private struct SettingsWindowAccessor: NSViewRepresentable {
    let onWindowResolve: (NSWindow?) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = WindowObservingNSView()
        view.onWindowResolve = onWindowResolve
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        if let observing = nsView as? WindowObservingNSView {
            observing.onWindowResolve = onWindowResolve
            observing.notifyWindowIfNeeded()
        }
    }

    private final class WindowObservingNSView: NSView {
        var onWindowResolve: ((NSWindow?) -> Void)?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            notifyWindowIfNeeded()
        }

        func notifyWindowIfNeeded() {
            onWindowResolve?(window)
        }
    }
}

public struct SettingsView: View {
    @ObservedObject private var preferencesStore: PreferencesStore
    @ObservedObject private var updateViewModel: UpdateViewModel
    @ObservedObject private var navigation: SettingsNavigationModel
    private let onDidChangeCLI: (() -> Void)?

    @State private var keyMonitor: Any?
    @State private var hostWindowBox = WeakWindowBox()

    public init(
        preferencesStore: PreferencesStore,
        updateViewModel: UpdateViewModel,
        initialTab: SettingsTab = .general,
        navigation: SettingsNavigationModel? = nil,
        onDidChangeCLI: (() -> Void)? = nil
    ) {
        self.preferencesStore = preferencesStore
        self.updateViewModel = updateViewModel
        self.navigation = navigation ?? SettingsNavigationModel(selectedTab: initialTab)
        self.onDidChangeCLI = onDidChangeCLI
    }

    private var lang: ResolvedLanguage {
        preferencesStore.effectiveLanguage
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            tabBar
            Divider()
            Group {
                switch navigation.selectedTab {
                case .general:
                    generalTab
                case .updates:
                    updatesTab
                }
            }
        }
        .padding(18)
        .frame(width: 520)
        .background(Color(nsColor: .windowBackgroundColor))
        .background(
            SettingsWindowAccessor { window in
                hostWindowBox.window = window
            }
        )
        .onChange(of: preferencesStore.isRecordingShortcut) { _, active in
            if !active {
                removeShortcutKeyMonitor()
            }
        }
        .onDisappear {
            stopShortcutRecording()
        }
    }

    private var tabBar: some View {
        HStack(spacing: 6) {
            ForEach(SettingsTab.allCases) { tab in
                let isSelected = navigation.selectedTab == tab
                Button {
                    selectTab(tab)
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: tab.iconName)
                            .font(.system(size: 12, weight: .semibold))
                        Text(tab.title(in: lang))
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .foregroundStyle(isSelected ? Color.primary : Color.secondary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 7)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .fill(isSelected ? Color.primary.opacity(0.11) : Color.primary.opacity(0.03))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(
                                isSelected ? Color.primary.opacity(0.22) : Color.primary.opacity(0.07),
                                lineWidth: 0.8
                            )
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(FlatSurfaceButtonStyle())
                .focusEffectDisabled()
                .accessibilityLabel(tab.accessibilityLabel(in: lang))
                .accessibilityAction {
                    selectTab(tab)
                }
            }
        }
    }

    private var generalTab: some View {
        VStack(alignment: .leading, spacing: 12) {
            settingsCard {
                VStack(alignment: .leading, spacing: 6) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(AppCopy.text(.settingsLanguageTitle, in: lang))
                            .font(.system(size: 13, weight: .semibold))
                        Text(AppCopy.text(.settingsLanguageSubtitle, in: lang))
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }

                    HStack(spacing: 6) {
                        ForEach(AppLanguage.allCases) { option in
                            let active = preferencesStore.preferences.language == option
                            Button {
                                preferencesStore.setLanguage(option)
                            } label: {
                                Text(option.displayName(in: lang))
                                    .font(.system(size: 11, weight: .semibold))
                                    .foregroundStyle(active ? Color.white : Color.primary)
                                    .frame(maxWidth: .infinity)
                                    .padding(.vertical, 6)
                                    .background(
                                        RoundedRectangle(cornerRadius: 7)
                                            .fill(active ? Color.accentColor : Color.primary.opacity(0.06))
                                    )
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(FlatSurfaceButtonStyle())
                            .focusEffectDisabled()
                            .accessibilityLabel(AppCopy.languageOptionAccessibility(option, in: lang))
                            .accessibilityAction {
                                preferencesStore.setLanguage(option)
                            }
                        }
                    }
                }
            }

            settingsCard {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(AppCopy.text(.settingsLaunchAtLoginTitle, in: lang))
                            .font(.system(size: 13, weight: .semibold))
                        Text(AppCopy.text(.settingsLaunchAtLoginSubtitle, in: lang))
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Toggle(
                        "",
                        isOn: Binding(
                            get: { preferencesStore.launchAtLogin },
                            set: { preferencesStore.setLaunchAtLogin($0) }
                        )
                    )
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .focusEffectDisabled()
                    .accessibilityLabel(AppCopy.text(.settingsLaunchAtLoginTitle, in: lang))
                }

                if let loginErr = preferencesStore.loginItemError, !loginErr.isEmpty {
                    Text(loginErr)
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                }
            }

            settingsCard {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(AppCopy.text(.settingsHotKeyTitle, in: lang))
                            .font(.system(size: 13, weight: .semibold))
                        Text(AppCopy.text(.settingsHotKeySubtitle, in: lang))
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Toggle(
                        "",
                        isOn: Binding(
                            get: { preferencesStore.preferences.hotKey.isEnabled },
                            set: { enabled in
                                if !enabled {
                                    stopShortcutRecording()
                                }
                                var next = preferencesStore.preferences.hotKey
                                next.isEnabled = enabled
                                preferencesStore.setHotKey(next)
                            }
                        )
                    )
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .focusEffectDisabled()
                    .accessibilityLabel(AppCopy.text(.settingsHotKeyEnableAX, in: lang))
                }

                HStack(spacing: 8) {
                    let recording = preferencesStore.isRecordingShortcut
                    Button {
                        toggleShortcutRecording()
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: recording ? "record.circle.fill" : "keyboard")
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundStyle(recording ? .orange : .secondary)
                            Text(
                                recording
                                    ? AppCopy.text(.settingsHotKeyRecordingPrompt, in: lang)
                                    : preferencesStore.preferences.hotKey.displayString
                            )
                            .font(.system(size: 12, weight: .semibold, design: recording ? .default : .monospaced))
                        }
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(
                            RoundedRectangle(cornerRadius: 7)
                                .fill(recording ? Color.orange.opacity(0.14) : Color.primary.opacity(0.07))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 7)
                                .stroke(
                                    recording ? Color.orange.opacity(0.55) : Color.primary.opacity(0.14),
                                    lineWidth: 0.8
                                )
                        )
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(FlatSurfaceButtonStyle())
                    .focusEffectDisabled()
                    .disabled(!preferencesStore.preferences.hotKey.isEnabled)
                    .accessibilityLabel(
                        recording
                            ? AppCopy.text(.settingsHotKeyRecordingAX, in: lang)
                            : AppCopy.hotKeyRecordButtonAccessibility(
                                shortcut: preferencesStore.preferences.hotKey.displayString,
                                in: lang
                            )
                    )
                    .accessibilityAction {
                        toggleShortcutRecording()
                    }

                    if preferencesStore.preferences.hotKey != .defaultShortcut {
                        Button {
                            stopShortcutRecording()
                            preferencesStore.resetHotKeyToDefault()
                        } label: {
                            Text(AppCopy.text(.settingsHotKeyReset, in: lang))
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 9)
                                .padding(.vertical, 6)
                                .background(
                                    RoundedRectangle(cornerRadius: 7)
                                        .fill(Color.primary.opacity(0.06))
                                )
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(FlatSurfaceButtonStyle())
                        .focusEffectDisabled()
                        .accessibilityLabel(AppCopy.text(.settingsHotKeyResetAX, in: lang))
                        .accessibilityAction {
                            stopShortcutRecording()
                            preferencesStore.resetHotKeyToDefault()
                        }
                    }

                    Spacer()
                }

                if preferencesStore.hotKeyRegistrationFailed,
                   preferencesStore.preferences.hotKey.isEnabled
                {
                    Text(
                        AppCopy.hotKeyConflictWarning(
                            shortcut: preferencesStore.preferences.hotKey.displayString,
                            in: lang
                        )
                    )
                    .font(.system(size: 11))
                    .foregroundStyle(.red)
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text(AppCopy.text(.settingsHotKeyBehaviorLabel, in: lang))
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.secondary)

                    HStack(spacing: 6) {
                        ForEach(HotKeyBehavior.allCases) { mode in
                            let active = preferencesStore.preferences.hotKeyBehavior == mode
                            let label = mode.displayName(in: lang)
                            Button {
                                preferencesStore.setHotKeyBehavior(mode)
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
                            .accessibilityLabel(AppCopy.hotKeyBehaviorAccessibility(label, in: lang))
                            .accessibilityAction {
                                preferencesStore.setHotKeyBehavior(mode)
                            }
                        }
                    }

                    Text(preferencesStore.preferences.hotKeyBehavior.helperDescription(in: lang))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            settingsCard {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(AppCopy.text(.settingsAutoActivateTitle, in: lang))
                            .font(.system(size: 13, weight: .semibold))
                        Text(AppCopy.text(.settingsAutoActivateSubtitle, in: lang))
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Toggle(
                        "",
                        isOn: Binding(
                            get: { preferencesStore.preferences.activateTargetAppAfterHandoff },
                            set: { preferencesStore.setActivateTargetAppAfterHandoff($0) }
                        )
                    )
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .focusEffectDisabled()
                    .accessibilityLabel(AppCopy.text(.settingsAutoActivateTitle, in: lang))
                }

                Divider()

                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(AppCopy.text(.settingsFeedLimitTitle, in: lang))
                            .font(.system(size: 13, weight: .semibold))
                        Text(AppCopy.text(.settingsFeedLimitSubtitle, in: lang))
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    HStack(spacing: 6) {
                        ForEach(AppPreferences.supportedFeedLimits, id: \.self) { count in
                            let selected = preferencesStore.preferences.recentFeedLimit == count
                            Button {
                                preferencesStore.setRecentFeedLimit(count)
                            } label: {
                                Text(AppCopy.feedLimitOptionLabel(count, in: lang))
                                    .font(.system(size: 11, weight: .semibold))
                                    .foregroundStyle(selected ? Color.white : Color.primary)
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 5)
                                    .background(
                                        RoundedRectangle(cornerRadius: 6)
                                            .fill(selected ? Color.accentColor : Color.primary.opacity(0.07))
                                    )
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(FlatSurfaceButtonStyle())
                            .focusEffectDisabled()
                            .accessibilityLabel(AppCopy.feedLimitOptionAccessibility(count, in: lang))
                            .accessibilityAction {
                                preferencesStore.setRecentFeedLimit(count)
                            }
                        }
                    }
                }

                Divider()

                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(AppCopy.text(.settingsAttentionDotTitle, in: lang))
                            .font(.system(size: 12, weight: .semibold))
                        Text(AppCopy.text(.settingsAttentionDotSubtitle, in: lang))
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Toggle(
                        "",
                        isOn: Binding(
                            get: { preferencesStore.preferences.showAttentionDotOnStatusIcon },
                            set: { preferencesStore.setShowAttentionDot($0) }
                        )
                    )
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .focusEffectDisabled()
                    .accessibilityLabel(AppCopy.text(.settingsAttentionDotTitle, in: lang))
                }
            }
        }
    }

    private var updatesTab: some View {
        VStack(alignment: .leading, spacing: 12) {
            settingsCard {
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
                    actionButton(AppCopy.text(.updatesChooseCLI, in: lang), systemImage: "folder") {
                        chooseCustomCLI()
                    }
                    if preferencesStore.preferences.customCLIPath != nil {
                        actionButton(
                            AppCopy.text(.updatesRestoreAutoDiscovery, in: lang),
                            systemImage: "arrow.uturn.backward"
                        ) {
                            preferencesStore.resetCustomCLIPath()
                            onDidChangeCLI?()
                            Task { await updateViewModel.probeEnvironment() }
                        }
                    }
                    Spacer()
                    actionButton(AppCopy.text(.updatesOpenStateDir, in: lang), systemImage: "externaldrive") {
                        updateViewModel.openStateDirectory()
                    }
                }
            }

            settingsCard {
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
                        smallInlineButton(AppCopy.text(.updatesChooseLocalRepo, in: lang)) {
                            chooseLocalSourceRepo()
                        }
                        if preferencesStore.preferences.localRepoPathForSync != nil {
                            smallInlineButton(AppCopy.text(.updatesClearLocalRepo, in: lang)) {
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
                    actionButton(
                        AppCopy.text(.updatesCheckNow, in: lang),
                        systemImage: "magnifyingglass",
                        disabled: updateViewModel.availability.isBusy
                    ) {
                        Task { await updateViewModel.checkForUpdates() }
                    }

                    if let staged = updateViewModel.stagedManifest {
                        primaryActionButton(
                            AppCopy.installStagedButtonTitle(tagName: staged.tagName, in: lang),
                            systemImage: "arrow.down.circle.fill",
                            tint: .green,
                            disabled: updateViewModel.availability.isBusy
                        ) {
                            Task { await updateViewModel.installStagedAndRelaunch() }
                        }
                    } else {
                        primaryActionButton(
                            AppCopy.text(.updatesSyncNow, in: lang),
                            systemImage: "arrow.triangle.2.circlepath",
                            disabled: updateViewModel.availability.isBusy
                        ) {
                            Task { await updateViewModel.syncUpdateNow() }
                        }
                    }

                    if updateViewModel.canRelaunchApp {
                        primaryActionButton(
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
        let color: Color = {
            switch updateViewModel.alignment {
            case .aligned:
                return .green
            case .mismatched:
                return .orange
            case .cliUnavailable:
                return .red
            }
        }()
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

    private func settingsCard<Content: View>(
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            content()
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

    private func actionButton(
        _ title: String,
        systemImage: String,
        disabled: Bool = false,
        action: @escaping () -> Void
    ) -> some View {
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

    private func primaryActionButton(
        _ title: String,
        systemImage: String,
        tint: Color = .accentColor,
        disabled: Bool,
        action: @escaping () -> Void
    ) -> some View {
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

    private func smallInlineButton(_ title: String, action: @escaping () -> Void) -> some View {
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

    private func selectTab(_ tab: SettingsTab) {
        stopShortcutRecording()
        navigation.selectedTab = tab
        DispatchQueue.main.async {
            NSApp.keyWindow?.makeFirstResponder(nil)
        }
    }

    private func toggleShortcutRecording() {
        if preferencesStore.isRecordingShortcut {
            stopShortcutRecording()
        } else {
            startShortcutRecording()
        }
    }

    private func startShortcutRecording() {
        stopShortcutRecording()
        preferencesStore.setShortcutRecording(true)
        let hostBox = hostWindowBox
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            let eventWindow = event.window
            let keyWindow = NSApp.keyWindow
            guard SettingsWindowMatch.shouldCaptureKeyEvent(
                isRecording: preferencesStore.isRecordingShortcut,
                hostWindow: hostBox.window,
                keyWindow: keyWindow,
                eventWindow: eventWindow
            ) else {
                return event
            }
            if Int(event.keyCode) == kVK_Escape {
                stopShortcutRecording()
                return nil
            }
            if let recorded = HotKeyShortcut.from(
                keyCode: event.keyCode,
                modifierFlags: event.modifierFlags,
                charactersIgnoringModifiers: event.charactersIgnoringModifiers,
                isEnabled: preferencesStore.preferences.hotKey.isEnabled
            ) {
                preferencesStore.setHotKey(recorded)
                stopShortcutRecording()
                return nil
            }
            return nil
        }
    }

    private func stopShortcutRecording() {
        removeShortcutKeyMonitor()
        preferencesStore.setShortcutRecording(false)
    }

    private func removeShortcutKeyMonitor() {
        if let monitor = keyMonitor {
            NSEvent.removeMonitor(monitor)
            keyMonitor = nil
        }
    }

    private func chooseCustomCLI() {
        let panel = NSOpenPanel()
        panel.title = AppCopy.text(.chooseCLIPanelTitle, in: lang)
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        NSApp.activate(ignoringOtherApps: true)
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
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            preferencesStore.setLocalRepoPathForSync(url.path)
            Task { await updateViewModel.probeEnvironment() }
        }
    }
}
