"""P5c — every human signal is captured immediately (storage only).

approve → voice sample + importance bump; edit → draft_edits row with diff;
skip → skip_log row. All capture is fire-and-forget: a failure here must never
propagate to the pipeline that called it."""

from __future__ import annotations

import unittest

from assistant.learning import recorder
from assistant.storage import db
from assistant.storage import repositories as repo


def _setup():
    conn = db.open_db(":memory:")
    # a decision_log row so the recorder can resolve the sender email from message_id
    from assistant.storage import decision_log
    decision_log.ensure(conn)
    conn.execute(
        "INSERT INTO decision_log (message_id, sender_email) VALUES (?,?)",
        ("m1", "alex@x.com"),
    )
    aid = repo.create_pending(
        conn, idempotency_key="k1", message_id="m1", thread_id="t1", tier=2,
        kind="reply_draft", summary="Alex: wants a call", draft_text="Hi, sounds good.",
    )
    return conn, repo.get_pending(conn, aid)


class TestFeedbackStorage(unittest.TestCase):
    def test_approve_records_voice_sample_and_bumps_importance(self):
        conn, row = _setup()
        try:
            recorder.record_approve(conn, row)
            self.assertGreaterEqual(repo.voice_sample_count(conn), 1)
            c = repo.get_contact(conn, "alex@x.com")
            self.assertIsNotNone(c)
            self.assertEqual(c.importance, 1)
        finally:
            conn.close()

    def test_edit_records_draft_edit_with_diff(self):
        conn, row = _setup()
        try:
            recorder.record_edit(
                conn, row, new_text="Hi Alex, Tuesday works great.",
                original_text="Hi, sounds good.",
            )
            edits = list(conn.execute("SELECT * FROM draft_edits"))
            self.assertEqual(len(edits), 1)
            self.assertEqual(edits[0]["message_id"], "m1")
            self.assertEqual(edits[0]["segment"], "external")  # P5c default
            self.assertTrue(edits[0]["final_draft"].startswith("Hi Alex"))
            self.assertTrue(edits[0]["diff"])  # a real unified diff
        finally:
            conn.close()

    def test_skip_records_skip_log(self):
        conn, row = _setup()
        try:
            recorder.record_skip(conn, row, detail={"reason": "not relevant"})
            skips = list(conn.execute("SELECT * FROM skip_log"))
            self.assertEqual(len(skips), 1)
            self.assertEqual(skips[0]["tier"], 2)
            self.assertEqual(skips[0]["reason"], "not relevant")
        finally:
            conn.close()

    def test_recording_failure_does_not_propagate(self):
        conn, row = _setup()
        try:
            orig = repo.add_skip_log

            def boom(*a, **k):
                raise RuntimeError("disk full")

            repo.add_skip_log = boom  # type: ignore[assignment]
            try:
                # Must not raise — learning is best-effort.
                recorder.record_skip(conn, row, detail={"reason": "x"})
            finally:
                repo.add_skip_log = orig  # type: ignore[assignment]
        finally:
            conn.close()

    def test_capture_is_mode_independent(self):
        # The recorder takes no Settings — capture happens the same in dry_run or live
        # (it's internal state, never an external action). Approve with no email row.
        conn = db.open_db(":memory:")
        try:
            aid = repo.create_pending(
                conn, idempotency_key="k2", message_id="m9", thread_id="t9", tier=2,
                kind="reply_draft", draft_text="A reply.",
            )
            recorder.record_approve(conn, repo.get_pending(conn, aid))
            self.assertGreaterEqual(repo.voice_sample_count(conn), 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
