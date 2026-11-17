import SwiftUI

struct GeneralSettingsPane: View {
    let preferencesStore: PreferencesStore
    let hostWindowBox: WeakWindowBox

    private var lang: ResolvedLanguage {
        preferencesStore.effectiveLanguage
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SettingsCard {
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

            SettingsCard {
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

            ShortcutRecorderSection(
                preferencesStore: preferencesStore,
                hostWindowBox: hostWindowBox
            )

            SettingsCard {
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
                            get: { preferencesStore.preferences.activateTargetAppAfterSync },
                            set: { preferencesStore.setActivateTargetAppAfterSync($0) }
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
}
