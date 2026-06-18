"""Read-only aggregations for the web console, all in PLAIN ENGLISH.

This is the read seam: the console never sees a tier number, a category code, or
raw JSON — every value is translated here, server-side. No writes happen in this
module (writes go through the existing guarded repository/dispatcher functions).

Stdlib only — testable against an in-memory DB without FastAPI.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any, Optional

from assistant.storage import decision_log
from assistant.storage import repositories as repo

# Plain-English labels — the ONLY four statuses the queue/detail ever show.
TIER_LABEL = {
    0: "Filed away quietly",
    1: "Told you, handled",
    2: "Drafting a reply for you",
    3: "Needs your decision",
}

CATEGORY_LABEL = {
    "spam_promotional": "Promotion / spam",
    "newsletter": "Newsletter",
    "automated_notification": "Automated notification",
    "transactional_receipt": "Receipt / confirmation",
    "social": "Social update",
    "personal": "Personal message",
    "work_request": "Work request",
    "scheduling": "Scheduling",
    "financial": "Money matter",
    "legal": "Legal matter",
    "investor": "Investor",
    "other": "Other",
    "unknown": "Unclear",
}

URGENCY_LABEL = {"low": "Low priority", "medium": "Worth noting", "high": "Urgent"}
UNDO_LABEL = {
    "reversible": "Easily undone",
    "hard_to_reverse": "Hard to undo",
    "irreversible": "Can't be undone",
}

# Feedback dropdown — plain English → the tier the user says was correct.
FEEDBACK_OPTIONS = [
    {"label": "Filed quietly — correct", "tier": 0},
    {"label": "Should have filed quietly", "tier": 0},
    {"label": "Should have told me, no reply needed", "tier": 1},
    {"label": "Should have drafted a reply", "tier": 2},
    {"label": "Should have flagged for my decision", "tier": 3},
]

PIPELINE_STAGES = [
    "New message arrived",
    "Reading the whole thread",
    "AI is thinking",
    "Safety check",
    "Action taken",
]

CHANNEL_LABEL = {"gmail": "Email", "whatsapp": "WhatsApp"}
CHANNEL_ICON = {"gmail": "📧", "whatsapp": "💬"}


def _channel(message_id: str) -> str:
    return "whatsapp" if (message_id or "").startswith("wa_") else "gmail"


def _wa_phone_number(conn: sqlite3.Connection, message_id: str) -> Optional[str]:
    """Relay-resolved real phone for a WhatsApp message ('+91…'), or None. Best-effort:
    the whatsapp_inbox table may not exist (email-only / fresh DB), so never raise."""
    try:
        row = conn.execute(
            "SELECT phone_number FROM whatsapp_inbox WHERE message_id=? LIMIT 1",
            (message_id,),
        ).fetchone()
        return row["phone_number"] if row and row["phone_number"] else None
    except sqlite3.Error:
        return None


def source_link(message_id: str, thread_id: str, channel: str,
                sender_email: str = "", phone_number: str = "") -> dict:
    """Backtrack link to the ORIGINAL conversation — Gmail thread (exact) or WhatsApp chat.
    Returns {"url": str, "label": str}; url is "" when we can't build one (a LID WhatsApp
    contact whose real number we don't have → the card shows "Save contact to open chat").

      * Email   → https://mail.google.com/mail/u/0/#all/<thread_id>     (the exact thread)
      * WhatsApp w/ number → whatsapp://send?phone=<digits>            (native app, that chat)

    NOTE: WhatsApp exposes NO per-message deep link — the best any app can do is open the
    CHAT (at its latest message). The Gmail link opens the precise thread."""
    if channel == "gmail":
        tid = (thread_id or message_id or "").strip()
        if tid:
            # Pin the link to the CONNECTED Gmail account by EMAIL (not the "u/0" default-account
            # index), so it opens the right account + exact thread even when other Google accounts
            # are logged in and one of them is the browser default. Falls back to u/0 if unknown.
            acct = (os.environ.get("GMAIL_ADDRESS", "") or "").strip()
            u = acct if acct else "0"
            return {"url": f"https://mail.google.com/mail/u/{u}/#all/{tid}", "label": "Open in Gmail"}
        return {"url": "", "label": ""}
    # WhatsApp: prefer a resolved phone number, else extract from a phone JID.
    digits = ""
    if phone_number:
        digits = re.sub(r"\D", "", phone_number)
    if not digits:
        local = (sender_email or "").split("@")[0]
        suffix = (sender_email or "").split("@")[-1] if "@" in (sender_email or "") else ""
        if suffix == "s.whatsapp.net" and local.isdigit():
            digits = local
    if digits:
        # Native scheme → opens the WhatsApp desktop app directly in that chat.
        return {"url": f"whatsapp://send?phone={digits}", "label": "Open in WhatsApp"}
    # LID-only contact — no number to open a chat with; let the card prompt a Save instead.
    return {"url": "", "label": ""}


def _wa_number_for(conn: sqlite3.Connection, identifier: str, message_id: str = "") -> str:
    """Best phone number (digits) for a WhatsApp identifier, so we can open its chat.
    Tries: a phone JID directly -> the person's linked phone JID (address-book/saved) ->
    the relay-resolved number. Returns '' for an unresolved @lid (no chat link possible)."""
    ident = (identifier or "").strip().lower()
    local = ident.split("@")[0]
    if ident.endswith("@s.whatsapp.net") and local.isdigit():
        return local
    try:
        pid = repo.person_link_get(conn, ident)
        if pid:
            prow = repo.person_get(conn, pid)
            for j in json.loads((prow["phone_jids"] if prow else "[]") or "[]"):
                jl = (j or "").lower()
                if jl.endswith("@s.whatsapp.net") and jl.split("@")[0].isdigit():
                    return jl.split("@")[0]
    except Exception:  # noqa: BLE001
        pass
    if message_id:
        ph = _wa_phone_number(conn, message_id)
        if ph:
            return re.sub(r"\D", "", ph)
    return ""


def _is_meaningful_name(name: str) -> bool:
    """True if name has at least 2 alphanumeric chars — filters out '..' etc."""
    return len(re.sub(r"[^a-zA-Z0-9]", "", name or "")) >= 2


def _wa_phone(jid: str) -> str:
    """Extract a human-readable phone / group label from a WhatsApp JID.
    Examples: '919164536565@s.whatsapp.net' → '+919164536565'
              '120363286054478624@g.us' → 'group'
              '120363407466491307@newsletter' → 'newsletter'
              '9136083337274@lid' → '' (LID is an internal privacy ID, NOT the real phone)"""
    local = (jid or "").split("@")[0]
    suffix = (jid or "").split("@")[-1] if "@" in (jid or "") else ""
    if suffix == "newsletter":
        return "newsletter"
    if suffix in ("g.us", "s.whatsapp.net"):
        return "group"
    if suffix == "lid":
        # WhatsApp LID (Linked ID) is a privacy-preserving internal identifier —
        # NOT the contact's real phone number. Never display it as one.
        return ""
    if local.isdigit():
        return f"+{local}"
    return local or jid


def _live_name(conn: sqlite3.Connection, identifier: str) -> tuple[str, bool]:
    """Single source of truth: identifier → (live_display_name, is_saved).

    Resolve the identifier (an @lid / @s.whatsapp.net / email) to its cross-channel PERSON
    and prefer the owner-asserted name, so one save propagates to every surface (home cards,
    People, history) on the next render — no re-ingest. Returns ('', False) when there is no
    saved/trusted name, so callers keep their existing push-name→number fallback UNCHANGED
    (the unsaved/unknown path is never altered)."""
    ident = (identifier or "").strip().lower()
    if not ident:
        return "", False
    dn = ""
    try:
        pid = repo.person_link_get(conn, ident)
        if pid:
            prow = repo.person_get(conn, pid)
            dn = ((prow["display_name"] if prow else "") or "").strip()
            if repo.person_is_saved(conn, pid) and dn:
                return dn, True   # owner-saved person: their name wins over any push-name
        # A trusted contacts.name (manual/saved/business) even without a person row.
        r = conn.execute(
            "SELECT name, COALESCE(name_source,'') FROM contacts WHERE email=?", (ident,)
        ).fetchone()
        if r and (r[0] or "").strip() and r[1] in ("saved", "business", "manual"):
            return r[0].strip(), True
        if pid and repo.person_is_saved(conn, pid):
            return dn, True       # saved person, empty display_name → still saved
    except sqlite3.Error:
        pass
    return "", False


def _display_name(conn: sqlite3.Connection, sender_name: str, sender_email: str,
                  channel: str, phone_number: str | None = None) -> str:
    """Best human-readable display name. Prefers the live saved/person name (so a saved
    contact's name shows everywhere the moment it's saved), then falls back to the push-name,
    then the phone number for WhatsApp contacts whose push_name is blank/meaningless.
    `phone_number` is the relay-resolved real phone ('+919164536565') for LID contacts."""
    # Live person/saved name wins — BEFORE the meaningful-push-name check, so a saved
    # "Aastik Nayyar" beats the push-name "Aastik". Only fires for saved/trusted identities;
    # unsaved senders fall straight through to the original fallback below.
    live, _ = _live_name(conn, sender_email)
    if live:
        return live
    if _is_meaningful_name(sender_name):
        return sender_name
    if channel == "whatsapp":
        # Use relay-resolved real phone number if available (covers LID contacts)
        if phone_number:
            return phone_number
        phone = _wa_phone(sender_email or "")
        if phone:
            return phone
        # @lid JID — WhatsApp privacy ID, real number not yet resolved.
        suffix = (sender_email or "").split("@")[-1] if "@" in (sender_email or "") else ""
        if suffix == "lid":
            return "Unknown WA contact (number not yet resolved)"
        return sender_email or "Unknown"
    return sender_email or "Unknown sender"


def _contact_info(conn: sqlite3.Connection, sender_email: str) -> dict:
    """Lookup contact record; returns is_saved flag and relationship note."""
    try:
        row = conn.execute(
            "SELECT importance, relationship, flags, reply_rate, notes, "
            "       COALESCE(name_source,'') FROM contacts WHERE email=?",
            (sender_email or "",),
        ).fetchone()
        if row is None:
            # No per-identifier row, but it may resolve to a SAVED person (e.g. a phone JID
            # with only the person_links bridge). Recognize via the person.
            try:
                _pid = repo.person_link_get(conn, sender_email or "")
                if _pid and repo.person_is_saved(conn, _pid):
                    return {"is_saved": True, "note": "Saved contact", "is_wa_contact": False}
            except sqlite3.Error:
                pass
            return {"is_saved": False, "note": ""}
        importance = int(row[0] or 0)
        relationship = (row[1] or "").strip()
        flags = row[2]
        reply_rate = float(row[3] or 0)
        notes = (row[4] or "").strip()
        name_source = (row[5] or "").strip()
        # name_source provenance: 'saved'/'business'/'manual' = a trustworthy, owner- or
        # platform-verified name → always recognized (the @lid "unknown" bug fix).
        is_trusted_name = name_source in ("saved", "business", "manual")
        # wa_contact = seen on WhatsApp (anyone who messaged); phone_contact = matched from
        # macOS Contacts. Only phone_contact or a user-set relationship counts as truly "saved."
        is_wa_contact = relationship == "wa_contact"
        is_phone_contact = relationship == "phone_contact"
        is_saved = bool(is_trusted_name or is_phone_contact or (not is_wa_contact and relationship)
                        or flags or (importance > 10) or (reply_rate > 0) or notes)
        if is_wa_contact and importance > 10:
            is_saved = True  # user explicitly boosted importance → treat as saved
        # Person-level recognition: if this identifier resolves to a SAVED person, it's
        # recognized even if THIS row lacks the manual/phone_contact stamp. This is what
        # lets a future inbound by a bare phone number be recognized via the person (so the
        # separate @s.whatsapp.net contacts row is no longer needed).
        if not is_saved:
            try:
                _pid = repo.person_link_get(conn, sender_email or "")
                if _pid and repo.person_is_saved(conn, _pid):
                    is_saved = True
            except sqlite3.Error:
                pass
        # Human-readable note
        if relationship and relationship not in ("wa_contact", "phone_contact"):
            note = relationship
        elif is_phone_contact:
            note = "Phone contact"
        elif is_trusted_name:
            note = "Saved contact"
        elif flags:
            note = ", ".join(sorted(f for f in (flags or []) if f))
        elif importance > 10:
            note = f"importance {importance}"
        else:
            note = ""
        if is_saved and not note:
            note = "Saved contact"
        return {"is_saved": is_saved, "note": note, "is_wa_contact": is_wa_contact}
    except Exception:  # noqa: BLE001
        return {"is_saved": False, "note": ""}


def _tier_label(tier: Optional[int]) -> str:
    if tier is None:
        return "Working on it"
    return TIER_LABEL.get(int(tier), "Working on it")


def _confidence_phrase(conf: Optional[float]) -> str:
    if conf is None:
        return "unsure"
    return f"{round(float(conf) * 100)}% sure"


def _placeholders(text: str) -> list[str]:
    return re.findall(r"\[([^\]]+)\]", text or "")


# ─────────────────────────────────────────────────────────────────────────────
# Performance indexes (scaling-time-1, scaling-time-3)
# ─────────────────────────────────────────────────────────────────────────────
# ROOT CAUSE (scaling-time-1): _latest_pending and _is_folded_child run
# `WHERE message_id=?` against pending_actions, which has NO index on message_id
# (db.py only indexes status / telegram_message_id / criticality / batch_id). get_queue
# does up to ``limit`` of these lookups per render, so every dashboard queue render was N
# full table scans of an ever-growing, never-pruned table — latency that climbs for the
# product's whole life.
#
# ROOT CAUSE (scaling-time-3): learning_summary and metrics_accuracy filter
# learning_events by ``ts`` (and the latter additionally by ``type``), but learning_events
# has NO ts index. /api/learning is uncached, so every analytics load full-scanned the
# highest-churn "kept forever" table in the schema.
#
# We cannot touch the shared migrations.py/db.py from this cluster, so we create the
# indexes idempotently here via CREATE INDEX IF NOT EXISTS, guarded so a hiccup never
# breaks a read. The integrator should also add these to migrations.py (see
# schema_or_config_needed) for the canonical wiring; until then this self-heals on first
# read. We remember success in a process-local flag so the DDL runs at most once per
# connection-process, not on every query.
_PERF_INDEXES = (
    # scaling-time-1: turns _latest_pending / _is_folded_child into index lookups.
    "CREATE INDEX IF NOT EXISTS idx_pa_message_id ON pending_actions(message_id)",
    # scaling-time-3: composite covers both the ts-only (learning_summary) and the
    # ts+type (metrics_accuracy / metrics_costs) filters with one index.
    "CREATE INDEX IF NOT EXISTS idx_learning_events_ts_type ON learning_events(ts, type)",
)

_perf_indexes_ready = False


def ensure_perf_indexes(conn: sqlite3.Connection) -> None:
    """Idempotently create the read-path performance indexes (scaling-time-1/3).

    Best-effort: a failure (e.g. a partial/legacy DB missing a table) is swallowed so a
    read never 500s. Runs the DDL once per process; CREATE INDEX IF NOT EXISTS is itself a
    no-op when the index already exists, so re-running is cheap and safe."""
    global _perf_indexes_ready
    if _perf_indexes_ready:
        return
    ok = True
    for ddl in _PERF_INDEXES:
        try:
            conn.execute(ddl)
        except sqlite3.Error:
            # Table may not exist yet on a fresh/partial DB; leave the flag unset so a
            # later call retries once the table is present.
            ok = False
    if ok:
        _perf_indexes_ready = True


def _latest_pending(conn: sqlite3.Connection, message_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pending_actions WHERE message_id=? ORDER BY id DESC LIMIT 1",
        (message_id,),
    ).fetchone()


# ─────────────────────────────────────────────────────────────────────────────
# Header / status
# ─────────────────────────────────────────────────────────────────────────────
def get_status(conn: sqlite3.Connection, settings) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "app": "Steward",
        "live": not settings.dry_run,
        "mode_label": "LIVE" if not settings.dry_run else "DRY-RUN",
        "gmail_address": settings.gmail_address,
        "paused": repo.is_paused(conn),
        "checks_every_seconds": settings.poll_interval_seconds,
        "whatsapp_enabled": getattr(settings, "whatsapp_enabled", False),
        "connections": _connection_health(settings),
    }


def _connection_health(settings) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Cheap, read-only signals of what's wired up — drives the dashboard's
    'what's connected' strip so a new user instantly sees the system is live.
    Never raises; a missing signal just reads as not-connected."""
    import json as _json
    from pathlib import Path as _Path

    # Gmail: the OAuth token file exists (created after the user signs in once).
    gmail_ok = False
    try:
        gmail_ok = bool(settings.gmail_address) and _Path(settings.gmail_token_path).exists()
    except Exception:  # noqa: BLE001
        pass

    # Telegram: both the bot token and the owner's chat id are configured.
    telegram_ok = bool(getattr(settings, "telegram_bot_token", "")) and bool(
        getattr(settings, "telegram_chat_id", "")
    )

    # WhatsApp: enabled AND the relay reports a live connection.
    wa_enabled = bool(getattr(settings, "whatsapp_enabled", False))
    wa_connected = False
    if wa_enabled:
        try:
            data = _json.loads(_Path("relay/status.json").read_text(encoding="utf-8"))
            wa_connected = bool(data.get("connected"))
        except Exception:  # noqa: BLE001
            wa_connected = False

    return {
        "gmail": {"connected": gmail_ok, "label": settings.gmail_address or "Not connected"},
        "telegram": {"connected": telegram_ok, "label": "Connected" if telegram_ok else "Not connected"},
        "whatsapp": {
            "enabled": wa_enabled,
            "connected": wa_connected,
            "label": ("Connected" if wa_connected else "Connecting…") if wa_enabled else "Off",
        },
    }


def get_wastatus(settings) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Relay health for the WhatsApp tab (reads relay/status.json the Node relay writes)."""
    if not getattr(settings, "whatsapp_enabled", False):
        return {"enabled": False}
    import json
    from pathlib import Path

    try:
        data = json.loads(Path("relay/status.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - relay not running yet / no file
        return {"enabled": True, "running": False}
    return {
        "enabled": True,
        "running": True,
        "connected": bool(data.get("connected")),
        "session_age_seconds": data.get("session_age_seconds"),
        "messages_today": data.get("messages_today", 0),
        "last_message_ts": data.get("last_message_ts"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stat cards
# ─────────────────────────────────────────────────────────────────────────────
def get_stats(conn: sqlite3.Connection) -> dict[str, int]:
    s = decision_log.stats(conn)
    pending = [r for r in repo.open_pending(conn) if r["kind"] == "reply_draft"]

    def _count(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()["n"])
        except Exception:  # noqa: BLE001 - a dashboard count must never 500 the page
            return 0

    # Replies actually sent (audit_log records every send; dry-run sends are logged too).
    sent = _count("SELECT COUNT(*) AS n FROM audit_log WHERE kind='send'")
    # Distinct conversations the brain has handled (auto-filed + surfaced).
    conversations = _count(
        "SELECT COUNT(DISTINCT thread_id) AS n FROM decision_log "
        "WHERE thread_id IS NOT NULL AND thread_id != ''"
    )
    return {
        "handled_quietly": s["handled_quietly"],
        "flagged_for_you": s["flagged_for_you"],
        "replies_waiting": len(pending),
        "near_misses": s["near_misses"],
        "sent": sent,
        "conversations": conversations,
    }


def get_notifications(conn: sqlite3.Connection, *, limit: int = 30, window_days: int = 7) -> dict[str, Any]:
    """Recent activity the operator hasn't cleared yet. NON-DESTRUCTIVE: 'clear' only
    stamps a KV cursor (notifications_cleared_ts); nothing in audit_log is deleted.
    Returns {items, count, cleared_at}."""
    try:
        cleared = int(repo.kv_get(conn, "notifications_cleared_ts") or 0)
    except (TypeError, ValueError):
        cleared = 0
    # cleared + 1: the cursor is EXCLUSIVE (the feed query is ts >= since), so anything up
    # to and including the moment of clearing counts as already-seen.
    since = max(repo.now_epoch() - window_days * 86400, cleared + 1 if cleared else 0)
    items = list_audit(conn, since)[:limit]
    return {"items": items, "count": len(items), "cleared_at": cleared}


def clear_notifications(conn: sqlite3.Connection) -> dict[str, Any]:
    """Mark everything up to now as seen. Reversible/forgiving: only moves a cursor, so
    the full history stays in /api/audit and the audit log is never touched."""
    ts = repo.now_epoch()
    repo.kv_set(conn, "notifications_cleared_ts", str(ts))
    return {"ok": True, "cleared_at": ts}


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline strip (last activity per the 5 stages)
# ─────────────────────────────────────────────────────────────────────────────
def get_pipeline(conn: sqlite3.Connection) -> dict[str, Any]:
    in_flight = conn.execute(
        "SELECT COUNT(*) AS n FROM processed_messages WHERE state='PROCESSING'"
    ).fetchone()["n"]
    latest = decision_log.recent(conn, limit=1)
    last = None
    if latest:
        d = latest[0]
        last = {
            "who": (_live_name(conn, d["sender_email"] or "")[0]
                    or d["sender_name"] or d["sender_email"] or "someone"),
            "subject": d["subject"] or "(no subject)",
            "label": _tier_label(d["final_tier"]),
            "at": d["ts"],
        }
    return {
        "stages": PIPELINE_STAGES,
        "in_flight": in_flight,
        "busy": in_flight > 0,
        "last": last,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Live queue
# ─────────────────────────────────────────────────────────────────────────────
# SEND_STUCK (crash mid-send) and SEND_AMBIGUOUS (maybe-delivered, never auto-resent)
# are terminal — handled out-of-band, never shown as an open actionable queue item.
# SUPERSEDED (collapsed into the living conversation card) and HANDLED_ELSEWHERE (owner
# replied on another device) are terminal — resolved, never shown as an open actionable item.
_TERMINAL_STATUSES = {"SENT", "SKIPPED", "EXPIRED", "SEND_FAILED", "SEND_STUCK", "SEND_AMBIGUOUS",
                      "SUPERSEDED", "HANDLED_ELSEWHERE"}
# SEND_BLOCKED is a send refused by the approval-integrity guard (WYSIWYG mismatch /
# unresolved placeholder): the item still NEEDS the owner (to Edit + re-approve), so it
# surfaces as WAITING, never "auto-handled".
_PENDING_STATUSES  = {"PENDING", "APPROVED", "EDITING", "EDITED", "SENDING", "SEND_BLOCKED"}
# Kinds that represent a reply we'd SEND. By the no-auto-send invariant, a SENT reply
# was ALWAYS approved by a human (the engine never sends one on its own) — so it must
# never be attributed to "auto", even if we failed to record the surface (web/telegram).
_REPLY_KINDS = {"reply_draft", "ask", "compose", "compose_reply"}

_VIA_LABEL = {
    "telegram": "via Telegram",
    "web":      "via App",
    "auto":     "auto-handled",
    "direct":   "replied directly",
    "human":    "you handled it",
}


def _is_folded_child(conn: sqlite3.Connection, message_id: str) -> bool:
    """True if this message was FOLDED into another sender-burst card (GAP 2) and has no
    pending action of its own. Such a message is represented by its parent card (which
    carries message_count), so it must NOT appear as its own queue row — otherwise it
    shows up as a phantom 'auto-handled' item.

    scaling-time-2: this is an O(1) indexed point lookup on folded_children
    (child_message_id PRIMARY KEY), populated when fold_message_into_action runs. It
    replaces the previous unindexed leading-wildcard ``LIKE '%"id"%'`` full-scan of the
    never-pruned pending_actions table, which ran once per non-pending queue row and got
    linearly slower for the app's lifetime. The legacy JSON-scan is kept ONLY as a fallback
    for rows folded before the table existed (or a DB without it), so behavior is identical
    on legacy data while new folds avoid the full-scan entirely."""
    if not message_id:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM folded_children WHERE child_message_id=? LIMIT 1",
            (message_id,),
        ).fetchone()
        if row is not None:
            return True
    except sqlite3.OperationalError:
        # folded_children not present (un-migrated DB) — fall through to the legacy scan.
        pass
    # Fallback for legacy rows whose fold predates the indexed table. Anchored to the
    # quoted id to avoid substring collisions, exactly as before.
    row = conn.execute(
        "SELECT 1 FROM pending_actions "
        "WHERE folded_message_ids LIKE '%\"' || ? || '\"%' AND message_id != ? LIMIT 1",
        (message_id, message_id),
    ).fetchone()
    return row is not None


def _attribute(status, kind, raw_via, final_tier=0):
    """Resolve (via, is_handled) for a queue item, honoring the no-auto-send invariant.

    * No pending action at all:
        - tier ≥ 2 (APPROVE/ASK) → it NEEDED you but no card exists (creation failed /
          stranded). Show it as WAITING, never "auto-handled" — that mislabel would
          falsely claim it was taken care of.
        - tier 0/1 → the brain filed it silently: genuinely "auto".
    * Still open (PENDING-ish)   → waiting, not handled.
    * Terminal:
        - recorded via wins (telegram / web / direct);
        - a SENT reply with no recorded via → "human" (you approved it — never "auto");
        - a SKIPPED item → "human" (skipping is always your call);
        - anything else terminal with no via (e.g. a silent filing) → "auto".
    """
    if status is None:
        if (final_tier or 0) >= 2:
            return None, False   # needed you, no card → surface as waiting, not "auto"
        return "auto", True
    if status in _PENDING_STATUSES:
        return None, False
    # terminal
    if raw_via:
        return raw_via, True
    if status == "SENT" and kind in _REPLY_KINDS:
        return "human", True
    if status == "SKIPPED":
        return "human", True
    return "auto", True


def get_queue(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    # scaling-time-1: ensure the message_id index exists so the per-row _latest_pending /
    # _is_folded_child lookups below are index probes, not full scans.
    ensure_perf_indexes(conn)
    waiting: list[dict[str, Any]] = []
    handled: list[dict[str, Any]] = []

    for d in decision_log.recent(conn, limit=limit):
        pend = _latest_pending(conn, d["message_id"])
        # A message folded into another sender-burst card is represented by that parent
        # (which carries message_count) — don't surface it as its own phantom row.
        if pend is None and _is_folded_child(conn, d["message_id"]):
            continue
        ch = _channel(d["message_id"])
        status = pend["status"] if pend else None
        kind = pend["kind"] if pend else None
        raw_via = (pend["response_via"] if pend and "response_via" in pend.keys() else None) or None
        decided_at = (pend["decided_at"] if pend and "decided_at" in pend.keys() else None)

        # Honor the no-auto-send invariant when attributing how the item was handled.
        via, is_handled = _attribute(status, kind, raw_via, d["final_tier"])

        # Pull relay-resolved phone number for LID contacts (NULL for regular phone JIDs)
        _phone_number = _wa_phone_number(conn, d["message_id"]) if ch == "whatsapp" else None
        sender_display = _display_name(conn, d["sender_name"] or "", d["sender_email"] or "", ch,
                                       phone_number=_phone_number)
        contact_info = _contact_info(conn, d["sender_email"] or "")
        item = {
            "message_id": d["message_id"],
            "channel": ch,
            "channel_label": CHANNEL_LABEL[ch],
            "channel_icon": CHANNEL_ICON[ch],
            "sender": sender_display,
            # Raw identifier (the @lid/jid or email) — lets the Mac app "Save contact" an
            # unknown sender straight from the card. Distinct from `sender` (display name).
            "sender_identifier": d["sender_email"] or "",
            "is_saved": contact_info["is_saved"],
            "is_wa_contact": contact_info.get("is_wa_contact", False),
            "contact_note": contact_info["note"],
            "subject": d["subject"] or "",
            "label": _tier_label(d["final_tier"]),
            "at": d["ts"],
            "has_draft": bool(pend and pend["kind"] == "reply_draft"),
            "action_id": pend["id"] if pend else None,
            "action_status": status,
            "message_count": _pending_message_count(pend),
            "response_via": via if is_handled else None,
            "via_label": _VIA_LABEL.get(via or "", "") if is_handled else "",
            "decided_at": decided_at,
        }

        if is_handled:
            handled.append(item)
        else:
            waiting.append(item)

    return waiting + handled


def rank_open_decisions(rows: list) -> list:
    """Order open pending decisions by URGENCY, most important first.

    ROOT CAUSE (ux-trust-1): the menu-bar popover headlined `decisions.first`, and the
    decisions list is built from repo.open_pending which orders by created_at ASC — i.e.
    the OLDEST item. A tier-3 (ASK / "needs you soon") that arrived later therefore got
    buried beneath an older tier-2, so the one thing the popover told you to handle was
    routinely NOT the most urgent. This ranks by tier DESC (3 before 2 before lower),
    then by recency (newest first) as the tie-breaker, so the surfaced "top" item is the
    most consequential one. Stable and pure: it sorts whatever pending rows it is given
    (sqlite3.Row or dict) and never queries — callers decide the input set.

    The id falls back to 0 and is used only as a final deterministic tie-break so the
    order is stable across calls even when tier and timestamp are equal.
    """
    def _get(r: Any, key: str, default: Any) -> Any:
        try:
            if hasattr(r, "keys") and key in r.keys():
                return r[key]
        except Exception:  # noqa: BLE001 - row may be a plain dict
            pass
        try:
            return r.get(key, default)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return default

    def _int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _key(r: Any) -> tuple[int, int, int, int]:
        tier = _int(_get(r, "tier", 0), 0)
        # Sender importance (0..100) bucketed COARSELY (0..4) so it only breaks ties WITHIN a
        # tier — it can never override the tier itself, so the cardinal floors (needs-attention,
        # VIP, money/legal) still decide the band. Within a pile of tier-3s the investor /
        # co-founder (high importance) now headlines above a cold stranger. Default 0 when the
        # caller didn't enrich the row (e.g. the pure-row tests), so behavior is unchanged.
        bucket = min(4, max(0, _int(_get(r, "importance", 0), 0) // 25))
        created = _int(_get(r, "created_at", 0), 0)
        rid = _int(_get(r, "id", 0), 0)
        # Negated so Python's ascending sort puts the highest tier / most-important / newest /
        # largest-id FIRST. importance breaks ties within a tier; created_at within importance.
        return (-tier, -bucket, -created, -rid)

    return sorted(list(rows or []), key=_key)


def _sender_importance(conn: sqlite3.Connection, message_id: str) -> int:
    """The sender's learned importance (0..100) for a pending item — for queue ranking.
    Resolves message_id → decision_log sender → contacts.importance. 0 when unknown."""
    try:
        from assistant.storage import decision_log
        d = decision_log.get(conn, message_id)
        email = ((d["sender_email"] if d else "") or "")
        if not email:
            return 0
        row = conn.execute("SELECT importance FROM contacts WHERE email=?", (email,)).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def top_open_decision(conn: sqlite3.Connection):
    """The single most urgent open decision (highest tier, then most recent), or None.

    This is the "one thing to handle first" the popover/headline should surface. Built on
    the guarded repo.open_pending read seam plus rank_open_decisions (ux-trust-1). Each row is
    enriched with the sender's learned importance so a high-importance person headlines above a
    cold stranger within the same tier."""
    rows = [{**dict(r), "importance": _sender_importance(conn, r["message_id"])}
            for r in repo.open_pending(conn)]
    ranked = rank_open_decisions(rows)
    return ranked[0] if ranked else None


def get_queue_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts for the summary bar: waiting + breakdown of how handled items were actioned."""
    today_start = int(conn.execute(
        "SELECT strftime('%s', date('now'))"
    ).fetchone()[0])

    rows = conn.execute(
        "SELECT status, response_via, kind, COUNT(*) as cnt FROM pending_actions "
        "WHERE decided_at >= ? OR status IN ('PENDING','APPROVED','EDITING','EDITED','SENDING') "
        "GROUP BY status, response_via, kind",
        (today_start,),
    ).fetchall()

    waiting = 0
    by_via: dict[str, int] = {}
    handled_total = 0

    for r in rows:
        st  = r[0]
        via = r[1]
        kind = r[2]
        cnt = r[3]
        if st in _PENDING_STATUSES:
            waiting += cnt
        elif st in _TERMINAL_STATUSES:
            # Same no-auto-send attribution as the queue: a sent reply / skip is yours.
            key, _ = _attribute(st, kind, via)
            key = key or "auto"
            by_via[key] = by_via.get(key, 0) + cnt
            handled_total += cnt

    return {
        "waiting": waiting,
        "handled_today": handled_total,
        "by_via": by_via,
    }


def _pending_message_count(pend) -> int:
    """message_count off a pending row, defaulting to 1 (legacy rows / no pending)."""
    if pend is None:
        return 1
    try:
        if "message_count" in pend.keys():
            return int(pend["message_count"] or 1)
    except Exception:  # noqa: BLE001 - row may be a plain dict
        try:
            return int(pend.get("message_count", 1) or 1)
        except Exception:  # noqa: BLE001
            return 1
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# Detail pane for one email
# ─────────────────────────────────────────────────────────────────────────────
def get_email(conn: sqlite3.Connection, message_id: str) -> Optional[dict[str, Any]]:
    d = decision_log.get(conn, message_id)
    if d is None:
        return None
    pend = _latest_pending(conn, message_id)

    ch = _channel(message_id)
    sender_email = d["sender_email"] or ""
    raw_name = d["sender_name"] or ""
    # Pull relay-resolved phone number for LID contacts
    _phone_number2 = _wa_phone_number(conn, message_id) if ch == "whatsapp" else None
    display = _display_name(conn, raw_name, sender_email, ch, phone_number=_phone_number2)
    contact_info = _contact_info(conn, sender_email)
    detail: dict[str, Any] = {
        "message_id": message_id,
        "channel": ch,
        "channel_label": CHANNEL_LABEL[ch],
        "channel_icon": CHANNEL_ICON[ch],
        "label": _tier_label(d["final_tier"]),
        "source_link": source_link(
            message_id, d["thread_id"], ch, sender_email,
            _wa_number_for(conn, sender_email, message_id) if ch == "whatsapp" else ""),
        "arrived": {
            "from": display,
            "from_raw": raw_name,
            "from_email": sender_email,
            "phone_number": _phone_number2,
            "subject": d["subject"] or "",
            "at": d["ts"],
            "quote": d["snippet"] or "",
            "is_saved": contact_info["is_saved"],
            "is_wa_contact": contact_info.get("is_wa_contact", False),
            "contact_note": contact_info["note"],
        },
        "ai": {
            "who_is_sender": CATEGORY_LABEL.get(d["category"], "Other"),
            "urgency": URGENCY_LABEL.get(d["stakes"], "Worth noting"),
            "undo": UNDO_LABEL.get(d["reversibility"], "Easily undone"),
            "confidence": _confidence_phrase(d["confidence"]),
            "why": d["reasoning"] or d["surfaced_reason"] or "No explanation recorded.",
            "needs_reply": bool(d["needs_reply"]),
        },
        "draft": None,
        "feedback": {"options": FEEDBACK_OPTIONS, "current": _tier_label(d["final_tier"])},
    }

    # Tier-2 AND tier-3 cards carry a pre-generated draft now (P0b).
    if pend is not None and pend["kind"] in ("reply_draft", "ask"):
        text = pend["draft_text"] or ""
        detail["draft"] = {
            "action_id": pend["id"],
            "text": text,
            "placeholders": _placeholders(text),
            "status": pend["status"],
            "decided": pend["status"] not in ("PENDING", "APPROVED", "EDITED"),
        }
    return detail


def get_pipeline_detail(conn: sqlite3.Connection, message_id: str) -> Optional[dict[str, Any]]:
    """Full per-message pipeline view (View 3): the email detail plus the three
    reasoning steps, whether it took the critical path, and the quality-gate result."""
    base = get_email(conn, message_id)
    if base is None:
        return None
    import json

    d = decision_log.get(conn, message_id)
    keys = set(d.keys()) if d is not None else set()

    def _col(name, default=None):
        return d[name] if (d is not None and name in keys) else default

    def _loads(s):
        if not s:
            return None
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            return s

    base["reasoning"] = {
        "think": _loads(_col("think_output")),
        "judge": _loads(_col("judge_output")),
        "critique": _loads(_col("critique_output")),
        "critique_adjustment": _col("critique_adjustment", 0) or 0,
        "was_critical": bool(_col("was_critical", 0)),
        "quality_gate": _loads(_col("quality_gate_result")),
    }
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Footer tabs: people, rules, today's audit
# ─────────────────────────────────────────────────────────────────────────────
def _handle_kind(identifier: str) -> str:
    e = (identifier or "").lower()
    if e.endswith("@lid"):
        return "whatsapp"
    if e.endswith("@s.whatsapp.net") or e.endswith("@g.us"):
        return "phone"
    if "@" in e:
        return "email"
    return "other"


def _handle_label(conn: sqlite3.Connection, identifier: str) -> str:
    """Human-readable label for one of a person's identifiers (the number, the email, etc.)."""
    e = identifier or ""
    kind = _handle_kind(e)
    if kind == "email":
        return e
    if kind == "phone":
        digits = "".join(c for c in e.split("@")[0] if c.isdigit())
        return f"+{digits}" if digits else e
    if kind == "whatsapp":
        ph = _wa_phone(e)
        return ph or "WhatsApp"
    return e


def list_contacts(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """One row per PERSON (not per identifier): a saved person's @lid + phone number + email
    collapse into a single entry with the saved name and their identifiers grouped under
    `handles`. Contacts with no linked person stay as their own single-handle entry, so the
    unsaved/unknown path is unchanged. Saving a name propagates here live (name resolves from
    the person), so one edit shows everywhere."""
    rows = conn.execute(
        "SELECT * FROM contacts ORDER BY importance DESC, reply_rate DESC, msg_count DESC"
    ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in rows:
        email = r["email"] or ""
        pid = None
        try:
            pid = repo.person_link_get(conn, email)
        except Exception:  # noqa: BLE001
            pid = None
        key = f"p:{pid}" if pid else f"e:{email.lower()}"
        if key not in groups:
            groups[key] = {"pid": pid, "rows": []}
            order.append(key)
        groups[key]["rows"].append(r)

    def _primary(rs: list) -> Any:
        def rank(x):
            ns = (x["name_source"] if "name_source" in x.keys() else "") or ""
            rel = (x["relationship"] or "")
            return (
                0 if ns in ("manual", "saved", "business") else 1,
                0 if rel == "phone_contact" else 1,
                0 if _is_meaningful_name(x["name"] or "") else 1,
            )
        return sorted(rs, key=rank)[0]

    out = []
    for key in order:
        g = groups[key]
        rs = g["rows"]
        pid = g["pid"]
        primary = _primary(rs)
        prim_email = primary["email"] or ""

        importance = max((int(x["importance"] or 0) for x in rs), default=0)
        messages = sum(int(x["msg_count"] or 0) for x in rs)
        reply_rate = max((float(x["reply_rate"] or 0.0) for x in rs), default=0.0)
        flags = sorted({f for x in rs for f in ((x["flags"] or "").split(",")) if f})
        is_saved = any(_contact_info(conn, x["email"] or "").get("is_saved") for x in rs)

        person_name = ""
        if pid:
            try:
                prow = repo.person_get(conn, pid)
                person_name = ((prow["display_name"] if prow else "") or "").strip()
                if not is_saved and repo.person_is_saved(conn, pid):
                    is_saved = True
            except Exception:  # noqa: BLE001
                pass
        name = person_name if (is_saved and person_name) else (primary["name"] or prim_email)

        handles = [
            {"id": (x["email"] or ""), "kind": _handle_kind(x["email"] or ""),
             "label": _handle_label(conn, x["email"] or "")}
            for x in rs if (x["email"] or "")
        ]
        if pid:
            seen = {h["id"].lower() for h in handles}
            try:
                for lr in conn.execute(
                    "SELECT identifier FROM person_links WHERE person_id=?", (pid,)):
                    hid = (lr["identifier"] or "")
                    if hid and hid.lower() not in seen:
                        handles.append({"id": hid, "kind": _handle_kind(hid),
                                        "label": _handle_label(conn, hid)})
                        seen.add(hid.lower())
            except sqlite3.Error:
                pass
        out.append({
            "name": name,
            "email": prim_email,
            "person_id": pid or "",
            "handles": handles,
            "relationship": primary["relationship"] or "",
            "relationship_type": repo.relationship_type_for_identifier(conn, prim_email),
            "importance": importance,
            "is_vip": importance >= 70 or "vip" in flags,
            "flags": flags,
            "you_reply_pct": round(reply_rate * 100),
            "messages": messages,
            "is_saved": bool(is_saved),
            "name_source": (primary["name_source"] if "name_source" in primary.keys() else "") or "",
        })

    out.sort(key=lambda i: (-i["importance"], -i["messages"]))
    return out[:limit]


def list_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in repo.list_rules(conn):
        out.append({
            "id": r["id"],
            "scope": r["scope"],
            "applies_to": r["match_key"] or "everything",
            "rule": r["instruction"],
            "status": r["status"],            # active | proposed | retired
            "learned": r["source"] == "inferred",
            "needs_confirm": r["status"] == "proposed",
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# P6 — commitments, voice profiles, proposed rules, metrics, test-pipeline
# ─────────────────────────────────────────────────────────────────────────────
def _commitment_person(conn: sqlite3.Connection, person_id: str, contact_email: str) -> str:
    """A human display name for a commitment's counterparty (persons.display_name, else the
    contact's name, else the email localpart), or '' when there's no counterparty."""
    pid = (person_id or "").strip()
    if pid:
        try:
            row = conn.execute("SELECT display_name FROM persons WHERE id=?", (pid,)).fetchone()
            if row and (row["display_name"] or "").strip():
                return row["display_name"].strip()
        except sqlite3.Error:
            pass
    email = (contact_email or "").strip()
    if email:
        try:
            row = conn.execute("SELECT name FROM contacts WHERE email=?", (email.lower(),)).fetchone()
            if row and (row["name"] or "").strip():
                return row["name"].strip()
        except sqlite3.Error:
            pass
        return email.split("@")[0]
    return ""


def list_commitments(conn: sqlite3.Connection) -> dict[str, Any]:
    """Open commitments + stale threads for the Commitments view. Each open item is enriched
    with the counterparty's display name, its status, and when it was promised — so the UI can
    group by urgency, show who it's with, and offer Done/Snooze (all already supported
    server-side) instead of a flat read-only list."""
    from assistant.memory import commitments as C

    open_items = []
    for r in C.open_commitments(conn):
        cols = r.keys()
        open_items.append({
            "id": r["id"],
            "to": r["contact_email"] or "someone",            # back-compat (older clients)
            "person": _commitment_person(
                conn, r["person_id"] if "person_id" in cols else "", r["contact_email"]),
            "promise": r["commitment_text"],
            "due_date": r["due_date"] or "",
            "status": (r["status"] if "status" in cols else "open") or "open",
            "created_at": int(r["created_at"]) if ("created_at" in cols and r["created_at"]) else 0,
        })
    stale = C.stale_threads(conn)
    return {"open": open_items, "stale": stale}


def list_voice_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    import json

    out = []
    for r in repo.list_voice_profiles(conn):
        try:
            data = json.loads(r["profile_json"] or "{}")
        except (ValueError, TypeError):
            data = {}
        out.append({
            "segment": r["segment"],
            "summary": data.get("summary", ""),
            "examples": data.get("examples", [])[:3],
            "sample_count": r["sample_count"] or 0,
            "last_rebuilt": r["last_rebuilt"],
        })
    return out


def list_proposed_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in repo.list_proposed_rules(conn, status="pending"):
        out.append({
            "id": r["id"],
            "rule": r["rule_text"],
            "source": r["source"],
            "evidence": r["pattern_evidence"] or "",
        })
    return out


def _day(epoch: int) -> str:
    import time as _t

    return _t.strftime("%Y-%m-%d", _t.localtime(epoch))


def learning_summary(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    """GAP 5 — learning-loop view: total counts per event type, plus a per-day count
    over the last `days` days. Reads learning_events grouped by type and by date."""
    out: dict[str, Any] = {"by_type": {}, "last_7_days": []}
    # scaling-time-3: the ts-filtered scan below uses idx_learning_events_ts_type.
    ensure_perf_indexes(conn)
    try:
        by_type = conn.execute(
            "SELECT type, COUNT(*) AS n FROM learning_events "
            "WHERE type IS NOT NULL AND type != '' GROUP BY type ORDER BY n DESC"
        ).fetchall()
        out["by_type"] = {r["type"]: int(r["n"]) for r in by_type}

        since = repo.now_epoch() - days * 86400
        rows = conn.execute(
            "SELECT ts FROM learning_events WHERE ts >= ?", (since,)
        ).fetchall()
        by_day: dict[str, int] = {}
        for r in rows:
            by_day[_day(int(r["ts"]))] = by_day.get(_day(int(r["ts"])), 0) + 1
        out["last_7_days"] = [{"date": d, "count": by_day[d]} for d in sorted(by_day)]
    except sqlite3.Error:
        pass
    return out


def metrics_daily_breakdown(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """Per-day tier 0/1/2/3 volume + handled-vs-surfaced, from the decision log."""
    since = repo.now_epoch() - days * 86400
    rows = conn.execute(
        "SELECT ts, final_tier FROM decision_log WHERE ts >= ?", (since,)
    ).fetchall()
    by_day: dict[str, dict[str, int]] = {}
    for r in rows:
        d = by_day.setdefault(_day(int(r["ts"])), {"t0": 0, "t1": 0, "t2": 0, "t3": 0})
        d[f"t{int(r['final_tier'] or 0)}"] = d.get(f"t{int(r['final_tier'] or 0)}", 0) + 1
    out = []
    for day in sorted(by_day):
        c = by_day[day]
        handled = c["t0"] + c["t1"]
        surfaced = c["t2"] + c["t3"]
        out.append({"day": day, **c, "handled": handled, "surfaced": surfaced})
    return out


def metrics_accuracy(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """Approval vs edit vs skip rates from the learning events."""
    # scaling-time-3: the ts+type filter below uses idx_learning_events_ts_type.
    ensure_perf_indexes(conn)
    since = repo.now_epoch() - days * 86400
    rows = conn.execute(
        "SELECT type, COUNT(*) AS n FROM learning_events WHERE ts >= ? "
        "AND type IN ('approve','edit','skip') GROUP BY type",
        (since,),
    ).fetchall()
    counts = {r["type"]: r["n"] for r in rows}
    total = sum(counts.values()) or 1
    return {
        "approve": counts.get("approve", 0),
        "edit": counts.get("edit", 0),
        "skip": counts.get("skip", 0),
        "approval_rate": round(counts.get("approve", 0) / total, 3),
        "edit_rate": round(counts.get("edit", 0) / total, 3),
    }


def metrics_costs(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    from assistant.storage import metrics

    since = repo.now_epoch() - days * 86400
    out = []
    for r in metrics.costs_by_task(conn, since):
        out.append({
            "task": r["task"] or "?",
            "model": r["model"] or "?",
            "calls": r["calls"] or 0,
            "cost": round(r["cost"] or 0.0, 4),
            "tokens": r["tokens"] or 0,
        })
    return out


def metrics_response_times(conn: sqlite3.Connection) -> dict[str, Any]:
    from assistant.storage import metrics

    since = repo.now_epoch() - 30 * 86400
    return {
        kind: metrics.response_percentiles(conn, kind, since)
        for kind in (metrics.RT_EMAIL_TO_NOTIFICATION,
                     metrics.RT_TAP_TO_CONFIRMATION,
                     metrics.RT_DRAFT_GENERATION)
    }


def audit_filtered(
    conn: sqlite3.Connection, *, start: int = 0, end: int = 0, tier: Optional[int] = None,
    contact: str = "", limit: int = 500,
) -> list[dict[str, Any]]:
    """Audit log with optional date/tier/contact filters (for the Audit view + CSV)."""
    clauses = ["1=1"]
    params: list[Any] = []
    if start:
        clauses.append("ts >= ?"); params.append(start)
    if end:
        clauses.append("ts <= ?"); params.append(end)
    if tier is not None:
        clauses.append("tier = ?"); params.append(int(tier))
    if contact:
        clauses.append("(message_id IN (SELECT message_id FROM decision_log WHERE sender_email LIKE ?))")
        params.append(f"%{contact.lower()}%")
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM audit_log WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT ?",
        params,
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "at": r["ts"], "kind": r["kind"], "detail": r["summary"] or "",
            "tier": r["tier"], "message_id": r["message_id"] or "",
            "was_dry_run": bool(r["dry_run"]),
        })
    return out


def list_audit(conn: sqlite3.Connection, since_epoch: int) -> list[dict[str, Any]]:
    _ACTION_LABEL = {
        "archive": "Filed away",
        "label": "Labeled and filed",
        "fyi": "Handled, told you",
        "surface": "Flagged for you",
        "send": "Reply sent",
        "undo": "Undid an action",
    }
    out = []
    for r in repo.recent_actions(conn, since_epoch):
        out.append({
            "at": r["ts"],
            "what": _ACTION_LABEL.get(r["kind"], r["kind"]),
            "detail": r["summary"] or "",
            "was_dry_run": bool(r["dry_run"]),
        })
    return list(reversed(out))  # newest first
