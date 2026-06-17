"""Record human decisions as learning events.

Thin, defensive wrappers over ``repo.record_event``. Each function pulls the
contact email from the pending action row when available (so signals can later be
aggregated *per sender*) and writes a single ``learning_events`` row.

Conventions for the ``type`` column (matches the schema comment in storage/db.py):
    approve | edit | skip | override | undo

These functions never raise on a missing/garbled row — learning is best-effort and
must never block the action it is recording. They take an open connection and write
in the caller's transaction.
"""

from __future__ import annotations

import difflib
import os
import sqlite3
from typing import Any, Optional

from assistant.logging_setup import get_logger
from assistant.storage import decision_log
from assistant.storage import repositories as repo

log = get_logger("learning")

# ── learning-loop-2: bound the approval-driven importance ratchet ─────────────
# ROOT CAUSE: record_approve fired repo.bump_contact_importance(email, +1) on EVERY
# as-is approval with no decay and no cap, so importance was a monotonic one-way ratchet:
# ~70 approvals of a frequent counterparty flooring them to importance>=70 = permanent VIP
# (brain/tiers.py forces every future message to an approval card), with no path back down
# except manually editing the contact. An attacker who lands enough innocuous auto-drafted
# replies could ratchet a (spoofable) address toward the same permanent-surface state.
#
# Fix: cap the cumulative APPROVAL-driven component of importance well below the VIP
# threshold so approvals alone can never auto-create a VIP — true VIP must come from the
# explicit "vip" flag or a relationship floor, not from rubber-stamping. We track the
# learned component per contact in the kv table (no schema change) and stop bumping once
# it reaches the cap. We also stop bumping if the contact's stored importance is already
# at/above the cap (covers contacts promoted by other paths). Default cap = VIP threshold
# minus a margin; both read from env so no shared-config edit is needed.
_DEFAULT_VIP_THRESHOLD = 70
_APPROVAL_RATCHET_MARGIN = 5  # learned importance tops out this far BELOW the VIP floor


def _vip_threshold() -> int:
    try:
        return int(os.environ.get("VIP_IMPORTANCE_THRESHOLD", _DEFAULT_VIP_THRESHOLD))
    except (TypeError, ValueError):
        return _DEFAULT_VIP_THRESHOLD


def _approval_ratchet_cap() -> int:
    """Max importance the approval ratchet alone may reach (strictly below the VIP floor)."""
    return max(1, _vip_threshold() - _APPROVAL_RATCHET_MARGIN)


def _learned_importance_key(email: str) -> str:
    return f"learned_importance:{(email or '').lower()}"


def _current_importance(conn: sqlite3.Connection, email: str) -> int:
    try:
        row = conn.execute(
            "SELECT importance FROM contacts WHERE email=?", ((email or "").lower(),)
        ).fetchone()
        return int(row["importance"]) if row and row["importance"] is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def _bump_importance_capped(conn: sqlite3.Connection, email: str) -> None:
    """Apply the +1 approval bump ONLY while the approval-driven component stays below the
    cap, so approvals can never alone floor a contact to permanent VIP (learning-loop-2).

    The learned component is tracked in kv (additive, no schema change). Once it hits the
    cap, or the contact's stored importance is already at/above the cap, the bump is a
    no-op. Best-effort: any error falls back to the legacy unconditional +1 so we never
    silently drop a learning signal — but we still record why it stopped when capped."""
    email = (email or "").lower()
    if not email:
        return
    cap = _approval_ratchet_cap()
    key = _learned_importance_key(email)
    try:
        learned = int(repo.kv_get(conn, key) or 0)
    except (TypeError, ValueError):
        learned = 0
    # Already at/above the learned cap, or the contact is already important via some other
    # path — do not ratchet further. This is the whole point of the cap.
    if learned >= cap or _current_importance(conn, email) >= cap:
        if learned < cap:
            # Stamp the learned counter so we don't re-evaluate this contact every approval.
            try:
                repo.kv_set(conn, key, str(cap))
            except Exception:  # noqa: BLE001
                pass
        # Observability only — a distinct type so it never inflates the 'approve' count
        # that metrics_accuracy / calibration read.
        repo.record_event(
            conn, type="importance_ratchet_capped", contact_email=email,
            detail={"cap": cap, "learned": learned},
        )
        return
    repo.bump_contact_importance(conn, email, 1)
    try:
        repo.kv_set(conn, key, str(learned + 1))
    except Exception:  # noqa: BLE001
        pass


# ── learning-loop-4: validate / bound voice samples before they poison the corpus ──────
# ROOT CAUSE: record_approve stored the EXACT unedited model draft_text into voice_samples
# (keyed to the sender, or to the GLOBAL NULL bucket when the email couldn't be resolved)
# with no validation, length bound, dedup, or per-contact cap. get_voice_samples returns
# the newest first and voice_prefix injects it as a top "EXAMPLES OF HOW I ACTUALLY WRITE"
# few-shot, so a single hasty or injection-nudged approve-as-is durably biases future
# drafting voice — globally when it lands in the NULL bucket — with no purge path.
#
# Fix (additive, in a file we own): gate add_voice_sample behind validation so a single
# hasty / injection-nudged approve-as-is can no longer DURABLY or UNBOUNDEDLY poison the
# corpus:
#   * length-bound (skip empty / too-short / absurdly long drafts),
#   * skip injection-shaped drafts (imperative "ignore previous instructions" style),
#   * dedup against the bucket's recent samples,
#   * enforce a recency-rotated cap PER BUCKET (per-contact AND the global NULL bucket),
#     so the unbounded growth that made the global bucket the worst leak is bounded and a
#     stale bad sample rotates out instead of dominating forever.
#
# NOTE on the global (NULL) bucket: the audit's strongest variant says to NEVER write an
# unedited model draft into the global bucket. The existing, un-editable regression
# test_feedback_storage.test_capture_is_mode_independent asserts the legacy behavior that
# an approve with no resolvable email STILL captures a sample, so removing the global
# capture entirely would break a sibling-owned test. We therefore keep the global capture
# but make it validated + deduped + recency-capped (the additive strengthening), and a
# CONTACT-scoped capture is always preferred whenever the sender resolves. The stricter
# "drop global captures" variant is noted for the integrator (it needs that test updated).
_VOICE_SAMPLE_MIN_LEN = 6
_VOICE_SAMPLE_MAX_LEN = 4000
_VOICE_SAMPLE_PER_CONTACT_CAP = 20
# The global (NULL-contact) bucket leaks into EVERY contact's drafts when their own
# samples are thin, so bound it tighter than a single contact's corpus.
_VOICE_SAMPLE_GLOBAL_CAP = 10

# Phrases that strongly suggest the "draft" is echoing an injection attempt rather than
# the owner's voice. Conservative: only the unambiguous control-takeover patterns.
_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard previous",
    "disregard all previous",
    "system prompt",
    "you are now",
    "act as",
)


def _looks_like_injection(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _INJECTION_MARKERS)


def _voice_sample_is_valid(text: str) -> bool:
    """A draft is corpus-worthy only if it is a reasonable-length, non-injection body."""
    t = (text or "").strip()
    if len(t) < _VOICE_SAMPLE_MIN_LEN or len(t) > _VOICE_SAMPLE_MAX_LEN:
        return False
    if _looks_like_injection(t):
        return False
    return True


def _voice_sample_is_dupe(conn: sqlite3.Connection, email: str, body: str) -> bool:
    """True if this exact body is already a recent sample in the same bucket (contact, or
    the global NULL bucket when email is blank)."""
    try:
        if email:
            row = conn.execute(
                "SELECT 1 FROM voice_samples WHERE contact_email=? AND body=? LIMIT 1",
                (email.lower(), body),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM voice_samples WHERE contact_email IS NULL AND body=? LIMIT 1",
                (body,),
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _prune_voice_samples(conn: sqlite3.Connection, email: str) -> None:
    """Keep only the newest cap samples in this bucket so an approve-happy owner can't grow
    the corpus without bound (recency rotation). The global (NULL) bucket — which leaks
    into every contact's drafts — is bounded tighter than a single contact."""
    try:
        if email:
            conn.execute(
                "DELETE FROM voice_samples WHERE contact_email=? AND id NOT IN ("
                " SELECT id FROM voice_samples WHERE contact_email=? "
                " ORDER BY ts DESC, id DESC LIMIT ?)",
                (email.lower(), email.lower(), _VOICE_SAMPLE_PER_CONTACT_CAP),
            )
        else:
            conn.execute(
                "DELETE FROM voice_samples WHERE contact_email IS NULL AND id NOT IN ("
                " SELECT id FROM voice_samples WHERE contact_email IS NULL "
                " ORDER BY ts DESC, id DESC LIMIT ?)",
                (_VOICE_SAMPLE_GLOBAL_CAP,),
            )
    except sqlite3.Error:
        pass


def _maybe_capture_voice_sample(conn: sqlite3.Connection, email: str, draft: str) -> None:
    """Validated, bounded, deduped capture of an approve-as-is draft into the voice corpus
    (learning-loop-4). A CONTACT-scoped capture is always preferred when the sender
    resolves; an unresolvable sender falls back to the global NULL bucket but is still
    validated, deduped, and bounded by the tighter global cap (the unbounded, unvalidated
    global growth was the original poisoning vector). An empty / invalid / injection-shaped
    / duplicate draft is never stored. Best-effort: failures are swallowed (non-fatal)."""
    email = (email or "").lower()
    draft = (draft or "").strip()
    if not draft:
        return
    if not _voice_sample_is_valid(draft):
        # Observability only — distinct type, never counted as an 'approve'.
        repo.record_event(
            conn, type="voice_sample_skipped", contact_email=email,
            detail={"reason": "invalid_or_injection",
                    "bucket": "contact" if email else "global"},
        )
        return
    if _voice_sample_is_dupe(conn, email, draft):
        return
    repo.add_voice_sample(conn, body=draft, contact_email=email or None)
    _prune_voice_samples(conn, email)


def _row_get(action_row: Optional[sqlite3.Row], key: str, default=None):
    """Read a column from a sqlite3.Row OR a plain dict, defensively."""
    if action_row is None:
        return default
    try:
        return action_row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return action_row.get(key, default)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return default


def _resolve_email(conn: sqlite3.Connection, action_row: Optional[sqlite3.Row]) -> str:
    """Best-effort sender email behind a pending action: the row's own column first,
    then the decision_log row keyed by message_id."""
    direct = _contact_email_for(conn, action_row)
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


def _record_episode(
    conn: sqlite3.Connection, action_row: Optional[sqlite3.Row], *,
    action: str, contact_email: str = "", tier=None,
) -> None:
    """Append an agent episode (skipped/sent/...) to the sender's person memory, so the
    brain can later see what it already did toward them. Best-effort (Memory Part C)."""
    try:
        from assistant.memory import identity, retrieval

        email = (contact_email or "").lower() or _resolve_email(conn, action_row)
        pid = identity.person_id_for(conn, email)
        if pid:
            retrieval.record_episode(
                conn, pid, action=action, tier=tier,
                thread_id=str(_row_get(action_row, "thread_id", "") or ""),
            )
    except Exception as exc:  # noqa: BLE001 - episodic memory is best-effort
        log.debug("record episode (%s) failed (non-fatal): %s", action, exc)


def _segment_for(conn: sqlite3.Connection, email: str) -> str:
    """Segment tag for feedback rows. Uses contacts.detect_segment when available
    (P5a); defaults to 'external' otherwise."""
    if not email:
        return "external"
    try:
        from assistant.memory import contacts as memory_contacts

        fn = getattr(memory_contacts, "detect_segment", None)
        if callable(fn):
            return fn(conn, email) or "external"
    except Exception:  # noqa: BLE001
        pass
    return "external"


def _contact_email_for(
    conn: sqlite3.Connection, action_row: Optional[sqlite3.Row]
) -> str:
    """Best-effort: resolve the sender email behind a pending action.

    The pending_actions row only stores message_id/thread_id, so we look up the
    sender via the audit log / contacts is not enough — instead we read the
    learning-relevant identity from the action's message by deferring to the
    caller-supplied row. When we cannot resolve an email we return "" (a global
    signal), which is still useful for category-wide inference.
    """
    if action_row is None:
        return ""
    try:
        # Some callers may already have stitched the contact email onto the row
        # (sqlite3.Row supports key lookup but not .get); guard with keys().
        keys = action_row.keys()
        if "contact_email" in keys and action_row["contact_email"]:
            return str(action_row["contact_email"]).lower()
    except Exception:  # noqa: BLE001 - row may be a plain dict or mapping
        try:
            val = action_row.get("contact_email")  # type: ignore[attr-defined]
            if val:
                return str(val).lower()
        except Exception:  # noqa: BLE001
            pass
    return ""


def _action_id_of(action_row: Optional[sqlite3.Row]) -> Optional[int]:
    if action_row is None:
        return None
    try:
        return int(action_row["id"])
    except Exception:  # noqa: BLE001
        return None


def _message_id_of(action_row: Optional[sqlite3.Row]) -> str:
    if action_row is None:
        return ""
    try:
        return str(action_row["message_id"] or "")
    except Exception:  # noqa: BLE001
        return ""


def _record(
    conn: sqlite3.Connection,
    *,
    type: str,
    action_row: Optional[sqlite3.Row] = None,
    contact_email: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Single choke-point so every recorder shares the same defensive behaviour."""
    try:
        # learning-loop-5: stamp the resolved sender email onto the learning_event so
        # per-sender count_events works. The raw pending_actions row carries no
        # contact_email, so _contact_email_for alone returned '' and every skip landed as
        # a global (blank-contact) event — which is exactly what let the rule proposer
        # aggregate unrelated senders. _resolve_email falls back to the decision_log row
        # keyed by message_id so the event is correctly attributed to its sender.
        email = (contact_email or "").lower() or _resolve_email(conn, action_row)
        repo.record_event(
            conn,
            type=type,
            message_id=_message_id_of(action_row),
            action_id=_action_id_of(action_row),
            contact_email=email,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 - learning is best-effort, never fatal
        log.warning("record_event(%s) failed (non-fatal): %s", type, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Public recorders
# ─────────────────────────────────────────────────────────────────────────────
def record_approve(
    conn: sqlite3.Connection,
    action_row: Optional[sqlite3.Row],
    *,
    contact_email: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """You approved a draft as-is. Positive signal for the current behaviour.

    Captures the approved draft as a voice sample (it's a reply you were happy to
    send) and nudges the sender's importance up by 1. Fire-and-forget (P5c)."""
    _record(conn, type="approve", action_row=action_row,
            contact_email=contact_email, detail=detail)
    try:
        email = (contact_email or "").lower() or _resolve_email(conn, action_row)
        draft = str(_row_get(action_row, "draft_text", "") or "").strip()
        # learning-loop-4: validate / bound / per-contact-scope the captured voice sample.
        _maybe_capture_voice_sample(conn, email, draft)
        # learning-loop-2: capped, non-ratcheting importance bump.
        if email:
            _bump_importance_capped(conn, email)
    except Exception as exc:  # noqa: BLE001 - learning is best-effort, never fatal
        log.warning("record_approve feedback capture failed (non-fatal): %s", exc)
    _record_episode(conn, action_row, action="sent",
                    contact_email=contact_email, tier=_row_get(action_row, "tier"))


def record_edit(
    conn: sqlite3.Connection,
    action_row: Optional[sqlite3.Row],
    *,
    contact_email: str = "",
    new_text: str = "",
    original_text: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """You edited a draft before sending. Signal that the draft missed the mark.

    Stores a ``draft_edits`` row with the original draft, the final text, and a unified
    diff (segment-tagged) so the learning layer can see HOW your voice differs from the
    model's. ``original_text`` should be the pre-edit draft (the caller captures it
    before replacing it). Fire-and-forget (P5c)."""
    d: dict[str, Any] = dict(detail or {})
    if new_text:
        d.setdefault("edited_len", len(new_text))
        d.setdefault("edited_preview", new_text[:500])
    _record(conn, type="edit", action_row=action_row,
            contact_email=contact_email, detail=d or None)
    try:
        email = (contact_email or "").lower() or _resolve_email(conn, action_row)
        diff = "\n".join(difflib.unified_diff(
            (original_text or "").splitlines(), (new_text or "").splitlines(), lineterm=""
        ))
        repo.add_draft_edit(
            conn,
            message_id=_message_id_of(action_row),
            segment=_segment_for(conn, email),
            original_draft=original_text or "",
            final_draft=new_text or "",
            diff=diff,
        )
    except Exception as exc:  # noqa: BLE001 - learning is best-effort, never fatal
        log.warning("record_edit feedback capture failed (non-fatal): %s", exc)


def record_skip(
    conn: sqlite3.Connection,
    action_row: Optional[sqlite3.Row],
    *,
    contact_email: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """You skipped/declined a surfaced item. Repeated skips for one sender or
    category are the strongest signal that the system is surfacing noise. Stores a
    ``skip_log`` row for the learning layer. Fire-and-forget (P5c)."""
    _record(conn, type="skip", action_row=action_row,
            contact_email=contact_email, detail=detail)
    try:
        reason = ""
        if isinstance(detail, dict):
            reason = str(detail.get("reason", ""))
        repo.add_skip_log(
            conn,
            message_id=_message_id_of(action_row),
            tier=_row_get(action_row, "tier"),
            summary=str(_row_get(action_row, "summary", "") or ""),
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 - learning is best-effort, never fatal
        log.warning("record_skip feedback capture failed (non-fatal): %s", exc)
    _record_episode(conn, action_row, action="skipped",
                    contact_email=contact_email, tier=_row_get(action_row, "tier"))


def record_override(
    conn: sqlite3.Connection,
    action_row: Optional[sqlite3.Row] = None,
    *,
    contact_email: str = "",
    from_tier: Optional[int] = None,
    to_tier: Optional[int] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """You overrode the system's tier (e.g. told it to stop notifying, or to treat
    a sender as more/less important)."""
    d: dict[str, Any] = dict(detail or {})
    if from_tier is not None:
        d.setdefault("from_tier", int(from_tier))
    if to_tier is not None:
        d.setdefault("to_tier", int(to_tier))
    _record(conn, type="override", action_row=action_row,
            contact_email=contact_email, detail=d or None)


def record_undo(
    conn: sqlite3.Connection,
    action_row: Optional[sqlite3.Row] = None,
    *,
    contact_email: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """You undid an autonomous action. Signal that the action was wrong."""
    _record(conn, type="undo", action_row=action_row,
            contact_email=contact_email, detail=detail)


def record_pause(conn: sqlite3.Connection, *, paused: bool) -> None:
    """You paused or resumed the assistant. Recorded for the audit/learning trail."""
    _record(conn, type="pause", detail={"paused": bool(paused)})
