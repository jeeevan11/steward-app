# Steward — your AI chief of staff

Steward is a personal assistant that lives in your Mac menu bar and quietly runs your
**WhatsApp and email** for you. It reads everything that comes in, deals with the noise on
its own, drafts replies **in your voice**, and only taps you on the shoulder for the things
that actually need *you*.

It runs **on your own computer** — your messages and keys never leave your Mac.

> **The golden rule:** Steward **never sends a message without your one tap of approval**, and
> it **never quietly handles something important**. You're always in control.

---

## What it does (in plain words)

- 📥 **Reads your WhatsApp + email, 24/7** — every message, so it always has the full picture.
- 🧹 **Kills the noise** — newsletters, spam, promotions get filed away quietly. You're not bothered.
- ✍️ **Drafts replies in your voice** — you just read it and tap **Send**. You never type.
- ✋ **Never sends on its own** — every reply needs your approval tap. Always.
- 🛑 **Won't auto-handle important stuff** — money, investors, big decisions → it flags them and
  waits for *you*. It will never answer something that matters on your behalf.
- 📱 **Knows what you do elsewhere** — reply to someone on your phone yourself and it notices,
  and clears it from your list. No double replies.
- 👀 **Tracks delivery + read** — it can tell you "they read your message 2 hours ago, no reply yet."
- 🧠 **Learns who matters** and **remembers what you promised people** (commitments).
- 📊 **Shows you the picture** — what it handled, time it saved you, today.

See [FEATURES.md](FEATURES.md) for the full list, still in plain language.

---

## Get started

**You'll need:** a Mac, and one API key from any AI provider (OpenRouter is easiest — one key,
all models). Email and WhatsApp are optional and can be added later.

The friendliest path: **open this folder in [Claude Code](https://claude.com/claude-code) (or
Cursor) and say "set this up for me following AGENTS.md."** Your AI will do all the technical
steps and then hand you a short checklist of the few things only you can do (get a key, make a
Telegram bot, scan a QR). See **[SETUP.md](SETUP.md)** for the exact, beginner-friendly walkthrough.

Prefer to do it yourself? One script gets the basics running:

```bash
git clone https://github.com/jeeevan11/steward-app.git steward && cd steward
bash bootstrap.sh        # installs everything, creates your .env, runs the tests
```

Then open **[SETUP.md](SETUP.md)** and do the handful of "your turn" steps it lists.

---

## Bring your own AI key (any provider)

Steward works with **any OpenAI-compatible provider** — OpenRouter, OpenAI, Together, Groq, or
a local model. You pick one and paste one key in `.env`. Defaults to OpenRouter (one key →
every model). Full details in `.env.example` under **"LLM — BRING ANY KEY."**

---

## Safety

- It starts in **dry-run mode** — it drafts and shows you everything but changes/sends **nothing**
  until *you* decide to switch `MODE=live`.
- Your secrets (`.env`), your data (`data/`), and your sign-ins (`secrets/`) are **never** committed
  to git and never leave your machine.

---

## How it's built (for the curious / for your AI)

- **`assistant/`** — the Python "brain" (reads messages, classifies, drafts, decides).
- **`relay/`** — a small Node app that connects to WhatsApp.
- **`mac/`** — the SwiftUI menu-bar app (the console you see).
- **`assistant/web/`** — an optional web dashboard.

Architecture notes live in [CONTEXT.md](CONTEXT.md); the setup playbook for an AI assistant is
[AGENTS.md](AGENTS.md); the house rules are in [CLAUDE.md](CLAUDE.md).
