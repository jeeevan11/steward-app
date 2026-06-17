"""Regression: P3's record_reasoning() creates the decision_log row first (NULL
sender); record() must still fill in sender_email/name/subject/snippet on the
conflict path, or the dashboard shows every sender as 'Unknown'."""

from __future__ import annotations

import unittest

from assistant.brain.tiers import decide
from assistant.models import Message, Thread
from assistant.storage import db, decision_log
from tests.helpers import make_contact, make_decision


class TestDecisionLogSender(unittest.TestCase):
    def test_record_fills_sender_after_record_reasoning(self):
        conn = db.open_db(":memory:")
        try:
            # 1) reasoning is written first (this is what classify_thread does) — no sender
            decision_log.record_reasoning(conn, message_id="m1", thread_id="t1",
                                          judge_output='{"x":1}', was_critical=True)
            pre = decision_log.get(conn, "m1")
            self.assertIn(pre["sender_email"], (None, ""))   # no sender yet

            # 2) then the full decision is recorded with the real sender
            msg = Message(id="m1", thread_id="t1", sender_email="alex@example.com",
                          sender_name="Alex Rivera", subject="hi", body_text="hello there")
            thread = Thread(id="t1", subject="hi", messages=[msg])
            decision_log.record(conn, message=msg, thread=thread,
                                decision=make_decision(category="personal"),
                                final=decide(thread, make_decision(category="personal"), make_contact()),
                                dry_run=True)

            row = decision_log.get(conn, "m1")
            self.assertEqual(row["sender_email"], "alex@example.com")   # filled on conflict
            self.assertEqual(row["sender_name"], "Alex Rivera")
            self.assertEqual(row["subject"], "hi")
            self.assertTrue(row["snippet"])
            self.assertEqual(row["judge_output"], '{"x":1}')        # reasoning preserved
            self.assertEqual(row["was_critical"], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
