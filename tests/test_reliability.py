"""Phase 11 — reliability hardening: retention/pruning + stuck-send detection.

Retention trims old high-volume rows but never the ledger/pending/memory. The stuck-send
reaper flags crashed-mid-send rows into a terminal state that is NEVER re-sent (so it can
never cause a double-send)."""

from __future__ import annotations

import time
import unittest

from assistant.config import Settings
from assistant.storage import db, metrics, retention, wa_messages
from assistant.storage import repositories as repo


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        metrics.ensure(self.conn)
        wa_messages.ensure(self.conn)
        self.now = int(time.time())
        self.old = self.now - 200 * 86400   # 200 days old

    def tearDown(self):
        self.conn.close()

    def test_prunes_old_keeps_new(self):
        # audit_log (core table)
        self.conn.execute("INSERT INTO audit_log (ts, kind, summary, undone) VALUES (?,?,?,0)",
                          (self.old, "surface", "ancient"))
        self.conn.execute("INSERT INTO audit_log (ts, kind, summary, undone) VALUES (?,?,?,0)",
                          (self.now, "surface", "fresh"))
        # llm_calls
        self.conn.execute("INSERT INTO llm_calls (ts, task, model) VALUES (?,?,?)", (self.old, "JUDGE", "x"))
        self.conn.execute("INSERT INTO llm_calls (ts, task, model) VALUES (?,?,?)", (self.now, "JUDGE", "x"))
        # wa_messages (30-day window)
        wa_messages.record(self.conn, {"message_id": "wa_old", "jid": "j", "body": "old", "ts": self.old})
        wa_messages.record(self.conn, {"message_id": "wa_new", "jid": "j", "body": "new", "ts": self.now})

        deleted = retention.prune(self.conn, Settings(retention_days=90, retention_wa_history_days=30))
        self.assertEqual(deleted.get("audit_log"), 1)
        self.assertEqual(deleted.get("llm_calls"), 1)
        self.assertEqual(deleted.get("wa_messages"), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"], 1)
        self.assertEqual(self.conn.execute("SELECT summary FROM audit_log").fetchone()["summary"], "fresh")
        self.assertIsNotNone(wa_messages.recent(self.conn, "j", since_ts=self.now - 100))

    def test_disabled_is_noop(self):
        self.conn.execute("INSERT INTO llm_calls (ts, task, model) VALUES (?,?,?)", (self.old, "JUDGE", "x"))
        self.assertEqual(retention.prune(self.conn, Settings(retention_enabled=False)), {})
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM llm_calls").fetchone()["n"], 1)

    def test_ledger_is_never_pruned(self):
        from assistant.storage import ledger
        ledger.mark_seen(self.conn, "ancient_msg")
        self.conn.execute("UPDATE processed_messages SET created_at=? WHERE message_id=?",
                          (self.old, "ancient_msg"))
        retention.prune(self.conn, Settings())
        # exactly-once guarantee preserved — the id is still known
        self.assertFalse(ledger.mark_seen(self.conn, "ancient_msg"))  # still deduped


class TestStuckSendReaper(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _sending_action(self, key, age_seconds):
        aid = repo.create_pending(self.conn, idempotency_key=key, message_id="m", thread_id="t",
                                  tier=2, kind="reply_draft", summary="hi", draft_text="d")
        repo.mark_approved(self.conn, aid)
        self.assertTrue(repo.begin_send(self.conn, aid))   # → SENDING
        # failure-recovery-2: the reaper now keys staleness on the dedicated send-start
        # clock (sending_started_at), not created_at, so age BOTH columns to simulate a
        # genuinely long-in-flight send. (created_at alone no longer ages a send.)
        self.conn.execute(
            "UPDATE pending_actions SET created_at=?, sending_started_at=? WHERE id=?",
            (int(time.time()) - age_seconds, int(time.time()) - age_seconds, aid))
        return aid

    def test_old_sending_is_flagged_recent_is_not(self):
        old = self._sending_action("k_old", 9999)
        recent = self._sending_action("k_recent", 5)
        cutoff = int(time.time()) - 30 * 60
        stuck = repo.stuck_sending(self.conn, cutoff)
        ids = {r["id"] for r in stuck}
        self.assertIn(old, ids)
        self.assertNotIn(recent, ids)

    def test_mark_stuck_is_terminal_and_unrepeatable(self):
        aid = self._sending_action("k", 9999)
        self.assertTrue(repo.mark_send_stuck(self.conn, aid))
        row = self.conn.execute("SELECT status FROM pending_actions WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row["status"], "SEND_STUCK")
        # cannot re-flag, cannot be sent (begin_send only acts on APPROVED/EDITED)
        self.assertFalse(repo.mark_send_stuck(self.conn, aid))
        self.assertFalse(repo.begin_send(self.conn, aid))


if __name__ == "__main__":
    unittest.main()
