import SwiftUI
import AppKit

/// One-time setup. Collects the essentials, writes them to `.env`, and hands off the
/// two interactive flows (Gmail OAuth, WhatsApp QR) to Terminal so the owner does it
/// once and never again.
struct OnboardingView: View {
    @ObservedObject var cfg = AppConfig.shared
    @ObservedObject var engine = EngineController.shared

    @State private var openRouter = ""
    @State private var tgToken = ""
    @State private var tgChat = ""
    @State private var liveMode = false
    @State private var saved = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Set up Steward").font(.title2).bold()
                Text("Do this once. After that it just runs.")
                    .font(.subheadline).foregroundColor(.secondary)

                group("Where the engine lives") {
                    HStack {
                        TextField("Repo path", text: $cfg.repoPath)
                            .textFieldStyle(.roundedBorder)
                        Button("Browse…", action: browse)
                    }
                    Label(cfg.repoLooksValid ? "Found run.py and the virtualenv"
                                             : "Can't find run.py / .venv here",
                          systemImage: cfg.repoLooksValid ? "checkmark.circle" : "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundColor(cfg.repoLooksValid ? .green : .orange)
                }

                group("Brain + notifications") {
                    SecureField("OpenRouter API key", text: $openRouter).textFieldStyle(.roundedBorder)
                    TextField("Telegram bot token", text: $tgToken).textFieldStyle(.roundedBorder)
                    TextField("Telegram chat id", text: $tgChat).textFieldStyle(.roundedBorder)
                    Toggle("Live mode (send/act for real — off = dry-run)", isOn: $liveMode)
                    Button("Save", action: save).keyboardShortcut(.defaultAction)
                    if saved { Text("Saved to .env").font(.caption).foregroundColor(.green) }
                }

                group("Connect your accounts (opens Terminal)") {
                    Button { runInTerminal("\"\(cfg.pythonPath)\" \"\(cfg.runPy)\" --onboard") } label: {
                        Label("Connect Gmail (sign in)", systemImage: "envelope.badge")
                    }
                    Button { linkWhatsApp() } label: {
                        Label("Link WhatsApp (scan QR)", systemImage: "qrcode")
                    }
                    Text("Gmail opens a Google sign-in. WhatsApp shows a QR in Terminal — "
                         + "open WhatsApp ▸ Linked devices ▸ Link a device.")
                        .font(.caption).foregroundColor(.secondary)
                }

                HStack {
                    Image(systemName: cfg.onboardingComplete ? "checkmark.seal.fill" : "circle.dashed")
                        .foregroundColor(cfg.onboardingComplete ? .green : .secondary)
                    Text(cfg.onboardingComplete ? "Ready to run." : "Add the key + Telegram details to finish.")
                        .font(.callout)
                    Spacer()
                    Button("Start the agent") { engine.startAll() }
                        .disabled(!cfg.onboardingComplete)
                }
            }
            .padding(22)
        }
        .frame(width: 460, height: 560)
        .onAppear {
            openRouter = cfg.envValue("OPENROUTER_API_KEY") ?? ""
            tgToken = cfg.envValue("TELEGRAM_BOT_TOKEN") ?? ""
            tgChat = cfg.envValue("TELEGRAM_CHAT_ID") ?? ""
            liveMode = (cfg.envValue("MODE") ?? "dry_run").lowercased() == "live"
        }
    }

    @ViewBuilder private func group(_ title: String, @ViewBuilder _ content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.headline)
            content()
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.secondary.opacity(0.08)))
    }

    private func save() {
        if !openRouter.isEmpty { cfg.setEnv("OPENROUTER_API_KEY", openRouter) }
        if !tgToken.isEmpty { cfg.setEnv("TELEGRAM_BOT_TOKEN", tgToken) }
        if !tgChat.isEmpty { cfg.setEnv("TELEGRAM_CHAT_ID", tgChat) }
        cfg.setEnv("MODE", liveMode ? "live" : "dry_run")
        saved = true
        cfg.objectWillChange.send()
    }

    private func browse() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url { cfg.repoPath = url.path }
    }

    private func linkWhatsApp() {
        cfg.setEnvBool("WHATSAPP_ENABLED", true)
        cfg.objectWillChange.send()
        if let node = EngineController.nodePath() {
            runInTerminal("cd \"\(cfg.relayDir)\" && \"\(node)\" whatsapp_relay.js")
        } else {
            runInTerminal("cd \"\(cfg.relayDir)\" && node whatsapp_relay.js")
        }
    }

    /// Run a command in a new Terminal window so the user can see OAuth / the QR code.
    private func runInTerminal(_ command: String) {
        let escaped = command.replacingOccurrences(of: "\\", with: "\\\\")
                             .replacingOccurrences(of: "\"", with: "\\\"")
        let script = "tell application \"Terminal\"\nactivate\ndo script \"\(escaped)\"\nend tell"
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        try? p.run()
    }
}
