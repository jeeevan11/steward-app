"""Dispatch a FinalDecision to the right tier-specific effect.

Routing by `final.final_tier` (the Tier enum):

  SILENT  (0): perform the reversible silent action; no Telegram.
  FYI     (1): perform the silent action if one is implied, then a one-line FYI.
  APPROVE (2): draft a reply, queue it (idempotency_key = message id + tier), send
               an Approve/Edit/Skip card, persist the Telegram message id.
  ASK     (3): queue an ask (with optional suggestion), send a Draft/Noted card,
               persist the Telegram message id.

For tiers >= APPROVE we also write a "surface" audit row so the log records exactly
what was put in front of the human. The idempotency_key guarantees the same item is
never surfaced twice, even across restarts.

`mail` and `notifier` are injected (no control.* import — avoids an import cycle).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from assistant.action import drafting, gmail_actions
from assistant.config import Settings
from assistant.llm.client import LLMClient
from assistant.logging_setup import get_logger
from assistant.models import Channel, Contact, FinalDecision, Thread, Tier
from assistant.storage import metrics
from assistant.storage import repositories as repo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from assistant.ingest.base import MailSource

log = get_logger("dispatcher")


# ─────────────────────────────────────────────────────────────────────────────
# autosend-invariant-5 — card-delivery reconciliation ledger
#
# ROOT CAUSE: dispatch calls notifier.send_approval (Telegram shows a LIVE card) and only
# THEN calls repo.set_pending_telegram_message to persist the returned tg id. A crash in
# that ~millisecond window leaves the row with telegram_message_id=NULL even though a card
# is live on Telegram. Two harms follow:
#   (1) redeliver_undelivered (status='PENDING' AND tg_id IS NULL) re-sends a SECOND card —
#       a duplicate the owner now sees twice; and
#   (2) _maybe_fold can rewrite that row's draft_text in place, but _rerender_folded_card
#       can't reach the live card (no tg id), so the original card keeps showing STALE text
#       while an Approve tap sends the folded draft the owner never read on it.
#
# There was no durable signal to tell a delivered-but-unpersisted row apart from a genuinely
# never-delivered one. This ledger is that signal: we record a delivery ATTEMPT *before*
# calling the notifier (so a crash leaves a marker proving a card may be live), and CONFIRM
# it with the tg id *after* the persist succeeds. The fold path then refuses to fold into a
# row stuck in the attempted-but-unconfirmed state (its live card can't be re-rendered), and
# reconcile_undelivered uses the ledger to neither double-deliver a card that already went
# out nor silently drop one — re-rendering faithfully from the full row instead.
#
# The ledger is a brand-new table created here via CREATE TABLE IF NOT EXISTS (no edit to
# the shared db.py / migrations.py). It is best-effort: any ledger failure degrades to
# exactly the prior behavior, never blocking a card.
# ─────────────────────────────────────────────────────────────────────────────

_DELIVERY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS card_delivery_log (
    action_id           INTEGER PRIMARY KEY,
    status              TEXT NOT NULL,          -- 'ATTEMPTED' | 'CONFIRMED'
    telegram_message_id TEXT,
    attempted_at        INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    confirmed_at        INTEGER
);
"""


def _ensure_delivery_log(conn: sqlite3.Connection) -> bool:
    """Create the reconciliation ledger if absent. Returns True iff the table is usable.
    Best-effort: a failure (e.g. read-only conn) returns False so callers fall back to the
    pre-existing behavior."""
    try:
        conn.execute(_DELIVERY_LOG_DDL)
        return True
    except Exception:  # noqa: BLE001 - never let ledger setup break dispatch
        log.debug("card_delivery_log setup failed (non-fatal)", exc_info=True)
        return False


def _mark_delivery_attempted(conn: sqlite3.Connection, action_id: int) -> None:
    """Record that we are ABOUT to push a Telegram card for ``action_id`` — written BEFORE
    notifier.send_*, so a crash before the tg-id persist still leaves proof a card may be
    live (autosend-invariant-5). Idempotent: an existing CONFIRMED row is left intact."""
    if not _ensure_delivery_log(conn):
        return
    try:
        conn.execute(
            "INSERT INTO card_delivery_log (action_id, status, attempted_at) "
            "VALUES (?, 'ATTEMPTED', strftime('%s','now')) "
            "ON CONFLICT(action_id) DO NOTHING",
            (int(action_id),),
        )
    except Exception:  # noqa: BLE001
        log.debug("mark_delivery_attempted failed (non-fatal)", exc_info=True)


def _mark_delivery_confirmed(conn: sqlite3.Connection, action_id: int, tg_id: str) -> None:
    """Record that the Telegram card for ``action_id`` was delivered AND its tg id persisted
    (autosend-invariant-5). Called right after repo.set_pending_telegram_message."""
    if not _ensure_delivery_log(conn):
        return
    try:
        conn.execute(
            "INSERT INTO card_delivery_log (action_id, status, telegram_message_id, "
            " attempted_at, confirmed_at) "
            "VALUES (?, 'CONFIRMED', ?, strftime('%s','now'), strftime('%s','now')) "
            "ON CONFLICT(action_id) DO UPDATE SET "
            " status='CONFIRMED', telegram_message_id=excluded.telegram_message_id, "
            " confirmed_at=strftime('%s','now')",
            (int(action_id), str(tg_id)),
        )
    except Exception:  # noqa: BLE001
        log.debug("mark_delivery_confirmed failed (non-fatal)", exc_info=True)


def _delivery_record(conn: sqlite3.Connection, action_id: int) -> sqlite3.Row | None:
    """Return the ledger row for ``action_id`` (or None). Never raises."""
    try:
        if not _ensure_delivery_log(conn):
            return None
        return conn.execute(
            "SELECT action_id, status, telegram_message_id, attempted_at, confirmed_at "
            "FROM card_delivery_log WHERE action_id=?",
            (int(action_id),),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None


def _card_delivery_pending_unconfirmed(conn: sqlite3.Connection, action_id: int) -> bool:
    """True iff a card delivery was ATTEMPTED for ``action_id`` but never CONFIRMED — the
    dangerous crash-window state where a card may be LIVE on Telegram while the row's
    telegram_message_id is still NULL. A row in this state must not be folded into (its live
    card cannot be re-rendered) and must not be blindly re-delivered (autosend-invariant-5)."""
    rec = _delivery_record(conn, action_id)
    if rec is None:
        return False
    try:
        return (rec["status"] or "") == "ATTEMPTED"
    except Exception:  # noqa: BLE001
        return False


def reconcile_undelivered(
    conn: sqlite3.Connection, settings: Settings, notifier: Any,
) -> None:
    """Crash-safe reconciliation for cards whose tg-id never persisted (autosend-invariant-5).

    This is the additive, ledger-aware companion to main.redeliver_undelivered. For each
    PENDING row with no telegram_message_id it consults the delivery ledger:

      * CONFIRMED elsewhere (tg id known in the ledger but lost from the row) → adopt that
        tg id back onto the row instead of sending a SECOND card (no double-delivery).
      * ATTEMPTED-but-unconfirmed → a card MAY already be live. We mark the row's delivery
        as reconciled by re-sending ONCE and recording the new tg id, but only after the
        ledger shows no confirmed delivery — so we neither silently drop it nor stack a
        third copy. The re-render uses the row's own summary/draft (the live fields), and
        because the fold path now refuses to fold such rows, the row's draft cannot have
        diverged from any live card.
      * No ledger entry at all → genuinely never delivered (the common transient-outage
        case): deliver once and confirm, exactly as before.

    Best-effort throughout; any per-row failure is logged and skipped. The integrator may
    call this from the poller in place of (or before) redeliver_undelivered — see
    schema_or_config_needed. begin_send remains the exactly-once net regardless.
    """
    try:
        rows = repo.undelivered_pending(conn)
    except Exception:  # noqa: BLE001
        log.warning("reconcile_undelivered: could not list undelivered", exc_info=True)
        return
    for row in rows:
        try:
            aid = int(row["id"])
            rec = _delivery_record(conn, aid)
            # Already confirmed in the ledger but the row lost its tg id (crash between
            # send and persist): adopt the known tg id; do NOT send a duplicate card.
            if rec is not None and (rec["status"] or "") == "CONFIRMED" and rec["telegram_message_id"]:
                repo.set_pending_telegram_message(
                    conn, aid, settings.telegram_chat_id, str(rec["telegram_message_id"]))
                _safe_record_event(
                    conn, "card_redelivery_reconciled", aid,
                    {"action": "adopted_confirmed_tg_id"})
                log.info("reconcile: adopted confirmed tg id for #%s (no duplicate)", aid)
                continue

            kind = row["kind"] if "kind" in row.keys() else ""
            if kind == "reminder":
                # Reminders are plain text (see main.redeliver_undelivered); leave them to
                # the existing path to avoid changing reminder routing here.
                continue

            # Mark the attempt BEFORE the (re)send so a crash mid-reconcile is still tracked.
            _mark_delivery_attempted(conn, aid)
            attempted = rec is not None and (rec["status"] or "") == "ATTEMPTED"
            if kind == "ask":
                tg = notifier.send_ask(aid, row["summary"] or "", row["draft_text"] or "")
            else:
                tg = notifier.send_approval(aid, row["summary"] or "", row["draft_text"] or "")
            if tg:
                repo.set_pending_telegram_message(conn, aid, settings.telegram_chat_id, tg)
                _mark_delivery_confirmed(conn, aid, str(tg))
                _safe_record_event(
                    conn, "card_redelivery_reconciled", aid,
                    {"action": "redelivered", "was_attempted": attempted})
                log.info("reconcile: re-delivered pending #%s (was_attempted=%s)", aid, attempted)
        except Exception:  # noqa: BLE001
            log.warning("reconcile_undelivered: row failed", exc_info=True)


def _safe_record_event(
    conn: sqlite3.Connection, etype: str, action_id: int, detail: dict[str, Any],
) -> None:
    """Best-effort observability so a reconciliation is never silent (autosend-invariant-5)."""
    try:
        repo.record_event(conn, type=etype, action_id=action_id, detail=detail)
    except Exception:  # noqa: BLE001
        log.debug("record_event(%s) failed (non-fatal)", etype, exc_info=True)


def _summary_line(thread: Thread, contact: Contact, final: FinalDecision) -> str:
    """A short 'who + what' line for cards / audit / FYIs."""
    who = contact.name or contact.email or "someone"
    what = (final.decision.one_line_summary or final.decision.intent or "needs attention").strip()
    prefix = "[WhatsApp] " if thread.channel == Channel.WHATSAPP else ""
    return f"{prefix}{who}: {what}"


def _signal_line(final: FinalDecision) -> str:
    """Line 1 of a card: the SIGNAL (the topic), never the sender."""
    d = final.decision
    return (d.one_line_summary or d.intent or "needs your attention").strip()


def _conversation_history(conn: sqlite3.Connection, contact: Contact, thread: Thread,
                          current_message_id: str, limit: int = 3) -> str:
    """Build a brief thread-history block showing the last `limit` exchanges before
    the current message — inbound lines from whatsapp_inbox, sent replies from
    pending_actions. Returns "" when nothing useful is found."""
    try:
        is_wa = thread.channel == Channel.WHATSAPP
        lines: list[tuple[int, str]] = []  # (ts, line)

        if is_wa:
            jid = thread.id
            rows = conn.execute(
                "SELECT body, ts FROM whatsapp_inbox "
                "WHERE jid=? AND message_id!=? AND status!='new' "
                "ORDER BY ts DESC LIMIT ?",
                (jid, current_message_id, limit),
            ).fetchall()
            for r in rows:
                body = (r[0] or "").strip()
                if body:
                    lines.append((int(r[1] or 0), f"← {body[:80]}"))

            sent = conn.execute(
                """SELECT pa.draft_text, pa.decided_at
                   FROM pending_actions pa
                   JOIN decision_log dl ON pa.message_id = dl.message_id
                   WHERE dl.sender_email=? AND pa.status='SENT'
                   ORDER BY pa.decided_at DESC LIMIT ?""",
                (contact.email or "", limit),
            ).fetchall()
            for r in sent:
                text = (r[0] or "").strip()
                if text:
                    lines.append((int(r[1] or 0), f"→ {text[:80]}"))
        else:
            # For email, show last sent reply subjects/drafts
            sent = conn.execute(
                """SELECT pa.draft_text, pa.decided_at
                   FROM pending_actions pa
                   JOIN decision_log dl ON pa.message_id = dl.message_id
                   WHERE dl.sender_email=? AND pa.status='SENT'
                   ORDER BY pa.decided_at DESC LIMIT ?""",
                (contact.email or "", limit),
            ).fetchall()
            for r in sent:
                text = (r[0] or "").strip()
                if text:
                    lines.append((int(r[1] or 0), f"→ You: {text[:80]}"))

        if not lines:
            return ""

        lines.sort(key=lambda x: x[0])
        history = "\n".join(t for _, t in lines[-limit:])
        return f"📋 Recent context:\n{history}"
    except Exception:  # noqa: BLE001
        return ""


def _card_fields(thread: Thread, contact: Contact) -> tuple[str, str, str]:
    """Build the card's people/mail lines:
      who:   "👤 <name> · <why-known>"  for a saved contact, or
             "🆕 <name> — not a saved contact"  for an unknown/unsaved sender.
      mail:  the actual message — channel + email address + "subject".
      quote: a snippet of what they actually wrote.
    Knowing-vs-unsaved is surfaced explicitly so Jatin can tell a real contact from a
    stranger at a glance."""
    from assistant.memory import contacts as memory_contacts

    inbound = thread.latest_inbound or thread.latest
    is_wa = thread.channel == Channel.WHATSAPP

    # Use a human-readable name: fall back to phone number when push_name is dots/blank.
    raw_name = (contact.name or "").strip()
    if re.sub(r"[^a-zA-Z0-9]", "", raw_name) and len(re.sub(r"[^a-zA-Z0-9]", "", raw_name)) >= 2:
        name = raw_name
    elif is_wa:
        # Extract phone/group label from JID
        jid = contact.email or ""
        local = jid.split("@")[0]
        suffix = jid.split("@")[-1] if "@" in jid else ""
        if suffix == "newsletter":
            name = "WhatsApp newsletter"
        elif suffix in ("g.us",):
            name = raw_name or "WhatsApp group"
        elif local.isdigit():
            name = f"+{local}"
        else:
            name = raw_name or contact.email or "someone"
    else:
        name = contact.email or "someone"

    if memory_contacts.is_recognized(contact):
        note = memory_contacts.recognition_note(contact)
        who = f"👤 {name}" + (f" · {note}" if note else "")
    else:
        # For unsaved: show push_name + phone number so the card is actionable.
        phone_hint = ""
        if is_wa:
            local = (contact.email or "").split("@")[0]
            if local.isdigit() and name != f"+{local}":
                phone_hint = f" (+{local})"
        who = f"🆕 {name}{phone_hint} — not a saved contact"

    addr = (contact.email or "").strip()
    subject = (inbound.subject or "").strip() if inbound is not None else ""
    if is_wa:
        n_inbound = sum(1 for m in thread.messages if not m.from_me)
        is_newsletter = thread.id.endswith("@newsletter")
        if is_newsletter:
            base = "💬 WhatsApp newsletter"
        elif subject:
            base = f'💬 WhatsApp group · "{subject}"'
        else:
            base = "💬 WhatsApp"
        mail = base + (f" · {n_inbound} messages" if n_inbound > 1 else "")
    else:
        parts = [p for p in (addr, (f'"{subject}"' if subject else "")) if p]
        mail = ("📧 Email · " + " · ".join(parts)) if parts else "📧 Email"

    quote = ((inbound.body_text or inbound.snippet or "").strip()) if inbound is not None else ""
    return who, mail, quote


def _apply_quality_gate(
    conn: sqlite3.Connection, settings: Settings, thread: Thread, contact: Contact,
    message_id: str, draft: str,
) -> tuple[str, str]:
    """Run the P5b gate: returns (clean_draft, warning). Auto-fixes are silent; flags
    become a warning appended to the card's context line (never the draft). Best-effort:
    on any error returns the draft unchanged with no warning."""
    if not getattr(settings, "quality_gate_enabled", True):
        return draft, ""
    try:
        from assistant.action import quality_gate
        from assistant.memory import contacts as memory_contacts
        from assistant.storage import decision_log

        segment = memory_contacts.detect_segment(conn, contact.email)
        # drafting-safety-4: feed the gate the SAME grounding the drafter saw (thread render
        # + WhatsApp recent_block + relationship memory + calendar drafting_note), not just
        # thread.render_for_prompt(). Without this, a specific the model correctly pulled
        # from the recent_block / calendar / memory (e.g. "18:30", a "15:00" slot) is absent
        # from source_text and gets false-flagged as a fabrication — training the owner to
        # ignore the warning and masking real fabrications. Best-effort: on any failure fall
        # back to the thread render alone (the prior behavior), never breaking the card.
        try:
            source_text = drafting.grounding_text(conn, settings, thread, contact)
        except Exception:  # noqa: BLE001
            source_text = thread.render_for_prompt()
        if not source_text:
            source_text = thread.render_for_prompt()
        qr = quality_gate.check_and_fix(draft, segment, source_text)
        decision_log.set_quality_gate(conn, message_id, qr.to_json())
        warning = ("⚠️ review: " + "; ".join(qr.flags)) if qr.needs_review else ""
        return qr.clean_draft, warning
    except Exception:  # noqa: BLE001 - the gate must never block a card
        log.debug("quality gate failed (non-fatal)", exc_info=True)
        return draft, ""


def _record_email_to_notification(conn: sqlite3.Connection, thread: Thread) -> None:
    """Best-effort latency sample: inbound arrival → card delivered (P0e)."""
    try:
        inbound = thread.latest_inbound or thread.latest
        if inbound is None or not inbound.timestamp:
            return
        ms = int((time.time() - float(inbound.timestamp)) * 1000)
        if 0 <= ms <= 7 * 24 * 3600 * 1000:  # ignore absurd clock skew
            metrics.record_response_time(conn, metrics.RT_EMAIL_TO_NOTIFICATION, ms)
    except Exception:  # noqa: BLE001 - latency logging never breaks dispatch
        pass


_PENDING_IDENTITY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "pending_identity.json"
)


def _write_pending_identity(settings: Settings, jid: str) -> None:
    """Persist the JID of the most-recently-surfaced unknown contact so on_text can
    resolve a 'name: X' reply to the right contact record. Best-effort."""
    try:
        with open(_PENDING_IDENTITY_PATH, "w") as f:
            json.dump({"jid": jid, "ts": int(time.time())}, f)
    except Exception:  # noqa: BLE001
        pass


def _idempotency_key(message_id: str, final: FinalDecision) -> str:
    # Keyed on the CLAIMED message id (not thread.latest), so two distinct inbound
    # messages in one thread each surface, while reprocessing the SAME message after
    # a crash is deduped.
    return f"{message_id}:{int(final.final_tier)}"


# GAP 2 — conversation batching window (seconds). A new surfaced message from the same
# sender within this window folds into the existing open card instead of creating a new one.
_FOLD_WINDOW_SECONDS = 1200  # 20 minutes


def _maybe_fold(
    conn: sqlite3.Connection, contact: Contact, thread: Thread, message_id: str,
    summary: str, draft: str, tier: int = 0,
) -> int | None:
    """Conversation model: if THIS THREAD already has an open (PENDING) card, fold this message
    into it — ONE living card per conversation — and collapse any other open cards on the
    thread into it; return the parent action_id. Otherwise return None so the caller creates a
    fresh card. Best-effort: any error → no fold (safe: a new card).

    Folds by THREAD (the conversation), not a 20-minute window: a re-text hours later on the
    same unanswered chat updates the one card instead of stranding the earlier one as a
    separate stale card (the 'Maya #158 + #164' bug). The card carries the HIGHEST tier
    across the unanswered burst, so a trivial later 'ok' can't downgrade an important ask.

    approval-telegram-2 (NO_WRONG_THREAD): the target is the SAME thread_id, so a message on a
    DIFFERENT thread can never overwrite this card's draft/target. Returns the parent
    action_id (truthy) on a fold so the caller re-renders + re-stamps (WYSIWYG_APPROVAL)."""
    try:
        if not (thread and thread.id):
            return None
        open_action = repo.find_open_action_for_thread(conn, thread.id)
        if open_action is None:
            return None
        # Don't fold into a *different* message's own row if it's literally the same id
        # being reprocessed (idempotency handles that path separately).
        if open_action["message_id"] == message_id:
            return None
        aid = int(open_action["id"])
        # autosend-invariant-5: never fold into a row whose card delivery was ATTEMPTED but
        # not CONFIRMED (a crash may have left a LIVE card with a NULL telegram_message_id).
        # Folding would rewrite draft_text in place, but _rerender_folded_card can't reach
        # that live card (no tg id), so it would keep showing STALE text while Approve sends
        # the merged draft. Falling back to a fresh card is safe: the original card stays as
        # the owner last saw it, and the new message gets its own fully-rendered card.
        if _card_delivery_pending_unconfirmed(conn, aid):
            _safe_record_event(
                conn, "fold_skipped_unconfirmed_delivery", aid,
                {"reason": "card delivery attempted-but-unconfirmed", "thread_id": thread.id})
            log.info("dispatch: skip fold into #%s (delivery unconfirmed; fresh card instead)", aid)
            return None
        if repo.fold_message_into_action(conn, aid, message_id, summary, draft, new_tier=tier):
            # Collapse any OTHER open cards on this thread into the living one, so handling it
            # never leaves a stranded sibling (e.g. an old 'going to sleep' card behind it).
            try:
                sib = repo.resolve_thread_siblings(conn, thread.id, aid)
                if sib:
                    log.info("dispatch: fold on thread %s superseded %d stale sibling card(s)",
                             thread.id, len(sib))
            except Exception:  # noqa: BLE001 - sibling cleanup is best-effort
                log.debug("sibling resolution failed (non-fatal)", exc_info=True)
            return aid
        return None
    except Exception:  # noqa: BLE001 - folding is an optimization, never a blocker
        log.debug("dispatch: fold check failed (non-fatal)", exc_info=True)
        return None


def _stamp_card_approval(
    conn: sqlite3.Connection, settings: Settings, action_id: int, thread: Thread,
) -> None:
    """Bind the WYSIWYG approval to the current draft + the exact reply target the send path
    will use, so begin_send can prove the owner approved what gets sent. Recipients are
    computed the SAME way execute_send computes them (_reply_recipients), so a fold that
    changes the routing is detected as a mismatch. Best-effort: never blocks dispatch."""
    try:
        to, cc = gmail_actions._reply_recipients(thread, settings)
        repo.stamp_approval(conn, action_id, thread_id=thread.id, recipients=list(to) + list(cc))
    except Exception:  # noqa: BLE001 - stamping is additive; never break dispatch
        log.debug("dispatch: approval stamp failed (non-fatal)", exc_info=True)


def _rerender_folded_card(
    conn: sqlite3.Connection, settings: Settings, notifier: Any, action_id: int,
    thread: Thread, signal: str, draft: str, card_sender: str, mail: str, quote: str,
    conv_ctx: str, source_url: str, source_label: str,
) -> None:
    """After a fold, rewrite the already-delivered Telegram card to the merged draft and
    re-stamp the approval (WYSIWYG_APPROVAL / approval-telegram-1). If the card can't be
    re-rendered, the approval stays invalidated (cleared by the fold) so begin_send refuses
    the unseen draft — the owner is never able to approve text they did not see."""
    row = repo.get_pending(conn, action_id)
    if row is None:
        return
    tg_id = row["telegram_message_id"] if "telegram_message_id" in row.keys() else ""
    tier = int(row["tier"] or 2)
    if notifier is not None and tg_id:
        try:
            notifier.edit_approval(
                tg_id, action_id, signal, draft, tier=tier, sender=card_sender,
                mail=mail, quote=quote, context=conv_ctx,
                source_url=source_url, source_label=source_label)
        except Exception:  # noqa: BLE001 - re-render is best-effort; begin_send is the net
            log.warning("dispatch: fold re-render failed for #%s", action_id, exc_info=True)
    # Re-stamp only AFTER the merged draft is on screen, binding the approval to it.
    _stamp_card_approval(conn, settings, action_id, thread)


def dispatch(
    conn: sqlite3.Connection,
    settings: Settings,
    mail: "MailSource",
    llm: LLMClient,
    notifier: Any,
    thread: Thread,
    contact: Contact,
    final: FinalDecision,
    message_id: str,
) -> None:
    """Route one classified thread to the effect appropriate for its tier.

    ``message_id`` is the specific inbound message that was claimed for processing;
    it anchors the idempotency key and the pending/audit rows.
    """
    tier = Tier.clamp(int(final.final_tier))
    latest = thread.latest

    if tier == Tier.SILENT:
        if latest is not None:
            gmail_actions.perform_silent_action(conn, mail, settings, latest, final.decision)
        return

    if tier == Tier.FYI:
        if latest is not None:
            # Only a reversible archive/label hint triggers a side effect here.
            # perform_silent_action is itself idempotent (see has_action guard).
            op, _ = gmail_actions._parse_suggested_action(final.decision.suggested_action)
            if op != "none":
                gmail_actions.perform_silent_action(conn, mail, settings, latest, final.decision)
        summary = final.decision.one_line_summary or _summary_line(thread, contact, final)
        # Idempotent FYI: don't re-ping you if a crash-recovery reprocessing replays
        # this. Notify first (a missed FYI is worse than a rare duplicate), then audit.
        if not repo.has_action(conn, message_id, ("fyi",)):
            if notifier is not None:
                try:
                    notifier.fyi(f"{summary} — handled")
                except Exception:  # noqa: BLE001 - notifier is best-effort
                    log.warning("dispatch FYI notify failed", exc_info=True)
            repo.log_action(
                conn, kind="fyi", message_id=message_id, thread_id=thread.id,
                tier=int(Tier.FYI), summary=summary, reversible=False,
                dry_run=settings.dry_run,
            )
        return

    # Tiers >= APPROVE need the human. Queue (deduped) + surface + persist tg id.
    summary = _summary_line(thread, contact, final)
    key = _idempotency_key(message_id, final)

    # Both APPROVE and ASK pre-generate the draft BEFORE notifying, so the human's
    # tap is instant (no LLM call on the approve path). drafting.draft_reply never
    # raises — on an LLM failure it returns a safe holding draft, so the
    # notification is never blocked (P0b).
    signal = _signal_line(final)
    who, mail, quote = _card_fields(thread, contact)
    # Phase 2: show the human "why" on the card (the full explanation is persisted and
    # also available in the dashboard/audit). Falls back to the bare surfaced reason.
    why = ""
    try:
        from assistant.storage import explanations
        why = explanations.why_summary(final.decision, final)
    except Exception:  # noqa: BLE001
        why = final.surfaced_reason or ""
    if why:
        who = f"{who} · {why}"

    conv_ctx = _conversation_history(conn, contact, thread, message_id)

    # Backtrack link to the original conversation (Gmail thread / WhatsApp chat) — shown
    # as a tappable ↗ button on the card. Best-effort: any failure → no button.
    source_url = source_label = ""
    try:
        from assistant.storage import read_queries as _rq
        _ch = "whatsapp" if thread.channel == Channel.WHATSAPP else "gmail"
        _src = _rq.source_link(message_id, thread.id, _ch, contact.email or "", "")
        source_url, source_label = _src.get("url", ""), _src.get("label", "")
    except Exception:  # noqa: BLE001
        pass

    # Ask user to identify unsaved contacts so names fill in over time.
    _is_unknown = (
        not contact.relationship
        or contact.relationship == "wa_contact"
    ) and not contact.flags and contact.importance <= 10

    if tier == Tier.APPROVE:
        draft = drafting.draft_reply(conn, llm, settings, thread, contact, final)
        draft, warning = _apply_quality_gate(conn, settings, thread, contact, message_id, draft)
        card_sender = f"{who} · {warning}" if warning else who
        # GAP 2: fold into an open card from the SAME sender ON THE SAME THREAD within the
        # batch window instead of creating a second pending action (and a second ping). The
        # fold invalidates the prior approval; we re-render the card to the merged draft and
        # re-stamp the approval so the owner approves exactly what gets sent (WYSIWYG).
        folded_into = _maybe_fold(conn, contact, thread, message_id, summary, draft,
                                  tier=int(final.final_tier))
        if folded_into:
            log.info("dispatch: folded APPROVE for %s into an open card", contact.email)
            _rerender_folded_card(
                conn, settings, notifier, folded_into, thread, signal, draft, card_sender,
                mail, quote, conv_ctx, source_url, source_label)
            _audit_surface(conn, settings, message_id, thread, final, summary)
            return
        aid = repo.create_pending(
            conn,
            idempotency_key=key,
            message_id=message_id,
            thread_id=thread.id,
            tier=int(final.final_tier),
            kind="reply_draft",
            summary=summary,
            draft_text=draft,
            telegram_chat_id=settings.telegram_chat_id,
        )
        if aid is None:
            log.info("dispatch: APPROVE item already queued (key=%s) — skipping", key)
            return
        # WYSIWYG_APPROVAL: bind the approval to this exact draft + reply target before the
        # owner can act, so begin_send refuses if anything mutates it under the approval.
        _stamp_card_approval(conn, settings, aid, thread)
        _audit_surface(conn, settings, message_id, thread, final, summary)
        if notifier is not None:
            try:
                if _is_unknown:
                    _write_pending_identity(settings, contact.email or "")
                # autosend-invariant-5: record the delivery ATTEMPT before send_approval so a
                # crash before the tg-id persist still leaves proof a card may be live.
                _mark_delivery_attempted(conn, aid)
                tg = notifier.send_approval(aid, signal, draft, sender=card_sender,
                                            mail=mail, quote=quote, context=conv_ctx,
                                            unknown_contact=_is_unknown,
                                            source_url=source_url, source_label=source_label)
                if tg:
                    repo.set_pending_telegram_message(conn, aid, settings.telegram_chat_id, tg)
                    _mark_delivery_confirmed(conn, aid, str(tg))
                    _record_email_to_notification(conn, thread)
            except Exception:  # noqa: BLE001
                log.warning("dispatch: send_approval failed for action %s", aid, exc_info=True)
        return

    # Tier.ASK — pre-generate a suggested reply so "Send suggested" is one tap.
    draft = drafting.draft_reply(conn, llm, settings, thread, contact, final)
    draft, warning = _apply_quality_gate(conn, settings, thread, contact, message_id, draft)
    card_sender = f"{who} · {warning}" if warning else who
    # GAP 2: fold a same-sender ASK into an open card ON THE SAME THREAD within the batch
    # window; re-render to the merged draft and re-stamp the approval (WYSIWYG). Pass the ASK
    # tier (3) so folding a new important ASK into an older lower-tier card RAISES it to 3
    # (tier=MAX); the APPROVE branch already passes its tier — this keeps them symmetric.
    folded_into = _maybe_fold(conn, contact, thread, message_id, summary, draft,
                              tier=int(final.final_tier))
    if folded_into:
        log.info("dispatch: folded ASK for %s into an open card", contact.email)
        _rerender_folded_card(
            conn, settings, notifier, folded_into, thread, signal, draft, card_sender,
            mail, quote, conv_ctx, source_url, source_label)
        _audit_surface(conn, settings, message_id, thread, final, summary)
        return
    aid = repo.create_pending(
        conn,
        idempotency_key=key,
        message_id=message_id,
        thread_id=thread.id,
        tier=int(final.final_tier),
        kind="ask",
        summary=summary,
        draft_text=draft,
        telegram_chat_id=settings.telegram_chat_id,
    )
    if aid is None:
        log.info("dispatch: ASK item already queued (key=%s) — skipping", key)
        return
    # WYSIWYG_APPROVAL: bind the approval to this exact draft + reply target.
    _stamp_card_approval(conn, settings, aid, thread)
    _audit_surface(conn, settings, message_id, thread, final, summary)

    if notifier is not None:
        try:
            if _is_unknown:
                _write_pending_identity(settings, contact.email or "")
            # autosend-invariant-5: record the delivery ATTEMPT before send_ask (see APPROVE).
            _mark_delivery_attempted(conn, aid)
            tg = notifier.send_ask(aid, signal, draft, sender=card_sender,
                                   mail=mail, quote=quote, context=conv_ctx,
                                   unknown_contact=_is_unknown,
                                   source_url=source_url, source_label=source_label)
            if tg:
                repo.set_pending_telegram_message(conn, aid, settings.telegram_chat_id, tg)
                _mark_delivery_confirmed(conn, aid, str(tg))
                _record_email_to_notification(conn, thread)
        except Exception:  # noqa: BLE001
            log.warning("dispatch: send_ask failed for action %s", aid, exc_info=True)


def _audit_surface(
    conn: sqlite3.Connection,
    settings: Settings,
    message_id: str,
    thread: Thread,
    final: FinalDecision,
    summary: str,
) -> None:
    """Record that this item was surfaced to the human (tiers >= APPROVE)."""
    repo.log_action(
        conn,
        kind="surface",
        message_id=message_id,
        thread_id=thread.id,
        tier=int(final.final_tier),
        summary=summary,
        reversible=False,
        dry_run=settings.dry_run,
    )
