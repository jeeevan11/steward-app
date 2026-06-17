# Chief of Staff — full system overview

A **local-first AI "chief of staff"** that reads a person's **Gmail + WhatsApp**, triages
every message through an LLM, silently handles noise, and surfaces only real decisions to
the owner on **Telegram** (one-tap Approve / Edit / Skip). It runs entirely on the owner's
Mac. Two external services only: **OpenRouter** (LLMs) and **Google** (Gmail). Secrets live
in a gitignored `.env`; all servers bind `127.0.0.1`.

The product goal: the owner's **peace of mind and time**. The agent is independent —
it acts on its own and only asks when something is genuinely important or uncertain — and
it **gets better over time** via a feedback loop.

---

## 1. The end-to-end flow (one message)

```
Gmail / WhatsApp
      │
      ▼
[ Ingest ]  normalize to a common Message/Thread
      │     WhatsApp: a dumb Node "relay" (Baileys) forwards each message to the engine
      │
      ▼
[ Settling gate ] (WhatsApp)  hold a burst of line-by-line texts until the chat goes
      │           quiet (75s for 1:1, 5m for groups), then process the WHOLE burst as one.
      ▼
[ Universal context ]  EVERY message (in/out, group, even muted) is recorded to history.
      │                Knowing is decoupled from notifying — suppression never stops learning.
      ▼
[ Brain ]  noise pass → THINK → JUDGE (Gemini Flash/Pro) → SELF-CRITIQUE
      │     reads: the thread + 2-week rolling context + cross-channel memory of the person
      ▼
[ Tier engine ]  decide tier 0–3, combining:
      │   • guardrail FLOORS (money/legal/investor/irreversible/personal) — can only RAISE
      │   • VIP / importance floors
      │   • LOWERING forces (memory "already handled", presence "you're handling it",
      │     feedback "you keep skipping them") — each clamped so it can NEVER drop below a
      │     guardrail floor and never silences VIP/personal/high-stakes.
      ▼
   tier 0 SILENT → do the reversible action, no ping
   tier 1 FYI    → one-line FYI
   tier 2 APPROVE→ pre-draft a reply in the owner's voice, send an Approve/Edit/Skip card
   tier 3 ASK    → surface a consequential item with a suggested reply, never auto-act
      │
      ▼
[ Telegram card ]  shows the inferred topic, 👤 known / 🆕 unsaved sender + name, the
      │             email subject (or group + message count), a quote, and the draft.
      ▼
[ Owner taps ]  Approve → sends (exactly-once, double-send-guarded) · Edit · Skip
      │
      ▼
[ Learning ]  the choice + the relationship are distilled back into memory.
```

Safety invariants (never violated): exactly-once processing (`storage/ledger.py`),
double-send guards (`storage/repositories.py`), fail-safe everywhere (error/uncertainty →
surface, never silent), dry-run respected, drafts never fabricate (use `[placeholder]`).

---

## 2. Architecture / processes

Four local processes, all on `127.0.0.1`:

| Process | What | Port |
|---|---|---|
| **Engine** (`run.py`) | the brain: pollers (Gmail + WhatsApp), tier engine, dispatch, Telegram bot, memory, learning. Writes `data/status.json` heartbeat. | WhatsApp receiver :7999 |
| **Relay** (`relay/whatsapp_relay.js`, Node/Baileys) | dumb pipe: forwards inbound + the owner's own outbound to the engine; performs sends/reads. | listens :7998 |
| **Web console** (`run_web.py`, FastAPI) | read/write dashboard API; serves the built React UI when present. | :8000 |
| **Mac app** (`mac/`, SwiftUI) | menu-bar shell: supervises the above, status, toggles, onboarding, launch-at-login. | — |

Storage: a single **SQLite** DB (WAL). Key tables: `processed_messages` (the exactly-once
ledger), `pending_actions` (approval queue), `contacts`, `persons` + `person_links`
(cross-channel identity), `relationship_memory`, `wa_messages` (universal WhatsApp history),
`whatsapp_inbox` (WhatsApp processing queue), `learning_events`, `decision_log`.

LLMs via OpenRouter: Gemini 2.5 Flash (default), Gemini 2.5 Pro (critical JUDGE), routed
per task. Three-step reasoning with budgets; cost metered per call.

---

## 3. What's been built (chronological)

**Foundations** — Gmail ingest, the brain (noise → classify), the tier engine + hard
guardrails, exactly-once ledger, Telegram approve/edit/skip, dry-run, the web console.

**Intelligence upgrade (P0–P7)** — speed/UX (Gmail push, pre-drafted replies, redesigned
cards, response metrics); three-step reasoning (THINK → JUDGE → SELF-CRITIQUE); calendar
context + commitment tracking; segmented voice profiles; a draft quality gate (no em-dashes,
fabrication flags); a 10-view dashboard rebuild; docs.

**Three-layer memory (A–D)** — cross-channel person identity (strict auto-merge, "ask once"
on weak matches, rejections remembered); a distilled relationship record (facts / open
situations / decided / episodes); wired into the brain (read the new message in light of
memory; memory-conflict floor; nudge suppression that only lowers within safety); recency/
staleness handling + a hard personal/family surface-only floor.

**WhatsApp** — channel via the Node relay (DMs, groups, voice-note transcription, `@lid`
ids); a **settling/debounce gate** so line-by-line bursts become one calm card; cards that
show known-vs-unsaved sender + inferred subject + quote.

**Layer 1 — smarter WhatsApp brain:**
- **1A Rules** — VIP "always-instant" (skips settling, never quieted) vs "mute/never"
  (silent, but still clamped to the guardrail floor).
- **1B Presence** — don't ping a chat the owner is handling himself (his recent outbound,
  or the native WhatsApp app being frontmost); per-conversation; VIP overrides; still tracks.
- **1C Rolling context** — the last ~14 days of a chat feed the classifier and drafter.
- **1D Style** — learns the owner's WhatsApp texting style from his own sent messages.
- **1E Feedback** — repeated skips quietly lower how loudly a sender is surfaced.
- **Universal context** — every message (incl. group-skipped + the owner's outbound) is
  recorded; suppression only ever withholds the notification, never the learning.

**Layer 2 — native macOS menu-bar app (`mac/`):**
- Menu-bar-only (no Dock icon). Live icon shows a subtle pending count, dims when off.
- Calm popover: status + Agent on/off + Open dashboard + Settings.
- Native dashboard (Apple-style, symmetric): status hero, 4 stat tiles, Email/WhatsApp/
  Open-at-login toggles, link to the full web console.
- Supervises the engine + relay + web backend; **adopts** already-running ones (heartbeat +
  port detection) so it never double-launches.
- One-time onboarding writes `.env` and hands off Gmail OAuth + the WhatsApp QR to Terminal.
- Launch-at-login via `SMAppService`. Engine side: `EMAIL_ENABLED` toggle, `data/status.json`
  heartbeat, backend serves the built React UI at :8000.

Tests: ~300 unit tests (Python, stdlib `unittest`), all green; the Swift app compiles and
bundles via `mac/build_app.sh`.

---

## 4. What's left / known limitations

- **Packaging for true hand-off**: the Mac app supervises the repo + virtualenv in place.
  A fully standalone, notarized `.app` (embed Python via PyInstaller, Node sidecar,
  Developer ID + notarization) is the remaining distribution step.
- **Presence app-focus**: the native WhatsApp Mac app's focus is detectable; WhatsApp Web
  in a browser tab is not (the reliable signal is the owner's own recent outbound).
- The dashboard UI is React (Vite); the backend serves the built `frontend/dist` at :8000
  when present (run `npm run build`), otherwise use the Vite dev server (:5173).
```
