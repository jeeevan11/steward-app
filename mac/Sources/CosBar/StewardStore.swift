import Foundation
import Combine
import SwiftUI
import Contacts

/// The redesigned data layer. Reads DECISIONS / PEOPLE / COMMITMENTS from the running
/// engine's local API and exposes "significance" — never counts. Best-effort: on any
/// failure it degrades to calm/empty, never errors.

struct Decision: Identifiable, Decodable, Equatable {
    let id: Int
    let message_id: String
    let tier: Int
    let title: String
    let sentence: String
    let kind: String
    let draft: String
    let context: String
    let sender: String
    var quote: String = ""
    var category: String = "Other"
    var channel: String = "Email"
    // Recognition + raw identifier, so an unknown sender can be saved straight from the card.
    // Tolerant of an older server (defaults: known sender, no identifier).
    var sender_identifier: String = ""
    var is_saved: Bool = true

    /// ux-trust-2: kinds that are NOT a reply we'd send. A "reminder" is a proactive nudge
    /// (created by the relationship-reminder sweep) with no draft and a message_id that is a
    /// raw chat id — approving it would call the send path, silently no-op, and falsely imply
    /// a reply went out. Such a card must offer "Done" (acknowledge), never a send-shaped
    /// Approve. Any future non-reply kinds can be added here.
    private static let nonSendKinds: Set<String> = ["reminder"]

    /// True when this card represents a reply Steward could actually send on approval.
    /// A non-send kind (e.g. reminder) is never sendable; a card with no draft has nothing
    /// to send yet (you open it to write one first).
    var isSendable: Bool { !Decision.nonSendKinds.contains(kind) && !draft.isEmpty }

    /// True for proactive reminders / other non-reply cards — these acknowledge, not send.
    var isReminder: Bool { Decision.nonSendKinds.contains(kind) }
}

struct Handle: Decodable, Equatable, Hashable {
    let id: String          // the raw identifier (@lid / @s.whatsapp.net / email)
    var kind: String = ""   // "whatsapp" | "phone" | "email" | "other"
    var label: String = ""  // human-readable: the number, the email, or "WhatsApp"
}

struct Person: Identifiable, Decodable, Equatable {
    // One row per PERSON: prefer the cross-channel person_id so a saved person is stable
    // even as identifiers merge; fall back to email for an older server / unlinked contact.
    var id: String { person_id.isEmpty ? email : person_id }
    let name: String
    let email: String
    let relationship: String
    let importance: Int
    let is_vip: Bool
    var flags: [String] = []
    // Recognition: false → an unsaved/unknown sender the owner can name + save in-app.
    // Tolerant of an older server that doesn't send these (defaults to "saved/known").
    var is_saved: Bool = true
    var name_source: String = ""
    var person_id: String = ""
    var handles: [Handle] = []   // every number/email/WhatsApp id grouped under this person
}

struct Commitment: Identifiable, Decodable, Equatable {
    let id: String
    let to: String
    let promise: String
    let due_date: String
    // Enriched by /api/commitments (optional → tolerant of an older server).
    var person: String? = nil
    var status: String? = nil
    var created_at: Int? = nil

    /// Parsed due date (ISO yyyy-MM-dd), or nil when there's no date.
    var due: Date? {
        guard !due_date.isEmpty else { return nil }
        return Commitment.iso.date(from: due_date)
    }
    static let iso: DateFormatter = {
        let f = DateFormatter(); f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd"; return f
    }()
}

/// A queue item that has already been handled (reply sent, filed, auto-handled) — the
/// "what Steward did" history the Today screen shows beneath the open decisions.
struct HandledItem: Identifiable, Decodable, Equatable {
    let message_id: String
    let sender: String
    let label: String
    let subject: String?
    let via_label: String?
    let response_via: String?
    let at: Double?
    let channel_label: String?
    var id: String { message_id }
}

/// Engine-wide activity numbers (from /api/trust) — the full analytical bundle.
struct TrustMetrics {
    var hoursSaved = 0
    var savedMinutes = 0
    var autoHandled = 0
    var processed = 0
    var escalated = 0
    var suppressed = 0
    var approvals = 0
    var approvalRate = 0.0
    var draftAcceptance = 0.0
    var decisionsAvoided = 0
    var memoryUpdates = 0
    var commitmentsOpen = 0
}

/// The full read-only detail of an already-handled item (from /api/email/{id}) — what
/// arrived, what the AI figured out, and the reply that was sent. Rendered natively.
struct HandledDetail: Equatable {
    var messageId = ""
    var sender = ""
    var subject = ""
    var quote = ""
    var why = ""
    var category = ""
    var channel = ""
    var reply = ""
    var viaLabel = ""
    var label = ""
    // "What the AI figured out" chips
    var who = ""
    var urgency = ""
    var undo = ""
    var confidence = ""
}

/// A learned/standing rule (from /api/rules + /api/rules/proposed).
struct Rule: Identifiable, Decodable, Equatable {
    let id: Int
    let rule: String
    var status: String? = nil
    var learned: Bool? = nil
    var needs_confirm: Bool? = nil
    var evidence: String? = nil
}

/// The live processing pipeline (from /api/pipeline) — the checkpoints a message moves
/// through, plus the last thing that finished.
struct PipelineState: Equatable {
    var stages: [String] = []
    var busy = false
    var lastWho = ""
    var lastLabel = ""
}

/// One line in the recent-activity feed (from /api/notifications).
struct Activity: Identifiable, Equatable {
    var what: String; var detail: String; var at: Double
    var id: String { "\(at)-\(what)-\(detail)" }
}

/// A learned writing-voice profile for one segment (from /api/voice-profiles).
struct VoiceProfile: Identifiable, Decodable, Equatable {
    let segment: String
    var summary: String = ""
    var examples: [String] = []
    var sample_count: Int = 0
    var id: String { segment }
}

/// Headline counts for the Console overview (from /api/stats).
struct ConsoleStats: Equatable {
    var conversations = 0
    var repliesWaiting = 0
    var sent = 0
    var flagged = 0
}

/// One day of volume-by-tier data (from /api/metrics/daily) for the analytics chart.
struct DailyMetric: Identifiable, Decodable, Equatable {
    let day: String
    let t0: Int; let t1: Int; let t2: Int; let t3: Int
    let handled: Int; let surfaced: Int
    var id: String { day }
    var total: Int { t0 + t1 + t2 + t3 }
}

/// The analytical dashboard data — volume over time + your feedback breakdown.
struct Analytics: Equatable {
    var daily: [DailyMetric] = []
    var approve = 0, edit = 0, skip = 0
    var approvalRate = 0.0
}

enum Significance { case calm, one, several, important }

/// A person annotated with their current state, derived from live decisions/commitments.
struct PersonState: Identifiable {
    let person: Person
    let state: String      // "Waiting on you" / "Proposal overdue" / "All clear" / relationship
    let tier: Int          // 0 none, 2 blue, 3 amber
    var id: String { person.id }
}

@MainActor
final class StewardStore: ObservableObject {
    static let shared = StewardStore()

    @Published var decisions: [Decision] = []
    @Published var people: [Person] = []
    @Published var commitments: [Commitment] = []
    @Published var significance: Significance = .calm
    @Published var focused: Decision?      // the single decision in focus (detail view)
    @Published var focusedHandled: HandledDetail?   // a handled item opened read-only
    @Published var tab: StewardTab = .today
    @Published var loading = false         // true only during a manual refresh
    @Published var lastUpdated: Date?
    @Published var hoursSaved = 0          // value: hours saved today (24h window)
    @Published var handled = 0             // quietly handled today (24h window)
    @Published var handledItems: [HandledItem] = []   // recent "what Steward did" history
    @Published var processed = 0           // total messages processed (trust)
    @Published var escalated = 0           // total flagged for you (trust)
    @Published var trust = TrustMetrics()  // full analytical bundle (today window)
    @Published var paused = false          // agent on/off (the real control)
    @Published var stats = ConsoleStats()  // headline counts for the Console overview
    @Published var analytics = Analytics() // volume-over-time + feedback breakdown
    @Published var pipeline = PipelineState()        // live processing checkpoints
    @Published var activity: [Activity] = []         // recent-activity feed
    @Published var voice: [VoiceProfile] = []        // writing-voice profiles
    @Published var connGmail = false
    @Published var connTelegram = false
    @Published var connWhatsApp = false
    @Published var reachable = true        // can we reach the engine right now?
    @Published var didLoadOnce = false     // distinguishes "starting up" from "offline"

    private var timer: Timer?
    private var base: String { AppConfig.shared.dashboardURL }   // http://127.0.0.1:8000

    func begin() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 12.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    /// `manual` = the user tapped refresh → show the spinner. The silent 12s poll passes false.
    func refresh(manual: Bool = false) {
        if manual { loading = true }
        Task { await self.load() }
    }

    private func load() async {
        let up = await ping()
        async let d: [Decision] = fetch("/api/decisions", key: "items")
        async let c: [Commitment] = fetchCommitments()
        async let p: [Person] = fetch("/api/contacts", key: "items")
        async let q: [HandledItem] = fetch("/api/queue", key: "items")
        let (dec, com, ppl, queue) = await (d, c, p, q)
        let m = await fetchTrust()
        let st = await fetchStats()
        let an = await fetchAnalytics()
        let pipe = await fetchPipeline()
        let act = await fetchActivity()
        let vox: [VoiceProfile] = await fetch("/api/voice-profiles", key: "items")
        withAnimation(.easeOut(duration: Steward.M.standard)) {
            // ux-trust-1: surface the MOST URGENT decision first, not the oldest. The
            // engine returns decisions oldest-first (open_pending orders by created_at
            // ASC), so .first was the oldest item — burying a later tier-3 urgency. Rank
            // by tier DESC (3 "needs you soon" before 2) so the popover headline and the
            // home list both lead with the most consequential thing. Stable: equal-tier
            // items keep the engine's arrival order.
            self.decisions = Self.rankedByUrgency(dec)
            self.commitments = com
            self.people = ppl
            self.stats = st
            self.analytics = an
            self.pipeline = pipe
            self.activity = act
            self.voice = vox
            // Recent handled history (replies sent / filed / auto-handled), newest first.
            self.handledItems = Array(
                queue.filter { ($0.response_via ?? "").isEmpty == false }
                     .sorted { ($0.at ?? 0) > ($1.at ?? 0) }
                     .prefix(20))
            self.significance = Self.significance(of: dec)
            self.trust = m
            self.hoursSaved = m.hoursSaved
            self.handled = m.autoHandled
            self.processed = m.processed
            self.escalated = m.escalated
            self.reachable = up
        }
        self.lastUpdated = Date()
        self.didLoadOnce = true
        self.loading = false
    }

    private func ping() async -> Bool {
        guard let url = URL(string: base + "/api/status") else { return false }
        do {
            let (data, resp) = try await URLSession.shared.data(for: URLRequest(url: url, timeoutInterval: 3))
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            // Parse the connection-health strip while we're here.
            if let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let c = o["connections"] as? [String: Any] {
                    func on(_ k: String) -> Bool { (c[k] as? [String: Any])?["connected"] as? Bool ?? false }
                    self.connGmail = on("gmail"); self.connTelegram = on("telegram"); self.connWhatsApp = on("whatsapp")
                }
                self.paused = (o["paused"] as? Bool) ?? self.paused
            }
            return ok
        } catch { return false }
    }

    private func fetchPipeline() async -> PipelineState {
        var p = PipelineState()
        guard let url = URL(string: base + "/api/pipeline") else { return p }
        if let (data, _) = try? await URLSession.shared.data(from: url),
           let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            p.stages = (o["stages"] as? [String]) ?? []
            p.busy = (o["busy"] as? Bool) ?? false
            if let last = o["last"] as? [String: Any] {
                p.lastWho = (last["who"] as? String) ?? ""
                p.lastLabel = (last["label"] as? String) ?? ""
            }
        }
        return p
    }

    private func fetchActivity() async -> [Activity] {
        guard let url = URL(string: base + "/api/notifications") else { return [] }
        if let (data, _) = try? await URLSession.shared.data(from: url),
           let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let arr = o["items"] as? [[String: Any]] {
            return arr.prefix(8).map {
                Activity(what: ($0["what"] as? String) ?? "",
                         detail: ($0["detail"] as? String) ?? "",
                         at: ($0["at"] as? Double) ?? Double($0["at"] as? Int ?? 0))
            }
        }
        return []
    }

    private func fetchTrust() async -> TrustMetrics {
        var m = TrustMetrics()
        // TODAY window (last 24h) — day-wise, not weekly.
        guard let url = URL(string: base + "/api/trust?period=daily") else { return m }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let o = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            func i(_ k: String) -> Int { (o?[k] as? Int) ?? Int((o?[k] as? Double) ?? 0) }
            func d(_ k: String) -> Double { (o?[k] as? Double) ?? Double((o?[k] as? Int) ?? 0) }
            let mins = d("estimated_time_saved_minutes")
            m.savedMinutes = Int(mins)
            m.hoursSaved = Int(mins / 60.0)
            m.autoHandled = i("messages_auto_handled")
            m.processed = i("messages_processed")
            m.escalated = i("messages_escalated")
            m.suppressed = i("messages_suppressed")
            m.approvals = i("approvals_requested")
            m.approvalRate = d("approval_rate")
            m.draftAcceptance = d("draft_acceptance_rate")
            m.decisionsAvoided = i("decisions_avoided")
            m.memoryUpdates = i("memory_updates")
            m.commitmentsOpen = i("commitments_open")
            return m
        } catch { return m }
    }

    private func fetchStats() async -> ConsoleStats {
        var s = stats
        guard let url = URL(string: base + "/api/stats") else { return s }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let o = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            s.conversations = (o?["conversations"] as? Int) ?? s.conversations
            s.repliesWaiting = (o?["replies_waiting"] as? Int) ?? s.repliesWaiting
            s.sent = (o?["sent"] as? Int) ?? s.sent
            s.flagged = (o?["flagged_for_you"] as? Int) ?? s.flagged
        } catch { /* keep previous */ }
        return s
    }

    private func fetchAnalytics() async -> Analytics {
        var a = Analytics()
        a.daily = await fetch("/api/metrics/daily", key: "items")
        if let url = URL(string: base + "/api/metrics/accuracy"),
           let (data, _) = try? await URLSession.shared.data(from: url),
           let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            a.approve = (o["approve"] as? Int) ?? 0
            a.edit = (o["edit"] as? Int) ?? 0
            a.skip = (o["skip"] as? Int) ?? 0
            a.approvalRate = (o["approval_rate"] as? Double) ?? 0
        }
        return a
    }

    /// ux-trust-6: the id of the most recent bulk-skip batch, so it can be undone. Set after
    /// a Clear all completes; cleared when the grace window passes or an undo runs.
    @Published var lastClearBatchId: String?
    /// How many tier-3 "needs you soon" items the last Clear all KEPT (it does not drop them
    /// unless the owner explicitly includes urgent), so the UI can say so.
    @Published var lastClearKeptUrgent = 0
    /// How many decisions the last Clear all actually skipped — used by the undo affordance.
    @Published var lastClearCount = 0

    /// Clear all (ux-trust-6) — SCOPED and RECOVERABLE. By default skips only non-urgent
    /// (tier < 3) decisions and keeps tier-3 "needs you soon" items; pass includeUrgent=true
    /// (after a distinct confirmation that names the urgent count) to clear those too. The
    /// response carries a batch_id so the owner can undo within the grace window, and the
    /// optimistic list is rolled back if the POST fails.
    func clearAll(includeUrgent: Bool = false) {
        let snapshot = decisions   // for rollback if the call fails
        // Optimistic: remove exactly the items this call will skip (keep urgent unless asked).
        withAnimation(.easeOut(duration: Steward.M.standard)) {
            decisions = includeUrgent ? [] : decisions.filter { $0.tier >= 3 }
            focused = nil
            significance = Self.significance(of: decisions)
        }
        guard let url = URL(string: base + "/api/decisions/clear") else { return }
        var req = URLRequest(url: url); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["include_urgent": includeUrgent])
        URLSession.shared.dataTask(with: req) { [weak self] data, resp, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                if ok, let data,
                   let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.lastClearBatchId = o["batch_id"] as? String
                    self.lastClearKeptUrgent = (o["kept_urgent"] as? Int) ?? 0
                    self.lastClearCount = (o["cleared"] as? Int) ?? 0
                } else {
                    // Rollback: the server did not confirm — never leave the list falsely empty.
                    withAnimation(.easeOut(duration: Steward.M.standard)) {
                        self.decisions = snapshot
                        self.significance = Self.significance(of: snapshot)
                    }
                }
                self.refresh()
            }
        }.resume()
    }

    /// Undo the last Clear all (ux-trust-6) — restores every decision skipped in that batch
    /// (within the server grace window) back to PENDING so it re-surfaces.
    func undoClearAll() {
        guard let batch = lastClearBatchId else { return }
        postAny("/api/decisions/clear/undo", body: ["batch_id": batch])
        lastClearBatchId = nil; lastClearKeptUrgent = 0; lastClearCount = 0
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in self?.refresh() }
    }

    static func significance(of decisions: [Decision]) -> Significance {
        if decisions.isEmpty { return .calm }
        if decisions.contains(where: { $0.tier >= 3 }) { return .important }
        return decisions.count == 1 ? .one : .several
    }

    /// ux-trust-1: order decisions most-urgent-first — highest tier (3 "needs you soon"
    /// before 2 "when you can"), then most recent (higher id) within a tier. `enumerated`
    /// keeps a stable order for equal-tier items (preserving the engine's arrival order)
    /// even though Swift's sort is not guaranteed stable.
    static func rankedByUrgency(_ decisions: [Decision]) -> [Decision] {
        decisions.enumerated()
            .sorted { a, b in
                if a.element.tier != b.element.tier { return a.element.tier > b.element.tier }
                if a.element.id != b.element.id { return a.element.id > b.element.id }
                return a.offset < b.offset
            }
            .map { $0.element }
    }

    /// People with a derived current state (the relationships view).
    func peopleStates() -> [PersonState] {
        let waiting = Set(decisions.map { $0.sender.lowercased() })
        let overdue = Set(commitments.filter { $0.due_date.isEmpty == false }.map { $0.to.lowercased() })
        let mine = Self.selfAddresses()
        return people
            .filter { !mine.contains($0.email.lowercased()) }   // never list the OWNER as a "person it knows"
            .prefix(12).map { p in
                let key = p.email.lowercased(), name = p.name.lowercased()
                if waiting.contains(key) || waiting.contains(name) {
                    return PersonState(person: p, state: "Waiting on you", tier: 3)
                }
                if overdue.contains(key) {
                    return PersonState(person: p, state: "Reply pending", tier: 2)
                }
                return PersonState(person: p, state: Self.humanRelationship(p.relationship), tier: 0)
            }
    }

    /// The owner's own addresses (from .env: GMAIL_ADDRESS + SELF_ADDRESSES), so Steward never
    /// surfaces the user to themselves as a contact. Best-effort; empty when unconfigured.
    static func selfAddresses() -> Set<String> {
        let cfg = AppConfig.shared
        var s = Set<String>()
        if let g = cfg.envValue("GMAIL_ADDRESS")?.lowercased(), !g.isEmpty { s.insert(g) }
        for a in (cfg.envValue("SELF_ADDRESSES") ?? "").split(separator: ",") {
            let t = a.trimmingCharacters(in: .whitespaces).lowercased()
            if !t.isEmpty { s.insert(t) }
        }
        return s
    }

    /// Human-readable relationship/source label — never a raw code like "wa_contact" /
    /// "phone_contact" that leaks the internal taxonomy to the owner.
    static func humanRelationship(_ r: String) -> String {
        switch r.lowercased() {
        case "": return "All clear"
        case "phone_contact": return "In your contacts"
        case "wa_contact": return "On WhatsApp"
        case "vip": return "VIP"
        default: return r.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    // MARK: - fetch helpers (best-effort)
    private func fetch<T: Decodable>(_ path: String, key: String) async -> [T] {
        guard let url = URL(string: base + path) else { return [] }
        do {
            var req = URLRequest(url: url, timeoutInterval: 4)
            let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
            if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
            let (data, _) = try await URLSession.shared.data(for: req)
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            guard let arr = obj?[key] else { return [] }
            let sub = try JSONSerialization.data(withJSONObject: arr)
            return try JSONDecoder().decode([T].self, from: sub)
        } catch { return [] }
    }

    private func fetchCommitments() async -> [Commitment] {
        guard let url = URL(string: base + "/api/commitments") else { return [] }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            guard let open = obj?["open"] else { return [] }
            let sub = try JSONSerialization.data(withJSONObject: open)
            return try JSONDecoder().decode([Commitment].self, from: sub)
        } catch { return [] }
    }

    /// Open a handled item's full detail (read-only) — fetches what arrived, the AI's
    /// reasoning, and the reply that was sent, and renders it natively.
    func openHandled(_ h: HandledItem) {
        let mid = h.message_id
        var d = HandledDetail(messageId: mid, sender: h.sender, viaLabel: h.via_label ?? "", label: h.label)
        d.subject = h.subject ?? ""
        focusedHandled = d   // show immediately with what we have; enrich on fetch
        Task { @MainActor in
            let enriched = await self.fetchDetail(mid, base: d)
            if self.focusedHandled != nil { self.focusedHandled = enriched }
        }
    }

    private func fetchDetail(_ messageId: String, base seed: HandledDetail) async -> HandledDetail {
        var d = seed
        let enc = messageId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? messageId
        guard let url = URL(string: base + "/api/email/" + enc) else { return d }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let o = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            if let a = o?["arrived"] as? [String: Any] {
                d.sender = (a["from"] as? String) ?? d.sender
                d.subject = (a["subject"] as? String) ?? d.subject
                d.quote = (a["quote"] as? String) ?? ""
            }
            if let ai = o?["ai"] as? [String: Any] {
                d.why = (ai["why"] as? String) ?? ""
                d.category = (ai["who_is_sender"] as? String) ?? ""
                d.who = (ai["who_is_sender"] as? String) ?? ""
                d.urgency = (ai["urgency"] as? String) ?? ""
                d.undo = (ai["undo"] as? String) ?? ""
                d.confidence = (ai["confidence"] as? String) ?? ""
            }
            if let draft = o?["draft"] as? [String: Any] {
                d.reply = (draft["text"] as? String) ?? ""
            }
            d.channel = (o?["channel_label"] as? String) ?? d.channel
        } catch { /* keep the seed */ }
        return d
    }

    // MARK: - feedback / teach / rules

    /// Teach Steward whether a handled item was the right call (👍/👎). Fire-and-forget.
    func sendFeedback(_ messageId: String, thumbs: String) {
        let enc = messageId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? messageId
        post("/api/email/\(enc)/feedback", body: ["thumbs": thumbs])
    }

    /// Plain-English instruction ("never reply to my landlord without me"). Returns the
    /// engine's reply text via the completion (on the main actor).
    func teach(_ text: String, _ done: @escaping (String) -> Void) {
        guard let url = URL(string: base + "/api/command") else { done("Couldn't reach Steward."); return }
        var req = URLRequest(url: url); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var reply = "Done."
            if let data, let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                reply = (o["reply"] as? String) ?? reply
            }
            DispatchQueue.main.async { done(reply); self.loadRules() }
        }.resume()
    }

    @Published var rules: [Rule] = []
    @Published var proposedRules: [Rule] = []

    func loadRules() {
        Task { @MainActor in
            self.rules = await fetch("/api/rules", key: "items")
            self.proposedRules = await fetch("/api/rules/proposed", key: "items")
        }
    }
    func ruleConfirm(_ id: Int) { post("/api/rules/\(id)/confirm", body: nil); loadRules() }
    func ruleReject(_ id: Int) { post("/api/rules/\(id)/reject", body: nil); loadRules() }
    func ruleDelete(_ id: Int) {
        rules.removeAll { $0.id == id }
        post("/api/rules/\(id)/delete", body: nil); loadRules()
    }

    /// Update a contact's importance + VIP flag (People tab). Optimistic; reloads after.
    func contactUpdate(_ email: String, importance: Int, vip: Bool) {
        let enc = email.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? email
        postAny("/api/contacts/\(enc)/update", body: ["importance": importance, "flags": vip ? ["vip"] : []])
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.refresh() }
    }

    /// Save an unknown/unsaved sender as a real contact: assigns a name (and optionally a
    /// phone number that bridges a WhatsApp @lid to the number). Recognition-only — never
    /// sends a message. Reloads People + refreshes once the server confirms.
    func saveContact(identifier: String, name: String, phone: String = "", email: String = "",
                     _ done: @escaping (Bool) -> Void = { _ in }) {
        let body: [String: Any] = ["identifier": identifier, "name": name,
                                   "phone": phone, "email": email]
        postResult("/api/contacts/save", body: body) { [weak self] ok in
            if ok { self?.refresh() }
            done(ok)
        }
    }

    /// Owner self-context ("About you", Settings). Load the current description; the agent
    /// uses it to judge priority. Saving takes effect on the next message (no restart).
    func loadOwnerAbout(_ done: @escaping (String) -> Void) {
        guard let url = URL(string: base + "/api/settings/about") else { done(""); return }
        var req = URLRequest(url: url, timeoutInterval: 4)
        let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var text = ""
            if let data, let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                text = (o["about"] as? String) ?? ""
            }
            DispatchQueue.main.async { done(text) }
        }.resume()
    }

    func saveOwnerAbout(_ text: String, _ done: @escaping (Bool) -> Void = { _ in }) {
        postResult("/api/settings/about", body: ["about": text]) { ok in done(ok) }
    }

    /// Import the owner's macOS address book (Apple Contacts) into recognition — so anyone they
    /// have saved is recognized by their saved name the instant they message. Asks for Contacts
    /// permission once. Recognition-only; never sends anything. Calls done(ok, importedCount).
    func importPhoneContacts(_ done: @escaping (Bool, Int) -> Void) {
        let store = CNContactStore()
        store.requestAccess(for: .contacts) { granted, _ in
            guard granted else { DispatchQueue.main.async { done(false, 0) }; return }
            var entries: [[String: Any]] = []
            let keys: [CNKeyDescriptor] = [
                CNContactGivenNameKey, CNContactFamilyNameKey, CNContactOrganizationNameKey,
                CNContactPhoneNumbersKey, CNContactEmailAddressesKey,
            ].map { $0 as CNKeyDescriptor }
            let req = CNContactFetchRequest(keysToFetch: keys)
            do {
                try store.enumerateContacts(with: req) { c, _ in
                    let full = "\(c.givenName) \(c.familyName)".trimmingCharacters(in: .whitespaces)
                    let name = full.isEmpty ? c.organizationName : full
                    let phones = c.phoneNumbers.map { $0.value.stringValue }
                    let emails = c.emailAddresses.map { String($0.value) }
                    if !name.isEmpty && (!phones.isEmpty || !emails.isEmpty) {
                        entries.append(["name": name, "phones": phones, "emails": emails])
                    }
                }
            } catch {
                DispatchQueue.main.async { done(false, 0) }; return
            }
            self.postContactsImport(entries, done)
        }
    }

    private func postContactsImport(_ entries: [[String: Any]],
                                    _ done: @escaping (Bool, Int) -> Void) {
        guard let url = URL(string: base + "/api/contacts/import") else { done(false, 0); return }
        var req = URLRequest(url: url); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["contacts": entries])
        URLSession.shared.dataTask(with: req) { [weak self] data, resp, _ in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            var imported = 0
            if let data, let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                imported = (o["imported"] as? Int) ?? 0
            }
            DispatchQueue.main.async {
                if (200..<300).contains(code) { self?.refresh() }
                done((200..<300).contains(code), imported)
            }
        }.resume()
    }

    func rebuildVoice() { post("/api/voice-profiles/rebuild", body: nil) }

    /// The real agent on/off — pauses/resumes the engine via the API (works no matter how
    /// the engine was launched). Optimistic; the next poll confirms.
    func setPaused(_ on: Bool) {
        paused = on
        post(on ? "/api/pause" : "/api/resume", body: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.refresh() }
    }

    /// Run a hypothetical message through the brain (zero side effects). Returns the
    /// result fields via the completion on the main actor.
    func testBrain(sender: String, subject: String, body: String, _ done: @escaping ([String: Any]) -> Void) {
        guard let url = URL(string: base + "/api/test-pipeline") else { done([:]); return }
        var req = URLRequest(url: url); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["sender": sender, "subject": subject, "email_text": body])
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var out: [String: Any] = [:]
            if let data, let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { out = o }
            DispatchQueue.main.async { done(out) }
        }.resume()
    }

    private func postAny(_ path: String, body: [String: Any]) {
        guard let url = URL(string: base + path) else { return }
        var req = URLRequest(url: url); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req).resume()
    }

    // MARK: - send state (UX/trust #1: the owner must SEE sending -> sent/failed)
    // A send is the one irreversible action; a fire-and-forget POST whose failure is
    // pixel-identical to success is the deepest contradiction of "earn the right to act".
    // The act surfaces watch sendPhase[id] and render Sending… / Sent ✓ / Couldn't send.
    enum SendPhase: Equatable { case sending, sent, failed }
    @Published var sendPhase: [Int: SendPhase] = [:]

    /// POST that reports whether the server actually ACCEPTED it (HTTP 2xx), on the main
    /// thread — unlike `post`, which discards the response.
    private func postResult(_ path: String, body: [String: Any]?, done: @escaping (Bool) -> Void) {
        guard let url = URL(string: base + path) else { done(false); return }
        var req = URLRequest(url: url); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
        if let body { req.httpBody = try? JSONSerialization.data(withJSONObject: body) }
        URLSession.shared.dataTask(with: req) { _, resp, err in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            let ok = err == nil && (200..<300).contains(code)
            DispatchQueue.main.async { done(ok) }
        }.resume()
    }

    // MARK: - actions (real, dry-run-safe via the engine)
    func skip(_ d: Decision) { post("/api/actions/\(d.id)/skip", body: nil); clearAndRefresh() }

    /// ux-trust-2: acknowledge a reminder / non-send card as handled WITHOUT sending
    /// anything. A reminder has no reply to send; marking it done must not pretend a reply
    /// went out. We close it through the same non-sending skip seam (terminal SKIPPED), so
    /// the engine never enters the send path for it.
    func acknowledge(_ d: Decision) { post("/api/actions/\(d.id)/skip", body: nil); clearAndRefresh() }

    func approve(_ d: Decision, editedDraft: String?) {
        // ux-trust-2: never route a non-send card (reminder) through the approve/send path —
        // it would silently no-op while the UI implies a reply was sent. Acknowledge instead.
        if d.isReminder { acknowledge(d); return }

        // ux-trust-5: guard on TRIMMED content, not Swift's length-only isEmpty. A
        // whitespace-only or cleared edit is a blank reply — never send it, and never
        // silently fall back to the original AI draft the owner cleared.
        var editBody: [String: Any]? = nil
        if let raw = editedDraft {
            let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty { return }   // leave the card open to write a reply or Skip
            if trimmed != d.draft.trimmingCharacters(in: .whitespacesAndNewlines) {
                editBody = ["text": raw]
            }
        }

        // Optimistic send: show "Sending…" at once, then resolve to Sent (drop the card) or
        // a REAL failure the owner can see and retry — never a silent fire-and-forget.
        sendPhase[d.id] = .sending
        let approveStep: () -> Void = { [weak self] in
            guard let self else { return }
            self.postResult("/api/actions/\(d.id)/approve", body: nil) { ok in
                self.sendPhase[d.id] = ok ? .sent : .failed
                guard ok else { return }   // failure: leave the card up, show Retry
                // brief "Sent ✓" beat, then drop the card + reconcile from the server.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) {
                    if self.focused?.id == d.id { self.focused = nil }
                    self.sendPhase[d.id] = nil
                    self.refresh()
                }
            }
        }
        if let editBody {
            postResult("/api/actions/\(d.id)/edit", body: editBody) { [weak self] ok in
                if ok { approveStep() } else { self?.sendPhase[d.id] = .failed }
            }
        } else {
            approveStep()
        }
    }

    /// Retry a send that previously failed (clears the failed phase and re-runs approve).
    func retrySend(_ d: Decision) { sendPhase[d.id] = nil; approve(d, editedDraft: nil) }

    /// Mark a commitment done (server resolves it). Optimistic: drop it from the list now.
    func commitmentDone(_ id: String) {
        commitments.removeAll { $0.id == id }
        post("/api/commitments/\(id)/done", body: nil)
        clearAndRefresh()
    }
    /// Snooze a commitment by N days (server pushes its due date out). Optimistic remove;
    /// it returns to the list when it next comes due.
    func commitmentSnooze(_ id: String, days: Int = 2) {
        commitments.removeAll { $0.id == id }
        postAny("/api/commitments/\(id)/snooze", body: ["days": days])
        clearAndRefresh()
    }

    private func clearAndRefresh() {
        focused = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in self?.refresh() }
    }

    private func post(_ path: String, body: [String: String]?) {
        guard let url = URL(string: base + path) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
        if let body { req.httpBody = try? JSONSerialization.data(withJSONObject: body) }
        URLSession.shared.dataTask(with: req).resume()
    }
}

enum StewardTab { case today, people, commitments, console, trust }
