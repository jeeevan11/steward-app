"""Typed accessors over the SQLite tables.

Grouped by domain: kv state, contacts, rules, pending actions, audit log, voice
samples, learning events. All functions take an open connection so they compose
in a single transaction when needed. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from typing import Any, Optional

from assistant.models import Contact

# ─────────────────────────────────────────────────────────────────────────────
# kv app state
# ─────────────────────────────────────────────────────────────────────────────
def kv_get(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def kv_get_bool(conn: sqlite3.Connection, key: str, default: bool = False) -> bool:
    v = kv_get(conn, key)
    return default if v is None else v == "1"


def kv_set_bool(conn: sqlite3.Connection, key: str, value: bool) -> None:
    kv_set(conn, key, "1" if value else "0")


# ── Owner self-context ("About you") ─────────────────────────────────────────
# A free-text self-description the owner writes in Settings. It is rendered into the
# TRUSTED system prefix of the triage + drafting prompts (never into the untrusted
# message body), so the agent judges priority through the lens of who the owner
# actually is and what matters to them right now. Single live-editable KV value, no
# restart. It can RAISE relevance freely but can never lower a deterministic guardrail
# floor — the cardinal "never auto-handle what needs you" rule holds regardless of text.
OWNER_ABOUT_KEY = "owner_about"
OWNER_ABOUT_MAX = 4000  # keep the prompt lean; long enough for a rich self-description
OWNER_ABOUT_DEFAULT = (
    "I'm a professional managing my own inbox. Until I personalize this in Settings, "
    "use general judgment: real messages from real people that ask a question or need a "
    "decision matter to me; bulk promotions, newsletters and automated notifications are "
    "low priority. When unsure, surface it rather than hide it. I prefer clear, concise, "
    "direct communication."
)


def get_owner_about(conn: sqlite3.Connection) -> str:
    """The owner's self-description (Settings → About you), or a neutral default."""
    v = (kv_get(conn, OWNER_ABOUT_KEY) or "").strip()
    return v or OWNER_ABOUT_DEFAULT


def set_owner_about(conn: sqlite3.Connection, text: str) -> str:
    """Save the owner's self-description (trusted context). Trimmed + length-capped.
    Empty clears it (reverts to the default). Returns the stored value."""
    t = (text or "").strip()[:OWNER_ABOUT_MAX]
    kv_set(conn, OWNER_ABOUT_KEY, t)
    return t


# Convenience for the well-known keys.
def get_last_history_id(conn: sqlite3.Connection) -> Optional[str]:
    return kv_get(conn, "gmail_last_history_id")


def set_last_history_id(conn: sqlite3.Connection, history_id: str) -> None:
    kv_set(conn, "gmail_last_history_id", str(history_id))


def is_paused(conn: sqlite3.Connection) -> bool:
    return kv_get_bool(conn, "paused", False)


def set_paused(conn: sqlite3.Connection, paused: bool) -> None:
    kv_set_bool(conn, "paused", paused)


# ─────────────────────────────────────────────────────────────────────────────
# Contacts
# ─────────────────────────────────────────────────────────────────────────────
def _flags_to_str(flags: set[str]) -> str:
    return ",".join(sorted(f for f in flags if f))


def _flags_from_str(s: str) -> set[str]:
    return {f.strip() for f in (s or "").split(",") if f.strip()}


def contact_from_row(row: sqlite3.Row) -> Contact:
    return Contact(
        email=row["email"],
        name=row["name"] or "",
        relationship=row["relationship"] or "",
        importance=row["importance"] or 0,
        flags=_flags_from_str(row["flags"] or ""),
        reply_rate=row["reply_rate"] or 0.0,
        avg_response_seconds=row["avg_response_seconds"],
        msg_count=row["msg_count"] or 0,
        notes=row["notes"] or "",
        name_source=(row["name_source"] if "name_source" in row.keys() else "") or "",
    )


def get_contact(conn: sqlite3.Connection, email: str) -> Optional[Contact]:
    if not email:
        return None
    row = conn.execute("SELECT * FROM contacts WHERE email=?", (email.lower(),)).fetchone()
    return contact_from_row(row) if row else None


def get_or_default_contact(conn: sqlite3.Connection, email: str, name: str = "") -> Contact:
    """Always returns a Contact: the stored profile, or a thin default for unknowns."""
    c = get_contact(conn, email)
    if c:
        if name and not c.name:
            c.name = name
        return c
    return Contact(email=(email or "").lower(), name=name)


def upsert_contact(conn: sqlite3.Connection, contact: Contact) -> None:
    # name_source is provenance-only: never DOWNGRADE a trustworthy source to a weaker one on
    # a routine re-upsert (e.g. a later push-name message must not clobber a 'manual'/'saved'
    # mark). COALESCE keeps the existing value unless the caller passes a non-empty one, and the
    # CASE refuses to overwrite a trustworthy source with 'push'/'unknown'.
    ns = (contact.name_source or "").strip()
    conn.execute(
        "INSERT INTO contacts (email, name, relationship, importance, flags, "
        " reply_rate, avg_response_seconds, msg_count, notes, name_source, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,strftime('%s','now')) "
        "ON CONFLICT(email) DO UPDATE SET "
        " name=excluded.name, relationship=excluded.relationship, "
        " importance=excluded.importance, flags=excluded.flags, "
        " reply_rate=excluded.reply_rate, avg_response_seconds=excluded.avg_response_seconds, "
        " msg_count=excluded.msg_count, notes=excluded.notes, "
        " name_source=CASE "
        "   WHEN excluded.name_source IN ('saved','business','manual') THEN excluded.name_source "
        "   WHEN contacts.name_source IN ('saved','business','manual') THEN contacts.name_source "
        "   WHEN excluded.name_source != '' THEN excluded.name_source "
        "   ELSE contacts.name_source END, "
        " updated_at=strftime('%s','now')",
        (
            contact.email.lower(),
            contact.name,
            contact.relationship,
            contact.importance,
            _flags_to_str(contact.flags),
            contact.reply_rate,
            contact.avg_response_seconds,
            contact.msg_count,
            contact.notes,
            ns or "unknown",
        ),
    )


def add_contact_flag(conn: sqlite3.Connection, email: str, flag: str) -> None:
    c = get_or_default_contact(conn, email)
    c.flags.add(flag)
    upsert_contact(conn, c)


_SAVE_IMPORTANCE_FLOOR = 20   # a saved contact is never below this (above the >10 "saved" gate)


def save_contact(
    conn: sqlite3.Connection, identifier: str, name: str, phone: str = "", email: str = "",
) -> dict[str, Any]:
    """Owner-asserted "Save this contact": the trustworthy source of truth for recognition.

    Marks `identifier` (a WhatsApp @lid/jid or email) as a SAVED contact — name +
    relationship='phone_contact' + importance floor + name_source='manual' — and flips the
    cross-channel PERSON to is_saved_contact=1. A phone number and/or an email are bridged to
    the SAME person via person_links ONLY (no duplicate visible contacts row — recognition of
    a future inbound by that number/email flows through the person). The owner-asserted name is
    propagated to every existing identifier of the person, so one edit names them all. Writes
    recognition state ONLY — never a message (NO_AUTO_SEND is untouched).
    Returns {ok, person_id, importance, phone_jid?, email?}.
    """
    import uuid as _uuid

    ident = (identifier or "").strip().lower()
    nm = (name or "").strip()
    if not ident or not nm:
        return {"ok": False, "error": "identifier and name are required"}

    def _save_one(key: str) -> None:
        c = get_or_default_contact(conn, key, name=nm)
        c.name = nm
        c.relationship = "phone_contact"
        c.importance = max(int(c.importance or 0), _SAVE_IMPORTANCE_FLOOR)
        c.name_source = "manual"
        upsert_contact(conn, c)

    _save_one(ident)

    # Resolve (or create) the cross-channel person and mark it saved.
    pid = person_link_get(conn, ident)
    if not pid:
        pid = _uuid.uuid4().hex
        is_jid = "@" in ident and not ("." in ident.split("@", 1)[1])  # jid-ish vs email
        person_add(conn, person_id=pid, display_name=nm,
                   emails=([] if is_jid else [ident]), phone_jids=([ident] if is_jid else []))
        person_link_set(conn, ident, pid, confidence=1.0, source="manual")
    else:
        person_update(conn, pid, display_name=nm)
    set_person_saved(conn, pid, True)

    def _bridge(key: str, *, as_jid: bool) -> None:
        """Link an extra identifier (phone JID / email) to this SAME person via person_links —
        WITHOUT creating a separate visible contacts row. Skips it if it already belongs to a
        different person (no silent cross-person steal)."""
        key = (key or "").strip().lower()
        if not key:
            return
        existing = person_link_get(conn, key)
        if existing and existing != pid:
            return
        if not existing:
            person_link_set(conn, key, pid, confidence=1.0, source="manual")
        p = person_get(conn, pid)
        if p is not None:
            field = "phone_jids" if as_jid else "emails"
            vals = json.loads(p[field] or "[]")
            if key not in [v.lower() for v in vals]:
                vals.append(key)
                person_update(conn, pid, **{field: vals})

    out: dict[str, Any] = {"ok": True, "person_id": pid,
                           "importance": _SAVE_IMPORTANCE_FLOOR}
    # Optional phone number → @lid↔number bridge (person link only; no duplicate contacts row).
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits:
        phone_jid = f"{digits}@s.whatsapp.net"
        _bridge(phone_jid, as_jid=True)
        out["phone_jid"] = phone_jid
    # Optional owner-asserted email → fold the same person's email identity in too.
    em = (email or "").strip().lower()
    if em and "@" in em:
        _bridge(em, as_jid=False)
        out["email"] = em

    # Continuous sync: name every EXISTING contacts row that belongs to this person (so an edit
    # propagates to all their identifiers). Only updates rows that already exist; never creates.
    try:
        for r in conn.execute("SELECT identifier FROM person_links WHERE person_id=?", (pid,)):
            sib = (r["identifier"] or "").strip().lower()
            if not sib or sib == ident:
                continue
            if conn.execute("SELECT 1 FROM contacts WHERE email=?", (sib,)).fetchone():
                _save_one(sib)
    except sqlite3.Error:
        pass

    try:
        record_event(conn, type="contact_saved_manual", contact_email=ident,
                     detail={"name": nm, "person_id": pid, "phone_jid": out.get("phone_jid"),
                             "email": out.get("email")})
    except Exception:  # noqa: BLE001
        pass
    return out


def set_person_saved(conn: sqlite3.Connection, person_id: str, saved: bool = True) -> None:
    """Mark/unmark a person as a SAVED contact (orthogonal to relationship_type). Defensive
    against a pre-migration DB without the column."""
    if not person_id:
        return
    try:
        conn.execute("UPDATE persons SET is_saved_contact=? WHERE id=?",
                     (1 if saved else 0, person_id))
    except sqlite3.OperationalError:
        pass


def person_is_saved(conn: sqlite3.Connection, person_id: str) -> bool:
    """True if this person has been saved by the owner. Defensive (returns False pre-migration)."""
    if not person_id:
        return False
    try:
        row = conn.execute(
            "SELECT is_saved_contact FROM persons WHERE id=?", (person_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row["is_saved_contact"])


def save_contacts_bulk(
    conn: sqlite3.Connection, entries: list[dict[str, Any]],
) -> dict[str, int]:
    """Bulk-import the owner's address book into the RECOGNITION INDEX. Each entry is
    {"name": str, "phones": [str...], "emails": [str...]}. Per entry we seed ONE saved
    person with every phone (as "<digits>@s.whatsapp.net") and every email as person_links —
    so the saved name + recognition fire the instant any of those identifiers messages
    (resolution funnels through person_link_get). It deliberately creates NO contacts rows,
    so the People list isn't flooded with the whole phone book — a person surfaces there only
    once they actually message. Idempotent; never STEALS an identifier already linked to a
    different person, and never renames an already owner-saved person (NO_AUTO_MERGE). Writes
    recognition state only — never a message. Returns {imported, links, skipped}."""
    import uuid as _uuid

    imported = links = skipped = 0
    for e in (entries or []):
        name = (str(e.get("name") or "")).strip()
        phones: list[str] = []
        for p in (e.get("phones") or []):
            digits = "".join(c for c in str(p) if c.isdigit())
            if len(digits) >= 7:                      # a plausible phone number
                phones.append(f"{digits}@s.whatsapp.net")
        emails = [s for s in ((str(em).strip().lower()) for em in (e.get("emails") or []))
                  if "@" in s]
        idents = list(dict.fromkeys(phones + emails))  # de-dup, stable order
        if not name or not idents:
            skipped += 1
            continue

        # Reuse an already-linked person if any identifier resolves to one; else mint a new one.
        pid = next((person_link_get(conn, i) for i in idents if person_link_get(conn, i)), None)
        if not pid:
            pid = _uuid.uuid4().hex
            person_add(conn, person_id=pid, display_name=name,
                       emails=emails, phone_jids=phones)
        else:
            # Reusing an existing person: adopt the book name ONLY if they aren't already an
            # owner-saved, named person — so the import never silently renames a contact the
            # owner deliberately saved/merged (e.g. a manual "Simba").
            prow = person_get(conn, pid)
            cur = ((prow["display_name"] if prow else "") or "").strip()
            if not (person_is_saved(conn, pid) and cur):
                person_update(conn, pid, display_name=name)
            p = person_get(conn, pid)
            if p is not None:
                ej = json.loads(p["phone_jids"] or "[]")
                ee = json.loads(p["emails"] or "[]")
                for ph in phones:
                    if ph not in [x.lower() for x in ej]:
                        ej.append(ph)
                for em in emails:
                    if em not in [x.lower() for x in ee]:
                        ee.append(em)
                person_update(conn, pid, phone_jids=ej, emails=ee)
        set_person_saved(conn, pid, True)
        for ident in idents:
            if not person_link_get(conn, ident):       # never steal a foreign link
                person_link_set(conn, ident, pid, confidence=1.0, source="address_book")
                links += 1
        imported += 1
    return {"imported": imported, "links": links, "skipped": skipped}


def bump_contact_stats(
    conn: sqlite3.Connection,
    email: str,
    *,
    received: int = 0,
    sent_to: int = 0,
    name: str = "",
) -> None:
    """Increment received/sent counters and recompute reply_rate. Used by onboarding
    and at runtime to keep importance signals fresh."""
    email = (email or "").lower()
    if not email:
        return
    conn.execute(
        "INSERT INTO contacts (email, name, received_count, sent_to_count, msg_count) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(email) DO UPDATE SET "
        " received_count = received_count + ?, "
        " sent_to_count  = sent_to_count + ?, "
        " msg_count      = msg_count + ?, "
        " name = CASE WHEN contacts.name='' THEN ? ELSE contacts.name END, "
        " updated_at = strftime('%s','now')",
        (email, name, received, sent_to, received + sent_to,
         received, sent_to, received + sent_to, name),
    )
    conn.execute(
        "UPDATE contacts SET reply_rate = "
        " CASE WHEN received_count > 0 "
        "      THEN MIN(1.0, CAST(sent_to_count AS REAL) / received_count) ELSE 0 END "
        "WHERE email=?",
        (email,),
    )


def top_contacts_by_reply(conn: sqlite3.Connection, limit: int = 25) -> list[sqlite3.Row]:
    """Contacts you engage with most — the raw signal for inferring VIPs."""
    return list(
        conn.execute(
            "SELECT * FROM contacts "
            "WHERE received_count > 0 "
            "ORDER BY reply_rate DESC, sent_to_count DESC, msg_count DESC LIMIT ?",
            (limit,),
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rules
# ─────────────────────────────────────────────────────────────────────────────
def add_rule(
    conn: sqlite3.Connection,
    *,
    scope: str,
    instruction: str,
    match_key: str = "",
    action: str = "",
    status: str = "active",
    source: str = "user",
    confidence: float = 1.0,
) -> int:
    cur = conn.execute(
        "INSERT INTO rules (scope, match_key, instruction, action, status, source, confidence) "
        "VALUES (?,?,?,?,?,?,?)",
        (scope, match_key.lower(), instruction, action, status, source, confidence),
    )
    return int(cur.lastrowid)


def get_active_rules(
    conn: sqlite3.Connection, *, contact_email: str = "", category: str = ""
) -> list[sqlite3.Row]:
    """Active rules relevant to this message, contact-scoped first then category
    then global — so retrieval can prioritize the most specific."""
    rows = list(
        conn.execute(
            "SELECT * FROM rules WHERE status='active' AND ("
            " (scope='contact'  AND match_key=?) OR "
            " (scope='category' AND match_key=?) OR "
            "  scope='global') "
            "ORDER BY CASE scope WHEN 'contact' THEN 0 WHEN 'category' THEN 1 ELSE 2 END",
            ((contact_email or "").lower(), (category or "").lower()),
        )
    )
    return rows


def list_rules(conn: sqlite3.Connection, status: Optional[str] = None) -> list[sqlite3.Row]:
    if status:
        return list(conn.execute("SELECT * FROM rules WHERE status=? ORDER BY id", (status,)))
    return list(conn.execute("SELECT * FROM rules ORDER BY id"))


def set_rule_status(conn: sqlite3.Connection, rule_id: int, status: str) -> None:
    conn.execute(
        "UPDATE rules SET status=?, updated_at=strftime('%s','now') WHERE id=?",
        (status, rule_id),
    )


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    """Hard-delete a standing rule (any status). Used by the owner-facing "remove rule"
    control. Returns True if a row was removed. An inferred rule can re-propose later from
    fresh evidence — that's intended; this just clears it now."""
    cur = conn.execute("DELETE FROM rules WHERE id=?", (int(rule_id),))
    return cur.rowcount > 0


def find_inferred_rule(
    conn: sqlite3.Connection, scope: str, match_key: str, action: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM rules WHERE scope=? AND match_key=? AND action=? AND source='inferred'",
        (scope, match_key.lower(), action),
    ).fetchone()


def bump_rule_evidence(conn: sqlite3.Connection, rule_id: int) -> int:
    conn.execute(
        "UPDATE rules SET evidence_count = evidence_count + 1, updated_at=strftime('%s','now') "
        "WHERE id=?",
        (rule_id,),
    )
    row = conn.execute("SELECT evidence_count FROM rules WHERE id=?", (rule_id,)).fetchone()
    return int(row["evidence_count"]) if row else 0


# ─────────────────────────────────────────────────────────────────────────────
# Pending actions
# ─────────────────────────────────────────────────────────────────────────────
def create_pending(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    message_id: str,
    thread_id: str,
    tier: int,
    kind: str,
    summary: str = "",
    draft_text: str = "",
    telegram_chat_id: str = "",
) -> Optional[int]:
    """Create a pending action. Returns its id, or None if one with the same
    idempotency_key already exists (dedup — never queue the same thing twice)."""
    try:
        cur = conn.execute(
            "INSERT INTO pending_actions "
            "(idempotency_key, message_id, thread_id, tier, kind, summary, draft_text, "
            " telegram_chat_id, status) VALUES (?,?,?,?,?,?,?,?,'PENDING')",
            (idempotency_key, message_id, thread_id, tier, kind, summary, draft_text,
             telegram_chat_id),
        )
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def find_open_action_for_sender(
    conn: sqlite3.Connection, sender: str, *, within_seconds: int = 1200,
    thread_id: Optional[str] = None,
) -> Optional[sqlite3.Row]:
    """GAP 2 — the most recent still-PENDING action whose underlying message came from
    ``sender`` and was created within ``within_seconds``. Used to FOLD a new message from
    the same sender into one card instead of creating a duplicate.

    Sender is resolved through decision_log (which stores sender_email per message_id).
    Returns None when there's no open, in-window action for that sender.

    approval-telegram-2 (NO_WRONG_THREAD): when ``thread_id`` is given, the open action must
    be on the SAME thread to be a fold target. Without this, a second message from the same
    sender on a DIFFERENT thread folded into the first card, and execute_send then routed the
    folded reply into the ORIGINAL thread's recipients/subject — a wrong-thread misroute and
    content leak. Callers on the live path always pass thread_id; it is optional only so the
    sender-only lookup stays available for callers that genuinely want it."""
    sender = (sender or "").lower().strip()
    if not sender:
        return None
    cutoff = int(time.time()) - max(1, int(within_seconds))
    sql = (
        "SELECT pa.* FROM pending_actions pa "
        "JOIN decision_log dl ON dl.message_id = pa.message_id "
        "WHERE pa.status = 'PENDING' AND pa.created_at >= ? "
        "  AND lower(dl.sender_email) = ? "
    )
    params: list[Any] = [cutoff, sender]
    if thread_id is not None:
        sql += "  AND pa.thread_id = ? "
        params.append(thread_id)
    sql += "ORDER BY pa.created_at DESC, pa.id DESC LIMIT 1"
    try:
        return conn.execute(sql, tuple(params)).fetchone()
    except sqlite3.OperationalError:
        # decision_log may not exist yet in a bare test DB → no fold target.
        return None


def fold_message_into_action(
    conn: sqlite3.Connection, action_id: int, new_message_id: str,
    new_summary: str, new_draft: str, new_tier: Optional[int] = None,
) -> bool:
    """GAP 2 — fold a newer message from the same sender into an existing open action:
    append the message id to folded_message_ids, bump message_count, refresh the summary
    and draft, and reset created_at to now (refreshing the fold window). Returns True iff
    the row was updated (still PENDING)."""
    import json as _json
    row = conn.execute(
        "SELECT folded_message_ids, message_count, message_id FROM pending_actions "
        "WHERE id=? AND status='PENDING'", (action_id,)
    ).fetchone()
    if row is None:
        return False
    try:
        folded = _json.loads(row["folded_message_ids"] or "[]")
        if not isinstance(folded, list):
            folded = []
    except (ValueError, TypeError):
        folded = []
    # Record the ORIGINAL representative message id once, then each newly folded id.
    if row["message_id"] and row["message_id"] not in folded:
        folded.append(row["message_id"])
    if new_message_id and new_message_id not in folded:
        folded.append(new_message_id)
    count = int(row["message_count"] or 1) + 1
    # WYSIWYG_APPROVAL (autosend-invariant-2 / approval-telegram-1): folding mutates the
    # card's draft_text under a card that may already be rendered/approved. The prior
    # approval was bound to the OLD draft + target, so it MUST be invalidated here — never
    # silently swap the draft under an existing approval. approval_hash is cleared in the
    # same UPDATE; the caller (dispatcher) re-renders + re-stamps so the owner approves the
    # merged draft, not the stale one. (begin_send independently refuses on a hash mismatch,
    # so even a missed re-render fails safe rather than sending unseen text.)
    try:
        # If the card carried a real approval, mark it INVALIDATED (fail-safe sentinel) so a
        # send refuses until a fresh render+stamp; if it never had one (NULL), keep NULL so a
        # never-approved card preserves its prior (contract-free) behavior.
        # IMPORTANT-NOT-BURIED: only let the summary be overwritten when the folded message is
        # at LEAST as important as the card (new_tier >= current tier). A trivial follow-up
        # ("ok lol") folding into an important card must not replace the surfaced one-liner with
        # its own — the owner reads the summary to decide, and the latest-but-trivial line was
        # burying the important reason. tier itself is already raise-only via MAX().
        # Preserve the summary ONLY when the folded message is a genuinely LOWER positive tier
        # (a trivial "ok lol" folding into an important card). An unspecified tier (0) still
        # updates the summary, as before — so callers that don't pass a tier are unaffected.
        cur = conn.execute(
            "UPDATE pending_actions SET folded_message_ids=?, message_count=?, "
            " summary=CASE WHEN ?>0 AND ?<tier THEN summary ELSE ? END, draft_text=?, "
            " tier=MAX(tier, ?), "
            " approval_hash=CASE WHEN approval_hash IS NOT NULL THEN ? ELSE NULL END, "
            " send_target=NULL, created_at=strftime('%s','now') "
            "WHERE id=? AND status='PENDING'",
            (_json.dumps(folded), count, int(new_tier or 0), int(new_tier or 0), new_summary,
             new_draft, int(new_tier or 0), _APPROVAL_INVALIDATED, action_id),
        )
    except sqlite3.OperationalError:
        # Legacy DB without approval columns — preserve the original fold UPDATE.
        cur = conn.execute(
            "UPDATE pending_actions SET folded_message_ids=?, message_count=?, "
            " summary=CASE WHEN ?>0 AND ?<tier THEN summary ELSE ? END, draft_text=?, "
            " tier=MAX(tier, ?), created_at=strftime('%s','now') "
            "WHERE id=? AND status='PENDING'",
            (_json.dumps(folded), count, int(new_tier or 0), int(new_tier or 0), new_summary,
             new_draft, int(new_tier or 0), action_id),
        )
    if cur.rowcount == 1:
        # scaling-time-2: keep the indexed fold-membership table in sync so the queue's
        # folded-child lookup is a point lookup, not a full-table LIKE scan.
        _record_folded_children(conn, action_id, folded)
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Conversation model: one living card per thread, resolvable from any surface.
# ─────────────────────────────────────────────────────────────────────────────
_FOLD_MAX_AGE_SECONDS = 14 * 86400  # don't fold into a card older than the context window


def find_open_action_for_thread(
    conn: sqlite3.Connection, thread_id: str, *, max_age_seconds: int = _FOLD_MAX_AGE_SECONDS,
) -> Optional[sqlite3.Row]:
    """The most recent still-PENDING card on this THREAD (the conversation), regardless of how
    long ago it was created (up to max_age_seconds). Folding by conversation — not a short time
    window — means re-texts hours apart on the same UNANSWERED chat collapse into ONE living
    card instead of a pile of stale per-burst siblings."""
    tid = (thread_id or "").strip()
    if not tid:
        return None
    cutoff = int(time.time()) - max(1, int(max_age_seconds))
    return conn.execute(
        "SELECT * FROM pending_actions WHERE status='PENDING' AND thread_id=? AND created_at >= ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (tid, cutoff),
    ).fetchone()


def resolve_thread_siblings(
    conn: sqlite3.Connection, thread_id: str, keep_id: int, *, status: str = "SUPERSEDED",
) -> list[int]:
    """Collapse a conversation to ONE living card: mark every OTHER open PENDING card on this
    thread terminal (SUPERSEDED), so handling the living card never leaves stranded siblings
    (e.g. an old 'going to sleep' card sitting behind a newer one). Terminal + non-sendable, so
    a sibling can never be approved into a second send. Returns the ids resolved.

    CARDINAL RULE (needs-attention is never auto-handled): a tier-3 ASK (kind='ask') is NEVER
    superseded as a sibling. Folding a newer (often trivial) message into one card, or sending a
    reply on the thread, must not silently discard a SEPARATE open question the owner still has
    to decide — the live ask #158 ('going to sleep' on the same thread as Maya #164) was lost
    exactly this way. This guard covers BOTH callers (the dispatcher fold path and the post-send
    _resolve_thread_after_send sweep). Mirrors the same exclusion in resolve_handled_elsewhere."""
    tid = (thread_id or "").strip()
    if not tid:
        return []
    rows = conn.execute(
        "SELECT id FROM pending_actions WHERE status='PENDING' AND thread_id=? AND id!=? "
        "AND kind!='ask'",
        (tid, keep_id),
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    for i in ids:
        conn.execute(
            "UPDATE pending_actions SET status=?, decided_at=strftime('%s','now') "
            "WHERE id=? AND status='PENDING'",
            (status, i),
        )
    return ids


def resolve_handled_elsewhere(conn: sqlite3.Connection, thread_id: str) -> list[sqlite3.Row]:
    """Cross-surface resolution: the owner replied to this thread on ANOTHER device (phone /
    PC WhatsApp / etc.), so a DRAFTED-REPLY card for it is already obsolete. Move it to the
    terminal HANDLED_ELSEWHERE state — a CLOSE, never a send — so the owner can't later approve
    a stale card and send a SECOND reply. Only PENDING rows are touched, so a card Steward
    itself just sent (SENDING/SENT) is never affected. Returns the rows it closed.

    CARDINAL RULE (needs-attention is never auto-handled): a tier-3 ASK (kind='ask' — an
    explicit decision Steward could NOT make for the owner) is NEVER closed this way. The owner
    sending *some* message to a chat does not mean he made the specific decision the ask is
    waiting on — in the live 'Sam $250k investor' incident, a casual reply to the same chat
    silently dismissed a high-stakes investor question. Asks stay PENDING until the owner acts
    on them explicitly; only drafted replies / FYIs (which his own reply genuinely obsoletes)
    are auto-closed. ``open_asks_for_thread`` lets a caller tell the owner an ask was kept."""
    tid = (thread_id or "").strip()
    if not tid:
        return []
    rows = conn.execute(
        "SELECT id, summary, tier FROM pending_actions "
        "WHERE status='PENDING' AND thread_id=? AND kind!='ask'",
        (tid,),
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE pending_actions SET status='HANDLED_ELSEWHERE', "
            "error='owner replied on another device', decided_at=strftime('%s','now') "
            "WHERE id=? AND status='PENDING'",
            (int(r["id"]),),
        )
    return list(rows)


def open_asks_for_thread(conn: sqlite3.Connection, thread_id: str) -> list[sqlite3.Row]:
    """Still-open tier-3 ASK cards on this thread — the decisions cross-surface deliberately
    does NOT auto-close. Lets a caller tell the owner 'I kept the question that needs you open'."""
    tid = (thread_id or "").strip()
    if not tid:
        return []
    return list(conn.execute(
        "SELECT id, summary, tier FROM pending_actions "
        "WHERE status='PENDING' AND thread_id=? AND kind='ask' ORDER BY id DESC",
        (tid,),
    ))


def get_pending(conn: sqlite3.Connection, action_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM pending_actions WHERE id=?", (action_id,)).fetchone()


def get_pending_by_key(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pending_actions WHERE idempotency_key=?", (key,)
    ).fetchone()


def set_pending_telegram_message(
    conn: sqlite3.Connection, action_id: int, chat_id: str, message_id: str
) -> None:
    conn.execute(
        "UPDATE pending_actions SET telegram_chat_id=?, telegram_message_id=? WHERE id=?",
        (chat_id, message_id, action_id),
    )


def update_pending_status(
    conn: sqlite3.Connection, action_id: int, status: str, *, error: str = ""
) -> None:
    conn.execute(
        "UPDATE pending_actions SET status=?, error=?, decided_at=strftime('%s','now') "
        "WHERE id=?",
        (status, error, action_id),
    )


def set_pending_draft(conn: sqlite3.Connection, action_id: int, draft_text: str) -> bool:
    """Replace the draft and move the action to EDITED — but ONLY from a still-
    sendable pre-send state. Returns True iff the transition happened.

    Guarding this is a hard safety requirement: without it, editing an already-SENT
    action would flip it back to EDITED and let it be approved + sent a SECOND time.
    Terminal states (SENT/SENDING/SKIPPED/EXPIRED) are not editable.

    SEND_BLOCKED is editable: a human re-writing a placeholder/mismatched draft is exactly
    how a blocked card recovers. A human Edit is itself a fresh approval of the new text, so
    we re-stamp the WYSIWYG approval hash over the edited body against the bound send target
    (recipients are unchanged by an edit) — otherwise begin_send's mismatch check would
    refuse the owner's own edit.
    """
    cur = conn.execute(
        "UPDATE pending_actions SET draft_text=?, status='EDITED' "
        "WHERE id=? AND status IN ('PENDING','APPROVED','EDITED','SEND_FAILED','SEND_BLOCKED')",
        (draft_text, action_id),
    )
    if cur.rowcount != 1:
        return False
    # Re-bind the approval to the edited body + the already-bound target.
    row = get_pending(conn, action_id)
    target_thread = row["thread_id"] if row is not None else ""
    recipients: Any = None
    try:
        if row is not None and "send_target" in row.keys() and row["send_target"]:
            tgt = json.loads(row["send_target"])
            if isinstance(tgt, dict):
                target_thread = tgt.get("thread_id", target_thread)
                recipients = tgt.get("recipients")
    except (ValueError, TypeError):
        recipients = None
    stamp_approval(conn, action_id, thread_id=target_thread or "", recipients=recipients or [])
    return True


def mark_approved(conn: sqlite3.Connection, action_id: int, via: str = "web") -> bool:
    """Move a pending action to APPROVED — ONLY from a non-terminal, pre-send state.

    Returns True iff this call won the transition. Together with begin_send this
    forms a two-step guard: a stale/re-delivered Approve tap on an action that is
    already SENDING/SENT/SKIPPED/EXPIRED cannot revive it, so it can never be sent
    twice. (EDITED is already sendable, so we leave it as-is and still return True.)
    """
    cur = conn.execute(
        "UPDATE pending_actions SET status='APPROVED', response_via=?, decided_at=strftime('%s','now') "
        "WHERE id=? AND status IN ('PENDING','SEND_FAILED')",
        (via, action_id),
    )
    if cur.rowcount == 1:
        return True
    # Already APPROVED or EDITED is fine (both are sendable); anything else is not.
    row = get_pending(conn, action_id)
    return bool(row and row["status"] in ("APPROVED", "EDITED"))


def mark_skipped(conn: sqlite3.Connection, action_id: int) -> bool:
    """Skip an action — only if it hasn't already been sent/sent-attempted. Returns
    True iff it transitioned (so we don't mislabel a SENT reply as skipped).

    SEND_BLOCKED is skippable: a card the integrity guard refused (placeholder / WYSIWYG
    mismatch) is exactly the kind of thing the owner may choose to dismiss rather than
    re-draft. It is a pre-send state (nothing was sent), so skipping it is safe."""
    cur = conn.execute(
        "UPDATE pending_actions SET status='SKIPPED', decided_at=strftime('%s','now') "
        "WHERE id=? AND status IN ('PENDING','APPROVED','EDITED','SEND_FAILED','SEND_BLOCKED')",
        (action_id,),
    )
    return cur.rowcount == 1


def undelivered_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Items that were queued for you but whose Telegram card never got delivered
    (no telegram_message_id). Used by the poller to re-deliver — so a transient
    Telegram outage can never silently swallow something that needs your eyes."""
    return list(
        conn.execute(
            "SELECT * FROM pending_actions "
            "WHERE status='PENDING' AND (telegram_message_id IS NULL OR telegram_message_id='') "
            "ORDER BY created_at ASC"
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Approval integrity (WYSIWYG_APPROVAL / NO_WRONG_THREAD / NO_WRONG_RECIPIENT)
#
# Root cause (autosend-invariant-2, approval-telegram-1/2): approval was bound to
# action_id, not to the EXACT draft + send target the owner saw. Fold-batching mutates
# draft_text / summary on an already-rendered card without re-rendering it or
# re-binding its target, so the owner could approve draft A while unseen draft B (on
# the wrong thread) was sent. The fix binds an approval_hash (a canonical hash of the
# displayed draft_text + recipients + thread_id) and a send_target to the card at
# render time; begin_send re-derives the hash from the LIVE row and REFUSES to send
# (fail safe) on any mismatch, and a fold INVALIDATES the prior approval (clears the
# hash + bumps the row out of any sendable state) so a fresh approval is required.
# ─────────────────────────────────────────────────────────────────────────────

# Status set for cards whose approval was invalidated (fold mutated the draft/target,
# or the WYSIWYG/target/placeholder guard refused the send). NOT in any sendable set,
# so it can never be sent without a fresh human Edit/Approve. Recoverable via Edit
# (set_pending_draft accepts it) which re-stamps the approval at the next render.
SEND_BLOCKED = "SEND_BLOCKED"

# Sentinel written into approval_hash when a fold INVALIDATES a card that previously carried
# an approval. Distinct from NULL ("no WYSIWYG contract was ever stamped" — legacy/un-rendered
# rows, which fall open to preserve prior behavior). A real sha256 hash can never equal this
# string, so approval_matches() returns False for it → begin_send refuses until a fresh render
# + stamp replaces it. This makes a fold fail SAFE even if the re-render/re-stamp never runs.
_APPROVAL_INVALIDATED = "INVALIDATED"


def canonical_approval_hash(draft_text: str, thread_id: str, recipients) -> str:
    """Stable hash of EXACTLY what the owner is approving: the draft body + the thread it
    routes to + the recipient set. Recipients are lower-cased + sorted so a reorder is not
    treated as a change; the body and thread are compared verbatim. Any difference here
    means the owner did not see what would be sent → begin_send refuses (fail safe).

    Pure/deterministic so the same inputs always yield the same digest across processes."""
    if recipients is None:
        recips: list[str] = []
    elif isinstance(recipients, (list, tuple, set)):
        recips = [str(r or "").strip().lower() for r in recipients if str(r or "").strip()]
    else:
        recips = [str(recipients).strip().lower()] if str(recipients).strip() else []
    recips = sorted(set(recips))
    payload = json.dumps(
        {"d": draft_text or "", "t": (thread_id or ""), "r": recips},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_approval_hash(row: sqlite3.Row) -> str:
    """Re-derive the approval hash from the LIVE row + its bound send_target. Uses the
    target recipients/thread bound at render time when present, so a fold that changed the
    routing (without re-binding) is detected as a mismatch rather than silently honored."""
    target_thread = row["thread_id"]
    recipients: Any = None
    try:
        if "send_target" in row.keys() and row["send_target"]:
            tgt = json.loads(row["send_target"])
            if isinstance(tgt, dict):
                target_thread = tgt.get("thread_id", target_thread)
                recipients = tgt.get("recipients")
    except (ValueError, TypeError):
        recipients = None
    return canonical_approval_hash(row["draft_text"] or "", target_thread or "", recipients)


def stamp_approval(
    conn: sqlite3.Connection, action_id: int, *,
    thread_id: str = "", recipients=None,
) -> str:
    """Bind the approval to EXACTLY the current draft + send target of the card. Called when
    the card is (re-)rendered to the owner and again right before a human Approve commits, so
    the hash always reflects what the owner is looking at. Returns the stamped hash.

    The send_target (thread_id + recipients) is persisted alongside so begin_send can prove
    the routing has not changed under the approval (NO_WRONG_THREAD / NO_WRONG_RECIPIENT)."""
    row = get_pending(conn, action_id)
    if row is None:
        return ""
    t = thread_id or (row["thread_id"] or "")
    target = json.dumps(
        {"thread_id": t,
         "recipients": [str(r).strip().lower() for r in (recipients or []) if str(r).strip()]},
        ensure_ascii=False, sort_keys=True,
    )
    h = canonical_approval_hash(row["draft_text"] or "", t, recipients)
    try:
        conn.execute(
            "UPDATE pending_actions SET approval_hash=?, send_target=? WHERE id=?",
            (h, target, action_id),
        )
    except sqlite3.OperationalError:
        # Legacy DB without the approval columns — degrade safely (no hash bound; the
        # begin_send check treats a NULL stored hash as "no WYSIWYG contract" and proceeds,
        # preserving prior behavior on un-migrated rows).
        pass
    return h


def approval_matches(conn: sqlite3.Connection, action_id: int) -> bool:
    """True iff the card's stored approval_hash equals the hash re-derived from the LIVE row.

    A NULL/absent stored hash means no WYSIWYG contract was ever stamped (legacy/un-migrated
    rows, or a path that never rendered a card) → returns True so begin_send keeps its prior
    behavior there. A stored-but-different hash means the draft/target changed under the
    approval (a fold, or any out-of-band mutation) → False, and begin_send must refuse."""
    row = get_pending(conn, action_id)
    if row is None:
        return False
    try:
        stored = row["approval_hash"] if "approval_hash" in row.keys() else None
    except (IndexError, KeyError):
        stored = None
    if not stored:
        return True
    if stored == _APPROVAL_INVALIDATED:
        # A fold invalidated a previously-approved card and no fresh stamp replaced it →
        # fail safe (refuse), never fall open to the unseen merged draft.
        return False
    return stored == _row_approval_hash(row)


def invalidate_approval(conn: sqlite3.Connection, action_id: int) -> None:
    """Drop any bound approval for this card. Called when a fold mutates the draft/target so
    a stale approval can never carry over to the new content — a fresh render + approval is
    required. Safe no-op on a legacy DB without the column."""
    try:
        conn.execute(
            "UPDATE pending_actions SET approval_hash=NULL, send_target=NULL WHERE id=?",
            (action_id,),
        )
    except sqlite3.OperationalError:
        pass


def mark_send_blocked(conn: sqlite3.Connection, action_id: int, reason: str) -> bool:
    """Refuse a send for an approval-integrity reason (WYSIWYG mismatch, wrong target, or an
    unresolved placeholder) and park the card in SEND_BLOCKED — a non-sendable state that
    requires a fresh human Edit/Approve. The prior approval is invalidated so it can never be
    resurrected silently. Returns True iff this call transitioned it (only from SENDING, where
    begin_send had already claimed the row)."""
    cur = conn.execute(
        "UPDATE pending_actions SET status=?, error=?, approval_hash=NULL, "
        "decided_at=strftime('%s','now') WHERE id=? AND status='SENDING'",
        (SEND_BLOCKED, ("blocked before send: " + (reason or ""))[:1000], action_id),
    )
    return cur.rowcount == 1


def _record_folded_children(
    conn: sqlite3.Connection, parent_action_id: int, child_message_ids,
) -> None:
    """scaling-time-2: maintain the indexed folded_children table so the queue's
    folded-child lookup is an O(1) point lookup instead of a leading-wildcard LIKE full-scan
    of the never-pruned pending_actions table. Idempotent (INSERT OR IGNORE). Best-effort:
    a legacy DB without the table simply keeps the old (slower) JSON-scan fallback working."""
    try:
        for cid in child_message_ids:
            if not cid:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO folded_children (child_message_id, parent_action_id) "
                "VALUES (?, ?)",
                (cid, parent_action_id),
            )
    except sqlite3.OperationalError:
        pass


def begin_send(conn: sqlite3.Connection, action_id: int) -> bool:
    """Compare-and-set guard against double-send.

    Atomically flips APPROVED/EDITED → SENDING. Returns True iff this call won the
    transition (i.e. it is the one allowed to actually call Gmail send). A second
    tap or a restart will see status SENDING/SENT and get False.

    failure-recovery-2: stamps a dedicated send-start clock (sending_started_at) inside the
    SAME compare-and-set, so the stuck-send reaper can key staleness on when the send genuinely
    began rather than on created_at (which a fold resets and an aged-but-just-approved card
    inflates). The column is set defensively only when present (legacy DBs ignore it)."""
    try:
        cur = conn.execute(
            "UPDATE pending_actions SET status='SENDING', "
            "sending_started_at=strftime('%s','now') "
            "WHERE id=? AND status IN ('APPROVED','EDITED')",
            (action_id,),
        )
    except sqlite3.OperationalError:
        # Un-migrated DB without sending_started_at — fall back to the original CAS so the
        # double-send guard is never weakened.
        cur = conn.execute(
            "UPDATE pending_actions SET status='SENDING' "
            "WHERE id=? AND status IN ('APPROVED','EDITED')",
            (action_id,),
        )
    return cur.rowcount == 1


def mark_sent(conn: sqlite3.Connection, action_id: int, sent_gmail_id: str) -> None:
    conn.execute(
        "UPDATE pending_actions SET status='SENT', sent_gmail_id=?, "
        "decided_at=strftime('%s','now') WHERE id=?",
        (sent_gmail_id, action_id),
    )


def mark_send_failed(conn: sqlite3.Connection, action_id: int, error: str) -> None:
    """Provably-NOT-delivered failure (the error happened BEFORE the irreversible provider
    send — e.g. building the reply). Reverts to SEND_FAILED, which IS retryable, because
    we know nothing was sent. Never use this for a failure at/after the send: that is
    ambiguous and must go to mark_send_ambiguous instead (EXACTLY_ONCE_SEND)."""
    conn.execute(
        "UPDATE pending_actions SET status='SEND_FAILED', error=? WHERE id=?",
        (error[:1000], action_id),
    )


def mark_send_ambiguous(
    conn: sqlite3.Connection, action_id: int, error: str, sent_id: str = ""
) -> bool:
    """Delivery could NOT be proven to have NOT happened — the failure occurred at or
    after the irreversible provider send (a lost/timed-out ACK, or a DB error after the
    message was accepted). Move SENDING → the terminal SEND_AMBIGUOUS.

    SEND_AMBIGUOUS is in NO sendable set (mark_approved / set_pending_draft / begin_send
    all exclude it), so a generic Retry tap can NEVER auto-resend it — that is the
    EXACTLY_ONCE_SEND guarantee for the lost-ACK / DB-lock-after-delivery race
    (findings autosend-invariant-1, storage-persistence-4/5). The only way back to a
    sendable state is an explicit human decision via force_resend_after_ambiguous.
    Returns True iff this call transitioned the row (only ever from SENDING)."""
    cur = conn.execute(
        "UPDATE pending_actions SET status='SEND_AMBIGUOUS', sent_gmail_id=?, error=? "
        "WHERE id=? AND status='SENDING'",
        (sent_id or "", error[:1000], action_id),
    )
    return cur.rowcount == 1


def force_resend_after_ambiguous(conn: sqlite3.Connection, action_id: int) -> bool:
    """Explicit human resolution of a SEND_AMBIGUOUS row: 'I checked the thread, it did
    NOT arrive — resend it once.' Moves SEND_AMBIGUOUS → APPROVED so the normal guarded
    single-send path runs again. This is the ONLY exit from SEND_AMBIGUOUS into a
    sendable state and must be wired ONLY to a dedicated confirm control, never the
    generic Retry. Returns True iff transitioned."""
    cur = conn.execute(
        "UPDATE pending_actions SET status='APPROVED', error='' "
        "WHERE id=? AND status='SEND_AMBIGUOUS'",
        (action_id,),
    )
    return cur.rowcount == 1


def open_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM pending_actions WHERE status IN ('PENDING','APPROVED','EDITED') "
            "ORDER BY created_at ASC"
        )
    )


def stuck_sending(conn: sqlite3.Connection, older_than_epoch: int) -> list[sqlite3.Row]:
    """Rows wedged in SENDING since before `older_than_epoch` — a normal send completes in
    seconds, so these indicate a crash mid-send. Read-only; the reaper decides what to do.

    failure-recovery-2: staleness is keyed on the dedicated send-start clock
    (sending_started_at, stamped by begin_send), NOT created_at. Folding resets created_at
    and an owner can approve an hour-old card, so created_at said nothing about how long the
    SEND had actually been in flight — healthy in-flight sends were spuriously flagged and a
    real loss on a freshly-folded card was detected ~30 min late. We COALESCE to created_at
    only for rows that predate the column (legacy in-flight sends), so a pre-migration wedge
    is never left undetectable."""
    try:
        return list(
            conn.execute(
                "SELECT * FROM pending_actions WHERE status='SENDING' "
                "AND COALESCE(sending_started_at, created_at) < ? "
                "ORDER BY COALESCE(sending_started_at, created_at) ASC",
                (older_than_epoch,),
            )
        )
    except sqlite3.OperationalError:
        # Un-migrated DB without sending_started_at — preserve the original behavior.
        return list(
            conn.execute(
                "SELECT * FROM pending_actions WHERE status='SENDING' AND created_at < ? "
                "ORDER BY created_at ASC",
                (older_than_epoch,),
            )
        )


def mark_send_stuck(conn: sqlite3.Connection, action_id: int) -> bool:
    """Move a wedged SENDING row to the terminal SEND_STUCK state for human review. This is
    a NEW terminal state that is NEVER re-sent (it is not in any sendable set), so flagging
    a row that may have actually sent can never cause a double-send. Returns True iff this
    call transitioned it (only ever from SENDING)."""
    cur = conn.execute(
        "UPDATE pending_actions SET status='SEND_STUCK', "
        "error='send did not confirm (crash mid-send?); flagged for review', "
        "decided_at=strftime('%s','now') WHERE id=? AND status='SENDING'",
        (action_id,),
    )
    return cur.rowcount == 1


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────
def log_action(
    conn: sqlite3.Connection,
    *,
    kind: str,
    message_id: str = "",
    thread_id: str = "",
    tier: Optional[int] = None,
    summary: str = "",
    reversible: bool = False,
    undo_data: Optional[dict[str, Any]] = None,
    dry_run: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log (kind, message_id, thread_id, tier, summary, reversible, "
        " undo_data, dry_run) VALUES (?,?,?,?,?,?,?,?)",
        (kind, message_id, thread_id, tier, summary, 1 if reversible else 0,
         json.dumps(undo_data) if undo_data else None, 1 if dry_run else 0),
    )
    return int(cur.lastrowid)


def has_action(conn: sqlite3.Connection, message_id: str, kinds: tuple[str, ...]) -> bool:
    """True if an audit row of one of `kinds` already exists for this message.

    Used to make SILENT/FYI effects idempotent across crash-recovery reprocessing:
    if we already archived/labeled/FYI'd this message, don't do it (or notify)
    again on a re-run."""
    if not message_id or not kinds:
        return False
    placeholders = ",".join("?" for _ in kinds)
    row = conn.execute(
        f"SELECT 1 FROM audit_log WHERE message_id=? AND kind IN ({placeholders}) LIMIT 1",
        (message_id, *kinds),
    ).fetchone()
    return row is not None


def last_undoable_action(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audit_log WHERE reversible=1 AND undone=0 AND undo_data IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


def mark_undone(conn: sqlite3.Connection, audit_id: int) -> None:
    conn.execute("UPDATE audit_log SET undone=1 WHERE id=?", (audit_id,))


def recent_actions(conn: sqlite3.Connection, since_epoch: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM audit_log WHERE ts >= ? ORDER BY ts ASC", (since_epoch,)
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Voice samples
# ─────────────────────────────────────────────────────────────────────────────
def add_voice_sample(
    conn: sqlite3.Connection, *, body: str, subject: str = "", contact_email: Optional[str] = None
) -> None:
    conn.execute(
        "INSERT INTO voice_samples (contact_email, subject, body) VALUES (?,?,?)",
        ((contact_email or None) and contact_email.lower(), subject, body),
    )


_VOICE_WORD = re.compile(r"[a-z0-9']+")
_VOICE_STOP = frozenset(
    "the a an and or to of in is it its i you we us he she they them for on at be this that "
    "with your my our as was are were will would can could just so if but not no yes do did "
    "have has had me him her his their there here what when how why who".split()
)


def _voice_tokens(text: str) -> set[str]:
    return {w for w in _VOICE_WORD.findall((text or "").lower())
            if len(w) > 1 and w not in _VOICE_STOP}


def get_voice_samples(
    conn: sqlite3.Connection, contact_email: str = "", limit: int = 5, context_text: str = "",
) -> list[sqlite3.Row]:
    """Return up to `limit` few-shot writing samples, preferring ones written to this contact,
    then falling back to global samples — so drafts echo how you write to *this* person.

    When `context_text` (the thread being replied to) is given, the samples are chosen by
    SIMILARITY to it (token-set cosine) instead of plain recency, so the examples match the
    SITUATION — a short casual ping gets short casual exemplars, a long formal email gets
    formal ones — which is exactly what a few-shot is for. Falls back to recency when there's
    no usable signal."""
    ctx = _voice_tokens(context_text) if (context_text or "").strip() else set()

    if ctx:
        # Pull a larger candidate pool (contact-preferred, then global) and rank by similarity.
        pool = max(limit * 8, 24)
        cand: list[sqlite3.Row] = []
        if contact_email:
            cand = list(conn.execute(
                "SELECT * FROM voice_samples WHERE contact_email=? ORDER BY ts DESC LIMIT ?",
                (contact_email.lower(), pool)))
        if len(cand) < pool:
            cand.extend(conn.execute(
                "SELECT * FROM voice_samples WHERE contact_email IS NULL ORDER BY ts DESC LIMIT ?",
                (pool - len(cand),)))
        if cand:
            ce = (contact_email or "").lower()

            def _score(r: sqlite3.Row) -> tuple[float, int, int]:
                toks = _voice_tokens(((r["subject"] or "") + " " + (r["body"] or "")))
                sim = (len(ctx & toks) / ((len(ctx) ** 0.5) * (len(toks) ** 0.5))) if toks else 0.0
                is_contact = 1 if ((r["contact_email"] or "").lower() == ce and ce) else 0
                return (sim, is_contact, int(r["ts"] or 0))   # similarity, then this-contact, then recency

            cand.sort(key=_score, reverse=True)
            return cand[:limit]
        # nothing in the pool → fall through to the recency path

    rows: list[sqlite3.Row] = []
    if contact_email:
        rows = list(
            conn.execute(
                "SELECT * FROM voice_samples WHERE contact_email=? ORDER BY ts DESC LIMIT ?",
                (contact_email.lower(), limit),
            )
        )
    if len(rows) < limit:
        more = conn.execute(
            "SELECT * FROM voice_samples WHERE contact_email IS NULL "
            "ORDER BY ts DESC LIMIT ?",
            (limit - len(rows),),
        )
        rows.extend(more)
    return rows


def voice_sample_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM voice_samples").fetchone()
    return int(row["n"]) if row else 0


def all_voice_samples(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every sample (for the per-segment rebuild). Segment is derived from the
    sample's contact_email at rebuild time, so no segment column is needed."""
    return list(conn.execute("SELECT * FROM voice_samples ORDER BY ts DESC"))


# ── Segmented voice profiles (P5a) ───────────────────────────────────────────
def get_voice_profile(conn: sqlite3.Connection, segment: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM voice_profiles WHERE segment=?", (segment,)
    ).fetchone()


def list_voice_profiles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM voice_profiles ORDER BY segment"))


def upsert_voice_profile(
    conn: sqlite3.Connection, segment: str, profile_json: str, sample_count: int
) -> None:
    conn.execute(
        "INSERT INTO voice_profiles (segment, profile_json, sample_count, last_rebuilt) "
        "VALUES (?,?,?,strftime('%s','now')) "
        "ON CONFLICT(segment) DO UPDATE SET "
        " profile_json=excluded.profile_json, sample_count=excluded.sample_count, "
        " last_rebuilt=excluded.last_rebuilt",
        (segment, profile_json, int(sample_count)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Learning events
# ─────────────────────────────────────────────────────────────────────────────
# GAP 5 — the canonical set of learning_events.type values. Every INSERT into
# learning_events must pass one of these so the learning loop can aggregate by type
# (rows with a NULL/blank type are invisible to the loop). 'approve'|'edit'|'skip' are
# the per-card signals; the rest are richer feedback the loop can grow into.
LEARNING_EVENT_TYPES = (
    "skip", "approve", "edit", "draft_accepted", "draft_rejected",
    "tier_feedback", "rule_confirmed",
    # legacy/auxiliary types still recorded elsewhere:
    "override", "undo", "pause", "sent", "send",
)


def record_event(
    conn: sqlite3.Connection,
    *,
    type: str,
    message_id: str = "",
    action_id: Optional[int] = None,
    contact_email: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    # Never write a blank/None type — that is exactly the bug GAP 5 fixes (events with a
    # missing type are invisible to the learning loop). Fall back to 'unknown' loudly.
    ev_type = (type or "").strip() or "unknown"
    conn.execute(
        "INSERT INTO learning_events (type, message_id, action_id, contact_email, detail) "
        "VALUES (?,?,?,?,?)",
        (ev_type, message_id, action_id, (contact_email or "").lower(),
         json.dumps(detail) if detail else None),
    )


def count_events(
    conn: sqlite3.Connection, *, type: str, contact_email: str = ""
) -> int:
    if contact_email:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM learning_events WHERE type=? AND contact_email=?",
            (type, contact_email.lower()),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM learning_events WHERE type=?", (type,)
        ).fetchone()
    return int(row["n"]) if row else 0


def now_epoch() -> int:
    return int(time.time())


# ─────────────────────────────────────────────────────────────────────────────
# Feedback-loop capture (P5c)
# ─────────────────────────────────────────────────────────────────────────────
def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex


def bump_contact_importance(conn: sqlite3.Connection, email: str, delta: int = 1) -> None:
    """Nudge a contact's importance (clamped 0..100). Approving their drafts is a
    positive signal that they matter."""
    email = (email or "").lower()
    if not email:
        return
    # Create the row if absent, then clamp.
    conn.execute(
        "INSERT INTO contacts (email, importance) VALUES (?, ?) "
        "ON CONFLICT(email) DO UPDATE SET importance="
        " MAX(0, MIN(100, importance + ?)), updated_at=strftime('%s','now')",
        (email, max(0, min(100, delta)), delta),
    )


def add_draft_edit(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    segment: str,
    original_draft: str,
    final_draft: str,
    diff: str,
) -> str:
    rid = _new_id()
    conn.execute(
        "INSERT INTO draft_edits (id, message_id, segment, original_draft, final_draft, diff) "
        "VALUES (?,?,?,?,?,?)",
        (rid, message_id, segment, original_draft, final_draft, diff),
    )
    return rid


def add_skip_log(
    conn: sqlite3.Connection, *, message_id: str, tier: Optional[int], summary: str, reason: str = ""
) -> str:
    rid = _new_id()
    conn.execute(
        "INSERT INTO skip_log (id, message_id, tier, summary, reason) VALUES (?,?,?,?,?)",
        (rid, message_id, tier, summary, reason),
    )
    return rid


def add_proposed_rule(
    conn: sqlite3.Connection, *, rule_text: str, source: str = "learned", pattern_evidence: str = ""
) -> str:
    rid = _new_id()
    conn.execute(
        "INSERT INTO proposed_rules (id, rule_text, source, pattern_evidence, status) "
        "VALUES (?,?,?,?,'pending')",
        (rid, rule_text, source, pattern_evidence),
    )
    return rid


def list_proposed_rules(conn: sqlite3.Connection, status: str = "pending") -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM proposed_rules WHERE status=? ORDER BY created_at DESC", (status,)
    ))


def get_proposed_rule(conn: sqlite3.Connection, rule_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM proposed_rules WHERE id=?", (rule_id,)).fetchone()


# ─────────────────────────────────────────────────────────────────────────────
# Cross-channel PERSON identity (Memory Part A)
# ─────────────────────────────────────────────────────────────────────────────
def person_add(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    display_name: str = "",
    emails: Optional[list[str]] = None,
    phone_jids: Optional[list[str]] = None,
    company: str = "",
    role: str = "",
    segment: str = "",
    relationship: str = "",
) -> None:
    conn.execute(
        "INSERT INTO persons (id, display_name, emails, phone_jids, company, role, "
        " segment, relationship) VALUES (?,?,?,?,?,?,?,?)",
        (person_id, display_name, json.dumps(emails or []), json.dumps(phone_jids or []),
         company, role, segment, relationship),
    )


def person_get(conn: sqlite3.Connection, person_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()


def person_update(
    conn: sqlite3.Connection,
    person_id: str,
    *,
    display_name: Optional[str] = None,
    emails: Optional[list[str]] = None,
    phone_jids: Optional[list[str]] = None,
    company: Optional[str] = None,
    role: Optional[str] = None,
    segment: Optional[str] = None,
    relationship: Optional[str] = None,
) -> None:
    sets, params = [], []
    for col, val in (
        ("display_name", display_name), ("company", company), ("role", role),
        ("segment", segment), ("relationship", relationship),
    ):
        if val is not None:
            sets.append(f"{col}=?"); params.append(val)
    if emails is not None:
        sets.append("emails=?"); params.append(json.dumps(emails))
    if phone_jids is not None:
        sets.append("phone_jids=?"); params.append(json.dumps(phone_jids))
    if not sets:
        return
    sets.append("updated_at=strftime('%s','now')")
    params.append(person_id)
    conn.execute(f"UPDATE persons SET {', '.join(sets)} WHERE id=?", params)


def person_delete(conn: sqlite3.Connection, person_id: str) -> None:
    conn.execute("DELETE FROM persons WHERE id=?", (person_id,))


# Valid relationship_type values (GAP 1). 'unknown' is the default until inferred.
RELATIONSHIP_TYPES = (
    "partner", "family", "investor", "mentor", "collaborator",
    "customer", "recruiter", "cold", "unknown",
)


def person_relationship_type(conn: sqlite3.Connection, person_id: str) -> str:
    """The person's classified relationship_type, or 'unknown' when absent/unset.
    Defensive: an older DB without the column (pre-migration) returns 'unknown'."""
    if not person_id:
        return "unknown"
    try:
        row = conn.execute(
            "SELECT relationship_type FROM persons WHERE id=?", (person_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return "unknown"
    if row is None:
        return "unknown"
    rt = (row["relationship_type"] or "").strip().lower()
    return rt if rt in RELATIONSHIP_TYPES else "unknown"


def set_person_relationship_type(conn: sqlite3.Connection, person_id: str, rel_type: str) -> bool:
    """Set a person's relationship_type (validated against RELATIONSHIP_TYPES). Returns
    True iff a known type was written. Unknown/garbage input is rejected (no-op)."""
    rt = (rel_type or "").strip().lower()
    if not person_id or rt not in RELATIONSHIP_TYPES:
        return False
    has_updated = "updated_at" in {r[1] for r in conn.execute("PRAGMA table_info(persons)")}
    if has_updated:
        conn.execute(
            "UPDATE persons SET relationship_type=?, updated_at=strftime('%s','now') WHERE id=?",
            (rt, person_id),
        )
    else:
        conn.execute(
            "UPDATE persons SET relationship_type=? WHERE id=?", (rt, person_id)
        )
    return True


def relationship_type_for_identifier(conn: sqlite3.Connection, identifier: str) -> str:
    """Resolve an inbound identifier (email or JID) → its person's relationship_type.
    Returns 'unknown' when the identifier isn't linked to a person. Best-effort."""
    if not identifier:
        return "unknown"
    pid = person_link_get(conn, (identifier or "").lower())
    return person_relationship_type(conn, pid) if pid else "unknown"


def persons_by_name(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM persons WHERE lower(display_name)=?", ((name or "").lower(),)
    ))


def persons_by_name_company(conn: sqlite3.Connection, name: str, company: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM persons WHERE lower(display_name)=? AND lower(company)=? AND company!=''",
        ((name or "").lower(), (company or "").lower()),
    ))


def person_link_get(conn: sqlite3.Connection, identifier: str) -> Optional[str]:
    row = conn.execute(
        "SELECT person_id FROM person_links WHERE identifier=?", ((identifier or "").lower(),)
    ).fetchone()
    return row["person_id"] if row else None


def person_link_set(
    conn: sqlite3.Connection, identifier: str, person_id: str,
    *, confidence: float = 1.0, source: str = "observed",
) -> None:
    conn.execute(
        "INSERT INTO person_links (identifier, person_id, confidence, source) VALUES (?,?,?,?) "
        "ON CONFLICT(identifier) DO UPDATE SET person_id=excluded.person_id, "
        " confidence=excluded.confidence, source=excluded.source",
        ((identifier or "").lower(), person_id, confidence, source),
    )


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def phone_digits_match(a: str, b: str) -> bool:
    """Whether two phone digit-strings denote the SAME number. Safe by construction:

      * exact full-digit equality, OR
      * an identical trailing run of >= 10 digits (a full national subscriber number),
        which absorbs country-code-prefix differences (e.g. 919876543210 vs 9876543210).

    Never a mid-string substring match — that was the `memory-identity-1` defect, where
    a short digit run inside an unrelated longer JID silently fused two people. A full
    10-digit national number is effectively unique, so trailing-equality is high
    confidence; anything shorter or only partially overlapping does NOT match.
    """
    a, b = _digits_only(a), _digits_only(b)
    if len(a) < 8 or len(b) < 8:
        return False
    if a == b:
        return True
    n = 10
    if len(a) >= n and len(b) >= n:
        return a[-n:] == b[-n:]
    return False


def person_link_by_phone_digits(conn: sqlite3.Connection, digits: str) -> Optional[str]:
    """Match a phone-number digit string (e.g. from an email signature) to the WhatsApp
    person who owns that exact number.

    Returns a person_id ONLY when exactly ONE person matches under `phone_digits_match`
    (exact / trailing-national equality, never substring). Zero matches OR more than one
    candidate person -> None, so an ambiguous signal can never trigger a silent merge
    (IDENTITY_SAFETY / NO_AUTO_MERGE_HIGH_CONFIDENCE). Regression: `memory-identity-1`.
    """
    digits = _digits_only(digits)
    if len(digits) < 8:
        return None
    matched: set[str] = set()
    for row in conn.execute(
        "SELECT identifier, person_id FROM person_links "
        "WHERE identifier LIKE '%@s.whatsapp.net'"
    ):
        local = (row["identifier"] or "").split("@", 1)[0]
        if phone_digits_match(local, digits):
            matched.add(row["person_id"])
            if len(matched) > 1:
                return None  # ambiguous -> no auto-link (fail safe)
    return next(iter(matched)) if len(matched) == 1 else None


def suggestion_add(
    conn: sqlite3.Connection, *, suggestion_id: str, identifier_new: str,
    candidate_person_id: str, reason: str = "", confidence: float = 0.0,
) -> str:
    conn.execute(
        "INSERT INTO person_link_suggestions (id, identifier_new, candidate_person_id, "
        " reason, confidence, status) VALUES (?,?,?,?,?,'pending')",
        (suggestion_id, (identifier_new or "").lower(), candidate_person_id, reason, confidence),
    )
    return suggestion_id


def suggestion_get(conn: sqlite3.Connection, suggestion_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM person_link_suggestions WHERE id=?", (suggestion_id,)
    ).fetchone()


def suggestion_exists(conn: sqlite3.Connection, identifier_new: str, candidate_person_id: str) -> bool:
    """True if this (identifier, candidate) pair was ever suggested — pending, confirmed,
    OR rejected — so a rejected pair is never re-asked."""
    row = conn.execute(
        "SELECT 1 FROM person_link_suggestions WHERE identifier_new=? AND candidate_person_id=? LIMIT 1",
        ((identifier_new or "").lower(), candidate_person_id),
    ).fetchone()
    return row is not None


def suggestion_list_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM person_link_suggestions WHERE status='pending' ORDER BY created_at"
    ))


def suggestion_set_status(conn: sqlite3.Connection, suggestion_id: str, status: str) -> bool:
    cur = conn.execute(
        "UPDATE person_link_suggestions SET status=?, resolved_at=strftime('%s','now') "
        "WHERE id=? AND status='pending'",
        (status, suggestion_id),
    )
    return cur.rowcount == 1


# ─────────────────────────────────────────────────────────────────────────────
# Relationship memory (Memory Part B)
# ─────────────────────────────────────────────────────────────────────────────
def relationship_memory_get(conn: sqlite3.Connection, person_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM relationship_memory WHERE person_id=?", (person_id,)
    ).fetchone()


def relationship_memory_upsert(
    conn: sqlite3.Connection,
    person_id: str,
    *,
    summary_json: str,
    open_situations_json: str,
    decided_json: str,
    episodes_json: str,
    superseded_json: str,
    last_distilled_at: Optional[int],
    version: int,
) -> None:
    conn.execute(
        "INSERT INTO relationship_memory (person_id, summary_json, open_situations_json, "
        " decided_json, episodes_json, superseded_json, last_distilled_at, version) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(person_id) DO UPDATE SET "
        " summary_json=excluded.summary_json, open_situations_json=excluded.open_situations_json, "
        " decided_json=excluded.decided_json, episodes_json=excluded.episodes_json, "
        " superseded_json=excluded.superseded_json, last_distilled_at=excluded.last_distilled_at, "
        " version=excluded.version",
        (person_id, summary_json, open_situations_json, decided_json, episodes_json,
         superseded_json, last_distilled_at, version),
    )


def set_proposed_rule_status(conn: sqlite3.Connection, rule_id: str, status: str) -> bool:
    cur = conn.execute(
        "UPDATE proposed_rules SET status=?, resolved_at=strftime('%s','now') "
        "WHERE id=? AND status='pending'",
        (status, rule_id),
    )
    return cur.rowcount == 1


# ─────────────────────────────────────────────────────────────────────────────
# ux-web-display cluster — additive helpers (no shared-schema changes).
#
# These live in repositories.py (a file this cluster owns) and create their own
# tables via CREATE TABLE IF NOT EXISTS so no migration/db.py edit is needed.
# ─────────────────────────────────────────────────────────────────────────────

# Domains/JID suffixes WhatsApp uses. A JID like "1234567890@s.whatsapp.net" (direct)
# or "...@g.us" (group), or the "@lid" linked-id form, is a WhatsApp identifier even
# when it lacks the message-level "wa_" id prefix.
_WHATSAPP_JID_SUFFIXES = ("@s.whatsapp.net", "@g.us", "@lid", "@c.us", "@broadcast")


def channel_for_identifier(identifier: str) -> str:
    """Map a thread/message identifier to a human channel label ("WhatsApp"/"Email").

    Root cause (ux-trust-4): reminder cards carry a WhatsApp JID as their message_id /
    thread_id (e.g. "1234567890@s.whatsapp.net"), which does NOT start with the message
    "wa_" prefix the API used to guess channel — so a WhatsApp situation was mislabeled
    "Email". Deriving the channel from the JID *shape* fixes that for both wa_-prefixed
    message ids and bare JIDs. Default "Email" is preserved for plain Gmail ids."""
    mid = (identifier or "").lower()
    if mid.startswith("wa_") or any(suf in mid for suf in _WHATSAPP_JID_SUFFIXES):
        return "WhatsApp"
    return "Email"


# ── ux-trust-4: reminder provenance (sender + a real quote + channel hint) ────
def ensure_reminder_meta(conn: sqlite3.Connection) -> None:
    """Create the reminder_meta side table if absent. Keyed by the reminder's
    idempotency_key so it joins 1:1 with the pending_actions reminder row without
    touching the shared pending_actions schema."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reminder_meta ("
        " idempotency_key TEXT PRIMARY KEY,"
        " sender_name TEXT DEFAULT '',"
        " quote TEXT DEFAULT '',"
        " channel TEXT DEFAULT '',"
        " thread_id TEXT DEFAULT '',"
        " created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))"
        ")"
    )


def set_reminder_meta(
    conn: sqlite3.Connection, idempotency_key: str, *,
    sender_name: str = "", quote: str = "", channel: str = "", thread_id: str = "",
) -> None:
    """Stamp the human-verifiable provenance for a reminder card so the approval UI can
    show WHO it is about, an actual quoted line, and the right channel — instead of an
    empty-sender / "Other" / "Email" tier-3 nag (ux-trust-4)."""
    try:
        ensure_reminder_meta(conn)
        conn.execute(
            "INSERT INTO reminder_meta (idempotency_key, sender_name, quote, channel, thread_id) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(idempotency_key) DO UPDATE SET "
            " sender_name=excluded.sender_name, quote=excluded.quote, "
            " channel=excluded.channel, thread_id=excluded.thread_id",
            (idempotency_key, sender_name or "", quote or "", channel or "", thread_id or ""),
        )
    except sqlite3.Error:
        # Provenance is best-effort enrichment; never let it break the reminder sweep.
        pass


def get_reminder_meta(conn: sqlite3.Connection, idempotency_key: str) -> Optional[sqlite3.Row]:
    try:
        ensure_reminder_meta(conn)
        return conn.execute(
            "SELECT * FROM reminder_meta WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
    except sqlite3.Error:
        return None


# ── ux-trust-6: recoverable, scoped bulk-skip (undo within a grace window) ────
def ensure_bulk_skip_undo(conn: sqlite3.Connection) -> None:
    """Create the bulk_skip_undo journal if absent. Each row records ONE action that a
    'Clear all' bulk-skip transitioned to SKIPPED, so it can be restored to PENDING
    within a short grace window. Own table → no shared-schema change."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bulk_skip_undo ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " batch_id TEXT NOT NULL,"
        " action_id INTEGER NOT NULL,"
        " prev_status TEXT NOT NULL,"
        " restored INTEGER NOT NULL DEFAULT 0,"
        " created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bulk_skip_batch ON bulk_skip_undo(batch_id)"
    )


def record_bulk_skip(
    conn: sqlite3.Connection, batch_id: str, action_id: int, prev_status: str
) -> None:
    """Journal that `action_id` (previously `prev_status`) was skipped as part of bulk
    `batch_id`, so the owner can undo the whole batch if they tapped 'Clear all' by
    mistake (ux-trust-6)."""
    try:
        ensure_bulk_skip_undo(conn)
        conn.execute(
            "INSERT INTO bulk_skip_undo (batch_id, action_id, prev_status) VALUES (?,?,?)",
            (batch_id, int(action_id), prev_status or "PENDING"),
        )
    except sqlite3.Error:
        pass


def restore_bulk_skip(
    conn: sqlite3.Connection, batch_id: str, *, within_seconds: int = 600
) -> int:
    """Undo a bulk skip: move every action journaled under `batch_id` (and skipped within
    the grace window) from terminal SKIPPED back to PENDING, so it re-surfaces. Returns the
    number of decisions actually restored.

    Safety: only restores rows that are STILL SKIPPED (never revives one the owner has since
    re-handled), and never touches a row that left the pre-send envelope. A reminder restored
    to PENDING is exactly its pre-clear state, so no send-state-machine guarantee is weakened
    — restore is the inverse of mark_skipped's PENDING→SKIPPED transition only."""
    restored = 0
    try:
        ensure_bulk_skip_undo(conn)
        cutoff = now_epoch() - max(1, int(within_seconds))
        rows = conn.execute(
            "SELECT id, action_id, prev_status FROM bulk_skip_undo "
            "WHERE batch_id=? AND restored=0 AND created_at >= ?",
            (batch_id, cutoff),
        ).fetchall()
        for r in rows:
            # Only un-skip rows that are still SKIPPED (the owner has not re-acted on them).
            cur = conn.execute(
                "UPDATE pending_actions SET status='PENDING', decided_at=NULL "
                "WHERE id=? AND status='SKIPPED'",
                (int(r["action_id"]),),
            )
            if cur.rowcount == 1:
                restored += 1
            conn.execute(
                "UPDATE bulk_skip_undo SET restored=1 WHERE id=?", (int(r["id"]),)
            )
        if restored:
            try:
                record_event(
                    conn, type="bulk_skip_undo",
                    detail={"batch_id": batch_id, "restored": restored},
                )
            except Exception:  # noqa: BLE001 - observability is best-effort
                pass
    except sqlite3.Error:
        pass
    return restored
