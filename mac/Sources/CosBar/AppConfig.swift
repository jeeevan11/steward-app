import Foundation
import Combine

/// Owns where the engine lives and reads/writes its `.env`. The Mac app is a thin,
/// seamless shell over the existing Python engine — it never duplicates logic, it just
/// configures and supervises it.
final class AppConfig: ObservableObject {
    static let shared = AppConfig()

    /// Absolute path to the repo root (contains run.py, .venv, relay/, data/).
    @Published var repoPath: String {
        didSet { UserDefaults.standard.set(repoPath, forKey: "repoPath") }
    }
    /// Dashboard URL the menu opens (the backend serves the built UI here).
    @Published var dashboardURL: String {
        didSet { UserDefaults.standard.set(dashboardURL, forKey: "dashboardURL") }
    }

    private init() {
        let defaults = UserDefaults.standard
        self.repoPath = defaults.string(forKey: "repoPath")
            ?? (NSHomeDirectory() + "/Desktop/JatinDhull")
        self.dashboardURL = defaults.string(forKey: "dashboardURL")
            ?? "http://127.0.0.1:8000"
    }

    // MARK: - Derived paths
    var pythonPath: String { repoPath + "/.venv/bin/python" }
    var runPy: String { repoPath + "/run.py" }
    var runWebPy: String { repoPath + "/run_web.py" }
    var relayDir: String { repoPath + "/relay" }
    var relayScript: String { repoPath + "/relay/whatsapp_relay.js" }
    var envPath: String { repoPath + "/.env" }
    var statusFile: String { repoPath + "/data/status.json" }
    var relayStatusFile: String { repoPath + "/relay/status.json" }

    var repoLooksValid: Bool {
        FileManager.default.fileExists(atPath: runPy)
            && FileManager.default.fileExists(atPath: pythonPath)
    }

    // MARK: - .env read/write (minimal, comment-preserving)
    private func envLines() -> [String] {
        (try? String(contentsOfFile: envPath, encoding: .utf8))?
            .components(separatedBy: "\n") ?? []
    }

    func envValue(_ key: String) -> String? {
        for raw in envLines() {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            guard let eq = line.firstIndex(of: "=") else { continue }
            if String(line[..<eq]).trimmingCharacters(in: .whitespaces) == key {
                return String(line[line.index(after: eq)...])
                    .trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            }
        }
        return nil
    }

    func envBool(_ key: String, default def: Bool) -> Bool {
        guard let v = envValue(key)?.lowercased() else { return def }
        return ["1", "true", "yes", "on"].contains(v)
    }

    /// Set (or insert) a key in `.env`, preserving every other line.
    @discardableResult
    func setEnv(_ key: String, _ value: String) -> Bool {
        var lines = envLines()
        var found = false
        for i in lines.indices {
            let line = lines[i].trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("#") || line.isEmpty { continue }
            guard let eq = line.firstIndex(of: "=") else { continue }
            if String(line[..<eq]).trimmingCharacters(in: .whitespaces) == key {
                lines[i] = "\(key)=\(value)"
                found = true
                break
            }
        }
        if !found { lines.append("\(key)=\(value)") }
        do {
            try lines.joined(separator: "\n").write(toFile: envPath, atomically: true, encoding: .utf8)
            return true
        } catch { return false }
    }

    func setEnvBool(_ key: String, _ on: Bool) { setEnv(key, on ? "true" : "false") }

    // MARK: - Onboarding state
    /// The minimum needed to actually run: LLM key + Telegram delivery.
    var onboardingComplete: Bool {
        !(envValue("OPENROUTER_API_KEY") ?? "").isEmpty
            && !(envValue("TELEGRAM_BOT_TOKEN") ?? "").isEmpty
            && !(envValue("TELEGRAM_CHAT_ID") ?? "").isEmpty
    }

    // Channel toggles read live from .env.
    var emailEnabled: Bool { envBool("EMAIL_ENABLED", default: true) }
    var whatsappEnabled: Bool { envBool("WHATSAPP_ENABLED", default: false) }
}
