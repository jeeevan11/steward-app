import SwiftUI
import AppKit

/// Menu-bar-only app (the "top-bar widget"). No Dock icon by default (LSUIElement +
/// .accessory policy). Auto-starts the engine on launch when setup is complete, so after
/// onboarding the owner literally never has to open it.
@main
struct CosBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            BriefingView()
        } label: {
            StewardBarLabel()
        }
        .menuBarExtraStyle(.window)
    }
}

/// The menu-bar mark: the significance glyph, never a number.
struct StewardBarLabel: View {
    @ObservedObject var store = StewardStore.shared
    var body: some View {
        Image(nsImage: Glyph.barImage(store.significance))
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)   // menu-bar only, no Dock icon
        EngineController.shared.beginMonitoring()
        StewardStore.shared.begin()             // significance + decisions/people/commitments
        let cfg = AppConfig.shared
        if cfg.onboardingComplete && cfg.repoLooksValid {
            EngineController.shared.startAll()    // it's an agent — be on by default
        } else {
            OnboardingWindow.shared.show()        // first run: finish setup in one sitting
        }
    }

    /// Deep link from the widget (steward://open) → open the Steward dashboard.
    func application(_ application: NSApplication, open urls: [URL]) {
        if urls.contains(where: { $0.scheme == "steward" }) {
            StewardWindow.shared.show()
        }
    }
}
