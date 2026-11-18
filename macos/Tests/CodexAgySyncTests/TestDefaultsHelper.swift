import Foundation

struct IsolatedTestDefaults {
    let defaults: UserDefaults
    let suiteName: String
    let containerURL: URL

    func cleanup() {
        defaults.removePersistentDomain(forName: suiteName)
        try? FileManager.default.removeItem(at: containerURL)
    }
}

func makeIsolatedTestDefaultsSuite(label: String = "tests") -> IsolatedTestDefaults {
    let repoRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let containerURL = repoRoot
        .appendingPathComponent("out", isDirectory: true)
        .appendingPathComponent("test-prefs-sandboxes", isDirectory: true)
        .appendingPathComponent("\(label)-\(UUID().uuidString)", isDirectory: true)
    let nestedDir = containerURL.appendingPathComponent("prefs", isDirectory: true)
    try? FileManager.default.createDirectory(at: nestedDir, withIntermediateDirectories: true)
    let suitePath = nestedDir.appendingPathComponent("suite", isDirectory: false).path
    guard let defaults = UserDefaults(suiteName: suitePath) else {
        fatalError("Failed to create sandbox UserDefaults at \(suitePath)")
    }
    return IsolatedTestDefaults(defaults: defaults, suiteName: suitePath, containerURL: containerURL)
}

func makeIsolatedTestAppSandbox(label: String = "app") throws -> URL {
    let containerURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Projects", isDirectory: true)
        .appendingPathComponent("_artifacts", isDirectory: true)
        .appendingPathComponent("codex-agy-sync", isDirectory: true)
        .appendingPathComponent("test-app-sandboxes", isDirectory: true)
        .appendingPathComponent("\(label)-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: containerURL, withIntermediateDirectories: true)
    return containerURL
}

func unregisterLaunchServicesApp(atPath appPath: String) {
    let fm = FileManager.default
    let lsregister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    guard fm.isExecutableFile(atPath: lsregister) else { return }
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: lsregister)
    proc.arguments = ["-u", appPath]
    proc.standardOutput = FileHandle.nullDevice
    proc.standardError = FileHandle.nullDevice
    try? proc.run()
    proc.waitUntilExit()
}

func cleanupIsolatedTestAppSandbox(_ root: URL, extraUnregisterPaths: [URL] = []) {
    let fm = FileManager.default
    var appPaths: Set<String> = Set(extraUnregisterPaths.map(\.path))
    if let enumerator = fm.enumerator(
        at: root,
        includingPropertiesForKeys: [.isSymbolicLinkKey],
        options: [.skipsPackageDescendants]
    ) {
        for case let url as URL in enumerator where url.pathExtension == "app" {
            let isSymlink = (try? url.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) ?? false
            if !isSymlink {
                appPaths.insert(url.path)
            }
        }
    }
    for appPath in appPaths.sorted(by: { $0.count > $1.count }) {
        unregisterLaunchServicesApp(atPath: appPath)
    }
    try? fm.removeItem(at: root)
}
