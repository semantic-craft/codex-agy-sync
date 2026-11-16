import AppKit
import CodexAgyHandoffCore
import Combine

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: StatusBarController?
    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let environment = ProcessInfo.processInfo.environment
        let preferencesStore = PreferencesStore(environment: environment)
        rebuildMainMenu(language: preferencesStore.effectiveLanguage)
        preferencesStore.$effectiveLanguage
            .dropFirst()
            .sink { [weak self] language in
                self?.rebuildMainMenu(language: language)
            }
            .store(in: &cancellables)

        let cli = SubprocessCLIClient(environment: environment)
        let activator = WorkspaceAppActivator(environment: environment)
        let viewModel = HandoffViewModel(
            cli: cli,
            activator: activator,
            preferencesStore: preferencesStore
        )
        let statusController = StatusBarController(
            viewModel: viewModel,
            environment: environment
        )
        self.controller = statusController
        statusController.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        cancellables.removeAll()
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
