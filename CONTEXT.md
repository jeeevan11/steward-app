# Steward — Project Context (handoff for a new chat)

This is the single authoritative orientation doc. Read it top to bottom and you can
work on the system without re-deriving anything. Last refreshed after the
parallel-merge reconciliation + repo reorganization (commit `3d22588` plus the
reorg commit that follows it).

---

## 1. What this is

**Steward** is a local-first AI "chief of staff." It reads the owner's **Gmail** and
**WhatsApp**, triages every message through an LLM brain, silently files the noise,
and surfaces only real decisions to the owner as one-tap actions in **Telegram**
(Approve / Edit / Skip) and a native **macOS menu-bar app** (`Steward.app`).

It runs entirely on the owner's Mac. It is **LIVE on a real account right now**
(`MODE=live`, `dry_run=false`), processing on the order of ~1,500 messages/day.

**Owner:** Jatin Chhanwal, founder/CEO of Acme Inc (stealth hardware + AI,
recently incorporated). Direct, technical, concise communicator. The full standing
context the brain uses is the top of `prompts/classifier.md`.

**Design philosophy:** fail-safe (any error or uncertainty is surfaced, never acted
on silently), conservative (earns the right to act silently with evidence), and never
does anything irreversible without the owner's tap.

---

## 2. Current state (where things are)

- **Live and healthy.** Engine + web console + WhatsApp relay all running.
- The codebase was reconciled after **three parallel chats** merged on top of
  each other and left it incoherent. Net result of the fix work (commit `3d22588`):
  - Recruiter mail no longer silently archived (reaches the classifier).
  - Natural-language deadlines parse correctly ("end of day friday", "June 30th").
  - Knowledge graph is wired AND backfilled (15 nodes, 14 edges from 14 contacts).
  - Manager/VP/C-suite added to a thread escalates the tier.
  - Briefs surface commitments due within 48h.
  - The dead second batching system (orphaned "Phases 1-6" `PhaseDispatcher` +
    `maybe_send_batches`) was removed. The wired **fold-batching** is canonical.
- Then the repo was **reorganized**: all docs moved under `docs/`, scripts under
  `scripts/`, root cleaned (see the repo map below).
- **508 tests pass** (stdlib-only core suite).

### Known deferred / gotchas (read these)
- **Running code is not auto-reloaded.** The engine loads Python modules once at
  startup. Editing a `.py` does nothing to the live process until you **restart** it.
  Prompts in `prompts/*.md` ARE re-read at runtime, so prompt edits take effect live.
- **Dead schema columns left on purpose.** `pending_actions.criticality_signal` and
  `pending_actions.batch_id` (plus two indexes) are vestigial from the removed
  batching system. Dropping columns on live SQLite is destructive, so per the
  additive-only rule they were left in place. Harmless; ignore them.
- **Graph populates going forward.** Fix wiring fires on each new message; existing
  contacts were backfilled once. It is not retroactive beyond that backfill.

---

## 3. How to run it

Three processes. All bind **127.0.0.1 only**. Config comes from `.env` (gitignored;
`.env.example` documents every knob).

```bash
# 1) The engine (Gmail/WhatsApp poller + Telegram control surface)
.venv/bin/python run.py
#    run.py --onboard   first-time voice/VIP/rule bootstrap, then start
#    run.py --once      one poll/process pass then exit (debugging)
#    run.py --status    print a status summary and exit

# 2) The web console backend (FastAPI, dashboard API)
.venv/bin/python run_web.py            # 127.0.0.1:8000

# 3) The WhatsApp relay (Node, holds the Baileys linked-device session)
node relay/whatsapp_relay.js           # only if WHATSAPP_ENABLED=true
```

Frontend dev UI (optional): `cd assistant/web/frontend && npm run dev` (127.0.0.1:5173).
The built dashboard is also served by run_web.py at :8000.

**Ports (live):** relay HTTP `:7998`, engine WhatsApp inbound receiver `:7999`, web
`:8000`, vite dev `:5173`. Exact values are in `.env` (`WHATSAPP_RELAY_PORT`,
`WHATSAPP_SEND_PORT`, `GMAIL_PUBSUB_PORT`, etc.).

**Liveness:** `data/status.json` is rewritten every poll cycle (mode, pending count,
last-24h tally, heartbeat_ts). Fresh heartbeat_ts = poller alive.

**Restarting safely (the engine talks to a real Telegram/Gmail/WhatsApp account):**
1. `kill -TERM <engine_pid>` and wait for it to fully exit (python-telegram-bot
   releases the long-poll cleanly; if you start the new one too early you get a
   Telegram 409). Confirm the port frees.
2. Relaunch detached: `nohup .venv/bin/python run.py > /tmp/steward_engine.log 2>&1 &`
3. Watch the log for `poller started ... mode=live` with no traceback / no 409.
   On startup it sends the owner a "Assistant online" Telegram message.

**Dry-run:** flip `MODE=live`/`dry_run` in `.env`. In dry-run it classifies and
drafts but changes nothing in Gmail and sends nothing. Restart to apply.

---

## 4. Architecture (the pipeline)

Single orchestrator process (`assistant/main.py`). The Telegram bot owns the asyncio
loop on the main thread; the Gmail poller and the WhatsApp poller each run on their
own background thread. **Threads never share a SQLite connection** (one connection
per thread; WAL allows concurrent readers + a single writer).

Per-message flow (`main.py:process_one` + `assistant/action/dispatcher.py`):

1. **Claim** the message in the ledger (`storage/ledger.py`) so it is processed
   **exactly once**, even across crashes/restarts.
2. **Fetch the full thread**; extract text from PDF attachments; skip self-sent mail.
3. **Resolve** the sender to a `Contact` and a cross-channel **person identity**
   (`memory/identity.py`); a weak match asks the owner once to confirm a link.
4. **Noise pre-filter** (cheap model, `prompts/noise_filter.md`): obvious bulk mail
   is filed silently. Anything uncertain falls through.
5. **Three-step reasoning** (`brain/classifier.py`):
   - **THINK** (`prompts/think.md`) prep.
   - **JUDGE** (`prompts/classifier.md`) the decision. Money/legal/investor threads
     route to **JUDGE_CRITICAL** (heavier model + bigger reasoning budget).
   - **SELF_CRITIQUE** (`prompts/self_critique.md`) a skeptic that can only **raise**
     involvement, never lower it.
   Output is strict, schema-validated (`brain/schema.py`); invalid output fails safe.
6. **Hard guardrails** (`brain/guardrails.py`) wrap the model: money/legal/investor,
   or a VIP/personal contact, can never be handled silently. Guardrails only RAISE
   the tier (floors). Lowering forces (memory nudge, presence suppression, feedback
   deprioritization) are clamped to that floor.
7. **Tier** (`brain/tiers.py`): **0 SILENT** (archive/label, reversible only),
   **1 FYI** (act + one line), **2 APPROVE** (draft a reply for one-tap approval),
   **3 ASK** (context + wait). When unsure between two tiers, pick the higher.
8. **Dispatch** (`action/dispatcher.py`): SILENT/FYI act immediately; APPROVE/ASK are
   **fold-batched** (a new message on an already-pending thread folds into the
   existing card instead of spamming a new one) and sent to Telegram as a card.
9. **Approve** sends the reply via the right channel. Sending is wrapped by
   **double-send guards** in `storage/repositories.py`
   (`begin_send` / `mark_approved` / `set_pending_draft`). Drafts never fabricate
   facts: unknowns are left as `[placeholder]`.

Two external services only: **OpenRouter** (LLM) and **Google** (Gmail/Calendar).
The **TaskRouter** (`assistant/llm/`) maps each task to a model + reasoning budget.
Defaults: Gemini 2.5 Flash for the everyday path, Gemini 2.5 Pro for the critical
path (slugs in `.env`: `JUDGE_MODEL` / `NOISE_MODEL` / `DRAFT_MODEL` / `PRO_MODEL`).

---

## 5. Key subsystems

- **Memory (three layers).**
  1. Per-thread + per-contact memory (`memory/contacts.py`, `retrieval.py`).
  2. Distilled **relationship memory** (`memory/distill.py` -> `relationship_memory`
     table): durable facts, what is open, what was decided. Recency wins over memory.
  3. Cross-channel **person identity** (`persons` / `person_links`) + a **knowledge
     graph** (`memory/graph.py`, `graph_nodes` / `graph_edges`). `MEMORY_*` knobs.
- **WhatsApp settling** (`ingest/whatsapp_source.py` + relay): trailing-edge debounce.
  Hold a rapid burst until the conversation goes quiet (`WHATSAPP_SETTLE_SECONDS`,
  ~75s after the LAST message), then process the whole burst as one card. Groups hold
  longer. Subject is inferred from context (WhatsApp has no subject line).
- **Presence-aware suppression** (`control/presence.py`): do not ping a chat the owner
  is actively handling (recent outbound, or the app is focused). `PRESENCE_*` knobs.
- **Universal context** (`storage/wa_messages.py`): every WhatsApp message is recorded
  (even muted/group) for context, separate from what gets surfaced.
- **Learned voice** (`onboarding/`, `voice_profiles` / `voice_samples`): drafts are
  written in the owner's segmented voice; `action/quality_gate.py` blocks bad drafts.
- **Feedback loop** (`learning/`, `learning_events`): approve/edit/skip patterns
  propose conservative rules and gently deprioritize repeatedly-skipped senders.
- **Proactive** (`control/proactive.py`): sweeps for commitments due soon, stale
  threads, relationship reminders. `PROACTIVE_*`, `COMMITMENT_CHECK_HOUR`.
- **Briefs** (`control/briefs.py`): morning/evening summaries; now include commitments
  due within 48h. `MORNING_BRIEF_HOUR` / `EVENING_BRIEF_HOUR`.
- **Production-hardening modules:** `storage/explanations.py` (why a decision was made),
  `storage/replay.py` (audit-trail replay), `storage/calibration.py` (confidence
  calibration), `storage/trust_metrics.py`, `storage/retention.py` + `memory/governance.py`
  (memory retention/governance). `RETENTION_*`, `MEMORY_GOVERNANCE_ENABLED`.
- **macOS app** (`mac/`, SwiftUI SPM package -> `Steward.app`): MenuBarExtra +
  dashboard + WidgetKit, talks to the web API over HTTP, with AppIntents
  (Refresh / Clear All) and a `steward://` deep link. Build with `mac/build_app.sh`.

---

## 6. Repo map

```
assistant/            # the Python engine (do NOT restructure; it is live)
  config.py main.py models.py
  llm/        # OpenRouter client + TaskRouter
  ingest/     # Gmail/WhatsApp sources, gmail_push, normalize, router
  brain/      # classifier (THINK->JUDGE->CRITIQUE), schema, guardrails, tiers
  action/     # dispatcher (fold-batching), drafting, quality_gate, gmail_actions
  memory/     # contacts, identity, distill, graph, retrieval, commitments, governance
  control/    # telegram_bot, notifier, briefs, presence, proactive, NL commands
  learning/   # approve/edit/skip capture -> rule proposals
  onboarding/ # voice + VIP + rule bootstrap
  storage/    # SQLite: ledger (exactly-once), repositories (send guards), db,
              #   migrations, read_queries, explanations, replay, calibration, ...
  web/        # FastAPI backend + service seams + React dashboard (frontend/)
prompts/      # editable LLM prompts (re-read at runtime)
relay/        # Node WhatsApp relay (Baileys); relay/session = linked-device creds
mac/          # native SwiftUI menu-bar app (Steward.app)
evaluation/   # offline eval harness (run_all, runner, datasets, reports, test_flow.py)
scripts/      # handover.sh, smoke_real.py, get_chat_id.py
deploy/       # launchd LaunchAgent + installer
docs/         # architecture + subsystem docs (architecture, intelligence, web, whatsapp)
tests/        # stdlib-only unittest suite
data/         # SQLite DB + status.json (gitignored)
secrets/      # OAuth client_secret.json + gmail_token.json (gitignored)
```

Entry points at root: `run.py`, `run_web.py`. Config: `.env` (+ `.env.example`).

---

## 7. Data model

One SQLite file: `data/assistant.db`. WAL mode, `isolation_level=None` (autocommit),
`check_same_thread=False`, **one connection per thread**. Schema is created by
`storage/db.py` and evolved idempotently by `storage/migrations.py` (runs in
`init_db`). Some tables are created on demand by their own module's `ensure()`
(e.g. `graph_nodes`/`graph_edges`, `wa_messages`, explanations/calibration tables).

Core tables: `processed_messages` (the ledger), `pending_actions`, `contacts`,
`persons` / `person_links` / `person_link_suggestions`, `relationship_memory`,
`rules` / `proposed_rules`, `learning_events`, `draft_edits`, `skip_log`,
`voice_profiles` / `voice_samples`, `commitments`, `audit_log`, `kv`.

---

## 8. Hard invariants (do NOT break these)

1. **Never touch the exactly-once + double-send machinery.** `storage/ledger.py` and
   the guards in `storage/repositories.py` (`begin_send`, `mark_approved`,
   `set_pending_draft`) are load-bearing. Do not modify them without explicit reason.
2. **Additive only.** New changes must not break existing behavior; all tests must
   pass after every change.
3. **Fail-safe everywhere.** On any error/uncertainty, surface to the owner. Never
   fail silently. Best-effort side features (graph, memory, presence) are wrapped in
   try/except and must never break the processing path.
4. **Never double-send. Never fabricate.** Drafts use `[placeholder]` for unknowns.
5. **Respect dry-run.** It must change/send nothing.
6. **Two external services only:** OpenRouter + Google. **Localhost-only binds**
   (127.0.0.1). Secrets live in `.env` / `secrets/` (gitignored), never in code.
7. **No em-dashes** in generated text.
8. **Git:** commit only when asked, as the repo owner's own git identity.
   Do not commit `.claude/`, `.env`, `secrets/`, `data/`, or `node_modules/`.

---

## 9. Working on it

- **Tests:** `.venv/bin/python -m unittest discover -s tests` (508 tests, fast, the
  core is stdlib-only so it needs nothing installed). Add tests with your changes.
- **Prompts** are data: edit `prompts/*.md` and the live engine picks them up on the
  next call (no restart). Code changes need a restart.
- **Config** is centralized in `assistant/config.py` (`Settings` dataclass) and
  documented in `.env.example`. Add a knob there, default it safely.
- **The console never reimplements logic:** every web write calls the same guarded
  functions Telegram does (see `docs/WEB.md`).
- **Parallel-merge caution:** if reports/docs disagree with the code, trust the code +
  runtime. A previous merge once left orphaned dead code; verify wiring before trusting
  a "shipped" claim.

### Doc pointers
- `docs/ARCHITECTURE.md` — system seams and module boundaries.
- `docs/INTELLIGENCE.md` — the reasoning layer (P0..P6), push delivery.
- `docs/WHATSAPP.md` + `relay/RELAY_README.md` — the WhatsApp channel + relay.
- `docs/WEB.md` — the console endpoint -> guarded-function map.
- `docs/STEWARD_DESIGN.md` — the dashboard/menu-bar/widget design language.
