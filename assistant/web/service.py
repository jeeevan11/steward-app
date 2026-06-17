"""Write actions for the console — thin wrappers that call the EXACT same guarded
functions the Telegram bot uses. No new safety logic lives here; the database-level
compare-and-set guards (mark_approved / begin_send / set_pending_draft / mark_skipped)
make every action safe even if the same item is acted on from Telegram at the same
time. Each function documents which existing seam it calls (see WEB.md).

Dependencies (conn, mail, settings, notifier) are passed in so these are directly
unit-testable and so the API can inject a fake Gmail client in tests.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from assistant.action import gmail_actions
from assistant.learning import recorder, updater
from assistant.logging_setup import get_logger
from assistant.storage import decision_log
from assistant.storage import repositories as repo

log = get_logger("web.service")


def approve(conn: sqlite3.Connection, mail: Any, settings: Any, notifier: Any, action_id: int,
            llm: Any = None) -> dict:
    """"Send this" — mirrors telegram_bot._handle_approve EXACTLY.

    Seam: repo.mark_approved (guard) → gmail_actions.execute_send (begin_send guard,
    dry-run aware) → recorder.record_approve. A stale/duplicate approve or an
    already-sent action returns "already" and sends nothing. When an llm is supplied,
    also captures commitments from the sent reply (P4b; no-op in dry-run).
    """
    if not repo.mark_approved(conn, action_id, via="web"):
        return {"result": "already", "dry_run": bool(settings.dry_run)}
    row = repo.get_pending(conn, action_id)
    ok = gmail_actions.execute_send(conn, mail, settings, action_id, notifier=notifier)
    if ok:
        recorder.record_approve(conn, repo.get_pending(conn, action_id))
        if llm is not None:
            try:
                from assistant.memory import commitments
                commitments.capture_from_send(conn, llm, settings, row)
            except Exception:  # noqa: BLE001 - best-effort, never affects the send
                log.debug("commitment capture failed (non-fatal)", exc_info=True)
            try:
                from assistant.memory import distill as distill_mod
                distill_mod.distill_after_send(
                    conn, llm, settings, mail, row["message_id"] if row else "")
            except Exception:  # noqa: BLE001 - best-effort, never affects the send
                log.debug("post-send distill failed (non-fatal)", exc_info=True)
        return {"result": "sent", "dry_run": bool(settings.dry_run)}
    return {"result": "failed", "dry_run": bool(settings.dry_run)}


def edit(conn: sqlite3.Connection, action_id: int, text: str) -> dict:
    """"Edit first" — mirrors the Telegram edit path.

    Seam: repo.set_pending_draft (guard — refuses terminal rows) → recorder.record_edit.
    """
    # ux-trust-5: a whitespace-only edit ("   "/newlines) is a blank reply. Swift's
    # `!text.isEmpty` counts whitespace as content, so such an edit reached here and would
    # have set a blank draft that the send path then transmitted under the owner's name.
    # Reject it server-side (defense-in-depth alongside the Swift trim-guard and the send-path
    # blank guard) so no console caller can persist an empty draft for an existing reply.
    if not (text or "").strip():
        return {"ok": False, "reason": "An empty reply can't be saved. Type something to send."}
    before = repo.get_pending(conn, action_id)
    original = (before["draft_text"] if before else "") or ""
    if not repo.set_pending_draft(conn, action_id, text):
        return {"ok": False, "reason": "This item has already been handled."}
    recorder.record_edit(
        conn, repo.get_pending(conn, action_id), new_text=text, original_text=original
    )
    return {"ok": True}


def skip(conn: sqlite3.Connection, action_id: int) -> dict:
    """"Skip" — mirrors telegram_bot._handle_skip.

    Seam: repo.mark_skipped (guard) → recorder.record_skip → updater.maybe_propose_rule
    (proposes a rule for confirmation; never auto-applies).
    """
    row = repo.get_pending(conn, action_id)
    if not repo.mark_skipped(conn, action_id):
        return {"ok": False, "reason": "This item has already been handled."}
    recorder.record_skip(conn, row)
    proposal = updater.maybe_propose_rule(conn, row, "skip")
    return {"ok": True, "proposal": proposal}


def feedback(conn: sqlite3.Connection, message_id: str, correct_tier: Optional[int], thumbs: str) -> dict:
    """"Was this the right call?" — teaches the brain.

    Seam: recorder.record_override (learning event). If the correction means the
    item was over-surfaced (a lower tier than what happened), also call
    updater.maybe_propose_rule to PROPOSE (never auto-apply) a quieter rule.
    """
    row = conn.execute(
        "SELECT * FROM pending_actions WHERE message_id=? ORDER BY id DESC LIMIT 1",
        (message_id,),
    ).fetchone()
    d = decision_log.get(conn, message_id)
    from_tier = int(d["final_tier"]) if d is not None else None

    recorder.record_override(
        conn, row, from_tier=from_tier, to_tier=correct_tier,
        detail={"thumbs": thumbs, "source": "web"},
    )

    proposal = None
    if (
        row is not None
        and correct_tier is not None
        and from_tier is not None
        and correct_tier < from_tier
    ):
        # over-surfaced → propose a quieter rule (still requires your confirmation)
        proposal = updater.maybe_propose_rule(conn, row, "skip")
    return {"ok": True, "proposal": proposal}


# ── P6: commitments / rules / contacts / voice (all guarded seams) ────────────
def commitment_done(conn: sqlite3.Connection, commitment_id: str) -> dict:
    from assistant.memory import commitments
    return {"ok": commitments.mark_done(conn, commitment_id)}


def commitment_snooze(conn: sqlite3.Connection, commitment_id: str, days: int = 2) -> dict:
    from assistant.memory import commitments
    return {"ok": commitments.snooze(conn, commitment_id, days=days)}


def confirm_rule(conn: sqlite3.Connection, rule_id: str) -> dict:
    """Confirm a proposed rule. Int id → an existing rules-table row goes active;
    string id → a learned proposed_rules row is confirmed AND promoted to an active
    global rule."""
    if str(rule_id).isdigit():
        repo.set_rule_status(conn, int(rule_id), "active")
        return {"ok": True}
    row = repo.get_proposed_rule(conn, rule_id)
    if repo.set_proposed_rule_status(conn, rule_id, "confirmed"):
        if row is not None:
            repo.add_rule(conn, scope="global", instruction=row["rule_text"], source="user")
        return {"ok": True}
    return {"ok": False, "reason": "already resolved or not found"}


def reject_rule(conn: sqlite3.Connection, rule_id: str) -> dict:
    if str(rule_id).isdigit():
        repo.set_rule_status(conn, int(rule_id), "retired")
        return {"ok": True}
    return {"ok": repo.set_proposed_rule_status(conn, rule_id, "rejected")}


def update_contact(conn: sqlite3.Connection, email: str, *, flags=None, importance=None) -> dict:
    """Update a contact's flags and/or importance floor (guarded upsert)."""
    contact = repo.get_or_default_contact(conn, email)
    if flags is not None:
        contact.flags = {str(f).strip() for f in flags if str(f).strip()}
    if importance is not None:
        try:
            contact.importance = max(0, min(100, int(importance)))
        except (TypeError, ValueError):
            pass
    repo.upsert_contact(conn, contact)
    return {"ok": True, "email": contact.email, "importance": contact.importance,
            "flags": sorted(contact.flags)}


def save_contact(conn: sqlite3.Connection, identifier: str, name: str, phone: str = "") -> dict:
    """Owner taps 'Save contact' in the Mac app: promote an unsaved/unknown sender to a
    real, recognized contact. Writes recognition state only — never a message (NO_AUTO_SEND
    is untouched). `identifier` is the WhatsApp @lid/jid or email; `phone` is optional and,
    when given, bridges the @lid to the phone number so future messages resolve to this person."""
    res = repo.save_contact(conn, identifier, name, phone=phone)
    conn.commit()
    if res.get("ok") and (identifier or "").lower().endswith("@lid"):
        _notify_relay_lid_map(identifier, name, res.get("phone_jid", ""))
    return res


def _notify_relay_lid_map(lid: str, name: str, phone_jid: str = "") -> None:
    """Best-effort: teach the WhatsApp relay the @lid↔number bridge + the saved name, so the
    relay attaches the right name to future inbound messages. Non-fatal if the relay is down."""
    try:
        import json
        import urllib.request

        from assistant.memory import phone_contacts as pc
        body = json.dumps({"lid": lid, "phone_jid": phone_jid or "",
                           "name": name, "source": "manual"}).encode()
        headers = {"Content-Type": "application/json", **pc._relay_auth_headers()}
        req = urllib.request.Request(
            f"http://127.0.0.1:{pc.RELAY_SEND_PORT}/lid-map", data=body,
            headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:  # noqa: BLE001 - relay may be offline; the DB bridge already suffices
        log.debug("relay /lid-map notify failed (non-fatal)", exc_info=True)


def rebuild_voice(conn: sqlite3.Connection, llm: Any, settings: Any) -> dict:
    """Trigger a per-segment voice rebuild. Dry-run safe: reports what it WOULD do
    without spending tokens; live actually rebuilds."""
    if settings.dry_run:
        log.info("voice rebuild requested in dry-run — not spending tokens")
        return {"ok": True, "dry_run": True, "note": "Would rebuild segment profiles."}
    from assistant.action import voice
    rebuilt = voice.build_segment_profiles(conn, llm, settings)
    return {"ok": True, "dry_run": False, "rebuilt": rebuilt}
