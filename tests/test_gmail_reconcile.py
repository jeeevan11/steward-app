"""Gmail cross-surface: the poller reconciles replies it can't see pushed to it.

`fetch_new_message_ids` is INBOX-only, so when the owner replies straight from Gmail the
drafted card sits stale forever. `_reconcile_owner_replies` re-reads the FULL thread per
open card and, when the latest message is the owner's own, closes the card
HANDLED_ELSEWHERE — a CLOSE, never a send. This is the "Chinese supplier" bug: owner sent
"Let's stop and call them" in Gmail, the card never updated.
"""

from __future__ import annotations

import json
import unittest

from assistant import main as engine
from assistant.config import Settings
from assistant.models import Channel, Message, Thread
from assistant.storage import db
from assistant.storage import repositories as repo


def _pending(conn, key, thread_id, *, kind="reply_draft", status="PENDING"):
    aid = repo.create_pending(conn, idempotency_key=key, message_id=key, thread_id=thread_id,
                              tier=2, kind=kind, summary="s", draft_text="d")
    if status != "PENDING":
        conn.execute("UPDATE pending_actions SET status=? WHERE id=?", (status, aid))
    return aid


def _status(conn, aid):
    return conn.execute("SELECT status FROM pending_actions WHERE id=?", (aid,)).fetchone()["status"]


class _FakeNotifier:
    def __init__(self):
        self.texts: list[str] = []

    def send_text(self, text):
        self.texts.append(text)
        return "tg-1"


class _FakeMail:
    """get_thread returns the FULL thread, including the owner's Sent reply — exactly what
    Gmail's get_thread does (it fetches by thread id, not the INBOX-only fetch)."""

    def __init__(self, threads):
        self._threads = threads          # message_id -> Thread

    def get_thread(self, message_id):
        t = self._threads.get(message_id)
        if t is None:
            raise KeyError(message_id)
        return t


def _thread(tid, *, last_from_me, sender_name="Supplier", sender_email="sup@cn.com"):
    inbound = Message(id="in1", thread_id=tid, channel=Channel.GMAIL,
                      sender_email=sender_email, sender_name=sender_name,
                      subject="Order", from_me=False)
    msgs = [inbound]
    if last_from_me:
        msgs.append(Message(id="out1", thread_id=tid, channel=Channel.GMAIL,
                            subject="Order", body_text="Let's stop and call them.", from_me=True))
    return Thread(id=tid, subject="Order", messages=msgs)


class TestGmailReconcile(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.settings = Settings(mode="live", gmail_address="me@x.com")
        self.notifier = _FakeNotifier()
        engine._last_owner_reconcile = 0.0   # defeat the throttle for each test

    def tearDown(self):
        self.conn.close()

    def _run(self, mail):
        engine._last_owner_reconcile = 0.0
        engine._reconcile_owner_replies(self.conn, self.settings, mail, self.notifier)

    def test_owner_reply_in_gmail_closes_the_card(self):
        aid = _pending(self.conn, "msgA", "thrA")
        mail = _FakeMail({"msgA": _thread("thrA", last_from_me=True)})
        self._run(mail)
        self.assertEqual(_status(self.conn, aid), "HANDLED_ELSEWHERE")
        self.assertEqual(len(self.notifier.texts), 1)
        self.assertIn("Supplier", self.notifier.texts[0])

    def test_reconcile_closed_card_is_terminal_unsendable(self):
        # Drive the REAL sweep to close the card, then prove the SAME aid the sweep
        # touched can never be approved into a second send — NO_AUTO_SEND / EXACTLY_ONCE
        # survive the reconcile entry point, not just the repo helper in isolation.
        aid = _pending(self.conn, "msgA", "thrA")
        mail = _FakeMail({"msgA": _thread("thrA", last_from_me=True)})
        self._run(mail)
        self.assertEqual(_status(self.conn, aid), "HANDLED_ELSEWHERE")
        self.assertFalse(repo.mark_approved(self.conn, aid))
        self.assertFalse(repo.begin_send(self.conn, aid))

    def test_reconcile_writes_audit_event(self):
        # The handled_elsewhere learning_events row is the durable record of WHY a card
        # left the queue (count/channel feed the metrics-honesty promise). Lock it.
        aid = _pending(self.conn, "msgA", "thrA")
        mail = _FakeMail({"msgA": _thread("thrA", last_from_me=True)})
        self._run(mail)
        rows = self.conn.execute(
            "SELECT detail FROM learning_events WHERE type='handled_elsewhere'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["channel"], "gmail")
        self.assertEqual(detail["count"], 1)
        self.assertEqual(detail["thread_id"], "thrA")

    def test_card_stays_live_when_they_replied_last(self):
        aid = _pending(self.conn, "msgA", "thrA")
        mail = _FakeMail({"msgA": _thread("thrA", last_from_me=False)})
        self._run(mail)
        self.assertEqual(_status(self.conn, aid), "PENDING")
        self.assertEqual(self.notifier.texts, [])
        # no close → no audit row written
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM learning_events WHERE type='handled_elsewhere'"
            ).fetchone()["n"],
            0,
        )

    def test_whatsapp_card_is_skipped(self):
        # wa_* ids belong to the WhatsApp poller's own cross-surface path; get_thread is
        # never called for them (a KeyError here would surface a wrong routing).
        aid = _pending(self.conn, "wa_in_1", "919812345678@s.whatsapp.net")
        mail = _FakeMail({})   # empty: any get_thread call would KeyError
        self._run(mail)
        self.assertEqual(_status(self.conn, aid), "PENDING")

    def test_reminder_card_is_skipped(self):
        # a reminder's message_id is a chat id, not a Gmail message id — must not be fetched.
        aid = _pending(self.conn, "chat-123", "chat-123", kind="reminder")
        mail = _FakeMail({})
        self._run(mail)
        self.assertEqual(_status(self.conn, aid), "PENDING")

    def test_empty_thread_id_card_is_skipped_without_fetch(self):
        # A self-authored follow-up (e.g. a commitment "Draft follow-up" card) has a real
        # Gmail message_id but NO thread_id. It must be skipped BEFORE get_thread — it's not
        # a reply awaiting the owner, and resolve_handled_elsewhere keys on thread_id, so the
        # fallback key would mis-close OTHER cards. Empty _FakeMail → any fetch would KeyError.
        aid = _pending(self.conn, "msgA", "")
        mail = _FakeMail({})
        self._run(mail)
        self.assertEqual(_status(self.conn, aid), "PENDING")
        self.assertEqual(self.notifier.texts, [])

    def test_approved_card_is_not_yanked(self):
        # owner is mid-approval via Steward; reconcile must not pull it out from under them.
        aid = _pending(self.conn, "msgA", "thrA", status="APPROVED")
        mail = _FakeMail({"msgA": _thread("thrA", last_from_me=True)})
        self._run(mail)
        self.assertEqual(_status(self.conn, aid), "APPROVED")

    def test_one_fetch_covers_sibling_cards(self):
        a = _pending(self.conn, "msgA", "thrA")
        b = _pending(self.conn, "msgB", "thrA")
        # Only msgA is fetchable; if the sweep tried msgB it would KeyError. The first fetch
        # marks thrA seen AND resolve_handled_elsewhere closes every PENDING row on the thread.
        mail = _FakeMail({"msgA": _thread("thrA", last_from_me=True)})
        self._run(mail)
        self.assertEqual(_status(self.conn, a), "HANDLED_ELSEWHERE")
        self.assertEqual(_status(self.conn, b), "HANDLED_ELSEWHERE")

    def test_throttle_blocks_a_second_pass(self):
        aid = _pending(self.conn, "msgA", "thrA")
        mail = _FakeMail({"msgA": _thread("thrA", last_from_me=False)})
        engine._last_owner_reconcile = 0.0
        engine._reconcile_owner_replies(self.conn, self.settings, mail, self.notifier)
        # second call immediately after is throttled — even though they now replied, no work.
        self.conn.execute("UPDATE pending_actions SET thread_id='thrA' WHERE id=?", (aid,))
        mail2 = _FakeMail({"msgA": _thread("thrA", last_from_me=True)})
        engine._reconcile_owner_replies(self.conn, self.settings, mail2, self.notifier)
        self.assertEqual(_status(self.conn, aid), "PENDING")   # throttled, untouched


if __name__ == "__main__":
    unittest.main()
