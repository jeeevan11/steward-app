"""GAP 4 — GET /api/brief structured morning brief + caching."""

from __future__ import annotations

import json
import time
import unittest
from datetime import date, timedelta

from assistant.control import briefs
from assistant.storage import db
from assistant.storage import repositories as repo


def _mkdb():
    return db.open_db(":memory:")


def _add_person_with_situation(conn, pid, name, rel_type, situation, awaiting, age_hours):
    repo.person_add(conn, person_id=pid, display_name=name, emails=[f"{pid}@x.com"])
    repo.person_link_set(conn, f"{pid}@x.com", pid, confidence=1.0, source="observed")
    repo.set_person_relationship_type(conn, pid, rel_type)
    last_ts = int(time.time()) - age_hours * 3600
    sit = [{"key": "k1", "situation": situation, "awaiting": awaiting,
            "status": "open", "last_activity_ts": last_ts}]
    repo.relationship_memory_upsert(
        conn, pid, summary_json="{}", open_situations_json=json.dumps(sit),
        decided_json="[]", episodes_json="[]", superseded_json="[]",
        last_distilled_at=last_ts, version=1,
    )


class TestStructuredBrief(unittest.TestCase):
    def test_commitment_due_soon_surfaces(self):
        conn = _mkdb()
        from assistant.memory import commitments as C
        tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="Send the deck", due_date=tomorrow)
        brief = briefs.generate_structured_brief(conn)
        types = [b["type"] for b in brief["bullets"]]
        self.assertIn("commitment", types)
        self.assertEqual(brief["top_priority"], "Send the deck")

    def test_open_situation_awaiting_owner(self):
        conn = _mkdb()
        _add_person_with_situation(conn, "p1", "Bob", "collaborator",
                                   "needs your sign-off", "owner", age_hours=20)
        brief = briefs.generate_structured_brief(conn)
        types = [b["type"] for b in brief["bullets"]]
        self.assertIn("open_situation", types)

    def test_relationship_attention_for_important_type(self):
        conn = _mkdb()
        _add_person_with_situation(conn, "p1", "VC", "investor",
                                   "waiting on your answer", "me", age_hours=30)
        brief = briefs.generate_structured_brief(conn)
        types = [b["type"] for b in brief["bullets"]]
        self.assertIn("relationship_attention", types)

    def test_awaiting_them_excluded(self):
        conn = _mkdb()
        _add_person_with_situation(conn, "p1", "Bob", "collaborator",
                                   "they owe you", "them", age_hours=30)
        brief = briefs.generate_structured_brief(conn)
        types = [b["type"] for b in brief["bullets"]]
        self.assertNotIn("open_situation", types)
        self.assertNotIn("relationship_attention", types)


class TestCaching(unittest.TestCase):
    def test_cache_hit_returns_same(self):
        conn = _mkdb()
        first = briefs.build_or_get_brief(conn)
        # Mutate the underlying data; a fresh cached read should NOT reflect it.
        from assistant.memory import commitments as C
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="new thing",
                         due_date=date.today().strftime("%Y-%m-%d"))
        second = briefs.build_or_get_brief(conn)
        self.assertEqual(first["generated_at"], second["generated_at"])

    def test_stale_cache_regenerates(self):
        conn = _mkdb()
        # Seed a stale cached brief (7h old).
        stale = {"generated_at": int(time.time()) - 7 * 3600, "bullets": [], "top_priority": "old"}
        repo.kv_set(conn, "brief_today", json.dumps(stale))
        fresh = briefs.build_or_get_brief(conn)
        self.assertGreater(fresh["generated_at"], stale["generated_at"])


class TestEndpoint(unittest.TestCase):
    def test_endpoint_200(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:  # noqa: BLE001
            self.skipTest("fastapi TestClient not installed")
        from assistant.config import Settings
        from assistant.web import api as webapi
        # Point the API at an in-memory DB via a temp file so get_conn works.
        import tempfile
        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        orig = webapi._settings
        webapi._settings = Settings(db_path=tf.name)
        try:
            client = TestClient(webapi.app)
            r = client.get("/api/brief")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIn("generated_at", body)
            self.assertIn("bullets", body)
            self.assertIn("top_priority", body)
        finally:
            webapi._settings = orig


if __name__ == "__main__":
    unittest.main()
