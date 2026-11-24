import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct DesktopToolsSettingsPane: View {
    let preferencesStore: PreferencesStore
    let inputHub: DesktopInputHub?
    let windowResizeDiagnostic: String?
    let windowResizeStatusStore: WindowResizeStatusStore?

    @State private var runningApps: [InstalledAppDescriptor] = []

    private var lang: ResolvedLanguage {
        preferencesStore.effectiveLanguage
    }

    private var windowResizePrefs: WindowResizePreferences {
        preferencesStore.preferences.windowResize
    }

    private var permissionState: DesktopPermissionState {
        inputHub?.permissionState ?? DesktopPermissionState(
            accessibilityGranted: false,
            inputMonitoringGranted: false
        )
    }

    private var resolvedDiagnostic: String? {
        if let explicit = windowResizeDiagnostic, !explicit.isEmpty {
            return explicit
        }
        return windowResizeStatusStore?.diagnosticMessage(in: lang)
    }

    private var activeConflicts: [ShortcutConflict] {
        guard let inputHub, windowResizePrefs.isEnabled else {
            return []
        }
        return inputHub.detectConflicts(
            extraBindings: WindowResizeShortcutCatalog.bindings(),
            checkNativeTilingOverlap: true
        ).filter { conflict in
            conflict.owner == .windowResize && conflict.kind != .macOSNativeTilingOverlap
        }
    }

    private var hasNativeTilingOverlap: Bool {
        guard let inputHub, windowResizePrefs.isEnabled else {
            return false
        }
        return inputHub.detectConflicts(
            extraBindings: WindowResizeShortcutCatalog.bindings(),
            checkNativeTilingOverlap: true
        ).contains { conflict in
            conflict.owner == .windowResize && conflict.kind == .macOSNativeTilingOverlap
        }
    }

    private var needsPermissionWarning: Bool {
        windowResizePrefs.isEnabled && !permissionState.hasEventTapAccess
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                permissionsCard
                windowResizeCard
            }
            .padding(.bottom, 4)
        }
        .frame(minHeight: 380, maxHeight: 490)
        .onAppear {
            runningApps = RunningAppCatalog.regularRunningApps()
            _ = inputHub?.refreshPermissions()
        }
    }

    private var permissionsCard: some View {
        SettingsCard {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(AppCopy.text(.desktopPermissionsTitle, in: lang))
                        .font(.system(size: 13, weight: .semibold))
                    Text(AppCopy.text(.desktopPermissionsSubtitle, in: lang))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                SettingsActionButton(
                    AppCopy.text(.desktopPermissionsRecheck, in: lang),
                    systemImage: "arrow.clockwise"
                ) {
                    _ = inputHub?.refreshPermissions()
                }
            }

            if needsPermissionWarning {
                Text(AppCopy.text(.desktopPermissionsPausedWarning, in: lang))
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 7)
                            .fill(Color.orange.opacity(0.1))
                    )
            }

            permissionRow(
                title: AppCopy.text(.desktopPermissionAccessibilityTitle, in: lang),
                granted: permissionState.accessibilityGranted,
                onRequest: {
                    _ = inputHub?.requestAccessibilityPrompt()
                },
                onOpenSettings: {
                    _ = inputHub?.openAccessibilitySettings()
                }
            )

            Divider()

            permissionRow(
                title: AppCopy.text(.desktopPermissionInputMonitoringTitle, in: lang),
                granted: permissionState.inputMonitoringGranted,
                onRequest: {
                    _ = inputHub?.requestInputMonitoringPrompt()
                },
                onOpenSettings: {
                    _ = inputHub?.openInputMonitoringSettings()
                }
            )
        }
    }

    private func permissionRow(
        title: String,
        granted: Bool,
        onRequest: @escaping () -> Void,
        onOpenSettings: @escaping () -> Void
    ) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(title)
                        .font(.system(size: 12, weight: .semibold))
                    permissionBadge(granted: granted)
                }
            }
            Spacer()
            if !granted {
                SettingsSmallInlineButton(AppCopy.text(.desktopPermissionsRequest, in: lang)) {
                    onRequest()
                }
            }
            SettingsSmallInlineButton(AppCopy.text(.desktopPermissionsOpenSettings, in: lang)) {
                onOpenSettings()
            }
        }
    }

    private func permissionBadge(granted: Bool) -> some View {
        let color: Color = granted ? .green : (needsPermissionWarning ? .orange : .secondary)
        let label = AppCopy.text(
            granted ? .desktopPermissionGrantedBadge : .desktopPermissionMissingBadge,
            in: lang
        )
        return HStack(spacing: 4) {
            Circle()
                .fill(color)
                .frame(width: 6, height: 6)
            Text(label)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(color)
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 2)
        .background(
            Capsule()
                .fill(color.opacity(0.12))
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel(label)
    }

    private var windowResizeCard: some View {
        SettingsCard {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(AppCopy.text(.windowResizeCardTitle, in: lang))
                        .font(.system(size: 13, weight: .semibold))
                    Text(AppCopy.text(.windowResizeCardSubtitle, in: lang))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Toggle(
                    "",
                    isOn: Binding(
                        get: { windowResizePrefs.isEnabled },
                        set: { enabled in
                            preferencesStore.setWindowResizeEnabled(enabled)
                            _ = inputHub?.refreshPermissions()
                        }
                    )
                )
                .labelsHidden()
                .toggleStyle(.switch)
                .focusEffectDisabled()
                .accessibilityLabel(AppCopy.text(.windowResizeEnableAX, in: lang))
            }

            Divider()

            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(AppCopy.text(.windowResizeNearestCornerTitle, in: lang))
                        .font(.system(size: 12, weight: .semibold))
                    Text(AppCopy.text(.windowResizeNearestCornerSubtitle, in: lang))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Toggle(
                    "",
                    isOn: Binding(
                        get: { windowResizePrefs.nearestCornerResize },
                        set: { preferencesStore.setWindowResizeNearestCorner($0) }
                    )
                )
                .labelsHidden()
                .toggleStyle(.switch)
                .focusEffectDisabled()
                .disabled(!windowResizePrefs.isEnabled)
                .accessibilityLabel(AppCopy.text(.windowResizeNearestCornerTitle, in: lang))
            }

            Divider()

            VStack(alignment: .leading, spacing: 6) {
                Text(AppCopy.text(.windowResizeGesturesHeader, in: lang))
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)

                shortcutReferenceRow(
                    combo: "fn + ctrl + drag",
                    description: AppCopy.text(.windowResizeGestureMoveDesc, in: lang)
                )
                shortcutReferenceRow(
                    combo: "fn + ctrl + option + drag",
                    description: AppCopy.text(.windowResizeGestureResizeDesc, in: lang)
                )
                shortcutReferenceRow(
                    combo: "fn + ctrl + ←/→/↑/↓/F/C/R",
                    description: AppCopy.text(.windowResizeGestureTilingDesc, in: lang)
                )
                shortcutReferenceRow(
                    combo: "fn + ctrl + option + cmd + drag",
                    description: AppCopy.text(.windowResizeGestureSplitDesc, in: lang)
                )
            }

            if !activeConflicts.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text(AppCopy.text(.windowResizeConflictsTitle, in: lang))
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.orange)
                    ForEach(Array(activeConflicts.enumerated()), id: \.offset) { _, conflict in
                        Text(conflictDescription(conflict))
                            .font(.system(size: 10.5))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 7)
                        .fill(Color.orange.opacity(0.1))
                )
            }

            if hasNativeTilingOverlap {
                Text(AppCopy.text(.windowResizeNativeTilingOverlapNote, in: lang))
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let diagnostic = resolvedDiagnostic {
                HStack(alignment: .top, spacing: 8) {
                    Text(diagnostic)
                        .font(.system(size: 11))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    if windowResizeStatusStore != nil {
                        SettingsSmallInlineButton(AppCopy.text(.windowResizeClearDiagnostic, in: lang)) {
                            windowResizeStatusStore?.clearDiagnostic()
                        }
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 7)
                        .fill(Color.orange.opacity(0.09))
                )
            }

            Text(AppCopy.text(.windowResizeConflictLimitationNote, in: lang))
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            Divider()

            excludedAppsSection
        }
    }

    private func shortcutReferenceRow(combo: String, description: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(combo)
                .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(
                    RoundedRectangle(cornerRadius: 5)
                        .fill(Color.primary.opacity(0.07))
                )
            Text(description)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }

    private var excludedAppsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(AppCopy.text(.excludedAppsTitle, in: lang))
                        .font(.system(size: 12, weight: .semibold))
                    Text(AppCopy.text(.excludedAppsSubtitle, in: lang))
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
            }

            HStack(spacing: 8) {
                Menu {
                    let candidates = availableRunningApps
                    if candidates.isEmpty {
                        Text(AppCopy.text(.excludedAppsNoRunningCandidates, in: lang))
                    } else {
                        ForEach(candidates) { app in
                            Button("\(app.displayName) (\(app.bundleId))") {
                                preferencesStore.addWindowResizeExcludedBundleId(app.bundleId)
                            }
                        }
                    }
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "plus.app")
                            .font(.system(size: 11, weight: .medium))
                        Text(AppCopy.text(.excludedAppsAddRunning, in: lang))
                            .font(.system(size: 11, weight: .semibold))
                    }
                    .foregroundStyle(.primary)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(
                        RoundedRectangle(cornerRadius: 7)
                            .fill(Color.primary.opacity(0.08))
                    )
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .focusEffectDisabled()
                .accessibilityLabel(AppCopy.text(.excludedAppsAddRunning, in: lang))
                .onTapGesture {
                    runningApps = RunningAppCatalog.regularRunningApps()
                }

                SettingsActionButton(
                    AppCopy.text(.excludedAppsChooseApp, in: lang),
                    systemImage: "folder"
                ) {
                    chooseExcludedApplication()
                }

                Spacer()
            }

            if windowResizePrefs.excludedBundleIds.isEmpty {
                Text(AppCopy.text(.excludedAppsEmpty, in: lang))
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(windowResizePrefs.excludedBundleIds, id: \.self) { bundleId in
                        excludedAppRow(bundleId: bundleId)
                    }
                }
            }
        }
    }

    private func excludedAppRow(bundleId: String) -> some View {
        let displayName = resolvedDisplayName(for: bundleId)
        return HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(displayName)
                    .font(.system(size: 11.5, weight: .medium))
                    .lineLimit(1)
                if displayName.lowercased() != bundleId.lowercased() {
                    Text(bundleId)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            SettingsSmallInlineButton(AppCopy.text(.excludedAppsRemove, in: lang)) {
                preferencesStore.removeWindowResizeExcludedBundleId(bundleId)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(Color.primary.opacity(0.04))
        )
    }

    private var availableRunningApps: [InstalledAppDescriptor] {
        let excluded = windowResizePrefs.excludedBundleIds
        return runningApps.filter { app in
            !ExcludedAppMatcher.isExcluded(bundleId: app.bundleId, in: excluded)
        }
    }

    private func resolvedDisplayName(for bundleId: String) -> String {
        if let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId),
           let descriptor = RunningAppCatalog.descriptor(forAppURL: appURL)
        {
            return descriptor.displayName
        }
        if let running = runningApps.first(where: { $0.bundleId.caseInsensitiveCompare(bundleId) == .orderedSame }) {
            return running.displayName
        }
        return bundleId
    }

    private func chooseExcludedApplication() {
        let panel = NSOpenPanel()
        panel.title = AppCopy.text(.excludedAppsChoosePanelTitle, in: lang)
        panel.allowedContentTypes = [.application]
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        NSApp.activate()
        if panel.runModal() == .OK,
           let url = panel.url,
           let descriptor = RunningAppCatalog.descriptor(forAppURL: url)
        {
            preferencesStore.addWindowResizeExcludedBundleId(descriptor.bundleId)
        }
    }

    private func conflictDescription(_ conflict: ShortcutConflict) -> String {
        switch conflict.kind {
        case .internalDuplicate:
            return AppCopy.windowResizeConflictInternal(conflict.detail, in: lang)
        case .systemSymbolicHotKey:
            return AppCopy.windowResizeConflictSymbolic(conflict.detail, in: lang)
        case .carbonRegistrationFailed:
            return conflict.detail
        case .macOSNativeTilingOverlap:
            return AppCopy.text(.windowResizeNativeTilingOverlapNote, in: lang)
        }
    }
}
