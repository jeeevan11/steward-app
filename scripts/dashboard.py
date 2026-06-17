#!/usr/bin/env python3
"""Steward — internal analytical dashboard.

A full, at-a-glance readout of the live system, straight from the SQLite DB + status
files. For the operator/dev to "see everything" without the web UI. Every section is
best-effort so a missing table never blanks the whole view.

Usage:
    .venv/bin/python scripts/dashboard.py            # one snapshot
    .venv/bin/python scripts/dashboard.py --watch    # refresh every 3s (Ctrl-C to stop)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from assistant.config import load_settings  # noqa: E402
from assistant.storage import db as dbmod  # noqa: E402
from assistant.storage import ledger  # noqa: E402
from assistant.storage import read_queries as rq  # noqa: E402
from assistant.storage import repositories as repo  # noqa: E402

C = "\033[1;36m"; M = "\033[1;35m"; G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; X = "\033[0m"


def _h(title: str) -> None:
    print(f"\n{C}── {title} ──{X}")


def _ago(epoch) -> str:
    try:
        s = max(0, int(time.time()) - int(epoch or 0))
    except (TypeError, ValueError):
        return "?"
    if s < 60: return f"{s}s ago"
    if s < 3600: return f"{s // 60}m ago"
    if s < 86400: return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def render() -> None:
    settings = load_settings()
    conn = dbmod.open_db(settings.db_path)

    print(f"{M}{'=' * 62}{X}")
    print(f"{M}  STEWARD — internal dashboard{X}")
    print(f"{M}{'=' * 62}{X}")
    live = "" if settings.dry_run else f"{R}LIVE{X}"
    print(f"  mode={settings.mode} {live}   paused={repo.is_paused(conn)}   "
          f"email={settings.email_enabled}  whatsapp={settings.whatsapp_enabled}")
    print(f"  gmail={settings.gmail_address}")

    _h("Engine heartbeat")
    try:
        hb = Path(settings.db_path).parent / "status.json"
        if hb.exists():
            st = json.loads(hb.read_text(encoding="utf-8"))
            age = repo.now_epoch() - int(st.get("heartbeat_ts") or 0)
            colour = G if age < 120 else R
            print(f"  {colour}heartbeat {_ago(st.get('heartbeat_ts'))}{X}  pending={st.get('pending')}  "
                  f"last24h={st.get('last_24h')}")
        else:
            print(f"  {R}no heartbeat — engine not running{X}")
    except Exception as e:  # noqa: BLE001
        print(f"  n/a ({e})")

    _h("WhatsApp relay")
    try:
        p = Path(settings.relay_status_path)
        if p.exists():
            st = json.loads(p.read_text(encoding="utf-8"))
            conn_ok = st.get("connected")
            colour = G if conn_ok else R
            print(f"  {colour}connected={conn_ok}{X}  msgs_today={st.get('messages_today')}  "
                  f"status {_ago(st.get('updated_at'))}")
        else:
            print(f"  {Y}no relay status.json — relay not running{X}")
    except Exception as e:  # noqa: BLE001
        print(f"  n/a ({e})")

    _h("Last 24h (ledger states)")
    try:
        c = ledger.counts_since(conn, repo.now_epoch() - 86400)
        print("  " + ("  ".join(f"{k}={v}" for k, v in c.items()) if c else "(none)"))
    except Exception as e:  # noqa: BLE001
        print(f"  n/a ({e})")

    _h("Decision stats (lifetime)")
    try:
        s = rq.get_stats(conn)
        print(f"  conversations={s.get('conversations')}   replies_sent={s.get('sent')}   "
              f"drafts_waiting={s.get('replies_waiting')}")
        print(f"  handled_quietly={s.get('handled_quietly')}   flagged={s.get('flagged_for_you')}   "
              f"near_misses={s.get('near_misses')}")
    except Exception as e:  # noqa: BLE001
        print(f"  n/a ({e})")

    _h("Tier distribution (decision_log)")
    try:
        from assistant.storage import decision_log
        decision_log.ensure(conn)
        rows = conn.execute(
            "SELECT final_tier AS t, COUNT(*) AS n FROM decision_log GROUP BY final_tier ORDER BY final_tier"
        ).fetchall()
        names = {0: "T0 silent", 1: "T1 fyi", 2: "T2 approve", 3: "T3 ask"}
        if not rows:
            print("  (no decisions yet)")
        for r in rows:
            print(f"  {names.get(r['t'], str(r['t'])):<11} {r['n']}")
    except Exception as e:  # noqa: BLE001
        print(f"  n/a ({e})")

    _h("Open queue (awaiting you)")
    try:
        pend = repo.open_pending(conn)
        if not pend:
            print("  (empty)")
        for r in pend[:15]:
            print(f"  #{r['id']} T{r['tier']} {(r['kind'] or ''):<11} {(r['status'] or ''):<9} "
                  f"{(r['summary'] or '')[:48]}")
    except Exception as e:  # noqa: BLE001
        print(f"  n/a ({e})")

    _h("Recent activity (24h)")
    try:
        acts = rq.list_audit(conn, repo.now_epoch() - 86400)
        if not acts:
            print("  (none)")
        for a in acts[:12]:
            print(f"  {_ago(a.get('at')):>8}  {a.get('what', ''):<18} {(a.get('detail') or '')[:42]}")
    except Exception as e:  # noqa: BLE001
        print(f"  n/a ({e})")

    conn.close()


if __name__ == "__main__":
    if "--watch" in sys.argv:
        try:
            while True:
                os.system("clear")
                render()
                print(f"\n{C}(watching — Ctrl-C to stop){X}")
                time.sleep(3)
        except KeyboardInterrupt:
            pass
    else:
        render()
