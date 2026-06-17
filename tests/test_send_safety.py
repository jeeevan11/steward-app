"""Phase 4 — EXACTLY_ONCE_SEND. Regression + failure-injection for the send path.

Findings closed: `autosend-invariant-1`, `storage-persistence-4`, `storage-persistence-5`.

The defect: a single try/except wrapped BOTH the pre-send build and the irreversible
provider send, so ANY failure (including a lost/timed-out ACK after delivery, or a DB
lock on `mark_sent` after delivery) landed the row in the re-sendable `SEND_FAILED`
state. A one-tap Retry then double-sent an already-delivered, irreversible message.

The fix splits the boundary:
  * a failure BEFORE the provider send  -> SEND_FAILED   (provably not sent; retryable)
  * a failure AT/AFTER the provider send -> SEND_AMBIGUOUS (maybe delivered; NEVER auto-resent)
SEND_AMBIGUOUS is in no sendable set, so mark_approved / begin_send / set_pending_draft
all refuse it; the only exit is the explicit force_resend_after_ambiguous.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest import mock

from assistant.action import gmail_actions
from assistant.config import Settings
from assistant.models import Channel, Message, Thread
from assistant.storage import db, decision_log
from assistant.storage import repositories as repo


def _mkdb():
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _thread():
    m = Message(id="m1", thread_id="t", channel=Channel.GMAIL,
                sender_email="alice@acme.com", sender_name="Alice", subject="Hi")
    return Thread(id="t", subject="Hi", messages=[m])


def _approved(conn, key="k1", message_id="m1", kind="reply_draft"):
    aid = repo.create_pending(conn, idempotency_key=key, message_id=message_id,
                              thread_id="t", tier=2, kind=kind, summary="s",
                              draft_text="hello")
    repo.mark_approved(conn, aid)  # PENDING -> APPROVED (sendable)
    return aid


class FakeMail:
    """A MailSource whose failures can be injected at a chosen point."""

    def __init__(self, *, thread=None, get_thread_exc=None, send_exc=None, sent_id="sent-1"):
        self.thread = thread
        self.get_thread_exc = get_thread_exc
        self.send_exc = send_exc
        self.sent_id = sent_id
        self.sent: list[dict] = []

    def source_for(self, mid):
        return self

    def get_thread(self, mid):
        if self.get_thread_exc is not None:
            raise self.get_thread_exc
        return self.thread

    def send_reply(self, **kw):
        if self.send_exc is not None:
            raise self.send_exc        # raised AFTER the provider may have delivered
        self.sent.append(kw)
        return self.sent_id


class FakeNotifier:
    def __init__(self):
        self.errors: list[str] = []

    def error(self, text):
        self.errors.append(text)
        return "tg-1"


_LIVE = Settings(mode="live", gmail_address="me@x.com")


class TestSendSafety(unittest.TestCase):
    def setUp(self):
        self.conn = _mkdb()

    def tearDown(self):
        self.conn.close()

    # ── pre-send failure: provably not sent → SEND_FAILED, retryable ──
    def test_presend_build_failure_is_retryable_send_failed(self):
        aid = _approved(self.conn)
        note = FakeNotifier()
        mail = FakeMail(get_thread_exc=RuntimeError("gmail get_thread 503"))
        ok = gmail_actions.execute_send(self.conn, mail, _LIVE, aid, notifier=note)
        self.assertFalse(ok)
        self.assertEqual(repo.get_pending(self.conn, aid)["status"], "SEND_FAILED")
        self.assertEqual(mail.sent, [])  # provider was never called
        # retryable: the normal retry path can re-approve and send
        self.assertTrue(repo.mark_approved(self.conn, aid, via="telegram"))
        self.assertTrue(repo.begin_send(self.conn, aid))
        self.assertTrue(any("Failed to send" in e for e in note.errors))

    # ── send raises after dispatch: maybe-delivered → SEND_AMBIGUOUS, NOT resendable ──
    def test_send_raise_is_ambiguous_and_cannot_be_resent(self):
        aid = _approved(self.conn)
        note = FakeNotifier()
        mail = FakeMail(thread=_thread(), send_exc=TimeoutError("read timed out after dispatch"))
        ok = gmail_actions.execute_send(self.conn, mail, _LIVE, aid, notifier=note)
        self.assertFalse(ok)
        self.assertEqual(repo.get_pending(self.conn, aid)["status"], "SEND_AMBIGUOUS")
        # The crux: a generic Retry tap (appr:) can NEVER revive an ambiguous send.
        self.assertFalse(repo.mark_approved(self.conn, aid, via="telegram"))
        self.assertFalse(repo.begin_send(self.conn, aid))
        self.assertFalse(repo.set_pending_draft(self.conn, aid, "edited"))
        self.assertTrue(any("did NOT resend" in e for e in note.errors))

    # ── delivered but DB write fails: must NOT become re-sendable (storage-persistence-5) ──
    def test_db_lock_after_delivery_is_ambiguous_not_failed(self):
        aid = _approved(self.conn)
        mail = FakeMail(thread=_thread(), sent_id="gmail-xyz")
        with mock.patch.object(repo, "mark_sent",
                               side_effect=sqlite3.OperationalError("database is locked")):
            ok = gmail_actions.execute_send(self.conn, mail, _LIVE, aid)
        self.assertFalse(ok)
        row = repo.get_pending(self.conn, aid)
        self.assertEqual(row["status"], "SEND_AMBIGUOUS")     # NOT SEND_FAILED
        self.assertEqual(row["sent_gmail_id"], "gmail-xyz")   # delivery id captured for the human
        self.assertEqual(len(mail.sent), 1)                   # delivered exactly once
        self.assertFalse(repo.begin_send(self.conn, aid))     # never re-sent

    # ── happy path still sends exactly once ──
    def test_happy_path_sends_once_and_marks_sent(self):
        aid = _approved(self.conn)
        mail = FakeMail(thread=_thread(), sent_id="gmail-1")
        ok = gmail_actions.execute_send(self.conn, mail, _LIVE, aid)
        self.assertTrue(ok)
        row = repo.get_pending(self.conn, aid)
        self.assertEqual(row["status"], "SENT")
        self.assertEqual(row["sent_gmail_id"], "gmail-1")
        self.assertEqual(len(mail.sent), 1)

    # ── post-send bookkeeping failure must not undo a delivered send ──
    def test_bookkeeping_failure_keeps_send_sent(self):
        aid = _approved(self.conn)
        mail = FakeMail(thread=_thread(), sent_id="gmail-2")
        with mock.patch.object(repo, "log_action", side_effect=RuntimeError("audit down")):
            ok = gmail_actions.execute_send(self.conn, mail, _LIVE, aid)
        self.assertTrue(ok)
        self.assertEqual(repo.get_pending(self.conn, aid)["status"], "SENT")

    # ── explicit resolution is the ONLY way out of ambiguous ──
    def test_force_resend_only_from_ambiguous(self):
        aid = _approved(self.conn)
        mail = FakeMail(thread=_thread(), send_exc=TimeoutError("after dispatch"))
        gmail_actions.execute_send(self.conn, mail, _LIVE, aid)
        self.assertEqual(repo.get_pending(self.conn, aid)["status"], "SEND_AMBIGUOUS")
        # human says "I checked, it didn't arrive" -> back to a single guarded send
        self.assertTrue(repo.force_resend_after_ambiguous(self.conn, aid))
        self.assertEqual(repo.get_pending(self.conn, aid)["status"], "APPROVED")
        self.assertTrue(repo.begin_send(self.conn, aid))     # now SENDING
        # not applicable from a non-ambiguous state
        self.assertFalse(repo.force_resend_after_ambiguous(self.conn, aid))

    # ── compose path has the identical guarantee ──
    def test_compose_send_raise_is_ambiguous(self):
        aid = repo.create_pending(self.conn, idempotency_key="c1", message_id="compose_1",
                                  thread_id="", tier=2, kind="compose", summary="s",
                                  draft_text="hi")
        self.conn.execute(
            "UPDATE pending_actions SET compose_meta=? WHERE id=?",
            (json.dumps({"channel": "gmail", "to": ["x@y.com"], "subject": "S"}), aid),
        )
        repo.mark_approved(self.conn, aid)
        mail = FakeMail(send_exc=TimeoutError("after dispatch"))
        ok = gmail_actions.execute_compose_send(self.conn, mail, _LIVE, aid)
        self.assertFalse(ok)
        self.assertEqual(repo.get_pending(self.conn, aid)["status"], "SEND_AMBIGUOUS")
        self.assertFalse(repo.begin_send(self.conn, aid))


if __name__ == "__main__":
    unittest.main()
