import AppKit
import Observation
import SwiftUI

public enum SettingsTab: String, CaseIterable, Identifiable, Sendable {
    case general
    case desktopTools
    case automation
    case updates

    public var id: String { rawValue }

    public func title(in language: ResolvedLanguage) -> String {
        switch self {
        case .general:
            return AppCopy.text(.settingsTabGeneral, in: language)
        case .desktopTools:
            return AppCopy.text(.settingsTabDesktopTools, in: language)
        case .automation:
            return AppCopy.text(.settingsTabAutomation, in: language)
        case .updates:
            return AppCopy.text(.settingsTabUpdates, in: language)
        }
    }

    public var iconName: String {
        switch self {
        case .general:
            return "slider.horizontal.3"
        case .desktopTools:
            return "macwindow.on.rectangle"
        case .automation:
            return "terminal"
        case .updates:
            return "arrow.triangle.2.circlepath.circle"
        }
    }

    public func accessibilityLabel(in language: ResolvedLanguage) -> String {
        AppCopy.settingsTabAccessibility(title(in: language), in: language)
    }
}

@MainActor
@Observable
public final class SettingsNavigationModel {
    public var selectedTab: SettingsTab

    public init(selectedTab: SettingsTab = .general) {
        self.selectedTab = selectedTab
    }
}

public struct SettingsView: View {
    private let preferencesStore: PreferencesStore
    private let updateViewModel: UpdateViewModel
    private let automationViewModel: AutomationViewModel
    private let navigation: SettingsNavigationModel
    private let inputHub: DesktopInputHub?
    private let windowResizeDiagnostic: String?
    private let windowResizeStatusStore: WindowResizeStatusStore?
    private let onDidChangeCLI: (() -> Void)?

    @State private var hostWindowBox = WeakWindowBox()

    public init(
        preferencesStore: PreferencesStore,
        updateViewModel: UpdateViewModel,
        automationViewModel: AutomationViewModel? = nil,
        initialTab: SettingsTab = .general,
        navigation: SettingsNavigationModel? = nil,
        inputHub: DesktopInputHub? = nil,
        windowResizeDiagnostic: String? = nil,
        windowResizeStatusStore: WindowResizeStatusStore? = nil,
        onDidChangeCLI: (() -> Void)? = nil
    ) {
        self.preferencesStore = preferencesStore
        self.updateViewModel = updateViewModel
        self.automationViewModel = automationViewModel ?? AutomationViewModel(
            preferencesStore: preferencesStore,
            appVersion: updateViewModel.appVersion
        )
        self.navigation = navigation ?? SettingsNavigationModel(selectedTab: initialTab)
        self.inputHub = inputHub
        self.windowResizeDiagnostic = windowResizeDiagnostic
        self.windowResizeStatusStore = windowResizeStatusStore
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
                    GeneralSettingsPane(
                        preferencesStore: preferencesStore,
                        hostWindowBox: hostWindowBox
                    )
                case .desktopTools:
                    DesktopToolsSettingsPane(
                        preferencesStore: preferencesStore,
                        inputHub: inputHub,
                        windowResizeDiagnostic: windowResizeDiagnostic,
                        windowResizeStatusStore: windowResizeStatusStore
                    )
                case .automation:
                    AutomationSettingsPane(
                        preferencesStore: preferencesStore,
                        viewModel: automationViewModel
                    )
                case .updates:
                    UpdatesSettingsPane(
                        preferencesStore: preferencesStore,
                        updateViewModel: updateViewModel,
                        onDidChangeCLI: onDidChangeCLI
                    )
                }
            }
        }
        .padding(18)
        .frame(width: 600)
        .background(Color(nsColor: .windowBackgroundColor))
        .background(
            SettingsWindowAccessor { window in
                hostWindowBox.window = window
            }
        )
    }

    private var tabBar: some View {
        HStack(spacing: 6) {
            ForEach(SettingsTab.allCases) { tab in
                let isSelected = navigation.selectedTab == tab
                Button {
                    selectTab(tab)
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: tab.iconName)
                            .font(.system(size: 12, weight: .semibold))
                        Text(tab.title(in: lang))
                            .font(.system(size: 12, weight: .semibold))
                            .lineLimit(1)
                            .minimumScaleFactor(0.88)
                    }
                    .foregroundStyle(isSelected ? Color.primary : Color.secondary)
                    .frame(maxWidth: .infinity)
                    .padding(.horizontal, 6)
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

    private func selectTab(_ tab: SettingsTab) {
        preferencesStore.setShortcutRecording(false)
        navigation.selectedTab = tab
        Task { @MainActor in
            NSApp.keyWindow?.makeFirstResponder(nil)
        }
    }
}
