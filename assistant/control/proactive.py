"""Proactive chief-of-staff sweep (Phase 9).

Where briefs report what already happened, this module looks FORWARD: once a day it
scans for the handful of things a good chief of staff would chase you about before
they slip — an important person you never got back to, a promise that is coming due,
a thread that went quiet after you replied, and a contact who keeps asking for the
same kind of thing.

Everything here is read-only selection plus one composed Telegram digest. There is no
new schema: it reuses the commitment selectors and reads decision_log / pending_actions
/ learning_events / contacts. Every function is best-effort and NEVER raises into the
poller — a missed nudge is acceptable, a crash in the control loop is not. Stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Any, Optional

from assistant.logging_setup import get_logger
from assistant.memory import commitments as C
from assistant.storage import decision_log
from assistant.storage import repositories as repo

log = get_logger("proactive")


def _tz(settings: Any):
    """The owner's configured timezone (settings.timezone), mirroring main._tz EXACTLY.

    Root cause (control-state-presence-4): run_sweep gated its 9am hour and stamped its
    once-a-day key on naive datetime.now() (the OS system tz). On a UTC host with
    TIMEZONE=America/Los_Angeles the digest fired at ~01:00 Pacific and its day boundary
    rolled at the wrong midnight — hours out of step with the morning brief / commitment
    check, which both use datetime.now(_tz(settings)). Returns None on any failure so
    callers fall back to naive now() exactly as main.py does."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(getattr(settings, "timezone", "") or "UTC")
    except Exception:  # noqa: BLE001
        return None


def _now(settings: Any) -> datetime:
    """Configured-tz 'now' (control-state-presence-4), with a naive fallback that mirrors
    main.py's try/except so a bad tz string never crashes the sweep."""
    try:
        return datetime.now(_tz(settings))
    except Exception:  # noqa: BLE001
        return datetime.now()


# GAP 7 — open situations awaiting the OWNER that have gone quiet are surfaced as
# reminders. "awaiting" is free text written by the distill LLM; these are the values
# that mean "waiting on Jatin/the owner".
_OWNER_AWAITING = frozenset({"owner", "me", "user", "owner", "you"})

# Window (days) used by the "surfaced but never acted on" and "recurring request"
# scans. Older items have either been handled or stopped mattering.
_UNANSWERED_WINDOW_DAYS = 7
_RECURRING_WINDOW_DAYS = 30
_RECURRING_MIN_COUNT = 3

# Digest shaping. We surface only a few of each kind so the message stays scannable in
# a phone banner; per-kind caps keep one noisy category from drowning the rest.
_PER_KIND_CAP = 4
_MAX_ITEMS = 10
_MAX_DIGEST_CHARS = 1600


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────
def _vip_emails(conn: sqlite3.Connection, settings: Any) -> set[str]:
    """Emails treated as high-importance (importance >= threshold OR a vip flag).

    Mirrors main._vip_emails so the proactive sweep applies the same VIP notion the
    commitment check does, without importing from the runner. Best-effort → empty set."""
    out: set[str] = set()
    try:
        threshold = getattr(settings, "vip_importance_threshold", 70)
        for row in conn.execute(
            "SELECT email FROM contacts WHERE importance >= ? OR flags LIKE '%vip%'",
            (threshold,),
        ):
            if row["email"]:
                out.add(row["email"].lower())
    except Exception:  # noqa: BLE001
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Selection functions — each returns list[{kind, summary, contact, detail}]
# ─────────────────────────────────────────────────────────────────────────────
def unanswered_important(conn: sqlite3.Connection, settings: Any) -> list[dict]:
    """Recent inbound from high-importance / VIP / personal contacts that the brain
    surfaced for you (final_tier >= 2) but which still has NO resolution: either a
    pending_action is still open, or there is no approve/sent learning event for it.

    These are the "you meant to get back to them" items — the most expensive kind of
    drop for a chief of staff to allow. Best-effort → [] on any error."""
    out: list[dict] = []
    try:
        decision_log.ensure(conn)
        vips = _vip_emails(conn, settings)
        since = repo.now_epoch() - _UNANSWERED_WINDOW_DAYS * 86400
        rows = conn.execute(
            "SELECT message_id, sender_email, sender_name, subject, category, final_tier "
            "FROM decision_log WHERE final_tier >= 2 AND ts >= ? ORDER BY ts DESC",
            (since,),
        ).fetchall()
        for r in rows:
            email = (r["sender_email"] or "").lower()
            category = (r["category"] or "").lower()
            important = (email in vips) or (category == "personal")
            if not important:
                continue
            if _is_resolved(conn, r["message_id"], email):
                continue
            who = r["sender_name"] or email or "someone"
            out.append({
                "kind": "unanswered_important",
                "summary": f"{who} is still waiting on you",
                "contact": email,
                "detail": (r["subject"] or "").strip() or "(no subject)",
            })
            if len(out) >= _PER_KIND_CAP:
                break
    except Exception:  # noqa: BLE001
        log.warning("unanswered_important failed (non-fatal)", exc_info=True)
    return out


def open_decisions(conn: sqlite3.Connection, settings: Any) -> list[dict]:
    """EVERY still-open tier-3 ASK, regardless of the sender's importance/category. An ASK is
    by definition a decision Steward could NOT make for the owner, so it must never depend on
    whether the contact is a learned VIP or flagged 'personal' (a one-off investor/legal/money
    sender is neither). This is the safety net that guarantees an unattended decision keeps
    resurfacing in the digest — the cardinal 'needs-attention is never auto-handled' rule applied
    to the FOLLOW-UP side. Best-effort → [] on any error."""
    out: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT id, summary, created_at FROM pending_actions "
            "WHERE kind='ask' AND status IN ('PENDING','APPROVED','EDITED') "
            "ORDER BY created_at ASC"
        ).fetchall()
        for r in rows:
            summary = (r["summary"] or "").strip()
            if not summary:
                continue
            out.append({
                "kind": "unanswered_important",   # same digest section ("Still waiting on you")
                "summary": summary if summary.lower().endswith(("?",)) else f"Needs your decision: {summary}",
                "contact": "",
                "detail": "",
            })
            if len(out) >= _PER_KIND_CAP:
                break
    except Exception:  # noqa: BLE001
        log.warning("open_decisions failed (non-fatal)", exc_info=True)
    return out


def _is_resolved(conn: sqlite3.Connection, message_id: str, email: str) -> bool:
    """True if this surfaced message has been dealt with: any non-open pending action
    for it (SENT/SKIPPED/...), or an approve/sent learning event recorded for it.

    An OPEN pending action (PENDING/APPROVED/EDITED) means it is still awaiting you, so
    it counts as unresolved. Best-effort → treat unknown as unresolved (surface it)."""
    try:
        pend = conn.execute(
            "SELECT status FROM pending_actions WHERE message_id=? "
            "ORDER BY id DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        if pend is not None:
            return pend["status"] not in ("PENDING", "APPROVED", "EDITED")
        # No pending row: treat an approve/sent learning event as resolution.
        ev = conn.execute(
            "SELECT 1 FROM learning_events "
            "WHERE message_id=? AND type IN ('approve','sent','send') LIMIT 1",
            (message_id,),
        ).fetchone()
        return ev is not None
    except Exception:  # noqa: BLE001
        return False


def at_risk_commitments(conn: sqlite3.Connection, settings: Any) -> list[dict]:
    """Promises you made that are due soon or overdue. Thin adapter over the existing,
    tested commitments.due_commitments — no duplicate logic. Best-effort → []."""
    out: list[dict] = []
    try:
        # control-state-presence-4: align the due window with the configured-tz clock that
        # maybe_surface_commitments uses, not the host system tz.
        now = _now(settings)
        vips = _vip_emails(conn, settings)
        for r in C.due_commitments(conn, now=now, vip_emails=vips)[:_PER_KIND_CAP]:
            email = (r["contact_email"] or "").lower()
            due = r["due_date"] or ""
            detail = f"due {due}" if due else "no date set"
            out.append({
                "kind": "at_risk_commitment",
                "summary": f"You promised: {r['commitment_text']}",
                "contact": email,
                "detail": detail,
            })
    except Exception:  # noqa: BLE001
        log.warning("at_risk_commitments failed (non-fatal)", exc_info=True)
    return out


def stalled_conversations(conn: sqlite3.Connection, settings: Any) -> list[dict]:
    """Threads you replied to that have since gone quiet. Thin adapter over the existing,
    tested commitments.stale_threads. Best-effort → []."""
    out: list[dict] = []
    try:
        # control-state-presence-4: configured-tz clock (see _now), matching the briefs.
        now = _now(settings)
        vips = _vip_emails(conn, settings)
        for s in C.stale_threads(conn, now=now, vip_emails=vips)[:_PER_KIND_CAP]:
            out.append({
                "kind": "stalled_conversation",
                "summary": f"{s['email'] or 'someone'} has gone quiet",
                "contact": (s["email"] or "").lower(),
                "detail": (
                    f"no reply in {s['days']}d on \"{s['subject'] or '(no subject)'}\""
                ),
            })
    except Exception:  # noqa: BLE001
        log.warning("stalled_conversations failed (non-fatal)", exc_info=True)
    return out


def recurring_requests(conn: sqlite3.Connection) -> list[dict]:
    """The same contact asking for the same KIND of thing, repeatedly. Group the recent
    decision_log by (sender_email, category) and flag any pair seen >= _RECURRING_MIN_COUNT
    times in the window — a signal the contact may want a standing rule or a real answer.

    Best-effort → []."""
    out: list[dict] = []
    try:
        decision_log.ensure(conn)
        since = repo.now_epoch() - _RECURRING_WINDOW_DAYS * 86400
        rows = conn.execute(
            "SELECT sender_email, sender_name, category, COUNT(*) AS n "
            "FROM decision_log "
            "WHERE ts >= ? AND sender_email IS NOT NULL AND sender_email != '' "
            "GROUP BY lower(sender_email), category "
            "HAVING n >= ? "
            "ORDER BY n DESC",
            (since, _RECURRING_MIN_COUNT),
        ).fetchall()
        for r in rows[:_PER_KIND_CAP]:
            email = (r["sender_email"] or "").lower()
            who = r["sender_name"] or email or "someone"
            cat = (r["category"] or "other").replace("_", " ")
            out.append({
                "kind": "recurring_request",
                "summary": f"{who} keeps surfacing {cat}",
                "contact": email,
                "detail": f"{int(r['n'])} times in {_RECURRING_WINDOW_DAYS}d — a rule may help",
            })
    except Exception:  # noqa: BLE001
        log.warning("recurring_requests failed (non-fatal)", exc_info=True)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Digest composition
# ─────────────────────────────────────────────────────────────────────────────
# Calm, scannable section headers. Order = priority (most expensive drop first).
_SECTION_ORDER = [
    ("unanswered_important", "🔴 Still waiting on you"),
    ("at_risk_commitment", "📋 Promises coming due"),
    ("stalled_conversation", "⏰ Gone quiet"),
    ("recurring_request", "🔁 Keeps coming up"),
]


def build_digest(items: list[dict]) -> str:
    """Compose ONE calm Telegram digest from selected items, grouped by kind in priority
    order. Returns "" when there is nothing genuinely useful to say — silence beats noise.

    Length-capped: at most _MAX_ITEMS lines and _MAX_DIGEST_CHARS characters."""
    if not items:
        return ""

    by_kind: dict[str, list[dict]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        summary = (it.get("summary") or "").strip()
        if not summary:
            continue
        by_kind.setdefault(it.get("kind") or "other", []).append(it)

    lines: list[str] = []
    shown = 0
    for kind, header in _SECTION_ORDER:
        group = by_kind.get(kind) or []
        if not group:
            continue
        section: list[str] = []
        for it in group:
            if shown >= _MAX_ITEMS:
                break
            summary = (it.get("summary") or "").strip()
            detail = (it.get("detail") or "").strip()
            line = f"  • {summary}"
            if detail:
                line += f" — {detail}"
            section.append(line)
            shown += 1
        if section:
            lines.append(header)
            lines.extend(section)
        if shown >= _MAX_ITEMS:
            break

    if not lines:
        return ""

    body = "🧭 A few things worth your attention\n\n" + "\n".join(lines)
    if len(body) > _MAX_DIGEST_CHARS:
        body = body[: _MAX_DIGEST_CHARS - 1].rstrip() + "…"
    return body


# ─────────────────────────────────────────────────────────────────────────────
# GAP 7 — proactive relationship reminders (creates pending reminder cards)
# ─────────────────────────────────────────────────────────────────────────────
def _person_display_name(conn: sqlite3.Connection, person_id: str) -> str:
    """Best-effort human name for a person_id (ux-trust-4). Falls back to "" so the card
    can render "someone" only when we genuinely have nothing."""
    try:
        p = repo.person_get(conn, person_id)
        if p is not None and "display_name" in p.keys():
            return (p["display_name"] or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _situation_quote(sit: dict) -> str:
    """A short, real quoted line for a reminder (ux-trust-4) — prefer an explicit quote/last
    message stored on the situation, else the situation text itself. Trimmed to a banner-
    friendly length so the card stays scannable."""
    for k in ("quote", "last_message", "last_line", "detail", "situation"):
        v = str(sit.get(k, "") or "").strip()
        if v:
            return v[:240]
    return ""


def _stamp_reminder_meta(
    conn: sqlite3.Connection, idem_key: str, person_id: str, sit: dict, thread_id: str
) -> None:
    """Persist verifiable provenance for a reminder card so the approval UI shows WHO it is
    about, an actual quoted line, and the right channel (ux-trust-4). Best-effort."""
    try:
        repo.set_reminder_meta(
            conn, idem_key,
            sender_name=_person_display_name(conn, person_id),
            quote=_situation_quote(sit),
            channel=repo.channel_for_identifier(thread_id or person_id),
            thread_id=thread_id or "",
        )
    except Exception:  # noqa: BLE001 - provenance never blocks reminder creation
        log.debug("reminder meta stamp failed (non-fatal)", exc_info=True)


def _relationship_reminder_sweep(conn: sqlite3.Connection, settings: Any) -> int:
    """Scan relationship_memory for open situations that are WAITING ON THE OWNER and have
    gone quiet past a relationship-aware threshold (4h for partner/family, 12h otherwise),
    and surface each as a deduplicated 'reminder' pending action so the user is proactively
    told instead of having to look. Returns the number of NEW reminders created.

    Deduplicated by idempotency_key (reminder:{person_id}:{situation_key}); an existing
    open reminder is never duplicated. Best-effort — never raises into the poller."""
    created = 0
    now = int(time.time())
    # control-state-presence-2 (defense-in-depth): run_sweep already returns early while
    # paused, so this sweep normally never runs during a pause. But this function is also
    # callable directly, and a paused agent must create ZERO reminder cards — otherwise the
    # whole backlog floods the owner on resume. Gate here too so the no-card-while-paused
    # guarantee holds no matter how the sweep is reached.
    try:
        if repo.is_paused(conn):
            log.info("paused — skipping relationship reminder sweep (no cards created)")
            return 0
    except Exception:  # noqa: BLE001 - unreadable pause state behaves as before
        pass
    try:
        rm_rows = conn.execute("SELECT * FROM relationship_memory").fetchall()
    except sqlite3.Error:
        return 0
    for row in rm_rows:
        try:
            person_id = row["person_id"]
            situations = json.loads(row["open_situations_json"] or "[]")
            if not isinstance(situations, list):
                continue
            rel_type = repo.person_relationship_type(conn, person_id)
            threshold_hours = 4 if rel_type in ("partner", "family") else 12
            threshold_ts = now - threshold_hours * 3600
            for sit in situations:
                if not isinstance(sit, dict):
                    continue
                if str(sit.get("status", "")).lower() == "resolved":
                    continue
                awaiting = str(sit.get("awaiting", "")).strip().lower()
                if awaiting not in _OWNER_AWAITING:
                    continue
                if int(sit.get("last_activity_ts") or now) > threshold_ts:
                    continue

                sit_key = str(sit.get("key", "") or "")
                idem_key = f"reminder:{person_id}:{sit_key}"
                existing = conn.execute(
                    "SELECT id FROM pending_actions WHERE idempotency_key=? "
                    "AND status NOT IN ('SENT','SKIPPED')",
                    (idem_key,),
                ).fetchone()
                if existing:
                    continue

                tier = 3 if rel_type in ("partner", "family") else 2
                situation_text = str(sit.get("situation", "") or "open situation")
                summary = f"Still waiting on your response: {situation_text}"
                thread_id = str(sit.get("thread_id", "") or "")
                aid = repo.create_pending(
                    conn, idempotency_key=idem_key, message_id=thread_id,
                    thread_id=thread_id, tier=tier, kind="reminder",
                    summary=summary, draft_text="",
                )
                if aid is not None:
                    created += 1
                    # ux-trust-4: stamp real provenance so this reminder is VERIFIABLE in the
                    # UI — a sender name (not "someone"), an actual quoted line (not empty),
                    # and the correct channel derived from the JID shape (a WhatsApp thread is
                    # never mislabeled "Email"). Without this, the card showed empty sender +
                    # "Other" + wrong channel + no quote — a maximally-urgent unverifiable nag.
                    _stamp_reminder_meta(conn, idem_key, person_id, sit, thread_id)
        except Exception:  # noqa: BLE001 - one bad record never blocks the rest
            log.debug("relationship reminder for a person failed (non-fatal)", exc_info=True)
    if created:
        log.info("relationship reminder sweep created %d reminder(s)", created)
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────
_STAMP_KEY = "last_proactive_sweep"


def _today(settings: Any) -> str:
    # control-state-presence-4: the once-a-day stamp must roll at the CONFIGURED-tz midnight
    # (same boundary as last_brief_* / last_commitment_check), not the system-tz midnight.
    return _now(settings).strftime("%Y-%m-%d")


def run_sweep(
    conn: sqlite3.Connection, settings: Any, notifier: Any, *, now: Optional[datetime] = None
) -> str:
    """Run the full proactive sweep ONCE per day and send a single digest.

    Gated by:
      * getattr(settings, 'proactive_enabled', True) — master switch (no-op when off).
      * getattr(settings, 'proactive_hour', 9)       — only fires at/after this local hour.
      * a kv stamp (`last_proactive_sweep` = today) — deduped, so a second call the same
        day sends nothing even if the poller wakes repeatedly.

    Returns the digest text that was sent (or "" if nothing was sent — disabled, wrong
    hour, already ran today, or no items). NEVER raises into the poller."""
    try:
        # ROOT CAUSE (control-state-presence-1): pause (agent OFF) only gated inbound
        # processing in main.poll_and_process; the proactive digest AND the relationship
        # reminder card sweep below still fired, so a "paused" agent kept pinging the
        # owner and creating pending reminder cards. When the owner turns the agent off
        # it must go fully quiet. Suppress the whole sweep while paused, and do NOT write
        # the daily stamp so a normal sweep still happens on the day they resume.
        try:
            if repo.is_paused(conn):
                log.info("paused — skipping proactive sweep")
                return ""
        except Exception:  # noqa: BLE001 - if pause state is unreadable, behave as before
            pass
        if not getattr(settings, "proactive_enabled", True):
            return ""
        # control-state-presence-4: when the caller does not inject `now` (the live poller
        # path), read it from the CONFIGURED timezone — exactly like main.py's other
        # scheduled jobs — so the 9am hour-gate fires at the owner's 9am, not the host's.
        # An explicit `now` (tests) is honored unchanged.
        now = now or _now(settings)
        if now.hour < int(getattr(settings, "proactive_hour", 9)):
            return ""
        today = now.strftime("%Y-%m-%d")
        if repo.kv_get(conn, _STAMP_KEY) == today:
            return ""

        # GAP 7: create proactive relationship reminders (pending cards) for situations
        # awaiting the owner that have gone quiet. Independent of the digest below — these
        # are actionable cards, not just a summary line.
        try:
            _relationship_reminder_sweep(conn, settings)
        except Exception:  # noqa: BLE001
            log.debug("relationship reminder sweep failed (non-fatal)", exc_info=True)

        items: list[dict] = []
        items.extend(unanswered_important(conn, settings))
        items.extend(open_decisions(conn, settings))   # every open ASK, importance-independent
        items.extend(at_risk_commitments(conn, settings))
        items.extend(stalled_conversations(conn, settings))
        items.extend(recurring_requests(conn))

        digest = build_digest(items)
        # Stamp BEFORE sending so a notifier hiccup can't cause a re-send storm on the
        # next poll. Once a day is the contract; a dropped digest waits for tomorrow.
        repo.kv_set(conn, _STAMP_KEY, today)
        if digest:
            notifier.send_text(digest)
            log.info("proactive sweep sent (%d items)", len(items))
        else:
            log.info("proactive sweep: nothing to surface")
        return digest
    except Exception:  # noqa: BLE001 - never raise into the poller
        log.warning("run_sweep failed (non-fatal)", exc_info=True)
        return ""
