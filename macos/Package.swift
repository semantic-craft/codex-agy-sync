// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "CodexAgySync",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(
            name: "CodexAgySyncCore",
            targets: ["CodexAgySyncCore"]
        ),
        .executable(
            name: "CodexAgySync",
            targets: ["CodexAgySync"]
        ),
    ],
    targets: [
        .target(
            name: "CodexAgySyncCore",
            linkerSettings: [
                .linkedFramework("Carbon"),
            ]
        ),
        .executableTarget(
            name: "CodexAgySync",
            dependencies: ["CodexAgySyncCore"],
            linkerSettings: [
                .linkedFramework("Carbon"),
            ]
        ),
        .testTarget(
            name: "CodexAgySyncTests",
            dependencies: ["CodexAgySyncCore"]
        ),
    ]
)
