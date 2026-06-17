#!/usr/bin/env bash
# Re-arm the demo: put the 4 MOST RECENT cards back to PENDING so they show up again
# for another take. Run this between takes, then hit ↻ refresh in the app.
# Usage:  bash scripts/demo_rearm.sh
set -euo pipefail
cd "$(dirname "$0")/.."

.venv/bin/python - <<'PY'
from assistant.storage import db
import sqlite3
conn = db.open_db("data/assistant.db"); conn.row_factory = sqlite3.Row
last4 = [r["id"] for r in conn.execute("SELECT id FROM pending_actions ORDER BY id DESC LIMIT 4")]
cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_actions)")}
sets = ["status='PENDING'", "decided_at=NULL", "error=''",
        "telegram_message_id=NULL", "telegram_chat_id=NULL"]
if "approval_hash" in cols: sets.append("approval_hash=NULL")
if "sending_started_at" in cols: sets.append("sending_started_at=NULL")
sql = "UPDATE pending_actions SET " + ", ".join(sets) + " WHERE id=?"
for i in last4:
    conn.execute(sql, (i,))
conn.commit()
print("Re-armed the last 4 cards:")
for r in conn.execute("SELECT id,status,substr(summary,1,50) s FROM pending_actions "
                      "WHERE status='PENDING' ORDER BY id DESC LIMIT 4"):
    print(f"  #{r['id']} {r['status']} | {r['s']}")
conn.close()
PY
echo "Done. Hit refresh ↻ in the app and the cards are back."
