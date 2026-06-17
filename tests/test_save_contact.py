"""The owner's in-app 'Save contact' action and the recognition it drives.

This is the fix for the "saved person reads as Unknown" bug: a WhatsApp @lid (privacy id)
that the auto-pipeline can't match to a phone-book entry. Saving is the trustworthy source
of truth — it must flip every recognition predicate, bridge the @lid to a phone number, and
never send a message (NO_AUTO_SEND is untouched).
"""

from __future__ import annotations

import unittest

from assistant.memory import contacts as cmem
from assistant.memory import retrieval
from assistant.storage import db
from assistant.storage import read_queries as rq
from assistant.storage import repositories as repo


class TestSaveContact(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.lid = "111122223333@lid"

    # ── the unknown baseline ────────────────────────────────────────────────
    def _seed_unknown_wa_sender(self):
        c = repo.get_or_default_contact(self.conn, self.lid, "Nathan Diniz")
        c.relationship = "wa_contact"          # only "someone who messaged"
        c.name_source = "push"                 # a spoofable push-name, no provenance
        repo.upsert_contact(self.conn, c)

    def test_unknown_wa_sender_is_not_recognized(self):
        self._seed_unknown_wa_sender()
        self.assertFalse(rq._contact_info(self.conn, self.lid)["is_saved"])
        self.assertFalse(cmem.is_recognized(repo.get_or_default_contact(self.conn, self.lid)))

    def test_unknown_profile_summary_marks_not_saved(self):
        self._seed_unknown_wa_sender()
        c = repo.get_or_default_contact(self.conn, self.lid)
        self.assertIn("NOT a saved contact", retrieval._profile_summary(c))

    # ── saving flips every recognition predicate ────────────────────────────
    def test_save_makes_recognized_everywhere(self):
        self._seed_unknown_wa_sender()
        res = repo.save_contact(self.conn, self.lid, "Nathan Diniz")
        self.assertTrue(res["ok"])

        c = repo.get_or_default_contact(self.conn, self.lid)
        self.assertEqual(c.name_source, "manual")
        self.assertEqual(c.relationship, "phone_contact")
        self.assertGreaterEqual(c.importance, 20)
        self.assertTrue(c.is_saved)
        self.assertTrue(rq._contact_info(self.conn, self.lid)["is_saved"])
        self.assertTrue(cmem.is_recognized(c))
        self.assertIn("SAVED contact", retrieval._profile_summary(c))

    def test_save_flips_person_is_saved(self):
        self._seed_unknown_wa_sender()
        res = repo.save_contact(self.conn, self.lid, "Nathan Diniz")
        pid = res["person_id"]
        self.assertTrue(pid)
        self.assertTrue(repo.person_is_saved(self.conn, pid))
        self.assertEqual(repo.person_link_get(self.conn, self.lid), pid)

    # ── the @lid ↔ phone bridge (the core of the unknown bug) ────────────────
    def test_phone_bridges_lid_to_number(self):
        self._seed_unknown_wa_sender()
        res = repo.save_contact(self.conn, self.lid, "Nathan Diniz",
                                phone="+1 (415) 555-0199")
        phone_jid = res["phone_jid"]
        self.assertEqual(phone_jid, "14155550199@s.whatsapp.net")
        # same person resolves from BOTH the @lid and the phone jid
        self.assertEqual(repo.person_link_get(self.conn, phone_jid), res["person_id"])
        # a future inbound message that arrives by phone number is now recognized
        self.assertTrue(rq._contact_info(self.conn, phone_jid)["is_saved"])

    # ── one person, not duplicate rows (the "two numbers" bug) ───────────────
    def test_phone_bridge_makes_NO_duplicate_contacts_row(self):
        self._seed_unknown_wa_sender()
        res = repo.save_contact(self.conn, self.lid, "Nathan Diniz", phone="14155550199")
        row = self.conn.execute(
            "SELECT 1 FROM contacts WHERE email=?", (res["phone_jid"],)).fetchone()
        self.assertIsNone(row, "save_contact must not create a duplicate @s.whatsapp.net row")
        self.assertTrue(rq._contact_info(self.conn, res["phone_jid"])["is_saved"])

    def test_people_list_shows_one_row_per_person(self):
        self._seed_unknown_wa_sender()
        repo.save_contact(self.conn, self.lid, "Nathan Diniz", phone="14155550199")
        self.conn.commit()
        rows = [p for p in rq.list_contacts(self.conn) if p["name"] == "Nathan Diniz"]
        self.assertEqual(len(rows), 1, "the @lid + phone must collapse to ONE person row")
        labels = [h["label"] for h in rows[0]["handles"]]
        self.assertTrue(any("14155550199" in x for x in labels))

    def test_owner_asserted_email_links_to_same_person(self):
        self._seed_unknown_wa_sender()
        em = "nathan.x@scaler.com"
        c = repo.get_or_default_contact(self.conn, em, "")
        repo.upsert_contact(self.conn, c)
        res = repo.save_contact(self.conn, self.lid, "Nathan Diniz",
                                phone="14155550199", email=em)
        self.conn.commit()
        self.assertEqual(repo.person_link_get(self.conn, em), res["person_id"])
        self.assertTrue(rq._contact_info(self.conn, em)["is_saved"])
        rows = [p for p in rq.list_contacts(self.conn) if p["name"] == "Nathan Diniz"]
        self.assertEqual(len(rows), 1)

    def test_live_name_resolves_saved_over_pushname(self):
        self._seed_unknown_wa_sender()
        self.assertEqual(rq._live_name(self.conn, self.lid), ("", False))
        repo.save_contact(self.conn, self.lid, "Nathan Diniz")
        nm, saved = rq._live_name(self.conn, self.lid)
        self.assertEqual((nm, saved), ("Nathan Diniz", True))
        self.assertEqual(
            rq._display_name(self.conn, "Nathan", self.lid, "whatsapp"), "Nathan Diniz")

    def test_unsaved_sender_still_uses_pushname_then_number(self):
        jid = "8888@lid"
        c = repo.get_or_default_contact(self.conn, jid, "SomeStranger")
        c.relationship = "wa_contact"; c.name_source = "push"
        repo.upsert_contact(self.conn, c)
        self.assertEqual(rq._live_name(self.conn, jid), ("", False))
        self.assertEqual(rq._display_name(self.conn, "SomeStranger", jid, "whatsapp"),
                         "SomeStranger")

    def test_save_is_idempotent_and_never_downgrades(self):
        self._seed_unknown_wa_sender()
        first = repo.save_contact(self.conn, self.lid, "Nathan Diniz", phone="14155550199")
        # a later push-name upsert (the relay re-caching) must NOT undo the manual save
        c = repo.get_or_default_contact(self.conn, self.lid)
        c.name_source = "push"
        repo.upsert_contact(self.conn, c)
        self.assertEqual(repo.get_or_default_contact(self.conn, self.lid).name_source, "manual")
        # re-saving is stable: same person, importance not reset
        second = repo.save_contact(self.conn, self.lid, "Nathan Diniz", phone="14155550199")
        self.assertEqual(second["person_id"], first["person_id"])
        self.assertGreaterEqual(repo.get_or_default_contact(self.conn, self.lid).importance, 20)

    def test_save_requires_identifier_and_name(self):
        self.assertFalse(repo.save_contact(self.conn, "", "Nathan")["ok"])
        self.assertFalse(repo.save_contact(self.conn, self.lid, "")["ok"])

    def test_business_and_saved_name_sources_are_recognized(self):
        # platform-verified names (WhatsApp Business / phone-book) are trustworthy too
        for src in ("saved", "business", "manual"):
            jid = f"{src}9999@lid"
            c = repo.get_or_default_contact(self.conn, jid, "Acme Co")
            c.relationship = "wa_contact"
            c.name_source = src
            repo.upsert_contact(self.conn, c)
            self.assertTrue(rq._contact_info(self.conn, jid)["is_saved"], src)
            self.assertTrue(cmem.is_recognized(repo.get_or_default_contact(self.conn, jid)), src)

    def test_save_writes_no_message(self):
        # NO_AUTO_SEND: saving touches recognition state only — no pending_actions row.
        self._seed_unknown_wa_sender()
        repo.save_contact(self.conn, self.lid, "Nathan Diniz")
        n = self.conn.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0]
        self.assertEqual(n, 0)


class TestSaveContactEndpoint(unittest.TestCase):
    def test_post_save_recognizes_sender(self):
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
            client = TestClient(webapi.app)
            # seed an unknown WA sender into the same DB the API uses
            conn = db.open_db(tf.name)
            lid = "444455556666@lid"
            c = repo.get_or_default_contact(conn, lid, "Mystery")
            c.relationship = "wa_contact"; c.name_source = "push"
            repo.upsert_contact(conn, c); conn.commit(); conn.close()

            # missing name → rejected
            bad = client.post("/api/contacts/save", json={"identifier": lid, "name": ""})
            self.assertFalse(bad.json().get("ok"))

            # valid save → ok + recognized afterwards
            r = client.post("/api/contacts/save",
                            json={"identifier": lid, "name": "Real Person", "phone": "14155550123"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json().get("ok"))

            conn = db.open_db(tf.name)
            self.assertTrue(rq._contact_info(conn, lid)["is_saved"])
            self.assertEqual(repo.get_or_default_contact(conn, lid).name, "Real Person")
            conn.close()
        finally:
            webapi._settings = orig


if __name__ == "__main__":
    unittest.main()
