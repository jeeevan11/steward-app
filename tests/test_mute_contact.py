"""Owner-controlled Mute: 'never bother me' for a sender, reversible, guardrail-safe."""

from __future__ import annotations

import unittest

from assistant.storage import db
from assistant.storage import read_queries as rq
from assistant.storage import repositories as repo
from assistant.web import service


class TestMuteContact(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        c = repo.get_or_default_contact(self.conn, "notifications@github.com")
        c.importance = 20
        repo.upsert_contact(self.conn, c)

    def test_mute_then_unmute(self):
        r = service.set_muted(self.conn, "notifications@github.com", True)
        self.assertTrue(r["muted"])
        self.assertTrue(repo.get_or_default_contact(self.conn, "notifications@github.com").is_muted())
        r2 = service.set_muted(self.conn, "notifications@github.com", False)
        self.assertFalse(r2["muted"])
        self.assertFalse(repo.get_or_default_contact(self.conn, "notifications@github.com").is_muted())

    def test_mute_preserves_other_flags(self):
        repo.add_contact_flag(self.conn, "notifications@github.com", "vip")
        service.set_muted(self.conn, "notifications@github.com", True)
        flags = repo.get_or_default_contact(self.conn, "notifications@github.com").flags
        self.assertIn("vip", flags)   # mute toggle never clobbers other flags
        self.assertIn("mute", flags)

    def test_people_row_exposes_is_muted(self):
        service.set_muted(self.conn, "notifications@github.com", True)
        rows = [p for p in rq.list_contacts(self.conn) if "github" in p["email"]]
        self.assertTrue(rows and rows[0]["is_muted"])

    def test_mute_endpoint(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:  # noqa: BLE001
            self.skipTest("fastapi TestClient not installed")
        import tempfile

        from assistant.config import Settings
        from assistant.web import api as webapi

        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        orig = webapi._settings
        webapi._settings = Settings(db_path=tf.name)
        try:
            r = TestClient(webapi.app).post("/api/contacts/notifications@github.com/mute",
                                            json={"muted": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["muted"])
        finally:
            webapi._settings = orig


if __name__ == "__main__":
    unittest.main()
