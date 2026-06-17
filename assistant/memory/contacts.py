"""Resolve a message's sender against the contacts table and keep stats fresh."""

from __future__ import annotations

import sqlite3
import time

from assistant.models import Contact, Message, Thread
from assistant.storage import repositories as repo

# Audience segments for voice profiling (P5a).
SEGMENTS = ("investor", "customer", "team", "external")

# GAP 1 — importance is calibrated by relationship_type. Each type sets a FLOOR (a
# minimum importance the contact can never drop below), then frequency + recency add on
# top. Cold/recruiter/unknown get no floor — they earn importance purely from activity.
RELATIONSHIP_IMPORTANCE_FLOOR = {
    "partner": 80,
    "family": 80,
    "investor": 65,
    "mentor": 65,
    "collaborator": 50,
    "customer": 50,
    "recruiter": 0,
    "cold": 0,
    "unknown": 0,
}
_FREQ_CAP = 30      # max points from message frequency (last 30 days)
_RECENCY_MAX = 10   # max points from recency (most recent → full, decaying to 0)
_RECENCY_WINDOW_DAYS = 30  # recency decays to 0 over this many days


def compute_importance(
    relationship_type: str, *, messages_last_30d: int, days_since_last_message: float,
) -> int:
    """Weighted importance (0..100) from the relationship_type floor plus activity.

    floor(relationship_type) + min(messages_last_30d, 30) [frequency]
                             + recency points (up to 10, decaying over 30 days)

    Cold/recruiter/unknown have a 0 floor, so they rise only with real activity. The
    result is clamped to 0..100. Pure function — unit-tested."""
    rt = (relationship_type or "unknown").strip().lower()
    floor = RELATIONSHIP_IMPORTANCE_FLOOR.get(rt, 0)
    freq = max(0, min(int(messages_last_30d or 0), _FREQ_CAP))
    try:
        days = max(0.0, float(days_since_last_message))
    except (TypeError, ValueError):
        days = float(_RECENCY_WINDOW_DAYS)
    recency = int(round(_RECENCY_MAX * max(0.0, 1.0 - days / _RECENCY_WINDOW_DAYS)))
    return max(0, min(100, floor + freq + recency))


def _activity_stats(conn: sqlite3.Connection, email: str, *, now: int) -> tuple[int, float]:
    """(messages in last 30d, days since last message) for an identifier from decision_log.
    Best-effort → (0, full-window) when there's no history."""
    email = (email or "").lower()
    if not email:
        return 0, float(_RECENCY_WINDOW_DAYS)
    try:
        since = now - 30 * 86400
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM decision_log WHERE lower(sender_email)=? AND ts >= ?",
            (email, since),
        ).fetchone()
        last = conn.execute(
            "SELECT MAX(ts) AS t FROM decision_log WHERE lower(sender_email)=?", (email,)
        ).fetchone()
        count = int(n["n"]) if n else 0
        last_ts = int(last["t"]) if last and last["t"] else 0
        days = (now - last_ts) / 86400.0 if last_ts else float(_RECENCY_WINDOW_DAYS)
        return count, days
    except sqlite3.Error:
        return 0, float(_RECENCY_WINDOW_DAYS)


def recompute_importance(conn: sqlite3.Connection, email: str, *, now: int = 0) -> int:
    """Recompute and persist a contact's importance from its person's relationship_type
    and recent activity. Returns the new importance. Best-effort: never raises."""
    try:
        now = now or int(time.time())
        rel_type = repo.relationship_type_for_identifier(conn, (email or "").lower())
        freq, days = _activity_stats(conn, email, now=now)
        score = compute_importance(
            rel_type, messages_last_30d=freq, days_since_last_message=days)
        c = repo.get_or_default_contact(conn, (email or "").lower())
        c.importance = score
        repo.upsert_contact(conn, c)
        return score
    except Exception:  # noqa: BLE001 - importance is additive
        return 0


def is_recognized(contact) -> bool:
    """True if this is a KNOWN/saved contact (someone in the owner's world), as opposed to
    an unsaved/unknown sender. 'Known' means we have a real signal of relationship —
    a trustworthy name provenance (phone book / WA-verified / owner saved in-app), a
    categorized relationship, a flag, an importance score, notes, or that the owner has
    replied to them before. A bare From-name does NOT count (it's spoofable / every
    email has one)."""
    # contact.is_saved already covers name_source∈(saved,business,manual), phone_contact,
    # flags and notes — the trustworthy-provenance half of recognition.
    if getattr(contact, "is_saved", False):
        return True
    rel = (contact.relationship or "").strip()
    # 'wa_contact' means only "someone who messaged on WhatsApp" — NOT a saved/known person.
    # Counting it as recognized is the bug that disagrees with read_queries.is_saved.
    rel_signal = bool(rel) and rel != "wa_contact"
    return bool(
        rel_signal
        or contact.flags
        or (contact.importance or 0) > 0
        or (contact.reply_rate or 0) > 0
        or (contact.notes or "").strip()
    )


def recognition_note(contact) -> str:
    """A short 'why we know them' descriptor for a known contact (for the card)."""
    rel = (contact.relationship or "").strip()
    if getattr(contact, "name_source", "") in ("saved", "business", "manual") and (
        not rel or rel in ("phone_contact", "wa_contact")
    ):
        return "saved contact"
    if rel:
        return rel
    if contact.flags:
        return ", ".join(sorted(f for f in contact.flags if f))
    if (contact.reply_rate or 0) > 0:
        return f"you reply ~{round(contact.reply_rate * 100)}%"
    if (contact.importance or 0) > 0:
        return f"importance {contact.importance}"
    return "known contact"


def detect_segment(conn: sqlite3.Connection, email: str) -> str:
    """Classify a contact into one of SEGMENTS for voice profiling.

    Order: explicit flag → VC-firm domain → relationship hint → 'external'. Pure
    lookup (no LLM); safe to call often."""
    email = (email or "").lower()
    if not email:
        return "external"
    contact = repo.get_contact(conn, email)
    flags = contact.flags if contact else set()
    if "investor" in flags:
        return "investor"
    if "team" in flags:
        return "team"
    if "customer" in flags:
        return "customer"
    from assistant.brain import guardrails

    if any(d in email for d in guardrails.INVESTOR_FIRM_DOMAINS):
        return "investor"
    rel = (contact.relationship.lower() if contact else "")
    if rel in ("investor",):
        return "investor"
    if rel in ("cofounder", "co-founder", "team", "colleague", "employee", "engineer"):
        return "team"
    if rel in ("customer", "client", "user", "buyer"):
        return "customer"
    return "external"


def resolve_sender(conn: sqlite3.Connection, message: Message) -> Contact:
    """Resolve the inbound sender into a Contact (stored profile or thin default)."""
    return repo.get_or_default_contact(conn, message.sender_email, message.sender_name)


def observe_thread(conn: sqlite3.Connection, thread: Thread, my_address: str) -> None:
    """Update lightweight engagement stats from a thread (received vs replied-to).

    This is how VIPs get inferred over time: people you reply to fastest/most rise
    in reply_rate. Importance itself is only ever *raised* by you or by a confirmed
    inferred rule — these stats are the evidence, not an automatic promotion.
    """
    my = (my_address or "").lower()
    for m in thread.messages:
        if m.from_me:
            for r in m.recipients:
                repo.bump_contact_stats(conn, r, sent_to=1)
        else:
            if m.sender_email and m.sender_email.lower() != my:
                repo.bump_contact_stats(conn, m.sender_email, received=1, name=m.sender_name)
