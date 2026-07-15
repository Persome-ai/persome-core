// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PersomeAppleHealth",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "PersomeAppleHealth", targets: ["PersomeAppleHealth"]),
        .executable(name: "persome-health-relay", targets: ["PersomeHealthRelay"]),
    ],
    targets: [
        .target(name: "PersomeAppleHealth"),
        .executableTarget(
            name: "PersomeHealthRelay",
            dependencies: ["PersomeAppleHealth"]
        ),
        .testTarget(name: "PersomeAppleHealthTests", dependencies: ["PersomeAppleHealth"]),
    ]
)
