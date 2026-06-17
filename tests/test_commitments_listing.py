"""Regression: /api/commitments (read_queries.list_commitments) is enriched so the Mac
Commitments view can group by urgency, show who it's with, and offer Done/Snooze — not the
old flat {to, promise, due_date} read-only list.
"""

from __future__ import annotations

import unittest

from assistant.memory import commitments as C
from assistant.storage import db, decision_log, read_queries as rq
from assistant.storage import repositories as repo


class TestListCommitmentsEnriched(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        decision_log.ensure(self.conn)   # stale_threads (in list_commitments) reads it

    def tearDown(self):
        self.conn.close()

    def test_open_items_carry_person_status_and_created_at(self):
        # a contact gives the counterparty a human name
        c = repo.get_or_default_contact(self.conn, "alice@acme.com", "Alice Roy")
        repo.upsert_contact(self.conn, c)
        cid = C.add_commitment(self.conn, message_id="m1", contact_email="alice@acme.com",
                               commitment_text="send the deck", due_date="2026-06-20")

        out = rq.list_commitments(self.conn)
        self.assertEqual(len(out["open"]), 1)
        item = out["open"][0]
        # back-compat keys still present
        self.assertEqual(item["id"], cid)
        self.assertEqual(item["promise"], "send the deck")
        self.assertEqual(item["due_date"], "2026-06-20")
        # new enrichment
        self.assertEqual(item["person"], "Alice Roy")        # resolved from the contact
        self.assertEqual(item["status"], "open")
        self.assertGreater(item["created_at"], 0)

    def test_person_falls_back_to_localpart_then_empty(self):
        C.add_commitment(self.conn, message_id="m2", contact_email="bob@x.com",
                         commitment_text="call back", due_date="")
        C.add_commitment(self.conn, message_id="m3", contact_email="",
                         commitment_text="get back to it", due_date="")
        people = {i["promise"]: i["person"] for i in rq.list_commitments(self.conn)["open"]}
        self.assertEqual(people["call back"], "bob")          # localpart fallback
        self.assertEqual(people["get back to it"], "")        # no counterparty → empty

    def test_done_drops_it_from_the_open_list(self):
        cid = C.add_commitment(self.conn, message_id="m4", contact_email="a@x.com",
                               commitment_text="ship", due_date="")
        self.assertEqual(len(rq.list_commitments(self.conn)["open"]), 1)
        C.mark_done(self.conn, cid)
        self.assertEqual(len(rq.list_commitments(self.conn)["open"]), 0)


if __name__ == "__main__":
    unittest.main()
