"""GAP 1 — relationship roles: migration, importance floors, contacts API, guardrails."""

from __future__ import annotations

import unittest

from assistant.memory import contacts as mc
from assistant.storage import db
from assistant.storage import migrations
from assistant.storage import repositories as repo


def _mkdb():
    return db.open_db(":memory:")


class TestMigration(unittest.TestCase):
    def test_relationship_type_column_present(self):
        conn = _mkdb()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(persons)")}
        self.assertIn("relationship_type", cols)

    def test_migration_idempotent(self):
        conn = _mkdb()
        # Re-running migrations (and init_db) must not raise or duplicate the column.
        migrations.apply_all_migrations(conn)
        db.init_db(conn)
        migrations.apply_all_migrations(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(persons)")]
        self.assertEqual(cols.count("relationship_type"), 1)

    def test_legacy_db_gets_column(self):
        # Simulate a persons table created before the column existed.
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE persons (id TEXT PRIMARY KEY, relationship TEXT)")
        conn.execute("INSERT INTO persons (id) VALUES ('p1')")
        migrations.apply_all_migrations(conn)
        self.assertEqual(repo.person_relationship_type(conn, "p1"), "unknown")
        self.assertTrue(repo.set_person_relationship_type(conn, "p1", "investor"))
        self.assertEqual(repo.person_relationship_type(conn, "p1"), "investor")


class TestImportanceFloors(unittest.TestCase):
    def test_partner_family_floor(self):
        # No activity at all → floor still applies.
        self.assertGreaterEqual(
            mc.compute_importance("partner", messages_last_30d=0, days_since_last_message=99), 80)
        self.assertGreaterEqual(
            mc.compute_importance("family", messages_last_30d=0, days_since_last_message=99), 80)

    def test_investor_mentor_floor(self):
        self.assertGreaterEqual(
            mc.compute_importance("investor", messages_last_30d=0, days_since_last_message=99), 65)
        self.assertGreaterEqual(
            mc.compute_importance("mentor", messages_last_30d=0, days_since_last_message=99), 65)

    def test_collaborator_customer_floor(self):
        self.assertGreaterEqual(
            mc.compute_importance("collaborator", messages_last_30d=0, days_since_last_message=99), 50)
        self.assertGreaterEqual(
            mc.compute_importance("customer", messages_last_30d=0, days_since_last_message=99), 50)

    def test_cold_no_floor(self):
        # cold/recruiter/unknown earn importance only from activity (0 floor).
        self.assertEqual(
            mc.compute_importance("cold", messages_last_30d=0, days_since_last_message=99), 0)
        self.assertEqual(
            mc.compute_importance("unknown", messages_last_30d=0, days_since_last_message=99), 0)
        self.assertEqual(
            mc.compute_importance("recruiter", messages_last_30d=0, days_since_last_message=99), 0)

    def test_frequency_and_recency_add_on_top(self):
        # Frequency capped at 30, recency up to 10. Active partner can approach 100.
        score = mc.compute_importance("partner", messages_last_30d=50, days_since_last_message=0)
        self.assertEqual(score, 100)  # 80 + min(50,30)=30 + 10 → clamp 100
        # Cold but active still rises above 0.
        self.assertGreater(
            mc.compute_importance("cold", messages_last_30d=5, days_since_last_message=0), 0)

    def test_recompute_persists(self):
        conn = _mkdb()
        # Build a person + link + relationship_type, then recompute the contact's importance.
        repo.person_add(conn, person_id="p1", display_name="Alice", emails=["a@x.com"])
        repo.person_link_set(conn, "a@x.com", "p1", confidence=1.0, source="observed")
        repo.set_person_relationship_type(conn, "p1", "partner")
        score = mc.recompute_importance(conn, "a@x.com")
        self.assertGreaterEqual(score, 80)
        c = repo.get_contact(conn, "a@x.com")
        self.assertGreaterEqual(c.importance, 80)


class TestContactsAPI(unittest.TestCase):
    def test_relationship_type_in_list(self):
        from assistant.storage import read_queries as rq
        conn = _mkdb()
        repo.person_add(conn, person_id="p1", display_name="Vee", emails=["vc@fund.com"])
        repo.person_link_set(conn, "vc@fund.com", "p1", confidence=1.0, source="observed")
        repo.set_person_relationship_type(conn, "p1", "investor")
        c = repo.get_or_default_contact(conn, "vc@fund.com", "Vee")
        c.importance = 70
        repo.upsert_contact(conn, c)
        items = rq.list_contacts(conn)
        match = [i for i in items if i["email"] == "vc@fund.com"]
        self.assertTrue(match)
        self.assertEqual(match[0]["relationship_type"], "investor")

    def test_unknown_when_no_person(self):
        from assistant.storage import read_queries as rq
        conn = _mkdb()
        c = repo.get_or_default_contact(conn, "nobody@x.com", "Nobody")
        repo.upsert_contact(conn, c)
        items = rq.list_contacts(conn)
        match = [i for i in items if i["email"] == "nobody@x.com"]
        self.assertEqual(match[0]["relationship_type"], "unknown")


class TestGuardrailFromRelationshipType(unittest.TestCase):
    def _eval(self, rel_type):
        from assistant.brain import guardrails
        from assistant.memory.retrieval import MemorySignals
        from assistant.models import Contact, Decision, Thread, Tier
        thread = Thread(id="t", subject="hi", messages=[])
        decision = Decision(category="other", intent="", one_line_summary="hi",
                            suggested_action="reply")
        contact = Contact(email="x@y.com")
        mem = MemorySignals(relationship_type=rel_type)
        return guardrails.evaluate(thread, decision, contact, memory=mem), Tier

    def test_partner_fires_ask(self):
        res, Tier = self._eval("partner")
        self.assertGreaterEqual(int(res.floor), int(Tier.ASK))

    def test_family_fires_ask(self):
        res, Tier = self._eval("family")
        self.assertGreaterEqual(int(res.floor), int(Tier.ASK))

    def test_investor_fires_approve(self):
        res, Tier = self._eval("investor")
        self.assertGreaterEqual(int(res.floor), int(Tier.APPROVE))

    def test_cold_no_floor_from_type(self):
        res, Tier = self._eval("cold")
        # A cold contact with a benign message has no relationship-type floor.
        self.assertEqual(int(res.floor), int(Tier.SILENT))


if __name__ == "__main__":
    unittest.main()
