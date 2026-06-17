#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# handover.sh — swap this assistant from one principal to another (e.g. Jatin → Jatin)
#
# What it does (all reversible — it backs everything up first):
#   1. Backs up the current .env, the SQLite DB, and the Gmail token.
#   2. Carries over SHARED infra (OpenRouter key, model ids, paths, thresholds).
#   3. Asks for the NEW principal's Gmail address (+ optionally Telegram bot/chat).
#   4. Writes a fresh .env with MODE=dry_run (safe first boot) and WhatsApp OFF.
#   5. Clears the old DB and Gmail token so onboarding starts clean.
#
# It NEVER overwrites an existing backup, NEVER touches code, and NEVER sends
# anything. Re-running is safe. Target: a clean swap in under 5 minutes.
#
# Usage:  bash handover.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"

OLD_ENV=".env"
ENV_BAK=".env.owner.backup"
DB_PATH="data/assistant.db"
DB_BAK="data/assistant.owner.bak.db"
TOKEN="secrets/gmail_token.json"
TOKEN_BAK="secrets/gmail_token.owner.bak.json"

say()  { printf '%s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }
# Read a value from the old .env (everything after the first '='). Empty if absent.
getval() {
  [ -f "$OLD_ENV" ] || { printf ''; return; }
  grep -E "^$1=" "$OLD_ENV" | head -1 | sed -E "s/^$1=//" || true
}

say "════════════════════════════════════════════════════════════════════"
say "  HANDOVER — point this assistant at a new principal's accounts"
say "════════════════════════════════════════════════════════════════════"
say ""
say "This will:"
say "  • back up   .env → $ENV_BAK"
say "  • back up   $DB_PATH → $DB_BAK"
say "  • back up   $TOKEN → $TOKEN_BAK"
say "  • write a fresh .env (MODE=dry_run, WhatsApp OFF) for the new account"
say "  • clear the old database + Gmail token (so onboarding starts clean)"
say ""
say "Shared settings (OpenRouter key, model ids, paths, thresholds) are kept."
say "Telegram + Gmail + WhatsApp identities are reset to the new person."
say ""

# ── Guard: refuse to clobber an existing backup ──────────────────────────────
if [ -e "$ENV_BAK" ] || [ -e "$DB_BAK" ] || [ -e "$TOKEN_BAK" ]; then
  err "A previous backup already exists ($ENV_BAK / $DB_BAK / $TOKEN_BAK)."
  err "Refusing to overwrite it. Move or delete the old backups first, then re-run."
  exit 1
fi

if [ ! -f "$OLD_ENV" ]; then
  err "No .env found in $(pwd). Nothing to hand over. Copy .env.example → .env first."
  exit 1
fi

printf 'Type exactly "yes" to proceed: '
read -r CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  say "Aborted. Nothing changed."
  exit 0
fi

# ── Question 1 (required): new Gmail address ─────────────────────────────────
NEW_GMAIL=""
while [ -z "$NEW_GMAIL" ]; do
  printf '\n1) New principal'\''s Gmail address (e.g. owner@example.com): '
  read -r NEW_GMAIL
  case "$NEW_GMAIL" in
    *@*.*) : ;;
    *) err "That doesn't look like an email address. Try again."; NEW_GMAIL="" ;;
  esac
done

# ── Question 2 (optional): Telegram bot token + chat id ──────────────────────
say ""
say "2) Telegram (press Enter to skip — you can fill these in later in .env)."
printf '   New Telegram bot token (from @BotFather): '
read -r NEW_TG_TOKEN
printf '   New Telegram chat id: '
read -r NEW_TG_CHAT

# ── Carry over shared infra from the old .env ────────────────────────────────
OPENROUTER_API_KEY="$(getval OPENROUTER_API_KEY)"
OPENROUTER_BASE_URL="$(getval OPENROUTER_BASE_URL)"; OPENROUTER_BASE_URL="${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}"
JUDGE_MODEL="$(getval JUDGE_MODEL)";   JUDGE_MODEL="${JUDGE_MODEL:-google/gemini-2.5-flash}"
NOISE_MODEL="$(getval NOISE_MODEL)";   NOISE_MODEL="${NOISE_MODEL:-google/gemini-2.5-flash}"
DRAFT_MODEL="$(getval DRAFT_MODEL)";   DRAFT_MODEL="${DRAFT_MODEL:-google/gemini-2.5-flash}"
PRO_MODEL="$(getval PRO_MODEL)";       PRO_MODEL="${PRO_MODEL:-google/gemini-2.5-pro}"
GMAIL_CREDENTIALS_PATH="$(getval GMAIL_CREDENTIALS_PATH)"; GMAIL_CREDENTIALS_PATH="${GMAIL_CREDENTIALS_PATH:-./secrets/client_secret.json}"
GMAIL_TOKEN_PATH="$(getval GMAIL_TOKEN_PATH)"; GMAIL_TOKEN_PATH="${GMAIL_TOKEN_PATH:-./secrets/gmail_token.json}"
DB_CFG="$(getval DB_PATH)";            DB_CFG="${DB_CFG:-./data/assistant.db}"
LOG_PATH="$(getval LOG_PATH)";         LOG_PATH="${LOG_PATH:-./data/assistant.log}"
PROMPTS_DIR="$(getval PROMPTS_DIR)";   PROMPTS_DIR="${PROMPTS_DIR:-./prompts}"
TIMEZONE="$(getval TIMEZONE)";         TIMEZONE="${TIMEZONE:-America/Los_Angeles}"
MORNING_BRIEF_HOUR="$(getval MORNING_BRIEF_HOUR)"; MORNING_BRIEF_HOUR="${MORNING_BRIEF_HOUR:-8}"
EVENING_BRIEF_HOUR="$(getval EVENING_BRIEF_HOUR)"; EVENING_BRIEF_HOUR="${EVENING_BRIEF_HOUR:-18}"
POLL_INTERVAL_SECONDS="$(getval POLL_INTERVAL_SECONDS)"; POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-45}"
SURFACE_CONFIDENCE_THRESHOLD="$(getval SURFACE_CONFIDENCE_THRESHOLD)"; SURFACE_CONFIDENCE_THRESHOLD="${SURFACE_CONFIDENCE_THRESHOLD:-0.75}"
AUTONOMY_CONFIDENCE_THRESHOLD="$(getval AUTONOMY_CONFIDENCE_THRESHOLD)"; AUTONOMY_CONFIDENCE_THRESHOLD="${AUTONOMY_CONFIDENCE_THRESHOLD:-0.85}"
VIP_IMPORTANCE_THRESHOLD="$(getval VIP_IMPORTANCE_THRESHOLD)"; VIP_IMPORTANCE_THRESHOLD="${VIP_IMPORTANCE_THRESHOLD:-70}"
WHATSAPP_RELAY_PORT="$(getval WHATSAPP_RELAY_PORT)"; WHATSAPP_RELAY_PORT="${WHATSAPP_RELAY_PORT:-7999}"
WHATSAPP_SEND_PORT="$(getval WHATSAPP_SEND_PORT)";   WHATSAPP_SEND_PORT="${WHATSAPP_SEND_PORT:-7998}"
WATCH_KEYWORDS="$(getval WATCH_KEYWORDS)";           WATCH_KEYWORDS="${WATCH_KEYWORDS:-urgent,decision,asap}"
WHATSAPP_TRANSCRIBE_MODEL="$(getval WHATSAPP_TRANSCRIBE_MODEL)"; WHATSAPP_TRANSCRIBE_MODEL="${WHATSAPP_TRANSCRIBE_MODEL:-google/gemini-2.5-flash}"

if [ -z "$OPENROUTER_API_KEY" ] || [ "$OPENROUTER_API_KEY" = "sk-or-v1-..." ]; then
  say ""
  say "NOTE: no real OpenRouter API key found in the old .env — set OPENROUTER_API_KEY"
  say "      in the new .env before starting."
fi

# ── Back up everything (only after we know we're proceeding) ──────────────────
say ""
say "Backing up..."
cp -p "$OLD_ENV" "$ENV_BAK"; say "  saved $ENV_BAK"
for f in "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"; do
  [ -f "$f" ] && cp -p "$f" "data/$(basename "$f" | sed "s/assistant\.db/assistant.owner.bak.db/")" && say "  saved backup of $f"
done
[ -f "$TOKEN" ] && { cp -p "$TOKEN" "$TOKEN_BAK"; say "  saved $TOKEN_BAK"; }

# ── Write the fresh .env ─────────────────────────────────────────────────────
cat > "$OLD_ENV" <<EOF
# Generated by handover.sh for: $NEW_GMAIL
# MODE is dry_run for a safe first boot. Flip to live only after you've watched it.

# --- LLM via OpenRouter (carried over from previous principal) ---
OPENROUTER_API_KEY=$OPENROUTER_API_KEY
OPENROUTER_BASE_URL=$OPENROUTER_BASE_URL

JUDGE_MODEL=$JUDGE_MODEL
NOISE_MODEL=$NOISE_MODEL
DRAFT_MODEL=$DRAFT_MODEL
PRO_MODEL=$PRO_MODEL

# Addresses that are the PRINCIPAL themselves — inbound from these is never processed.
SELF_ADDRESSES=

# --- Gmail (new principal) ---
GMAIL_CREDENTIALS_PATH=$GMAIL_CREDENTIALS_PATH
GMAIL_TOKEN_PATH=$GMAIL_TOKEN_PATH
GMAIL_ADDRESS=$NEW_GMAIL

# --- Telegram control surface (new principal's own bot + chat) ---
TELEGRAM_BOT_TOKEN=$NEW_TG_TOKEN
TELEGRAM_CHAT_ID=$NEW_TG_CHAT

# --- Behaviour ---
MODE=dry_run
POLL_INTERVAL_SECONDS=$POLL_INTERVAL_SECONDS
SURFACE_CONFIDENCE_THRESHOLD=$SURFACE_CONFIDENCE_THRESHOLD
AUTONOMY_CONFIDENCE_THRESHOLD=$AUTONOMY_CONFIDENCE_THRESHOLD
VIP_IMPORTANCE_THRESHOLD=$VIP_IMPORTANCE_THRESHOLD

# --- Storage / paths ---
DB_PATH=$DB_CFG
LOG_PATH=$LOG_PATH
PROMPTS_DIR=$PROMPTS_DIR

TIMEZONE=$TIMEZONE
MORNING_BRIEF_HOUR=$MORNING_BRIEF_HOUR
EVENING_BRIEF_HOUR=$EVENING_BRIEF_HOUR

# --- WhatsApp relay (OFF after handover — re-pair the new phone, see WHATSAPP.md) ---
WHATSAPP_ENABLED=false
WHATSAPP_RELAY_PORT=$WHATSAPP_RELAY_PORT
WHATSAPP_SEND_PORT=$WHATSAPP_SEND_PORT
PERSONAL_JIDS=
WATCH_KEYWORDS=$WATCH_KEYWORDS
WA_USER_JID=
WHATSAPP_TRANSCRIBE_MODEL=$WHATSAPP_TRANSCRIBE_MODEL
EOF
say "  wrote fresh .env for $NEW_GMAIL"

# ── Clear old DB + Gmail token (backups already made) ────────────────────────
rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm" && say "  cleared old database"
rm -f "$TOKEN" && say "  cleared old Gmail token (a fresh browser login is required)"

say ""
say "════════════════════════════════════════════════════════════════════"
say "  HANDOVER COMPLETE"
say "════════════════════════════════════════════════════════════════════"
say ""
say "Next:"
say "  1. (If $NEW_GMAIL uses a different Google Cloud project, replace"
say "     secrets/client_secret.json with the new OAuth client first.)"
say "  2. Run:  python run.py --onboard"
say "     → opens a browser to log into $NEW_GMAIL, then mines Sent mail"
say "       to learn the new voice and seed contacts."
say "  3. Watch it in dry_run. When happy, set MODE=live in .env and restart."
say ""
say "To roll back: stop the assistant, then"
say "  mv $ENV_BAK .env  &&  mv $DB_BAK $DB_PATH  &&  mv $TOKEN_BAK $TOKEN"
say ""
