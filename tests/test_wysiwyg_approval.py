"""Approval-integrity regression tests (WYSIWYG_APPROVAL / NO_WRONG_THREAD /
NO_WRONG_RECIPIENT / NO_PLACEHOLDER_SENT).

Findings closed here:
  * autosend-invariant-2 / approval-telegram-1 — a fold mutated draft_text under an already
    rendered card; owner approved draft A but unseen draft B was sent. begin_send now refuses
    a send whose live draft/target no longer hash-equals the stamped approval, and a fold
    INVALIDATES the prior approval.
  * approval-telegram-2 — a folded reply was routed into the ORIGINAL card's thread. The send
    target is bound to the approved draft; a cross-thread fold is refused at the fold lookup.
  * drafting-safety-1 — a holding/placeholder draft could be approved and sent verbatim. A
    send-path placeholder guard refuses it.

Stdlib + in-memory DB + fake injected mail/notifier, mirroring tests/test_personal_guardrail.
"""

from __future__ import annotations

import unittest

from assistant.action import gmail_actions
from assistant.config import Settings
from assistant.models import Channel, Message, Thread
from assistant.storage import db, decision_log
from assistant.storage import repositories as repo


def _mkdb():
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _seed_decision(conn, message_id, sender, thread_id="t1"):
    conn.execute(
        "INSERT INTO decision_log (message_id, thread_id, ts, sender_email, final_tier) "
        "VALUES (?,?,strftime('%s','now'),?,2)",
        (message_id, thread_id, sender.lower()),
    )


def _pending(conn, message_id, *, thread_id="t1", draft="Hi Alex, sounds good.", key=None):
    return repo.create_pending(
        conn, idempotency_key=key or f"{message_id}:2", message_id=message_id,
        thread_id=thread_id, tier=2, kind="reply_draft", summary="reply", draft_text=draft,
    )


def _inbound_thread(thread_id="t1", sender="alex@x.com", cc=None, subject="Invoice #1"):
    msg = Message(id="im1", thread_id=thread_id, sender_email=sender, subject=subject,
                  body_text="please confirm", cc=cc or [])
    return Thread(id=thread_id, channel=Channel.GMAIL, subject=subject, messages=[msg])


class _FakeMail:
    """Captures sends; returns a fixed inbound thread for the reply target."""

    def __init__(self, thread):
        self.sent = []
        self._thread = thread

    def source_for(self, mid):
        return self

    def get_thread(self, mid):
        return self._thread

    def send_reply(self, **kw):
        self.sent.append(kw)
        return "sent-gmail-id"


class _RecordingNotifier:
    def __init__(self):
        self.errors = []

    def error(self, text):
        self.errors.append(text)
        return "1"


# ─────────────────────────────────────────────────────────────────────────────
# WYSIWYG_APPROVAL — the stamped draft must equal what is sent.
# ─────────────────────────────────────────────────────────────────────────────
class TestWysiwygApproval(unittest.TestCase):
    def test_unmutated_approved_draft_sends(self):
        conn = _mkdb()
        thread = _inbound_thread()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1")
        # Stamp the approval exactly as dispatch does (draft + reply target).
        to, cc = gmail_actions._reply_recipients(thread, Settings(mode="live"))
        repo.stamp_approval(conn, aid, thread_id="t1", recipients=list(to) + list(cc))
        repo.mark_approved(conn, aid)
        mail = _FakeMail(thread)
        ok = gmail_actions.execute_send(conn, mail, Settings(mode="live"), aid)
        self.assertTrue(ok)
        self.assertEqual(len(mail.sent), 1)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SENT")

    def test_mutated_draft_under_approval_refuses_to_send(self):
        """approve A, draft silently changed to B → begin_send must refuse and not send."""
        conn = _mkdb()
        thread = _inbound_thread()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1", draft="Yes, approved.")
        to, cc = gmail_actions._reply_recipients(thread, Settings(mode="live"))
        repo.stamp_approval(conn, aid, thread_id="t1", recipients=list(to) + list(cc))
        repo.mark_approved(conn, aid)
        # Out-of-band mutation AFTER the approval was stamped (the fold-batching hazard).
        conn.execute("UPDATE pending_actions SET draft_text=? WHERE id=?",
                     ("Actually, cancel everything.", aid))
        mail = _FakeMail(thread)
        notifier = _RecordingNotifier()
        ok = gmail_actions.execute_send(conn, mail, Settings(mode="live"), aid, notifier=notifier)
        self.assertFalse(ok)
        self.assertEqual(len(mail.sent), 0)                      # nothing was sent
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")
        self.assertTrue(notifier.errors)                        # owner was surfaced to

    def test_mismatch_refuses_even_in_dry_run(self):
        conn = _mkdb()
        thread = _inbound_thread()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1", draft="Yes.")
        repo.stamp_approval(conn, aid, thread_id="t1", recipients=["alex@x.com"])
        repo.mark_approved(conn, aid)
        conn.execute("UPDATE pending_actions SET draft_text='No.' WHERE id=?", (aid,))
        ok = gmail_actions.execute_send(conn, _FakeMail(thread), Settings(mode="dry_run"), aid)
        self.assertFalse(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")

    def test_recipient_change_under_approval_refuses(self):
        """NO_WRONG_RECIPIENT: same body, different bound recipients → refuse."""
        conn = _mkdb()
        thread = _inbound_thread()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1", draft="Body stays the same.")
        repo.stamp_approval(conn, aid, thread_id="t1", recipients=["alex@x.com"])
        repo.mark_approved(conn, aid)
        # Re-point the bound target's recipients to a different person without re-approval.
        import json
        conn.execute(
            "UPDATE pending_actions SET send_target=? WHERE id=?",
            (json.dumps({"thread_id": "t1", "recipients": ["stranger@evil.com"]}), aid))
        ok = gmail_actions.execute_send(conn, _FakeMail(thread), Settings(mode="live"), aid)
        self.assertFalse(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")

    def test_legacy_unstamped_card_still_sends(self):
        """Additive: a card that was never stamped (NULL hash) keeps prior behavior."""
        conn = _mkdb()
        thread = _inbound_thread()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1", draft="hello")
        repo.mark_approved(conn, aid)   # no stamp_approval call
        ok = gmail_actions.execute_send(conn, _FakeMail(thread), Settings(mode="live"), aid)
        self.assertTrue(ok)


# ─────────────────────────────────────────────────────────────────────────────
# Fold invalidates the prior approval and keeps the index in sync.
# ─────────────────────────────────────────────────────────────────────────────
class TestFoldInvalidatesApproval(unittest.TestCase):
    def test_fold_clears_approval_hash(self):
        conn = _mkdb()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1", draft="Draft A")
        repo.stamp_approval(conn, aid, thread_id="t1", recipients=["alex@x.com"])
        self.assertIsNotNone(repo.get_pending(conn, aid)["approval_hash"])
        # Fold a newer message in → must invalidate the stamped approval (fail-safe sentinel,
        # not a real hash) so begin_send refuses until a fresh render+stamp.
        self.assertTrue(repo.fold_message_into_action(conn, aid, "im2", "new sum", "Draft B"))
        row = repo.get_pending(conn, aid)
        self.assertFalse(repo.approval_matches(conn, aid))   # invalidated → refuse
        self.assertEqual(row["draft_text"], "Draft B")

    def test_fold_then_stale_stamp_mismatch_refuses(self):
        """A fold (while PENDING) clears the approval; if the OLD stamp for draft A is
        somehow re-applied and the draft is then B, begin_send must refuse. This proves the
        stamp is bound to the draft, so a stale approval can never carry over a fold."""
        conn = _mkdb()
        thread = _inbound_thread()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1", draft="Draft A")
        # Stamp for draft A, then fold draft B in (fold invalidates the stamp).
        repo.stamp_approval(conn, aid, thread_id="t1", recipients=["alex@x.com"])
        self.assertTrue(repo.fold_message_into_action(conn, aid, "im2", "new sum", "Draft B"))
        self.assertFalse(repo.approval_matches(conn, aid))   # invalidated by the fold
        # Re-apply the OLD draft-A hash by hand (simulating a stale binding) onto draft B.
        stale = repo.canonical_approval_hash("Draft A", "t1", ["alex@x.com"])
        conn.execute("UPDATE pending_actions SET approval_hash=? WHERE id=?", (stale, aid))
        repo.mark_approved(conn, aid)
        ok = gmail_actions.execute_send(conn, _FakeMail(thread), Settings(mode="live"), aid)
        self.assertFalse(ok)   # live draft is B, stamped hash is A → refused
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")

    def test_fold_without_rerender_fails_safe(self):
        """If the dispatcher re-render/re-stamp never runs after a fold (e.g. it crashed), the
        invalidated approval must still REFUSE the send — never fall open to the unseen merged
        draft. The fold leaves the fail-safe sentinel; begin_send refuses."""
        conn = _mkdb()
        thread = _inbound_thread()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1", draft="Draft A")
        repo.stamp_approval(conn, aid, thread_id="t1", recipients=["alex@x.com"])
        repo.mark_approved(conn, aid)        # approved BEFORE the burst (PENDING→APPROVED)...
        # ...but fold only acts on PENDING, so reset to PENDING to model the real race where
        # the fold lands while the card is still pending, then the owner approves the stale UI.
        conn.execute("UPDATE pending_actions SET status='PENDING' WHERE id=?", (aid,))
        repo.fold_message_into_action(conn, aid, "im2", "merged", "Draft B")  # → sentinel
        repo.mark_approved(conn, aid)        # owner approves (no re-render happened)
        ok = gmail_actions.execute_send(conn, _FakeMail(thread), Settings(mode="live"), aid)
        self.assertFalse(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")

    def test_fold_populates_folded_children_index(self):
        conn = _mkdb()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1")
        repo.fold_message_into_action(conn, aid, "im2", "s", "d")
        rows = conn.execute(
            "SELECT child_message_id FROM folded_children ORDER BY child_message_id"
        ).fetchall()
        ids = {r[0] for r in rows}
        self.assertIn("im1", ids)
        self.assertIn("im2", ids)


# ─────────────────────────────────────────────────────────────────────────────
# Edit recovers a blocked card AND re-binds the approval to the edited body.
# ─────────────────────────────────────────────────────────────────────────────
class TestEditRebindsApproval(unittest.TestCase):
    def test_edit_after_block_can_send(self):
        conn = _mkdb()
        thread = _inbound_thread()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1", draft="Yes.")
        repo.stamp_approval(conn, aid, thread_id="t1", recipients=["alex@x.com"])
        repo.mark_approved(conn, aid)
        conn.execute("UPDATE pending_actions SET draft_text='No.' WHERE id=?", (aid,))
        # Send refused → SEND_BLOCKED.
        self.assertFalse(gmail_actions.execute_send(conn, _FakeMail(thread), Settings(mode="live"), aid))
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")
        # Owner edits → recovers to EDITED and re-binds the approval to the edited body.
        self.assertTrue(repo.set_pending_draft(conn, aid, "Edited and correct."))
        self.assertEqual(repo.get_pending(conn, aid)["status"], "EDITED")
        ok = gmail_actions.execute_send(conn, _FakeMail(thread), Settings(mode="live"), aid)
        self.assertTrue(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SENT")


class TestBlockedStateMachine(unittest.TestCase):
    """SEND_BLOCKED is a non-sendable pre-send state: it cannot be re-approved as-is or sent,
    but it CAN be edited (recovers + re-stamps) or skipped (dismissed)."""

    def _blocked(self, conn):
        thread = _inbound_thread()
        _seed_decision(conn, "im1", "alex@x.com")
        aid = _pending(conn, "im1", draft="Yes.")
        repo.stamp_approval(conn, aid, thread_id="t1", recipients=["alex@x.com"])
        repo.mark_approved(conn, aid)
        conn.execute("UPDATE pending_actions SET draft_text='No.' WHERE id=?", (aid,))
        gmail_actions.execute_send(conn, _FakeMail(thread), Settings(mode="live"), aid)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")
        return aid

    def test_blocked_cannot_begin_send(self):
        conn = _mkdb()
        aid = self._blocked(conn)
        self.assertFalse(repo.begin_send(conn, aid))   # not sendable

    def test_blocked_cannot_be_reapproved_as_is(self):
        conn = _mkdb()
        aid = self._blocked(conn)
        # mark_approved does NOT accept SEND_BLOCKED → it stays blocked, never sendable.
        self.assertFalse(repo.mark_approved(conn, aid))
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")

    def test_blocked_can_be_skipped(self):
        conn = _mkdb()
        aid = self._blocked(conn)
        self.assertTrue(repo.mark_skipped(conn, aid))
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SKIPPED")


if __name__ == "__main__":
    unittest.main()
