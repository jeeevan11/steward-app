// swift-tools-version:5.9
import PackageDescription

// Builds the menu-bar executable. `build_app.sh` wraps the product into a proper
// .app bundle (LSUIElement, no Dock icon). macOS 13+ for MenuBarExtra.
let package = Package(
    name: "CosBar",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "CosBar", path: "Sources/CosBar")
    ]
)
