#!/usr/bin/env bash
# Steward — one-shot bootstrap. Installs everything the machine can do on its own
# (Part A of AGENTS.md), then prints the short list of human-only steps (Part B).
# Safe to re-run: every step is idempotent. It NEVER writes secrets and NEVER goes live.
#
# Usage:   bash bootstrap.sh
set -uo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

say "Steward bootstrap — installing the basics (no keys, no sending, nothing leaves your Mac)"

# 1) Tool versions
command -v python3 >/dev/null || { echo "Python 3.11+ is required. Install it from python.org, then re-run."; exit 1; }
PYV=$(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')
ok "python3 $PYV  (need 3.11+)"
command -v node >/dev/null && ok "node $(node --version)" || warn "node not found — WhatsApp relay needs Node 18+ (install from nodejs.org). Email-only setups can skip it."

# 2) Python virtualenv + deps
if [ ! -d .venv ]; then python3 -m venv .venv && ok "created .venv"; else ok ".venv already exists"; fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt && ok "Python dependencies installed"

# 3) Node deps for the WhatsApp relay (optional channel)
if command -v npm >/dev/null; then ( cd relay && npm install --silent ) && ok "WhatsApp relay dependencies installed"; else warn "skipped relay npm install (no npm)"; fi

# 4) Your .env (from the template) — only if you don't have one yet
if [ -f .env ]; then ok ".env already exists (left untouched)"; else cp .env.example .env && ok "created .env from template — you'll fill in your keys next"; fi

# 5) Prove it works — offline test suite (no keys, no network)
say "Running the offline test suite to confirm the install…"
if python -m unittest discover -s tests -p "test_*.py" >/tmp/steward_test.log 2>&1; then
  ok "all tests passed"
else
  warn "some tests failed — see /tmp/steward_test.log (often just an optional dependency; your AI can fix it)"
fi

# 6) Optionally build the Mac menu-bar app (only if Xcode command-line tools are present)
if xcode-select -p >/dev/null 2>&1 && [ -f mac/build_app.sh ]; then
  say "Building the Steward Mac app…"
  ( cd mac && bash build_app.sh ) && ok "Steward.app built" || warn "Mac app build skipped/failed (you can still use Telegram)"
else
  warn "skipped Mac app build (Xcode command-line tools not found — run 'xcode-select --install' if you want the menu-bar app)"
fi

# ── Part B: the human-only steps ───────────────────────────────────────────────
cat <<'EOF'

────────────────────────────────────────────────────────────────────────────
✅ The machine part is done. Now YOUR turn — a few quick steps only you can do
   (full details in SETUP.md):

  1) Get one AI key  →  openrouter.ai → Keys → paste into .env as LLM_API_KEY
  2) Make a Telegram bot  →  message @BotFather, /newbot, paste token into .env
        as TELEGRAM_BOT_TOKEN, then text your bot once.
  3) (optional) Email  →  Google Cloud OAuth → save secrets/client_secret.json
        …or set EMAIL_ENABLED=false to skip email.
  4) (optional) WhatsApp  →  set WHATSAPP_ENABLED=true, start the relay, scan the QR.

  Then: start it (it begins in safe dry-run mode — drafts but sends nothing).
  Flip MODE=live in .env only when you're ready. It always needs your tap to send.

  Tip: open this folder in Claude Code / Cursor and say "finish my setup" — it can
  do everything except the account sign-ups above.
────────────────────────────────────────────────────────────────────────────
EOF
