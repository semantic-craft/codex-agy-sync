import AppKit
import Carbon.HIToolbox
import SwiftUI

@MainActor
final class WeakWindowBox {
    weak var window: NSWindow?
}

struct SettingsWindowAccessor: NSViewRepresentable {
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

struct ShortcutRecorderSection: View {
    let preferencesStore: PreferencesStore
    let hostWindowBox: WeakWindowBox

    @State private var keyMonitor: Any?

    private var lang: ResolvedLanguage {
        preferencesStore.effectiveLanguage
    }

    var body: some View {
        SettingsCard {
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
        .onChange(of: preferencesStore.isRecordingShortcut) { _, active in
            if !active {
                removeShortcutKeyMonitor()
            }
        }
        .onDisappear {
            stopShortcutRecording()
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
}
