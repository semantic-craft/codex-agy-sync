import AppKit
import Carbon.HIToolbox
import SwiftUI

public enum SettingsTab: String, CaseIterable, Identifiable, Sendable {
    case general
    case appearance
    case updates

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .general:
            return "常规与接力"
        case .appearance:
            return "图标与外观"
        case .updates:
            return "版本与同步更新"
        }
    }

    public var iconName: String {
        switch self {
        case .general:
            return "slider.horizontal.3"
        case .appearance:
            return "sparkles.rectangle.stack"
        case .updates:
            return "arrow.triangle.2.circlepath.circle"
        }
    }

    public var accessibilityLabel: String {
        "设置分栏：\(title)"
    }
}

public struct SettingsView: View {
    @ObservedObject private var preferencesStore: PreferencesStore
    @ObservedObject private var updateViewModel: UpdateViewModel
    private let onDidChangeCLI: (() -> Void)?

    @State private var selectedTab: SettingsTab = .general
    @State private var keyMonitor: Any?

    public init(
        preferencesStore: PreferencesStore,
        updateViewModel: UpdateViewModel,
        initialTab: SettingsTab = .general,
        onDidChangeCLI: (() -> Void)? = nil
    ) {
        self.preferencesStore = preferencesStore
        self.updateViewModel = updateViewModel
        self._selectedTab = State(initialValue: initialTab)
        self.onDidChangeCLI = onDidChangeCLI
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            tabBar
            Divider()
            Group {
                switch selectedTab {
                case .general:
                    generalTab
                case .appearance:
                    appearanceTab
                case .updates:
                    updatesTab
                }
            }
        }
        .padding(18)
        .frame(width: 500)
        .background(Color(nsColor: .windowBackgroundColor))
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
                let isSelected = selectedTab == tab
                Button {
                    selectTab(tab)
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: tab.iconName)
                            .font(.system(size: 12, weight: .semibold))
                        Text(tab.title)
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
                .accessibilityLabel(tab.accessibilityLabel)
                .accessibilityAction {
                    selectTab(tab)
                }
            }
        }
    }

    private var generalTab: some View {
        VStack(alignment: .leading, spacing: 12) {
            settingsCard {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("登录时自动启动")
                            .font(.system(size: 13, weight: .semibold))
                        Text("进入 macOS 桌面后常驻菜单栏（零后台轮询，按需唤醒）")
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
                    .accessibilityLabel("登录时自动启动")
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
                        Text("全局快捷键")
                            .font(.system(size: 13, weight: .semibold))
                        Text("随时从任意桌面空间唤起会话接力")
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
                    .accessibilityLabel("启用全局快捷键")
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
                                    ? "按下新组合键… (Esc 取消)"
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
                            ? "正在录制快捷键，按 Esc 取消"
                            : "录制快捷键：\(preferencesStore.preferences.hotKey.displayString)"
                    )
                    .accessibilityAction {
                        toggleShortcutRecording()
                    }

                    if preferencesStore.preferences.hotKey != .defaultShortcut {
                        Button {
                            stopShortcutRecording()
                            preferencesStore.resetHotKeyToDefault()
                        } label: {
                            Text("恢复默认")
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
                        .accessibilityLabel("恢复默认快捷键")
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
                    Text("快捷键 \(preferencesStore.preferences.hotKey.displayString) 已被系统或其他应用占用，请录制其他组合键。")
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text("快捷键触发行为")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.secondary)

                    HStack(spacing: 6) {
                        ForEach(HotKeyBehavior.allCases) { mode in
                            let active = preferencesStore.preferences.hotKeyBehavior == mode
                            Button {
                                preferencesStore.setHotKeyBehavior(mode)
                            } label: {
                                Text(mode.displayName)
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
                            .accessibilityLabel("快捷键触发行为：\(mode.displayName)")
                            .accessibilityAction {
                                preferencesStore.setHotKeyBehavior(mode)
                            }
                        }
                    }
                }
            }

            settingsCard {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("交接完成后自动切到目标端应用")
                            .font(.system(size: 13, weight: .semibold))
                        Text("关闭后可在菜单栏面板连续批量交接多条会话，不切换前台窗口")
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
                    .accessibilityLabel("交接完成后自动切到目标端应用")
                }

                Divider()

                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("面板最近会话条数")
                            .font(.system(size: 13, weight: .semibold))
                        Text("控制点击菜单栏时单次读取展示的候选会话数量")
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
                                Text("\(count) 条")
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
                            .accessibilityLabel("面板最近会话条数：\(count) 条")
                            .accessibilityAction {
                                preferencesStore.setRecentFeedLimit(count)
                            }
                        }
                    }
                }
            }
        }
    }

    private var appearanceTab: some View {
        VStack(alignment: .leading, spacing: 12) {
            settingsCard {
                HStack(spacing: 14) {
                    Image(nsImage: NSApp.applicationIconImage)
                        .resizable()
                        .interpolation(.high)
                        .frame(width: 48, height: 48)
                        .clipShape(RoundedRectangle(cornerRadius: 11))

                    VStack(alignment: .leading, spacing: 3) {
                        Text("CodexAgyHandoff")
                            .font(.system(size: 15, weight: .semibold))
                        Text("v\(updateViewModel.appVersion) · 双轨接力桥应用图标与自适应单色模板状态栏图标")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }
            }

            settingsCard {
                Text("菜单栏状态图标样式")
                    .font(.system(size: 13, weight: .semibold))

                let columns = [
                    GridItem(.flexible(), spacing: 10),
                    GridItem(.flexible(), spacing: 10),
                ]
                LazyVGrid(columns: columns, spacing: 10) {
                    ForEach(MenuBarIconStyle.allCases) { style in
                        iconStyleCard(style)
                    }
                }

                Divider()

                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("存在未完成回复或待确认对话时显示状态栏圆点")
                            .font(.system(size: 12, weight: .semibold))
                        Text("在菜单栏模板图标右下角叠加高对比镂空圆点提示")
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
                    .accessibilityLabel("存在未完成回复或待确认对话时显示状态栏圆点")
                }
            }
        }
    }

    private func iconStyleCard(_ style: MenuBarIconStyle) -> some View {
        let isSelected = preferencesStore.preferences.menuBarIconStyle == style
        let previewImage = MenuBarIconRenderer.image(
            for: style,
            showAttentionDot: preferencesStore.preferences.showAttentionDotOnStatusIcon
        )

        return Button {
            preferencesStore.setMenuBarIconStyle(style)
        } label: {
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: 8)
                        .fill(Color.primary.opacity(0.08))
                        .frame(width: 36, height: 36)
                    Image(nsImage: previewImage)
                        .resizable()
                        .interpolation(.high)
                        .renderingMode(.template)
                        .foregroundStyle(.primary)
                        .frame(width: 20, height: 20)
                }

                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 5) {
                        Text(style.displayName)
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(.primary)
                        if isSelected {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.system(size: 11))
                                .foregroundStyle(Color.accentColor)
                        }
                    }
                    Text(style.subtitle)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                Spacer(minLength: 0)
            }
            .padding(10)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(isSelected ? Color.accentColor.opacity(0.11) : Color.primary.opacity(0.035))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .stroke(
                        isSelected ? Color.accentColor.opacity(0.65) : Color.primary.opacity(0.10),
                        lineWidth: isSelected ? 1.4 : 0.8
                    )
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(FlatSurfaceButtonStyle())
        .focusEffectDisabled()
        .accessibilityLabel("菜单栏图标：\(style.displayName)")
        .accessibilityAction {
            preferencesStore.setMenuBarIconStyle(style)
        }
    }

    private var updatesTab: some View {
        VStack(alignment: .leading, spacing: 12) {
            settingsCard {
                HStack(spacing: 8) {
                    Text("双端版本对齐看板")
                        .font(.system(size: 13, weight: .semibold))
                    Spacer()
                    alignmentBadge
                }

                HStack(spacing: 12) {
                    versionBox(
                        title: "菜单栏应用 App",
                        value: updateViewModel.pendingRelaunchVersion != nil
                            ? "v\(updateViewModel.effectiveAppVersion)（待重启）"
                            : "v\(updateViewModel.effectiveAppVersion)"
                    )
                    Image(systemName: "arrow.left.arrow.right")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.secondary)
                    versionBox(title: "同步引擎 CLI", value: updateViewModel.alignment.cliVersionLabel)
                }

                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text("CLI 路径")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                        Text(updateViewModel.cliSource.displayName)
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(
                                Capsule()
                                    .fill(Color.primary.opacity(0.08))
                            )
                    }
                    Text(updateViewModel.resolvedCLIPath ?? "未找到可执行文件")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if updateViewModel.cliSource == .envOverride {
                        Text("当前由环境变量 CODEX_AGY_SYNC_BIN 优先指定")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                }

                HStack(spacing: 8) {
                    actionButton("选择 CLI…", systemImage: "folder") {
                        chooseCustomCLI()
                    }
                    if preferencesStore.preferences.customCLIPath != nil {
                        actionButton("恢复自动发现", systemImage: "arrow.uturn.backward") {
                            preferencesStore.resetCustomCLIPath()
                            onDidChangeCLI?()
                            Task { await updateViewModel.probeEnvironment() }
                        }
                    }
                    Spacer()
                    actionButton("打开状态目录", systemImage: "externaldrive") {
                        updateViewModel.openStateDirectory()
                    }
                }
            }

            settingsCard {
                HStack(spacing: 8) {
                    Text("按需检查与双端同步更新")
                        .font(.system(size: 13, weight: .semibold))
                    Spacer()
                    if updateViewModel.availability.isBusy {
                        ProgressView()
                            .controlSize(.small)
                            .scaleEffect(0.8)
                    }
                }

                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text("本地源码仓")
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
                        smallInlineButton("选择本地源码仓…") {
                            chooseLocalSourceRepo()
                        }
                        if preferencesStore.preferences.localRepoPathForSync != nil {
                            smallInlineButton("清除") {
                                Task { await updateViewModel.clearLocalRepoPath() }
                            }
                        }
                    }

                    Text(updateViewModel.effectiveRepoPath ?? "未绑定（将通过 GitHub Release 校验升级）")
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
                        .accessibilityLabel("更新状态：\(statusMsg)")
                }

                HStack(spacing: 8) {
                    actionButton(
                        "检查更新",
                        systemImage: "magnifyingglass",
                        disabled: updateViewModel.availability.isBusy
                    ) {
                        Task { await updateViewModel.checkForUpdates() }
                    }

                    primaryActionButton(
                        "同步更新 App + CLI",
                        systemImage: "arrow.triangle.2.circlepath",
                        disabled: updateViewModel.availability.isBusy
                    ) {
                        Task { await updateViewModel.syncUpdateNow() }
                    }

                    if updateViewModel.canRelaunchApp {
                        primaryActionButton(
                            "立即重启应用",
                            systemImage: "power.circle.fill",
                            tint: .green,
                            disabled: false
                        ) {
                            updateViewModel.relaunchApp()
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

        return HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
            Text(updateViewModel.alignment.badgeTitle)
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
        .accessibilityLabel("版本状态：\(updateViewModel.alignment.badgeTitle)")
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
        .accessibilityLabel("\(title)：\(value)")
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
        selectedTab = tab
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
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard preferencesStore.isRecordingShortcut,
                  NSApp.keyWindow?.title == "CodexAgyHandoff 设置"
            else {
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
        panel.title = "选择 codex-agy-sync 可执行文件"
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
        panel.title = "选择 codex-agy-sync 本地源码仓库目录"
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
