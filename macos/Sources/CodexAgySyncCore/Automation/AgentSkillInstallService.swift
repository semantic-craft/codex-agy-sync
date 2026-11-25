import Darwin
import Foundation

public enum AgentSkillDiffLineKind: Equatable, Sendable {
    case context
    case removed
    case added
}

public struct AgentSkillDiffLine: Identifiable, Equatable, Sendable {
    public let id: Int
    public let kind: AgentSkillDiffLineKind
    public let text: String

    public init(id: Int, kind: AgentSkillDiffLineKind, text: String) {
        self.id = id
        self.kind = kind
        self.text = text
    }
}

public struct AgentSkillFileDiff: Identifiable, Equatable, Sendable {
    public var id: String { relativePath }
    public let relativePath: String
    public let existingContent: String?
    public let proposedContent: String
    public let lines: [AgentSkillDiffLine]

    public init(
        relativePath: String,
        existingContent: String?,
        proposedContent: String,
        lines: [AgentSkillDiffLine]
    ) {
        self.relativePath = relativePath
        self.existingContent = existingContent
        self.proposedContent = proposedContent
        self.lines = lines
    }

    public var addedCount: Int {
        lines.reduce(into: 0) { count, line in
            if line.kind == .added { count += 1 }
        }
    }

    public var removedCount: Int {
        lines.reduce(into: 0) { count, line in
            if line.kind == .removed { count += 1 }
        }
    }
}

public struct AgentSkillDiffReview: Identifiable, Equatable, Sendable {
    public let id: UUID
    public let diffs: [AgentSkillFileDiff]

    public init(id: UUID = UUID(), diffs: [AgentSkillFileDiff]) {
        self.id = id
        self.diffs = diffs
    }
}

public enum AgentSkillInstallStatus: Equatable, Sendable {
    case notInstalled
    case installedUpToDate
    case installedBrokenLinks(brokenPaths: [String])
    case updateAvailable(diffs: [AgentSkillFileDiff])
    case externallyManaged(path: String, destination: String)
    case conflictNonManaged(path: String)
}

public struct AgentSkillInstallState: Equatable, Sendable {
    public let status: AgentSkillInstallStatus
    public let canonicalDirectoryPath: String
    public let linkedPaths: [String]

    public init(
        status: AgentSkillInstallStatus,
        canonicalDirectoryPath: String,
        linkedPaths: [String]
    ) {
        self.status = status
        self.canonicalDirectoryPath = canonicalDirectoryPath
        self.linkedPaths = linkedPaths
    }
}

public enum AgentSkillInstallError: Error, Equatable, Sendable {
    case externallyManaged(String)
    case conflictNonManaged(String)
    case filesystemError(String)
}

public struct AgentSkillInstallService: Sendable {
    public let canonicalSkillsRootURL: URL
    public let clientLinkRootURLs: [URL]
    public let skillName: String
    public let renderContext: AgentSkillRenderContext

    public init(
        canonicalSkillsRootURL: URL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".agents", isDirectory: true)
            .appendingPathComponent("skills", isDirectory: true),
        clientLinkRootURLs: [URL] = [
            FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".claude", isDirectory: true)
                .appendingPathComponent("skills", isDirectory: true),
        ],
        skillName: String = "codex-agy-sync",
        renderContext: AgentSkillRenderContext = AgentSkillRenderContext()
    ) {
        self.canonicalSkillsRootURL = canonicalSkillsRootURL
        self.clientLinkRootURLs = clientLinkRootURLs
        self.skillName = skillName
        self.renderContext = renderContext
    }

    public var canonicalDirectoryURL: URL {
        canonicalSkillsRootURL.appendingPathComponent(skillName, isDirectory: true)
    }

    public var clientLinkURLs: [URL] {
        clientLinkRootURLs.map { $0.appendingPathComponent(skillName, isDirectory: false) }
    }

    public func inspect() -> AgentSkillInstallState {
        let canonicalPath = canonicalDirectoryURL.standardizedFileURL.path
        let linkedPaths = clientLinkURLs.map { $0.standardizedFileURL.path }

        let canonicalEntry = inspectPath(canonicalPath)
        switch canonicalEntry {
        case .symlink(let rawDest, _):
            return AgentSkillInstallState(
                status: .externallyManaged(path: canonicalPath, destination: rawDest),
                canonicalDirectoryPath: canonicalPath,
                linkedPaths: linkedPaths
            )
        case .regularFile:
            return AgentSkillInstallState(
                status: .conflictNonManaged(path: canonicalPath),
                canonicalDirectoryPath: canonicalPath,
                linkedPaths: linkedPaths
            )
        case .missing, .directory:
            break
        }

        var brokenPaths: [String] = []
        for linkURL in clientLinkURLs {
            let linkPath = linkURL.standardizedFileURL.path
            switch inspectPath(linkPath) {
            case .missing:
                brokenPaths.append(linkPath)
            case .regularFile, .directory:
                return AgentSkillInstallState(
                    status: .conflictNonManaged(path: linkPath),
                    canonicalDirectoryPath: canonicalPath,
                    linkedPaths: linkedPaths
                )
            case .symlink(let rawDest, let resolvedDest):
                if !isManagedClientLinkDestination(rawDest: rawDest, resolvedDest: resolvedDest, canonicalPath: canonicalPath) {
                    return AgentSkillInstallState(
                        status: .externallyManaged(path: linkPath, destination: rawDest),
                        canonicalDirectoryPath: canonicalPath,
                        linkedPaths: linkedPaths
                    )
                }
                if !pathsMatchCanonical(resolvedDest, canonicalPath) {
                    brokenPaths.append(linkPath)
                }
            }
        }

        if case .missing = canonicalEntry {
            return AgentSkillInstallState(
                status: .notInstalled,
                canonicalDirectoryPath: canonicalPath,
                linkedPaths: linkedPaths
            )
        }

        let expectedFiles = AgentSkillTemplateRenderer.render(context: renderContext)
        var diffs: [AgentSkillFileDiff] = []
        for file in expectedFiles {
            let fileURL = canonicalDirectoryURL.appendingPathComponent(file.relativePath, isDirectory: false)
            let filePath = fileURL.standardizedFileURL.path
            switch inspectPath(filePath) {
            case .directory:
                return AgentSkillInstallState(
                    status: .conflictNonManaged(path: filePath),
                    canonicalDirectoryPath: canonicalPath,
                    linkedPaths: linkedPaths
                )
            case .symlink(let rawDest, _):
                return AgentSkillInstallState(
                    status: .externallyManaged(path: filePath, destination: rawDest),
                    canonicalDirectoryPath: canonicalPath,
                    linkedPaths: linkedPaths
                )
            case .missing:
                diffs.append(
                    Self.makeFileDiff(
                        relativePath: file.relativePath,
                        existingContent: nil,
                        proposedContent: file.content
                    )
                )
            case .regularFile:
                let existing = try? String(contentsOf: fileURL, encoding: .utf8)
                if existing != file.content {
                    diffs.append(
                        Self.makeFileDiff(
                            relativePath: file.relativePath,
                            existingContent: existing,
                            proposedContent: file.content
                        )
                    )
                }
            }
        }

        if !diffs.isEmpty {
            return AgentSkillInstallState(
                status: .updateAvailable(diffs: diffs),
                canonicalDirectoryPath: canonicalPath,
                linkedPaths: linkedPaths
            )
        }

        if !brokenPaths.isEmpty {
            return AgentSkillInstallState(
                status: .installedBrokenLinks(brokenPaths: brokenPaths),
                canonicalDirectoryPath: canonicalPath,
                linkedPaths: linkedPaths
            )
        }

        return AgentSkillInstallState(
            status: .installedUpToDate,
            canonicalDirectoryPath: canonicalPath,
            linkedPaths: linkedPaths
        )
    }

    @discardableResult
    public func installFresh() throws -> AgentSkillInstallState {
        let current = inspect()
        switch current.status {
        case .externallyManaged(let path, _):
            throw AgentSkillInstallError.externallyManaged(path)
        case .conflictNonManaged(let path):
            throw AgentSkillInstallError.conflictNonManaged(path)
        case .updateAvailable:
            return current
        case .installedUpToDate:
            return current
        case .installedBrokenLinks:
            return try repairLinks()
        case .notInstalled:
            try writeRenderedFilesAtomically()
            try ensureClientSymlinks()
            return inspect()
        }
    }

    @discardableResult
    public func applyUpdateAfterDiffReview() throws -> AgentSkillInstallState {
        let current = inspect()
        switch current.status {
        case .externallyManaged(let path, _):
            throw AgentSkillInstallError.externallyManaged(path)
        case .conflictNonManaged(let path):
            throw AgentSkillInstallError.conflictNonManaged(path)
        case .notInstalled, .installedUpToDate, .installedBrokenLinks, .updateAvailable:
            try writeRenderedFilesAtomically()
            try ensureClientSymlinks()
            return inspect()
        }
    }

    @discardableResult
    public func repairLinks() throws -> AgentSkillInstallState {
        let current = inspect()
        switch current.status {
        case .externallyManaged(let path, _):
            throw AgentSkillInstallError.externallyManaged(path)
        case .conflictNonManaged(let path):
            throw AgentSkillInstallError.conflictNonManaged(path)
        case .notInstalled:
            return current
        case .installedUpToDate, .installedBrokenLinks, .updateAvailable:
            try ensureClientSymlinks()
            return inspect()
        }
    }

    @discardableResult
    public func uninstall() throws -> AgentSkillInstallState {
        let current = inspect()
        switch current.status {
        case .externallyManaged(let path, _):
            throw AgentSkillInstallError.externallyManaged(path)
        case .conflictNonManaged(let path):
            throw AgentSkillInstallError.conflictNonManaged(path)
        case .notInstalled, .installedUpToDate, .installedBrokenLinks, .updateAvailable:
            break
        }

        let fm = FileManager.default
        let canonicalPath = canonicalDirectoryURL.standardizedFileURL.path

        for linkURL in clientLinkURLs {
            let linkPath = linkURL.standardizedFileURL.path
            if case .symlink(let rawDest, let resolvedDest) = inspectPath(linkPath),
               isManagedClientLinkDestination(rawDest: rawDest, resolvedDest: resolvedDest, canonicalPath: canonicalPath)
            {
                do {
                    try fm.removeItem(atPath: linkPath)
                } catch {
                    throw AgentSkillInstallError.filesystemError(error.localizedDescription)
                }
            }
        }

        if case .directory = inspectPath(canonicalPath) {
            let expectedFiles = AgentSkillTemplateRenderer.render(context: renderContext)
            var candidateSubdirs = Set<URL>()
            for file in expectedFiles {
                let fileURL = canonicalDirectoryURL.appendingPathComponent(file.relativePath, isDirectory: false)
                let parent = fileURL.deletingLastPathComponent().standardizedFileURL
                if parent.path != canonicalPath {
                    candidateSubdirs.insert(parent)
                }
                if case .regularFile = inspectPath(fileURL.standardizedFileURL.path) {
                    do {
                        try fm.removeItem(at: fileURL)
                    } catch {
                        throw AgentSkillInstallError.filesystemError(error.localizedDescription)
                    }
                }
            }

            for subdir in candidateSubdirs.sorted(by: { $0.path.count > $1.path.count }) {
                removeDirectoryIfEmpty(subdir)
            }
            removeDirectoryIfEmpty(canonicalDirectoryURL)
        }

        return inspect()
    }

    public static func makeFileDiff(
        relativePath: String,
        existingContent: String?,
        proposedContent: String
    ) -> AgentSkillFileDiff {
        let lines = computeLineDiff(existingContent: existingContent, proposedContent: proposedContent)
        return AgentSkillFileDiff(
            relativePath: relativePath,
            existingContent: existingContent,
            proposedContent: proposedContent,
            lines: lines
        )
    }

    public static func computeLineDiff(
        existingContent: String?,
        proposedContent: String
    ) -> [AgentSkillDiffLine] {
        let newLines = splitLines(proposedContent)
        guard let existingContent else {
            return newLines.enumerated().map { idx, text in
                AgentSkillDiffLine(id: idx, kind: .added, text: text)
            }
        }
        let oldLines = splitLines(existingContent)
        if oldLines == newLines {
            return []
        }

        var prefixCount = 0
        let minCount = min(oldLines.count, newLines.count)
        while prefixCount < minCount && oldLines[prefixCount] == newLines[prefixCount] {
            prefixCount += 1
        }

        var suffixCount = 0
        while suffixCount < (minCount - prefixCount)
            && oldLines[oldLines.count - 1 - suffixCount] == newLines[newLines.count - 1 - suffixCount]
        {
            suffixCount += 1
        }

        let oldMiddle = Array(oldLines[prefixCount..<(oldLines.count - suffixCount)])
        let newMiddle = Array(newLines[prefixCount..<(newLines.count - suffixCount)])

        var rawResult: [(AgentSkillDiffLineKind, String)] = []
        for i in 0..<prefixCount {
            rawResult.append((.context, oldLines[i]))
        }

        if oldMiddle.count * newMiddle.count <= 250_000 {
            rawResult.append(contentsOf: lcsDiff(oldMiddle, newMiddle))
        } else {
            for line in oldMiddle {
                rawResult.append((.removed, line))
            }
            for line in newMiddle {
                rawResult.append((.added, line))
            }
        }

        for i in (oldLines.count - suffixCount)..<oldLines.count {
            rawResult.append((.context, oldLines[i]))
        }

        return rawResult.enumerated().map { idx, entry in
            AgentSkillDiffLine(id: idx, kind: entry.0, text: entry.1)
        }
    }

    private static func lcsDiff(
        _ oldLines: [String],
        _ newLines: [String]
    ) -> [(AgentSkillDiffLineKind, String)] {
        let n = oldLines.count
        let m = newLines.count
        var dp = Array(repeating: Array(repeating: 0, count: m + 1), count: n + 1)
        if n > 0 && m > 0 {
            for i in stride(from: n - 1, through: 0, by: -1) {
                for j in stride(from: m - 1, through: 0, by: -1) {
                    if oldLines[i] == newLines[j] {
                        dp[i][j] = dp[i + 1][j + 1] + 1
                    } else {
                        dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
                    }
                }
            }
        }

        var out: [(AgentSkillDiffLineKind, String)] = []
        var i = 0
        var j = 0
        while i < n && j < m {
            if oldLines[i] == newLines[j] {
                out.append((.context, oldLines[i]))
                i += 1
                j += 1
            } else if dp[i + 1][j] >= dp[i][j + 1] {
                out.append((.removed, oldLines[i]))
                i += 1
            } else {
                out.append((.added, newLines[j]))
                j += 1
            }
        }
        while i < n {
            out.append((.removed, oldLines[i]))
            i += 1
        }
        while j < m {
            out.append((.added, newLines[j]))
            j += 1
        }
        return out
    }

    private static func splitLines(_ text: String) -> [String] {
        guard !text.isEmpty else { return [] }
        let normalized = text.replacingOccurrences(of: "\r\n", with: "\n")
        var parts = normalized.components(separatedBy: "\n")
        if normalized.hasSuffix("\n"), parts.last == "" {
            parts.removeLast()
        }
        return parts
    }

    private enum PathKind {
        case missing
        case regularFile
        case directory
        case symlink(rawDest: String, resolvedDest: String)
    }

    private func inspectPath(_ path: String) -> PathKind {
        var statBuf = stat()
        guard lstat(path, &statBuf) == 0 else {
            return .missing
        }
        let fileType = statBuf.st_mode & S_IFMT
        if fileType == S_IFLNK {
            guard let rawDest = try? FileManager.default.destinationOfSymbolicLink(atPath: path) else {
                return .regularFile
            }
            let parentDir = URL(fileURLWithPath: path).deletingLastPathComponent()
            let resolved: String
            if rawDest.hasPrefix("/") {
                resolved = URL(fileURLWithPath: rawDest).standardizedFileURL.path
            } else {
                resolved = parentDir.appendingPathComponent(rawDest).standardizedFileURL.path
            }
            return .symlink(rawDest: rawDest, resolvedDest: resolved)
        }
        if fileType == S_IFDIR {
            return .directory
        }
        return .regularFile
    }

    private func isManagedClientLinkDestination(
        rawDest: String,
        resolvedDest: String,
        canonicalPath: String
    ) -> Bool {
        if pathsMatchCanonical(resolvedDest, canonicalPath) {
            return true
        }
        let canonicalSuffix = "/.agents/skills/\(skillName)"
        if rawDest.hasSuffix(canonicalSuffix) || resolvedDest.hasSuffix(canonicalSuffix) {
            return true
        }
        return false
    }

    private func pathsMatchCanonical(_ candidatePath: String, _ canonicalPath: String) -> Bool {
        if candidatePath == canonicalPath {
            return true
        }
        let fm = FileManager.default
        if fm.fileExists(atPath: candidatePath) && fm.fileExists(atPath: canonicalPath) {
            let lhsReal = URL(fileURLWithPath: candidatePath).resolvingSymlinksInPath().standardizedFileURL.path
            let rhsReal = URL(fileURLWithPath: canonicalPath).resolvingSymlinksInPath().standardizedFileURL.path
            return lhsReal == rhsReal
        }
        return false
    }

    private func writeRenderedFilesAtomically() throws {
        let fm = FileManager.default
        let files = AgentSkillTemplateRenderer.render(context: renderContext)
        do {
            try fm.createDirectory(at: canonicalDirectoryURL, withIntermediateDirectories: true)
        } catch {
            throw AgentSkillInstallError.filesystemError(error.localizedDescription)
        }

        for file in files {
            let destURL = canonicalDirectoryURL.appendingPathComponent(file.relativePath, isDirectory: false)
            let parentURL = destURL.deletingLastPathComponent()
            do {
                try fm.createDirectory(at: parentURL, withIntermediateDirectories: true)
            } catch {
                throw AgentSkillInstallError.filesystemError(error.localizedDescription)
            }
            let tempURL = parentURL.appendingPathComponent(
                ".\(destURL.lastPathComponent).tmp.\(UUID().uuidString)",
                isDirectory: false
            )
            guard let data = file.content.data(using: .utf8) else {
                throw AgentSkillInstallError.filesystemError("Unable to encode \(file.relativePath) as UTF-8")
            }
            do {
                try data.write(to: tempURL, options: [.atomic])
            } catch {
                try? fm.removeItem(at: tempURL)
                throw AgentSkillInstallError.filesystemError(error.localizedDescription)
            }
            if rename(tempURL.path, destURL.path) != 0 {
                let code = errno
                try? fm.removeItem(at: tempURL)
                throw AgentSkillInstallError.filesystemError(String(cString: strerror(code)))
            }
        }
    }

    private func ensureClientSymlinks() throws {
        let fm = FileManager.default
        let canonicalPath = canonicalDirectoryURL.standardizedFileURL.path
        for linkURL in clientLinkURLs {
            let linkPath = linkURL.standardizedFileURL.path
            let parentURL = linkURL.deletingLastPathComponent()
            do {
                try fm.createDirectory(at: parentURL, withIntermediateDirectories: true)
            } catch {
                throw AgentSkillInstallError.filesystemError(error.localizedDescription)
            }
            let tempLinkURL = parentURL.appendingPathComponent(
                ".\(skillName).tmp.\(UUID().uuidString)",
                isDirectory: false
            )
            do {
                try fm.createSymbolicLink(atPath: tempLinkURL.path, withDestinationPath: canonicalPath)
            } catch {
                throw AgentSkillInstallError.filesystemError(error.localizedDescription)
            }
            if rename(tempLinkURL.path, linkPath) != 0 {
                let code = errno
                try? fm.removeItem(atPath: tempLinkURL.path)
                throw AgentSkillInstallError.filesystemError(String(cString: strerror(code)))
            }
        }
    }

    private func removeDirectoryIfEmpty(_ url: URL) {
        let fm = FileManager.default
        guard let contents = try? fm.contentsOfDirectory(atPath: url.path), contents.isEmpty else {
            return
        }
        try? fm.removeItem(at: url)
    }
}
