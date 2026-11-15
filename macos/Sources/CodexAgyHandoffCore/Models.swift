import Foundation

public enum MenubarCandidateState: String, Codable, Sendable, Equatable, CaseIterable {
    case interruptedStopped = "interrupted_stopped"
    case completedReady = "completed_ready"
    case pendingReconcile = "pending_reconcile"
    case alreadyHanded = "already_handed"
    case activeRunning = "active_running"

    public var statusBadgeText: String {
        switch self {
        case .interruptedStopped:
            return "未完成回复"
        case .completedReady:
            return "可继续"
        case .pendingReconcile:
            return "待确认"
        case .alreadyHanded:
            return "已同步"
        case .activeRunning:
            return "对话中"
        }
    }
}

public enum HostDisplayMeta {
    public static func displayName(for host: String) -> String {
        switch host.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "codex":
            return "Codex"
        case "antigravity", "agy":
            return "Antigravity"
        default:
            return host
        }
    }

    public static func monogram(for host: String) -> String {
        switch host.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "antigravity", "agy":
            return "A"
        default:
            return "C"
        }
    }
}

public struct MenubarRowPresentation: Equatable, Sendable {
    public let monogram: String
    public let isActionable: Bool
    public let subtitle: String
    public let selectedActionTitle: String
    public let unselectedTrailingTitle: String
    public let accessibilityLabel: String

    public init(
        monogram: String,
        isActionable: Bool,
        subtitle: String,
        selectedActionTitle: String,
        unselectedTrailingTitle: String,
        accessibilityLabel: String
    ) {
        self.monogram = monogram
        self.isActionable = isActionable
        self.subtitle = subtitle
        self.selectedActionTitle = selectedActionTitle
        self.unselectedTrailingTitle = unselectedTrailingTitle
        self.accessibilityLabel = accessibilityLabel
    }
}

public struct MenubarSessionItem: Codable, Sendable, Equatable, Identifiable {
    public let host: String
    public let targetHost: String
    public let direction: String
    public let sessionId: String
    public let title: String
    public let workspacePath: String
    public let workspaceName: String
    public let paired: Bool
    public let state: MenubarCandidateState
    public let updatedAtIso: String
    public let sortEpoch: Double
    public let turnCount: Int
    public let hasInflightTurn: Bool
    public let handedTargetId: String?
    public let pendingError: String?
    public let chainDepth: Int

    public var id: String { "\(host):\(sessionId)" }

    public var sourceDisplayName: String {
        HostDisplayMeta.displayName(for: host)
    }

    public var targetDisplayName: String {
        HostDisplayMeta.displayName(for: targetHost)
    }

    public func presentation(nowEpoch: Double? = nil) -> MenubarRowPresentation {
        let monogram = HostDisplayMeta.monogram(for: host)
        let isActionable: Bool
        let subtitle: String
        let selectedActionTitle: String
        let unselectedTrailingTitle: String

        switch state {
        case .interruptedStopped:
            isActionable = true
            subtitle = "\(workspaceName) · \(turnCount) 轮 · 未完成回复"
            selectedActionTitle = "用 \(targetDisplayName) 继续"
            unselectedTrailingTitle = "→ \(targetDisplayName)"
        case .completedReady:
            isActionable = true
            var base = "\(workspaceName) · \(turnCount) 轮"
            if let nowEpoch, sortEpoch > 0, nowEpoch >= sortEpoch {
                base += Self.relativeAgeSuffix(elapsedSeconds: nowEpoch - sortEpoch)
            }
            subtitle = base
            selectedActionTitle = "用 \(targetDisplayName) 继续"
            unselectedTrailingTitle = "→ \(targetDisplayName)"
        case .pendingReconcile:
            isActionable = true
            subtitle = "\(workspaceName) · 上次切换未完成确认"
            selectedActionTitle = "重新同步状态"
            unselectedTrailingTitle = "重试"
        case .alreadyHanded:
            isActionable = false
            subtitle = "\(workspaceName) · \(turnCount) 轮 · 已同步"
            selectedActionTitle = "已同步"
            unselectedTrailingTitle = "已同步"
        case .activeRunning:
            isActionable = false
            subtitle = "\(workspaceName) · 对话进行中"
            selectedActionTitle = "对话中"
            unselectedTrailingTitle = "对话中"
        }

        let accessibilityLabel = isActionable
            ? "\(selectedActionTitle) · \(title)"
            : "\(title) · \(unselectedTrailingTitle)"

        return MenubarRowPresentation(
            monogram: monogram,
            isActionable: isActionable,
            subtitle: subtitle,
            selectedActionTitle: selectedActionTitle,
            unselectedTrailingTitle: unselectedTrailingTitle,
            accessibilityLabel: accessibilityLabel
        )
    }

    public func matchesSearchQuery(_ rawQuery: String) -> Bool {
        let trimmed = rawQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return true }
        let needle = trimmed.lowercased()
        let haystacks = [
            title,
            workspaceName,
            workspacePath,
            host,
            sourceDisplayName,
            targetDisplayName,
            presentation().subtitle,
        ]
        return haystacks.contains { $0.lowercased().contains(needle) }
    }

    private static func relativeAgeSuffix(elapsedSeconds: Double) -> String {
        let seconds = max(0, Int(elapsedSeconds))
        if seconds < 60 {
            return " · 刚刚"
        }
        if seconds < 3600 {
            return " · \(max(1, seconds / 60)) 分钟前"
        }
        if seconds < 86_400 {
            return " · \(max(1, seconds / 3600)) 小时前"
        }
        return " · \(max(1, seconds / 86_400)) 天前"
    }

    public init(
        host: String,
        targetHost: String,
        direction: String,
        sessionId: String,
        title: String,
        workspacePath: String,
        workspaceName: String,
        paired: Bool,
        state: MenubarCandidateState,
        updatedAtIso: String,
        sortEpoch: Double,
        turnCount: Int,
        hasInflightTurn: Bool,
        handedTargetId: String? = nil,
        pendingError: String? = nil,
        chainDepth: Int = 0
    ) {
        self.host = host
        self.targetHost = targetHost
        self.direction = direction
        self.sessionId = sessionId
        self.title = title
        self.workspacePath = workspacePath
        self.workspaceName = workspaceName
        self.paired = paired
        self.state = state
        self.updatedAtIso = updatedAtIso
        self.sortEpoch = sortEpoch
        self.turnCount = turnCount
        self.hasInflightTurn = hasInflightTurn
        self.handedTargetId = handedTargetId
        self.pendingError = pendingError
        self.chainDepth = chainDepth
    }

    public enum CodingKeys: String, CodingKey {
        case host
        case targetHost = "target_host"
        case direction
        case sessionId = "session_id"
        case title
        case workspacePath = "workspace_path"
        case workspaceName = "workspace_name"
        case paired
        case state
        case updatedAtIso = "updated_at_iso"
        case sortEpoch = "sort_epoch"
        case turnCount = "turn_count"
        case hasInflightTurn = "has_inflight_turn"
        case handedTargetId = "handed_target_id"
        case pendingError = "pending_error"
        case chainDepth = "chain_depth"
    }
}

public struct MenubarFeedSnapshot: Codable, Sendable, Equatable {
    public let generatedAtIso: String
    public let agyDaemonActive: Bool
    public let codexDaemonActive: Bool
    public let hero: MenubarSessionItem?
    public let recent: [MenubarSessionItem]

    public init(
        generatedAtIso: String,
        agyDaemonActive: Bool,
        codexDaemonActive: Bool,
        hero: MenubarSessionItem?,
        recent: [MenubarSessionItem]
    ) {
        self.generatedAtIso = generatedAtIso
        self.agyDaemonActive = agyDaemonActive
        self.codexDaemonActive = codexDaemonActive
        self.hero = hero
        self.recent = recent
    }

    public enum CodingKeys: String, CodingKey {
        case generatedAtIso = "generated_at_iso"
        case agyDaemonActive = "agy_daemon_active"
        case codexDaemonActive = "codex_daemon_active"
        case hero
        case recent
    }
}

public struct HandoffReceiptPayload: Codable, Sendable, Equatable {
    public let workspace: String
    public let sourceHost: String
    public let sourceId: String
    public let sourceDigest: String
    public let targetHost: String
    public let targetId: String?
    public let title: String
    public let status: String
    public let stagingPath: String
    public let runtimeStateNotTransferred: Bool
    public let error: String?
    public let createdAtIso: String
    public let chainId: String?
    public let chainedFromSourceId: String?
    public let chainDepth: Int
    public let targetSeedTurnCount: Int?
    public let targetSeedTextDigest: String?
    public let pairingMode: String?

    public init(
        workspace: String,
        sourceHost: String,
        sourceId: String,
        sourceDigest: String = "",
        targetHost: String,
        targetId: String? = nil,
        title: String,
        status: String,
        stagingPath: String = "",
        runtimeStateNotTransferred: Bool = false,
        error: String? = nil,
        createdAtIso: String = "",
        chainId: String? = nil,
        chainedFromSourceId: String? = nil,
        chainDepth: Int = 0,
        targetSeedTurnCount: Int? = nil,
        targetSeedTextDigest: String? = nil,
        pairingMode: String? = nil
    ) {
        self.workspace = workspace
        self.sourceHost = sourceHost
        self.sourceId = sourceId
        self.sourceDigest = sourceDigest
        self.targetHost = targetHost
        self.targetId = targetId
        self.title = title
        self.status = status
        self.stagingPath = stagingPath
        self.runtimeStateNotTransferred = runtimeStateNotTransferred
        self.error = error
        self.createdAtIso = createdAtIso
        self.chainId = chainId
        self.chainedFromSourceId = chainedFromSourceId
        self.chainDepth = chainDepth
        self.targetSeedTurnCount = targetSeedTurnCount
        self.targetSeedTextDigest = targetSeedTextDigest
        self.pairingMode = pairingMode
    }

    public enum CodingKeys: String, CodingKey {
        case workspace
        case sourceHost = "source_host"
        case sourceId = "source_id"
        case sourceDigest = "source_digest"
        case targetHost = "target_host"
        case targetId = "target_id"
        case title
        case status
        case stagingPath = "staging_path"
        case runtimeStateNotTransferred = "runtime_state_not_transferred"
        case error
        case createdAtIso = "created_at_iso"
        case chainId = "chain_id"
        case chainedFromSourceId = "chained_from_source_id"
        case chainDepth = "chain_depth"
        case targetSeedTurnCount = "target_seed_turn_count"
        case targetSeedTextDigest = "target_seed_text_digest"
        case pairingMode = "pairing_mode"
    }
}

public struct ReconcileEntryPayload: Codable, Sendable, Equatable {
    public let receiptFile: String
    public let sourceHost: String
    public let sourceId: String
    public let targetHost: String
    public let targetId: String?
    public let title: String
    public let reason: String
    public let stagingCleaned: Bool

    public init(
        receiptFile: String = "",
        sourceHost: String,
        sourceId: String,
        targetHost: String,
        targetId: String? = nil,
        title: String,
        reason: String,
        stagingCleaned: Bool = false
    ) {
        self.receiptFile = receiptFile
        self.sourceHost = sourceHost
        self.sourceId = sourceId
        self.targetHost = targetHost
        self.targetId = targetId
        self.title = title
        self.reason = reason
        self.stagingCleaned = stagingCleaned
    }

    public enum CodingKeys: String, CodingKey {
        case receiptFile = "receipt_file"
        case sourceHost = "source_host"
        case sourceId = "source_id"
        case targetHost = "target_host"
        case targetId = "target_id"
        case title
        case reason
        case stagingCleaned = "staging_cleaned"
    }
}

public struct ReconcileSummaryPayload: Codable, Sendable, Equatable {
    public let workspace: String
    public let dryRun: Bool
    public let promoted: [ReconcileEntryPayload]
    public let cleared: [ReconcileEntryPayload]
    public let unresolved: [ReconcileEntryPayload]
    public let totalPendingBefore: Int
    public let totalPendingAfter: Int

    public init(
        workspace: String,
        dryRun: Bool = false,
        promoted: [ReconcileEntryPayload] = [],
        cleared: [ReconcileEntryPayload] = [],
        unresolved: [ReconcileEntryPayload] = [],
        totalPendingBefore: Int = 0,
        totalPendingAfter: Int = 0
    ) {
        self.workspace = workspace
        self.dryRun = dryRun
        self.promoted = promoted
        self.cleared = cleared
        self.unresolved = unresolved
        self.totalPendingBefore = totalPendingBefore
        self.totalPendingAfter = totalPendingAfter
    }

    public enum CodingKeys: String, CodingKey {
        case workspace
        case dryRun = "dry_run"
        case promoted
        case cleared
        case unresolved
        case totalPendingBefore = "total_pending_before"
        case totalPendingAfter = "total_pending_after"
    }
}

public struct CLIErrorPayload: Codable, Sendable, Equatable, Error {
    public let error: String
    public let message: String
    public let cause: String?

    public init(error: String, message: String, cause: String? = nil) {
        self.error = error
        self.message = message
        self.cause = cause
    }
}
