"""Durable inbox for raw WhatsApp payloads from the relay.

Why this exists: the exactly-once ledger stores only message ids, but
`WhatsAppSource.get_thread(id)` needs the actual message (body/jid/media) to rebuild
a Thread, and an un-processed payload must survive a crash (never skip a message).
So the relay's payload is persisted HERE before its id is ever handed to the poller
— the same "record-before-advance" durability the Gmail source uses.

ADDITIVE: its own table via CREATE TABLE IF NOT EXISTS; touches no existing storage
file or schema. Stdlib only.

status flow:  new ──settled & fetched──▶ queued ──process──▶ (ledger owns it)
              new ──folded into a later burst message──▶ folded (folded_into=<rep id>)
              new ──group-skip at intake──▶ skipped (+ ledger marked DONE)

Settling (debounce): people text line-by-line. Rather than process each line, a
conversation's burst is held as `new` until it goes quiet; then the LATEST message
becomes the "representative" (queued + ledgered, exactly once), and the earlier lines
are marked `folded` pointing at it. `get_thread(representative)` reassembles the whole
burst from the representative + its folded members, so the brain sees full context and
the owner gets a single card.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

from assistant.logging_setup import get_logger

log = get_logger("whatsapp_inbox")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS whatsapp_inbox (
    message_id   TEXT PRIMARY KEY,   -- "wa_{baileys message id}"
    jid          TEXT,               -- chat JID (sender, or group)
    sender_jid   TEXT,               -- actual sender (participant in groups)
    push_name    TEXT,
    phone_number TEXT,               -- resolved real phone e.g. '+919164536565', NULL for unresolved LIDs
    body         TEXT,               -- text, or transcript/placeholder for media
    media_type   TEXT,               -- "", "audio", "image", ...
    media_b64    TEXT,               -- base64 audio for transcription (cleared after)
    audio_format TEXT,
    is_group     INTEGER NOT NULL DEFAULT 0,
    group_name   TEXT,
    quoted_body  TEXT,
    mentions     TEXT,               -- comma-separated JIDs mentioned
    ts           INTEGER,
    status       TEXT NOT NULL DEFAULT 'new',   -- new | queued | folded | skipped
    folded_into  TEXT,                          -- representative msg id (for folded rows)
    opaque       INTEGER NOT NULL DEFAULT 0,     -- 1 = media arrived but couldn't be transcribed/described
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_wainbox_status ON whatsapp_inbox(status);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # Additive migration: an inbox created before settling shipped has no folded_into
    # column. Add it in place (cheap, idempotent) so existing live DBs upgrade safely.
    # The folded index is created AFTER the column is guaranteed to exist (fresh DBs get
    # it from CREATE TABLE; old DBs from the ALTER) — never inside the schema script.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(whatsapp_inbox)")}
    if "folded_into" not in cols:
        try:
            conn.execute("ALTER TABLE whatsapp_inbox ADD COLUMN folded_into TEXT")
        except sqlite3.OperationalError:
            # Another connection won the race and already added it — that's fine.
            pass
    # Fix 5: opaque-media flag. Additive on existing DBs (fresh DBs get it from _SCHEMA).
    if "opaque" not in cols:
        try:
            conn.execute("ALTER TABLE whatsapp_inbox ADD COLUMN opaque INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    if "phone_number" not in cols:
        try:
            conn.execute("ALTER TABLE whatsapp_inbox ADD COLUMN phone_number TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wainbox_folded ON whatsapp_inbox(folded_into)")


def _normalize_media_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """GAP 8 — accept both the relay's native shape (media_type + media_b64) and the
    spec's audio_b64/image_b64 fields. Maps the latter onto media_type/media_b64 so the
    rest of the pipeline (transcribe/describe) is unchanged. Non-destructive copy."""
    f = dict(fields)
    if not f.get("media_b64") and not f.get("media_type"):
        if f.get("audio_b64"):
            f["media_type"] = "audio"
            f["media_b64"] = f["audio_b64"]
            f.setdefault("audio_format", f.get("audio_format") or "ogg")
        elif f.get("image_b64"):
            f["media_type"] = "image"
            f["media_b64"] = f["image_b64"]
    return f


def put(conn: sqlite3.Connection, message_id: str, fields: dict[str, Any], *, status: str = "new") -> bool:
    """Persist a payload. Returns True if newly inserted (dedup on message_id)."""
    ensure(conn)
    fields = _normalize_media_fields(fields)
    cur = conn.execute(
        "INSERT OR IGNORE INTO whatsapp_inbox "
        "(message_id, jid, sender_jid, push_name, phone_number, body, media_type, media_b64, "
        " audio_format, is_group, group_name, quoted_body, mentions, ts, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            message_id,
            fields.get("jid", ""),
            fields.get("sender_jid", "") or fields.get("jid", ""),
            fields.get("push_name", ""),
            fields.get("phone_number") or None,
            fields.get("body", ""),
            fields.get("media_type", ""),
            fields.get("media_b64", ""),
            fields.get("audio_format", ""),
            1 if fields.get("is_group") else 0,
            fields.get("group_name", ""),
            fields.get("quoted_body", ""),
            ",".join(fields.get("mentions", []) or []),
            int(fields.get("ts") or time.time()),
            status,
        ),
    )
    return cur.rowcount == 1


def get(conn: sqlite3.Connection, message_id: str) -> Optional[sqlite3.Row]:
    ensure(conn)
    return conn.execute(
        "SELECT * FROM whatsapp_inbox WHERE message_id=?", (message_id,)
    ).fetchone()


# ingest-whatsapp-7 ROOT CAUSE FIX
# ────────────────────────────────
# The old query was a FLAT global window:
#     SELECT * ... WHERE status='new' ORDER BY created_at ASC LIMIT 100
# The settling planner is fed exactly these rows. A single active group in which an
# attacker (or a runaway bot) sends 100+ owner-mentioning lines within the group's
# hold window keeps every one of those lines status='new' (an active group never
# settles), and — being the OLDEST rows — they fully occupy the top-100 ORDER BY
# created_at window every poll. A genuinely urgent 1:1 (even a VIP) that arrives
# AFTER the flood has a higher created_at, falls outside the top-100, and is never
# handed to plan_settling — so it is never settled, never released, never surfaced.
# _vip_instant_jids is built from the same capped rows, so VIP instant-release can't
# rescue it either. Result: anyone in a shared group can suppress the owner's other
# replies for hours by spamming.
#
# Fix: do not budget the planner input by a flat global LIMIT across all jids. Select
# fairly PER conversation so one noisy jid cannot crowd out others. We:
#   1. find every DISTINCT pending jid (ordered by its oldest waiting row, so the
#      longest-waiting conversation is served first), and
#   2. pull a bounded slice (`per_jid_cap`) of the oldest rows for each jid,
#   3. round-robin across jids so EVERY conversation with a pending 'new' row is
#      represented in plan_settling before any single jid is deepened — even when the
#      busiest group has thousands of rows.
# This is purely a SELECTION-fairness change: which rows are released is still decided
# by plan_settling on our receive clock; a flooding group simply can no longer consume
# the window reserved for settle-eligible 1:1s. Bounded total cost: O(distinct jids ×
# per_jid_cap) rows, capped by `limit`.

# Per-jid slice cap for one planner pass. Generous enough that a real multi-line burst
# folds into one card, small enough that no single jid can monopolise the budget.
# Overridable via env without touching the shared config.py (a file this cluster must
# not edit); safe default constant lives here.
_DEFAULT_PER_JID_CAP = 20


def _per_jid_cap() -> int:
    import os
    raw = os.environ.get("WHATSAPP_INBOX_PER_JID_CAP")
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    return _DEFAULT_PER_JID_CAP


def list_new(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Pending ('new') rows for the settling planner, selected PER-JID-FAIRLY so one
    flooding conversation cannot starve others out of the window (ingest-whatsapp-7).

    Returns at most `limit` rows. Every distinct pending jid is represented (round-robin)
    before any single jid is given more than one slot, and no jid contributes more than
    `per_jid_cap` rows. Falls back to the legacy flat query on any error so ingestion is
    never blocked."""
    ensure(conn)
    try:
        return _list_new_fair(conn, limit=limit, per_jid_cap=_per_jid_cap())
    except sqlite3.Error:
        # Fail-safe: never let a fairness-selection error stop the planner from seeing
        # SOMETHING. The flat query is the pre-fix behavior.
        log.warning("fair list_new failed; falling back to flat window", exc_info=True)
        return list(
            conn.execute(
                "SELECT * FROM whatsapp_inbox WHERE status='new' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
        )


def _list_new_fair(
    conn: sqlite3.Connection, *, limit: int, per_jid_cap: int
) -> list[sqlite3.Row]:
    """Round-robin fair selection of pending rows across distinct jids. Pure SQL +
    in-process interleave; no row is ever folded/queued here (read-only)."""
    if limit <= 0:
        return []
    # 1) Distinct pending jids, longest-waiting first (oldest min(created_at)).
    jids = [
        r[0]
        for r in conn.execute(
            "SELECT jid FROM whatsapp_inbox WHERE status='new' "
            "GROUP BY jid ORDER BY MIN(created_at) ASC, jid ASC"
        )
    ]
    if not jids:
        return []
    # 2) Pull a bounded per-jid slice (oldest first) for each jid, capped at per_jid_cap.
    #    A flooding jid contributes at most per_jid_cap rows regardless of its true depth.
    per_jid: dict[str, list[sqlite3.Row]] = {}
    for jid in jids:
        per_jid[jid] = list(
            conn.execute(
                "SELECT * FROM whatsapp_inbox WHERE status='new' AND jid=? "
                "ORDER BY created_at ASC, message_id ASC LIMIT ?",
                (jid, per_jid_cap),
            )
        )
    # 3) Round-robin interleave so every jid gets its first row before any jid gets a
    #    second — guaranteeing representation for every conversation within `limit`.
    out: list[sqlite3.Row] = []
    depth = 0
    while len(out) < limit:
        progressed = False
        for jid in jids:
            slice_ = per_jid[jid]
            if depth < len(slice_):
                out.append(slice_[depth])
                progressed = True
                if len(out) >= limit:
                    break
        if not progressed:
            break  # every jid exhausted its capped slice
        depth += 1
    return out


def mark_queued(conn: sqlite3.Connection, message_id: str) -> None:
    conn.execute("UPDATE whatsapp_inbox SET status='queued' WHERE message_id=?", (message_id,))


def mark_folded(conn: sqlite3.Connection, message_id: str, into: str) -> None:
    """Roll an earlier burst line into a later representative message. The folded row
    is never processed or ledgered on its own; it resurfaces only as part of the
    representative's reassembled thread."""
    conn.execute(
        "UPDATE whatsapp_inbox SET status='folded', folded_into=? WHERE message_id=?",
        (into, message_id),
    )


def folded_members(conn: sqlite3.Connection, representative_id: str) -> list[sqlite3.Row]:
    """The earlier burst lines folded into this representative, oldest first."""
    ensure(conn)
    return list(
        conn.execute(
            "SELECT * FROM whatsapp_inbox WHERE folded_into=? ORDER BY ts ASC, created_at ASC",
            (representative_id,),
        )
    )


def mark_skipped(conn: sqlite3.Connection, message_id: str) -> None:
    conn.execute("UPDATE whatsapp_inbox SET status='skipped' WHERE message_id=?", (message_id,))


def set_body(conn: sqlite3.Connection, message_id: str, body: str) -> None:
    """Cache a transcript and drop the audio blob once transcribed."""
    conn.execute(
        "UPDATE whatsapp_inbox SET body=?, media_b64='' WHERE message_id=?",
        (body, message_id),
    )
