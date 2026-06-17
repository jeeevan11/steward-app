# AGENTS.md — setup playbook for a coding agent (Codex / Claude Code)

You are a coding agent setting up **Steward** on the user's Mac on their behalf. The
user is the new owner (call them "the owner"). Your job: **do every step you can
autonomously, verify it works, then STOP and hand the owner a short checklist of the
things only they can do.** Do not guess or fabricate secrets. Do not send or act on
anyone's behalf.

Steward is a local-first personal agent: a Python brain reads the owner's Gmail and
(optionally) WhatsApp, triages everything, and surfaces only what matters as one-tap
approvals in Telegram. It is fail-safe and defaults to **dry-run** (drafts, sends
nothing). Full architecture: see [CONTEXT.md](CONTEXT.md) and [README.md](README.md).

---

## Prime directive

1. Run every **automatable** step below, in order, and verify each.
2. Never do, fake, or work around a **human-only** step (marked 🧍). You physically
   cannot — they need a browser login, a phone, or the owner's private keys.
3. When automatable steps are done, **post the final checklist** (template at the
   bottom) telling the owner exactly what to do, in what order, and where to paste it.
4. **Never commit secrets.** `.env`, `secrets/`, and all runtime state are gitignored —
   keep it that way. Confirm with `git status` before any commit.

---

> For a friendly, beginner-facing version of this, see **[SETUP.md](SETUP.md)**.

## Part A — Automatable setup (you do this)

Run from the repo root. Stop and report if any step fails; don't paper over it.

```bash
# A1. Tooling check — report versions; if missing, tell the owner to `brew install`.
python3 --version        # need 3.11+
node --version           # need 18+   (only required for WhatsApp)

# A2. Python brain
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# A3. Node WhatsApp bridge
( cd relay && npm install )

# A4. Settings file (does NOT overwrite an existing .env)
[ -f .env ] || cp .env.example .env

# A5. Verify the install without any credentials or network (stdlib unittest — no
#     extra deps; the suite is offline). Expect the full suite to pass (a couple may skip if an optional dep like `telegram` isn't installed).
python -m unittest discover -s tests -p "test_*.py"
```

Optional, if the owner wants the menu-bar app and Xcode CLT is present
(`xcode-select -p` succeeds):

```bash
# A6. Build the macOS menu-bar app (unsigned; owner right-clicks → Open the first time)
( cd mac && ./build_app.sh )
```

**Do NOT** run `run.py`, `run_web.py`, or `install_all.sh` yet — they need the owner's
keys (Part B) to do anything useful. After Part B is done by the owner, the run step is
A7 below.

---

## Part B — Human-only steps (you must NOT attempt these — list them for the owner)

These need the owner's accounts, a browser, or a phone. Surface them; never fabricate.

- 🧍 **OpenRouter API key** — owner signs up at openrouter.ai, creates a key, pastes it
  into `.env` as `OPENROUTER_API_KEY`. (This is the LLM the brain uses.)
- 🧍 **Telegram bot** — owner messages **@BotFather** → `/newbot` → copies the token into
  `.env` as `TELEGRAM_BOT_TOKEN`. Then they message their new bot once; you can then run
  `python scripts/get_chat_id.py` to fetch `TELEGRAM_CHAT_ID` and write it to `.env`.
- 🧍 **Gmail / Google Cloud OAuth** — owner creates a Google Cloud project, enables the
  Gmail API, configures the OAuth consent screen, creates a **Desktop** OAuth client, and
  saves the JSON to `secrets/client_secret.json`. Then `python run.py --onboard` opens a
  browser sign-in (owner only). Note: while unverified the app is in "Testing" mode — the
  owner must add themselves as a test user, and the login token needs redoing ~weekly
  until the app is verified. *If the owner wants to skip email for now, set
  `EMAIL_ENABLED=false` in `.env` and run WhatsApp + Telegram only.*
- 🧍 **WhatsApp pairing** (optional) — owner sets `WHATSAPP_ENABLED=true`, then runs
  `( cd relay && node whatsapp_relay.js )` and scans the QR with their phone once.
- 🧍 **Go live** — Steward stays in `MODE=dry_run` (drafts only, sends nothing) until the
  owner reviews it and flips `MODE=live`. Even live, nothing sends without their tap.

After the owner has done the Part B items they want:

```bash
# A7. Start everything as always-on background services (engine + dashboard + relay).
./deploy/install_all.sh
# Dashboard: http://localhost:8000
```

---

## Guardrails (non-negotiable)

- **No-auto-send invariant:** Steward never sends a message without a human approval tap.
  Don't add code or config that bypasses this.
- **Secrets never leave the machine / never get committed.** If you ever see a secret
  staged in `git status`, stop and remove it.
- **Dry-run by default.** Don't flip `MODE=live` — that's the owner's call (Part B).
- If anything is ambiguous, ask the owner rather than guessing.

---

## Final checklist to post to the owner (fill in real results)

> ✅ I've set up everything I can:
> - Python deps installed, Node deps installed, tests: **<X passed / Y failed>**
> - `.env` created from the template
> - Menu-bar app built: **<yes / skipped — no Xcode CLT>**
>
> 🧍 **Your turn — I can't do these (they need your accounts / browser / phone):**
> 1. **OpenRouter key** → openrouter.ai → create key → paste into `.env` as `OPENROUTER_API_KEY`
> 2. **Telegram** → @BotFather `/newbot` → paste token into `.env` as `TELEGRAM_BOT_TOKEN`,
>    then message your bot once and tell me — I'll fetch your chat id.
> 3. **Gmail (optional)** → make a Google Cloud OAuth desktop client → save to
>    `secrets/client_secret.json`, then run `python run.py --onboard`. *(Or skip with
>    `EMAIL_ENABLED=false`.)*
> 4. **WhatsApp (optional)** → set `WHATSAPP_ENABLED=true`, run the relay, scan the QR.
>
> When you've pasted the keys, tell me and I'll start it in dry-run, confirm it's
> healthy on the dashboard, and walk you through going live.
