"""The conversation model: one living card per thread (fold by conversation, not a 20-min
window), collapse stale siblings, carry the highest tier, and clear a card when the owner
replies on ANOTHER device (cross-surface). Fixes the 'Maya #158 + #164' stranding + the
double-reply risk.
"""

from __future__ import annotations

import time
import unittest

from assistant.storage import db, wa_messages
from assistant.storage import repositories as repo


def _pending(conn, key, thread_id, *, tier=2, status="PENDING", age_s=0):
    aid = repo.create_pending(conn, idempotency_key=key, message_id=key, thread_id=thread_id,
                              tier=tier, kind="reply_draft", summary="s", draft_text="d")
    if status != "PENDING":
        conn.execute("UPDATE pending_actions SET status=? WHERE id=?", (status, aid))
    if age_s:
        conn.execute("UPDATE pending_actions SET created_at=created_at-? WHERE id=?", (age_s, aid))
    return aid


def _status(conn, aid):
    return conn.execute("SELECT status FROM pending_actions WHERE id=?", (aid,)).fetchone()["status"]


def _tier(conn, aid):
    return conn.execute("SELECT tier FROM pending_actions WHERE id=?", (aid,)).fetchone()["tier"]


class TestThreadFold(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_finds_open_card_on_thread_regardless_of_age(self):
        # a card 2 hours old is still the fold target (the old 20-min window would have missed it)
        aid = _pending(self.conn, "m1", "thrA", age_s=2 * 3600)
        found = repo.find_open_action_for_thread(self.conn, "thrA")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], aid)
        # a different thread is never a target
        self.assertIsNone(repo.find_open_action_for_thread(self.conn, "thrB"))

    def test_does_not_fold_into_an_ancient_card(self):
        _pending(self.conn, "m1", "thrA", age_s=30 * 86400)   # 30 days old
        self.assertIsNone(repo.find_open_action_for_thread(self.conn, "thrA"))

    def test_fold_keeps_the_higher_tier(self):
        aid = _pending(self.conn, "m1", "thrA", tier=3)        # an important ask
        repo.fold_message_into_action(self.conn, aid, "m2", "s2", "d2", new_tier=1)  # trivial 'ok'
        self.assertEqual(_tier(self.conn, aid), 3)             # not downgraded
        repo.fold_message_into_action(self.conn, aid, "m3", "s3", "d3", new_tier=3)
        self.assertEqual(_tier(self.conn, aid), 3)

    def test_fold_raises_tier_when_a_later_message_is_more_important(self):
        aid = _pending(self.conn, "m1", "thrA", tier=1)
        repo.fold_message_into_action(self.conn, aid, "m2", "s2", "d2", new_tier=3)
        self.assertEqual(_tier(self.conn, aid), 3)

    def test_resolve_siblings_collapses_to_one_living_card(self):
        old = _pending(self.conn, "m_old", "thrA", age_s=2 * 3600)   # the stranded 'going to sleep'
        new = _pending(self.conn, "m_new", "thrA")                   # the newer card we keep
        superseded = repo.resolve_thread_siblings(self.conn, "thrA", new)
        self.assertEqual(superseded, [old])
        self.assertEqual(_status(self.conn, old), "SUPERSEDED")
        self.assertEqual(_status(self.conn, new), "PENDING")

    def test_superseded_is_terminal_and_unsendable(self):
        old = _pending(self.conn, "m_old", "thrA")
        repo.resolve_thread_siblings(self.conn, "thrA", -1)   # supersede all
        self.assertEqual(_status(self.conn, old), "SUPERSEDED")
        self.assertFalse(repo.mark_approved(self.conn, old))  # can't revive
        self.assertFalse(repo.begin_send(self.conn, old))

    def test_resolve_siblings_never_supersedes_a_tier3_ask(self):
        # CARDINAL: folding a newer (trivial) message into one card, or sending on the thread,
        # must NOT silently discard a SEPARATE open decision — the live ask #158 was lost this way.
        ask = repo.create_pending(self.conn, idempotency_key="a", message_id="a", thread_id="thrA",
                                  tier=3, kind="ask", summary="going to sleep?", draft_text="")
        draft = _pending(self.conn, "m_new", "thrA")            # a newer reply_draft card
        superseded = repo.resolve_thread_siblings(self.conn, "thrA", draft)
        self.assertNotIn(ask, superseded)                       # the ask is protected
        self.assertEqual(_status(self.conn, ask), "PENDING")    # decision stays open

    def test_fold_keeps_summary_when_a_trivial_message_folds_in(self):
        aid = _pending(self.conn, "m1", "thrA", tier=3)
        self.conn.execute("UPDATE pending_actions SET summary='need money for medicine' WHERE id=?", (aid,))
        # a trivial lower-tier follow-up folds in — tier stays 3, summary must NOT become "ok lol"
        repo.fold_message_into_action(self.conn, aid, "m2", "ok lol", "d2", new_tier=1)
        row = self.conn.execute("SELECT summary, tier FROM pending_actions WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row["summary"], "need money for medicine")
        self.assertEqual(row["tier"], 3)
        # an equally-important message DOES update the summary
        repo.fold_message_into_action(self.conn, aid, "m3", "also the rent is due", "d3", new_tier=3)
        self.assertEqual(
            self.conn.execute("SELECT summary FROM pending_actions WHERE id=?", (aid,)).fetchone()["summary"],
            "also the rent is due")


class TestCrossSurface(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        wa_messages.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_resolve_handled_elsewhere_closes_only_pending(self):
        jid = "919812345678@s.whatsapp.net"
        pend = _pending(self.conn, "m1", jid)
        sent = _pending(self.conn, "m2", "otherthread", status="SENT")
        closed = repo.resolve_handled_elsewhere(self.conn, jid)
        self.assertEqual([r["id"] for r in closed], [pend])
        self.assertEqual(_status(self.conn, pend), "HANDLED_ELSEWHERE")
        self.assertEqual(_status(self.conn, sent), "SENT")    # untouched

    def test_handled_elsewhere_cannot_be_resent(self):
        jid = "919812345678@s.whatsapp.net"
        pend = _pending(self.conn, "m1", jid)
        repo.resolve_handled_elsewhere(self.conn, jid)
        self.assertFalse(repo.mark_approved(self.conn, pend))  # no stale-approve -> no 2nd reply
        self.assertFalse(repo.begin_send(self.conn, pend))

    def test_tier3_ask_is_never_auto_closed_by_cross_surface(self):
        # CARDINAL: a decision that needs the owner (kind='ask') must NOT be silently dismissed
        # just because he messaged the chat — the live 'Sam $250k investor' incident.
        jid = "919812345678@s.whatsapp.net"
        draft = repo.create_pending(self.conn, idempotency_key="d1", message_id="d1", thread_id=jid,
                                    tier=2, kind="reply_draft", summary="chit-chat", draft_text="x")
        ask = repo.create_pending(self.conn, idempotency_key="a1", message_id="a1", thread_id=jid,
                                  tier=3, kind="ask", summary="Sam: did you get the $250k?", draft_text="")
        closed = repo.resolve_handled_elsewhere(self.conn, jid)
        self.assertEqual([r["id"] for r in closed], [draft])         # only the draft is cleared
        self.assertEqual(_status(self.conn, draft), "HANDLED_ELSEWHERE")
        self.assertEqual(_status(self.conn, ask), "PENDING")          # the decision stays open
        self.assertEqual([r["id"] for r in repo.open_asks_for_thread(self.conn, jid)], [ask])

    def test_ingest_outbound_clears_the_pending_card(self):
        from assistant.ingest.whatsapp_source import ingest_outbound
        jid = "919812345678@s.whatsapp.net"
        pend = _pending(self.conn, "m1", jid)
        ingest_outbound(self.conn, {"messageId": "out1", "jid": jid}, settings=None)
        self.assertEqual(_status(self.conn, pend), "HANDLED_ELSEWHERE")

    def test_group_post_does_not_clear_a_group_card(self):
        from assistant.ingest.whatsapp_source import ingest_outbound
        gjid = "120363000000000000@g.us"
        pend = _pending(self.conn, "m1", gjid)
        ingest_outbound(self.conn, {"messageId": "out1", "jid": gjid}, settings=None)
        self.assertEqual(_status(self.conn, pend), "PENDING")  # a group reply must not clear it


class TestSendResolvesSiblings(unittest.TestCase):
    """Replying to the latest message archives the rest of the conversation (post-send)."""

    def setUp(self):
        from assistant.storage import decision_log
        self.conn = db.open_db(":memory:")
        decision_log.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_live_send_supersedes_sibling_on_same_thread(self):
        from assistant.action import gmail_actions
        from assistant.config import Settings
        from assistant.models import Channel, Message, Thread

        sibling = _pending(self.conn, "m_old", "t")             # earlier stranded card
        sendable = _pending(self.conn, "m_new", "t")
        repo.mark_approved(self.conn, sendable)

        class FakeMail:
            def source_for(self, mid): return self
            def get_thread(self, mid):
                m = Message(id="m_new", thread_id="t", channel=Channel.GMAIL,
                            sender_email="a@x.com", sender_name="A", subject="Hi")
                return Thread(id="t", subject="Hi", messages=[m])
            def send_reply(self, **kw): return "sent-1"

        ok = gmail_actions.execute_send(self.conn, FakeMail(),
                                        Settings(mode="live", gmail_address="me@x.com"), sendable)
        self.assertTrue(ok)
        self.assertEqual(_status(self.conn, sendable), "SENT")
        self.assertEqual(_status(self.conn, sibling), "SUPERSEDED")   # conversation archived


if __name__ == "__main__":
    unittest.main()
