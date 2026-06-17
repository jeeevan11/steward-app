"""NO_WRONG_THREAD (approval-telegram-2): a folded reply must never be routed into the
ORIGINAL card's thread. Folding is constrained to the SAME thread_id, so a second message
from the same sender on a DIFFERENT thread can never overwrite the first card's draft/target.

Also covers the failure-recovery-2 send-start reaper clock.

Stdlib + in-memory DB, mirroring tests/test_conversation_batching.
"""

from __future__ import annotations

import time
import unittest

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


def _create(conn, message_id, thread_id, key=None):
    return repo.create_pending(
        conn, idempotency_key=key or f"{message_id}:2", message_id=message_id,
        thread_id=thread_id, tier=2, kind="reply_draft", summary="s", draft_text="d")


class TestCrossThreadFoldRefused(unittest.TestCase):
    def test_same_thread_folds(self):
        conn = _mkdb()
        _seed_decision(conn, "m1", "boss@x.com", "thread_A")
        aid = _create(conn, "m1", "thread_A")
        _seed_decision(conn, "m2", "boss@x.com", "thread_A")
        found = repo.find_open_action_for_sender(
            conn, "boss@x.com", within_seconds=1200, thread_id="thread_A")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], aid)

    def test_different_thread_does_not_fold(self):
        """The $50k-wire-vs-cancel-lunch misroute: a second email on thread_B from the same
        sender must NOT match the open thread_A card."""
        conn = _mkdb()
        _seed_decision(conn, "m1", "boss@x.com", "thread_A")
        _create(conn, "m1", "thread_A")
        _seed_decision(conn, "m2", "boss@x.com", "thread_B")
        found = repo.find_open_action_for_sender(
            conn, "boss@x.com", within_seconds=1200, thread_id="thread_B")
        self.assertIsNone(found)   # no cross-thread fold target → a fresh card is created

    def test_sender_only_lookup_still_available(self):
        """Omitting thread_id keeps the legacy sender-only behavior (no regression)."""
        conn = _mkdb()
        _seed_decision(conn, "m1", "boss@x.com", "thread_A")
        aid = _create(conn, "m1", "thread_A")
        _seed_decision(conn, "m2", "boss@x.com", "thread_B")
        found = repo.find_open_action_for_sender(conn, "boss@x.com", within_seconds=1200)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], aid)


class TestReaperKeysOnSendStart(unittest.TestCase):
    """failure-recovery-2: the reaper keys staleness on sending_started_at, not created_at."""

    def _sending(self, conn, key):
        aid = _create(conn, "m_" + key, "t", key=key)
        repo.mark_approved(conn, aid)
        self.assertTrue(repo.begin_send(conn, aid))   # stamps sending_started_at=now
        return aid

    def test_begin_send_stamps_send_start(self):
        conn = _mkdb()
        aid = self._sending(conn, "k1")
        row = repo.get_pending(conn, aid)
        self.assertIsNotNone(row["sending_started_at"])

    def test_old_card_just_approved_is_not_flagged(self):
        """A card CREATED an hour ago but whose send STARTED seconds ago must NOT be flagged
        — the exact false-alarm the old created_at keying produced."""
        conn = _mkdb()
        aid = self._sending(conn, "k1")
        # Age created_at to 60 min ago; leave sending_started_at = now.
        conn.execute("UPDATE pending_actions SET created_at=? WHERE id=?",
                     (int(time.time()) - 3600, aid))
        cutoff = int(time.time()) - 30 * 60
        ids = {r["id"] for r in repo.stuck_sending(conn, cutoff)}
        self.assertNotIn(aid, ids)   # healthy in-flight send, not stuck

    def test_genuinely_old_send_is_flagged(self):
        conn = _mkdb()
        aid = self._sending(conn, "k1")
        conn.execute("UPDATE pending_actions SET sending_started_at=? WHERE id=?",
                     (int(time.time()) - 3600, aid))
        cutoff = int(time.time()) - 30 * 60
        ids = {r["id"] for r in repo.stuck_sending(conn, cutoff)}
        self.assertIn(aid, ids)

    def test_folded_card_send_start_is_independent_of_created_at_reset(self):
        """A fold resets created_at, but the send-start clock (set at begin_send AFTER the
        fold + approval) is what the reaper uses — so a freshly-folded card whose send has
        genuinely been wedged is flagged on its real age, not delayed by the fold."""
        conn = _mkdb()
        aid = _create(conn, "m1", "t", key="k1")
        # Fold resets created_at to now (simulating the fold window refresh).
        repo.fold_message_into_action(conn, aid, "m2", "s", "d")
        repo.mark_approved(conn, aid)
        self.assertTrue(repo.begin_send(conn, aid))
        # The send has now been wedged a long time.
        conn.execute("UPDATE pending_actions SET sending_started_at=? WHERE id=?",
                     (int(time.time()) - 3600, aid))
        cutoff = int(time.time()) - 30 * 60
        ids = {r["id"] for r in repo.stuck_sending(conn, cutoff)}
        self.assertIn(aid, ids)


if __name__ == "__main__":
    unittest.main()
