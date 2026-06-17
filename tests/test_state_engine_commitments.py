"""Regression: control-state-presence-3 — the operating-state engine's commitment views.

overdue_commitments() and this_week_items() previously queried a non-existent `completed`
column and compared the TEXT due_date ('YYYY-MM-DD') against epoch integers, so OVERDUE /
THIS WEEK / the daily risk derivation were PERMANENTLY empty. These tests pin the real
schema (status TEXT 'open|done|...' + ISO date strings) so the regression cannot return.

In-memory SQLite only; commitments are inserted via the real memory.commitments CRUD so the
test exercises the same rows the live system stores.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from assistant.control import state_engine as SE
from assistant.memory import commitments as C
from assistant.storage import db
from assistant.storage import repositories as repo


def _iso(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).strftime("%Y-%m-%d")


class TestOverdueCommitments(unittest.TestCase):
    def test_overdue_open_commitment_is_classified(self):
        conn = db.open_db(":memory:")
        try:
            C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                             commitment_text="send the overdue deck", due_date=_iso(-1))
            rows = SE.overdue_commitments(conn)
            self.assertEqual([r["commitment_text"] for r in rows], ["send the overdue deck"])
        finally:
            conn.close()

    def test_completed_commitment_excluded_even_if_past_due(self):
        conn = db.open_db(":memory:")
        try:
            cid = C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                                   commitment_text="paid invoice", due_date=_iso(-3))
            C.mark_done(conn, cid)  # status -> 'done'
            self.assertEqual(SE.overdue_commitments(conn), [])
        finally:
            conn.close()

    def test_future_commitment_not_overdue(self):
        conn = db.open_db(":memory:")
        try:
            C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                             commitment_text="future thing", due_date=_iso(3))
            self.assertEqual(SE.overdue_commitments(conn), [])
        finally:
            conn.close()

    def test_no_due_date_is_never_overdue(self):
        conn = db.open_db(":memory:")
        try:
            # A commitment with no date can't be "past due"; it must not be mis-flagged.
            C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                             commitment_text="someday", due_date="")
            self.assertEqual(SE.overdue_commitments(conn), [])
        finally:
            conn.close()

    def test_overdue_sorted_soonest_first(self):
        conn = db.open_db(":memory:")
        try:
            C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                             commitment_text="older", due_date=_iso(-5))
            C.add_commitment(conn, message_id="m2", contact_email="b@x.com",
                             commitment_text="newer", due_date=_iso(-1))
            rows = SE.overdue_commitments(conn)
            self.assertEqual([r["commitment_text"] for r in rows], ["older", "newer"])
        finally:
            conn.close()


class TestThisWeekItems(unittest.TestCase):
    def test_commitment_due_this_week_is_included(self):
        conn = db.open_db(":memory:")
        try:
            C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                             commitment_text="this week thing", due_date=_iso(2))
            rows = SE.this_week_items(conn)
            texts = [r.get("commitment_text") for r in rows]
            self.assertIn("this week thing", texts)
        finally:
            conn.close()

    def test_far_future_and_done_excluded(self):
        conn = db.open_db(":memory:")
        try:
            C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                             commitment_text="far", due_date=_iso(40))
            done = C.add_commitment(conn, message_id="m2", contact_email="b@x.com",
                                    commitment_text="done soon", due_date=_iso(1))
            C.mark_done(conn, done)
            rows = SE.this_week_items(conn)
            texts = [r.get("commitment_text") for r in rows]
            self.assertNotIn("far", texts)
            self.assertNotIn("done soon", texts)
        finally:
            conn.close()

    def test_open_pending_action_is_included(self):
        conn = db.open_db(":memory:")
        try:
            repo.create_pending(conn, idempotency_key="k1", message_id="x1", thread_id="t1",
                                tier=2, kind="reply_draft", summary="reply to x")
            rows = SE.this_week_items(conn)
            kinds = [r.get("_item_type") for r in rows]
            self.assertIn("pending_action", kinds)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
