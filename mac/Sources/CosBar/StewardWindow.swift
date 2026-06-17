import AppKit
import SwiftUI

/// Hosts the Steward app window (the calm home for review/reflection). Directly managed
/// NSWindow (reliable for a menu-bar-only app), same pattern as OnboardingWindow.
@MainActor
final class StewardWindow: NSObject, NSWindowDelegate {
    static let shared = StewardWindow()
    private var window: NSWindow?

    func show(tab: StewardTab? = nil) {
        if let tab { StewardStore.shared.tab = tab; StewardStore.shared.focused = nil }
        StewardStore.shared.refresh()
        if window == nil {
            let w = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 820, height: 660),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered, defer: false
            )
            w.title = "Steward"
            w.titlebarAppearsTransparent = true
            w.backgroundColor = NSColor(hex: 0x1B1A18)
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.contentViewController = NSHostingController(rootView: StewardRootView())
            w.center()
            window = w
        }
        NSApp.setActivationPolicy(.regular)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// HIG: an accessory (menu-bar-only) app must not keep a Dock icon after its last
    /// window closes. We flip to .regular only while a window is open (so the app behaves
    /// like a real app with a menu + Dock presence); on close we drop back to .accessory
    /// once no other Steward window is still visible.
    func windowWillClose(_ notification: Notification) {
        DispatchQueue.main.async {
            let stillOpen = NSApp.windows.contains {
                $0.isVisible && $0 !== self.window && !($0 is NSPanel)
            }
            if !stillOpen { NSApp.setActivationPolicy(.accessory) }
        }
    }
}
