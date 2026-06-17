"""GAP 7 — proactive relationship reminders for open situations awaiting the owner."""

from __future__ import annotations

import json
import time
import unittest

from assistant.config import Settings
from assistant.control import proactive
from assistant.storage import db
from assistant.storage import repositories as repo


def _mkdb():
    return db.open_db(":memory:")


def _person_with_situation(conn, pid, rel_type, *, awaiting, age_hours, status="open", key="k1"):
    repo.person_add(conn, person_id=pid, display_name=pid, emails=[f"{pid}@x.com"])
    repo.person_link_set(conn, f"{pid}@x.com", pid, confidence=1.0, source="observed")
    repo.set_person_relationship_type(conn, pid, rel_type)
    last_ts = int(time.time()) - age_hours * 3600
    sit = [{"key": key, "situation": "needs your reply", "awaiting": awaiting,
            "status": status, "last_activity_ts": last_ts, "thread_id": "t1"}]
    repo.relationship_memory_upsert(
        conn, pid, summary_json="{}", open_situations_json=json.dumps(sit),
        decided_json="[]", episodes_json="[]", superseded_json="[]",
        last_distilled_at=last_ts, version=1,
    )


def _reminders(conn):
    return conn.execute(
        "SELECT * FROM pending_actions WHERE kind='reminder'"
    ).fetchall()


class TestRelationshipReminders(unittest.TestCase):
    def test_awaiting_owner_over_threshold_creates_reminder(self):
        conn = _mkdb()
        _person_with_situation(conn, "p1", "collaborator", awaiting="owner", age_hours=20)
        n = proactive._relationship_reminder_sweep(conn, Settings())
        self.assertEqual(n, 1)
        rows = _reminders(conn)
        self.assertEqual(len(rows), 1)
        self.assertIn("Still waiting on your response", rows[0]["summary"])

    def test_partner_uses_shorter_threshold(self):
        conn = _mkdb()
        # 5h old, partner threshold is 4h → fires (a non-personal 12h threshold would not).
        _person_with_situation(conn, "p1", "partner", awaiting="me", age_hours=5)
        n = proactive._relationship_reminder_sweep(conn, Settings())
        self.assertEqual(n, 1)
        self.assertEqual(_reminders(conn)[0]["tier"], 3)

    def test_under_threshold_no_reminder(self):
        conn = _mkdb()
        # collaborator threshold is 12h; 2h old → no reminder.
        _person_with_situation(conn, "p1", "collaborator", awaiting="owner", age_hours=2)
        n = proactive._relationship_reminder_sweep(conn, Settings())
        self.assertEqual(n, 0)

    def test_no_duplicate(self):
        conn = _mkdb()
        _person_with_situation(conn, "p1", "collaborator", awaiting="owner", age_hours=20)
        proactive._relationship_reminder_sweep(conn, Settings())
        # Second sweep must NOT create a duplicate.
        n2 = proactive._relationship_reminder_sweep(conn, Settings())
        self.assertEqual(n2, 0)
        self.assertEqual(len(_reminders(conn)), 1)

    def test_awaiting_them_no_reminder(self):
        conn = _mkdb()
        _person_with_situation(conn, "p1", "collaborator", awaiting="them", age_hours=48)
        n = proactive._relationship_reminder_sweep(conn, Settings())
        self.assertEqual(n, 0)

    def test_resolved_no_reminder(self):
        conn = _mkdb()
        _person_with_situation(conn, "p1", "collaborator", awaiting="owner",
                               age_hours=48, status="resolved")
        n = proactive._relationship_reminder_sweep(conn, Settings())
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
