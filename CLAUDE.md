# CLAUDE.md

**Setting up this project? Follow [AGENTS.md](AGENTS.md)** — it's the full setup
playbook: do every automatable step, verify with the test suite, then hand the owner a
checklist of the human-only steps (keys, Google OAuth, WhatsApp QR, going live).

Hard rules while working here:
- **No-auto-send invariant:** Steward never sends a message without a human approval
  tap. Never add code or config that bypasses this.
- **Never commit secrets.** `.env`, `secrets/`, and runtime state are gitignored — keep
  it that way; check `git status` before committing.
- **Dry-run by default.** Don't flip `MODE=live`; that's the owner's decision.

New here / non-technical? Start with **[SETUP.md](SETUP.md)** (plain-English setup) and **[FEATURES.md](FEATURES.md)**.

Orientation: [CONTEXT.md](CONTEXT.md) (architecture, invariants, repo map) and
[README.md](README.md) (quick start).
