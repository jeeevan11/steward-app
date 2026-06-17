"""Layer 1C — rolling WhatsApp conversation context.

Render the last N days of a chat (both directions, from the universal history store)
into a compact block the classifier and drafter can read, so the brain reasons over
the whole relationship rather than a single just-arrived line. Pure read; best-effort.
"""

from __future__ import annotations

import sqlite3
import time

from assistant.storage import wa_messages

_MAX_LINES = 40        # cap how many recent turns we include
_MAX_BODY = 240        # cap each line so the block stays compact
_MAX_BLOCK = 4000      # hard cap on the whole block


def _has(row: sqlite3.Row, col: str) -> bool:
    try:
        return col in row.keys()
    except Exception:  # noqa: BLE001
        return False


def _label(row: sqlite3.Row, me_jid: str) -> str:
    if row["from_me"]:
        # Distinguish a reply Steward sent on the owner's behalf from the owner's own message,
        # so the brain reasons correctly about who actually spoke.
        if _has(row, "is_agent") and row["is_agent"]:
            return "Steward (on your behalf)"
        return "Me"
    return (row["push_name"] or row["sender_jid"] or "Them").strip() or "Them"


def _status_tag(row: sqlite3.Row) -> str:
    """A compact delivery-state tag for OUR outbound lines, so the drafter knows whether the
    prior message landed, was read, or never went — and can address the silence or resend
    instead of re-pinging content. Empty for inbound and for pre-feature rows (status 0)."""
    if not row["from_me"] or not _has(row, "status"):
        return ""
    try:
        s = int(row["status"] or 0)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""          # unknown / pre-feature — say nothing rather than guess
    if s == 1:
        return " [NOT DELIVERED]"
    if s == 2:
        return " [sent]"
    if s == 3:
        return " [delivered, unread]"
    return " [read]"        # s >= 4


def recent_block(conn: sqlite3.Connection, jid: str, *, days: int = 14, me_jid: str = "") -> str:
    """A compact 'recent conversation' block for `jid` over the last `days`. Empty when
    there is nothing (or on any error) — never raises."""
    if not jid:
        return ""
    try:
        since = int(time.time()) - max(1, int(days)) * 86400
        rows = wa_messages.recent(conn, jid, since_ts=since, limit=200)
        if not rows or len(rows) <= 1:
            return ""   # nothing meaningful beyond the current message
        rows = rows[-_MAX_LINES:]
        lines = ["=== RECENT CONVERSATION (last %d days, this chat) ===" % int(days)]
        for r in rows:
            body = (r["body"] or "").strip().replace("\n", " ")
            if len(body) > _MAX_BODY:
                body = body[:_MAX_BODY].rstrip() + " …"
            if body:
                lines.append(f"{_label(r, me_jid)}: {body}{_status_tag(r)}")
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)[:_MAX_BLOCK]
    except Exception:  # noqa: BLE001 - context is additive; never break the pipeline
        return ""
