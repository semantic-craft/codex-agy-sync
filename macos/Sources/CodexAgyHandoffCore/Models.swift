import Foundation

public enum MenubarCandidateState: String, Codable, Sendable, Equatable, CaseIterable {
    case interruptedStopped = "interrupted_stopped"
    case completedReady = "completed_ready"
    case pendingReconcile = "pending_reconcile"
    case alreadyHanded = "already_handed"
    case activeRunning = "active_running"
}

public enum HostDisplayMeta {
    public static func displayName(for host: String) -> String {
        switch host.lowercased() {
        case "codex":
            return "Codex"
        case "antigravity":
            return "Antigravity"
        default:
            return host
        }
    }

    public static func shortTag(for host: String) -> String {
        switch host.lowercased() {
        case "codex":
            return "CDX"
        case "antigravity":
            return "AGY"
        default:
            return host.uppercased()
        }
    }

    public static func directionBadge(from sourceHost: String, to targetHost: String) -> String {
        "\(shortTag(for: sourceHost)) → \(shortTag(for: targetHost))"
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

    public var targetDisplayName: String {
        HostDisplayMeta.displayName(for: targetHost)
    }

    public var sourceShortTag: String {
        HostDisplayMeta.shortTag(for: host)
    }

    public var directionBadge: String {
        HostDisplayMeta.directionBadge(from: host, to: targetHost)
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
