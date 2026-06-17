"""Operating state engine -- ten views that answer "What is my current state?"

Powers the /state Telegram command and the War Room dashboard. Every query is
wrapped in try/except; missing tables (threads, projects, opportunities, risks)
are handled gracefully -- they are expected to be absent until the migration runs.

Stdlib only. All public functions take db (sqlite3.Connection) as first argument.
Never raises -- returns [] or {} on any error.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, timedelta

try:
    from assistant.storage import operating_state as os_store
except ImportError:
    os_store = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _today_iso(today: date | None = None) -> str:
    """Today's date as 'YYYY-MM-DD'. The commitments table stores due_date as an ISO
    date string (db.py), and ISO strings sort lexicographically, so all due-date
    comparisons are done as string comparisons against this value."""
    return (today or date.today()).strftime("%Y-%m-%d")


def _iso_after_days(days: int, today: date | None = None) -> str:
    """The ISO date string `days` days from today (used for the THIS WEEK window end)."""
    return ((today or date.today()) + timedelta(days=days)).strftime("%Y-%m-%d")


def _rows_to_dicts(rows) -> list[dict]:
    """Convert sqlite3.Row objects to plain dicts."""
    out = []
    for r in rows:
        try:
            out.append(dict(r))
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# Ten view functions
# ---------------------------------------------------------------------------

def waiting_on_me(db: sqlite3.Connection) -> list[dict]:
    """Threads where the ball is in our court, oldest first."""
    try:
        now = int(time.time())
        rows = db.execute(
            "SELECT * FROM threads WHERE status='awaiting_me' "
            "ORDER BY last_activity_ts ASC LIMIT 20"
        ).fetchall()
        out = _rows_to_dicts(rows)
        for item in out:
            try:
                item["days_waiting"] = int(
                    (now - int(item.get("last_activity_ts") or now)) / 86400
                )
            except Exception:  # noqa: BLE001
                item["days_waiting"] = 0
        return out
    except sqlite3.OperationalError:
        return []
    except Exception:  # noqa: BLE001
        return []


def waiting_on_them(db: sqlite3.Connection) -> list[dict]:
    """Threads where we are waiting for the other party, oldest first."""
    try:
        now = int(time.time())
        rows = db.execute(
            "SELECT * FROM threads WHERE status='awaiting_them' "
            "ORDER BY last_activity_ts ASC LIMIT 20"
        ).fetchall()
        out = _rows_to_dicts(rows)
        for item in out:
            try:
                item["days_waiting"] = int(
                    (now - int(item.get("last_activity_ts") or now)) / 86400
                )
            except Exception:  # noqa: BLE001
                item["days_waiting"] = 0
        return out
    except sqlite3.OperationalError:
        return []
    except Exception:  # noqa: BLE001
        return []


def blocked_projects(db: sqlite3.Connection) -> list[dict]:
    """Projects currently in a blocked state, oldest first."""
    try:
        rows = db.execute(
            "SELECT * FROM projects WHERE status='blocked' ORDER BY created_at ASC"
        ).fetchall()
        return _rows_to_dicts(rows)
    except sqlite3.OperationalError:
        return []
    except Exception:  # noqa: BLE001
        return []


def overdue_commitments(db: sqlite3.Connection) -> list[dict]:
    """Commitments whose due date has passed and that are not yet done.

    ROOT CAUSE (control-state-presence-3): the prior query filtered on a non-existent
    `completed` column and compared the TEXT `due_date` (stored as 'YYYY-MM-DD', see
    db.py CREATE TABLE commitments) against an epoch INTEGER. The bad column raised
    sqlite3.OperationalError which the except swallowed into [], so OVERDUE / the daily
    state risk derivation were PERMANENTLY empty. The real schema tracks completion via
    `status` ('open' | 'done' | 'snoozed' | 'stale') and stores due_date as a date
    string, so we must compare strings (ISO dates sort lexicographically) and exclude
    only status='done'. Only commitments WITH a concrete due_date can be "overdue".
    """
    try:
        today = _today_iso()
        rows = db.execute(
            "SELECT * FROM commitments "
            "WHERE due_date IS NOT NULL AND due_date != '' AND due_date < ? "
            "AND status != 'done' "
            "ORDER BY due_date ASC",
            (today,),
        ).fetchall()
        return _rows_to_dicts(rows)
    except sqlite3.OperationalError:
        return []
    except Exception:  # noqa: BLE001
        return []


def hot_opportunities(db: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Unresolved opportunities ranked by expected value (value_est * probability)."""
    try:
        rows = db.execute(
            f"SELECT * FROM opportunities WHERE resolved_at IS NULL "
            f"ORDER BY (value_est * probability) DESC, last_activity_ts DESC "
            f"LIMIT {int(limit)}"
        ).fetchall()
        return _rows_to_dicts(rows)
    except sqlite3.OperationalError:
        return []
    except Exception:  # noqa: BLE001
        return []


def this_week_items(db: sqlite3.Connection) -> list[dict]:
    """Commitments due in the next 7 days plus all pending actions, up to 15 items.

    ROOT CAUSE (control-state-presence-3): same schema mismatch as overdue_commitments —
    the commitments leg filtered on a non-existent `completed` column and BETWEEN'd the
    TEXT due_date against epoch integers, so THIS WEEK was permanently empty. We compare
    ISO date strings (today..today+7) and exclude only status='done'. The pending_actions
    leg also used a lowercase status='pending' that never matches the stored upper-case
    'PENDING' (see repositories.open_pending), so it too returned nothing.
    """
    out: list[dict] = []
    today = _today_iso()
    week_end = _iso_after_days(7)

    # Commitments due within 7 days (inclusive). ISO date strings sort lexicographically.
    try:
        rows = db.execute(
            "SELECT *, 'commitment' AS _item_type FROM commitments "
            "WHERE due_date IS NOT NULL AND due_date != '' "
            "AND due_date BETWEEN ? AND ? AND status != 'done' "
            "ORDER BY due_date ASC",
            (today, week_end),
        ).fetchall()
        out.extend(_rows_to_dicts(rows))
    except sqlite3.OperationalError:
        pass
    except Exception:  # noqa: BLE001
        pass

    # Pending actions still awaiting the human. Stored statuses are upper-case
    # (PENDING/APPROVED/EDITED); the old lowercase 'pending' never matched.
    try:
        rows = db.execute(
            "SELECT *, 'pending_action' AS _item_type FROM pending_actions "
            "WHERE status IN ('PENDING','APPROVED','EDITED')"
        ).fetchall()
        out.extend(_rows_to_dicts(rows))
    except sqlite3.OperationalError:
        pass
    except Exception:  # noqa: BLE001
        pass

    return out[:15]


def gone_quiet(db: sqlite3.Connection, days: int = 7) -> list[dict]:
    """Threads we are waiting on that have had no activity for `days` days."""
    try:
        cutoff = int(time.time()) - int(days) * 86400
        rows = db.execute(
            "SELECT * FROM threads WHERE status='awaiting_them' "
            "AND last_activity_ts < ? "
            "ORDER BY last_activity_ts ASC LIMIT 10",
            (cutoff,),
        ).fetchall()
        return _rows_to_dicts(rows)
    except sqlite3.OperationalError:
        return []
    except Exception:  # noqa: BLE001
        return []


def changed_since_yesterday(db: sqlite3.Connection) -> list[dict]:
    """Threads that had activity in the last 24 hours, most recent first."""
    try:
        since = int(time.time()) - 86400
        rows = db.execute(
            "SELECT * FROM threads WHERE last_activity_ts > ? "
            "ORDER BY last_activity_ts DESC LIMIT 10",
            (since,),
        ).fetchall()
        return _rows_to_dicts(rows)
    except sqlite3.OperationalError:
        return []
    except Exception:  # noqa: BLE001
        return []


def top_risks(db: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Unresolved risks ordered by severity (critical > high > medium > other)."""
    try:
        rows = db.execute(
            f"SELECT * FROM risks WHERE resolved_at IS NULL "
            f"ORDER BY CASE severity "
            f"  WHEN 'critical' THEN 0 "
            f"  WHEN 'high' THEN 1 "
            f"  WHEN 'medium' THEN 2 "
            f"  ELSE 3 END, "
            f"created_at DESC "
            f"LIMIT {int(limit)}"
        ).fetchall()
        return _rows_to_dicts(rows)
    except sqlite3.OperationalError:
        return []
    except Exception:  # noqa: BLE001
        return []


def channel_health(db: sqlite3.Connection) -> dict:
    """Return health status of Gmail and WhatsApp relay channels.

    Returns a dict with keys:
      gmail, whatsapp       -- 'ok' | 'stale' | 'unknown'
      gmail_last_ts         -- int | None
      wa_last_ts            -- int | None

    A channel is 'stale' when its last heartbeat was more than 120 seconds ago.
    """
    now = int(time.time())
    stale_threshold = 120

    def _read_ts(key: str):
        try:
            row = db.execute(
                "SELECT value FROM kv WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            return int(row["value"])
        except Exception:  # noqa: BLE001
            return None

    def _status(ts) -> str:
        if ts is None:
            return "unknown"
        return "ok" if (now - ts) <= stale_threshold else "stale"

    gmail_ts = _read_ts("heartbeat_ts")
    wa_ts = _read_ts("wa_relay_last_ok")

    return {
        "gmail": _status(gmail_ts),
        "whatsapp": _status(wa_ts),
        "gmail_last_ts": gmail_ts,
        "wa_last_ts": wa_ts,
    }


# ---------------------------------------------------------------------------
# Snapshot aggregator
# ---------------------------------------------------------------------------

def get_state_snapshot(db: sqlite3.Connection) -> dict:
    """Call all ten view functions and return a single state snapshot dict.

    Each section is individually guarded; a failure in one never blocks the rest.
    Never raises.
    """
    result: dict = {}

    try:
        result["waiting_on_me"] = waiting_on_me(db)
    except Exception:  # noqa: BLE001
        result["waiting_on_me"] = []

    try:
        result["waiting_on_them"] = waiting_on_them(db)
    except Exception:  # noqa: BLE001
        result["waiting_on_them"] = []

    try:
        result["blocked_projects"] = blocked_projects(db)
    except Exception:  # noqa: BLE001
        result["blocked_projects"] = []

    try:
        result["overdue_commitments"] = overdue_commitments(db)
    except Exception:  # noqa: BLE001
        result["overdue_commitments"] = []

    try:
        result["hot_opportunities"] = hot_opportunities(db)
    except Exception:  # noqa: BLE001
        result["hot_opportunities"] = []

    try:
        result["this_week_items"] = this_week_items(db)
    except Exception:  # noqa: BLE001
        result["this_week_items"] = []

    try:
        result["gone_quiet"] = gone_quiet(db)
    except Exception:  # noqa: BLE001
        result["gone_quiet"] = []

    try:
        result["changed_since_yesterday"] = changed_since_yesterday(db)
    except Exception:  # noqa: BLE001
        result["changed_since_yesterday"] = []

    try:
        result["top_risks"] = top_risks(db)
    except Exception:  # noqa: BLE001
        result["top_risks"] = []

    try:
        result["channel_health"] = channel_health(db)
    except Exception:  # noqa: BLE001
        result["channel_health"] = {
            "gmail": "unknown",
            "whatsapp": "unknown",
            "gmail_last_ts": None,
            "wa_last_ts": None,
        }

    return result


# ---------------------------------------------------------------------------
# Telegram /state formatter
# ---------------------------------------------------------------------------

def _trunc(text: str, max_len: int = 60) -> str:
    """Truncate a string to max_len characters."""
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[:max_len - 1].rstrip() + "."


def format_state_chat(snapshot: dict) -> str:
    """Compact plain-text summary of the state snapshot for the /state Telegram command.

    Omits sections whose list is empty. Max 900 chars. No markdown.
    """
    lines: list[str] = ["Your state", ""]

    blocked = snapshot.get("blocked_projects") or []
    if blocked:
        lines.append(f"BLOCKED ({len(blocked)})")
        for p in blocked[:2]:
            name = _trunc(p.get("name") or p.get("title") or p.get("id") or "")
            if name:
                lines.append(f"  {name}")

    waiting_me = snapshot.get("waiting_on_me") or []
    if waiting_me:
        lines.append(f"WAITING ON YOU ({len(waiting_me)})")
        for t in waiting_me[:3]:
            subj = _trunc(t.get("subject") or t.get("title") or t.get("id") or "")
            if subj:
                lines.append(f"  {subj}")

    waiting_them = snapshot.get("waiting_on_them") or []
    if waiting_them:
        lines.append(f"WAITING ON THEM ({len(waiting_them)})")
        for t in waiting_them[:3]:
            subj = _trunc(t.get("subject") or t.get("title") or t.get("id") or "")
            if subj:
                lines.append(f"  {subj}")

    overdue = snapshot.get("overdue_commitments") or []
    if overdue:
        lines.append(f"OVERDUE ({len(overdue)})")
        for c in overdue[:2]:
            desc = _trunc(
                c.get("commitment_text") or c.get("description") or c.get("id") or ""
            )
            if desc:
                lines.append(f"  {desc}")

    hot = snapshot.get("hot_opportunities") or []
    if hot:
        lines.append(f"HOT ({len(hot)})")
        for o in hot[:2]:
            opp_type = (o.get("type") or o.get("kind") or "").strip()
            stage = (o.get("stage") or "").strip()
            parts = [p for p in [opp_type, stage] if p]
            label = _trunc(" / ".join(parts) if parts else o.get("name") or o.get("id") or "")
            if label:
                lines.append(f"  {label}")

    week = snapshot.get("this_week_items") or []
    if week:
        lines.append(f"THIS WEEK ({len(week)} items)")

    risks = snapshot.get("top_risks") or []
    if risks:
        lines.append(f"RISKS ({len(risks)})")

    lines.append("")
    lines.append("[Open War Room]")

    text = "\n".join(lines)
    if len(text) > 900:
        text = text[:899].rstrip() + "."
    return text
