import Foundation

public struct SemanticVersion: Comparable, Equatable, Sendable, CustomStringConvertible {
    public let major: Int
    public let minor: Int
    public let patch: Int
    public let prerelease: String?

    public var description: String {
        if let prerelease, !prerelease.isEmpty {
            return "\(major).\(minor).\(patch)-\(prerelease)"
        }
        return "\(major).\(minor).\(patch)"
    }

    public init(major: Int, minor: Int, patch: Int, prerelease: String? = nil) {
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = prerelease
    }

    private static let versionRegex = try! NSRegularExpression(
        pattern: #"(?:^|[^0-9])v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z.-]+))?"#
    )

    public static func parse(_ raw: String) -> SemanticVersion? {
        let nsRange = NSRange(raw.startIndex..<raw.endIndex, in: raw)
        guard let match = versionRegex.firstMatch(in: raw, range: nsRange),
              let r1 = Range(match.range(at: 1), in: raw),
              let r2 = Range(match.range(at: 2), in: raw),
              let r3 = Range(match.range(at: 3), in: raw),
              let major = Int(raw[r1]),
              let minor = Int(raw[r2]),
              let patch = Int(raw[r3])
        else {
            return nil
        }
        var pre: String?
        if match.range(at: 4).location != NSNotFound,
           let r4 = Range(match.range(at: 4), in: raw)
        {
            pre = String(raw[r4])
        }
        return SemanticVersion(major: major, minor: minor, patch: patch, prerelease: pre)
    }

    public static func < (lhs: SemanticVersion, rhs: SemanticVersion) -> Bool {
        if lhs.major != rhs.major { return lhs.major < rhs.major }
        if lhs.minor != rhs.minor { return lhs.minor < rhs.minor }
        if lhs.patch != rhs.patch { return lhs.patch < rhs.patch }
        switch (lhs.prerelease, rhs.prerelease) {
        case (nil, nil):
            return false
        case (.some, nil):
            return true
        case (nil, .some):
            return false
        case (.some(let lp), .some(let rp)):
            return lp < rp
        }
    }
}
