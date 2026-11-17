import Foundation

extension UpdateViewModel {
    public static func resolveSyncStateDirectoryURL(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default
    ) -> URL {
        LocalRepoEnvironment.resolveSyncStateDirectoryURL(environment: environment, fileManager: fileManager)
    }

    public static func isValidSourceRepo(_ repoURL: URL, fileManager: FileManager = .default) -> Bool {
        LocalRepoEnvironment.isValidSourceRepo(repoURL, fileManager: fileManager)
    }

    public static func parsePyprojectVersion(in repoURL: URL) -> String? {
        LocalRepoEnvironment.parsePyprojectVersion(in: repoURL)
    }

    public static func loadCheckpoint(from defaults: UserDefaults) -> UpdateCheckCheckpoint {
        UpdateStagingStore.loadCheckpoint(from: defaults)
    }

    public static func saveCheckpoint(_ checkpoint: UpdateCheckCheckpoint, to defaults: UserDefaults) {
        UpdateStagingStore.saveCheckpoint(checkpoint, to: defaults)
    }

    public static func hasCompletedCooldown(
        now: TimeInterval,
        checkpoint: UpdateCheckCheckpoint
    ) -> Bool {
        UpdateStagingStore.hasCompletedCooldown(now: now, checkpoint: checkpoint)
    }

    nonisolated static func isRealAppBundleDirectory(
        _ candidate: URL,
        inside parentDir: URL? = nil,
        fileManager: FileManager = .default
    ) -> Bool {
        UpdateStagingStore.isRealAppBundleDirectory(candidate, inside: parentDir, fileManager: fileManager)
    }

    nonisolated static func resolveExtractedAppBundle(
        in extractDir: URL,
        fileManager: FileManager = .default
    ) -> URL {
        UpdateStagingStore.resolveExtractedAppBundle(in: extractDir, fileManager: fileManager)
    }
}
