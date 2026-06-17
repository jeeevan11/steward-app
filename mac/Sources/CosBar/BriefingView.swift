import SwiftUI

/// The menu-bar popover. Two crisp zones that are never confused:
///   • NEEDS YOU  — things still to handle, with one-click actions (approve / skip).
///   • HANDLED    — a calm summary of what Steward already did (described by the ACTION
///                  taken, never a tier label), so "to-do" and "done" are unmistakable.
/// No number bar, no pause toggle — summary + quick actions only.
struct BriefingView: View {
    @ObservedObject var store = StewardStore.shared
    @ObservedObject var cfg = AppConfig.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                GlyphView(significance: store.significance, size: 13)
                Text("Steward").font(Steward.F.label).tracking(0.6).foregroundColor(Steward.C.t2)
                Spacer()
                RefreshButton()
            }
            .padding(.bottom, 14)

            if !cfg.onboardingComplete {
                setup
            } else if store.didLoadOnce && !store.reachable {
                Text("Steward is waking up.").font(Steward.F.popHero).foregroundColor(Steward.C.tx)
                Text("One moment while it starts.")
                    .font(Steward.F.support).foregroundColor(Steward.C.t2).padding(.top, 6)
            } else {
                needsYou
                handledSummary
            }

            Divider().overlay(Steward.C.line).padding(.vertical, 12)
            HStack {
                Button { StewardWindow.shared.show() } label: {
                    HStack(spacing: 6) {
                        Text("Open dashboard").font(Steward.F.meta).foregroundColor(Steward.C.tx)
                        Image(systemName: "arrow.up.right").font(.system(size: 10)).foregroundColor(Steward.C.t3)
                    }
                }.buttonStyle(.plain)
                Spacer()
                Button { NSApp.terminate(nil) } label: {
                    Text("Quit").font(Steward.F.meta).foregroundColor(Steward.C.t3)
                }.buttonStyle(.plain)
            }
        }
        .padding(16)
        .frame(width: 340)
        .background(Steward.C.surface)
        .environment(\.colorScheme, .dark)
    }

    // MARK: - NEEDS YOU (still to handle — quick actions)
    @ViewBuilder private var needsYou: some View {
        if let top = store.decisions.first {
            zoneLabel("Needs you", Steward.C.amber)
            Text(headline).font(Steward.F.popHero).foregroundColor(Steward.C.tx)
                .fixedSize(horizontal: false, vertical: true).padding(.top, 2).padding(.bottom, 12)
            actCard(top)
            ForEach(store.decisions.dropFirst().prefix(2)) { d in compactRow(d) }
            if store.decisions.count > 3 {
                Button { StewardWindow.shared.show() } label: {
                    Text("+ \(store.decisions.count - 3) more — review all")
                        .font(Steward.F.meta).foregroundColor(Steward.C.t2)
                }.buttonStyle(.plain).padding(.top, 8)
            }
        } else {
            Text("You're all caught up.").font(Steward.F.popHero).foregroundColor(Steward.C.tx)
            Text("Nothing needs you right now.")
                .font(Steward.F.support).foregroundColor(Steward.C.t2).padding(.top, 6)
        }
    }

    // MARK: - HANDLED (already done — summary by action, never a tier label)
    @ViewBuilder private var handledSummary: some View {
        if store.handled > 0 || !store.handledItems.isEmpty {
            Divider().overlay(Steward.C.line).padding(.vertical, 14)
            zoneLabel("Handled", Steward.C.t3)
            Text(summaryLine).font(Steward.F.support).foregroundColor(Steward.C.t2)
                .fixedSize(horizontal: false, vertical: true).padding(.top, 2)
            ForEach(store.handledItems.prefix(2)) { h in
                Button { store.openHandled(h) } label: {
                    HStack(spacing: 8) {
                        Text(h.sender).font(Steward.F.meta).foregroundColor(Steward.C.t2).lineLimit(1)
                        Text("· \(actionPhrase(h))").font(Steward.F.meta).foregroundColor(Steward.C.t3).lineLimit(1)
                        Spacer(minLength: 0)
                    }.padding(.top, 7).contentShape(Rectangle())
                }.buttonStyle(.plain)
            }
        }
    }
    private var summaryLine: String {
        let n = store.handled
        if n <= 0 { return "Nothing needed handling yet today." }
        return "Quietly handled \(n) today — the noise. You've seen everything that mattered."
    }
    /// What was actually DONE (not the tier intent) — keeps "done" unmistakable from "to-do".
    private func actionPhrase(_ h: HandledItem) -> String {
        switch (h.response_via ?? "") {
        case "telegram", "web", "human", "direct": return "you replied"
        case "auto":
            let l = h.label.lowercased()
            if l.contains("filed") { return "filed away" }
            if l.contains("told") { return "noted for you" }
            return "handled for you"
        default: return h.via_label ?? "handled"
        }
    }

    // MARK: - act card (one-click approve / skip on the top thing to handle)
    private func actCard(_ d: Decision) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Circle().fill(Steward.accent(forTier: d.tier)).frame(width: 7, height: 7)
                Text(d.sender.isEmpty ? d.title : d.sender)
                    .font(Steward.F.support).foregroundColor(Steward.C.tx).lineLimit(1)
                Spacer(minLength: 8)
                Text(d.channel).font(Steward.F.label).foregroundColor(Steward.C.t3)
            }
            if !d.sentence.isEmpty {
                Text(d.sentence).font(Steward.F.meta).foregroundColor(Steward.C.t2)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            if !d.draft.isEmpty {
                Text(d.draft).font(Steward.F.meta).foregroundColor(Steward.C.t2)
                    .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Steward.C.canvas).clipShape(RoundedRectangle(cornerRadius: 8))
            }
            HStack(spacing: 8) {
                // ux-trust-2: a reminder is a nudge with no reply to send. Show a truthful
                // "Done" that acknowledges it (no send), never a send-shaped Approve that
                // would silently no-op while implying a reply went out. Sendable cards keep
                // Send / Approve; a draftless reply card opens so you can write one first.
                if d.isReminder {
                    actBtn("Mark done", fill: true) { store.acknowledge(d) }
                    actBtn("Snooze", fill: false) { store.skip(d) }
                } else if store.sendPhase[d.id] == .sending {
                    actStatus("Sending…")
                } else if store.sendPhase[d.id] == .sent {
                    actStatus("Sent ✓")
                } else if store.sendPhase[d.id] == .failed {
                    actStatus("Couldn’t send")
                    actBtn("Retry", fill: true) { store.retrySend(d) }
                    actBtn("Skip", fill: false) { store.skip(d) }
                } else {
                    actBtn(d.draft.isEmpty ? "Open to reply" : (d.tier >= 3 ? "Send" : "Approve"), fill: true) {
                        if d.draft.isEmpty { store.focused = d; StewardWindow.shared.show() }
                        else { store.approve(d, editedDraft: nil) }
                    }
                    actBtn("Skip", fill: false) { store.skip(d) }
                }
                Spacer()
                Button { store.focused = d; StewardWindow.shared.show() } label: {
                    Image(systemName: "arrow.up.right").font(.system(size: 11)).foregroundColor(Steward.C.t3)
                }.buttonStyle(.plain)
            }
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(Steward.C.raised).clipShape(RoundedRectangle(cornerRadius: 12))
    }
    private func actBtn(_ label: String, fill: Bool, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label).font(Steward.F.meta).foregroundColor(fill ? Steward.C.canvas : Steward.C.tx)
                .padding(.horizontal, 16).padding(.vertical, 8)
                .background(fill ? Steward.C.onLight : Color.clear)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(fill ? Color.clear : Color.white.opacity(0.16)))
                .clipShape(RoundedRectangle(cornerRadius: 8))
        }.buttonStyle(.plain)
    }
    /// Non-interactive status pill for the in-flight / sent / failed send states.
    private func actStatus(_ s: String) -> some View {
        Text(s).font(Steward.F.meta).foregroundColor(Steward.C.t2)
            .padding(.horizontal, 16).padding(.vertical, 8)
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.white.opacity(0.10)))
    }
    private func compactRow(_ d: Decision) -> some View {
        Button { store.focused = d; StewardWindow.shared.show() } label: {
            HStack(spacing: 10) {
                Circle().fill(Steward.accent(forTier: d.tier)).frame(width: 6, height: 6)
                Text(d.sender.isEmpty ? d.title : d.sender)
                    .font(Steward.F.meta).foregroundColor(Steward.C.t2).lineLimit(1)
                Spacer(minLength: 8)
                Image(systemName: "chevron.right").font(.system(size: 9)).foregroundColor(Steward.C.t3)
            }.padding(.vertical, 9).contentShape(Rectangle())
        }.buttonStyle(.plain)
    }

    private func zoneLabel(_ s: String, _ tint: Color) -> some View {
        HStack(spacing: 7) {
            Circle().fill(tint).frame(width: 6, height: 6)
            Text(s).font(Steward.F.label).tracking(2).textCase(.uppercase).foregroundColor(Steward.C.t3)
        }.padding(.bottom, 6)
    }

    private var setup: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Finish setting up.").font(Steward.F.popHero).foregroundColor(Steward.C.tx)
            Text("Add your details and Steward starts handling things.")
                .font(Steward.F.support).foregroundColor(Steward.C.t2)
            Button { OnboardingWindow.shared.show() } label: {
                Text("Set up").font(Steward.F.support).foregroundColor(Steward.C.tx)
                    .frame(maxWidth: .infinity).padding(.vertical, 10)
                    .overlay(RoundedRectangle(cornerRadius: 9).stroke(Color.white.opacity(0.18)))
            }.buttonStyle(.plain).padding(.top, 10)
        }
    }
    private var headline: String {
        store.decisions.count == 1 ? "One thing to handle."
                                   : "\(store.decisions.count) things to handle."
    }
}
