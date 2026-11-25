import Darwin
import Foundation

public enum CLIInstallStatus: Equatable, Sendable {
    case bundledCLIMissing
    case notInstalled
    case installed(targetPath: String)
    case outdatedSymlink(currentTarget: String, expectedTarget: String)
    case conflictNonManaged(existingPath: String)
}

public struct CLIInstallState: Equatable, Sendable {
    public let status: CLIInstallStatus
    public let installPath: String
    public let bundledCLIPath: String?
    public let isInstallDirectoryOnPATH: Bool

    public init(
        status: CLIInstallStatus,
        installPath: String,
        bundledCLIPath: String?,
        isInstallDirectoryOnPATH: Bool
    ) {
        self.status = status
        self.installPath = installPath
        self.bundledCLIPath = bundledCLIPath
        self.isInstallDirectoryOnPATH = isInstallDirectoryOnPATH
    }
}

public enum CLIInstallError: Error, Equatable, Sendable {
    case bundledCLIMissing
    case conflictNonManaged(String)
    case filesystemError(String)
}

public struct CLIInstallService: Sendable {
    public let installDirectoryURL: URL
    public let cliName: String
    private let bundledCLIResolver: @Sendable () -> URL?
    private let environment: [String: String]

    public init(
        installDirectoryURL: URL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("bin", isDirectory: true),
        cliName: String = "codex-agy-sync",
        bundledCLIResolver: (@Sendable () -> URL?)? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.installDirectoryURL = installDirectoryURL
        self.cliName = cliName
        self.bundledCLIResolver = bundledCLIResolver ?? {
            Self.defaultBundledCLIURL(bundleURL: Bundle.main.bundleURL, cliName: cliName)
        }
        self.environment = environment
    }

    public static func defaultBundledCLIURL(
        bundleURL: URL = Bundle.main.bundleURL,
        cliName: String = "codex-agy-sync"
    ) -> URL? {
        guard bundleURL.pathExtension.lowercased() == "app" else {
            return nil
        }
        let candidate = bundleURL
            .appendingPathComponent("Contents", isDirectory: true)
            .appendingPathComponent("Helpers", isDirectory: true)
            .appendingPathComponent(cliName, isDirectory: false)
        guard FileManager.default.isExecutableFile(atPath: candidate.path) else {
            return nil
        }
        return candidate
    }

    public var installURL: URL {
        installDirectoryURL.appendingPathComponent(cliName, isDirectory: false)
    }

    public func inspect() -> CLIInstallState {
        let installPath = installURL.standardizedFileURL.path
        let bundledURL = bundledCLIResolver()
        let expectedPath = bundledURL?.standardizedFileURL.path
        let onPATH = Self.isDirectoryOnPATH(installDirectoryURL.path, environment: environment)

        let entry = inspectFilesystemEntry(atPath: installPath)
        switch entry {
        case .missing:
            guard let expectedPath else {
                return CLIInstallState(
                    status: .bundledCLIMissing,
                    installPath: installPath,
                    bundledCLIPath: nil,
                    isInstallDirectoryOnPATH: onPATH
                )
            }
            return CLIInstallState(
                status: .notInstalled,
                installPath: installPath,
                bundledCLIPath: expectedPath,
                isInstallDirectoryOnPATH: onPATH
            )
        case .nonSymlink:
            return CLIInstallState(
                status: .conflictNonManaged(existingPath: installPath),
                installPath: installPath,
                bundledCLIPath: expectedPath,
                isInstallDirectoryOnPATH: onPATH
            )
        case .symlink(let rawTarget, let resolvedTarget):
            guard isManagedTarget(rawTarget: rawTarget, resolvedTarget: resolvedTarget) else {
                return CLIInstallState(
                    status: .conflictNonManaged(existingPath: installPath),
                    installPath: installPath,
                    bundledCLIPath: expectedPath,
                    isInstallDirectoryOnPATH: onPATH
                )
            }
            guard let expectedPath else {
                return CLIInstallState(
                    status: .bundledCLIMissing,
                    installPath: installPath,
                    bundledCLIPath: nil,
                    isInstallDirectoryOnPATH: onPATH
                )
            }
            if pathsReferToSameTarget(resolvedTarget, expectedPath) {
                return CLIInstallState(
                    status: .installed(targetPath: expectedPath),
                    installPath: installPath,
                    bundledCLIPath: expectedPath,
                    isInstallDirectoryOnPATH: onPATH
                )
            }
            return CLIInstallState(
                status: .outdatedSymlink(currentTarget: resolvedTarget, expectedTarget: expectedPath),
                installPath: installPath,
                bundledCLIPath: expectedPath,
                isInstallDirectoryOnPATH: onPATH
            )
        }
    }

    @discardableResult
    public func installOrUpdate() throws -> CLIInstallState {
        let installPath = installURL.standardizedFileURL.path
        switch inspectFilesystemEntry(atPath: installPath) {
        case .missing:
            break
        case .nonSymlink:
            throw CLIInstallError.conflictNonManaged(installPath)
        case .symlink(let rawTarget, let resolvedTarget):
            if !isManagedTarget(rawTarget: rawTarget, resolvedTarget: resolvedTarget) {
                throw CLIInstallError.conflictNonManaged(installPath)
            }
        }

        guard let bundledURL = bundledCLIResolver() else {
            throw CLIInstallError.bundledCLIMissing
        }
        let targetPath = bundledURL.standardizedFileURL.path
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: installDirectoryURL, withIntermediateDirectories: true)
        } catch {
            throw CLIInstallError.filesystemError(error.localizedDescription)
        }

        let tempLinkURL = installDirectoryURL.appendingPathComponent(
            ".\(cliName).tmp.\(UUID().uuidString)",
            isDirectory: false
        )
        do {
            try fm.createSymbolicLink(atPath: tempLinkURL.path, withDestinationPath: targetPath)
        } catch {
            throw CLIInstallError.filesystemError(error.localizedDescription)
        }

        if rename(tempLinkURL.path, installPath) != 0 {
            let code = errno
            try? fm.removeItem(atPath: tempLinkURL.path)
            let message = String(cString: strerror(code))
            throw CLIInstallError.filesystemError(message)
        }

        return inspect()
    }

    @discardableResult
    public func uninstall() throws -> CLIInstallState {
        let installPath = installURL.standardizedFileURL.path
        switch inspectFilesystemEntry(atPath: installPath) {
        case .missing:
            return inspect()
        case .nonSymlink:
            throw CLIInstallError.conflictNonManaged(installPath)
        case .symlink(let rawTarget, let resolvedTarget):
            guard isManagedTarget(rawTarget: rawTarget, resolvedTarget: resolvedTarget) else {
                throw CLIInstallError.conflictNonManaged(installPath)
            }
            do {
                try FileManager.default.removeItem(atPath: installPath)
            } catch {
                throw CLIInstallError.filesystemError(error.localizedDescription)
            }
            return inspect()
        }
    }

    public static func isDirectoryOnPATH(
        _ directoryPath: String,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> Bool {
        guard let rawPATH = environment["PATH"], !rawPATH.isEmpty else {
            return false
        }
        let targetNormalized = normalizeDirectoryPath(directoryPath, environment: environment)
        let targetReal = URL(fileURLWithPath: targetNormalized).resolvingSymlinksInPath().standardizedFileURL.path
        for rawEntry in rawPATH.split(separator: ":") {
            let entry = String(rawEntry).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !entry.isEmpty else { continue }
            let normalized = normalizeDirectoryPath(entry, environment: environment)
            if normalized == targetNormalized {
                return true
            }
            let entryReal = URL(fileURLWithPath: normalized).resolvingSymlinksInPath().standardizedFileURL.path
            if entryReal == targetReal {
                return true
            }
        }
        return false
    }

    private enum FilesystemEntry {
        case missing
        case nonSymlink
        case symlink(rawTarget: String, resolvedTarget: String)
    }

    private func inspectFilesystemEntry(atPath path: String) -> FilesystemEntry {
        var statBuf = stat()
        guard lstat(path, &statBuf) == 0 else {
            return .missing
        }
        guard (statBuf.st_mode & S_IFMT) == S_IFLNK else {
            return .nonSymlink
        }
        guard let rawTarget = try? FileManager.default.destinationOfSymbolicLink(atPath: path) else {
            return .nonSymlink
        }
        let resolved: String
        if rawTarget.hasPrefix("/") {
            resolved = URL(fileURLWithPath: rawTarget).standardizedFileURL.path
        } else {
            resolved = installDirectoryURL
                .appendingPathComponent(rawTarget, isDirectory: false)
                .standardizedFileURL.path
        }
        return .symlink(rawTarget: rawTarget, resolvedTarget: resolved)
    }

    private func isManagedTarget(rawTarget: String, resolvedTarget: String) -> Bool {
        let managedSuffix = "/Contents/Helpers/\(cliName)"
        if rawTarget.hasSuffix(managedSuffix) || resolvedTarget.hasSuffix(managedSuffix) {
            return true
        }
        let canonical = URL(fileURLWithPath: resolvedTarget).resolvingSymlinksInPath().standardizedFileURL.path
        return canonical.hasSuffix(managedSuffix)
    }

    private func pathsReferToSameTarget(_ lhs: String, _ rhs: String) -> Bool {
        if lhs == rhs {
            return true
        }
        let fm = FileManager.default
        if fm.fileExists(atPath: lhs) && fm.fileExists(atPath: rhs) {
            let lhsReal = URL(fileURLWithPath: lhs).resolvingSymlinksInPath().standardizedFileURL.path
            let rhsReal = URL(fileURLWithPath: rhs).resolvingSymlinksInPath().standardizedFileURL.path
            return lhsReal == rhsReal
        }
        return false
    }

    private static func normalizeDirectoryPath(
        _ rawPath: String,
        environment: [String: String]
    ) -> String {
        let expanded: String
        if rawPath == "~" {
            expanded = environment["HOME"] ?? FileManager.default.homeDirectoryForCurrentUser.path
        } else if rawPath.hasPrefix("~/") {
            let home = environment["HOME"] ?? FileManager.default.homeDirectoryForCurrentUser.path
            expanded = home + String(rawPath.dropFirst())
        } else {
            expanded = (rawPath as NSString).expandingTildeInPath
        }
        return URL(fileURLWithPath: expanded).standardizedFileURL.path
    }
}
