import Foundation
import ServiceManagement

/// Launch-at-login via SMAppService (macOS 13+). The app registers itself so the agent
/// is always there after a reboot — the owner never has to open anything.
enum LoginItem {
    static var isEnabled: Bool {
        if #available(macOS 13.0, *) { return SMAppService.mainApp.status == .enabled }
        return false
    }

    static func setEnabled(_ on: Bool) {
        guard #available(macOS 13.0, *) else { return }
        do {
            if on { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
        } catch {
            NSLog("LoginItem toggle failed: \(error.localizedDescription)")
        }
    }
}
