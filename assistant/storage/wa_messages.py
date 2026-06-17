"""Universal WhatsApp context log — EVERY message, in or out, surfaced or not.

The owner's rule: the agent must always know the context of everything, regardless of
whether it pings him. This append-only table is that memory. It is deliberately
separate from `whatsapp_inbox` (which is the transient PROCESSING queue for inbound
messages — settling/folding/claiming): this one is the durable HISTORY used to build
relationship context and to know when the owner is handling a conversation himself.

Recorded here, no matter what:
  * every inbound message (including group chatter that is NOT surfaced),
  * every outbound message the OWNER sends (captured via the relay),
so suppression/mute/settling only ever affect NOTIFICATION, never LEARNING.

ADDITIVE: own table via CREATE TABLE IF NOT EXISTS; stdlib only.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

from assistant.logging_setup import get_logger

log = get_logger("wa_messages")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_messages (
    message_id  TEXT PRIMARY KEY,   -- wa_<id> (inbound) or wa_out_<id> (owner outbound)
    jid         TEXT NOT NULL,      -- conversation key (other party for 1:1, group jid)
    sender_jid  TEXT,               -- who actually sent it (the owner, for outbound)
    from_me     INTEGER NOT NULL DEFAULT 0,
    push_name   TEXT,
    body        TEXT,
    is_group    INTEGER NOT NULL DEFAULT 0,
    group_name  TEXT,
    ts          INTEGER NOT NULL,
    -- ── delivery lifecycle (WebMessageInfo.Status: 0=unknown 1=PENDING 2=SERVER_ACK
    --    3=DELIVERY_ACK 4=READ 5=PLAYED). Only OUR outbound (from_me=1) is tracked.
    --    Monotonic: status never regresses; *_at are write-once. NULL read_at means
    --    read-state UNKNOWN (recipient may have read receipts off), never "unread".
    status       INTEGER NOT NULL DEFAULT 0,
    delivered_at INTEGER,           -- our clock when status first reached >=3 (on device)
    read_at      INTEGER,           -- our clock when status first reached >=4 (blue ticks)
    -- 1 = Steward sent this on the owner's behalf (an approved reply). It IS conversation
    -- state (so "you haven't replied" never fires on a chat Steward answered), but it is NOT
    -- the owner personally handling it and NOT his voice — so presence (last_outbound_ts) and
    -- the style corpus (owner_outbound) deliberately exclude it.
    is_agent     INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_wamsg_jid_ts ON wa_messages(jid, ts);
-- Per-recipient group read state — CONTEXT ONLY (the "not coming back" nudge is 1:1-only,
-- so a group is never nudged off a single fast reader's receipt).
CREATE TABLE IF NOT EXISTS wa_receipts (
    message_id TEXT NOT NULL,       -- the wa_messages.message_id this receipt is for
    user_jid   TEXT NOT NULL,       -- which group member delivered/read it
    status     INTEGER NOT NULL DEFAULT 0,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_jid)
);
"""

# The monotonic delivery-status enum (mirrors Baileys' WebMessageInfo.Status).
SERVER_ACK = 2
DELIVERY_ACK = 3
READ = 4


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # The status/delivered_at/read_at columns are guaranteed by either this _SCHEMA (fresh
    # DBs) or migrations._migrate_wa_messages (a live pre-existing table). Create the sweep's
    # perf index defensively — a no-op if a not-yet-migrated connection lacks the column, so
    # context capture is never broken by index creation.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wamsg_from_status ON wa_messages(from_me, status, ts)"
        )
    except sqlite3.Error:
        pass


def record(conn: sqlite3.Connection, fields: dict[str, Any], *,
           from_me: bool = False, is_agent: bool = False) -> bool:
    """Append one message to the history (dedup on message_id). Best-effort — context
    capture must never block ingestion. Returns True if newly inserted. ``is_agent`` marks a
    reply Steward sent on the owner's behalf (still conversation state, but never presence/style)."""
    ensure(conn)
    mid = str(fields.get("message_id") or fields.get("messageId") or "").strip()
    jid = (fields.get("jid") or "").strip()
    if not mid or not jid:
        return False
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO wa_messages "
            "(message_id, jid, sender_jid, from_me, push_name, body, is_group, group_name, ts, is_agent) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                mid, jid,
                fields.get("sender_jid", "") or jid,
                1 if from_me else 0,
                fields.get("push_name", ""),
                fields.get("body", ""),
                1 if fields.get("is_group") else 0,
                fields.get("group_name", ""),
                int(fields.get("ts") or fields.get("timestamp") or time.time()),
                1 if is_agent else 0,
            ),
        )
        return cur.rowcount == 1
    except sqlite3.Error as exc:
        log.debug("wa_messages.record failed (non-fatal): %s", exc)
        return False


def is_agent_send(conn: sqlite3.Connection, raw_id: str) -> bool:
    """True if a raw WhatsApp id is already recorded as one of STEWARD's own sends (the
    `wa_<id>` is_agent=1 row written by send_reply). Lets ingest_outbound recognize the relay's
    delayed fromMe echo of an agent send and NOT re-record it as `wa_out_<id>` (is_agent=0) —
    which would pollute presence (last_outbound_ts) + the style corpus."""
    ensure(conn)
    r = (raw_id or "").strip()
    if not r:
        return False
    row = conn.execute(
        "SELECT 1 FROM wa_messages WHERE message_id='wa_'||? AND is_agent=1 LIMIT 1", (r,)
    ).fetchone()
    return row is not None


def contact_name(conn: sqlite3.Connection, jid: str) -> str:
    """Best display name for the OTHER party in a chat — the most recent INBOUND push_name.
    Never derived from an owner-authored (from_me=1) row, whose push_name is the OWNER's own
    name (so a 'they haven't replied' FYI never accidentally names the owner himself)."""
    ensure(conn)
    j = (jid or "").strip()
    if not j:
        return ""
    row = conn.execute(
        "SELECT push_name FROM wa_messages WHERE jid=? AND from_me=0 "
        "AND push_name IS NOT NULL AND trim(push_name)!='' ORDER BY ts DESC LIMIT 1", (j,)
    ).fetchone()
    return (row["push_name"] or "").strip() if row else ""


def recent(conn: sqlite3.Connection, jid: str, *, since_ts: int, limit: int = 60) -> list[sqlite3.Row]:
    """All messages in one conversation since `since_ts`, oldest first (for context)."""
    ensure(conn)
    return list(conn.execute(
        "SELECT * FROM wa_messages WHERE jid=? AND ts>=? ORDER BY ts ASC, created_at ASC LIMIT ?",
        (jid, int(since_ts), limit),
    ))


def last_outbound_ts(conn: sqlite3.Connection, jid: str) -> int:
    """Timestamp of the owner's most recent message in this conversation (0 if none).
    This is the reliable 'am I handling this chat myself right now?' signal."""
    ensure(conn)
    # is_agent=0: presence is about the OWNER personally handling the chat. A reply Steward
    # sent on his behalf must NOT read as "the owner is here" (that would suppress real pings).
    row = conn.execute(
        "SELECT MAX(ts) AS t FROM wa_messages WHERE jid=? AND from_me=1 AND is_agent=0", (jid,)
    ).fetchone()
    return int(row["t"]) if row and row["t"] is not None else 0


def owner_outbound(conn: sqlite3.Connection, *, limit: int = 40, group: bool = False) -> list[sqlite3.Row]:
    """The owner's own recent WhatsApp messages, newest first — the corpus for learning
    his talking style. `group=False` keeps it to 1:1 chats (cleaner style signal)."""
    ensure(conn)
    # is_agent=0: the style corpus is the OWNER's own voice — never Steward's drafted words.
    return list(conn.execute(
        "SELECT * FROM wa_messages WHERE from_me=1 AND is_agent=0 AND is_group=? "
        "AND body IS NOT NULL AND length(trim(body))>0 ORDER BY ts DESC LIMIT ?",
        (1 if group else 0, limit),
    ))


# ── delivery-receipt lifecycle ───────────────────────────────────────────────
_MONOTONIC_UPDATE = (
    "UPDATE wa_messages SET status=MAX(status, :s), "
    " delivered_at=CASE WHEN delivered_at IS NULL AND :s>=3 THEN :ts ELSE delivered_at END, "
    " read_at=CASE WHEN read_at IS NULL AND :s>=4 THEN :ts ELSE read_at END "
    "WHERE message_id=:mid AND from_me=1 AND :s>status"
)


def apply_receipt(
    conn: sqlite3.Connection,
    *,
    raw_id: str,
    status: int,
    ts: Optional[int] = None,
    remote_jid: str = "",
    participant: str = "",
) -> bool:
    """Record a delivery/read receipt for one of OUR outbound messages.

    Monotonic by construction: status only ever moves FORWARD (MAX + ``:s>status`` guard),
    and delivered_at/read_at are write-once — so an out-of-order, duplicate, or replayed
    receipt is a harmless no-op. A receipt for a message we don't have (a pre-feature send,
    or an inbound) is a SILENT no-op — never a stub INSERT (that would inject a body-less
    from_me=1 row and corrupt last_outbound_ts). Best-effort; never raises into the receiver.

    For a GROUP message the per-recipient state goes to wa_receipts (context only) and the
    row's own status is capped at DELIVERY_ACK, so one fast reader can't mark the whole group
    "read" — the 1:1 row columns are what the silence sweep reads. Returns True on a real bump.
    """
    try:
        s = int(status)
    except (TypeError, ValueError):
        return False
    raw = str(raw_id or "").strip()
    if not raw or s <= 0:
        return False
    t = int(ts if ts is not None else time.time())
    try:
        ensure(conn)
        row = conn.execute(
            "SELECT message_id, is_group FROM wa_messages "
            "WHERE message_id IN ('wa_'||?, 'wa_out_'||?)",
            (raw, raw),
        ).fetchone()
        if row is None:
            return False  # no such message — silent no-op (never a stub insert)
        mid = row["message_id"]
        is_group = bool(row["is_group"]) or (remote_jid or "").endswith("@g.us")
        if is_group and participant:
            conn.execute(
                "INSERT INTO wa_receipts (message_id, user_jid, status, ts) VALUES (?,?,?,?) "
                "ON CONFLICT(message_id, user_jid) DO UPDATE SET status=MAX(status, excluded.status), "
                " ts=excluded.ts WHERE excluded.status > wa_receipts.status",
                (mid, participant, s, t),
            )
            s = min(s, DELIVERY_ACK)  # never let one member's READ mark the group read
        cur = conn.execute(_MONOTONIC_UPDATE, {"s": s, "ts": t, "mid": mid})
        return cur.rowcount > 0
    except sqlite3.Error as exc:
        log.debug("wa_messages.apply_receipt failed (non-fatal): %s", exc)
        return False


_DEFAULT_MAX_AGE = 4 * 86400   # never dredge up silences older than this (no first-run burst)

# The silence-sweep queries also exclude '@newsletter' and '@broadcast' jids: a channel
# broadcast or a status post is never a two-way chat the owner "replies to" (groups are
# already excluded via is_group). 1:1 chats are @s.whatsapp.net or @lid.


def stuck_outbound(
    conn: sqlite3.Connection, *, stuck_secs: int, now: Optional[int] = None,
    max_age_secs: int = _DEFAULT_MAX_AGE, limit: int = 50,
) -> list[sqlite3.Row]:
    """The owner's 1:1 sends that are explicitly PENDING (status==1: reached our linked device
    but NEVER got SERVER_ACK) and stuck longer than ``stuck_secs`` — "not going" candidates.

    status==1 ONLY: status==0 is "no receipt data / unknown" (every pre-feature row, which we
    must never flag as failed). is_agent=0: Steward's own sends have the SEND_AMBIGUOUS path.
    Bounded to messages newer than ``max_age_secs`` so the first sweep can't nag about ancient
    sends we'll never get an update for."""
    ensure(conn)
    n = int(now if now is not None else time.time())
    cutoff = n - max(1, int(stuck_secs))
    floor = n - max(1, int(max_age_secs))
    return list(conn.execute(
        "SELECT message_id, jid, body, ts, push_name, status FROM wa_messages "
        "WHERE from_me=1 AND is_agent=0 AND is_group=0 AND status=1 AND ts<? AND ts>? AND jid NOT LIKE '%@newsletter' AND jid NOT LIKE '%@broadcast' "
        "ORDER BY ts ASC LIMIT ?",
        (cutoff, floor, limit),
    ))


def stuck_agent_send(
    conn: sqlite3.Connection, *, stuck_secs: int, now: Optional[int] = None,
    max_age_secs: int = _DEFAULT_MAX_AGE, limit: int = 50,
) -> list[sqlite3.Row]:
    """Replies STEWARD sent on the owner's behalf (is_agent=1) that never reached SERVER_ACK
    (status<2) past ``stuck_secs`` — the safety net for an approved reply that may have been
    enqueued but not delivered. Mirrors stuck_outbound but for the is_agent=1 path, which the
    owner-send sweep and the SENDING reaper deliberately don't cover. 1:1 only, recency-floored."""
    ensure(conn)
    n = int(now if now is not None else time.time())
    cutoff = n - max(1, int(stuck_secs))
    floor = n - max(1, int(max_age_secs))
    return list(conn.execute(
        "SELECT message_id, jid, body, ts, push_name, status FROM wa_messages "
        "WHERE from_me=1 AND is_agent=1 AND is_group=0 AND status<? AND ts<? AND ts>? "
        "AND jid NOT LIKE '%@newsletter' AND jid NOT LIKE '%@broadcast' "
        "ORDER BY ts ASC LIMIT ?",
        (SERVER_ACK, cutoff, floor, limit),
    ))


def unanswered_outbound(
    conn: sqlite3.Connection, *, min_age_secs: int, now: Optional[int] = None,
    max_age_secs: int = _DEFAULT_MAX_AGE, limit: int = 50,
) -> list[sqlite3.Row]:
    """1:1 chats where the LATEST message is the owner's own, DELIVERED (status>=3), older
    than ``min_age_secs`` with no reply since — "they haven't come back to you".

    is_agent=0: nudge only about the owner's OWN pending conversations, not Steward's auto-
    replies. status>=3 means we have real delivery evidence (a status==0 send is never flagged
    here). Bounded below by ``max_age_secs`` so the first sweep skips ancient threads."""
    ensure(conn)
    n = int(now if now is not None else time.time())
    cutoff = n - max(1, int(min_age_secs))
    floor = n - max(1, int(max_age_secs))
    return list(conn.execute(
        "SELECT m.jid AS jid, m.message_id AS message_id, m.body AS body, m.ts AS ts, "
        "       m.status AS status, m.read_at AS read_at, m.push_name AS push_name "
        "FROM wa_messages m WHERE m.is_group=0 AND m.from_me=1 AND m.is_agent=0 "
        "  AND m.status>=? AND m.ts<? AND m.ts>? "
        "  AND m.jid NOT LIKE '%@newsletter' AND m.jid NOT LIKE '%@broadcast' "
        "  AND m.ts = (SELECT MAX(x.ts) FROM wa_messages x WHERE x.jid=m.jid) "
        "ORDER BY m.ts ASC LIMIT ?",
        (DELIVERY_ACK, cutoff, floor, limit),
    ))


def unanswered_inbound(
    conn: sqlite3.Connection, *, min_age_secs: int, now: Optional[int] = None,
    max_age_secs: int = _DEFAULT_MAX_AGE, limit: int = 50,
) -> list[sqlite3.Row]:
    """1:1 chats where the LATEST message is THEIRS, older than ``min_age_secs`` (with no owner
    reply since) but newer than ``max_age_secs`` — "you haven't come back to them". The recency
    floor keeps the first sweep from dredging up long-stale threads the owner consciously left."""
    ensure(conn)
    n = int(now if now is not None else time.time())
    cutoff = n - max(1, int(min_age_secs))
    floor = n - max(1, int(max_age_secs))
    return list(conn.execute(
        "SELECT m.jid AS jid, m.message_id AS message_id, m.body AS body, m.ts AS ts, "
        "       m.push_name AS push_name, m.sender_jid AS sender_jid "
        "FROM wa_messages m WHERE m.is_group=0 AND m.from_me=0 AND m.ts<? AND m.ts>? AND m.jid NOT LIKE '%@newsletter' AND m.jid NOT LIKE '%@broadcast' "
        "  AND m.ts = (SELECT MAX(x.ts) FROM wa_messages x WHERE x.jid=m.jid) "
        "ORDER BY m.ts ASC LIMIT ?",
        (cutoff, floor, limit),
    ))
