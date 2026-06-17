"""Regression: ux-trust-1 — the popover/headline must surface the MOST URGENT pending
item, not the oldest.

The decisions list is built from repo.open_pending, which orders by created_at ASC (oldest
first). The popover headlined `.first`, so a later tier-3 ("needs you soon") was buried under
an older tier-2. read_queries.rank_open_decisions / top_open_decision rank by tier DESC then
recency, fixing the selection on the read seam that feeds the UI.

In-memory SQLite only; pending actions are created via the real repo seam.
"""

from __future__ import annotations

import unittest

from assistant.storage import db
from assistant.storage import read_queries as rq
from assistant.storage import repositories as repo


def _mk(conn, key, *, tier, kind="reply_draft", created_at=None):
    aid = repo.create_pending(conn, idempotency_key=key, message_id=key, thread_id="t",
                              tier=tier, kind=kind, summary=key)
    if created_at is not None:
        conn.execute("UPDATE pending_actions SET created_at=? WHERE id=?", (created_at, aid))
    return aid


class TestRankOpenDecisions(unittest.TestCase):
    def test_higher_tier_beats_older_lower_tier(self):
        conn = db.open_db(":memory:")
        try:
            # Old tier-2 created FIRST, newer tier-3 created LATER. Oldest-first ordering
            # would headline the tier-2; urgency ranking must headline the tier-3.
            _mk(conn, "old_t2", tier=2, created_at=1000)
            _mk(conn, "new_t3", tier=3, created_at=2000)
            ranked = rq.rank_open_decisions(repo.open_pending(conn))
            self.assertEqual(ranked[0]["message_id"], "new_t3")
            top = rq.top_open_decision(conn)
            self.assertEqual(top["message_id"], "new_t3")
        finally:
            conn.close()

    def test_within_same_tier_newest_first(self):
        conn = db.open_db(":memory:")
        try:
            _mk(conn, "t3_old", tier=3, created_at=1000)
            _mk(conn, "t3_new", tier=3, created_at=5000)
            ranked = rq.rank_open_decisions(repo.open_pending(conn))
            self.assertEqual([r["message_id"] for r in ranked], ["t3_new", "t3_old"])
        finally:
            conn.close()

    def test_full_ordering_tier_then_recency(self):
        conn = db.open_db(":memory:")
        try:
            _mk(conn, "t2_a", tier=2, created_at=1000)
            _mk(conn, "t3_a", tier=3, created_at=1500)
            _mk(conn, "t2_b", tier=2, created_at=3000)
            _mk(conn, "t3_b", tier=3, created_at=4000)
            ranked = [r["message_id"] for r in rq.rank_open_decisions(repo.open_pending(conn))]
            # Both tier-3 first (newest of them first), then both tier-2 (newest first).
            self.assertEqual(ranked, ["t3_b", "t3_a", "t2_b", "t2_a"])
        finally:
            conn.close()

    def test_empty_input(self):
        self.assertEqual(rq.rank_open_decisions([]), [])
        conn = db.open_db(":memory:")
        try:
            self.assertIsNone(rq.top_open_decision(conn))
        finally:
            conn.close()

    def test_accepts_plain_dicts(self):
        # The ranker must work on dict rows too (it is reused on already-serialized items).
        rows = [
            {"id": 1, "tier": 2, "created_at": 100, "message_id": "a"},
            {"id": 2, "tier": 3, "created_at": 50, "message_id": "b"},
        ]
        ranked = rq.rank_open_decisions(rows)
        self.assertEqual([r["message_id"] for r in ranked], ["b", "a"])


if __name__ == "__main__":
    unittest.main()
