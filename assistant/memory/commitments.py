"""Commitment tracker (P4b) — promises you made, surfaced before you drop them.

After you send a reply, the model extracts any explicit promises ("I'll send the
deck by Friday"). They're stored, and the daily check surfaces ones that are due
soon or have gone quiet, plus threads that have stalled after you replied.

LLM extraction is best-effort: invalid output ⇒ no commitments, never a crash. The
query/CRUD layer is pure SQL + datetime and is unit-tested.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from assistant.config import Settings
from assistant.llm import prompts
from assistant.llm.client import LLMClient
from assistant.llm.router import Task
from assistant.logging_setup import get_logger

log = get_logger("commitments")

COMMIT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "commitment_text": {"type": "string"},
                    "due_date_hint": {"type": ["string", "null"]},
                    "contact_email": {"type": ["string", "null"]},
                },
                "required": ["commitment_text", "due_date_hint", "contact_email"],
            },
        }
    },
    "required": ["commitments"],
}


# ─────────────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Phase 8 additive migration: owner ('me'|'them') + direction ('outbound'|'inbound')
    so we can track commitments BOTH parties make. Backward-compatible defaults."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(commitments)")}
        if "owner" not in cols:
            conn.execute("ALTER TABLE commitments ADD COLUMN owner TEXT NOT NULL DEFAULT 'me'")
        if "direction" not in cols:
            conn.execute("ALTER TABLE commitments ADD COLUMN direction TEXT NOT NULL DEFAULT 'outbound'")
    except sqlite3.OperationalError:
        pass  # raced with another connection; column already added


def _normalize_date(hint: Any, *, today=None) -> str:
    """ISO YYYY-MM-DD as-is; otherwise try natural-language parsing ('Friday', 'next
    week', 'eod', 'in 3 days') via Phase 8's parser; unparseable → ''."""
    s = str(hint or "").strip()
    if not s:
        return ""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except (ValueError, TypeError):
        pass
    try:
        from datetime import date as _date
        from assistant.memory.commitment_extract import parse_nl_date
        return parse_nl_date(s, today=today or _date.today())
    except Exception:  # noqa: BLE001
        return ""


def parse_commitments(raw: Any, contact_email: str = "") -> list[dict]:
    """Parse the extractor output into clean dicts. Never raises → [] on bad input."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    items = data.get("commitments") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = str(it.get("commitment_text", "")).strip()
        if not text:
            continue
        out.append({
            "commitment_text": text,
            "due_date": _normalize_date(it.get("due_date_hint")),
            "contact_email": (str(it.get("contact_email") or "") or contact_email or "").lower(),
        })
    return out


def extract_commitments(
    llm: LLMClient, settings: Settings, sent_text: str, contact_email: str, *, message_id: str = ""
) -> list[dict]:
    """Extract commitments from a sent reply. Best-effort: [] on any failure."""
    if not (sent_text or "").strip():
        return []
    try:
        system = prompts.load("commitments", settings.prompts_dir)
        raw = llm.complete_json(
            task=Task.COMMITMENT_EXTRACT, system_prefix=system, user_text=sent_text,
            schema=COMMIT_JSON_SCHEMA, message_id=message_id,
        )
        return parse_commitments(raw, contact_email)
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        log.warning("extract_commitments failed (non-fatal): %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-5 — negation/polarity awareness for commitment dedup.
# ROOT CAUSE: dedup compared raw token-overlap Jaccard with NO awareness of polarity.
# "I will send the report by Aug 30" and "I will NOT send the report by Aug 20 (deal
# dead)" share ~0.7 of their tokens, so the contradicting retraction was treated as a
# duplicate: it was dropped (never inserted) AND it silently pulled the surviving
# commitment's due_date 10 days EARLIER (because _is_more_specific_due only ever
# tightens). The owner was then nagged about an obligation that was explicitly
# cancelled, on a date nobody stated. Symmetrically, a re-stated commitment carrying a
# CORRECTED LATER deadline was silently ignored because the date never relaxed.
# FIX: (1) negation tokens carry polarity, not topic — strip them (and stopwords)
# BEFORE Jaccard so overlap reflects the deliverable, not filler; (2) if the two texts
# DISAGREE on polarity (one negated, the other not), they are NOT duplicates — never
# merge a "will" with a "won't"; (3) only adjust a due_date on a HIGH-confidence match
# (not a borderline 0.6 overlap), and allow correcting to a LATER date too, not only
# tightening earlier.
_NEGATION_TOKENS = frozenset({
    "not", "no", "never", "wont", "cant", "cannot", "dont", "doesnt", "didnt",
    "isnt", "arent", "wasnt", "werent", "shouldnt", "wouldnt", "couldnt",
    "cancel", "cancelled", "canceled", "cancelling", "canceling", "scrap",
    "scrapped", "drop", "dropped", "abort", "aborted", "withdraw", "withdrawn",
    "rescind", "rescinded", "void", "dead", "off",
})

# Common filler tokens that inflate Jaccard without identifying the deliverable. Kept
# deliberately small + conservative so genuine deduplication still fires.
_STOPWORDS = frozenset({
    "i", "ill", "will", "would", "can", "could", "to", "the", "a", "an", "of",
    "for", "by", "and", "on", "in", "at", "is", "be", "you", "your", "we", "it",
    "that", "this", "with", "send", "get", "do", "make",
})


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens (split on whitespace + punctuation) for Jaccard similarity.

    Contractions are normalised by dropping apostrophes BEFORE splitting so "won't" /
    "don't" survive as a single token ("wont"/"dont") and are recognised as negations,
    rather than splitting into "won"/"t"."""
    import re
    collapsed = (text or "").lower().replace("'", "").replace("’", "")
    return {t for t in re.split(r"[^a-z0-9]+", collapsed) if t}


def _has_negation(text: str) -> bool:
    """Whether the text expresses a negative/cancelling polarity ("won't", "cancel")."""
    return bool(_tokenize(text) & _NEGATION_TOKENS)


def _polarity_disagrees(a: str, b: str) -> bool:
    """True when one text is negated and the other is not — a polarity contradiction.
    Two negated texts (or two affirmative texts) do NOT disagree."""
    return _has_negation(a) != _has_negation(b)


def _content_tokens(text: str) -> set[str]:
    """Tokens that identify the DELIVERABLE: negation + stopword tokens removed so
    polarity words and filler never carry the overlap (memory-knowledge-5)."""
    return _tokenize(text) - _NEGATION_TOKENS - _STOPWORDS


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity of two texts' CONTENT token sets (0..1), with negation and
    stopword tokens stripped first so polarity ("will" vs "won't") is never what carries
    the overlap. Empty/empty → 0."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    union = len(ta | tb)
    return (len(ta & tb) / union) if union else 0.0


_DEDUP_JACCARD_THRESHOLD = 0.6
# Only adjust a surviving commitment's due_date when the texts match strongly — a
# borderline 0.6 topical overlap is too weak to silently rewrite a deadline.
_DUE_ADJUST_JACCARD_THRESHOLD = 0.8


def _is_more_specific_due(new_due: str, old_due: str) -> bool:
    """True if new_due is a more specific deadline than old_due: a date where there was
    none, OR a DIFFERENT valid date (the corrected value wins — earlier OR later).

    memory-knowledge-5: the old version only accepted EARLIER dates, so a re-stated
    commitment with a corrected LATER deadline was silently ignored. Date adjustment is
    now gated by a high-confidence text match in the caller, so accepting a later
    correction here cannot be exploited by a weak token overlap."""
    new_due = (new_due or "").strip()
    old_due = (old_due or "").strip()
    if not new_due:
        return False
    if not old_due:
        return True
    try:
        nd = datetime.strptime(new_due, "%Y-%m-%d")
        od = datetime.strptime(old_due, "%Y-%m-%d")
        return nd != od
    except (ValueError, TypeError):
        return False


def _has_person_id_column(conn: sqlite3.Connection) -> bool:
    """Whether the commitments table has the person_id column (added by migration)."""
    try:
        return "person_id" in {r[1] for r in conn.execute("PRAGMA table_info(commitments)")}
    except sqlite3.OperationalError:
        return False


def add_commitment(
    conn: sqlite3.Connection, *, message_id: str, contact_email: str,
    commitment_text: str, due_date: str = "", owner: str = "me", direction: str = "outbound",
    person_id: str = "",
) -> str:
    """Add a commitment, deduplicating against existing OPEN commitments for the same
    person/contact (GAP 6). If a near-duplicate (Jaccard > 0.6) already exists, skip the
    insert and instead tighten the existing one's due_date when the new one is more
    specific. Returns the existing id when deduplicated, else the new id."""
    _ensure_columns(conn)
    has_pid = _has_person_id_column(conn)
    contact_email = (contact_email or "").lower()
    person_id = (person_id or "").strip()

    # ── Dedup: compare against open commitments for the same person (or contact) ──
    try:
        if has_pid and person_id:
            existing = conn.execute(
                "SELECT * FROM commitments WHERE status='open' AND person_id=?",
                (person_id,),
            ).fetchall()
        else:
            existing = conn.execute(
                "SELECT * FROM commitments WHERE status='open' AND contact_email=?",
                (contact_email,),
            ).fetchall()
        for r in existing:
            other_text = r["commitment_text"] or ""
            # memory-knowledge-5: a polarity contradiction (one "will", the other
            # "won't"/"cancel") is NOT a duplicate — never merge a promise with its
            # retraction. Let it fall through to a NEW row so the tracker can hold the
            # resolution alongside (or instead of) the original, rather than corrupting
            # the surviving obligation.
            if _polarity_disagrees(commitment_text, other_text):
                continue
            sim = _jaccard(commitment_text, other_text)
            if sim > _DEDUP_JACCARD_THRESHOLD:
                # Near-duplicate of the SAME polarity. Only rewrite the surviving
                # due_date on a HIGH-confidence text match (a borderline 0.6 overlap is
                # too weak to silently move a deadline), and allow a corrected LATER date
                # as well as an earlier one (memory-knowledge-5).
                if (sim >= _DUE_ADJUST_JACCARD_THRESHOLD
                        and _is_more_specific_due(due_date, r["due_date"] or "")):
                    conn.execute(
                        "UPDATE commitments SET due_date=? WHERE id=?", (due_date, r["id"]))
                return r["id"]
    except sqlite3.OperationalError:
        pass  # table-shape race — fall through to a plain insert

    cid = uuid.uuid4().hex
    if has_pid:
        conn.execute(
            "INSERT INTO commitments (id, message_id, contact_email, person_id, "
            " commitment_text, due_date, owner, direction) VALUES (?,?,?,?,?,?,?,?)",
            (cid, message_id, contact_email, person_id, commitment_text, due_date or "",
             owner, direction),
        )
    else:
        conn.execute(
            "INSERT INTO commitments (id, message_id, contact_email, commitment_text, due_date, "
            " owner, direction) VALUES (?,?,?,?,?,?,?)",
            (cid, message_id, contact_email, commitment_text, due_date or "", owner, direction),
        )
    return cid


def _anchor_date(inbound, now=None):
    """The date to anchor relative deadline parsing on (memory-knowledge-6).

    Priority: (1) the inbound message's own send timestamp (Message.timestamp, epoch
    seconds) so "Friday"/"tomorrow" resolve relative to WHEN IT WAS SENT, not when it
    happens to be processed; (2) the caller-supplied `now` (a date or datetime);
    (3) wall-clock date.today() as the last resort. Never raises."""
    from datetime import date as _date, datetime as _datetime
    try:
        ts = float(getattr(inbound, "timestamp", 0) or 0) if inbound is not None else 0.0
        if ts > 0:
            return _datetime.fromtimestamp(ts).date()
    except (ValueError, OverflowError, OSError, TypeError):
        pass  # implausible/overflowing timestamp -> fall back to the run clock
    if isinstance(now, _datetime):
        return now.date()
    if isinstance(now, _date):
        return now
    return _date.today()


def capture_from_inbound(
    conn: sqlite3.Connection, llm: LLMClient, settings: Settings, thread, *, now=None
) -> int:
    """Phase 8: extract commitments OTHERS make to YOU from an inbound thread ("I'll send
    it Friday") and store them (owner='them', direction='inbound'). Best-effort; returns
    the count stored; never raises into the pipeline."""
    try:
        if thread is None:
            return 0
        from assistant.memory import commitment_extract
        inbound = (thread.latest_inbound or thread.latest) if hasattr(thread, "latest") else None
        mid = inbound.id if inbound is not None else ""
        # memory-knowledge-6: anchor relative/weekday deadlines ("Friday", "tomorrow",
        # "in 3 days") on the message's SEND date, not the processing wall-clock. During
        # a backlog/restart catch-up, date.today() can be days after the message arrived,
        # so "Friday" would resolve to the NEXT week's Friday and a real obligation would
        # be mis-dated forward. Prefer the inbound message timestamp; fall back to the
        # caller's `now`/wall-clock only when the message has no usable timestamp (never
        # silently for a historical message that DOES carry one).
        anchor = _anchor_date(inbound, now)
        found = commitment_extract.extract(
            llm, thread, today=anchor, owner_is_sender=False)
        n = 0
        for c in found:
            counterparty = c.get("counterparty") or ""
            pid = _resolve_person_id(conn, counterparty)
            add_commitment(
                conn, message_id=mid, contact_email=counterparty,
                commitment_text=c.get("text", ""), due_date=c.get("due_date", ""),
                owner="them", direction="inbound", person_id=pid,
            )
            n += 1
        return n
    except Exception as exc:  # noqa: BLE001
        log.warning("capture_from_inbound failed (non-fatal): %s", exc)
        return 0


def open_commitments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM commitments WHERE status='open' ORDER BY created_at ASC"
    ))


def get_commitment(conn: sqlite3.Connection, cid: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM commitments WHERE id=?", (cid,)).fetchone()


def mark_done(conn: sqlite3.Connection, cid: str) -> bool:
    cur = conn.execute(
        "UPDATE commitments SET status='done', resolved_at=strftime('%s','now') "
        "WHERE id=? AND status!='done'",
        (cid,),
    )
    return cur.rowcount == 1


def snooze(conn: sqlite3.Connection, cid: str, *, days: int = 2, now: Optional[datetime] = None) -> bool:
    new_due = ((now or datetime.now()) + timedelta(days=days)).strftime("%Y-%m-%d")
    cur = conn.execute(
        "UPDATE commitments SET due_date=?, status='open' WHERE id=?", (new_due, cid)
    )
    return cur.rowcount == 1


# ─────────────────────────────────────────────────────────────────────────────
# Daily surfacing (8am) — due/stale selection
# ─────────────────────────────────────────────────────────────────────────────
def due_commitments(
    conn: sqlite3.Connection, *, now: Optional[datetime] = None, vip_emails=()
) -> list[sqlite3.Row]:
    """Open commitments worth surfacing today: due within 1 day, OR aged past the
    staleness threshold (VIP contacts: 3 days; others: 5)."""
    now = now or datetime.now()
    today = now.date()
    vips = {e.lower() for e in vip_emails}
    out: list[sqlite3.Row] = []
    for r in open_commitments(conn):
        due = r["due_date"]
        if due:
            try:
                if datetime.strptime(due, "%Y-%m-%d").date() <= today + timedelta(days=1):
                    out.append(r)
                    continue
            except ValueError:
                pass
        created = datetime.fromtimestamp(int(r["created_at"])).date()
        age = (today - created).days
        threshold = 3 if (r["contact_email"] or "").lower() in vips else 5
        if age >= threshold:
            out.append(r)
    return out


def stale_threads(
    conn: sqlite3.Connection, *, now: Optional[datetime] = None, vip_emails=()
) -> list[dict]:
    """Threads you replied to (tier 2/3, a send happened) that have since gone quiet
    past the threshold with no newer activity for that contact."""
    now = now or datetime.now()
    now_ts = int(now.timestamp())
    vips = {e.lower() for e in vip_emails}
    rows = conn.execute(
        "SELECT dl.sender_email AS email, dl.subject AS subject, dl.message_id AS mid, "
        "       MAX(al.ts) AS sent_ts "
        "FROM decision_log dl JOIN audit_log al "
        "  ON al.message_id = dl.message_id AND al.kind='send' "
        "WHERE dl.final_tier IN (2,3) "
        "GROUP BY dl.message_id"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        email = (r["email"] or "").lower()
        sent_ts = int(r["sent_ts"] or 0)
        if not sent_ts:
            continue
        age_days = (now_ts - sent_ts) // 86400
        threshold = 3 if email in vips else 6
        if age_days < threshold:
            continue
        # Skip if there's been newer activity for this contact since the send.
        newer = conn.execute(
            "SELECT 1 FROM decision_log WHERE sender_email=? AND ts > ? LIMIT 1",
            (email, sent_ts),
        ).fetchone()
        if newer:
            continue
        out.append({"email": email, "subject": r["subject"] or "", "days": int(age_days)})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Capture on send (best-effort)
# ─────────────────────────────────────────────────────────────────────────────
def capture_from_send(
    conn: sqlite3.Connection, llm: LLMClient, settings: Settings, action_row, *, contact_email: str = ""
) -> int:
    """Extract + store commitments from a sent reply. No-op in dry-run. Returns the
    count stored. Best-effort: never raises into the send path."""
    try:
        if settings.dry_run or action_row is None:
            return 0
        draft = ""
        mid = ""
        try:
            draft = action_row["draft_text"] or ""
            mid = action_row["message_id"] or ""
        except Exception:  # noqa: BLE001
            return 0
        found = extract_commitments(llm, settings, draft, contact_email, message_id=mid)
        for c in found:
            pid = _resolve_person_id(conn, c["contact_email"])
            add_commitment(
                conn, message_id=mid, contact_email=c["contact_email"],
                commitment_text=c["commitment_text"], due_date=c["due_date"], person_id=pid,
            )
        return len(found)
    except Exception as exc:  # noqa: BLE001
        log.warning("capture_from_send failed (non-fatal): %s", exc)
        return 0


def _resolve_person_id(conn: sqlite3.Connection, identifier: str) -> str:
    """Resolve a contact identifier (email/JID) to a cross-channel person_id for dedup.
    Best-effort → '' when unresolvable."""
    try:
        from assistant.memory import identity
        return identity.person_id_for(conn, (identifier or "").lower()) or ""
    except Exception:  # noqa: BLE001
        return ""
