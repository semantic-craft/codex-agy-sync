import AppKit
import Foundation

public enum AppLanguage: String, Codable, CaseIterable, Sendable, Identifiable {
    case system = "system"
    case zhHans = "zh-Hans"
    case en = "en"

    public var id: String { rawValue }

    public func resolve(preferredLanguages: [String] = Locale.preferredLanguages) -> ResolvedLanguage {
        switch self {
        case .zhHans:
            return .zhHans
        case .en:
            return .en
        case .system:
            var sawTraditionalChinese = false
            for tag in preferredLanguages {
                let normalized = tag
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                    .replacingOccurrences(of: "_", with: "-")
                    .lowercased()
                if normalized.isEmpty {
                    continue
                }
                if normalized.hasPrefix("zh-hant")
                    || normalized == "zh-tw" || normalized.hasPrefix("zh-tw-")
                    || normalized == "zh-hk" || normalized.hasPrefix("zh-hk-")
                    || normalized == "zh-mo" || normalized.hasPrefix("zh-mo-")
                {
                    sawTraditionalChinese = true
                    continue
                }
                if normalized == "zh"
                    || normalized.hasPrefix("zh-hans")
                    || normalized.hasPrefix("zh-cn")
                    || normalized.hasPrefix("zh-sg")
                    || normalized.hasPrefix("zh-")
                {
                    return .zhHans
                }
                if normalized == "en" || normalized.hasPrefix("en-") {
                    return .en
                }
            }
            return sawTraditionalChinese ? .zhHans : .en
        }
    }

    public func displayName(in language: ResolvedLanguage) -> String {
        switch self {
        case .system:
            return AppCopy.text(.languageSystem, in: language)
        case .zhHans:
            return "简体中文"
        case .en:
            return "English"
        }
    }
}

public enum ResolvedLanguage: String, Codable, CaseIterable, Sendable, Equatable {
    case zhHans = "zh-Hans"
    case en = "en"
}

public struct BilingualText: Equatable, Sendable {
    public let zhHans: String
    public let en: String

    public init(zhHans: String, en: String) {
        self.zhHans = zhHans
        self.en = en
    }

    public func resolve(in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return zhHans
        case .en:
            return en
        }
    }
}

public enum SettingsWindowIdentity {
    public static let identifier = NSUserInterfaceItemIdentifier("dev.semanticcraft.codex-agy-sync.settings")
}

@MainActor
public enum SettingsWindowMatch {
    public static func isSettingsWindow(_ window: NSWindow?) -> Bool {
        window?.identifier == SettingsWindowIdentity.identifier
    }

    public static func shouldCaptureKeyEvent(
        isRecording: Bool,
        hostWindow: NSWindow?,
        keyWindow: NSWindow?,
        eventWindow: NSWindow?
    ) -> Bool {
        guard isRecording else { return false }
        if let hostWindow {
            guard isSettingsWindow(hostWindow) else { return false }
            if let eventWindow {
                return eventWindow === hostWindow
            }
            return keyWindow === hostWindow
        }
        if let eventWindow {
            return isSettingsWindow(eventWindow)
        }
        return isSettingsWindow(keyWindow)
    }
}

@MainActor
public enum AppDateFormatting {
    private static let zhFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_Hans")
        formatter.dateStyle = .short
        formatter.timeStyle = .short
        return formatter
    }()

    private static let enFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.dateStyle = .short
        formatter.timeStyle = .short
        return formatter
    }()

    private static let zhTimeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_Hans")
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return formatter
    }()

    private static let enTimeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return formatter
    }()

    public static func formatCheckTimestamp(_ epoch: TimeInterval, now: Date = Date(), in language: ResolvedLanguage) -> String {
        let date = Date(timeIntervalSince1970: epoch)
        let isToday = Calendar.current.isDate(date, inSameDayAs: now)
        switch (language, isToday) {
        case (.zhHans, true):
            return zhTimeFormatter.string(from: date)
        case (.zhHans, false):
            return zhFormatter.string(from: date)
        case (.en, true):
            return enTimeFormatter.string(from: date)
        case (.en, false):
            return enFormatter.string(from: date)
        }
    }
}

public enum AppCopyKey: CaseIterable, Sendable {
    case appTitle
    case statusItemLabel
    case languageSystem

    case sectionContinue
    case sectionRecent
    case stateInterruptedBadge
    case stateCompletedBadge
    case statePendingBadge
    case stateHandedBadge
    case stateActiveBadge
    case pendingSubtitle
    case pendingActionTitle
    case pendingTrailingTitle
    case activeSubtitle
    case relativeJustNow

    case cliSourceEnv
    case cliSourceCustom
    case cliSourceAuto
    case cliSourceMissing
    case cliMissingError

    case handoffUnconfirmedBanner
    case reconcileSyncedBanner

    case searchPlaceholder
    case searchAccessibility
    case clearSearchAccessibility
    case chooseCLIButton
    case chooseCLIPanelTitle
    case loadingSessions
    case searchEmptyTitle
    case searchEmptyHint
    case emptyFeedTitle
    case emptyFeedHint
    case footerContinue
    case footerContinueAX
    case footerRefresh
    case footerRefreshAX
    case footerSettings
    case footerSettingsAX
    case footerQuit
    case footerQuitAX
    case daemonBothOnline
    case daemonAGYOnly
    case daemonCodexOnly
    case daemonBothOffline

    case settingsWindowTitle
    case settingsTabGeneral
    case settingsTabUpdates
    case settingsLanguageTitle
    case settingsLanguageSubtitle
    case settingsLaunchAtLoginTitle
    case settingsLaunchAtLoginSubtitle
    case settingsHotKeyTitle
    case settingsHotKeySubtitle
    case settingsHotKeyEnableAX
    case settingsHotKeyRecordingPrompt
    case settingsHotKeyRecordingAX
    case settingsHotKeyReset
    case settingsHotKeyResetAX
    case settingsHotKeyBehaviorLabel
    case hotKeyTogglePanelLabel
    case hotKeyTogglePanelHelper
    case hotKeyInstantContinueLabel
    case hotKeyInstantContinueHelper
    case settingsAutoActivateTitle
    case settingsAutoActivateSubtitle
    case settingsFeedLimitTitle
    case settingsFeedLimitSubtitle
    case settingsAttentionDotTitle
    case settingsAttentionDotSubtitle

    case updatesAlignmentCardTitle
    case updatesAppBoxTitle
    case updatesCLIBoxTitle
    case updatesPendingRelaunchSuffix
    case updatesCLIPathLabel
    case updatesCLINotFoundPath
    case updatesCLIEnvHint
    case updatesChooseCLI
    case updatesRestoreAutoDiscovery
    case updatesOpenStateDir
    case updatesPolicyCardTitle
    case updatesPolicyLabel
    case updatesPolicyOffLabel
    case updatesPolicyOffHelper
    case updatesPolicyCheckOnlyLabel
    case updatesPolicyCheckOnlyHelper
    case updatesPolicyAutoStageLabel
    case updatesPolicyAutoStageHelper
    case updatesNeverChecked
    case updatesLocalRepoLabel
    case updatesChooseLocalRepo
    case updatesChooseLocalRepoPanelTitle
    case updatesClearLocalRepo
    case updatesNoLocalRepoText
    case updatesCheckNow
    case updatesSyncNow
    case updatesRelaunchNow
    case updatesNotProbed
    case updatesCLIPendingProbe
    case updatesCLINotReady
    case updatesAlignmentAligned
    case updatesAlignmentMismatched
    case updatesAlignmentUnavailable
    case updatesDefaultNotesSummary

    case updatesStepBuildingLocal
    case updatesStepUpgradingLocalCLI
    case updatesStepDownloadingChecksums
    case updatesStepVerifyingApp
    case updatesStepInstallingCLI
    case updatesNoticeRelaunchAction
    case updatesNoticeInstallStagedAction
    case updatesNoticeOpenSettingsAction

    case updatesErrMissingChecksums
    case updatesErrMissingAssets
    case updatesErrPythonMissing
    case updatesErrHandoffInProgress
    case updatesErrInstallationInProgress
    case updatesErrBundlePathMissing
    case updatesErrNoWritableBundleFallback

    case menuSettings
    case menuQuit
    case menuEdit
    case menuUndo
    case menuRedo
    case menuCut
    case menuCopy
    case menuPaste
    case menuSelectAll
}

public enum AppCopy {
    public static func entry(for key: AppCopyKey) -> BilingualText {
        switch key {
        case .appTitle:
            return BilingualText(zhHans: "CodexAgySync", en: "CodexAgySync")
        case .statusItemLabel:
            return BilingualText(zhHans: "会话同步", en: "Session Sync")
        case .languageSystem:
            return BilingualText(zhHans: "跟随系统", en: "Follow System")

        case .sectionContinue:
            return BilingualText(zhHans: "继续当前工作", en: "Continue Current Work")
        case .sectionRecent:
            return BilingualText(zhHans: "最近对话", en: "Recent Sessions")
        case .stateInterruptedBadge:
            return BilingualText(zhHans: "未完成回复", en: "Incomplete Reply")
        case .stateCompletedBadge:
            return BilingualText(zhHans: "可继续", en: "Ready")
        case .statePendingBadge:
            return BilingualText(zhHans: "待确认", en: "Needs Confirm")
        case .stateHandedBadge:
            return BilingualText(zhHans: "已同步", en: "Synced")
        case .stateActiveBadge:
            return BilingualText(zhHans: "对话中", en: "Active")
        case .pendingSubtitle:
            return BilingualText(zhHans: "上次同步未完成确认", en: "Previous sync unconfirmed")
        case .pendingActionTitle:
            return BilingualText(zhHans: "重新同步状态", en: "Resync Status")
        case .pendingTrailingTitle:
            return BilingualText(zhHans: "重试", en: "Retry")
        case .activeSubtitle:
            return BilingualText(zhHans: "对话进行中", en: "Session active")
        case .relativeJustNow:
            return BilingualText(zhHans: "刚刚", en: "Just now")

        case .cliSourceEnv:
            return BilingualText(zhHans: "环境变量", en: "Environment")
        case .cliSourceCustom:
            return BilingualText(zhHans: "手动指定", en: "Custom Path")
        case .cliSourceAuto:
            return BilingualText(zhHans: "自动发现", en: "Auto-Discovered")
        case .cliSourceMissing:
            return BilingualText(zhHans: "未找到", en: "Not Found")
        case .cliMissingError:
            return BilingualText(
                zhHans: "未找到 codex-agy-sync 命令行工具，请点击“选择工具…”指定路径。",
                en: "codex-agy-sync CLI not found. Click \"Choose CLI…\" to locate it."
            )

        case .handoffUnconfirmedBanner:
            return BilingualText(
                zhHans: "同步尚未完成确认，请稍后点击重试同步状态。",
                en: "Sync is not confirmed yet. Click Retry to resync status."
            )
        case .reconcileSyncedBanner:
            return BilingualText(zhHans: "状态已同步", en: "Status synced")

        case .searchPlaceholder:
            return BilingualText(zhHans: "筛选项目或对话标题…", en: "Filter projects or session titles…")
        case .searchAccessibility:
            return BilingualText(zhHans: "筛选项目或对话标题", en: "Filter projects or session titles")
        case .clearSearchAccessibility:
            return BilingualText(zhHans: "清空筛选", en: "Clear search")
        case .chooseCLIButton:
            return BilingualText(zhHans: "选择工具…", en: "Choose CLI…")
        case .chooseCLIPanelTitle:
            return BilingualText(zhHans: "选择 codex-agy-sync 命令行工具", en: "Choose codex-agy-sync CLI")
        case .loadingSessions:
            return BilingualText(zhHans: "正在读取最近对话…", en: "Loading recent sessions…")
        case .searchEmptyTitle:
            return BilingualText(zhHans: "未找到匹配的对话", en: "No matching sessions")
        case .searchEmptyHint:
            return BilingualText(zhHans: "换个项目名或关键词试试，按 Esc 清空筛选", en: "Try another project or keyword, or press Esc to clear")
        case .emptyFeedTitle:
            return BilingualText(zhHans: "暂无可继续的对话", en: "No sessions ready to continue")
        case .emptyFeedHint:
            return BilingualText(zhHans: "在 Codex 或 Antigravity 开始对话后会显示在这里", en: "Sessions started in Codex or Antigravity will appear here")
        case .footerContinue:
            return BilingualText(zhHans: "继续", en: "Continue")
        case .footerContinueAX:
            return BilingualText(zhHans: "执行当前选中操作", en: "Continue selected session")
        case .footerRefresh:
            return BilingualText(zhHans: "刷新", en: "Refresh")
        case .footerRefreshAX:
            return BilingualText(zhHans: "刷新对话列表", en: "Refresh sessions")
        case .footerSettings:
            return BilingualText(zhHans: "设置", en: "Settings")
        case .footerSettingsAX:
            return BilingualText(zhHans: "打开设置", en: "Open Settings")
        case .footerQuit:
            return BilingualText(zhHans: "退出", en: "Quit")
        case .footerQuitAX:
            return BilingualText(zhHans: "退出应用", en: "Quit App")
        case .daemonBothOnline:
            return BilingualText(zhHans: "双端在线", en: "Both Online")
        case .daemonAGYOnly:
            return BilingualText(zhHans: "仅 AGY 在线", en: "AGY Only")
        case .daemonCodexOnly:
            return BilingualText(zhHans: "仅 Codex 在线", en: "Codex Only")
        case .daemonBothOffline:
            return BilingualText(zhHans: "双端未运行", en: "Both Offline")

        case .settingsWindowTitle:
            return BilingualText(zhHans: "CodexAgySync 设置", en: "CodexAgySync Settings")
        case .settingsTabGeneral:
            return BilingualText(zhHans: "常规与同步", en: "General & Sync")
        case .settingsTabUpdates:
            return BilingualText(zhHans: "版本与更新", en: "Version & Updates")
        case .settingsLanguageTitle:
            return BilingualText(zhHans: "界面语言", en: "Language")
        case .settingsLanguageSubtitle:
            return BilingualText(zhHans: "切换后立即更新面板与设置界面，无需重启", en: "Updates popover and settings immediately without restart")
        case .settingsLaunchAtLoginTitle:
            return BilingualText(zhHans: "登录时自动启动", en: "Launch at Login")
        case .settingsLaunchAtLoginSubtitle:
            return BilingualText(zhHans: "进入 macOS 桌面后常驻菜单栏（零后台轮询，按需唤醒）", en: "Stays in the menu bar on login (zero background polling)")
        case .settingsHotKeyTitle:
            return BilingualText(zhHans: "全局快捷键", en: "Global Shortcut")
        case .settingsHotKeySubtitle:
            return BilingualText(zhHans: "随时从任意桌面空间唤起会话同步", en: "Open session sync from any Space")
        case .settingsHotKeyEnableAX:
            return BilingualText(zhHans: "启用全局快捷键", en: "Enable Global Shortcut")
        case .settingsHotKeyRecordingPrompt:
            return BilingualText(zhHans: "按下新组合键… (Esc 取消)", en: "Press shortcut… (Esc to cancel)")
        case .settingsHotKeyRecordingAX:
            return BilingualText(zhHans: "正在录制快捷键，按 Esc 取消", en: "Recording shortcut, press Esc to cancel")
        case .settingsHotKeyReset:
            return BilingualText(zhHans: "恢复默认", en: "Reset Default")
        case .settingsHotKeyResetAX:
            return BilingualText(zhHans: "恢复默认快捷键", en: "Reset to default shortcut")
        case .settingsHotKeyBehaviorLabel:
            return BilingualText(zhHans: "快捷键触发行为", en: "Shortcut Behavior")
        case .hotKeyTogglePanelLabel:
            return BilingualText(zhHans: "仅展开面板", en: "Open Panel Only")
        case .hotKeyTogglePanelHelper:
            return BilingualText(
                zhHans: "按下快捷键仅展开或收起面板，选择对话后再按回车继续。",
                en: "Toggles the panel without auto-continuing so you can pick a session first."
            )
        case .hotKeyInstantContinueLabel:
            return BilingualText(zhHans: "展开并自动同步首个对话", en: "Open & Sync Top Session")
        case .hotKeyInstantContinueHelper:
            return BilingualText(
                zhHans: "按下快捷键后立即展开面板，并自动将首条可操作对话同步到目标端。",
                en: "Opens the panel and immediately syncs the top actionable session to the target app."
            )
        case .settingsAutoActivateTitle:
            return BilingualText(zhHans: "同步完成后自动切到目标端应用", en: "Switch to Target App After Sync")
        case .settingsAutoActivateSubtitle:
            return BilingualText(zhHans: "关闭后可在菜单栏面板连续批量同步多条对话，不切换前台窗口", en: "Turn off to sync multiple sessions without switching focus")
        case .settingsFeedLimitTitle:
            return BilingualText(zhHans: "面板最近会话条数", en: "Recent Sessions Shown")
        case .settingsFeedLimitSubtitle:
            return BilingualText(zhHans: "控制点击菜单栏时单次读取展示的候选会话数量", en: "Number of candidate sessions fetched when opening the panel")
        case .settingsAttentionDotTitle:
            return BilingualText(zhHans: "存在未完成回复或待确认对话时显示状态栏圆点", en: "Show Menu Bar Dot for Pending Sessions")
        case .settingsAttentionDotSubtitle:
            return BilingualText(zhHans: "在菜单栏模板图标右下角叠加高对比镂空圆点提示", en: "Adds a high-contrast dot badge to the menu bar icon")

        case .updatesAlignmentCardTitle:
            return BilingualText(zhHans: "双端版本对齐看板", en: "Version Alignment")
        case .updatesAppBoxTitle:
            return BilingualText(zhHans: "菜单栏应用 App", en: "Menu Bar App")
        case .updatesCLIBoxTitle:
            return BilingualText(zhHans: "同步引擎 CLI", en: "Sync CLI")
        case .updatesPendingRelaunchSuffix:
            return BilingualText(zhHans: "（待重启）", en: " (Restart Pending)")
        case .updatesCLIPathLabel:
            return BilingualText(zhHans: "CLI 路径", en: "CLI Path")
        case .updatesCLINotFoundPath:
            return BilingualText(zhHans: "未找到可执行文件", en: "Executable not found")
        case .updatesCLIEnvHint:
            return BilingualText(zhHans: "当前由环境变量 CODEX_AGY_SYNC_BIN 优先指定", en: "Overridden by CODEX_AGY_SYNC_BIN environment variable")
        case .updatesChooseCLI:
            return BilingualText(zhHans: "选择 CLI…", en: "Choose CLI…")
        case .updatesRestoreAutoDiscovery:
            return BilingualText(zhHans: "恢复自动发现", en: "Use Auto-Discovery")
        case .updatesOpenStateDir:
            return BilingualText(zhHans: "打开状态目录", en: "Open State Folder")
        case .updatesPolicyCardTitle:
            return BilingualText(zhHans: "更新策略与双端同步", en: "Update Policy & Sync")
        case .updatesPolicyLabel:
            return BilingualText(zhHans: "自动更新", en: "Automatic Updates")
        case .updatesPolicyOffLabel:
            return BilingualText(zhHans: "手动检查", en: "Manual")
        case .updatesPolicyOffHelper:
            return BilingualText(
                zhHans: "不自动联网检查，仅在你点击“检查更新”或“同步更新”时运行。",
                en: "No automatic checks; runs only when you click Check or Sync."
            )
        case .updatesPolicyCheckOnlyLabel:
            return BilingualText(zhHans: "仅自动检查", en: "Check Only")
        case .updatesPolicyCheckOnlyHelper:
            return BilingualText(
                zhHans: "打开面板或设置时按需检查（最多每 24 小时一次，无后台定时轮询）。",
                en: "Checks at most every 24h on launch, panel open, or settings open (no background timer)."
            )
        case .updatesPolicyAutoStageLabel:
            return BilingualText(zhHans: "自动下载准备", en: "Auto-Stage")
        case .updatesPolicyAutoStageHelper:
            return BilingualText(
                zhHans: "无本地源码仓时自动下载并校验签名到缓存目录，需你点击“安装并重启”后生效。",
                en: "Downloads and verifies SHA-256 and signature in staging when no local repo is bound; installs on click."
            )
        case .updatesNeverChecked:
            return BilingualText(zhHans: "尚未检查", en: "Never checked")
        case .updatesLocalRepoLabel:
            return BilingualText(zhHans: "本地源码仓", en: "Local Source Repo")
        case .updatesChooseLocalRepo:
            return BilingualText(zhHans: "选择本地源码仓…", en: "Choose Repo…")
        case .updatesChooseLocalRepoPanelTitle:
            return BilingualText(zhHans: "选择 codex-agy-sync 本地源码仓库目录", en: "Choose codex-agy-sync Local Source Repository")
        case .updatesClearLocalRepo:
            return BilingualText(zhHans: "清除", en: "Clear")
        case .updatesNoLocalRepoText:
            return BilingualText(zhHans: "未绑定（将通过 GitHub Release 校验升级）", en: "Not bound (updates verify via GitHub Releases)")
        case .updatesCheckNow:
            return BilingualText(zhHans: "检查更新", en: "Check for Updates")
        case .updatesSyncNow:
            return BilingualText(zhHans: "同步更新 App + CLI", en: "Sync App + CLI")
        case .updatesRelaunchNow:
            return BilingualText(zhHans: "立即重启应用", en: "Restart App")
        case .updatesNotProbed:
            return BilingualText(zhHans: "尚未探测", en: "Not checked yet")
        case .updatesCLIPendingProbe:
            return BilingualText(zhHans: "等待验证", en: "Pending check")
        case .updatesCLINotReady:
            return BilingualText(zhHans: "未就绪", en: "Not Ready")
        case .updatesAlignmentAligned:
            return BilingualText(zhHans: "双端已同频", en: "Aligned")
        case .updatesAlignmentMismatched:
            return BilingualText(zhHans: "版本未对齐", en: "Mismatch")
        case .updatesAlignmentUnavailable:
            return BilingualText(zhHans: "CLI 未就绪", en: "CLI Unavailable")
        case .updatesDefaultNotesSummary:
            return BilingualText(zhHans: "包含最新双端同步与菜单栏改进", en: "Includes the latest sync and menu bar improvements")

        case .updatesStepBuildingLocal:
            return BilingualText(zhHans: "正在从源码仓构建并签名 CodexAgySync.app…", en: "Building and signing CodexAgySync.app from local repo…")
        case .updatesStepUpgradingLocalCLI:
            return BilingualText(zhHans: "正在升级 Python CLI 环境…", en: "Upgrading Python CLI environment…")
        case .updatesStepDownloadingChecksums:
            return BilingualText(zhHans: "正在下载并校验 SHA256SUMS…", en: "Downloading and verifying SHA256SUMS…")
        case .updatesStepVerifyingApp:
            return BilingualText(zhHans: "正在解包并校验应用签名…", en: "Extracting and verifying app code signature…")
        case .updatesStepInstallingCLI:
            return BilingualText(zhHans: "正在升级 codex-agy-sync CLI…", en: "Upgrading codex-agy-sync CLI…")
        case .updatesNoticeRelaunchAction:
            return BilingualText(zhHans: "立即重启", en: "Restart")
        case .updatesNoticeInstallStagedAction:
            return BilingualText(zhHans: "安装并重启", en: "Install & Restart")
        case .updatesNoticeOpenSettingsAction:
            return BilingualText(zhHans: "查看更新", en: "View Update")

        case .updatesErrMissingChecksums:
            return BilingualText(
                zhHans: "远端 Release 缺少 SHA256SUMS 校验文件，已中止更新。",
                en: "Remote release is missing SHA256SUMS; update aborted."
            )
        case .updatesErrMissingAssets:
            return BilingualText(
                zhHans: "远端 Release 未包含可用的安装包资产（.whl 或 CodexAgySync-macos.zip）。",
                en: "Remote release has no installable assets (.whl or CodexAgySync-macos.zip)."
            )
        case .updatesErrPythonMissing:
            return BilingualText(
                zhHans: "未找到可用的 Python 3 解释器以更新 CLI。",
                en: "No Python 3 interpreter found to update the CLI."
            )
        case .updatesErrHandoffInProgress:
            return BilingualText(
                zhHans: "正在执行会话同步，请等待当前操作完成后再安装更新。",
                en: "A session sync is in progress; wait for it to finish before installing."
            )
        case .updatesErrInstallationInProgress:
            return BilingualText(
                zhHans: "正在安装更新，请等待更新完成后再继续。",
                en: "An update is being installed; wait for it to finish before continuing."
            )
        case .updatesErrBundlePathMissing:
            return BilingualText(
                zhHans: "未找到可重启的应用包路径。",
                en: "Unable to locate the running app bundle to restart."
            )
        case .updatesErrNoWritableBundleFallback:
            return BilingualText(
                zhHans: "当前未从 .app 应用包启动",
                en: "Not launched from an .app bundle"
            )

        case .menuSettings:
            return BilingualText(zhHans: "设置…", en: "Settings…")
        case .menuQuit:
            return BilingualText(zhHans: "退出 CodexAgySync", en: "Quit CodexAgySync")
        case .menuEdit:
            return BilingualText(zhHans: "编辑", en: "Edit")
        case .menuUndo:
            return BilingualText(zhHans: "撤销", en: "Undo")
        case .menuRedo:
            return BilingualText(zhHans: "重做", en: "Redo")
        case .menuCut:
            return BilingualText(zhHans: "剪切", en: "Cut")
        case .menuCopy:
            return BilingualText(zhHans: "拷贝", en: "Copy")
        case .menuPaste:
            return BilingualText(zhHans: "粘贴", en: "Paste")
        case .menuSelectAll:
            return BilingualText(zhHans: "全选", en: "Select All")
        }
    }

    public static func text(_ key: AppCopyKey, in language: ResolvedLanguage) -> String {
        entry(for: key).resolve(in: language)
    }

    public static func turnsLabel(_ count: Int, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "\(count) 轮"
        case .en:
            return count == 1 ? "1 turn" : "\(count) turns"
        }
    }

    public static func relativeAgeSuffix(elapsedSeconds: Double, in language: ResolvedLanguage) -> String {
        let seconds = max(0, Int(elapsedSeconds))
        if seconds < 60 {
            return " · \(text(.relativeJustNow, in: language))"
        }
        if seconds < 3600 {
            let mins = max(1, seconds / 60)
            return language == .zhHans ? " · \(mins) 分钟前" : " · \(mins)m ago"
        }
        if seconds < 86_400 {
            let hours = max(1, seconds / 3600)
            return language == .zhHans ? " · \(hours) 小时前" : " · \(hours)h ago"
        }
        let days = max(1, seconds / 86_400)
        return language == .zhHans ? " · \(days) 天前" : " · \(days)d ago"
    }

    public static func continueInTarget(_ targetDisplayName: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "用 \(targetDisplayName) 继续"
        case .en:
            return "Continue in \(targetDisplayName)"
        }
    }

    public static func handoffOpenedBanner(targetName: String, title: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "已在 \(targetName) 打开对话 · \(title)"
        case .en:
            return "Opened session in \(targetName) · \(title)"
        }
    }

    public static func handoffOpenedExistingBanner(targetName: String, title: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "已在 \(targetName) 打开现有对话 · \(title)"
        case .en:
            return "Opened existing session in \(targetName) · \(title)"
        }
    }

    public static func handoffFailedBanner(_ detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "同步未成功：\(detail)"
        case .en:
            return "Sync failed: \(detail)"
        }
    }

    public static func reconcileCompletedBanner(unresolvedCount: Int, in language: ResolvedLanguage) -> String {
        if unresolvedCount == 0 {
            return text(.reconcileSyncedBanner, in: language)
        }
        switch language {
        case .zhHans:
            return "状态已同步，仍有 \(unresolvedCount) 条对话等待响应"
        case .en:
            return "Status synced; \(unresolvedCount) session(s) awaiting confirmation"
        }
    }

    public static func reconcileFailedBanner(_ detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "状态同步未成功：\(detail)"
        case .en:
            return "Status sync failed: \(detail)"
        }
    }

    public static func dismissBannerAccessibility(_ bannerText: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "关闭提示：\(bannerText)"
        case .en:
            return "Dismiss notice: \(bannerText)"
        }
    }

    public static func cliTimedOut(seconds: TimeInterval, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "命令行工具响应超时（超过 \(Int(seconds)) 秒），请稍后重试。"
        case .en:
            return "CLI timed out after \(Int(seconds))s. Please try again."
        }
    }

    public static func cliStructuredError(message: String, cause: String?, in language: ResolvedLanguage) -> String {
        guard let cause, !cause.isEmpty else {
            return message
        }
        switch language {
        case .zhHans:
            return "\(message)（\(cause)）"
        case .en:
            return "\(message) (\(cause))"
        }
    }

    public static func cliNonZeroExit(code: Int32, stderr: String, in language: ResolvedLanguage) -> String {
        let trimmed = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        switch language {
        case .zhHans:
            return trimmed.isEmpty ? "命令执行返回错误码 \(code)" : "命令执行返回错误码 \(code)：\(trimmed)"
        case .en:
            return trimmed.isEmpty ? "CLI exited with status \(code)" : "CLI exited with status \(code): \(trimmed)"
        }
    }

    public static func cliDecodingFailed(_ detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "无法解析工具输出：\(detail)"
        case .en:
            return "Unable to decode CLI output: \(detail)"
        }
    }

    public static func cliLaunchFailed(_ detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "无法启动命令行工具：\(detail)"
        case .en:
            return "Unable to launch CLI: \(detail)"
        }
    }

    public static func statusToolTip(shortcutSuffix: String, in language: ResolvedLanguage) -> String {
        "\(text(.statusItemLabel, in: language))\(shortcutSuffix)"
    }

    public static func settingsTabAccessibility(_ title: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "设置分栏：\(title)"
        case .en:
            return "Settings tab: \(title)"
        }
    }

    public static func languageOptionAccessibility(_ option: AppLanguage, in language: ResolvedLanguage) -> String {
        let label = option.displayName(in: language)
        switch language {
        case .zhHans:
            return "界面语言：\(label)"
        case .en:
            return "Language: \(label)"
        }
    }

    public static func hotKeyRecordButtonAccessibility(shortcut: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "录制快捷键：\(shortcut)"
        case .en:
            return "Record shortcut: \(shortcut)"
        }
    }

    public static func hotKeyConflictWarning(shortcut: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "快捷键 \(shortcut) 已被系统或其他应用占用，请录制其他组合键。"
        case .en:
            return "Shortcut \(shortcut) is already in use by macOS or another app. Record a different combination."
        }
    }

    public static func hotKeyBehaviorAccessibility(_ label: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "快捷键触发行为：\(label)"
        case .en:
            return "Shortcut behavior: \(label)"
        }
    }

    public static func feedLimitOptionLabel(_ count: Int, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "\(count) 条"
        case .en:
            return "\(count)"
        }
    }

    public static func feedLimitOptionAccessibility(_ count: Int, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "面板最近会话条数：\(count) 条"
        case .en:
            return "Recent sessions shown: \(count)"
        }
    }

    public static func autoUpdatePolicyAccessibility(_ label: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "自动更新：\(label)"
        case .en:
            return "Automatic updates: \(label)"
        }
    }

    public static func lastCheckedReadout(_ formattedTime: String?, in language: ResolvedLanguage) -> String {
        let value = formattedTime ?? text(.updatesNeverChecked, in: language)
        switch language {
        case .zhHans:
            return "上次检查：\(value)"
        case .en:
            return "Last checked: \(value)"
        }
    }

    public static func backgroundCheckErrorReadout(_ detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "最近自动检查未成功：\(detail)"
        case .en:
            return "Last automatic check failed: \(detail)"
        }
    }

    public static func installStagedButtonTitle(tagName: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "安装 \(tagName) 并重启"
        case .en:
            return "Install \(tagName) & Restart"
        }
    }

    public static func statusReadoutAccessibility(_ status: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "更新状态：\(status)"
        case .en:
            return "Update status: \(status)"
        }
    }

    public static func alignmentBadgeAccessibility(_ title: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "版本状态：\(title)"
        case .en:
            return "Version status: \(title)"
        }
    }

    public static func versionBoxAccessibility(title: String, value: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "\(title)：\(value)"
        case .en:
            return "\(title): \(value)"
        }
    }

    public static func stepDownloadingWheel(_ name: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "正在下载并校验 CLI 安装包（\(name)）…"
        case .en:
            return "Downloading and verifying CLI wheel (\(name))…"
        }
    }

    public static func stepDownloadingApp(_ name: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "正在下载并校验应用包（\(name)）…"
        case .en:
            return "Downloading and verifying app archive (\(name))…"
        }
    }

    public static func stepStagingRelease(_ tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "正在后台准备更新 \(tag)…"
        case .en:
            return "Staging update \(tag)…"
        }
    }

    public static func stepApplyingStaged(_ tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "正在安装已校验的更新 \(tag)…"
        case .en:
            return "Installing verified update \(tag)…"
        }
    }

    public static func noticeRelaunchTitle(version: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "v\(version) 已更新，等待重启"
        case .en:
            return "v\(version) installed; restart to apply"
        }
    }

    public static func noticeStagedTitle(tagName: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "\(tagName) 已准备就绪"
        case .en:
            return "\(tagName) ready to install"
        }
    }

    public static func noticeReleaseAvailableTitle(tagName: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "发现新版本 \(tagName)"
        case .en:
            return "Update \(tagName) available"
        }
    }

    public static func noticeLocalSyncTitle(version: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "双端版本待同步（v\(version)）"
        case .en:
            return "CLI sync needed (v\(version))"
        }
    }

    public static func updateErrForbiddenTempPath(_ path: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "拒绝使用 /tmp 临时目录：\(path)"
        case .en:
            return "Refusing /tmp temporary path: \(path)"
        }
    }

    public static func updateErrInvalidAssetName(_ name: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "拒绝不安全的更新资产文件名：\(name)"
        case .en:
            return "Unsafe update asset filename: \(name)"
        }
    }

    public static func updateErrInvalidReleaseURL(_ url: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "无效的更新检查地址：\(url)"
        case .en:
            return "Invalid update check URL: \(url)"
        }
    }

    public static func updateErrHttpStatus(_ code: Int, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "请求远端发布信息失败（HTTP \(code)）。"
        case .en:
            return "Remote release request failed (HTTP \(code))."
        }
    }

    public static func updateErrMissingChecksum(assetName: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "SHA256SUMS 中缺少资产校验项：\(assetName)"
        case .en:
            return "SHA256SUMS is missing entry for \(assetName)"
        }
    }

    public static func updateErrChecksumMismatch(
        assetName: String,
        expected: String,
        actual: String,
        in language: ResolvedLanguage
    ) -> String {
        switch language {
        case .zhHans:
            return "\(assetName) SHA-256 校验不匹配（预期 \(expected.prefix(12))…，实际 \(actual.prefix(12))…）。"
        case .en:
            return "\(assetName) SHA-256 mismatch (expected \(expected.prefix(12))…, got \(actual.prefix(12))…)."
        }
    }

    public static func updateErrInvalidLocalRepo(_ path: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "无效的本地源码仓目录（需包含 pyproject.toml 与 scripts/build_menubar_app.sh）：\(path)"
        case .en:
            return "Invalid local source repository (requires pyproject.toml and scripts/build_menubar_app.sh): \(path)"
        }
    }

    public static func updateErrTargetBundleNotWritable(_ detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "无法写入应用安装目录（\(detail)），请将应用移至可写目录后重试。"
        case .en:
            return "Target app directory is not writable (\(detail)). Move the app to a writable folder and retry."
        }
    }

    public static func updateErrCommandFailed(step: String, detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return detail.isEmpty ? "\(step) 执行失败。" : "\(step) 失败：\(detail)"
        case .en:
            return detail.isEmpty ? "\(step) failed." : "\(step) failed: \(detail)"
        }
    }

    public static func updateErrCodesignFailed(_ detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "新构建的应用签名校验未通过：\(detail)"
        case .en:
            return "Code signature verification failed: \(detail)"
        }
    }

    public static func updatesMissingAppBundle(_ name: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "未找到 \(name)"
        case .en:
            return "missing \(name)"
        }
    }

    public static func updateFailureCliUnavailableMissingAsset(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "CLI 尚未就绪，且远端 Release（\(tag)）未包含 CLI 安装包。"
        case .en:
            return "CLI is not ready, and release \(tag) has no CLI wheel asset."
        }
    }

    public static func updateFailureMismatchMissingAsset(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "双端版本未对齐，且远端 Release（\(tag)）未包含对齐所需安装包。"
        case .en:
            return "App and CLI versions differ, and release \(tag) has no installable assets."
        }
    }

    public static func updateFailureRelaunchFailed(_ detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "重启应用失败：\(detail)"
        case .en:
            return "Failed to restart app: \(detail)"
        }
    }

    public static func updateFailureStagedArtifactInvalid(_ detail: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "已准备的更新包校验失败（\(detail)），请重新检查并同步更新。"
        case .en:
            return "Staged update verification failed (\(detail)). Please check and sync again."
        }
    }

    public static func updatesStatusAlignedWithRemote(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "当前 App 与远端最新 Release（\(tag)）一致。"
        case .en:
            return "App and CLI are up to date with \(tag)."
        }
    }

    public static func updatesStatusLocalRepoSynced(version: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "已从本地源码仓同步更新至 v\(version)，点击“立即重启应用”加载新壳层。"
        case .en:
            return "Synced from local repo to v\(version). Click \"Restart App\" to apply."
        }
    }

    public static func updatesStatusReleaseSyncedWithApp(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "已同步升级至 \(tag)，点击“立即重启应用”加载新应用壳层。"
        case .en:
            return "Updated to \(tag). Click \"Restart App\" to apply."
        }
    }

    public static func updatesStatusReleaseSyncedCLIOnly(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "已校验并升级 CLI 至 \(tag)（该 Release 未附带 .app 压缩包）。"
        case .en:
            return "Verified and updated CLI to \(tag) (release did not include a macOS .app archive)."
        }
    }

    public static func updatesStatusNewRelease(tag: String, summary: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "发现新版本 \(tag)：\(summary)"
        case .en:
            return "New version \(tag): \(summary)"
        }
    }

    public static func updatesSummaryInstallCLI(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "安装 CLI 并同步对齐至 \(tag)"
        case .en:
            return "Install CLI and align to \(tag)"
        }
    }

    public static func updatesStatusCLIMissing(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "CLI 尚未就绪，可点击同步更新安装 \(tag)。"
        case .en:
            return "CLI is not ready. Click Sync App + CLI to install \(tag)."
        }
    }

    public static func updatesSummaryAlignVersions(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "重新同步对齐 App 与 CLI 至 \(tag)"
        case .en:
            return "Resync App and CLI to \(tag)"
        }
    }

    public static func updatesStatusVersionMismatch(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "双端版本未对齐，可点击同步更新对齐至 \(tag)。"
        case .en:
            return "App and CLI versions differ. Click Sync App + CLI to align to \(tag)."
        }
    }

    public static func updatesStatusStagedReady(tag: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "已下载并校验 \(tag)，点击“安装并重启”即可生效。"
        case .en:
            return "Downloaded and verified \(tag). Click \"Install & Restart\" to apply."
        }
    }

    public static func updatesStatusLocalSyncCLIMissing(version: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "CLI 尚未就绪，可从本地源码仓（v\(version)）安装并同步。"
        case .en:
            return "CLI is not ready. Sync from local repo (v\(version)) to install."
        }
    }

    public static func updatesStatusLocalSyncMismatch(version: String, in language: ResolvedLanguage) -> String {
        switch language {
        case .zhHans:
            return "检测到双端版本不一致，可从本地源码仓（v\(version)）或远端 Release 对齐。"
        case .en:
            return "App and CLI versions differ. Sync from local repo (v\(version)) or a release to align."
        }
    }

    public static func updatesStatusLocalSyncRemoteUnavailable(
        detail: String,
        repoVersion: String,
        in language: ResolvedLanguage
    ) -> String {
        switch language {
        case .zhHans:
            return "远端检查不可用（\(detail)）；可直接从本地源码仓 v\(repoVersion) 同步构建。"
        case .en:
            return "Remote check unavailable (\(detail)); sync directly from local repo v\(repoVersion)."
        }
    }
}

@MainActor
public enum AppMenuBuilder {
    public static func buildMainMenu(
        language: ResolvedLanguage,
        target: AnyObject?,
        settingsAction: Selector
    ) -> NSMenu {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu(title: "CodexAgySync")

        let settingsItem = NSMenuItem(
            title: AppCopy.text(.menuSettings, in: language),
            action: settingsAction,
            keyEquivalent: ","
        )
        settingsItem.target = target
        appMenu.addItem(settingsItem)
        appMenu.addItem(.separator())
        appMenu.addItem(
            NSMenuItem(
                title: AppCopy.text(.menuQuit, in: language),
                action: #selector(NSApplication.terminate(_:)),
                keyEquivalent: "q"
            )
        )
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: AppCopy.text(.menuEdit, in: language))
        editMenu.addItem(NSMenuItem(title: AppCopy.text(.menuUndo, in: language), action: Selector(("undo:")), keyEquivalent: "z"))
        editMenu.addItem(NSMenuItem(title: AppCopy.text(.menuRedo, in: language), action: Selector(("redo:")), keyEquivalent: "Z"))
        editMenu.addItem(.separator())
        editMenu.addItem(NSMenuItem(title: AppCopy.text(.menuCut, in: language), action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: AppCopy.text(.menuCopy, in: language), action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: AppCopy.text(.menuPaste, in: language), action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(title: AppCopy.text(.menuSelectAll, in: language), action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editMenuItem.submenu = editMenu

        return mainMenu
    }
}
