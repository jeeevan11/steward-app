#!/usr/bin/env python3
"""Print your Telegram chat id so you can paste it into .env.

Steps:
  1. Open Telegram and send your bot ANY message (e.g. "hi").
  2. Run:  python scripts/get_chat_id.py
  3. Copy the printed id into TELEGRAM_CHAT_ID in .env.

Uses only the standard library + the bot token already in your .env.
"""

import json
import sys
import urllib.request
from pathlib import Path

# Load TELEGRAM_BOT_TOKEN from .env without requiring python-dotenv.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from assistant.config import load_settings  # noqa: E402

settings = load_settings()
token = settings.telegram_bot_token
if not token or token.startswith("123456"):
    print("No TELEGRAM_BOT_TOKEN found in .env. Add it first.")
    sys.exit(1)

url = f"https://api.telegram.org/bot{token}/getUpdates"
try:
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
except Exception as exc:  # noqa: BLE001
    print(f"Couldn't reach Telegram: {exc}")
    sys.exit(1)

if not data.get("ok"):
    print(f"Telegram error: {data}")
    sys.exit(1)

updates = data.get("result", [])
if not updates:
    print(
        "No messages found. Open Telegram, send your bot a message (any text), "
        "then run this again."
    )
    sys.exit(0)

seen: dict[str, str] = {}
for u in updates:
    msg = u.get("message") or u.get("edited_message") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if cid is not None:
        name = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
        seen[str(cid)] = name

if not seen:
    print("Got updates but no chat id — send your bot a normal text message and retry.")
    sys.exit(0)

print("Found chat id(s) — paste the right one into TELEGRAM_CHAT_ID in .env:\n")
for cid, name in seen.items():
    print(f"  {cid}   ({name})")
if len(seen) == 1:
    only = next(iter(seen))
    print(f"\n→ It's almost certainly: TELEGRAM_CHAT_ID={only}")
