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
