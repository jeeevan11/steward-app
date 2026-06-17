"""Gmail-side effects: send a reply, perform a silent reversible action, undo.

Everything here is dry-run aware and idempotent/guarded:

  * execute_send  — gated by repo.begin_send (compare-and-set APPROVED/EDITED →
    SENDING) so a double tap or a restart can never double-send. In dry_run it
    marks the row SENT with a DRYRUN id and audits with dry_run=True, touching
    Gmail not at all.
  * perform_silent_action — honors decision.suggested_action ("archive" or
    "label:Name"). Always logs an audit row with undo_data so it can be reversed,
    even in dry_run (where Gmail is not touched).
  * undo_last — replays the most recent reversible, not-yet-undone audit row.

`mail` is a MailSource instance (typed via TYPE_CHECKING to avoid an import cycle).
`notifier`, when supplied, is only used best-effort for error reporting.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any, Optional

from assistant.config import Settings
from assistant.logging_setup import get_logger
from assistant.models import Decision, Message
from assistant.storage import repositories as repo

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing ingest at runtime
    from assistant.ingest.base import MailSource

log = get_logger("gmail_actions")


def _notify_send_failed(notifier: Optional[Any], action_id: int, exc: Exception) -> None:
    """Tell the owner a send provably did not happen (safe to retry)."""
    if notifier is None:
        return
    try:
        notifier.error(f"Failed to send reply for action {action_id}: {exc}")
    except Exception:  # noqa: BLE001 - notifier is strictly best-effort
        log.warning("notifier.error failed", exc_info=True)


def _notify_send_ambiguous(notifier: Optional[Any], action_id: int) -> None:
    """Tell the owner a send could NOT be confirmed and will NOT be auto-resent — they
    must verify the thread (EXACTLY_ONCE_SEND: never silently resend a maybe-delivered
    message)."""
    if notifier is None:
        return
    try:
        notifier.error(
            f"⚠️ Reply #{action_id}: I could not confirm whether it was delivered, so I "
            f"did NOT resend it (to avoid a duplicate). Please check the conversation and "
            f"reply manually if it didn't arrive."
        )
    except Exception:  # noqa: BLE001 - notifier is strictly best-effort
        log.warning("notifier.error failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Send (irreversible) — guarded against double-send.
# ─────────────────────────────────────────────────────────────────────────────
def execute_send(
    conn: sqlite3.Connection,
    mail: "MailSource",
    settings: Settings,
    action_id: int,
    notifier: Optional[Any] = None,
) -> bool:
    """Send the approved/edited reply for pending action `action_id`.

    Returns True iff the send (or its dry-run equivalent) succeeded. The
    repo.begin_send compare-and-set is the double-send guard: only the caller that
    wins APPROVED/EDITED → SENDING proceeds; everyone else returns False.
    """
    row = repo.get_pending(conn, action_id)
    if row is None:
        log.warning("execute_send: no pending action %s", action_id)
        return False

    # Defense-in-depth: a "reminder" is an informational proactive nudge, never a reply.
    # It has no draft and a non-message message_id, so it must NEVER reach a real send
    # (it would mis-route and error). Treat an approve on one as a no-op skip.
    if (row["kind"] or "") == "reminder":
        log.info("execute_send: action %s is a reminder — not a send; marking skipped", action_id)
        repo.mark_skipped(conn, action_id)
        return False

    # GAP 3 — personal-contact send guardrail. A reply to a partner/family contact is
    # NEVER sent without explicit per-message approval unless personal_auto_send is on.
    # A human approval transitions the row APPROVED/EDITED first (mark_approved), so only
    # the AUTONOMOUS path (status still PENDING) is held here. We keep the row PENDING and
    # annotate the summary so it surfaces for a human decision instead of sending.
    if not getattr(settings, "personal_auto_send", False) and row["status"] == "PENDING":
        if _is_personal_recipient(conn, row):
            note = "[Held for approval — personal contact]"
            summary = (row["summary"] or "")
            if note not in summary:
                summary = (summary + " " + note).strip()
                conn.execute(
                    "UPDATE pending_actions SET summary=? WHERE id=? AND status='PENDING'",
                    (summary, action_id),
                )
            log.info("execute_send: action %s held — personal contact, awaiting approval",
                     action_id)
            return False

    # Double-send guard (REQUIRED): atomically claim the send.
    if not repo.begin_send(conn, action_id):
        log.info(
            "execute_send: action %s not in a sendable state (status=%s) — skipping",
            action_id,
            row["status"],
        )
        return False

    # Approval-integrity guard (WYSIWYG_APPROVAL / NO_WRONG_THREAD / NO_PLACEHOLDER_SENT).
    # Runs AFTER begin_send claimed the row (status=SENDING) and BEFORE any send, in BOTH
    # dry-run and live — a mismatched/placeholder draft must never be recorded as sent.
    if not _passes_send_integrity(conn, action_id, notifier):
        return False

    # Dry-run: pretend to send, audit it, never touch Gmail.
    if settings.dry_run:
        repo.mark_sent(conn, action_id, "DRYRUN")
        repo.log_action(
            conn,
            kind="send",
            message_id=row["message_id"],
            thread_id=row["thread_id"],
            tier=row["tier"],
            summary=f"[dry-run] would send reply: {row['summary'] or ''}".strip(),
            reversible=False,
            dry_run=True,
        )
        log.info("execute_send: action %s sent (DRY-RUN)", action_id)
        _record_reply_in_memory(conn, row)
        return True

    # Live send. Route to the correct channel's source when given a MailRouter
    # (a WhatsApp reply must go via WhatsApp, not Gmail). A plain MailSource has no
    # source_for and is used directly — preserving single-channel behavior.
    src = mail.source_for(row["message_id"]) if hasattr(mail, "source_for") else mail

    # ── 1) Pre-send build. A failure here is BEFORE any irreversible call, so the reply
    #       was provably NOT delivered → SEND_FAILED (safe to retry).
    try:
        thread = src.get_thread(row["message_id"])
        to, cc = _reply_recipients(thread, settings)
        subject = _reply_subject(thread, row)
        in_reply_to = thread.latest.id if thread.latest is not None else row["message_id"]
        # Observability (ingest-email-2 / ingest-email-7): record the RESOLVED reply target
        # so a misroute or an oversized Cc is never silent. Best-effort; never blocks a send.
        try:
            inbound = thread.latest_inbound or thread.latest
            from_addr = (inbound.sender_email if inbound is not None else "") or ""
            reply_to = _reply_to_address(inbound, settings) if inbound is not None else ""
            if reply_to and to and to[0].lower() != (from_addr or "").lower():
                repo.record_event(
                    conn, type="reply_routed_to_replyto", action_id=action_id,
                    message_id=row["message_id"],
                    detail={"to": to, "from": from_addr, "cc_count": len(cc)},
                )
        except Exception:  # noqa: BLE001 - observability never blocks the send
            pass
    except Exception as exc:  # noqa: BLE001 - definitely-not-sent
        log.error("execute_send: action %s build failed (not sent): %s", action_id, exc)
        repo.mark_send_failed(conn, action_id, str(exc))
        _notify_send_failed(notifier, action_id, exc)
        return False

    # ── 2) The irreversible send. If this raises we CANNOT prove non-delivery (a lost or
    #       timed-out ACK may follow a successful delivery) → SEND_AMBIGUOUS, which is
    #       never auto-resent (EXACTLY_ONCE_SEND).
    try:
        sent_id = src.send_reply(
            thread_id=row["thread_id"],
            to=to,
            cc=cc,
            subject=subject,
            body=row["draft_text"] or "",
            in_reply_to_gmail_id=in_reply_to,
        )
    except Exception as exc:  # noqa: BLE001 - maybe-delivered → ambiguous, not retryable
        log.error("execute_send: action %s send raised after dispatch (ambiguous): %s",
                  action_id, exc)
        repo.mark_send_ambiguous(conn, action_id, str(exc))
        _notify_send_ambiguous(notifier, action_id)
        return False

    # ── 3) Delivered. Record SENT. A DB failure here must NOT downgrade to a re-sendable
    #       state — the message is already out, so mark ambiguous (with the sent id) so it
    #       is never resent (closes storage-persistence-5: DB-lock-after-delivery).
    try:
        repo.mark_sent(conn, action_id, sent_id)
    except Exception as exc:  # noqa: BLE001
        log.error("execute_send: action %s delivered as %s but mark_sent failed: %s",
                  action_id, sent_id, exc)
        try:
            repo.mark_send_ambiguous(
                conn, action_id, f"delivered as {sent_id} but DB write failed: {exc}",
                sent_id=sent_id)
        except Exception:  # noqa: BLE001 - leave it SENDING for the SEND_STUCK reaper
            log.error("execute_send: action %s could not be marked ambiguous either",
                      action_id, exc_info=True)
        _notify_send_ambiguous(notifier, action_id)
        return False

    # ── 4) Post-send bookkeeping is best-effort and must NEVER flip a delivered send to a
    #       failure/retry state.
    try:
        repo.log_action(
            conn,
            kind="send",
            message_id=row["message_id"],
            thread_id=row["thread_id"],
            tier=row["tier"],
            summary=f"sent reply: {row['summary'] or ''}".strip(),
            reversible=False,
            dry_run=False,
        )
        _record_reply_in_memory(conn, row)
        _detect_and_close_agreements(conn, row)
        _resolve_thread_after_send(conn, row, action_id)
    except Exception:  # noqa: BLE001 - the send already succeeded; do not undo it
        log.warning("execute_send: action %s post-send bookkeeping failed (sent OK)",
                    action_id, exc_info=True)
    log.info("execute_send: action %s sent as %s", action_id, sent_id)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Compose send (Fix 4) — a brand-new outbound message (no inbound thread to reply
# to). Reached ONLY from a human Approve tap, exactly like execute_send. begin_send
# is the same compare-and-set double-send guard, so a compose can never double-send
# and is never sent autonomously.
# ─────────────────────────────────────────────────────────────────────────────
def execute_compose_send(
    conn: sqlite3.Connection,
    mail: "MailSource",
    settings: Settings,
    action_id: int,
    notifier: Optional[Any] = None,
) -> bool:
    """Send the approved compose card `action_id`. The send target lives in the row's
    compose_meta JSON ({channel, to:[...]|jid, subject}). Returns True on success.

    Mirrors execute_send's guarantees: begin_send wins APPROVED/EDITED -> SENDING (so a
    double tap / restart can't double-send), and in dry_run nothing is sent."""
    import json as _json

    row = repo.get_pending(conn, action_id)
    if row is None:
        log.warning("execute_compose_send: no pending action %s", action_id)
        return False

    # Double-send guard (REQUIRED): atomically claim the send.
    if not repo.begin_send(conn, action_id):
        log.info("execute_compose_send: action %s not in a sendable state (status=%s)",
                 action_id, row["status"])
        return False

    # drafting-safety-1 (NO_PLACEHOLDER_SENT): a compose draft with an unresolved
    # placeholder/holding sentinel must never be sent verbatim. (WYSIWYG hash and target
    # binding apply to reply cards; compose cards carry their own send target in
    # compose_meta and are validated below, so here we run the placeholder guard.)
    if not _passes_send_integrity(conn, action_id, notifier, check_approval=False):
        return False

    try:
        meta = _json.loads(row["compose_meta"]) if ("compose_meta" in row.keys() and row["compose_meta"]) else {}
    except (ValueError, TypeError):
        meta = {}
    body = row["draft_text"] or ""
    channel = (meta.get("channel") or "gmail").lower()

    # Dry-run: pretend to send, audit it, touch nothing.
    if settings.dry_run:
        repo.mark_sent(conn, action_id, "DRYRUN")
        repo.log_action(
            conn, kind="send", message_id=row["message_id"], thread_id=row["thread_id"],
            tier=row["tier"], summary=f"[dry-run] would compose-send: {row['summary'] or ''}".strip(),
            reversible=False, dry_run=True,
        )
        log.info("execute_compose_send: action %s sent (DRY-RUN)", action_id)
        return True

    # ── 1) Pre-send validation/routing. Failures here are BEFORE any send → SEND_FAILED.
    try:
        # Route to the right channel's source. The compose message_id carries the channel
        # prefix (wa_compose_* -> WhatsApp; compose_* -> Gmail) so a MailRouter picks correctly.
        src = mail.source_for(row["message_id"]) if hasattr(mail, "source_for") else mail
        if channel == "whatsapp":
            target_jid = meta.get("jid") or row["thread_id"] or ""
            if not target_jid:
                raise ValueError("compose: no WhatsApp recipient jid")
            send_kwargs = dict(thread_id=target_jid, to=[], cc=[], subject="",
                               body=body, in_reply_to_gmail_id="")
        else:
            to = meta.get("to") or ([row["thread_id"]] if row["thread_id"] else [])
            to = [t for t in to if t]
            if not to:
                raise ValueError("compose: no email recipient")
            # drafting-safety-3 (NO_WRONG_RECIPIENT) defense-in-depth: a compose draft is
            # written for ONE person ("Hi Samuel, ..."). Compose ambiguity is now gated at
            # draft time (compose_and_queue: >1 match => needs_clarification), but a legacy /
            # stale card could still carry a multi-To. Refuse rather than blast the 1:1 draft
            # to every fuzzy match; park it for the owner to pick one.
            if len({t.strip().lower() for t in to}) > 1:
                repo.mark_send_blocked(
                    conn, action_id,
                    "compose addressed to multiple recipients but the draft is written for "
                    "one; pick a single recipient")
                log.error("execute_compose_send: action %s refused — %d recipients for a "
                          "1:1 compose draft", action_id, len(to))
                _notify_blocked(
                    notifier, action_id,
                    "is addressed to one person but would go to several. "
                    "Re-send it to a single recipient.")
                return False
            subject = meta.get("subject") or "(no subject)"
            # thread_id="" => GmailSource starts a NEW thread (no In-Reply-To).
            send_kwargs = dict(thread_id="", to=to, cc=[], subject=subject,
                               body=body, in_reply_to_gmail_id="")
    except Exception as exc:  # noqa: BLE001 - definitely-not-sent
        log.error("execute_compose_send: action %s build failed (not sent): %s", action_id, exc)
        repo.mark_send_failed(conn, action_id, str(exc))
        _notify_send_failed(notifier, action_id, exc)
        return False

    # ── 2) Irreversible send. A raise here is ambiguous (maybe-delivered) → never resent.
    try:
        src.send_reply(**send_kwargs)
    except Exception as exc:  # noqa: BLE001
        log.error("execute_compose_send: action %s send raised after dispatch (ambiguous): %s",
                  action_id, exc)
        repo.mark_send_ambiguous(conn, action_id, str(exc))
        _notify_send_ambiguous(notifier, action_id)
        return False

    # ── 3) Delivered. Record SENT; a DB failure must not make it re-sendable.
    try:
        repo.mark_sent(conn, action_id, "COMPOSED")
    except Exception as exc:  # noqa: BLE001
        log.error("execute_compose_send: action %s delivered but mark_sent failed: %s",
                  action_id, exc)
        try:
            repo.mark_send_ambiguous(conn, action_id, f"delivered but DB write failed: {exc}")
        except Exception:  # noqa: BLE001 - leave SENDING for the reaper
            log.error("execute_compose_send: action %s could not be marked ambiguous",
                      action_id, exc_info=True)
        _notify_send_ambiguous(notifier, action_id)
        return False

    # ── 4) Best-effort bookkeeping; never flip a delivered send.
    try:
        repo.log_action(
            conn, kind="send", message_id=row["message_id"], thread_id=row["thread_id"],
            tier=row["tier"], summary=f"compose-sent: {row['summary'] or ''}".strip(),
            reversible=False, dry_run=False,
        )
        _resolve_thread_after_send(conn, row, action_id)
    except Exception:  # noqa: BLE001
        log.warning("execute_compose_send: action %s post-send bookkeeping failed (sent OK)",
                    action_id, exc_info=True)
    log.info("execute_compose_send: action %s sent (%s)", action_id, channel)
    return True


def _passes_send_integrity(
    conn: sqlite3.Connection, action_id: int, notifier: Optional[Any],
    *, check_approval: bool = True,
) -> bool:
    """Last-line approval-integrity gate, run after begin_send claimed the row (SENDING) and
    before any send. Refuses (parks the row in SEND_BLOCKED + notifies) when:

      * WYSIWYG_APPROVAL / NO_WRONG_THREAD / NO_WRONG_RECIPIENT — the live draft+target no
        longer hash-equal the approval the owner gave (a fold mutated it under the approval,
        or any out-of-band change). ``check_approval=False`` for compose cards, which bind
        their target via compose_meta rather than the reply-thread approval hash.
      * NO_PLACEHOLDER_SENT (drafting-safety-1) — the body still carries an unresolved
        placeholder / holding-draft sentinel.

    Returns True to proceed, False to abort the send. Fail-safe: ANY error → refuse."""
    try:
        row = repo.get_pending(conn, action_id)
        if row is None:
            return False
        body = row["draft_text"] or ""

        # ux-trust-5 (blank-send guard) — NEVER transmit a visually-empty reply. The Mac
        # editor's `!text.isEmpty` check counted whitespace as content, so a whitespace-only
        # edit ("   "/newlines) reached set_pending_draft and would have been sent verbatim
        # under the owner's name. This is the last line of defense at the send path itself
        # (covers reply AND compose, every channel): if the draft is empty after trimming,
        # refuse the send, park the row SEND_BLOCKED, and tell the owner — never ship blank.
        if not body.strip():
            repo.mark_send_blocked(conn, action_id, "blank draft (empty/whitespace-only)")
            log.error("send refused (action %s): blank/whitespace-only draft — not sending",
                      action_id)
            try:
                repo.record_event(
                    conn, type="send_blocked_blank", action_id=action_id,
                    message_id=(row["message_id"] or ""),
                )
            except Exception:  # noqa: BLE001 - observability best-effort
                pass
            _notify_blocked(
                notifier, action_id,
                "is blank. Type a reply before sending (an empty message is never sent).")
            return False

        # NO_PLACEHOLDER_SENT — checked on every send path (reply AND compose).
        from assistant.action import quality_gate
        reason = quality_gate.placeholder_reason(body)
        if reason:
            repo.mark_send_blocked(conn, action_id, f"placeholder: {reason}")
            log.error("send refused (action %s): unresolved placeholder — %s", action_id, reason)
            _notify_blocked(
                notifier, action_id,
                "still has an unfilled placeholder. Edit it before sending.")
            return False

        # WYSIWYG_APPROVAL — the approved draft+target must hash-equal the live row.
        if check_approval and not repo.approval_matches(conn, action_id):
            repo.mark_send_blocked(conn, action_id, "approval hash mismatch (draft/target changed)")
            log.error("send refused (action %s): WYSIWYG approval mismatch — draft/target "
                      "changed under the approval; not sending", action_id)
            _notify_blocked(
                notifier, action_id,
                "changed after you approved it (a newer message folded in). "
                "Re-open and approve the updated draft.")
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - a guard failure must FAIL SAFE (refuse).
        log.error("send integrity guard errored (action %s): %s — refusing", action_id, exc)
        try:
            repo.mark_send_blocked(conn, action_id, f"integrity guard error: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return False


def _notify_blocked(notifier: Optional[Any], action_id: int, why: str) -> None:
    """Surface a refused send to the owner (fail loud, never silent). Best-effort."""
    if notifier is None:
        return
    try:
        notifier.error(f"Did not send #{action_id}: the draft {why}")
    except Exception:  # noqa: BLE001 - notifier is strictly best-effort
        log.warning("execute_send: blocked-send notify failed", exc_info=True)


def _is_personal_recipient(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """GAP 3 — True if the reply's recipient is a personal contact (relationship_type
    partner/family, resolved via the message's sender). Falls back to the contact's
    'personal' flag for backwards compatibility. Best-effort → False on any error."""
    try:
        from assistant.storage import decision_log
        mid = row["message_id"] if row is not None else ""
        if not mid:
            return False
        d = decision_log.get(conn, mid)
        sender = (d["sender_email"] if d is not None else "") or ""
        if not sender:
            return False
        rel = repo.relationship_type_for_identifier(conn, sender)
        if rel in ("partner", "family"):
            return True
        # Fallback: an explicit personal contact flag (pre-relationship_type behavior).
        c = repo.get_contact(conn, sender)
        return bool(c and c.has_flag("personal"))
    except Exception:  # noqa: BLE001 - guardrail check must never crash the send path
        return False


# ingest-email-7: a sender-controlled inbound Cc can list hundreds of third parties. An
# approved reply must never fan out to an attacker-chosen audience, so the reply Cc is
# hard-capped. A normal multi-party thread has a handful of Cc'd people; anything beyond
# this bound is dropped (the reply still reaches the To: sender) rather than blasted.
_MAX_REPLY_CC = 10

# A pragmatic address shape: local@domain.tld. We re-validate every inbound Cc against this
# before copying it onto our outgoing reply, so a malformed / injected header token (e.g.
# "x@y, evil\nBcc: ...") can never smuggle a recipient or a header into the reply.
_ADDR_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")


def _extract_addr(value: str) -> str:
    """Pull the bare address out of a possibly display-named header value
    ("Acme Support <tickets@acme.com>" -> "tickets@acme.com"). Empty if none."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        inner = raw[raw.find("<") + 1: raw.find(">")].strip()
        if inner:
            raw = inner
    return raw.strip().strip("<>").strip()


def _reply_to_address(inbound, settings: Settings) -> str:
    """ingest-email-2: where a standard mail client (Gmail/Apple Mail/Outlook) would send the
    reply. RFC 5322 says: if the inbound message carries a Reply-To header, replies go THERE,
    not to From. We never had a Message.reply_to field, so replies routed to From and mail to
    no-reply / ticketing senders was lost or bounced.

    This reads an optional ``reply_to`` carried on the inbound Message (set at ingest time;
    the field is read defensively via getattr so this is safe even on Messages that predate
    it). A Reply-To equal to our own address is ignored (never reply to ourselves)."""
    me = (settings.gmail_address or "").lower()
    rt_raw = getattr(inbound, "reply_to", "") or ""
    rt = _extract_addr(rt_raw if isinstance(rt_raw, str) else "")
    if rt and _ADDR_RE.match(rt) and rt.lower() != me:
        return rt
    return ""


def _reply_recipients(thread, settings: Settings) -> tuple[list[str], list[str]]:
    """Recipients = where a standard MUA would reply (Reply-To if present, else the sender of
    the latest inbound message); cc preserved, but sanitized and hard-capped.

    Filters our own address out of cc so we don't email ourselves.

    ingest-email-2: prefer the inbound Reply-To over From (RFC 5322 / MUA behavior).
    ingest-email-7: re-validate every Cc address and cap the count so an attacker-controlled
    inbound Cc cannot fan an approved reply out to a large third-party audience.
    """
    me = (settings.gmail_address or "").lower()
    inbound = thread.latest_inbound or thread.latest
    to: list[str] = []
    cc: list[str] = []
    if inbound is not None:
        # ingest-email-2: Reply-To wins over From, matching every standard mail client.
        target = _reply_to_address(inbound, settings) or (inbound.sender_email or "")
        if target:
            to.append(target)
        for addr in inbound.cc:
            if len(cc) >= _MAX_REPLY_CC:
                # ingest-email-7: stop copying once we hit the cap — the To: sender is still
                # reached; we just refuse to amplify the reply to an unbounded list.
                log.warning(
                    "reply Cc capped at %d (inbound listed %d) — extra recipients dropped",
                    _MAX_REPLY_CC, len(inbound.cc),
                )
                break
            a = _extract_addr(addr)
            if not a or not _ADDR_RE.match(a):
                continue  # ingest-email-7: drop malformed / injected tokens.
            if a.lower() != me and a.lower() not in (t.lower() for t in to) \
                    and a.lower() not in (c.lower() for c in cc):
                cc.append(a)
    return to, cc


def _reply_subject(thread, row: sqlite3.Row) -> str:
    base = ""
    if thread.latest is not None and thread.latest.subject:
        base = thread.latest.subject
    elif thread.subject:
        base = thread.subject
    base = base.strip()
    if not base:
        return "Re:"
    return base if base.lower().startswith("re:") else f"Re: {base}"


# ─────────────────────────────────────────────────────────────────────────────
# Silent reversible actions (archive / label) — honors the Decision's hint.
# ─────────────────────────────────────────────────────────────────────────────
def _parse_suggested_action(suggested: str) -> tuple[str, str]:
    """Map decision.suggested_action to (op, arg).

    "archive"        -> ("archive", "")
    "label:Name"     -> ("label",   "Name")
    anything else    -> ("none",    "")  (e.g. "reply", "fyi", "ask")
    """
    s = (suggested or "").strip()
    low = s.lower()
    if low == "archive":
        return "archive", ""
    if low.startswith("label:"):
        return "label", s[len("label:"):].strip()
    return "none", ""


def perform_silent_action(
    conn: sqlite3.Connection,
    mail: "MailSource",
    settings: Settings,
    message: Message,
    decision: Decision,
) -> None:
    """Apply the Decision's reversible silent action (archive/label).

    Always records an audit row (with undo_data) so the action can be undone. In
    dry_run, Gmail is NOT touched — we only log and audit with dry_run=True.
    """
    op, arg = _parse_suggested_action(decision.suggested_action)
    if op == "none":
        log.debug(
            "perform_silent_action: no reversible action implied by %r",
            decision.suggested_action,
        )
        return

    # Idempotency: if we've already archived/labeled this message (e.g. a crash
    # before ledger.complete is causing a reprocess), don't repeat the effect or
    # write a duplicate undo audit row.
    if repo.has_action(conn, message.id, (op,)):
        log.debug("perform_silent_action: %s already recorded for %s — skipping", op, message.id)
        return

    if op == "archive":
        kind = "archive"
        summary = "archived"
        undo_data: Optional[dict[str, Any]] = {
            "op": "archive",
            "message_id": message.id,
            "removed_labels": ["INBOX"],
        }
        live_call = lambda: mail.archive(message.id)  # noqa: E731
    else:  # label
        if not arg:
            log.warning("perform_silent_action: label hint had no name; skipping")
            return
        kind = "label"
        summary = f"labeled {arg}"
        undo_data = {"op": "label", "message_id": message.id, "label": arg}
        live_call = lambda: mail.apply_label(message.id, arg)  # noqa: E731

    if settings.dry_run:
        log.info("perform_silent_action: [dry-run] would %s message %s", summary, message.id)
        repo.log_action(
            conn,
            kind=kind,
            message_id=message.id,
            thread_id=message.thread_id,
            tier=int(decision.proposed_tier),
            summary=f"[dry-run] would {summary}",
            reversible=True,
            undo_data=undo_data,
            dry_run=True,
        )
        return

    try:
        result = live_call()
        if isinstance(result, dict) and result:
            undo_data = result  # prefer the source's own undo payload
    except Exception as exc:  # noqa: BLE001 - record + log, never crash the pipeline
        log.error("perform_silent_action: %s on %s failed: %s", kind, message.id, exc)
        return

    repo.log_action(
        conn,
        kind=kind,
        message_id=message.id,
        thread_id=message.thread_id,
        tier=int(decision.proposed_tier),
        summary=summary,
        reversible=True,
        undo_data=undo_data,
        dry_run=False,
    )
    log.info("perform_silent_action: %s message %s", summary, message.id)


# ─────────────────────────────────────────────────────────────────────────────
# Undo the most recent reversible action.
# ─────────────────────────────────────────────────────────────────────────────
def undo_last(conn: sqlite3.Connection, mail: "MailSource", settings: Settings) -> str:
    """Undo the most recent reversible, not-yet-undone action. Returns a summary."""
    row = repo.last_undoable_action(conn)
    if row is None:
        return "Nothing to undo."

    import json

    try:
        undo_data = json.loads(row["undo_data"]) if row["undo_data"] else None
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.error("undo_last: could not parse undo_data for audit %s: %s", row["id"], exc)
        return "Couldn't undo the last action (corrupt undo data)."

    if not undo_data:
        return "Nothing to undo."

    descr = row["summary"] or row["kind"] or "last action"

    if settings.dry_run:
        repo.mark_undone(conn, row["id"])
        repo.log_action(
            conn,
            kind="undo",
            message_id=row["message_id"] or "",
            thread_id=row["thread_id"] or "",
            summary=f"[dry-run] undid: {descr}",
            reversible=False,
            dry_run=True,
        )
        log.info("undo_last: [dry-run] undid audit %s (%s)", row["id"], descr)
        return f"[dry-run] Undid: {descr}"

    try:
        mail.undo(undo_data)
    except Exception as exc:  # noqa: BLE001
        log.error("undo_last: mail.undo failed for audit %s: %s", row["id"], exc)
        return f"Couldn't undo '{descr}': {exc}"

    repo.mark_undone(conn, row["id"])
    repo.log_action(
        conn,
        kind="undo",
        message_id=row["message_id"] or "",
        thread_id=row["thread_id"] or "",
        summary=f"undid: {descr}",
        reversible=False,
        dry_run=False,
    )
    log.info("undo_last: undid audit %s (%s)", row["id"], descr)
    return f"Undid: {descr}"


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1: Record sent reply in relationship memory (agent_said episode)
# Fix 6: Mutual agreement detection — close inbound commitments on agreement signal
# ─────────────────────────────────────────────────────────────────────────────
_AGREEMENT_SIGNALS = frozenset({
    "sounds good", "confirmed", "agreed", "will do", "got it",
    "i'll do it", "on it", "done", "sure", "absolutely", "of course",
    "yes", "yep", "ok", "okay",
})

def _resolve_thread_after_send(conn: sqlite3.Connection, row: sqlite3.Row, sent_action_id: int) -> None:
    """Conversation model: replying to the latest message resolves the whole conversation. Mark
    any OTHER open pending card on this thread SUPERSEDED so an earlier stranded card (e.g. a
    'going to sleep' from before) is archived, not left for the owner to clear by hand. Pure
    cleanup, never affects the send."""
    try:
        tid = row["thread_id"]
        if tid:
            sib = repo.resolve_thread_siblings(conn, tid, int(sent_action_id))
            if sib:
                log.info("send: reply on thread %s resolved %d sibling card(s)", tid, len(sib))
    except Exception:  # noqa: BLE001 - best-effort, never undo a delivered send
        log.debug("post-send sibling resolution failed (non-fatal)", exc_info=True)


def _record_reply_in_memory(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Fix 1: append a 'sent_reply' episode to the sender's relationship memory so
    future context includes what the agent last said. Best-effort, never raises."""
    try:
        from assistant.storage import decision_log
        from assistant.memory import retrieval
        mid = row["message_id"] if row is not None else ""
        if not mid:
            return
        d = decision_log.get(conn, mid)
        sender = (d["sender_email"] if d is not None else "") or ""
        if not sender:
            return
        person_id = repo.person_link_get(conn, sender)
        if not person_id:
            return
        draft = (row["draft_text"] or "")[:120].strip()
        if draft:
            draft = draft + ("…" if len(row["draft_text"] or "") > 120 else "")
        retrieval.record_episode(
            conn, person_id,
            action="sent_reply",
            thread_id=row["thread_id"] or "",
            note=draft,
        )
    except Exception:  # noqa: BLE001 - memory write never blocks a send
        pass


def _detect_and_close_agreements(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Fix 6: if the sent reply contains agreement language AND the sender has open
    inbound commitments, mark those commitments done (mutually agreed). Best-effort."""
    try:
        draft = (row["draft_text"] or "").lower()
        if not any(sig in draft for sig in _AGREEMENT_SIGNALS):
            return
        from assistant.storage import decision_log
        mid = row["message_id"] if row is not None else ""
        if not mid:
            return
        d = decision_log.get(conn, mid)
        sender = (d["sender_email"] if d is not None else "") or ""
        if not sender:
            return
        # Close open inbound commitments from this contact that we just agreed to.
        rows = conn.execute(
            "SELECT id FROM commitments WHERE contact_email=? AND owner='them' "
            "AND direction='inbound' AND status='open'",
            (sender,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE commitments SET status='done', resolved_at=strftime('%s','now') WHERE id=?",
                (r["id"],),
            )
        if rows:
            log.info("mutual agreement: closed %d inbound commitment(s) for %s", len(rows), sender)
    except Exception:  # noqa: BLE001 - never blocks the send path
        pass
