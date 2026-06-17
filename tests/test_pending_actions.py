import unittest

from assistant.storage import db
from assistant.storage import repositories as repo


class TestPendingActions(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _make(self, key="k1"):
        return repo.create_pending(
            self.conn, idempotency_key=key, message_id="m1", thread_id="t1",
            tier=2, kind="reply_draft", summary="reply to Alex", draft_text="hi",
        )

    def test_idempotency_key_prevents_duplicate_queue(self):
        first = self._make("k1")
        self.assertIsNotNone(first)
        dup = self._make("k1")  # same key
        self.assertIsNone(dup)  # not queued twice

    def test_begin_send_is_single_winner(self):
        aid = self._make("k1")
        repo.update_pending_status(self.conn, aid, "APPROVED")
        # first call wins, second loses → no double send
        self.assertTrue(repo.begin_send(self.conn, aid))
        self.assertFalse(repo.begin_send(self.conn, aid))
        repo.mark_sent(self.conn, aid, "sent-gmail-id")
        row = repo.get_pending(self.conn, aid)
        self.assertEqual(row["status"], "SENT")
        self.assertEqual(row["sent_gmail_id"], "sent-gmail-id")

    def test_cannot_send_unapproved(self):
        aid = self._make("k1")  # status PENDING
        self.assertFalse(repo.begin_send(self.conn, aid))

    def test_edited_can_be_sent(self):
        aid = self._make("k1")
        self.assertTrue(repo.set_pending_draft(self.conn, aid, "edited body"))
        row = repo.get_pending(self.conn, aid)
        self.assertEqual(row["status"], "EDITED")
        self.assertEqual(row["draft_text"], "edited body")
        self.assertTrue(repo.begin_send(self.conn, aid))

    # --- regression tests for the reviewer-found double-send paths ---
    def test_approve_cannot_revive_a_sent_action(self):
        aid = self._make("k1")
        self.assertTrue(repo.mark_approved(self.conn, aid))
        self.assertTrue(repo.begin_send(self.conn, aid))
        repo.mark_sent(self.conn, aid, "gid")
        # A stale/re-tapped Approve must NOT be able to revive a SENT row.
        self.assertFalse(repo.mark_approved(self.conn, aid))
        self.assertFalse(repo.begin_send(self.conn, aid))

    def test_edit_cannot_reopen_a_sent_action(self):
        aid = self._make("k1")
        repo.mark_approved(self.conn, aid)
        repo.begin_send(self.conn, aid)
        repo.mark_sent(self.conn, aid, "gid")
        # Editing a SENT action must be refused (otherwise it becomes sendable again).
        self.assertFalse(repo.set_pending_draft(self.conn, aid, "sneaky resend"))
        self.assertEqual(repo.get_pending(self.conn, aid)["status"], "SENT")
        self.assertFalse(repo.begin_send(self.conn, aid))

    def test_skip_cannot_relabel_a_sent_action(self):
        aid = self._make("k1")
        repo.mark_approved(self.conn, aid)
        repo.begin_send(self.conn, aid)
        repo.mark_sent(self.conn, aid, "gid")
        self.assertFalse(repo.mark_skipped(self.conn, aid))
        self.assertEqual(repo.get_pending(self.conn, aid)["status"], "SENT")

    def test_send_failed_can_be_retried(self):
        aid = self._make("k1")
        repo.mark_approved(self.conn, aid)
        repo.begin_send(self.conn, aid)
        repo.mark_send_failed(self.conn, aid, "network blip")
        # A genuinely failed send may be re-approved and retried.
        self.assertTrue(repo.mark_approved(self.conn, aid))
        self.assertTrue(repo.begin_send(self.conn, aid))

    def test_undelivered_pending_lists_only_undelivered(self):
        a1 = self._make("k1")
        a2 = self._make("k2")
        repo.set_pending_telegram_message(self.conn, a1, "1", "tg-1")
        undelivered = repo.undelivered_pending(self.conn)
        ids = {r["id"] for r in undelivered}
        self.assertIn(a2, ids)       # never delivered
        self.assertNotIn(a1, ids)    # delivered

    def test_has_action(self):
        from assistant.storage import db as _db  # noqa: F401
        repo.log_action(self.conn, kind="archive", message_id="m1", reversible=True)
        self.assertTrue(repo.has_action(self.conn, "m1", ("archive", "label")))
        self.assertFalse(repo.has_action(self.conn, "m1", ("fyi",)))
        self.assertFalse(repo.has_action(self.conn, "other", ("archive",)))


if __name__ == "__main__":
    unittest.main()
