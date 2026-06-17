"""The OTHER half of the Gmail cross-surface fix: the dispatch-time guard inside
process_one. When the latest message on a thread is the owner's OWN (they already
replied — in Gmail, on their phone, anywhere), Steward must NOT draft a reply: it
closes any open card HANDLED_ELSEWHERE and completes without ever touching the LLM.

The reconcile sweep (_reconcile_owner_replies) is covered by test_gmail_reconcile.py;
this locks the in-line guard, which runs on the live drafting path. A tripwire LLM
proves the suppress path never classifies/drafts, and the close is proven terminal
(no second send) — the NO_AUTO_SEND / EXACTLY_ONCE invariants survive this entry point.
"""

from __future__ import annotations

import unittest

from assistant import main as orchestrator
from assistant.config import Settings
from assistant.ingest.base import MailSource
from assistant.models import Message, Thread
from assistant.storage import db, ledger
from assistant.storage import repositories as repo


def _settings() -> Settings:
    return Settings(
        openrouter_api_key="x", telegram_bot_token="x", telegram_chat_id="1",
        gmail_address="me@x.com", mode="dry_run", db_path=":memory:",
        prompts_dir="./prompts",
    )


class _TripwireLLM:
    """Any classify/draft/noise call on the suppress path is a test failure — the
    guard must short-circuit BEFORE the brain is ever consulted (and before we spend
    a token on a thread that has already moved on)."""

    def __init__(self):
        self.calls: list[str] = []

    def noise_pass(self, **kw):
        self.calls.append("noise")
        raise AssertionError("LLM.noise_pass called on the owner-replied suppress path")

    def classify(self, **kw):
        self.calls.append("classify")
        raise AssertionError("LLM.classify called on the owner-replied suppress path")

    def draft(self, **kw):
        self.calls.append("draft")
        raise AssertionError("LLM.draft called on the owner-replied suppress path")

    def complete_text(self, **kw):
        return ""


class _FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, tuple]] = []

    def _rec(self, kind, *a):
        self.sent.append((kind, a))
        return "tg-1"

    def send_text(self, text): return self._rec("text", text)
    def fyi(self, text): return self._rec("fyi", text)
    def send_approval(self, action_id, summary, draft, **kw): return self._rec("approval", action_id, summary, draft)
    def send_ask(self, action_id, summary, suggestion, **kw): return self._rec("ask", action_id, summary, suggestion)
    def error(self, text): return self._rec("error", text)


class _FakeMail(MailSource):
    def __init__(self, thread: Thread, claim_id: str):
        self._thread = thread
        self._claim_id = claim_id
        self._served = False

    def connect(self): pass

    def fetch_new_message_ids(self):
        if self._served:
            return []
        self._served = True
        return [self._claim_id]

    def get_thread(self, message_id): return self._thread
    def archive(self, message_id): return {"op": "archive", "message_id": message_id}
    def apply_label(self, message_id, label): return {"op": "label"}
    def undo(self, undo_data): pass

    def send_reply(self, *, thread_id, to, cc, subject, body, in_reply_to_gmail_id):
        raise AssertionError("send_reply must never be called on the suppress path")


def _owner_replied_thread() -> Thread:
    """A thread whose LATEST message is the owner's own reply (they answered in Gmail)."""
    them = Message(id="m1", thread_id="t1", sender_email="boss@x.com", sender_name="Boss",
                   recipients=["me@x.com"], subject="Quick ask", body_text="Can you confirm Friday?",
                   from_me=False)
    me = Message(id="m2", thread_id="t1", sender_email="me@x.com", sender_name="Me",
                 recipients=["boss@x.com"], subject="Quick ask",
                 body_text="Yep, Friday works. Let's stop and call them.", from_me=True)
    return Thread(id="t1", subject="Quick ask", messages=[them, me])


def _category(conn, message_id):
    row = conn.execute(
        "SELECT category FROM processed_messages WHERE message_id=?", (message_id,)
    ).fetchone()
    return row["category"] if row else None


def _status(conn, aid):
    return conn.execute("SELECT status FROM pending_actions WHERE id=?", (aid,)).fetchone()["status"]


class TestProcessOneOwnerReplied(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.settings = _settings()
        self.notifier = _FakeNotifier()

    def tearDown(self):
        self.conn.close()

    def test_owner_replied_last_closes_card_and_never_drafts(self):
        # An open card on the thread, created before the owner answered directly in Gmail.
        aid = repo.create_pending(self.conn, idempotency_key="m1", message_id="m1", thread_id="t1",
                                  tier=2, kind="reply_draft", summary="Boss wants Friday confirmed",
                                  draft_text="old draft")
        mail = _FakeMail(_owner_replied_thread(), claim_id="m1")

        orchestrator.poll_and_process(self.conn, self.settings, mail, _TripwireLLM(), self.notifier)

        # the message was completed (not stuck) and tagged owner_replied, tier 0
        self.assertTrue(ledger.is_done(self.conn, "m1"))
        self.assertEqual(_category(self.conn, "m1"), "owner_replied")
        # the stale card is CLOSED (a close, never a send)
        self.assertEqual(_status(self.conn, aid), "HANDLED_ELSEWHERE")
        # and exactly one human-facing "you replied yourself" notice was sent
        texts = [a for k, a in self.notifier.sent if k == "text"]
        self.assertEqual(len(texts), 1)
        self.assertIn("Boss", texts[0][0])

    def test_owner_replied_close_is_terminal_unsendable(self):
        aid = repo.create_pending(self.conn, idempotency_key="m1", message_id="m1", thread_id="t1",
                                  tier=2, kind="reply_draft", summary="s", draft_text="d")
        mail = _FakeMail(_owner_replied_thread(), claim_id="m1")
        orchestrator.poll_and_process(self.conn, self.settings, mail, _TripwireLLM(), self.notifier)
        self.assertEqual(_status(self.conn, aid), "HANDLED_ELSEWHERE")
        # NO_AUTO_SEND / EXACTLY_ONCE survive this entry point: the closed card can never
        # be approved into a second reply.
        self.assertFalse(repo.mark_approved(self.conn, aid))
        self.assertFalse(repo.begin_send(self.conn, aid))

    def test_no_open_card_still_suppresses_without_notifying(self):
        # Owner replied, but there was no open card (already handled) — still no draft,
        # and no spurious "cleared from your queue" notice.
        mail = _FakeMail(_owner_replied_thread(), claim_id="m1")
        orchestrator.poll_and_process(self.conn, self.settings, mail, _TripwireLLM(), self.notifier)
        self.assertTrue(ledger.is_done(self.conn, "m1"))
        self.assertEqual(_category(self.conn, "m1"), "owner_replied")
        self.assertEqual([a for k, a in self.notifier.sent if k == "text"], [])


if __name__ == "__main__":
    unittest.main()
