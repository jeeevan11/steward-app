# Setup — the simple version

This is the whole setup, start to finish. Each step is tagged:

- **[AI]** — your AI assistant (Claude Code / Cursor) can do this for you. Just ask it.
- **[YOU]** — only a human can do this. It's quick, and each one is spelled out below.

> Easiest path: open this folder in your AI assistant and say:
> *"Set this up for me by following AGENTS.md, then give me the human-only checklist."*
> It will run the **[AI]** steps and hand you the **[YOU]** steps.

---

## Part 1 — Install the basics

**1. [AI] Get the code running.** In a terminal:
```bash
bash bootstrap.sh
```
This installs Python + Node dependencies, creates your `.env` file from the template, and runs
the test suite to confirm everything works. (Your AI can run this and fix anything that fails.)

---

## Part 2 — Your turn (the human-only steps)

These need a real person — your AI can't make accounts or hold your phone. Each takes 1–3 minutes.

**2. [YOU] Get one AI key.** This is the only *required* one.
- Go to **https://openrouter.ai**, sign up, open **Keys**, and create a key.
- Open the `.env` file and paste it after `LLM_API_KEY=` (or `OPENROUTER_API_KEY=`).
- *(Want to use OpenAI or another provider instead? See the "BRING ANY KEY" notes at the top of
  `.env.example`. Your AI can help you switch.)*

**3. [YOU] Make a Telegram bot** (this is how Steward shows you cards and you approve them).
- In Telegram, search **@BotFather**, send **/newbot**, and follow the prompts.
- Copy the token it gives you into `.env` after `TELEGRAM_BOT_TOKEN=`.
- Send any message ("hi") to your new bot once, so it can find you.
- **[AI]** can then run `python scripts/get_chat_id.py` to fill in `TELEGRAM_CHAT_ID`.

**4. [YOU] Connect email — *optional* (skip if you only want WhatsApp).**
- In **Google Cloud Console**: create a project → enable the **Gmail API** → set up the consent
  screen → create a **Desktop** OAuth client → download the file → save it as
  `secrets/client_secret.json`. Add your own Gmail as a "test user."
- On first run, a browser window opens — click **Allow**.
- Don't want email? Set `EMAIL_ENABLED=false` in `.env` and skip this.

**5. [YOU] Connect WhatsApp — *optional*.**
- Set `WHATSAPP_ENABLED=true` in `.env`.
- **[AI]** starts the relay; a **QR code** appears in the terminal.
- On your phone: **WhatsApp → Settings → Linked Devices → Link a Device → scan the QR.**

---

## Part 3 — Run it

**6. [AI] Start everything.** Your AI can start the engine (and the relay, and build the Mac app).
It begins in **dry-run mode**: it drafts replies and shows you cards, but **sends nothing**.

**7. [YOU] Go live when you're ready.** Watch it for a bit in dry-run. When you trust it, change
`MODE=live` in `.env` and restart. Even live, it **never sends without your approval tap.** This
switch is your decision — only you should flip it.

---

## That's it

- Talk to it / approve cards in **Telegram**, or open the **Steward** menu-bar app on your Mac.
- Everything personal (`.env`, `data/`, `secrets/`) stays on your machine and is never shared.
- Stuck on a step? Paste the error to your AI — it can read this whole project and fix it.
