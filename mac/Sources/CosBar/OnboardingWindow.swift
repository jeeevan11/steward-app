import AppKit
import SwiftUI

/// A directly-managed window for onboarding/settings. Avoids SwiftUI window-id plumbing
/// (unreliable for a menu-bar-only app) so it works the same on first run and from the
/// menu. Hosts the SwiftUI `OnboardingView`.
@MainActor
final class OnboardingWindow {
    static let shared = OnboardingWindow()
    private var window: NSWindow?

    func show() {
        if window == nil {
            let w = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 460, height: 560),
                styleMask: [.titled, .closable, .miniaturizable],
                backing: .buffered, defer: false
            )
            w.title = "Steward Setup"
            w.contentViewController = NSHostingController(rootView: OnboardingView())
            w.isReleasedWhenClosed = false
            w.center()
            window = w
        }
        NSApp.setActivationPolicy(.regular)        // show in Dock while a window is open
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}
