"""GAP 6 — commitment dedup (Jaccard) + staleness pruning + person_id on insert."""

from __future__ import annotations

import time
import unittest
from datetime import date, timedelta

from assistant.config import Settings
from assistant.memory import commitments as C
from assistant.storage import db, retention
from assistant.storage import repositories as repo


def _mkdb():
    return db.open_db(":memory:")


def _count_open(conn, contact=None):
    if contact:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM commitments WHERE status='open' AND contact_email=?",
            (contact,),
        ).fetchone()["n"]
    return conn.execute("SELECT COUNT(*) AS n FROM commitments WHERE status='open'").fetchone()["n"]


class TestDedup(unittest.TestCase):
    def test_same_text_same_contact_dedups(self):
        conn = _mkdb()
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="I will send the deck by Friday")
        C.add_commitment(conn, message_id="m2", contact_email="a@x.com",
                         commitment_text="I will send the deck by Friday")
        self.assertEqual(_count_open(conn, "a@x.com"), 1)

    def test_high_overlap_dedups(self):
        conn = _mkdb()
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="send the quarterly report to the team")
        # Heavy token overlap (> 0.6 Jaccard) → deduplicated.
        C.add_commitment(conn, message_id="m2", contact_email="a@x.com",
                         commitment_text="send the quarterly report to the team today")
        self.assertEqual(_count_open(conn, "a@x.com"), 1)

    def test_different_text_not_dedup(self):
        conn = _mkdb()
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="send the deck")
        C.add_commitment(conn, message_id="m2", contact_email="a@x.com",
                         commitment_text="schedule a demo call next week")
        self.assertEqual(_count_open(conn, "a@x.com"), 2)

    def test_different_contact_not_dedup(self):
        conn = _mkdb()
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="send the deck by Friday")
        C.add_commitment(conn, message_id="m2", contact_email="b@x.com",
                         commitment_text="send the deck by Friday")
        self.assertEqual(_count_open(conn), 2)

    def test_dedup_tightens_due_date(self):
        conn = _mkdb()
        cid = C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                               commitment_text="send the deck by Friday")  # no due date
        due = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        ret = C.add_commitment(conn, message_id="m2", contact_email="a@x.com",
                               commitment_text="send the deck by Friday", due_date=due)
        self.assertEqual(ret, cid)  # deduplicated → returns existing id
        row = C.get_commitment(conn, cid)
        self.assertEqual(row["due_date"], due)  # tightened

    def test_dedup_by_person_id(self):
        conn = _mkdb()
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="send the deck by Friday", person_id="p1")
        C.add_commitment(conn, message_id="m2", contact_email="b@x.com",  # different email
                         commitment_text="send the deck by Friday", person_id="p1")  # same person
        self.assertEqual(_count_open(conn), 1)

    def test_person_id_stored(self):
        conn = _mkdb()
        cid = C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                               commitment_text="x", person_id="p99")
        row = C.get_commitment(conn, cid)
        self.assertEqual(row["person_id"], "p99")


class TestStaleness(unittest.TestCase):
    def test_stale_due_date_pruned(self):
        conn = _mkdb()
        old_due = (date.today() - timedelta(days=120)).strftime("%Y-%m-%d")
        cid = C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                               commitment_text="old promise", due_date=old_due)
        n = retention.mark_stale_commitments(conn)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(C.get_commitment(conn, cid)["status"], "stale")

    def test_recent_due_not_pruned(self):
        conn = _mkdb()
        due = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")
        cid = C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                               commitment_text="upcoming", due_date=due)
        retention.mark_stale_commitments(conn)
        self.assertEqual(C.get_commitment(conn, cid)["status"], "open")

    def test_no_due_date_old_pruned(self):
        conn = _mkdb()
        cid = C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                               commitment_text="ancient undated")
        # Backdate created_at to 200 days ago.
        conn.execute(
            "UPDATE commitments SET created_at = unixepoch('now') - ? WHERE id=?",
            (200 * 86400, cid),
        )
        n = retention.mark_stale_commitments(conn)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(C.get_commitment(conn, cid)["status"], "stale")

    def test_no_due_date_recent_not_pruned(self):
        conn = _mkdb()
        cid = C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                               commitment_text="recent undated")
        retention.mark_stale_commitments(conn)
        self.assertEqual(C.get_commitment(conn, cid)["status"], "open")


if __name__ == "__main__":
    unittest.main()
