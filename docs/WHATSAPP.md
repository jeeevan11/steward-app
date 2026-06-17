# WhatsApp channel (Phase 2)

A thin WhatsApp channel that feeds the **same** brain as Gmail. The brain, tiers,
classifier, Telegram controls, and SQLite store are unchanged — WhatsApp is just a
new input/output channel behind the existing `MailSource` seam.

## Two processes, one brain
```
WhatsApp ⇄ relay/whatsapp_relay.js (Node, Baileys — dumb pipe, no logic)
   │  POST /inbound  ─────────────▶  Python receiver 127.0.0.1:7999
   ◀── POST /send, /read ─────────  WhatsAppSource → relay 127.0.0.1:7998
                                       │
Python assistant ── ingest/whatsapp_source.py (MailSource) ── same brain/ledger/Telegram
```
- The **relay** (Node) only moves bytes: it forwards every incoming message to Python
  and performs the send/read commands Python returns. No triage, no drafting.
- The **Python side** normalizes each payload into the existing `Message` model and
  runs it through the identical pipeline Gmail uses. A separate poller thread claims
  only `wa_*` ids from the shared ledger; the Gmail poller ignores them.

## Setup
1. Python `.env`:
   ```
   WHATSAPP_ENABLED=true
   WHATSAPP_RELAY_PORT=7999      # Python receives /inbound here
   WHATSAPP_SEND_PORT=7998       # relay receives /send,/read here
   WA_USER_JID=<your jid, e.g. 919876543210@s.whatsapp.net>
   PERSONAL_JIDS=<comma-separated jids that are family/close friends>
   WATCH_KEYWORDS=urgent,decision,asap
   WHATSAPP_TRANSCRIBE_MODEL=google/gemini-2.5-flash
   ```
2. Start the assistant: `python run.py` (it now also starts the WhatsApp receiver).
3. Start the relay and pair once via QR: see [relay/RELAY_README.md](relay/RELAY_README.md).

To find your JID / a contact's JID: it's the phone number in international format with
no `+`, followed by `@s.whatsapp.net` (e.g. `919876543210@s.whatsapp.net`). Group JIDs
end in `@g.us`. The relay logs the `sender_jid` of every message to `relay/relay.log`,
which is the easiest way to read off real JIDs.

## How messages are handled (all by the existing brain)
- **Tier 0/1** (noise / FYI): WhatsApp's `archive` = **mark the chat read** (not
  "archive"); FYIs still go to Telegram.
- **Tier 2** (reply): a draft in your voice (short + conversational for WhatsApp) is
  sent to Telegram as a normal **Approve / Edit / Skip** card labeled `[WhatsApp]`.
  Approving sends via the relay (with a 2–8s human-like delay). Same guarded path as
  Gmail — no double-send.
- **Tier 3** (ask): surfaced for your decision; never auto-replied.

## Hard rules (deterministic, not AI)
- **Personal contacts:** any JID in `PERSONAL_JIDS` is stamped with a `personal`
  contact flag at intake and the guardrail floors it to **Tier 3** — always surfaced,
  never auto-handled. (Same mechanism as the investor/legal guardrail.)
- **Groups:** a group message is processed **only** if it @mentions `WA_USER_JID` or
  contains a `WATCH_KEYWORDS` word. Otherwise it's recorded in the ledger as done
  (`group_skipped`) and ignored. The assistant never auto-replies in a group.
- **Voice notes:** transcribed best-effort via the configured model; the body becomes
  `[voice note, transcribed]: …`. If transcription fails it becomes
  `[voice note — open WhatsApp to listen]` (never a fabricated transcript).

## Exactly-once & crash safety
Each WhatsApp message id is `wa_{baileys id}` so it can never collide with a Gmail id
in the shared ledger. The relay's payload is persisted to `whatsapp_inbox` **before**
its id is handed to the poller, and recorded in the ledger before the inbox row is
marked queued — so a crash never drops or double-processes a message.

## Dry-run
Respected exactly like Gmail: in `MODE=dry_run` nothing is sent or marked read; the
assistant only logs what it would do. Telegram `/pause` halts WhatsApp too.

## If the relay drops
The assistant keeps running; new WhatsApp messages simply stop arriving until the
relay reconnects (it auto-retries with backoff). Anything already queued is still
processed. Check state any time with the Telegram **/wastatus** command (it reads
`relay/status.json`). Sends attempted while the relay is down fail safe — the reply
isn't marked sent and the error surfaces to you.

## Status
Telegram **/wastatus** → connection state, session age, messages today, last message.
