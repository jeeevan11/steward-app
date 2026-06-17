"""approval-telegram-1 / autosend-invariant-2 — dispatcher-level fold handling.

When a same-thread same-sender message folds into an already-rendered card, the dispatcher
must (a) constrain the fold to the SAME thread, (b) re-render the displayed Telegram card to
the merged draft (so the owner sees what will be sent), and (c) re-stamp the approval to the
merged draft + reply target (so begin_send accepts the merged draft and would refuse an
unseen swap). These tests exercise the dispatcher helpers directly (no LLM pipeline needed).
"""

from __future__ import annotations

import unittest

from assistant.action import dispatcher
from assistant.config import Settings
from assistant.models import Channel, Contact, Message, Thread
from assistant.storage import db, decision_log
from assistant.storage import repositories as repo


def _mkdb():
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _seed_decision(conn, message_id, sender, thread_id):
    conn.execute(
        "INSERT INTO decision_log (message_id, thread_id, ts, sender_email, final_tier) "
        "VALUES (?,?,strftime('%s','now'),?,2)",
        (message_id, thread_id, sender.lower()),
    )


def _thread(thread_id="thread_A", sender="boss@x.com"):
    msg = Message(id="im1", thread_id=thread_id, channel=Channel.GMAIL, sender_email=sender,
                  subject="Invoice", body_text="confirm?", recipients=["me@x.com"])
    return Thread(id=thread_id, channel=Channel.GMAIL, subject="Invoice", messages=[msg])


def _card(conn, message_id, thread_id, draft, tg_id="42"):
    aid = repo.create_pending(
        conn, idempotency_key=f"{message_id}:2", message_id=message_id, thread_id=thread_id,
        tier=2, kind="reply_draft", summary="reply", draft_text=draft)
    repo.set_pending_telegram_message(conn, aid, "chat", tg_id)
    return aid


class _EditNotifier:
    def __init__(self):
        self.edited = []

    def edit_approval(self, message_id, action_id, signal, draft_text, **kw):
        self.edited.append({"message_id": message_id, "action_id": action_id,
                            "draft": draft_text})
        return message_id


class TestFoldRerender(unittest.TestCase):
    def test_same_thread_fold_returns_parent(self):
        conn = _mkdb()
        thread = _thread()
        _seed_decision(conn, "im1", "boss@x.com", "thread_A")
        aid = _card(conn, "im1", "thread_A", "Yes, approved.")
        contact = Contact(email="boss@x.com", name="Boss")
        folded = dispatcher._maybe_fold(conn, contact, thread, "im2", "new sum", "Cancel it.")
        self.assertEqual(folded, aid)
        # The fold mutated the draft and invalidated the prior approval.
        row = repo.get_pending(conn, aid)
        self.assertEqual(row["draft_text"], "Cancel it.")
        self.assertIsNone(row["approval_hash"])

    def test_cross_thread_does_not_fold(self):
        conn = _mkdb()
        # The open card is on thread_A; the new message belongs to thread_B.
        _seed_decision(conn, "im1", "boss@x.com", "thread_A")
        _card(conn, "im1", "thread_A", "Yes, approved.")
        thread_b = _thread(thread_id="thread_B")
        contact = Contact(email="boss@x.com", name="Boss")
        folded = dispatcher._maybe_fold(conn, contact, thread_b, "im2", "house offer", "Re: house")
        self.assertIsNone(folded)   # no cross-thread misroute

    def test_rerender_edits_card_and_restamps(self):
        conn = _mkdb()
        thread = _thread()
        _seed_decision(conn, "im1", "boss@x.com", "thread_A")
        aid = _card(conn, "im1", "thread_A", "Yes, approved.")
        contact = Contact(email="boss@x.com", name="Boss")
        merged = "On reflection, please hold the wire."
        dispatcher._maybe_fold(conn, contact, thread, "im2", "merged sum", merged)
        notifier = _EditNotifier()
        dispatcher._rerender_folded_card(
            conn, Settings(mode="live", gmail_address="me@x.com"), notifier, aid, thread,
            signal="merged sum", draft=merged, card_sender="Boss", mail="", quote="",
            conv_ctx="", source_url="", source_label="")
        # The displayed card was rewritten to the merged draft...
        self.assertEqual(len(notifier.edited), 1)
        self.assertEqual(notifier.edited[0]["draft"], merged)
        self.assertEqual(notifier.edited[0]["message_id"], "42")
        # ...and the approval was re-stamped to the merged draft (so begin_send accepts it,
        # and now equals what is on screen).
        self.assertTrue(repo.approval_matches(conn, aid))
        self.assertIsNotNone(repo.get_pending(conn, aid)["approval_hash"])


if __name__ == "__main__":
    unittest.main()
