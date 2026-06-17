import SwiftUI

/// The Steward app window — calm, editorial, organized around Decision / Person / Commitment.
/// Single-focus: when a decision is in focus, the whole window is that one decision.
struct StewardRootView: View {
    @ObservedObject var store = StewardStore.shared

    var body: some View {
        ZStack {
            Steward.C.canvas.ignoresSafeArea()
            if let d = store.focused {
                DecisionDetailView(decision: d)
                    .transition(.opacity)
            } else if let h = store.focusedHandled {
                HandledDetailView(detail: h)
                    .transition(.opacity)
            } else {
                VStack(spacing: 0) {
                    tabBar
                    ScrollView {
                        Group {
                            switch store.tab {
                            case .today: HomeView()
                            case .people: PeopleView()
                            case .commitments: CommitmentsView()
                            case .console: ConsoleView()
                            case .trust: TrustCenterView()
                            }
                        }
                        .frame(maxWidth: 720)
                        .frame(maxWidth: .infinity)
                        .id(store.tab)
                        .transition(.opacity)
                    }
                    .animation(.easeOut(duration: Steward.M.standard), value: store.tab)
                }
            }
        }
        .frame(minWidth: 760, minHeight: 620)
        .environment(\.colorScheme, .dark)
        .animation(.easeOut(duration: Steward.M.screen), value: store.focused)
        .animation(.easeOut(duration: Steward.M.screen), value: store.focusedHandled)
        .onAppear { store.refresh() }
    }

    private var tabBar: some View {
        HStack(spacing: 26) {
            tab("Today", .today); tab("People", .people); tab("Commitments", .commitments)
            tab("Console", .console)
            Spacer()
            if let u = store.lastUpdated {
                Text(relative(u)).font(.system(size: 11)).foregroundColor(Steward.C.t3)
            }
            RefreshButton()
            Button { withAnimation { store.tab = .trust } } label: {
                Image(systemName: "gearshape").foregroundColor(store.tab == .trust ? Steward.C.tx : Steward.C.t3)
            }.buttonStyle(.plain).help("Settings, channels, and trust")
        }
        .padding(.horizontal, 28).padding(.vertical, 16)
    }

    private func relative(_ d: Date) -> String {
        let s = Int(Date().timeIntervalSince(d))
        if s < 5 { return "Updated just now" }
        if s < 60 { return "Updated \(s)s ago" }
        return "Updated \(s / 60)m ago"
    }

    private func tab(_ label: String, _ t: StewardTab) -> some View {
        Button { store.focused = nil; store.focusedHandled = nil; store.tab = t } label: {
            Text(label).font(Steward.F.support)
                .foregroundColor(store.tab == t ? Steward.C.tx : Steward.C.t3)
        }.buttonStyle(.plain)
    }
}

// MARK: - Guided state card (spoon-feeds setup / offline / loading)
struct GuidedCard: View {
    let icon: String
    let title: String
    let message: String
    var actionLabel: String? = nil
    var action: (() -> Void)? = nil
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: icon).font(.system(size: 34, weight: .light)).foregroundColor(Steward.C.t2)
            Text(title).font(Steward.F.hero(30)).foregroundColor(Steward.C.tx).multilineTextAlignment(.center)
            Text(message).font(Steward.F.body).foregroundColor(Steward.C.t3)
                .multilineTextAlignment(.center).frame(maxWidth: 440).lineSpacing(4)
            if let actionLabel, let action {
                Button(action: action) {
                    Text(actionLabel).font(Steward.F.support).foregroundColor(Steward.C.canvas)
                        .padding(.horizontal, 22).padding(.vertical, 11)
                        .background(Steward.C.onLight).clipShape(RoundedRectangle(cornerRadius: 10))
                }.buttonStyle(.plain)
            }
        }
        .frame(maxWidth: .infinity).padding(.top, 90).padding(.horizontal, Steward.S.hero)
    }
}

// MARK: - Home (the hero + decisions)
struct HomeView: View {
    @ObservedObject var store = StewardStore.shared
    @ObservedObject var cfg = AppConfig.shared
    @State private var confirmingClear = false

    var body: some View {
        if !cfg.onboardingComplete {
            GuidedCard(icon: "hand.wave", title: "Welcome to Steward",
                       message: "Add a few details and Steward starts quietly handling your inbox. It takes about a minute.",
                       actionLabel: "Set up Steward") { OnboardingWindow.shared.show() }
        } else if store.didLoadOnce && !store.reachable {
            GuidedCard(icon: "powersleep", title: "Steward is waking up",
                       message: "This can take a few seconds after you turn it on. If it keeps showing, open Settings to check it’s running.",
                       actionLabel: "Try again") { store.refresh(manual: true) }
        } else {
            normalHome
        }
    }

    private var normalHome: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                Text(greeting).font(Steward.F.support).foregroundColor(Steward.C.t2)
                    .padding(.bottom, Steward.S.lg)
                Text(hero).font(Steward.F.hero()).foregroundColor(Steward.C.tx)
                    .fixedSize(horizontal: false, vertical: true)
                if !store.decisions.isEmpty {
                    Text("Everything else has been handled.")
                        .font(.system(size: 18, weight: .light)).foregroundColor(Steward.C.t3)
                        .padding(.top, Steward.S.lg)
                }
            }
            .padding(.top, Steward.S.hero).padding(.bottom, Steward.S.lg)
            .padding(.horizontal, Steward.S.hero)

            if store.hoursSaved > 0 || store.handled > 0 {
                valueStrip.padding(.horizontal, Steward.S.hero).padding(.bottom, Steward.S.md)
            }
            if store.processed > 0 {
                metricsRow.padding(.horizontal, Steward.S.hero).padding(.bottom, Steward.S.xl)
            }

            if !store.decisions.isEmpty {
                let soon = store.decisions.filter { $0.tier >= 3 }
                let later = store.decisions.filter { $0.tier < 3 }
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 12) {
                        Text("Decisions").font(Steward.F.label).tracking(2).textCase(.uppercase)
                            .foregroundColor(Steward.C.t3)
                        Spacer()
                        // ux-trust-6: Clear all is tier-aware and recoverable. The non-urgent
                        // count is what a plain "Clear all" skips; tier-3 "needs you soon"
                        // items are KEPT unless the owner explicitly taps the second, named
                        // confirmation. An undo affordance appears after a clear runs.
                        let urgentCount = store.decisions.filter { $0.tier >= 3 }.count
                        let nonUrgentCount = store.decisions.count - urgentCount
                        if store.lastClearBatchId != nil {
                            Text("Cleared \(store.lastClearCount).").font(Steward.F.meta)
                                .foregroundColor(Steward.C.t2)
                            Button { withAnimation { store.undoClearAll() } } label: {
                                Text("Undo").font(Steward.F.meta).foregroundColor(Steward.C.blue)
                            }.buttonStyle(.plain).help("Restore the decisions you just cleared")
                        } else if confirmingClear {
                            Text(urgentCount > 0
                                 ? "Skip \(nonUrgentCount)? \(urgentCount) needs-you-soon kept."
                                 : "Skip all \(nonUrgentCount)? You won’t reply to any.")
                                .font(Steward.F.meta).foregroundColor(Steward.C.t2)
                            if nonUrgentCount > 0 {
                                Button { withAnimation { store.clearAll(includeUrgent: false); confirmingClear = false } } label: {
                                    Text(urgentCount > 0 ? "Skip \(nonUrgentCount)" : "Skip all")
                                        .font(Steward.F.meta).foregroundColor(Steward.C.amber)
                                }.buttonStyle(.plain)
                            }
                            if urgentCount > 0 {
                                // Distinct second confirmation that NAMES the urgent count.
                                Button { withAnimation { store.clearAll(includeUrgent: true); confirmingClear = false } } label: {
                                    Text("Skip incl. \(urgentCount) urgent")
                                        .font(Steward.F.meta).foregroundColor(Steward.C.t3)
                                }.buttonStyle(.plain).help("Also skip the \(urgentCount) urgent item(s) that need you soon")
                            }
                            Button { withAnimation { confirmingClear = false } } label: {
                                Text("Cancel").font(Steward.F.meta).foregroundColor(Steward.C.t3)
                            }.buttonStyle(.plain)
                        } else {
                            Button { withAnimation { confirmingClear = true } } label: {
                                Text("Clear all").font(Steward.F.meta).foregroundColor(Steward.C.t3)
                            }.buttonStyle(.plain).help("Skip the non-urgent decisions; urgent ones are kept unless you confirm")
                        }
                    }
                    .padding(.bottom, 4)
                    if !soon.isEmpty {
                        groupLabel("Needs you soon", Steward.C.amber)
                        ForEach(soon) { decisionRow($0) }
                    }
                    if !later.isEmpty {
                        groupLabel("When you can", Steward.C.blue).padding(.top, soon.isEmpty ? 0 : Steward.S.md)
                        ForEach(later) { decisionRow($0) }
                    }
                }
                .padding(.horizontal, Steward.S.hero).padding(.bottom, Steward.S.xl)
            }

            if !store.handledItems.isEmpty {
                recentlyHandled.padding(.horizontal, Steward.S.hero).padding(.bottom, Steward.S.xxl)
            }
        }
    }

    // Compact engine-wide metrics — calm, monochrome tiles.
    private var metricsRow: some View {
        HStack(spacing: Steward.S.sm) {
            metric("\(store.processed)", "Processed")
            metric("\(store.handled)", "Handled quietly")
            metric("\(store.escalated)", "Flagged for you")
        }
    }
    private func metric(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(value).font(.system(size: 24, weight: .light)).foregroundColor(Steward.C.tx)
            Text(label).font(Steward.F.label).foregroundColor(Steward.C.t3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 14).padding(.horizontal, 16)
        .background(Steward.C.surface).clipShape(RoundedRectangle(cornerRadius: 12))
    }

    // The "what Steward did" history — replies sent, filed, auto-handled.
    private var recentlyHandled: some View {
        PreviewSection(title: "Recently handled", items: store.handledItems, preview: 5) { handledRow($0) }
    }
    private func handledRow(_ h: HandledItem) -> some View {
        Button { store.openHandled(h) } label: {
            HStack(spacing: Steward.S.md) {
                Circle().fill(Steward.C.raised).frame(width: 30, height: 30)
                    .overlay(Text(initial(h.sender)).font(Steward.F.meta).foregroundColor(Steward.C.t2))
                VStack(alignment: .leading, spacing: 3) {
                    Text(h.sender).font(Steward.F.support).foregroundColor(Steward.C.tx).lineLimit(1)
                    Text(handledSubtitle(h)).font(Steward.F.meta).foregroundColor(Steward.C.t3).lineLimit(1)
                }
                Spacer(minLength: 8)
                Text(timeAgo(h.at)).font(Steward.F.meta).foregroundColor(Steward.C.t3)
            }
            .padding(.vertical, 12).padding(.horizontal, 8)
            .contentShape(Rectangle())
            .hoverHighlight(radius: 12)
        }.buttonStyle(.plain)
    }
    private func handledSubtitle(_ h: HandledItem) -> String {
        let extras = [h.via_label ?? "", h.subject ?? ""].filter { !$0.isEmpty }
        let tail = extras.joined(separator: " · ")
        return tail.isEmpty ? h.label : "\(h.label) · \(tail)"
    }
    private func timeAgo(_ epoch: Double?) -> String {
        guard let e = epoch, e > 0 else { return "" }
        let s = Int(Date().timeIntervalSince1970 - e)
        if s < 60 { return "just now" }
        if s < 3600 { return "\(s / 60)m ago" }
        if s < 86400 { return "\(s / 3600)h ago" }
        return "\(s / 86400)d ago"
    }

    private var valueStrip: some View {
        HStack(spacing: 10) {
            Image(systemName: "leaf").font(.system(size: 13)).foregroundColor(Steward.C.t3)
            Text(valueText).font(Steward.F.meta).foregroundColor(Steward.C.t2)
        }
        .padding(.vertical, 12).padding(.horizontal, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Steward.C.surface).clipShape(RoundedRectangle(cornerRadius: 12))
    }
    private var valueText: String {
        // ux-trust-3: the underlying bundle is a 24h "today" window (/api/trust?period=daily),
        // matching Settings ("Analytics · today"). Present the time as the WORK-EQUIVALENT of
        // the triage we did ("about N hours of triage"), NOT "saved you N hours today" — a 24h
        // day can't yield 33 reclaimed hours, so that framing makes an honest estimate read as
        // fabricated and breaks the trust ledger. "about" keeps it labelled as an estimate.
        guard store.hoursSaved >= 1 else {
            return "\(store.handled) handled quietly today."
        }
        return "\(store.handled) handled quietly today · about \(store.hoursSaved) hours of triage."
    }

    private func groupLabel(_ s: String, _ tint: Color) -> some View {
        HStack(spacing: 8) {
            Circle().fill(tint).frame(width: 6, height: 6)
            Text(s).font(Steward.F.label).foregroundColor(Steward.C.t3)
        }
        .padding(.top, Steward.S.md).padding(.bottom, 2)
    }

    private func decisionRow(_ d: Decision) -> some View {
        Button { store.focused = d } label: {
            HStack(spacing: Steward.S.md) {
                Circle().fill(Steward.C.raised).frame(width: 34, height: 34)
                    .overlay(Text(initial(d.sender)).font(Steward.F.meta).foregroundColor(Steward.C.t2))
                VStack(alignment: .leading, spacing: 5) {
                    Text(d.title).font(Steward.F.title).foregroundColor(Steward.C.tx).lineLimit(1)
                    if !d.sentence.isEmpty {
                        Text(d.sentence).font(Steward.F.support).foregroundColor(Steward.C.t2)
                            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    }
                    Text("\(d.category) · \(d.channel)").font(.system(size: 12)).foregroundColor(Steward.C.t3)
                }
                Spacer()
                Text("Review").font(Steward.F.meta).foregroundColor(Steward.C.tx)
                    .padding(.horizontal, 18).padding(.vertical, 8)
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.white.opacity(0.16)))
            }
            .padding(.vertical, 18).padding(.horizontal, 8)
            .contentShape(Rectangle())
            .hoverHighlight(radius: 12)
        }.buttonStyle(.plain)
    }
    private func initial(_ s: String) -> String { String((s.first ?? "?")).uppercased() }

    private var greeting: String {
        let h = Calendar.current.component(.hour, from: Date())
        return h < 12 ? "Good morning." : (h < 18 ? "Good afternoon." : "Good evening.")
    }
    private var hero: String {
        switch store.decisions.count {
        case 0: return "You’re clear."
        case 1: return "One decision needs\nyour attention."
        default: return "\(spelled(store.decisions.count)) things need\nyour attention."
        }
    }
    private func spelled(_ n: Int) -> String {
        let w = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven"]
        return w.indices.contains(n) ? w[n] : "\(n)"
    }
}

// MARK: - Decision detail (single focus)
struct DecisionDetailView: View {
    let decision: Decision
    @ObservedObject var store = StewardStore.shared
    @State private var editing = false
    @State private var draft = ""
    @State private var showSave = false

    /// Offer "Save contact" only when this is a genuinely unknown sender we have an
    /// identifier for (never for reminders, which are keyed by thread, not a person).
    private var canSaveContact: Bool {
        !decision.is_saved && !decision.sender_identifier.isEmpty && !decision.isReminder
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { store.focused = nil } label: {
                HStack(spacing: 8) { Image(systemName: "chevron.left"); Text("Decisions").font(Steward.F.meta) }
                    .foregroundColor(Steward.C.t3)
            }.buttonStyle(.plain).padding(.horizontal, 28).padding(.vertical, 16)
            Divider().overlay(Steward.C.line)

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 10) {
                        Circle().fill(Steward.accent(forTier: decision.tier)).frame(width: 8, height: 8)
                        // ux-trust-4: a reminder is something Steward NOTICED, not a peer
                        // reply-decision. Label it distinctly ("Steward noticed") instead of
                        // the maximally-urgent peer-decision "Needs you soon" so the owner
                        // calibrates trust — the proactive nudge is not a verified obligation.
                        Text(decision.isReminder ? "Steward noticed"
                                 : (decision.tier >= 3 ? "Needs you soon" : "When you can"))
                            .font(Steward.F.meta).foregroundColor(Steward.accent(forTier: decision.tier))
                    }.padding(.bottom, 14)
                    Text(decision.title).font(Steward.F.titleLg).foregroundColor(Steward.C.tx)
                        .padding(.bottom, 10)

                    // who / what kind / where — context, always there.
                    Text("From \(decision.sender.isEmpty ? "someone" : decision.sender) · \(decision.category) · \(decision.channel)")
                        .font(Steward.F.meta).foregroundColor(Steward.C.t3)
                        .padding(.bottom, canSaveContact ? 8 : Steward.S.xl)

                    // Unknown sender → let the owner name + save them right from the card.
                    if canSaveContact {
                        HStack(spacing: 8) {
                            Text("UNKNOWN CONTACT").font(Steward.F.label).tracking(1)
                                .foregroundColor(Steward.C.amber)
                            Button { showSave = true } label: {
                                Text("Save contact").font(Steward.F.meta).foregroundColor(Steward.C.canvas)
                                    .padding(.horizontal, 10).padding(.vertical, 4)
                                    .background(Steward.C.onLight).clipShape(RoundedRectangle(cornerRadius: 7))
                            }.buttonStyle(.plain)
                        }.padding(.bottom, Steward.S.xl)
                    }

                    if !decision.sentence.isEmpty {
                        sectionLabel("Why this reached you")
                        Text(decision.sentence).font(Steward.F.body).foregroundColor(Steward.C.t2)
                            .lineSpacing(5).padding(.bottom, Steward.S.xl)
                    }

                    if !decision.quote.isEmpty {
                        sectionLabel("What they said")
                        Text("“\(decision.quote)”").font(Steward.F.body).foregroundColor(Steward.C.t2)
                            .lineSpacing(5).padding(18)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Steward.C.raised).clipShape(RoundedRectangle(cornerRadius: 12))
                            .padding(.bottom, Steward.S.xl)
                    }

                    if !decision.draft.isEmpty {
                        sectionLabel("Suggested response")
                        if editing {
                            TextEditor(text: $draft)
                                .font(Steward.F.body).foregroundColor(Steward.C.tx)
                                .scrollContentBackground(.hidden)
                                .frame(minHeight: 120)
                                .padding(14)
                                .background(Steward.C.raised)
                                .clipShape(RoundedRectangle(cornerRadius: 12))
                        } else {
                            Text(decision.draft).font(Steward.F.body).foregroundColor(Steward.C.t2)
                                .lineSpacing(5).padding(18)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(Steward.C.raised)
                                .clipShape(RoundedRectangle(cornerRadius: 12))
                        }
                    }

                    // ux-trust-2: a reminder card has no reply to send. Present truthful
                    // acknowledge controls (Mark done / Snooze) instead of a send-shaped
                    // Approve + "Approve sends this reply" caption, which silently no-opped
                    // while implying a reply was sent. Reply cards keep Approve / Edit / Skip.
                    if decision.isReminder {
                        HStack(spacing: 12) {
                            actionButton("Mark done", fill: true,
                                         help: "Acknowledge this reminder. Nothing is sent.") {
                                store.acknowledge(decision)
                            }
                            actionButton("Snooze", fill: false, quiet: true,
                                         help: "Set this reminder aside for now") { store.skip(decision) }
                        }.padding(.top, Steward.S.xl)
                        Text("This is a reminder, not a reply. Marking it done just clears it. Nothing is sent.")
                            .font(Steward.F.meta).foregroundColor(Steward.C.t3).padding(.top, 12)
                    } else if store.sendPhase[decision.id] == .sending {
                        sendStatusRow("Sending your reply…")
                    } else if store.sendPhase[decision.id] == .sent {
                        sendStatusRow("Sent ✓")
                    } else if store.sendPhase[decision.id] == .failed {
                        VStack(alignment: .leading, spacing: 12) {
                            sendStatusRow("Couldn’t send — it was not delivered. Check the thread before retrying.")
                            HStack(spacing: 12) {
                                actionButton("Retry", fill: true, help: "Try sending this reply again") {
                                    store.retrySend(decision)
                                }
                                actionButton("Skip", fill: false, quiet: true,
                                             help: "Don’t reply — set this aside") { store.skip(decision) }
                            }
                        }
                    } else {
                        // ux-trust-5: while editing, a draft that is empty after trimming
                        // whitespace is a blank reply. Disable Approve so a visually-blank
                        // message can never be sent, and never silently fall back to the AI's
                        // original text when the owner has cleared the editor.
                        let approveDisabled = editing &&
                            draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        HStack(spacing: 12) {
                            actionButton("Approve", fill: true,
                                         help: "Send this reply now") {
                                store.approve(decision, editedDraft: editing ? draft : nil)
                            }
                            .disabled(approveDisabled)
                            .opacity(approveDisabled ? 0.4 : 1)
                            actionButton(editing ? "Save changes" : "Edit", fill: false,
                                         help: "Change the reply before sending") {
                                if !editing { draft = decision.draft }
                                editing.toggle()
                            }
                            actionButton("Skip", fill: false, quiet: true,
                                         help: "Don’t reply — set this aside") { store.skip(decision) }
                        }.padding(.top, Steward.S.xl)
                        Text(approveDisabled ? "Type a reply above before sending. An empty message is never sent."
                                : (editing ? "Type your changes above, then Approve to send."
                                           : "Approve sends this reply. Edit changes it first. Skip means no reply."))
                            .font(Steward.F.meta).foregroundColor(Steward.C.t3).padding(.top, 12)
                    }
                }
                .padding(.horizontal, Steward.S.hero).padding(.vertical, Steward.S.xxl)
                .frame(maxWidth: 720)
                .frame(maxWidth: .infinity)   // centre the column like the rest
            }
        }
        .sheet(isPresented: $showSave) {
            SaveContactSheet(identifier: decision.sender_identifier, suggestedName: "")
        }
    }

    private func sectionLabel(_ s: String) -> some View {
        Text(s).font(Steward.F.label).tracking(2).textCase(.uppercase)
            .foregroundColor(Steward.C.t3).padding(.bottom, 12)
    }
    /// In-flight / sent / failed send status, shown in place of the action buttons so a
    /// send is never a fire-and-forget the owner can't see (UX/trust #1).
    private func sendStatusRow(_ s: String) -> some View {
        Text(s).font(Steward.F.support).foregroundColor(Steward.C.t2)
            .frame(maxWidth: .infinity).padding(.vertical, 13)
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.10)))
            .padding(.top, Steward.S.xl)
    }
    private func actionButton(_ label: String, fill: Bool, quiet: Bool = false,
                              help: String = "", _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label).font(Steward.F.support)
                .foregroundColor(fill ? Steward.C.canvas : (quiet ? Steward.C.t3 : Steward.C.tx))
                .frame(maxWidth: .infinity).padding(.vertical, 13)
                .background(fill ? Steward.C.onLight : Color.clear)
                .overlay(RoundedRectangle(cornerRadius: 10)
                    .stroke(fill || quiet ? Color.clear : Color.white.opacity(0.16)))
                .clipShape(RoundedRectangle(cornerRadius: 10))
        }.buttonStyle(.plain).help(help)
    }
}

// MARK: - Handled detail (read-only: what arrived, what the AI did, the reply sent)
struct HandledDetailView: View {
    let detail: HandledDetail
    @ObservedObject var store = StewardStore.shared
    @State private var fbSent = false

    private var chips: some View {
        let items = [detail.who, detail.urgency, detail.undo, detail.confidence].filter { !$0.isEmpty }
        return HStack(spacing: 8) {
            ForEach(items, id: \.self) { c in
                Text(c).font(Steward.F.meta).foregroundColor(Steward.C.t2)
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .background(Steward.C.raised).clipShape(Capsule())
            }
        }.padding(.bottom, 14)
    }

    private func feedbackButton(_ label: String, _ thumbs: String) -> some View {
        Button { store.sendFeedback(detail.messageId, thumbs: thumbs); fbSent = true } label: {
            Text(label).font(Steward.F.meta).foregroundColor(Steward.C.tx)
                .padding(.horizontal, 18).padding(.vertical, 9)
                .overlay(RoundedRectangle(cornerRadius: 9).stroke(Color.white.opacity(0.16)))
        }.buttonStyle(.plain).disabled(fbSent)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { store.focusedHandled = nil } label: {
                HStack(spacing: 8) { Image(systemName: "chevron.left"); Text("Today").font(Steward.F.meta) }
                    .foregroundColor(Steward.C.t3)
            }.buttonStyle(.plain).padding(.horizontal, 28).padding(.vertical, 16)
            Divider().overlay(Steward.C.line)

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 10) {
                        Circle().fill(Steward.C.t3).frame(width: 8, height: 8)
                        Text(statusLine).font(Steward.F.meta).foregroundColor(Steward.C.t3)
                    }.padding(.bottom, 14)

                    Text(detail.subject.isEmpty ? detail.sender : detail.subject)
                        .font(Steward.F.titleLg).foregroundColor(Steward.C.tx).padding(.bottom, 10)
                    Text("From \(detail.sender.isEmpty ? "someone" : detail.sender)"
                         + (detail.category.isEmpty ? "" : " · \(detail.category)")
                         + (detail.channel.isEmpty ? "" : " · \(detail.channel)"))
                        .font(Steward.F.meta).foregroundColor(Steward.C.t3)
                        .padding(.bottom, Steward.S.xl)

                    if !detail.why.isEmpty {
                        label("What Steward figured out")
                        chips
                        Text(detail.why).font(Steward.F.body).foregroundColor(Steward.C.t2)
                            .lineSpacing(5).padding(.bottom, Steward.S.xl)
                    }
                    if !detail.quote.isEmpty {
                        label("What they said")
                        box("“\(detail.quote)”")
                    }
                    if !detail.reply.isEmpty {
                        label("The reply")
                        box(detail.reply)
                    } else {
                        Text("No reply was drafted — this was filed or handled quietly.")
                            .font(Steward.F.meta).foregroundColor(Steward.C.t3)
                            .padding(.bottom, Steward.S.xl)
                    }

                    label("Was this the right call?")
                    HStack(spacing: 10) {
                        feedbackButton("👍 Good", "up")
                        feedbackButton("👎 Off", "down")
                        if fbSent { Text("Thanks — noted.").font(Steward.F.meta).foregroundColor(Steward.C.t3) }
                    }
                }
                .padding(.horizontal, Steward.S.hero).padding(.vertical, Steward.S.xxl)
                .frame(maxWidth: 720)
                .frame(maxWidth: .infinity)   // centre the column like the rest
            }
        }
    }

    private var statusLine: String {
        detail.viaLabel.isEmpty ? detail.label : "\(detail.label) · \(detail.viaLabel)"
    }
    private func label(_ s: String) -> some View {
        Text(s).font(Steward.F.label).tracking(2).textCase(.uppercase)
            .foregroundColor(Steward.C.t3).padding(.bottom, 12)
    }
    private func box(_ s: String) -> some View {
        Text(s).font(Steward.F.body).foregroundColor(Steward.C.t2)
            .lineSpacing(5).padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Steward.C.raised).clipShape(RoundedRectangle(cornerRadius: 12))
            .padding(.bottom, Steward.S.xl)
    }
}

// MARK: - Preview section — shows the first `preview` rows always, expands to show all.
struct PreviewSection<Item: Identifiable, Row: View>: View {
    let title: String
    let tint: Color
    let items: [Item]
    let preview: Int
    let row: (Item) -> Row
    @State private var expanded = false

    init(title: String, tint: Color = Steward.C.t3, items: [Item], preview: Int = 5,
         @ViewBuilder row: @escaping (Item) -> Row) {
        self.title = title; self.tint = tint; self.items = items
        self.preview = preview; self.row = row
    }
    var body: some View {
        let shown = expanded ? items : Array(items.prefix(preview))
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Circle().fill(tint).frame(width: 6, height: 6)
                Text(title).font(Steward.F.label).tracking(2).textCase(.uppercase)
                    .foregroundColor(Steward.C.t3)
                Text("\(items.count)").font(Steward.F.meta).foregroundColor(Steward.C.t3)
                Spacer()
            }.padding(.vertical, 10)
            ForEach(shown) { row($0) }
            if items.count > preview {
                Button { withAnimation(.easeOut(duration: 0.2)) { expanded.toggle() } } label: {
                    HStack(spacing: 6) {
                        Text(expanded ? "Show less" : "Show all \(items.count)")
                            .font(Steward.F.meta).foregroundColor(Steward.C.t2)
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .font(.system(size: 10)).foregroundColor(Steward.C.t3)
                    }.padding(.vertical, 10)
                }.buttonStyle(.plain)
            }
        }
    }
}

// MARK: - Console (the full queue + stats, native — same line/editorial design)
struct ConsoleView: View {
    @ObservedObject var store = StewardStore.shared

    @State private var mode = 0   // 0 = Queue · 1 = Analytics

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("Console").font(Steward.F.hero(30)).foregroundColor(Steward.C.tx)
                Spacer()
                connectionStrip
            }
            .padding(.top, Steward.S.hero).padding(.bottom, Steward.S.lg)

            modeToggle.padding(.bottom, Steward.S.xl)

            if mode == 0 { queueSection } else { analyticsSection }
        }
        .padding(.horizontal, Steward.S.hero).padding(.bottom, Steward.S.xxl)
    }

    private var modeToggle: some View {
        HStack(spacing: 0) {
            segButton("Queue", 0)
            segButton("Analytics", 1)
        }
        .padding(3).background(Steward.C.surface).clipShape(RoundedRectangle(cornerRadius: 11))
    }
    private func segButton(_ title: String, _ i: Int) -> some View {
        Button { withAnimation(.easeOut(duration: 0.18)) { mode = i } } label: {
            Text(title).font(Steward.F.meta)
                .foregroundColor(mode == i ? Steward.C.canvas : Steward.C.t2)
                .padding(.horizontal, 20).padding(.vertical, 7)
                .background(mode == i ? Steward.C.onLight : Color.clear)
                .clipShape(RoundedRectangle(cornerRadius: 9))
        }.buttonStyle(.plain)
    }

    @ViewBuilder private var queueSection: some View {
        HStack(spacing: Steward.S.sm) {
            stat("\(store.stats.conversations)", "Conversations")
            stat("\(store.decisions.count)", "Waiting")
            stat("\(store.stats.sent)", "Replies sent")
            stat("\(store.stats.flagged)", "Flagged")
        }.padding(.bottom, Steward.S.xl)

        if !store.decisions.isEmpty {
            PreviewSection(title: "Waiting for you", tint: Steward.C.amber,
                           items: store.decisions, preview: 5) { d in
                row(initial: cap(d.sender), title: d.title,
                    subtitle: "\(d.category) · \(d.channel)",
                    trailing: d.tier >= 3 ? "Needs you" : "When you can",
                    tint: Steward.accent(forTier: d.tier)) { store.focused = d }
            }
            Spacer().frame(height: Steward.S.sm)
        }
        if !store.handledItems.isEmpty {
            PreviewSection(title: "Handled", items: store.handledItems, preview: 5) { h in
                row(initial: cap(h.sender), title: h.sender,
                    subtitle: [h.label, h.via_label ?? ""].filter { !$0.isEmpty }.joined(separator: " · "),
                    trailing: "", tint: Steward.C.t3) { store.openHandled(h) }
            }
        }
        if store.decisions.isEmpty && store.handledItems.isEmpty {
            Text("Nothing yet — new mail and messages will appear here.")
                .font(Steward.F.body).foregroundColor(Steward.C.t3).padding(.top, Steward.S.xl)
        }
    }

    @ViewBuilder private var analyticsSection: some View {
        pipelineStrip.padding(.bottom, Steward.S.xl)

        if !store.analytics.daily.isEmpty {
            groupLabel("Volume — last 30 days", Steward.C.t3)
            volumeChart(store.analytics.daily)
            legend.padding(.bottom, Steward.S.xl)

            groupLabel("Your feedback", Steward.C.t3)
            HStack(spacing: Steward.S.sm) {
                stat("\(store.analytics.approve)", "Approved")
                stat("\(store.analytics.edit)", "Edited")
                stat("\(store.analytics.skip)", "Skipped")
                stat("\(Int(store.analytics.approvalRate * 100))%", "Approval rate")
            }.padding(.bottom, Steward.S.xl)
        }

        if !store.activity.isEmpty {
            PreviewSection(title: "Recent activity", items: store.activity, preview: 5) { a in
                HStack(spacing: 8) {
                    Text(a.what).font(Steward.F.meta).foregroundColor(Steward.C.t2)
                    Text(a.detail).font(Steward.F.meta).foregroundColor(Steward.C.t3).lineLimit(1)
                    Spacer(minLength: 8)
                    Text(ago(a.at)).font(Steward.F.meta).foregroundColor(Steward.C.t3)
                }.padding(.vertical, 7)
            }
        }
    }

    private func cap(_ s: String) -> String { String((s.first ?? "?")).uppercased() }
    private func ago(_ e: Double) -> String {
        guard e > 0 else { return "" }
        let s = Int(Date().timeIntervalSince1970 - e)
        if s < 60 { return "just now" }; if s < 3600 { return "\(s/60)m ago" }
        if s < 86400 { return "\(s/3600)h ago" }; return "\(s/86400)d ago"
    }

    // Connection-health pills (Email / Telegram / WhatsApp).
    private var connectionStrip: some View {
        HStack(spacing: 8) {
            connPill("Email", store.connGmail)
            connPill("Telegram", store.connTelegram)
            connPill("WhatsApp", store.connWhatsApp)
        }
    }
    private func connPill(_ name: String, _ on: Bool) -> some View {
        HStack(spacing: 6) {
            Circle().fill(on ? Steward.C.blue : Steward.C.t3).frame(width: 7, height: 7)
            Text(name).font(Steward.F.meta).foregroundColor(on ? Steward.C.t2 : Steward.C.t3)
        }
        .padding(.horizontal, 12).padding(.vertical, 6)
        .background(Steward.C.surface).clipShape(Capsule())
    }

    // The processing checkpoints a message moves through.
    private var pipelineStrip: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(store.pipeline.stages.enumerated()), id: \.offset) { i, s in
                HStack(spacing: 8) {
                    Circle().fill(store.pipeline.busy && i == 2 ? Steward.C.amber : Steward.C.t3.opacity(0.6))
                        .frame(width: 6, height: 6)
                    Text(s).font(Steward.F.meta).foregroundColor(Steward.C.t3)
                }
            }
            if !store.pipeline.lastWho.isEmpty {
                Text("Last: \(store.pipeline.lastWho) — \(store.pipeline.lastLabel)")
                    .font(Steward.F.meta).foregroundColor(Steward.C.t2).padding(.top, 4)
            }
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(Steward.C.surface).clipShape(RoundedRectangle(cornerRadius: 12))
    }

    // Stacked volume-by-tier bars, one column per day. Monochrome + tier accents.
    private func volumeChart(_ days: [DailyMetric]) -> some View {
        let maxTotal = max(1, days.map { $0.total }.max() ?? 1)
        return HStack(alignment: .bottom, spacing: 6) {
            ForEach(days) { d in
                VStack(spacing: 2) {
                    Spacer(minLength: 0)
                    bar(d.t3, Steward.C.amber, maxTotal)
                    bar(d.t2, Steward.C.blue, maxTotal)
                    bar(d.t1, Steward.C.t2.opacity(0.7), maxTotal)
                    bar(d.t0, Steward.C.t3.opacity(0.5), maxTotal)
                }
                .frame(width: 26)   // fixed-width columns → reads as a bar chart, even sparse
            }
            Spacer(minLength: 0)    // left-align when there are few days
        }
        .frame(height: 130)
    }
    private func bar(_ v: Int, _ c: Color, _ maxTotal: Int) -> some View {
        RoundedRectangle(cornerRadius: 2).fill(c)
            .frame(height: v <= 0 ? 0 : max(2, CGFloat(v) / CGFloat(maxTotal) * 112))
    }
    private var legend: some View {
        HStack(spacing: Steward.S.md) {
            legendDot(Steward.C.amber, "Asked you")
            legendDot(Steward.C.blue, "Drafted")
            legendDot(Steward.C.t2.opacity(0.7), "Told you")
            legendDot(Steward.C.t3.opacity(0.5), "Filed")
        }
    }
    private func legendDot(_ c: Color, _ s: String) -> some View {
        HStack(spacing: 6) {
            RoundedRectangle(cornerRadius: 2).fill(c).frame(width: 9, height: 9)
            Text(s).font(Steward.F.meta).foregroundColor(Steward.C.t3)
        }
    }

    private func stat(_ v: String, _ l: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(v).font(.system(size: 24, weight: .light)).foregroundColor(Steward.C.tx)
            Text(l).font(Steward.F.label).foregroundColor(Steward.C.t3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 14).padding(.horizontal, 16)
        .background(Steward.C.surface).clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private func groupLabel(_ s: String, _ tint: Color) -> some View {
        HStack(spacing: 8) {
            Circle().fill(tint).frame(width: 6, height: 6)
            Text(s).font(Steward.F.label).tracking(2).textCase(.uppercase).foregroundColor(Steward.C.t3)
        }.padding(.top, Steward.S.md).padding(.bottom, 4)
    }

    private func row(initial: String, title: String, subtitle: String,
                     trailing: String, tint: Color, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: Steward.S.md) {
                Circle().fill(Steward.C.raised).frame(width: 32, height: 32)
                    .overlay(Text(initial).font(Steward.F.meta).foregroundColor(Steward.C.t2))
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(Steward.F.title).foregroundColor(Steward.C.tx).lineLimit(1)
                    if !subtitle.isEmpty {
                        Text(subtitle).font(Steward.F.meta).foregroundColor(Steward.C.t3).lineLimit(1)
                    }
                }
                Spacer(minLength: 8)
                if !trailing.isEmpty {
                    Text(trailing).font(Steward.F.meta).foregroundColor(tint)
                        .padding(.horizontal, 14).padding(.vertical, 6)
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(tint.opacity(0.4)))
                }
            }
            .padding(.vertical, 14).padding(.horizontal, 8)
            .contentShape(Rectangle())
            .hoverHighlight(radius: 12)
        }.buttonStyle(.plain)
    }
}

// MARK: - People (editable — set VIP)
struct PeopleView: View {
    @ObservedObject var store = StewardStore.shared
    var body: some View {
        let all = store.peopleStates()
        VStack(alignment: .leading, spacing: 0) {
            Text("People it knows").font(Steward.F.label).tracking(2).textCase(.uppercase)
                .foregroundColor(Steward.C.t3).padding(.bottom, Steward.S.xs)
            Text("Flip VIP on and Steward always asks you before acting for that person.")
                .font(Steward.F.meta).foregroundColor(Steward.C.t3).padding(.bottom, Steward.S.md)
            if all.isEmpty {
                emptyGuide("No one yet",
                           "People you talk to most will appear here — set who's a VIP.")
            }
            ForEach(all) { ps in EditablePersonRow(ps: ps) }
        }
        .padding(.horizontal, Steward.S.hero).padding(.vertical, Steward.S.xl)
    }
}

/// One contact row with a live VIP toggle + Save (only when changed). Unsaved/unknown
/// senders get an "Unknown" chip and a "Save contact" action that names them for good.
struct EditablePersonRow: View {
    let ps: PersonState
    @State private var vip: Bool
    @State private var showSave = false
    init(ps: PersonState) { self.ps = ps; _vip = State(initialValue: ps.person.is_vip) }
    var body: some View {
        HStack(spacing: 14) {
            Circle().fill(Steward.C.raised).frame(width: 42, height: 42)
                .overlay(Text(String(ps.person.name.prefix(1)).uppercased())
                    .font(Steward.F.meta).foregroundColor(Steward.C.t2))
            VStack(alignment: .leading, spacing: 2) {
                Text(ps.person.name).font(.system(size: 17)).foregroundColor(Steward.C.tx)
                HStack(spacing: 6) {
                    if ps.tier > 0 { Circle().fill(Steward.accent(forTier: ps.tier)).frame(width: 6, height: 6) }
                    Text(ps.state).font(Steward.F.meta).foregroundColor(Steward.C.t3)
                    if !ps.person.is_saved {
                        Text("UNKNOWN").font(Steward.F.label).tracking(1)
                            .foregroundColor(Steward.C.amber)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .overlay(RoundedRectangle(cornerRadius: 5)
                                .stroke(Steward.C.amber.opacity(0.4), lineWidth: 1))
                    }
                }
                // Every number / email / WhatsApp id this person has — grouped under ONE name,
                // so the same human is never shown as two contacts.
                if ps.person.is_saved {
                    let labels = ps.person.handles.map { $0.label }.filter { !$0.isEmpty }
                    if labels.count > 1 || (labels.count == 1 && labels[0] != "WhatsApp") {
                        Text(labels.joined(separator: "  ·  "))
                            .font(Steward.F.label).foregroundColor(Steward.C.t3.opacity(0.85))
                            .lineLimit(1).truncationMode(.middle)
                    }
                }
            }
            Spacer()
            if !ps.person.is_saved {
                Button { showSave = true } label: {
                    Text("Save contact").font(Steward.F.meta).foregroundColor(Steward.C.canvas)
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(Steward.C.onLight).clipShape(RoundedRectangle(cornerRadius: 8))
                }.buttonStyle(.plain)
                .help("Name this person and save them, so Steward stops treating them as unknown.")
            }
            Text("VIP").font(Steward.F.meta).foregroundColor(vip ? Steward.C.amber : Steward.C.t3)
            Toggle("", isOn: $vip).labelsHidden().toggleStyle(.switch)
            if vip != ps.person.is_vip {
                Button { StewardStore.shared.contactUpdate(ps.person.email, importance: ps.person.importance, vip: vip) } label: {
                    Text("Save").font(Steward.F.meta).foregroundColor(Steward.C.canvas)
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(Steward.C.onLight).clipShape(RoundedRectangle(cornerRadius: 8))
                }.buttonStyle(.plain)
            }
        }
        .padding(.vertical, 12).padding(.horizontal, 8)
        .hoverHighlight(radius: 10)
        .sheet(isPresented: $showSave) {
            SaveContactSheet(identifier: ps.person.email,
                             suggestedName: ps.person.is_saved ? ps.person.name : "")
        }
    }
}

/// "This is an unsaved contact — what would you like to name it?" A name (required) and an
/// optional phone number that bridges a WhatsApp @lid to the real number so future messages
/// resolve to this person. Saving only writes recognition state — it never sends anything.
struct SaveContactSheet: View {
    let identifier: String
    let suggestedName: String
    @Environment(\.dismiss) private var dismiss
    @State private var name: String = ""
    @State private var phone: String = ""
    @State private var email: String = ""
    @State private var saving = false
    @State private var failed = false

    private var isWhatsApp: Bool { identifier.contains("@lid") || identifier.contains("@s.whatsapp.net") }

    var body: some View {
        VStack(alignment: .leading, spacing: Steward.S.lg) {
            Text("Save this contact").font(.system(size: 18, weight: .semibold))
                .foregroundColor(Steward.C.tx)
            Text("Steward doesn't recognize this sender yet. Give them a name and it will stop "
                 + "treating them as unknown.").font(Steward.F.meta).foregroundColor(Steward.C.t3)
                .fixedSize(horizontal: false, vertical: true)

            VStack(alignment: .leading, spacing: 6) {
                Text("NAME").font(Steward.F.label).tracking(1).foregroundColor(Steward.C.t3)
                TextField("e.g. Nathan Diniz", text: $name).textFieldStyle(.roundedBorder)
            }
            if isWhatsApp {
                VStack(alignment: .leading, spacing: 6) {
                    Text("PHONE NUMBER (OPTIONAL)").font(Steward.F.label).tracking(1)
                        .foregroundColor(Steward.C.t3)
                    TextField("e.g. +1 415 555 0199", text: $phone).textFieldStyle(.roundedBorder)
                    Text("Links this WhatsApp ID to a number so future messages are recognized too.")
                        .font(Steward.F.meta).foregroundColor(Steward.C.t3)
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("EMAIL (OPTIONAL)").font(Steward.F.label).tracking(1)
                        .foregroundColor(Steward.C.t3)
                    TextField("e.g. nathan@company.com", text: $email).textFieldStyle(.roundedBorder)
                    Text("Connects their email to the same person, so email + WhatsApp are one contact.")
                        .font(Steward.F.meta).foregroundColor(Steward.C.t3)
                }
            }
            if failed {
                Text("Couldn't save — is the engine running?").font(Steward.F.meta)
                    .foregroundColor(Steward.C.amber)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.buttonStyle(.plain).foregroundColor(Steward.C.t3)
                Button {
                    saving = true; failed = false
                    StewardStore.shared.saveContact(
                        identifier: identifier,
                        name: name.trimmingCharacters(in: .whitespacesAndNewlines),
                        phone: phone.trimmingCharacters(in: .whitespacesAndNewlines),
                        email: email.trimmingCharacters(in: .whitespacesAndNewlines)
                    ) { ok in
                        saving = false
                        if ok { dismiss() } else { failed = true }
                    }
                } label: {
                    Text(saving ? "Saving…" : "Save contact").foregroundColor(Steward.C.canvas)
                        .padding(.horizontal, 14).padding(.vertical, 7)
                        .background(Steward.C.onLight).clipShape(RoundedRectangle(cornerRadius: 8))
                }.buttonStyle(.plain)
                .disabled(saving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .opacity(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.5 : 1)
            }
        }
        .padding(Steward.S.xl).frame(width: 380)
        .background(Steward.C.canvas)
        .onAppear { if name.isEmpty { name = suggestedName } }
    }
}

// MARK: - Commitments (grouped by urgency, with Done / Snooze — server-backed)
struct CommitmentsView: View {
    @ObservedObject var store = StewardStore.shared

    private var today: Date { Calendar.current.startOfDay(for: Date()) }

    /// Open commitments split into Overdue / Coming up / No date yet, each sorted sensibly.
    private var sections: [(String, Color, [Commitment])] {
        var overdue: [Commitment] = [], soon: [Commitment] = [], undated: [Commitment] = []
        for c in store.commitments {
            if let d = c.due {
                if d < today { overdue.append(c) } else { soon.append(c) }
            } else {
                undated.append(c)
            }
        }
        overdue.sort { ($0.due ?? .distantPast) < ($1.due ?? .distantPast) }
        soon.sort { ($0.due ?? .distantFuture) < ($1.due ?? .distantFuture) }
        undated.sort { ($0.created_at ?? 0) > ($1.created_at ?? 0) }
        var out: [(String, Color, [Commitment])] = []
        if !overdue.isEmpty { out.append(("Overdue", Steward.C.amber, overdue)) }
        if !soon.isEmpty { out.append(("Coming up", Steward.C.t2, soon)) }
        if !undated.isEmpty { out.append(("No date yet", Steward.C.t3, undated)) }
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Text("Commitments").font(Steward.F.label).tracking(2).textCase(.uppercase)
                    .foregroundColor(Steward.C.t3)
                Image(systemName: "info.circle").font(.system(size: 12)).foregroundColor(Steward.C.t3)
                    .help("Promises Steward heard — yours and theirs — so nothing slips.")
            }.padding(.bottom, Steward.S.lg)

            if store.commitments.isEmpty {
                emptyGuide("Nothing outstanding",
                           "Promises you make (and ones made to you) will land here, grouped by when they're due. Mark them done or snooze them as you go.")
            } else {
                ForEach(sections, id: \.0) { (label, tint, items) in
                    sectionHeader(label, tint, items.count)
                    ForEach(items) { row($0) }
                }
            }
        }
        .padding(.horizontal, Steward.S.hero).padding(.vertical, Steward.S.xl)
        .frame(maxWidth: 760).frame(maxWidth: .infinity)
    }

    private func sectionHeader(_ label: String, _ tint: Color, _ n: Int) -> some View {
        HStack(spacing: 7) {
            Circle().fill(tint).frame(width: 6, height: 6)
            Text(label).font(Steward.F.label).tracking(2).textCase(.uppercase).foregroundColor(Steward.C.t3)
            Text("\(n)").font(Steward.F.label).foregroundColor(Steward.C.t3)
        }.padding(.top, Steward.S.lg).padding(.bottom, 4)
    }

    private func row(_ c: Commitment) -> some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(c.promise).font(Steward.F.body).foregroundColor(Steward.C.tx)
                    .fixedSize(horizontal: false, vertical: true)
                if let sub = subtitle(c) {
                    Text(sub).font(Steward.F.meta).foregroundColor(Steward.C.t3)
                }
            }
            Spacer(minLength: 12)
            Text(dueLabel(c)).font(Steward.F.meta).foregroundColor(dueTint(c)).fixedSize()
            HStack(spacing: 8) {
                miniBtn("Done") { store.commitmentDone(c.id) }
                miniBtn("Snooze") { store.commitmentSnooze(c.id, days: 2) }
            }
        }
        .padding(.vertical, 14).padding(.horizontal, 8)
        .hoverHighlight(radius: 10)
    }

    /// "with <person> · promised 3d ago" — context the old flat list never showed.
    private func subtitle(_ c: Commitment) -> String? {
        var parts: [String] = []
        if let p = c.person, !p.isEmpty { parts.append("with \(p)") }
        if let ts = c.created_at, ts > 0 {
            let when = Self.rel.localizedString(for: Date(timeIntervalSince1970: Double(ts)), relativeTo: Date())
            parts.append("promised \(when)")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
    private func dueLabel(_ c: Commitment) -> String {
        guard let d = c.due else { return "No date" }
        return d < today ? "Overdue · \(Self.short.string(from: d))" : Self.short.string(from: d)
    }
    private func dueTint(_ c: Commitment) -> Color {
        guard let d = c.due else { return Steward.C.t3 }
        return d < today ? Steward.C.amber : Steward.C.t2
    }
    private func miniBtn(_ label: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label).font(Steward.F.meta).foregroundColor(Steward.C.tx)
                .padding(.horizontal, 12).padding(.vertical, 6)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.white.opacity(0.16)))
        }.buttonStyle(.plain)
    }
    static let rel: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter(); f.unitsStyle = .abbreviated; return f
    }()
    static let short: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "EEE, MMM d"; return f
    }()
}

// MARK: - Settings (channels + Teach + Rules — the native control room)
struct TrustCenterView: View {
    @ObservedObject var engine = EngineController.shared
    @ObservedObject var cfg = AppConfig.shared
    @ObservedObject var store = StewardStore.shared
    @State private var teachText = ""
    @State private var teachReply = ""
    @State private var teaching = false
    @State private var tSender = ""
    @State private var tSubject = ""
    @State private var tBody = ""
    @State private var tResult = ""
    @State private var aboutText = ""
    @State private var aboutLoaded = false
    @State private var aboutSaved = false

    var body: some View {
        VStack(alignment: .leading, spacing: Steward.S.lg) {
            Text("Settings").font(Steward.F.label).tracking(2).textCase(.uppercase).foregroundColor(Steward.C.t3)
            Text(store.paused ? "Steward is paused." : "Steward is watching quietly.")
                .font(Steward.F.hero(28)).foregroundColor(Steward.C.tx)

            // The real on/off — pause/resume (works regardless of how the engine launched).
            row("Steward — agent on", binding: Binding(get: { !store.paused },
                                                       set: { store.setPaused(!$0) }))
            row("Email", binding: Binding(get: { cfg.emailEnabled }, set: { engine.setEmail($0) }))
            row("WhatsApp", binding: Binding(get: { cfg.whatsappEnabled }, set: { engine.setWhatsApp($0) }))

            // ── About you — owner self-context that shapes how Steward prioritizes ──
            Divider().overlay(Steward.C.line).padding(.vertical, Steward.S.xs)
            sectionLabel("About you")
            Text("Tell Steward who you are and what matters to you. It uses this to judge what's "
                 + "important — e.g. “I'm job-hunting, so recruiter emails are high priority.” "
                 + "It can raise priority for what matters to you; it can never hide anything truly "
                 + "critical. Takes effect on your next message.")
                .font(Steward.F.meta).foregroundColor(Steward.C.t3).fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 6)
            TextEditor(text: $aboutText)
                .font(Steward.F.body).foregroundColor(Steward.C.tx)
                .scrollContentBackground(.hidden)
                .frame(minHeight: 110)
                .padding(10).background(Steward.C.raised).clipShape(RoundedRectangle(cornerRadius: 10))
                .onChange(of: aboutText) { _ in aboutSaved = false }
            HStack(spacing: 12) {
                Button {
                    store.saveOwnerAbout(aboutText) { ok in aboutSaved = ok }
                } label: {
                    Text("Save").font(Steward.F.meta).foregroundColor(Steward.C.canvas)
                        .padding(.horizontal, 18).padding(.vertical, 9)
                        .background(Steward.C.onLight).clipShape(RoundedRectangle(cornerRadius: 9))
                }.buttonStyle(.plain)
                .disabled(aboutText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                if aboutSaved {
                    Text("Saved ✓").font(Steward.F.meta).foregroundColor(Steward.C.t2)
                }
            }
            .onAppear {
                if !aboutLoaded {
                    aboutLoaded = true
                    store.loadOwnerAbout { txt in aboutText = txt }
                }
            }

            // ── Analytics (TODAY — last 24h, all real counts; "saved" is an estimate) ──
            Divider().overlay(Steward.C.line).padding(.vertical, Steward.S.xs)
            sectionLabel("Analytics · last 24h")
            // Honesty: when the engine is unreachable, hide the grid rather than render a wall
            // of zeros that looks like real data (a true quiet day must not look like an outage).
            if store.reachable {
                let t = store.trust
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                    analytic("\(t.processed)", "processed")
                    analytic("\(t.autoHandled)", "handled quietly")
                    analytic("\(t.escalated)", "flagged you")
                    analytic("\(t.suppressed)", "suppressed")
                    analytic("\(t.approvals)", "approvals asked")
                    analytic("\(min(100, Int(t.approvalRate * 100)))%", "approval rate")
                    analytic("\(Int(t.draftAcceptance * 100))%", "draft accepted")
                    analytic("\(t.decisionsAvoided)", "decisions avoided")
                    analytic(t.hoursSaved >= 1 ? "~\(t.hoursSaved)h" : "\(t.savedMinutes)m", "saved (est.)")
                    analytic("\(t.memoryUpdates)", "memory updates")
                    analytic("\(t.commitmentsOpen)", "open promises")
                    analytic("\(store.stats.sent)", "sent (all-time)")
                }
            } else {
                HStack(spacing: 8) {
                    Image(systemName: "wifi.exclamationmark").font(.system(size: 12)).foregroundColor(Steward.C.t3)
                    Text("Can't reach the engine — analytics hidden so you don't see false zeros.")
                        .font(Steward.F.meta).foregroundColor(Steward.C.t3)
                }.padding(.vertical, 12)
            }

            // ── Teach Steward ──────────────────────────────────────────
            Divider().overlay(Steward.C.line).padding(.vertical, Steward.S.xs)
            sectionLabel("Teach Steward")
            Text("Tell it in plain English — e.g. “never reply to my landlord without me”, “treat Acme as high importance”.")
                .font(Steward.F.meta).foregroundColor(Steward.C.t3).padding(.bottom, 6)
            TextField("Tell Steward…", text: $teachText)
                .textFieldStyle(.plain).foregroundColor(Steward.C.tx)
                .padding(12).background(Steward.C.raised).clipShape(RoundedRectangle(cornerRadius: 10))
                .onSubmit(submitTeach)
            HStack(spacing: 12) {
                Button(action: submitTeach) {
                    Text(teaching ? "Thinking…" : "Tell Steward").font(Steward.F.meta)
                        .foregroundColor(Steward.C.canvas)
                        .padding(.horizontal, 18).padding(.vertical, 9)
                        .background(Steward.C.onLight).clipShape(RoundedRectangle(cornerRadius: 9))
                }.buttonStyle(.plain).disabled(teaching || teachText.trimmingCharacters(in: .whitespaces).isEmpty)
                if !teachReply.isEmpty {
                    Text(teachReply).font(Steward.F.meta).foregroundColor(Steward.C.t2).lineLimit(2)
                }
            }

            // ── Rules ──────────────────────────────────────────────────
            if !store.proposedRules.isEmpty {
                sectionLabel("Proposed — needs your OK").padding(.top, Steward.S.sm)
                ForEach(store.proposedRules) { r in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(r.rule).font(Steward.F.support).foregroundColor(Steward.C.tx)
                        if let ev = r.evidence, !ev.isEmpty {
                            Text("Why: \(ev)").font(Steward.F.meta).foregroundColor(Steward.C.t3)
                        }
                        HStack(spacing: 10) {
                            Button { store.ruleConfirm(r.id) } label: {
                                Text("Confirm").font(Steward.F.meta).foregroundColor(Steward.C.canvas)
                                    .padding(.horizontal, 14).padding(.vertical, 7)
                                    .background(Steward.C.onLight).clipShape(RoundedRectangle(cornerRadius: 8))
                            }.buttonStyle(.plain)
                            Button { store.ruleReject(r.id) } label: {
                                Text("Reject").font(Steward.F.meta).foregroundColor(Steward.C.amber)
                            }.buttonStyle(.plain)
                        }
                    }
                    .padding(14).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Steward.C.surface).clipShape(RoundedRectangle(cornerRadius: 12))
                }
            }
            if !store.rules.isEmpty {
                sectionLabel("Active rules").padding(.top, Steward.S.sm)
                ForEach(store.rules) { r in
                    HStack(spacing: 10) {
                        Text(r.rule).font(Steward.F.meta).foregroundColor(Steward.C.t2)
                        Spacer()
                        Text((r.learned ?? false) ? "Learned" : "You set").font(Steward.F.label).foregroundColor(Steward.C.t3)
                        Button { store.ruleDelete(r.id) } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(Steward.F.meta).foregroundColor(Steward.C.t3)
                        }
                        .buttonStyle(.plain)
                        .help("Remove this rule")
                    }.padding(.vertical, 8)
                }
            }

            // ── Test the brain ─────────────────────────────────────────
            Divider().overlay(Steward.C.line).padding(.vertical, Steward.S.xs)
            sectionLabel("Test the brain")
            Text("Run a hypothetical message through Steward — zero side effects.")
                .font(Steward.F.meta).foregroundColor(Steward.C.t3).padding(.bottom, 6)
            testField("From (name or email)", $tSender)
            testField("Subject", $tSubject)
            testField("Message…", $tBody)
            HStack(spacing: 12) {
                Button {
                    store.testBrain(sender: tSender, subject: tSubject, body: tBody) { r in
                        let label = (r["final_label"] as? String) ?? "—"
                        let cat = (r["category"] as? String) ?? ""
                        let conf = (r["confidence"] as? String) ?? ""
                        tResult = "Would: \(label)  ·  \(cat)  ·  \(conf)"
                    }
                } label: {
                    Text("Run it").font(Steward.F.meta).foregroundColor(Steward.C.canvas)
                        .padding(.horizontal, 18).padding(.vertical, 9)
                        .background(Steward.C.onLight).clipShape(RoundedRectangle(cornerRadius: 9))
                }.buttonStyle(.plain).disabled(tBody.trimmingCharacters(in: .whitespaces).isEmpty)
                if !tResult.isEmpty { Text(tResult).font(Steward.F.meta).foregroundColor(Steward.C.t2) }
            }

            // ── Voice profiles ─────────────────────────────────────────
            Divider().overlay(Steward.C.line).padding(.vertical, Steward.S.xs)
            HStack {
                sectionLabel("Voice profiles")
                Spacer()
                Button { store.rebuildVoice() } label: {
                    Text("Rebuild").font(Steward.F.meta).foregroundColor(Steward.C.t2)
                }.buttonStyle(.plain)
            }
            ForEach(store.voice) { v in
                VStack(alignment: .leading, spacing: 6) {
                    Text("\(v.segment) · \(v.sample_count) samples")
                        .font(Steward.F.support).foregroundColor(Steward.C.tx)
                    ForEach(v.examples.prefix(3), id: \.self) { ex in
                        Text("“\(ex)”").font(Steward.F.meta).foregroundColor(Steward.C.t3).lineLimit(1)
                    }
                }
                .padding(14).frame(maxWidth: .infinity, alignment: .leading)
                .background(Steward.C.surface).clipShape(RoundedRectangle(cornerRadius: 12))
            }

            Button { OnboardingWindow.shared.show() } label: {
                HStack { Text("Setup & accounts").font(Steward.F.support).foregroundColor(Steward.C.t2)
                    Spacer(); Image(systemName: "chevron.right").foregroundColor(Steward.C.t3) }
            }.buttonStyle(.plain).padding(.top, Steward.S.sm)
        }
        .padding(.horizontal, Steward.S.hero).padding(.vertical, Steward.S.xl)
        .onAppear { store.loadRules() }
    }

    private func testField(_ placeholder: String, _ text: Binding<String>) -> some View {
        TextField(placeholder, text: text)
            .textFieldStyle(.plain).foregroundColor(Steward.C.tx)
            .padding(10).background(Steward.C.raised).clipShape(RoundedRectangle(cornerRadius: 8))
            .padding(.bottom, 6)
    }

    private func submitTeach() {
        let t = teachText.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, !teaching else { return }
        teaching = true; teachReply = ""
        store.teach(t) { reply in teachReply = reply; teaching = false; teachText = "" }
    }
    private func sectionLabel(_ s: String) -> some View {
        Text(s).font(Steward.F.label).tracking(2).textCase(.uppercase).foregroundColor(Steward.C.t3)
    }
    private func analytic(_ v: String, _ l: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(v).font(.system(size: 20, weight: .light)).foregroundColor(Steward.C.tx)
                .lineLimit(1).minimumScaleFactor(0.7)
            Text(l).font(Steward.F.label).foregroundColor(Steward.C.t3).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12).background(Steward.C.surface).clipShape(RoundedRectangle(cornerRadius: 10))
    }
    private func row(_ label: String, binding: Binding<Bool>) -> some View {
        HStack { Text(label).font(Steward.F.support).foregroundColor(Steward.C.tx); Spacer()
            Toggle("", isOn: binding).labelsHidden().toggleStyle(.switch) }
            .padding(.vertical, 8)
    }
}

private func emptyGuide(_ title: String, _ message: String) -> some View {
    VStack(alignment: .leading, spacing: 8) {
        Text(title).font(Steward.F.body).foregroundColor(Steward.C.t2)
        Text(message).font(Steward.F.meta).foregroundColor(Steward.C.t3)
            .frame(maxWidth: 420, alignment: .leading).lineSpacing(3)
    }.padding(.vertical, Steward.S.xl)
}
