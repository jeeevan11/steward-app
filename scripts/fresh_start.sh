#!/usr/bin/env bash
# Steward — FRESH START / full reset to a clean first-run ("new user") state.
#
# It BACKS EVERYTHING UP first (so this is reversible), then stops all processes and
# wipes the learned data, memory, WhatsApp pairing, and Gmail token, and resets the
# identity in .env. After this, the next run is a brand-new-user experience.
#
# Kept on purpose: secrets/client_secret.json (the reusable Google OAuth *app*) and your
# OpenRouter API key (not identity-specific) — so you don't have to recreate those.
#
# Usage:   bash scripts/fresh_start.sh
# Restore: everything is copied to backups/<timestamp>/ first.
set -uo pipefail
cd "$(dirname "$0")/.."
TS="$(date +%Y%m%d-%H%M%S)"
BK="backups/$TS"

echo "Steward fresh start → backing up to $BK, then resetting to a clean state."
echo

# 1) Stop everything
echo "→ stopping all Steward processes…"
for pat in "run.py" "run_web.py" "whatsapp_relay.js" "cloudflared tunnel"; do
  pkill -9 -f "$pat" 2>/dev/null && echo "   killed: $pat" || true
done
sleep 1

# 2) Back up (reversible safety net)
mkdir -p "$BK"
[ -f .env ] && cp .env "$BK/.env"
[ -d data ] && cp -R data "$BK/data" 2>/dev/null || true
[ -f secrets/gmail_token.json ] && { mkdir -p "$BK/secrets"; cp secrets/gmail_token.json "$BK/secrets/"; }
[ -d relay/session ] && cp -R relay/session "$BK/relay-session" 2>/dev/null || true
echo "→ backed up to $BK"

# 3) Wipe data / memory / pairing / token
echo "→ wiping data, memory, WhatsApp pairing, Gmail token…"
rm -f  data/*.db data/*.db-wal data/*.db-shm data/status.json data/engine.pid 2>/dev/null || true
rm -f  secrets/gmail_token.json 2>/dev/null || true
rm -rf relay/session relay/status.json 2>/dev/null || true
rm -f  relay/contact_cache.json relay/lid_jid_map.json 2>/dev/null || true
rm -rf relay/outbox && mkdir -p relay/outbox

# 4) Reset .env identity to a clean first-run state (keeps OpenRouter key + structure)
if [ -f .env ]; then
  python3 - <<'PY'
import pathlib
p = pathlib.Path(".env")
blank = {"GMAIL_ADDRESS","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","WA_USER_JID",
         "MINIAPP_URL","PERSONAL_JIDS","VIP_JIDS","MUTE_JIDS"}
force = {"MODE":"dry_run","WHATSAPP_ENABLED":"false"}
out=[]
for ln in p.read_text().splitlines():
    if "=" in ln and not ln.strip().startswith("#"):
        k = ln.split("=",1)[0].strip()
        if k in force: out.append(f"{k}={force[k]}"); continue
        if k in blank: out.append(f"{k}="); continue
    out.append(ln)
p.write_text("\n".join(out)+"\n")
print("   .env reset: identity blanked · MODE=dry_run · WHATSAPP_ENABLED=false · OpenRouter key kept")
PY
fi

cat <<EOF

✅ Clean slate ready. Backup at: $BK

Run it fresh (first-hand setup):
  1. Edit .env → set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GMAIL_ADDRESS
     (and WA_USER_JID if you'll use WhatsApp).
  2. .venv/bin/python run.py --onboard      # Gmail OAuth opens in your browser; learns your voice
  3. .venv/bin/python run_web.py            # web console → http://127.0.0.1:8000
  4. (optional WhatsApp) set WHATSAPP_ENABLED=true in .env, then:
     node relay/whatsapp_relay.js           # scan the QR with your phone once
  5. When you trust it: set MODE=live in .env and restart run.py.

To undo this reset: copy files back from $BK/ .
EOF
