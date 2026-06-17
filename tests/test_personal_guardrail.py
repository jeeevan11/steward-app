"""GAP 3 — autonomous-send guardrail: personal contacts are held for approval."""

from __future__ import annotations

import dataclasses
import unittest

from assistant.action import gmail_actions
from assistant.config import Settings
from assistant.storage import db, decision_log
from assistant.storage import repositories as repo


def _mkdb():
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _seed_person(conn, sender, rel_type):
    repo.person_add(conn, person_id="p1", display_name="Partner", emails=[sender])
    repo.person_link_set(conn, sender, "p1", confidence=1.0, source="observed")
    repo.set_person_relationship_type(conn, "p1", rel_type)


def _seed_decision(conn, message_id, sender):
    conn.execute(
        "INSERT INTO decision_log (message_id, thread_id, ts, sender_email, final_tier) "
        "VALUES (?,?,strftime('%s','now'),?,2)",
        (message_id, "t", sender.lower()),
    )


def _pending(conn, message_id, *, status="PENDING"):
    aid = repo.create_pending(
        conn, idempotency_key=f"{message_id}:2", message_id=message_id, thread_id="t",
        tier=2, kind="reply_draft", summary="reply", draft_text="hi there",
    )
    if status != "PENDING":
        conn.execute("UPDATE pending_actions SET status=? WHERE id=?", (status, aid))
    return aid


class FakeMail:
    def __init__(self):
        self.sent = []

    def source_for(self, mid):
        return self

    def get_thread(self, mid):  # pragma: no cover - should not be reached when held
        raise AssertionError("send should have been held")

    def send_reply(self, **kw):  # pragma: no cover
        self.sent.append(kw)
        return "sent-1"


class TestPersonalGuardrail(unittest.TestCase):
    def test_personal_contact_held(self):
        conn = _mkdb()
        _seed_person(conn, "love@x.com", "partner")
        _seed_decision(conn, "m1", "love@x.com")
        aid = _pending(conn, "m1")
        settings = Settings(mode="live", personal_auto_send=False)
        ok = gmail_actions.execute_send(conn, FakeMail(), settings, aid)
        self.assertFalse(ok)
        row = repo.get_pending(conn, aid)
        self.assertEqual(row["status"], "PENDING")
        self.assertIn("Held for approval", row["summary"])

    def test_family_contact_held(self):
        conn = _mkdb()
        _seed_person(conn, "mom@x.com", "family")
        _seed_decision(conn, "m1", "mom@x.com")
        aid = _pending(conn, "m1")
        settings = Settings(mode="live", personal_auto_send=False)
        ok = gmail_actions.execute_send(conn, FakeMail(), settings, aid)
        self.assertFalse(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "PENDING")

    def test_non_personal_proceeds(self):
        conn = _mkdb()
        _seed_person(conn, "vc@fund.com", "investor")
        _seed_decision(conn, "m1", "vc@fund.com")
        # Approved (human path) → should proceed. Dry-run so no real Gmail.
        aid = _pending(conn, "m1", status="APPROVED")
        settings = Settings(mode="dry_run", personal_auto_send=False)
        ok = gmail_actions.execute_send(conn, FakeMail(), settings, aid)
        self.assertTrue(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SENT")

    def test_auto_send_override(self):
        conn = _mkdb()
        _seed_person(conn, "love@x.com", "partner")
        _seed_decision(conn, "m1", "love@x.com")
        # personal_auto_send=True overrides the hold; APPROVED + dry-run proceeds.
        aid = _pending(conn, "m1", status="APPROVED")
        settings = Settings(mode="dry_run", personal_auto_send=True)
        ok = gmail_actions.execute_send(conn, FakeMail(), settings, aid)
        self.assertTrue(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SENT")

    def test_personal_flag_fallback(self):
        # No relationship_type, but the contact carries the legacy 'personal' flag.
        conn = _mkdb()
        c = repo.get_or_default_contact(conn, "friend@x.com", "Friend")
        c.flags = {"personal"}
        repo.upsert_contact(conn, c)
        _seed_decision(conn, "m1", "friend@x.com")
        aid = _pending(conn, "m1")
        settings = Settings(mode="live", personal_auto_send=False)
        ok = gmail_actions.execute_send(conn, FakeMail(), settings, aid)
        self.assertFalse(ok)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
