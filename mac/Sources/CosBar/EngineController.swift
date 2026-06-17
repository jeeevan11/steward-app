import Foundation
import AppKit
import Combine

/// Supervises the three local processes that make up the assistant:
///   * the engine        (`.venv/bin/python run.py`)
///   * the web dashboard (`.venv/bin/python run_web.py`, serves http://127.0.0.1:8000)
///   * the WhatsApp relay (`node relay/whatsapp_relay.js`, only if WhatsApp is on)
///
/// "Agent on/off" = start/stop these. Per-channel toggles edit `.env` and restart so
/// the change takes effect. Everything is best-effort and logged; a failed launch never
/// crashes the menu bar.
@MainActor
final class EngineController: ObservableObject {
    static let shared = EngineController()

    @Published var running = false
    @Published var status = EngineStatus()
    @Published var relay = RelayStatus()
    @Published var lastError: String = ""

    private var engine: Process?
    private var web: Process?
    private var relayProc: Process?
    private var pollTimer: Timer?

    private let cfg = AppConfig.shared

    private init() {}

    // MARK: - lifecycle
    func beginMonitoring() {
        pollTimer = Timer.scheduledTimer(withTimeInterval: 4.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshStatus() }
        }
        refreshStatus()
    }

    /// Start the whole stack. ADOPTS anything already running (fresh heartbeat / port in
    /// use) instead of launching a duplicate — so it coexists with processes started
    /// elsewhere and never spawns a second copy that just exits.
    func startAll() {
        guard cfg.repoLooksValid else {
            lastError = "Repo not found at \(cfg.repoPath). Open Settings to set it."
            return
        }
        refreshStatus()
        // Engine: a fresh heartbeat means one is already running → adopt it.
        let engineAlive = (engine?.isRunning ?? false) || status.isFresh
        if !engineAlive {
            engine = launch(cfg.pythonPath, [cfg.runPy], cwd: cfg.repoPath, label: "engine")
        }
        // Dashboard backend (:8000).
        if !(web?.isRunning ?? false) && !PortCheck.inUse(8000) {
            web = launch(cfg.pythonPath, [cfg.runWebPy], cwd: cfg.repoPath, label: "web")
        }
        // WhatsApp relay (:7998) — only when WhatsApp is on.
        if cfg.whatsappEnabled, let node = Self.nodePath() {
            if !(relayProc?.isRunning ?? false) && !PortCheck.inUse(7998) {
                relayProc = launch(node, [cfg.relayScript], cwd: cfg.relayDir, label: "relay")
            }
        }
        running = true
        refreshStatus()
    }

    func stopAll() {
        for p in [engine, web, relayProc] { p?.terminate() }
        engine = nil; web = nil; relayProc = nil
        running = false
        refreshStatus()
    }

    func restart() {
        stopAll()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.startAll() }
    }

    // MARK: - toggles (edit .env, then restart so it takes effect)
    func setEmail(_ on: Bool) { cfg.setEnvBool("EMAIL_ENABLED", on); objectWillChange.send(); if running { restart() } }
    func setWhatsApp(_ on: Bool) { cfg.setEnvBool("WHATSAPP_ENABLED", on); objectWillChange.send(); if running { restart() } }

    func openDashboard() {
        // Everything is native now — open the in-app Steward screens (no webview, no Safari).
        StewardWindow.shared.show()
    }

    // MARK: - status
    func refreshStatus() {
        if let s = EngineStatus.read(cfg.statusFile) { status = s }
        if let r = RelayStatus.read(cfg.relayStatusFile) { relay = r }
        // "Running" reflects reality: a process we own OR a fresh heartbeat (adopted).
        let alive = (engine?.isRunning ?? false) || status.isFresh
        if alive != running { running = alive }
    }

    var healthy: Bool { running && status.isFresh }
    var pending: Int { status.pending }
    var handled24h: Int { status.handled24h }

    // MARK: - process launching
    private func launch(_ exe: String, _ args: [String], cwd: String, label: String) -> Process? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.currentDirectoryURL = URL(fileURLWithPath: cwd)
        var env = ProcessInfo.processInfo.environment
        // GUI/launchd apps get a minimal PATH; make sure node/python tooling resolves.
        let extra = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        env["PATH"] = extra + ":" + (env["PATH"] ?? "")
        p.environment = env
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in
                self?.lastError = "\(label) exited (code \(proc.terminationStatus))"
                self?.refreshStatus()
            }
        }
        do { try p.run() } catch {
            lastError = "Could not start \(label): \(error.localizedDescription)"
            return nil
        }
        return p
    }

    /// Resolve `node` for a GUI app (no shell PATH). Probe the usual install spots.
    static func nodePath() -> String? {
        for c in ["/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node"] {
            if FileManager.default.isExecutableFile(atPath: c) { return c }
        }
        return nil
    }
}
