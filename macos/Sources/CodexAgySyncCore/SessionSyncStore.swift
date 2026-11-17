import Foundation
import Observation
import SwiftUI

public enum LocalizedFailure: Equatable, Sendable {
    case cli(CLIClientError)
    case update(UpdateServiceError)
    case message(String)

    public init(error: Error) {
        if let cliError = error as? CLIClientError {
            self = .cli(cliError)
        } else if let updateError = error as? UpdateServiceError {
            self = .update(updateError)
        } else if let localized = error as? LocalizedError, let message = localized.errorDescription {
            self = .message(message)
        } else {
            self = .message(error.localizedDescription)
        }
    }

    public func text(in language: ResolvedLanguage) -> String {
        switch self {
        case .cli(let cliError):
            return cliError.localizedMessage(in: language)
        case .update(let updateError):
            return updateError.localizedMessage(in: language)
        case .message(let raw):
            return raw
        }
    }
}

public enum SyncBannerEvent: Equatable, Sendable {
    case syncOpened(targetName: String, title: String)
    case syncOpenedExisting(targetName: String, title: String)
    case syncUnconfirmed
    case syncFailed(LocalizedFailure)
    case reconcileCompleted(unresolvedCount: Int)
    case reconcileFailed(LocalizedFailure)
    case feedFailed(LocalizedFailure)

    public func statusBanner(in language: ResolvedLanguage) -> StatusBanner {
        switch self {
        case .syncOpened(let targetName, let title):
            return StatusBanner(
                kind: .success,
                text: AppCopy.syncOpenedBanner(targetName: targetName, title: title, in: language)
            )
        case .syncOpenedExisting(let targetName, let title):
            return StatusBanner(
                kind: .info,
                text: AppCopy.syncOpenedExistingBanner(targetName: targetName, title: title, in: language)
            )
        case .syncUnconfirmed:
            return StatusBanner(
                kind: .error,
                text: AppCopy.text(.syncUnconfirmedBanner, in: language)
            )
        case .syncFailed(let failure):
            return StatusBanner(
                kind: .error,
                text: AppCopy.syncFailedBanner(failure.text(in: language), in: language)
            )
        case .reconcileCompleted(let unresolvedCount):
            return StatusBanner(
                kind: .success,
                text: AppCopy.reconcileCompletedBanner(unresolvedCount: unresolvedCount, in: language)
            )
        case .reconcileFailed(let failure):
            return StatusBanner(
                kind: .error,
                text: AppCopy.reconcileFailedBanner(failure.text(in: language), in: language)
            )
        case .feedFailed(let failure):
            return StatusBanner(kind: .error, text: failure.text(in: language))
        }
    }
}

public struct PaletteSection: Identifiable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let items: [MenubarSessionItem]

    public init(id: String, title: String, items: [MenubarSessionItem]) {
        self.id = id
        self.title = title
        self.items = items
    }
}

public enum StatusBanner: Equatable, Sendable {
    public enum Kind: Equatable, Sendable {
        case success
        case info
        case error
    }

    case success(String)
    case info(String)
    case error(String)

    public init(kind: Kind, text: String) {
        switch kind {
        case .success:
            self = .success(text)
        case .info:
            self = .info(text)
        case .error:
            self = .error(text)
        }
    }

    public var kind: Kind {
        switch self {
        case .success:
            return .success
        case .info:
            return .info
        case .error:
            return .error
        }
    }

    public var text: String {
        switch self {
        case .success(let message), .info(let message), .error(let message):
            return message
        }
    }
}

@MainActor
@Observable
public final class SessionSyncStore {
    public private(set) var snapshot: MenubarFeedSnapshot?
    public private(set) var isLoading: Bool = false
    public private(set) var busyItemId: String?
    public private(set) var bannerEvent: SyncBannerEvent?
    public private(set) var cliMissing: Bool = false
    public var searchQuery: String = "" {
        didSet {
            if searchQuery != oldValue {
                reconcileSelection()
            }
        }
    }
    public private(set) var selectedItemId: String?
    public private(set) var feedReferenceEpoch: Double

    public let preferencesStore: PreferencesStore
    @ObservationIgnored public var onDidSyncAndActivate: (() -> Void)?
    @ObservationIgnored public var canMutateEnvironment: (@MainActor () -> Bool)?

    public var banner: StatusBanner? {
        bannerEvent?.statusBanner(in: preferencesStore.effectiveLanguage)
    }

    private let cli: any CLIRunnerProtocol
    private let activator: any AppActivating
    private let explicitFeedLimit: Int?
    private let nowProvider: @Sendable () -> Date
    @ObservationIgnored private var activeRefreshTask: Task<Bool, Never>?

    public var needsAttentionBadge: Bool {
        guard let state = snapshot?.hero?.state else { return false }
        return state == .interruptedStopped || state == .pendingReconcile
    }

    public var effectiveFeedLimit: Int {
        explicitFeedLimit ?? preferencesStore.preferences.recentFeedLimit
    }

    public var sections: [PaletteSection] {
        let lang = preferencesStore.effectiveLanguage
        var built: [PaletteSection] = []
        let hero = snapshot?.hero
        if let hero, hero.matchesSearchQuery(searchQuery) {
            built.append(
                PaletteSection(
                    id: "continue",
                    title: AppCopy.text(.sectionContinue, in: lang),
                    items: [hero]
                )
            )
        }

        var seenIds = Set<String>()
        if let heroId = hero?.id {
            seenIds.insert(heroId)
        }
        var filteredRecent: [MenubarSessionItem] = []
        for item in snapshot?.recent ?? [] {
            guard seenIds.insert(item.id).inserted else { continue }
            if item.matchesSearchQuery(searchQuery) {
                filteredRecent.append(item)
            }
        }
        let cappedRecent = Array(filteredRecent.prefix(max(1, effectiveFeedLimit - 1)))
        if !cappedRecent.isEmpty {
            built.append(
                PaletteSection(
                    id: "recent",
                    title: AppCopy.text(.sectionRecent, in: lang),
                    items: cappedRecent
                )
            )
        }
        return built
    }

    public var visibleItems: [MenubarSessionItem] {
        sections.flatMap(\.items)
    }

    public var selectedItem: MenubarSessionItem? {
        guard let selectedItemId else { return nil }
        return visibleItems.first { $0.id == selectedItemId }
    }

    public init(
        cli: any CLIRunnerProtocol = SubprocessCLIClient(),
        activator: any AppActivating = WorkspaceAppActivator(),
        preferencesStore: PreferencesStore? = nil,
        feedLimit: Int? = nil,
        defaults: UserDefaults? = nil,
        nowProvider: @escaping @Sendable () -> Date = Date.init,
        canMutateEnvironment: (@MainActor () -> Bool)? = nil,
        onDidSyncAndActivate: (() -> Void)? = nil
    ) {
        self.cli = cli
        self.activator = activator
        let store: PreferencesStore
        if let preferencesStore {
            store = preferencesStore
        } else if let defaults {
            store = PreferencesStore(
                defaults: defaults,
                loginItemManager: NoOpLoginItemManager()
            )
        } else {
            store = PreferencesStore()
        }
        self.preferencesStore = store
        self.explicitFeedLimit = feedLimit
        self.nowProvider = nowProvider
        self.feedReferenceEpoch = nowProvider().timeIntervalSince1970
        self.canMutateEnvironment = canMutateEnvironment
        self.onDidSyncAndActivate = onDidSyncAndActivate
    }

    public func updateFeedReferenceTimestamp() {
        feedReferenceEpoch = nowProvider().timeIntervalSince1970
    }

    public func dismissBanner() {
        bannerEvent = nil
    }

    public func selectItem(_ id: String?) {
        guard let id else {
            selectedItemId = nil
            return
        }
        if visibleItems.contains(where: { $0.id == id }) {
            selectedItemId = id
        }
    }

    public func moveSelectionDown() {
        let candidates = navigableItems()
        guard !candidates.isEmpty else {
            selectedItemId = nil
            return
        }
        guard let current = selectedItemId,
              let index = candidates.firstIndex(where: { $0.id == current })
        else {
            selectedItemId = candidates.first?.id
            return
        }
        selectedItemId = candidates[(index + 1) % candidates.count].id
    }

    public func moveSelectionUp() {
        let candidates = navigableItems()
        guard !candidates.isEmpty else {
            selectedItemId = nil
            return
        }
        guard let current = selectedItemId,
              let index = candidates.firstIndex(where: { $0.id == current })
        else {
            selectedItemId = candidates.last?.id
            return
        }
        selectedItemId = candidates[(index - 1 + candidates.count) % candidates.count].id
    }

    public func refresh() async {
        await refreshFeed(preserveBanner: false)
    }

    public func performSelectedAction() async {
        guard let item = selectedItem else {
            await performPrimaryAction()
            return
        }
        await executeItemAction(item)
    }

    public func performPrimaryAction() async {
        if snapshot == nil {
            await refreshFeed(preserveBanner: false)
        }
        guard let hero = snapshot?.hero else { return }
        await executeItemAction(hero)
    }

    public func refreshAndPerformPrimaryAction() async {
        guard busyItemId == nil else { return }
        if let existingTask = activeRefreshTask {
            let succeeded = await existingTask.value
            if succeeded {
                bannerEvent = nil
            }
        } else {
            await refreshFeed(preserveBanner: false)
        }
        guard let hero = snapshot?.hero else { return }
        await executeItemAction(hero)
    }

    private func executeItemAction(_ item: MenubarSessionItem) async {
        switch item.state {
        case .interruptedStopped, .completedReady:
            await sync(item)
        case .pendingReconcile:
            await reconcile(workspacePath: item.workspacePath, itemId: item.id)
        case .alreadyHanded, .activeRunning:
            return
        }
    }

    public func sync(_ item: MenubarSessionItem) async {
        guard busyItemId == nil else { return }
        guard canMutateEnvironment?() ?? true else {
            bannerEvent = .syncFailed(LocalizedFailure(error: UpdateServiceError.updateInstallationInProgress))
            return
        }
        busyItemId = item.id
        bannerEvent = nil
        defer { busyItemId = nil }

        do {
            let receipt = try await cli.executeSync(
                workspacePath: item.workspacePath,
                sourceId: item.sessionId
            )
            cliMissing = false
            let targetName = HostDisplayMeta.displayName(
                for: receipt.targetHost.isEmpty ? item.targetHost : receipt.targetHost
            )
            if receipt.status == "ready" {
                bannerEvent = .syncOpened(targetName: targetName, title: item.title)
                if preferencesStore.preferences.activateTargetAppAfterSync {
                    _ = await activator.activateTarget(host: item.targetHost)
                    onDidSyncAndActivate?()
                }
            } else if receipt.status == "no_op" {
                bannerEvent = .syncOpenedExisting(targetName: targetName, title: item.title)
                if preferencesStore.preferences.activateTargetAppAfterSync {
                    _ = await activator.activateTarget(host: item.targetHost)
                    onDidSyncAndActivate?()
                }
            } else {
                bannerEvent = .syncUnconfirmed
            }
            await refreshFeed(preserveBanner: true)
        } catch {
            updateMissingFlag(for: error)
            bannerEvent = .syncFailed(LocalizedFailure(error: error))
        }
    }

    public func reconcile(workspacePath: String? = nil, itemId: String? = nil) async {
        guard busyItemId == nil else { return }
        guard canMutateEnvironment?() ?? true else {
            bannerEvent = .reconcileFailed(LocalizedFailure(error: UpdateServiceError.updateInstallationInProgress))
            return
        }
        busyItemId = itemId ?? workspacePath ?? "__reconcile__"
        bannerEvent = nil
        defer { busyItemId = nil }

        do {
            var unresolved = 0

            if let workspacePath, !workspacePath.isEmpty {
                let summary = try await cli.reconcilePending(workspacePath: workspacePath)
                unresolved += summary.unresolved.count
            } else {
                let pendingWorkspaces = deduplicatedPendingWorkspaces()
                for ws in pendingWorkspaces {
                    let summary = try await cli.reconcilePending(workspacePath: ws)
                    unresolved += summary.unresolved.count
                }
            }

            cliMissing = false
            bannerEvent = .reconcileCompleted(unresolvedCount: unresolved)
            await refreshFeed(preserveBanner: true)
        } catch {
            updateMissingFlag(for: error)
            bannerEvent = .reconcileFailed(LocalizedFailure(error: error))
        }
    }

    public func setCustomCLIPath(_ path: String) {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        preferencesStore.setCustomCLIPath(trimmed)
        cliMissing = false
        bannerEvent = nil
        Task {
            await refresh()
        }
    }

    private func refreshFeed(preserveBanner: Bool) async {
        if let existingTask = activeRefreshTask {
            let succeeded = await existingTask.value
            if succeeded && !preserveBanner {
                bannerEvent = nil
            }
            return
        }

        isLoading = true
        let task = Task<Bool, Never> { @MainActor in
            do {
                let nextSnapshot = try await cli.fetchFeed(limit: effectiveFeedLimit)
                snapshot = nextSnapshot
                feedReferenceEpoch = nowProvider().timeIntervalSince1970
                reconcileSelection()
                cliMissing = false
                return true
            } catch {
                applyError(error)
                return false
            }
        }
        activeRefreshTask = task
        let succeeded = await task.value
        activeRefreshTask = nil
        isLoading = false
        if succeeded && !preserveBanner {
            bannerEvent = nil
        }
    }

    private func navigableItems() -> [MenubarSessionItem] {
        let lang = preferencesStore.effectiveLanguage
        let items = visibleItems
        let actionable = items.filter { $0.presentation(language: lang).isActionable }
        return actionable.isEmpty ? items : actionable
    }

    private func reconcileSelection() {
        let lang = preferencesStore.effectiveLanguage
        let items = visibleItems
        guard !items.isEmpty else {
            selectedItemId = nil
            return
        }
        let actionable = items.filter { $0.presentation(language: lang).isActionable }
        if let current = selectedItemId {
            if actionable.contains(where: { $0.id == current }) {
                return
            }
            if actionable.isEmpty, items.contains(where: { $0.id == current }) {
                return
            }
        }
        selectedItemId = (actionable.first ?? items.first)?.id
    }

    private func deduplicatedPendingWorkspaces() -> [String] {
        var result: [String] = []
        var seen = Set<String>()
        let items = [snapshot?.hero].compactMap { $0 } + (snapshot?.recent ?? [])
        for item in items where item.state == .pendingReconcile {
            if seen.insert(item.workspacePath).inserted {
                result.append(item.workspacePath)
            }
        }
        return result
    }

    private func updateMissingFlag(for error: Error) {
        if let cliErr = error as? CLIClientError, cliErr.isMissingExecutable {
            cliMissing = true
        }
    }

    private func applyError(_ error: Error) {
        updateMissingFlag(for: error)
        bannerEvent = .feedFailed(LocalizedFailure(error: error))
    }
}
