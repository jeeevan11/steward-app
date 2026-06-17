"""PHASE 10 + 13 — trust & value metrics.

Inserts synthetic ledger / pending-action / learning-event / commitment /
decision-log rows into an in-memory DB and asserts compute() returns sensible
counts, that the daily/weekly/monthly windows differ, and that an empty DB
returns an all-zero bundle without raising.
"""

from __future__ import annotations

import time
import unittest

from assistant.storage import db, decision_log, trust_metrics


class TestTrustMetrics(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        decision_log.ensure(self.conn)
        self.now = int(time.time())

    def tearDown(self):
        self.conn.close()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _ledger(self, mid, tier, ts, category="personal", state="DONE"):
        self.conn.execute(
            "INSERT INTO processed_messages "
            "(message_id, thread_id, state, tier, category, updated_at, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (mid, "t" + mid, state, tier, category, ts, ts),
        )

    def _decision(self, mid, base_tier, final_tier, ts, category="personal"):
        self.conn.execute(
            "INSERT INTO decision_log "
            "(message_id, thread_id, ts, category, base_tier, final_tier) "
            "VALUES (?,?,?,?,?,?)",
            (mid, "t" + mid, ts, category, base_tier, final_tier),
        )

    def _pending(self, key, mid, tier, status, ts):
        self.conn.execute(
            "INSERT INTO pending_actions "
            "(idempotency_key, message_id, tier, kind, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (key, mid, tier, "reply_draft", status, ts),
        )

    def _learn(self, kind, ts, mid="m"):
        self.conn.execute(
            "INSERT INTO learning_events (ts, type, message_id) VALUES (?,?,?)",
            (ts, kind, mid),
        )

    def _commit(self, cid, status="open"):
        self.conn.execute(
            "INSERT INTO commitments (id, commitment_text, status) VALUES (?,?,?)",
            (cid, "send the deck", status),
        )

    def _seed(self):
        """A small, recent (within last hour) population across all sources."""
        t = self.now - 600  # 10 min ago: inside daily/weekly/monthly windows
        # 5 processed messages: 2 auto-handled (tier 0/1), 1 escalated (tier 3),
        # 2 approvals (tier 2 + tier 3).
        self._ledger("a", 0, t, category="newsletter")   # noise tier-0 (filed)
        self._ledger("b", 1, t)                            # auto-handled FYI
        self._ledger("c", 2, t)                            # surfaced for approval
        self._ledger("d", 3, t)                            # escalated + approval
        self._ledger("e", 0, t, category="personal")      # real mail filed quietly
        # decision_log: one dropped (base 2 -> final 0 = suppressed), plus 'e' is a
        # non-noise tier-0 (also suppressed).
        self._decision("f", 2, 0, t, category="work_request")  # silenced
        self._decision("e", 0, 0, t, category="personal")      # quiet real mail
        self._decision("a", 0, 0, t, category="newsletter")    # noise, not counted
        # pending_actions: 2 surfaced, 1 approved, 1 edited
        self._pending("k1", "c", 2, "SENT", t)
        self._pending("k2", "d", 3, "EDITED", t)
        # learning_events: approve/edit/skip
        self._learn("approve", t)
        self._learn("approve", t)
        self._learn("edit", t)
        self._learn("skip", t)
        # commitments: 2 open, 1 done
        self._commit("c1", "open")
        self._commit("c2", "open")
        self._commit("c3", "done")
        self.conn.commit()

    # ── tests ────────────────────────────────────────────────────────────────
    def test_compute_sensible_counts(self):
        self._seed()
        m = trust_metrics.compute(self.conn, since_epoch=self.now - 3600)

        self.assertEqual(m["messages_processed"], 5)
        self.assertEqual(m["messages_escalated"], 1)          # tier 3
        self.assertEqual(m["messages_auto_handled"], 3)       # tiers 0,1,0
        self.assertEqual(m["approvals_requested"], 2)         # tiers 2,3

        # suppressed: 'f' (dropped 2->0) + 'e' (non-noise tier-0). 'a' excluded (noise).
        self.assertEqual(m["messages_suppressed"], 2)

        # approval_rate = approve(2) / surfaced(2 pending) = 1.0
        self.assertGreater(m["approval_rate"], 0)
        self.assertLessEqual(m["approval_rate"], 1.0)

        # draft acceptance = approve(2) / (approve2 + edit1 + skip1) = 0.5
        self.assertAlmostEqual(m["draft_acceptance_rate"], 0.5, places=3)

        self.assertEqual(m["commitments_open"], 2)

        # decisions_avoided = auto_handled(3) + suppressed(2) = 5
        self.assertEqual(m["decisions_avoided"], 5)

        # time saved = 3*2 + 2*1 + 2*3 = 14 minutes, must be > 0
        self.assertGreater(m["estimated_time_saved_minutes"], 0)
        self.assertAlmostEqual(m["estimated_time_saved_minutes"], 14.0, places=1)

    def test_windows_differ(self):
        # one recent row (inside daily) + one old row (only inside monthly).
        self._ledger("recent", 1, self.now - 600)
        self._ledger("old", 1, self.now - 10 * 86400)  # 10 days ago
        self.conn.commit()

        daily = trust_metrics.period(self.conn, "daily")
        weekly = trust_metrics.period(self.conn, "weekly")
        monthly = trust_metrics.period(self.conn, "monthly")

        self.assertEqual(daily["messages_processed"], 1)
        self.assertEqual(weekly["messages_processed"], 1)
        self.assertEqual(monthly["messages_processed"], 2)
        # windows genuinely differ
        self.assertNotEqual(daily["messages_processed"], monthly["messages_processed"])
        self.assertEqual(daily["period"], "daily")
        self.assertEqual(monthly["period"], "monthly")

    def test_empty_db_all_zero_no_raise(self):
        m = trust_metrics.compute(self.conn, since_epoch=self.now - 3600)
        self.assertEqual(m["messages_processed"], 0)
        self.assertEqual(m["messages_escalated"], 0)
        self.assertEqual(m["messages_auto_handled"], 0)
        self.assertEqual(m["messages_suppressed"], 0)
        self.assertEqual(m["approvals_requested"], 0)
        self.assertEqual(m["approval_rate"], 0.0)
        self.assertEqual(m["draft_acceptance_rate"], 0.0)
        self.assertEqual(m["memory_updates"], 0)
        self.assertEqual(m["commitments_open"], 0)
        self.assertEqual(m["decisions_avoided"], 0)
        self.assertEqual(m["estimated_time_saved_minutes"], 0.0)

    def test_missing_tables_degrade_to_zero(self):
        # A bare connection with NO schema at all must not raise.
        import sqlite3

        bare = sqlite3.connect(":memory:")
        try:
            m = trust_metrics.compute(bare, since_epoch=0)
            self.assertEqual(m["messages_processed"], 0)
            self.assertEqual(m["decisions_avoided"], 0)
            self.assertIsNone(m["response_time_reduction"])
        finally:
            bare.close()

    def test_period_unknown_falls_back_to_daily(self):
        m = trust_metrics.period(self.conn, "bogus")
        self.assertEqual(m["period"], "daily")


if __name__ == "__main__":
    unittest.main()
