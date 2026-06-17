"""GAP 2 — conversation batching: same-sender messages fold into one open card."""

from __future__ import annotations

import time
import unittest

from assistant.storage import db, decision_log
from assistant.storage import repositories as repo


def _mkdb():
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _seed_decision(conn, message_id, sender, *, ts=None):
    conn.execute(
        "INSERT INTO decision_log (message_id, thread_id, ts, sender_email, final_tier) "
        "VALUES (?,?,?,?,2)",
        (message_id, "t", int(ts or time.time()), sender.lower()),
    )


def _create(conn, message_id, sender, *, created_offset=0):
    """Create a pending action for a message, then backdate created_at by offset secs."""
    aid = repo.create_pending(
        conn, idempotency_key=f"{message_id}:2", message_id=message_id, thread_id="t",
        tier=2, kind="reply_draft", summary="s", draft_text="d",
    )
    if created_offset:
        conn.execute(
            "UPDATE pending_actions SET created_at=strftime('%s','now') - ? WHERE id=?",
            (created_offset, aid),
        )
    return aid


class TestBatching(unittest.TestCase):
    def test_same_sender_within_window_folds(self):
        conn = _mkdb()
        _seed_decision(conn, "m1", "alice@x.com")
        aid = _create(conn, "m1", "alice@x.com")

        # Second message from the same sender, 5 min later → should find the open action.
        _seed_decision(conn, "m2", "alice@x.com")
        found = repo.find_open_action_for_sender(conn, "alice@x.com", within_seconds=1200)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], aid)

        ok = repo.fold_message_into_action(conn, aid, "m2", "new summary", "new draft")
        self.assertTrue(ok)

        row = repo.get_pending(conn, aid)
        self.assertEqual(row["message_count"], 2)
        self.assertEqual(row["summary"], "new summary")
        self.assertEqual(row["draft_text"], "new draft")
        import json
        folded = json.loads(row["folded_message_ids"])
        self.assertIn("m1", folded)
        self.assertIn("m2", folded)

        # Only one pending action exists for this sender (no duplicate card).
        n = conn.execute("SELECT COUNT(*) AS n FROM pending_actions").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_sender_after_window_not_folded(self):
        conn = _mkdb()
        _seed_decision(conn, "m1", "bob@x.com")
        # Action created 21 minutes ago → outside the 20-min window.
        _create(conn, "m1", "bob@x.com", created_offset=21 * 60)
        _seed_decision(conn, "m2", "bob@x.com")
        found = repo.find_open_action_for_sender(conn, "bob@x.com", within_seconds=1200)
        self.assertIsNone(found)

    def test_different_sender_always_separate(self):
        conn = _mkdb()
        _seed_decision(conn, "m1", "alice@x.com")
        _create(conn, "m1", "alice@x.com")
        _seed_decision(conn, "m2", "carol@x.com")
        found = repo.find_open_action_for_sender(conn, "carol@x.com", within_seconds=1200)
        self.assertIsNone(found)

    def test_non_pending_not_folded(self):
        conn = _mkdb()
        _seed_decision(conn, "m1", "dan@x.com")
        aid = _create(conn, "m1", "dan@x.com")
        repo.mark_skipped(conn, aid)
        _seed_decision(conn, "m2", "dan@x.com")
        found = repo.find_open_action_for_sender(conn, "dan@x.com", within_seconds=1200)
        self.assertIsNone(found)

    def test_queue_exposes_message_count(self):
        from assistant.storage import read_queries as rq
        conn = _mkdb()
        _seed_decision(conn, "m1", "eve@x.com")
        aid = _create(conn, "m1", "eve@x.com")
        repo.fold_message_into_action(conn, aid, "m2", "s2", "d2")
        items = rq.get_queue(conn, limit=10)
        match = [i for i in items if i["message_id"] == "m1"]
        self.assertTrue(match)
        self.assertEqual(match[0]["message_count"], 2)


if __name__ == "__main__":
    unittest.main()
