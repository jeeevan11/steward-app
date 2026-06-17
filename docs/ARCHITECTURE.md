# Architecture

A local-first personal "chief of staff". Phase 1 is **Gmail only**, one Python
process on your Mac. The design goal is **zero cognitive load, fail-safe**: every
uncertainty surfaces to you, nothing irreversible happens without your tap, and no
message is ever processed twice.

## Layered pipeline

```
            ┌──────────────────────────────────────────────────────────────┐
            │                     orchestrator (main.py)                     │
            │  poller thread  ───────────────────────────────  Telegram app │
            └───────┬───────────────────────────────────────────────┬──────┘
                    │                                                 │
   ┌────────────────▼─────────────┐                       ┌──────────▼─────────┐
   │ ingest/                       │                       │ control/           │
   │  MailSource (interface)       │                       │  Telegram bot      │
   │  GmailSource: history.list    │                       │  Approve/Edit/Skip │
   │  thread fetch, PDF text,      │                       │  NL commands       │
   │  normalize → Message/Thread   │                       │  briefs, undo,     │
   └───────┬───────────────────────┘                       │  pause, audit      │
           │                                                └──────────▲─────────┘
   ┌───────▼────────┐   ┌──────────────┐   ┌───────────────┐          │
   │ memory/        │   │ brain/       │   │ action/       │          │
   │  contacts      │──▶│ classifier   │──▶│ dispatcher    │──────────┘
   │  retrieval     │   │ (haiku→opus) │   │ drafting+voice│  (Tier 2/3 → Telegram)
   │  rules         │   │ guardrails   │   │ gmail_actions │
   │                │   │ tiers        │   │ (Tier 0/1)    │
   └───────┬────────┘   └──────────────┘   └───────┬───────┘
           │                                        │
           └──────────────┬─────────────────────────┘
                          ▼
              storage/ (one SQLite file, WAL)
   ledger · pending_actions · contacts · rules · voice · audit · learning · kv
                          ▲
                          │
                    learning/ (record edits/skips → conservative rule proposals)
```

Each numbered layer in the spec maps to a package: `ingest/`, `memory/`,
`brain/`, `action/`, `control/`, `learning/`. Every layer is swappable behind a
narrow seam.

## The exactly-once contract

`storage/ledger.py` is a per-message state machine keyed by the channel message id:

```
(absent) --mark_seen--> SEEN --claim--> PROCESSING --complete--> DONE
                                          └----fail-------------> FAILED
```

* `mark_seen` is `INSERT OR IGNORE` on the primary key — the dedup gate.
* `claim` is an atomic compare-and-set (`SEEN/PROCESSING → PROCESSING`); only one
  caller wins.
* A message is marked `DONE` **only after all side effects complete**. A crash
  mid-`PROCESSING` is re-queued by `recover_stale()` on next startup.
* **Re-processing is safe** because the autonomous pipeline performs only
  reversible/idempotent Gmail operations (archive/label). The single irreversible
  action — sending a reply — never happens in the pipeline. It happens only when
  *you* tap Approve, through `pending_actions` with a unique `idempotency_key` and
  an `APPROVED → SENDING` compare-and-set (`repo.begin_send`) that makes a
  double-tap or restart a no-op.

## The decision: brain → tiers → guardrails

`brain/classifier.py` runs a cheap **haiku** "is this noise?" pass, then **opus**
for judgment, emitting strict JSON validated by `brain/schema.py`. Invalid output
becomes `Decision.failsafe()` → Tier 3. `brain/tiers.py` then composes the final
tier from (a) intent/category, (b) sender importance from memory, (c) stakes +
reversibility, (d) a confidence score — and `brain/guardrails.py` applies hard
floors the model **cannot** override:

* investor/legal contact, or money/legal keywords, or an irreversible action ⇒
  never below Tier 2;
* a payment/account-change pattern ⇒ Tier 3;
* low confidence on a consequential item ⇒ surface, don't act.

## Modes

* **dry_run (default):** classifies and drafts, logs what it *would* do, changes
  nothing in Gmail and sends no replies. Audit rows are written with `dry_run=1`.
* **live:** Tier 0/1 act (reversible only); Tier 2/3 still require your tap.

`MODE` in `.env` selects it; a runtime `pause` (Telegram) halts all autonomous
action regardless.

## Concurrency model (Phase 1)

The Telegram bot owns the asyncio loop on the **main thread**. The Gmail poller
runs on a **background thread**. They never share a SQLite connection — each
thread opens its own connection to the same WAL database (concurrent readers + a
single writer, with `busy_timeout`). Outbound notifications from the poller use
`control/notifier.py`, which talks to Telegram over plain stdlib HTTP, so there is
no cross-thread asyncio coupling.

---

## Where Phase 2+ plugs in (built but NOT implemented now)

### WhatsApp relay (via Baileys)
* `ingest/base.py` defines the channel-agnostic **`MailSource`** interface and the
  domain models (`Message`, `Thread`, `Contact`) are already channel-tagged
  (`Channel.WHATSAPP` reserved). Add `ingest/whatsapp_source.py` implementing the
  same interface against a local Baileys bridge (a small Node sidecar exposing a
  localhost socket/HTTP for receive + send).
* The brain, tiers, guardrails, drafting, learning, and Telegram control layers
  are **unchanged** — they already operate on `Message`/`Thread`/`Decision`, not
  on email specifics.
* The only channel-specific bits to add are: normalization (WhatsApp payload →
  `Message`), and `send_reply`/`archive` semantics for chat (archive ≈ mark read).
* `main.py` would instantiate both sources and round-robin them into the same
  ledger/pipeline. The ledger key is already a generic message id.

### Cloud deployment
* **Process split:** the poller and the brain are pure functions over a DB +
  interfaces; lift them to a worker. The control surface (Telegram) becomes a
  separate webhook service. They communicate only through the SQLite tables today
  — swap `storage/db.py` for a hosted Postgres by reimplementing the same
  `repositories`/`ledger` function signatures (they are the seam; nothing else
  imports SQL).
* **Push instead of poll:** `GmailSource.fetch_new_message_ids()` is the seam for
  swapping `history.list` polling for **Gmail Pub/Sub push** — the rest of the
  pipeline consumes message ids identically. The stored `historyId` already lives
  in `kv`.
* **Secrets:** `config.Settings` is the single source; point it at a secrets
  manager instead of `.env` without touching callers.
* **Multi-tenant:** every table is single-user today; add a `user_id` column and
  thread it through `repositories` — again, the only layer that touches SQL.

### What deliberately stays local in Phase 1
OAuth token, SQLite DB, and the LLM API key all live on your Mac. The launchd
LaunchAgent (`deploy/`) keeps the single process alive.
