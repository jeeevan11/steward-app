# Steward — Complete Experience Redesign

A steward of human attention. Not a dashboard, not an inbox, not an AI control panel.
The interface exists to make one belief true: *if something matters, Steward will tell me.*

North star, stated once and obeyed everywhere: **show significance, never activity.**

---

## 1. Information architecture

The whole product reorganizes from `Message → Thread → Inbox` to **`Decision → Person → Commitment`**. Three nouns. Nothing else is a first-class object.

```
Steward
├─ Menu bar  ……………… the true home (a trusted system service)
│   └─ Briefing popover  → "what needs me right now?"  → Review
│
└─ App window (rarely opened — for trust, review, reflection)
    ├─ Home …………………… editorial hero: the state of your attention
    ├─ Decisions ……………… P1  one card per decision → single-focus detail
    ├─ People ………………… P2  relationships and their current state
    ├─ Commitments …………… P3  promises, deadlines, obligations (external memory)
    └─ Trust Center ………… P4  collapsed by default: why / confidence / replay / health
```

Strict priority, never violated: **Decisions (1) › People (2) › Commitments (3) › Trust (4).**

Everything deleted from the old UI: *Assistant Running, Agent Active, Live Mode, Connected, Messages Processed, Pending/Handled/Unread Count, Relay Status, Infrastructure Status.* These are implementation; the owner only sees outcomes. (The numbers still exist — they live in Trust Center, off the daily path.)

The unit of everything the owner sees is a **Decision**: a title, one human sentence, and a way to resolve it. Never a message, thread, email, or chat.

---

## 2–8. Wireframes

The five rendered mockups above are the canonical wireframes for the menu bar, dashboard home, decision detail, widgets + empty state, and people + commitments. In text, for completeness:

**Menu bar icon** — a single ring that fills with significance, nothing else:
`○ calm · ◔ one waiting · ◑ several · ● important (warm)`. No number, no badge, no counter.

**Briefing popover** — answers one question:
```
Steward
Two things need your attention.
──────────────────────────────
●  Investor follow-up      You promised an update six days ago.
●  University application   A decision is needed by tomorrow.
──────────────────────────────
                [ Review ]
```
Calm state collapses to `Steward / Everything handled. / Review decisions · Settings`.

**Trust Center** (not rendered above — collapsed by default; one disclosure per decision):
```
Why this reached you                                         ⌄
──────────────────────────────────────────────────────────────
  Because you promised Daniel an update and six days passed.
  Confidence            high
  Remembered            "I'll send Q2 numbers early next week" — Jun 4
  What I handled        37 messages filed quietly today
  ──────────────────────────────────────────────────────────
  Replay reasoning   ·   Models & cost   ·   System health
```
It is the only place words like *confidence, model, evaluation, health* ever appear. Purpose: trust, not daily use.

---

## 9. Motion

Motion communicates confidence, never seeks attention.

| Moment | Motion | Duration | Easing |
|---|---|---|---|
| Popover open | fade + 4px rise + 0.98→1 scale | 240ms | ease-out `cubic-bezier(.2,0,0,1)` |
| Screen change | depth cross-fade (outgoing −8px/0.96, incoming opposite) | 340ms | ease-out |
| Decision resolved | card fades, list closes the gap | 300ms | ease-in-out |
| Significance arrives | menu-bar mark fills, once | 600ms | ease-out |
| Hover/focus | opacity/border only | 200ms | ease-out |

Banned: bounce, spring overshoot, pulse, blink, slide-in toasts, any looping/attention-seeking animation. Nothing ever moves to be noticed. The empty state does not animate — stillness is the reward.

---

## 10. Typography

**SF Pro** throughout (Display ≥ 24px, Text < 24px). Sentence case everywhere. Three weights only: **Light 300** (heroes, calm statements), **Regular 400** (body, titles), **Medium 500** (the single primary action — used sparingly).

| Role | Size | Weight | Tracking / leading |
|---|---|---|---|
| Dashboard hero | 42px (→48 on large displays) | 300 | -0.01em / 1.18 |
| Decision title (detail) | 30px | 300 | -0.01em |
| Decision title (card) | 22px | 400 | -0.01em |
| Popover hero | 20–23px | 300 | 1.3 |
| Context / body | 17px | 300 | 1.6 |
| Supporting line | 15px | 400 | 1.5 |
| Meta / row value | 13–14px | 400 | — |
| Section label | 12px | 400 | 0.14em, uppercase |

Never below 11px. Large type + large whitespace = editorial composition, not dashboard composition.

---

## 11. Color

95% monochrome, warm. Accent appears **only on a decision's significance** — a single 7–8px dot, never fills, borders, or backgrounds.

**Dark (primary):**
- Canvas `#1B1A18` · Surface `#232120` · Raised `#26221F`
- Hairline `rgba(255,255,255,.08)`
- Text: primary `#F2EFE9` · secondary `#A7A399` · tertiary `#6B675E`
- Tier 2 (decide when you can) — soft blue `#7FA6CE`
- Tier 3 (decide soon / overdue promise) — soft amber `#D4AD77`
- Primary action: fill `#EFECE6`, text `#1B1A18`

**Light (derived):** Canvas `#F4F1EB` (warm paper) · Surface `#FBF9F4` · Text primary `#1F1D1A` / secondary `#6B675E` / tertiary `#9A958B` · Hairline `rgba(0,0,0,.08)` · Tier 2 `#3A6FA0` · Tier 3 `#B07F3C`.

No red. No neon. No status LEDs. No green "all good" dot — calm is the *absence* of accent, not a color.

---

## 12. Spacing

8pt base; generosity is the rule. Tokens: `4 · 8 · 12 · 16 · 24 · 32 · 48 · 64`.

- Hero block: 64 top / 52 sides / 56 bottom — occupies ≈40% of the window.
- Decision rhythm: 22px row padding, separated by hairlines (not cards-in-cards).
- Radii: window 14 · card/tile 16–18 · pill/button 8–10. Single-sided borders never get radius.
- Targets ≥ 36px. Popover width 300–320. App content column max ≈ 720, centered, never edge-to-edge text.

The most important spacing decision: leave the hero mostly empty. Emptiness is the message.

---

## 13. Component library

1. **Significance mark** — the ring glyph; 4 states; the only "status" in the product.
2. **Briefing popover** — wordmark, one hero line, 0–N decision rows, one Review action.
3. **Hero block** — greeting eyebrow + 42px statement + reassurance line.
4. **Decision card** — tier dot · title · one sentence · Review. Nothing more.
5. **Decision detail** — Title · Context · Suggested action · Suggested response · Approve / Edit / Skip.
6. **Person row** — avatar (photo, else monogram) · name · relationship · current state.
7. **Commitment row** — promise · when (amber only when it genuinely needs you).
8. **Trust disclosure** — collapsed reasoning, expandable per decision.
9. **Quiet button** (hairline) and **Primary action** (one per screen, 500 weight).
10. **Hairline divider** · **Tier dot** · **Section label**.

Rule for every component: a title and one sentence. If a third line is needed, the design is wrong.

---

## 14. Dark mode

Designed dark-first (above is the primary spec). Dark must feel *warm, premium, calm, expensive* — never pure black `#000`, never harsh white-on-black. Canvas is warm charcoal `#1B1A18`; text is warm off-white `#F2EFE9`; separation comes from hairlines and elevation, not shadows or borders-everywhere. Light mode is the same composition on warm paper — it inherits every spacing/type rule unchanged; only the four neutrals and two accents swap.

---

## 15. SwiftUI implementation plan

Reuse the existing engine entirely — this is a presentation layer over data that already exists. "Significance" is derived from tiers (2/3) and proactive items, **never from counts**.

- **Design tokens** → one `Steward.Design` enum: `Color`, `Font` (SF Pro weights), `Spacing`, `Motion` (durations/curves). Single source for app, menu bar, and widget.
- **Menu bar** → `MenuBarExtra(... ) { BriefingView() }` with `.menuBarExtraStyle(.window)`. The bar icon is a custom **template `NSImage`** redrawn per `SignificanceState { calm, one, several, important }` (the ring fill); template image so macOS tints it. State maps from open decisions, not a number.
- **App** → `Window`/`WindowGroup` + `NavigationStack`. Views: `HomeView` (hero + `DecisionsList`), `DecisionDetailView` (single-focus, no list/queue), `PeopleView`, `CommitmentsView`, `TrustCenterView` (`DisclosureGroup`s). `.preferredColorScheme` follows system; tokens handle both.
- **Widget** → WidgetKit extension, `systemSmall` + `systemMedium`. `TimelineProvider` emits a `SignificanceEntry`; the view renders State 1–4. `widgetURL(steward://decision/<id>)` so a tap goes **straight to the decision**, never via the dashboard.
- **Notifications** → significance-framed copy ("Daniel has been waiting six days. A suggested reply is ready."), never per-message pings or counts.
- **Data mapping** (to what's already built): Decisions ← pending tier 2/3 + proactive sweep; People ← persons/contacts + open state; Commitments ← commitments table (`owner/direction/status_of`); Trust Center ← the explanation / replay / calibration / trust endpoints.

No engineering change to the brain is implied — only the surfaces are replaced.

---

## 16. Apple HIG review

Applied the "remove 50%, then again, then again" pass to every screen:

- **Home** kept: greeting, one hero statement, decisions. Removed: stats, status, mode, counts, charts, activity feed.
- **Decision detail** kept: one decision, context, suggested response, three actions. Removed: thread history, headers, metadata, the queue itself.
- **People** kept: who + their state. Removed: channels, message logs, last-seen, unread.
- **Menu bar** kept: the mark + a briefing. Removed: every operational readout.
- **Widget** kept: significance + one line. Removed: every number.

Against the HIG pillars — **Clarity** (one idea per screen, editorial type), **Deference** (the UI recedes; content and whitespace lead), **Depth** (gentle cross-fades signal hierarchy, not decoration). Every remaining element earns its place by answering "what needs me?" If it doesn't, it moved to Trust Center or was deleted.

---

## 17. Final production-ready recommendation

Ship the **menu bar as the product** and treat the dashboard as a place you can trust but rarely need. Build in this order:

1. **Menu bar + briefing + significance glyph** — this alone delivers the core promise.
2. **Decision detail** (single-focus Approve/Edit/Skip) — the one interaction that matters.
3. **Home hero + empty state** — make "you're clear" feel earned and rewarding.
4. **Widget** (4 states, deep-link to decision) — the ambient, glanceable proof.
5. **People + Commitments** — the emotional and memory layers.
6. **Trust Center** — last; collapsed; the safety net that lets the owner let go.

Success is measured not in engagement but in its absence: after six months the owner rarely opens Gmail or WhatsApp, doesn't fear missing anything, and feels calmer. The interface should read less like software and more like delegation — quiet competence, holding the line so a person doesn't have to.
