// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "CodexAgyHandoff",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(
            name: "CodexAgyHandoffCore",
            targets: ["CodexAgyHandoffCore"]
        ),
        .executable(
            name: "CodexAgyHandoff",
            targets: ["CodexAgyHandoff"]
        ),
    ],
    targets: [
        .target(
            name: "CodexAgyHandoffCore"
        ),
        .executableTarget(
            name: "CodexAgyHandoff",
            dependencies: ["CodexAgyHandoffCore"],
            linkerSettings: [
                .linkedFramework("Carbon"),
            ]
        ),
        .testTarget(
            name: "CodexAgyHandoffTests",
            dependencies: ["CodexAgyHandoffCore"]
        ),
    ]
)
