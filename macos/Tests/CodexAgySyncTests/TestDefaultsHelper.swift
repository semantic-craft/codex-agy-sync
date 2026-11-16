import CoreFoundation
import Foundation

func cleanupIsolatedTestDefaults(_ defaults: UserDefaults, suiteName: String) {
    defaults.removePersistentDomain(forName: suiteName)
    if let anyKeys = CFPreferencesCopyKeyList(suiteName as CFString, kCFPreferencesCurrentUser, kCFPreferencesAnyHost) {
        CFPreferencesSetMultiple(nil, anyKeys, suiteName as CFString, kCFPreferencesCurrentUser, kCFPreferencesAnyHost)
    }
    if let hostKeys = CFPreferencesCopyKeyList(suiteName as CFString, kCFPreferencesCurrentUser, kCFPreferencesCurrentHost) {
        CFPreferencesSetMultiple(nil, hostKeys, suiteName as CFString, kCFPreferencesCurrentUser, kCFPreferencesCurrentHost)
    }
    _ = CFPreferencesSynchronize(suiteName as CFString, kCFPreferencesCurrentUser, kCFPreferencesAnyHost)
    _ = CFPreferencesAppSynchronize(suiteName as CFString)
    let plistURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Preferences", isDirectory: true)
        .appendingPathComponent("\(suiteName).plist", isDirectory: false)
    try? FileManager.default.removeItem(at: plistURL)
}
