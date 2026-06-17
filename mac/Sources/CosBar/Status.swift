import Foundation

/// The engine heartbeat (data/status.json, written by the Python poller each pass).
struct EngineStatus: Decodable {
    var mode: String = "dry_run"
    var dry_run: Bool = true
    var paused: Bool = false
    var email_enabled: Bool = true
    var whatsapp_enabled: Bool = false
    var pending: Int = 0
    var last_24h: [String: Int] = [:]
    var heartbeat_ts: Int = 0

    /// Items the assistant has quietly handled in the last 24h (the "it's working for
    /// me" number) — every ledger row that reached DONE.
    var handled24h: Int { last_24h["DONE"] ?? 0 }

    static func read(_ path: String) -> EngineStatus? {
        guard let data = FileManager.default.contents(atPath: path) else { return nil }
        return try? JSONDecoder().decode(EngineStatus.self, from: data)
    }

    /// Fresh if the engine wrote within the last ~2 minutes.
    var isFresh: Bool {
        heartbeat_ts > 0 && (Int(Date().timeIntervalSince1970) - heartbeat_ts) < 120
    }
}

/// The relay heartbeat (relay/status.json, written by the Node relay).
struct RelayStatus: Decodable {
    var connected: Bool = false
    var messages_today: Int = 0
    var last_message_ts: Int = 0

    static func read(_ path: String) -> RelayStatus? {
        guard let data = FileManager.default.contents(atPath: path) else { return nil }
        return try? JSONDecoder().decode(RelayStatus.self, from: data)
    }
}
