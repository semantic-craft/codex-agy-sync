import Foundation
import SwiftUI

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
public final class HandoffViewModel: ObservableObject {
    @Published public private(set) var snapshot: MenubarFeedSnapshot?
    @Published public private(set) var isLoading: Bool = false
    @Published public private(set) var busyItemId: String?
    @Published public private(set) var banner: StatusBanner?
    @Published public private(set) var cliMissing: Bool = false
    @Published public var searchQuery: String = "" {
        didSet {
            if searchQuery != oldValue {
                reconcileSelection()
            }
        }
    }
    @Published public private(set) var selectedItemId: String?

    public let preferencesStore: PreferencesStore
    public var onDidHandoffAndActivate: (() -> Void)?

    private let cli: any CLIRunnerProtocol
    private let activator: any AppActivating
    private let explicitFeedLimit: Int?

    public var needsAttentionBadge: Bool {
        guard let state = snapshot?.hero?.state else { return false }
        return state == .interruptedStopped || state == .pendingReconcile
    }

    public var effectiveFeedLimit: Int {
        explicitFeedLimit ?? preferencesStore.preferences.recentFeedLimit
    }

    public var sections: [PaletteSection] {
        var built: [PaletteSection] = []
        let hero = snapshot?.hero
        if let hero, hero.matchesSearchQuery(searchQuery) {
            built.append(PaletteSection(id: "continue", title: "继续当前工作", items: [hero]))
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
            built.append(PaletteSection(id: "recent", title: "最近对话", items: cappedRecent))
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
        onDidHandoffAndActivate: (() -> Void)? = nil
    ) {
        self.cli = cli
        self.activator = activator
        if let preferencesStore {
            self.preferencesStore = preferencesStore
        } else if let defaults {
            self.preferencesStore = PreferencesStore(
                defaults: defaults,
                loginItemManager: NoOpLoginItemManager()
            )
        } else {
            self.preferencesStore = PreferencesStore()
        }
        self.explicitFeedLimit = feedLimit
        self.onDidHandoffAndActivate = onDidHandoffAndActivate
    }

    public func dismissBanner() {
        banner = nil
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
        switch item.state {
        case .interruptedStopped, .completedReady:
            await handoff(item: item)
        case .pendingReconcile:
            await reconcile(workspacePath: item.workspacePath, itemId: item.id)
        case .alreadyHanded, .activeRunning:
            return
        }
    }

    public func performPrimaryAction() async {
        if snapshot == nil {
            await refreshFeed(preserveBanner: false)
        }
        guard let hero = snapshot?.hero else { return }
        switch hero.state {
        case .interruptedStopped, .completedReady:
            await handoff(item: hero)
        case .pendingReconcile:
            await reconcile(workspacePath: hero.workspacePath, itemId: hero.id)
        case .alreadyHanded, .activeRunning:
            return
        }
    }

    public func handoff(item: MenubarSessionItem) async {
        await handoff(item)
    }

    public func handoff(_ item: MenubarSessionItem) async {
        guard busyItemId == nil else { return }
        busyItemId = item.id
        banner = nil
        defer { busyItemId = nil }

        do {
            let receipt = try await cli.executeHandoff(
                workspacePath: item.workspacePath,
                sourceId: item.sessionId
            )
            cliMissing = false
            let targetName = HostDisplayMeta.displayName(
                for: receipt.targetHost.isEmpty ? item.targetHost : receipt.targetHost
            )
            if receipt.status == "ready" {
                banner = StatusBanner(kind: .success, text: "已在 \(targetName) 打开对话 · \(item.title)")
                if preferencesStore.preferences.activateTargetAppAfterHandoff {
                    _ = await activator.activateTarget(host: item.targetHost)
                    onDidHandoffAndActivate?()
                }
            } else if receipt.status == "no_op" {
                banner = StatusBanner(kind: .info, text: "已在 \(targetName) 打开现有对话 · \(item.title)")
                if preferencesStore.preferences.activateTargetAppAfterHandoff {
                    _ = await activator.activateTarget(host: item.targetHost)
                    onDidHandoffAndActivate?()
                }
            } else {
                banner = StatusBanner(kind: .error, text: "切换尚未完成确认，请稍后点击重试同步状态。")
            }
            await refreshFeed(preserveBanner: true)
        } catch {
            updateMissingFlag(for: error)
            banner = StatusBanner(kind: .error, text: "切换未成功：\(Self.describeError(error))")
        }
    }

    public func reconcile(workspacePath: String? = nil, itemId: String? = nil) async {
        guard busyItemId == nil else { return }
        busyItemId = itemId ?? workspacePath ?? "__reconcile__"
        banner = nil
        defer { busyItemId = nil }

        do {
            var unresolved = 0

            if let workspacePath, !workspacePath.isEmpty {
                let summary = try await cli.reconcilePending(workspacePath: workspacePath)
                unresolved += summary.unresolved.count
            } else {
                let pendingWorkspaces = deduplicatedPendingWorkspaces()
                if pendingWorkspaces.isEmpty {
                    let summary = try await cli.reconcilePending()
                    unresolved += summary.unresolved.count
                } else {
                    for ws in pendingWorkspaces {
                        let summary = try await cli.reconcilePending(workspacePath: ws)
                        unresolved += summary.unresolved.count
                    }
                }
            }

            cliMissing = false
            let message = unresolved == 0 ? "状态已同步" : "状态已同步，仍有 \(unresolved) 条对话等待响应"
            banner = StatusBanner(kind: .success, text: message)
            await refreshFeed(preserveBanner: true)
        } catch {
            updateMissingFlag(for: error)
            banner = StatusBanner(kind: .error, text: "状态同步未成功：\(Self.describeError(error))")
        }
    }

    public func setCustomCLIPath(_ path: String) {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        preferencesStore.setCustomCLIPath(trimmed)
        cliMissing = false
        banner = nil
        Task {
            await refresh()
        }
    }

    private func refreshFeed(preserveBanner: Bool) async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }

        do {
            let nextSnapshot = try await cli.fetchFeed(limit: effectiveFeedLimit)
            snapshot = nextSnapshot
            reconcileSelection()
            cliMissing = false
            if !preserveBanner {
                banner = nil
            }
        } catch {
            applyError(error)
        }
    }

    private func navigableItems() -> [MenubarSessionItem] {
        let items = visibleItems
        let actionable = items.filter { $0.presentation().isActionable }
        return actionable.isEmpty ? items : actionable
    }

    private func reconcileSelection() {
        let items = visibleItems
        guard !items.isEmpty else {
            selectedItemId = nil
            return
        }
        let actionable = items.filter { $0.presentation().isActionable }
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
        banner = StatusBanner(kind: .error, text: Self.describeError(error))
    }

    private static func describeError(_ error: Error) -> String {
        if let localized = error as? LocalizedError, let message = localized.errorDescription {
            return message
        }
        return error.localizedDescription
    }
}
