import Foundation
import SwiftUI

public enum StatusBanner: Equatable, Sendable {
    case success(String)
    case error(String)
}

@MainActor
public final class HandoffViewModel: ObservableObject {
    @Published public private(set) var snapshot: MenubarFeedSnapshot?
    @Published public private(set) var isLoading: Bool = false
    @Published public private(set) var busyItemId: String?
    @Published public private(set) var banner: StatusBanner?
    @Published public private(set) var cliMissing: Bool = false

    public var onDidHandoffAndActivate: (() -> Void)?

    private let cli: any CLIRunnerProtocol
    private let activator: any AppActivating
    private let feedLimit: Int
    private let defaults: UserDefaults

    public init(
        cli: any CLIRunnerProtocol = SubprocessCLIClient(),
        activator: any AppActivating = WorkspaceAppActivator(),
        feedLimit: Int = 6,
        defaults: UserDefaults = .standard,
        onDidHandoffAndActivate: (() -> Void)? = nil
    ) {
        self.cli = cli
        self.activator = activator
        self.feedLimit = feedLimit
        self.defaults = defaults
        self.onDidHandoffAndActivate = onDidHandoffAndActivate
    }

    public func dismissBanner() {
        banner = nil
    }

    public func refresh() async {
        await refreshFeed(preserveBanner: false)
    }

    public func performPrimaryAction() async {
        if snapshot == nil {
            await refreshFeed(preserveBanner: false)
        }
        guard let hero = snapshot?.hero else { return }
        switch hero.state {
        case .interruptedStopped, .completedReady:
            await handoff(hero)
        case .pendingReconcile:
            await reconcile(workspacePath: hero.workspacePath)
        case .alreadyHanded, .activeRunning:
            return
        }
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
            if receipt.status == "ready" || receipt.status == "no_op" {
                let targetName = HostDisplayMeta.displayName(
                    for: receipt.targetHost.isEmpty ? item.targetHost : receipt.targetHost
                )
                let shortTarget = receipt.targetId.map { " (\($0.prefix(8)))" } ?? ""
                let reusedNote = receipt.status == "no_op" ? "（no_op · 沿用已有会话）" : ""
                banner = .success("已交接至 \(targetName)\(shortTarget)\(reusedNote) · \(item.title)")
                _ = await activator.activateTarget(host: item.targetHost)
                onDidHandoffAndActivate?()
            } else {
                banner = .error(
                    receipt.error ?? "交接处于挂起状态（pending），请在 \(item.targetDisplayName) 打开或稍后核对。"
                )
            }
            await refreshFeed(preserveBanner: true)
        } catch {
            applyError(error)
        }
    }

    public func reconcile(workspacePath: String? = nil) async {
        guard busyItemId == nil else { return }
        busyItemId = workspacePath ?? "__reconcile__"
        banner = nil
        defer { busyItemId = nil }

        do {
            var promoted = 0
            var cleared = 0
            var unresolved = 0

            if let workspacePath, !workspacePath.isEmpty {
                let summary = try await cli.reconcilePending(workspacePath: workspacePath)
                promoted += summary.promoted.count
                cleared += summary.cleared.count
                unresolved += summary.unresolved.count
            } else {
                let pendingWorkspaces = deduplicatedPendingWorkspaces()
                if pendingWorkspaces.isEmpty {
                    let summary = try await cli.reconcilePending()
                    promoted += summary.promoted.count
                    cleared += summary.cleared.count
                    unresolved += summary.unresolved.count
                } else {
                    for ws in pendingWorkspaces {
                        let summary = try await cli.reconcilePending(workspacePath: ws)
                        promoted += summary.promoted.count
                        cleared += summary.cleared.count
                        unresolved += summary.unresolved.count
                    }
                }
            }

            cliMissing = false
            banner = .success("核对完成：晋升 \(promoted) 项，清理 \(cleared) 项，待确认 \(unresolved) 项")
            await refreshFeed(preserveBanner: true)
        } catch {
            applyError(error)
        }
    }

    public func setCustomCLIPath(_ path: String) {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        defaults.set(trimmed, forKey: SubprocessCLIClient.cliExecutableDefaultsKey)
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
            let nextSnapshot = try await cli.fetchFeed(limit: feedLimit)
            snapshot = nextSnapshot
            cliMissing = false
            if !preserveBanner {
                banner = nil
            }
        } catch {
            applyError(error)
        }
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

    private func applyError(_ error: Error) {
        if let cliErr = error as? CLIClientError, cliErr.isMissingExecutable {
            cliMissing = true
        }
        if let localized = error as? LocalizedError, let message = localized.errorDescription {
            banner = .error(message)
        } else {
            banner = .error(error.localizedDescription)
        }
    }
}
