import AppKit
import CodexAgySyncCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: StatusBarController?
    private var languageObserverToken: ObservationToken?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let environment = ProcessInfo.processInfo.environment
        let preferencesStore = PreferencesStore(environment: environment)
        rebuildMainMenu(language: preferencesStore.effectiveLanguage)
        languageObserverToken = observeContinuously {
            preferencesStore.effectiveLanguage
        } onChange: { [weak self] language in
            self?.rebuildMainMenu(language: language)
        }

        let cli = SubprocessCLIClient(environment: environment)
        let activator = WorkspaceAppActivator(environment: environment)
        let store = SessionSyncStore(
            cli: cli,
            activator: activator,
            preferencesStore: preferencesStore
        )
        let statusController = StatusBarController(
            store: store,
            environment: environment
        )
        self.controller = statusController
        statusController.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        languageObserverToken?.cancel()
        languageObserverToken = nil
        controller?.stop()
    }

    @objc
    private func openSettingsFromMenu(_ sender: Any?) {
        controller?.openSettings(initialTab: .general)
    }

    private func rebuildMainMenu(language: ResolvedLanguage) {
        NSApp.mainMenu = AppMenuBuilder.buildMainMenu(
            language: language,
            target: self,
            settingsAction: #selector(openSettingsFromMenu(_:))
        )
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
