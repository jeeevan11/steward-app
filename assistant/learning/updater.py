"""Conservative rule inference from accumulated learning events.

Philosophy: *one signal is a hint, a repeated signal is a rule.* We count how
often you have skipped or edited items from the same sender (or, falling back,
from the same category). When the count reaches a small threshold we create a
single **proposed** rule (``status='proposed'``, ``source='inferred'``) and hand
the caller a confirmation prompt to put in front of the human.

Hard invariants:
  * An inferred rule is NEVER auto-activated. It always lands as 'proposed'.
  * We propose a given (scope, match_key, action) rule at most once: if a proposed
    or active inferred rule already exists we bump its evidence instead of spamming.

Stdlib only. Takes an open connection and writes in the caller's transaction.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from assistant.logging_setup import get_logger
from assistant.storage import decision_log
from assistant.storage import repositories as repo

log = get_logger("learning")

# How many like signals before we dare to propose a rule. One = hint, threshold = rule.
DEFAULT_THRESHOLD = 3

# Map a recorded signal type → the action we would propose to stop seeing it.
# skip  → you keep declining to surface this sender's mail  → stop notifying.
# edit  → you keep rewriting drafts for this sender         → ask before drafting.
_SIGNAL_ACTION = {
    "skip": "never_notify",
    "edit": "review_before_draft",
}


def _contact_email_for(action_row: Optional[sqlite3.Row]) -> str:
    if action_row is None:
        return ""
    try:
        keys = action_row.keys()
        if "contact_email" in keys and action_row["contact_email"]:
            return str(action_row["contact_email"]).lower()
    except Exception:  # noqa: BLE001
        try:
            val = action_row.get("contact_email")  # type: ignore[attr-defined]
            if val:
                return str(val).lower()
        except Exception:  # noqa: BLE001
            pass
    return ""


def _message_id_of(action_row: Optional[sqlite3.Row]) -> str:
    if action_row is None:
        return ""
    try:
        return str(action_row["message_id"] or "")
    except Exception:  # noqa: BLE001
        try:
            return str(action_row.get("message_id") or "")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return ""


def _resolve_email(conn: sqlite3.Connection, action_row: Optional[sqlite3.Row]) -> str:
    """Best-effort sender email behind a pending action.

    ROOT CAUSE (learning-loop-5): a raw pending_actions row has NO contact_email column,
    so _contact_email_for(row) almost always returned '' and maybe_propose_rule took the
    GLOBAL branch — counting EVERY skip ever, across all senders, with no time window.
    Three unrelated skips (sender A, B, C — one each) crossed the threshold and produced
    ONE high-confidence GLOBAL never_notify proposal whose wording ("that category")
    misrepresented cross-sender noise as a learned category pattern.

    Fix: resolve the sender the way recorder._resolve_email does — fall back to the
    decision_log row keyed by the action's message_id — BEFORE deciding scope, so a skip
    proposes a CONTACT-scoped rule counting only that sender's events. (If a contact_email
    column is genuinely wanted on pending_actions, see schema_or_config_needed; this
    additive resolution makes per-sender counting work without it.)"""
    direct = _contact_email_for(action_row)
    if direct:
        return direct
    mid = _message_id_of(action_row)
    if mid:
        try:
            d = decision_log.get(conn, mid)
            if d is not None and d["sender_email"]:
                return str(d["sender_email"]).lower()
        except Exception:  # noqa: BLE001
            pass
    return ""


def _proposal_text(signal_type: str, scope: str, match_key: str) -> str:
    who = match_key or "this sender"
    if signal_type == "skip":
        if scope == "contact":
            return (f"You've repeatedly skipped items from {who}. "
                    f"Want me to stop surfacing their mail (file it quietly)?")
        return (f"You've repeatedly skipped '{who}' items. "
                f"Want me to stop surfacing that category?")
    if signal_type == "edit":
        if scope == "contact":
            return (f"You've repeatedly rewritten my drafts to {who}. "
                    f"Want me to check with you before drafting replies to them?")
        return (f"You've repeatedly rewritten my drafts for '{who}' items. "
                f"Want me to check with you before drafting these?")
    return (f"I've noticed a repeated pattern for {who}. "
            f"Want me to create a rule for it?")


def maybe_propose_rule(
    conn: sqlite3.Connection,
    action_row: Optional[sqlite3.Row],
    signal_type: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
) -> Optional[str]:
    """Inspect accumulated events of ``signal_type`` and, if a threshold is met,
    create one *proposed* inferred rule. Returns a human-readable confirmation
    prompt the caller can show the user, or ``None`` when there's nothing to
    propose yet.

    Resolution order for the rule scope:
      1. Resolve the sender email (row column, else decision_log via message_id) →
         propose a CONTACT-scoped rule keyed on that email, counting only that
         sender's events.
      2. If the sender cannot be resolved → return None. We do NOT fall back to a
         GLOBAL rule aggregated from an empty match_key: that branch (learning-loop-5)
         counted unrelated cross-sender skips as one category pattern and surfaced a
         misleadingly high-confidence system-wide never_notify proposal.

    Never raises: any failure logs and returns None (learning is best-effort).
    """
    try:
        action = _SIGNAL_ACTION.get(signal_type)
        if action is None:
            # We only infer rules from skip/edit signals; other signals are recorded
            # for auditing but don't drive automatic rule proposals.
            return None

        # learning-loop-5: resolve the real sender (via decision_log if the raw
        # pending_actions row carries no contact_email) so we count PER-SENDER, not
        # lifetime cross-sender events.
        email = _resolve_email(conn, action_row)
        if not email:
            # No resolvable sender → do not propose an over-broad global rule from a
            # cross-sender aggregate. Stay quiet; record why for observability.
            try:
                repo.record_event(
                    conn, type="rule_proposal_skipped",
                    detail={"reason": "unresolved_sender", "signal_type": signal_type},
                )
            except Exception:  # noqa: BLE001
                pass
            return None

        scope = "contact"
        match_key = email
        count = repo.count_events(conn, type=signal_type, contact_email=email)

        if count < max(1, int(threshold)):
            return None  # still just a hint — not enough evidence to propose a rule

        instruction = _proposal_text(signal_type, scope, match_key)

        # Idempotency: if we've already proposed (or the user already activated) this
        # exact inferred rule, just strengthen its evidence and stay quiet.
        existing = repo.find_inferred_rule(conn, scope, match_key, action)
        if existing is not None:
            try:
                repo.bump_rule_evidence(conn, int(existing["id"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("bump_rule_evidence failed (non-fatal): %s", exc)
            # Only re-surface the prompt if it is still merely proposed (not yet
            # confirmed/retired by the user).
            if str(existing["status"]) == "proposed":
                return instruction
            return None

        confidence = _confidence_for(count, threshold)
        rule_id = repo.add_rule(
            conn,
            scope=scope,
            match_key=match_key,
            instruction=instruction,
            action=action,
            status="proposed",     # NEVER auto-activate an inferred rule
            source="inferred",
            confidence=confidence,
        )
        log.info(
            "Proposed inferred rule #%s (scope=%s key=%s action=%s evidence=%s conf=%.2f)",
            rule_id, scope, match_key or "<global>", action, count, confidence,
        )
        return instruction
    except Exception as exc:  # noqa: BLE001 - inference must never break the caller
        log.warning("maybe_propose_rule failed (non-fatal): %s", exc)
        return None


def _confidence_for(count: int, threshold: int) -> float:
    """A gentle, bounded confidence: starts modest at the threshold and creeps up
    with more evidence, capped well below 1.0 because this is still only inferred.
    """
    threshold = max(1, int(threshold))
    over = max(0, int(count) - threshold)
    return round(min(0.9, 0.6 + 0.1 * over), 3)


def confirm_proposed_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    """Promote a proposed inferred rule to active (called when the user confirms).

    Returns True iff a proposed rule with that id was activated. Defensive: returns
    False rather than raising on a bad id.
    """
    try:
        row = conn.execute(
            "SELECT status FROM rules WHERE id=?", (rule_id,)
        ).fetchone()
        if row is None or str(row["status"]) != "proposed":
            return False
        repo.set_rule_status(conn, rule_id, "active")
        log.info("Activated inferred rule #%s on user confirmation", rule_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("confirm_proposed_rule failed (non-fatal): %s", exc)
        return False


def reject_proposed_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    """Retire a proposed inferred rule (user declined). Returns True on success."""
    try:
        row = conn.execute(
            "SELECT status FROM rules WHERE id=?", (rule_id,)
        ).fetchone()
        if row is None or str(row["status"]) != "proposed":
            return False
        repo.set_rule_status(conn, rule_id, "retired")
        log.info("Retired inferred rule #%s on user rejection", rule_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("reject_proposed_rule failed (non-fatal): %s", exc)
        return False
