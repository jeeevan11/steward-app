"""NO_PLACEHOLDER_SENT (drafting-safety-1): a holding/placeholder draft can never be sent
verbatim. The guard is on the SEND path (execute_send / execute_compose_send), not only at
draft time, so an LLM-outage holding draft or any unresolved sentinel is refused even after a
human taps Approve.

Stdlib + in-memory DB + fake injected mail, mirroring tests/test_personal_guardrail.
"""

from __future__ import annotations

import json
import unittest

from assistant.action import gmail_actions
from assistant.action import quality_gate
from assistant.config import Settings
from assistant.models import Channel, Message, Thread
from assistant.storage import db, decision_log
from assistant.storage import repositories as repo

# The exact holding draft drafting._holding_draft emits during an LLM outage.
_HOLDING = (
    "Hi Alex,\n\n"
    "[Could not auto-draft a reply — please write the response.]\n"
    "[Re: your message]\n"
    "[Key points to cover: PLACEHOLDER]\n\n"
    "Best,\n[your name]"
)


def _mkdb():
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _thread():
    msg = Message(id="im1", thread_id="t1", sender_email="alex@x.com", subject="Hi",
                  body_text="hello")
    return Thread(id="t1", channel=Channel.GMAIL, subject="Hi", messages=[msg])


class _FakeMail:
    def __init__(self, thread):
        self.sent = []
        self._thread = thread

    def source_for(self, mid):
        return self

    def get_thread(self, mid):
        return self._thread

    def send_reply(self, **kw):
        self.sent.append(kw)
        return "sent-id"


class _Notifier:
    def __init__(self):
        self.errors = []

    def error(self, t):
        self.errors.append(t)
        return "1"


class TestPlaceholderReason(unittest.TestCase):
    def test_holding_draft_flagged(self):
        self.assertTrue(quality_gate.placeholder_reason(_HOLDING))

    def test_your_name_flagged(self):
        self.assertTrue(quality_gate.placeholder_reason("Thanks!\n\nBest,\n[your name]"))

    def test_allcaps_bracket_flagged(self):
        self.assertTrue(quality_gate.placeholder_reason("Send the [PLACEHOLDER] now."))

    def test_normal_draft_not_flagged(self):
        self.assertFalse(quality_gate.placeholder_reason(
            "Hi Alex, confirmed for 3pm. See you then."))

    def test_benign_bracket_aside_not_flagged(self):
        # A lowercase bracketed aside is NOT a placeholder sentinel.
        self.assertFalse(quality_gate.placeholder_reason("Done [see attached] — thanks."))


class TestSendPathPlaceholderGuard(unittest.TestCase):
    def _approved(self, conn, draft):
        aid = repo.create_pending(
            conn, idempotency_key="im1:2", message_id="im1", thread_id="t1",
            tier=2, kind="reply_draft", summary="reply", draft_text=draft)
        repo.mark_approved(conn, aid)
        return aid

    def test_holding_draft_refused_on_reply_send(self):
        conn = _mkdb()
        aid = self._approved(conn, _HOLDING)
        mail = _FakeMail(_thread())
        notifier = _Notifier()
        ok = gmail_actions.execute_send(conn, mail, Settings(mode="live"), aid, notifier=notifier)
        self.assertFalse(ok)
        self.assertEqual(len(mail.sent), 0)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")
        self.assertTrue(notifier.errors)

    def test_placeholder_refused_even_in_dry_run(self):
        conn = _mkdb()
        aid = self._approved(conn, "Hi,\n\nBest,\n[your name]")
        ok = gmail_actions.execute_send(conn, _FakeMail(_thread()), Settings(mode="dry_run"), aid)
        self.assertFalse(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")

    def test_clean_draft_still_sends(self):
        conn = _mkdb()
        aid = self._approved(conn, "Hi Alex, confirmed for 3pm.")
        ok = gmail_actions.execute_send(conn, _FakeMail(_thread()), Settings(mode="live"), aid)
        self.assertTrue(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SENT")

    def test_compose_placeholder_refused(self):
        conn = _mkdb()
        aid = repo.create_pending(
            conn, idempotency_key="compose_x", message_id="compose_x", thread_id="",
            tier=2, kind="compose", summary="compose", draft_text=_HOLDING)
        conn.execute("UPDATE pending_actions SET compose_meta=? WHERE id=?",
                     (json.dumps({"channel": "gmail", "to": ["a@x.com"], "subject": "Hi"}), aid))
        repo.mark_approved(conn, aid)
        mail = _FakeMail(_thread())
        ok = gmail_actions.execute_compose_send(conn, mail, Settings(mode="live"), aid)
        self.assertFalse(ok)
        self.assertEqual(len(mail.sent), 0)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")


if __name__ == "__main__":
    unittest.main()
