import unittest

from assistant.storage import db
from assistant.storage import ledger


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_mark_seen_dedup(self):
        self.assertTrue(ledger.mark_seen(self.conn, "m1", "t1"))
        # second sighting of the same id must be rejected — exactly-once gate
        self.assertFalse(ledger.mark_seen(self.conn, "m1", "t1"))

    def test_claim_then_complete(self):
        ledger.mark_seen(self.conn, "m1", "t1")
        self.assertTrue(ledger.claim(self.conn, "m1"))
        ledger.complete(self.conn, "m1", tier=0, category="newsletter", confidence=0.9, dry_run=True)
        row = ledger.get(self.conn, "m1")
        self.assertEqual(row["state"], ledger.DONE)
        # cannot claim a DONE row
        self.assertFalse(ledger.claim(self.conn, "m1"))

    def test_unknown_claim_returns_false(self):
        self.assertFalse(ledger.claim(self.conn, "does-not-exist"))

    def test_recover_stale_requeues_processing(self):
        ledger.mark_seen(self.conn, "m1", "t1")
        ledger.claim(self.conn, "m1")  # now PROCESSING
        # simulate crash: row left PROCESSING. Recovery should requeue it.
        n = ledger.recover_stale(self.conn)
        self.assertEqual(n, 1)
        self.assertEqual(ledger.get(self.conn, "m1")["state"], ledger.SEEN)
        # and it can be claimed again
        self.assertTrue(ledger.claim(self.conn, "m1"))

    def test_recover_stale_parks_poison_message(self):
        ledger.mark_seen(self.conn, "m1", "t1")
        for _ in range(6):  # exceed max_attempts
            ledger.claim(self.conn, "m1")
            # leave in PROCESSING each loop
            self.conn.execute("UPDATE processed_messages SET state='PROCESSING' WHERE message_id='m1'")
        n = ledger.recover_stale(self.conn, max_attempts=5)
        self.assertEqual(ledger.get(self.conn, "m1")["state"], ledger.FAILED)
        self.assertEqual(n, 0)

    def test_fail_sets_failed_and_error(self):
        ledger.mark_seen(self.conn, "m1", "t1")
        ledger.claim(self.conn, "m1")
        ledger.fail(self.conn, "m1", "kaboom")
        row = ledger.get(self.conn, "m1")
        self.assertEqual(row["state"], ledger.FAILED)
        self.assertIn("kaboom", row["last_error"])

    def test_full_exactly_once_cycle(self):
        # The canonical guarantee: a message processed once, never twice.
        self.assertTrue(ledger.mark_seen(self.conn, "x", "t"))
        self.assertTrue(ledger.claim(self.conn, "x"))
        ledger.complete(self.conn, "x", tier=2, dry_run=False)
        # later poll re-sees the same id
        self.assertFalse(ledger.mark_seen(self.conn, "x", "t"))
        self.assertFalse(ledger.claim(self.conn, "x"))
        self.assertTrue(ledger.is_done(self.conn, "x"))


class TestCrossThreadConnection(unittest.TestCase):
    """The Telegram bot creates the connection on one thread and runs queries on a
    worker thread. Confirm the connection allows that (check_same_thread=False)."""

    def test_connection_usable_from_another_thread(self):
        import threading

        conn = db.open_db(":memory:")
        ledger.mark_seen(conn, "m1", "t1")
        errors = []

        def worker():
            try:
                # would raise sqlite3.ProgrammingError without check_same_thread=False
                ledger.claim(conn, "m1")
                ledger.complete(conn, "m1", tier=0, dry_run=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(errors, [], f"cross-thread use failed: {errors}")
        self.assertTrue(ledger.is_done(conn, "m1"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
