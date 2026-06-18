"""Bulk address-book import (L1) + relay-resolved @lid bridge (L2) + reply-quote rendering.

The structural fix for "known person shows as Unknown": seed the recognition index from the
owner's phone book so anyone they've saved is recognized by their saved name the instant they
message — and bridge a privacy @lid onto that person the moment the relay reveals its number.
"""

from __future__ import annotations

import unittest

from assistant.config import Settings
from assistant.ingest import whatsapp_source as wa
from assistant.memory import identity
from assistant.models import Channel, Message
from assistant.storage import db
from assistant.storage import read_queries as rq
from assistant.storage import repositories as repo


class TestAddressBookImport(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.res = repo.save_contacts_bulk(self.conn, [
            {"name": "Simba", "phones": ["+91 87507 15626"], "emails": []},
            {"name": "Mom", "phones": ["9988776655"], "emails": ["mom@home.com"]},
            {"name": "NoIds", "phones": [], "emails": []},        # skipped
        ])

    def test_import_counts(self):
        self.assertEqual(self.res["imported"], 2)
        self.assertEqual(self.res["links"], 3)   # 1 + (1 phone + 1 email)
        self.assertGreaterEqual(self.res["skipped"], 1)

    def test_saved_number_recognized_on_first_message_with_saved_name(self):
        jid = "918750715626@s.whatsapp.net"
        nm, saved = rq._live_name(self.conn, jid)
        self.assertEqual((nm, saved), ("Simba", True))
        self.assertTrue(rq._contact_info(self.conn, jid)["is_saved"])
        # and identity.resolve hits the seeded person (zero taps)
        m = Message(id="m", thread_id="t", channel=Channel.WHATSAPP,
                    sender_email=jid, sender_name="some-push-name")
        self.assertIsNotNone(identity.resolve(self.conn, m).person_id)

    def test_saved_email_recognized(self):
        self.assertTrue(rq._contact_info(self.conn, "mom@home.com")["is_saved"])

    def test_people_list_not_flooded(self):
        # no contacts rows are created on import → the People tab stays clean until they message
        self.assertEqual(len(rq.list_contacts(self.conn)), 0)

    def test_import_is_idempotent(self):
        again = repo.save_contacts_bulk(self.conn, [{"name": "Simba", "phones": ["+91 87507 15626"]}])
        self.assertEqual(again["links"], 0)

    def test_reimport_never_overrides_an_owner_saved_name(self):
        # 9988776655 belongs to "Mom" (a saved person). Importing a different-named entry with
        # the same number must NOT steal the link OR rename Mom.
        mom_pid = repo.person_link_get(self.conn, "9988776655@s.whatsapp.net")
        repo.save_contacts_bulk(self.conn, [{"name": "Imposter", "phones": ["9988776655"]}])
        self.assertEqual(repo.person_link_get(self.conn, "9988776655@s.whatsapp.net"), mom_pid)
        self.assertEqual(rq._live_name(self.conn, "9988776655@s.whatsapp.net")[0], "Mom")

    def test_import_names_an_unsaved_pushname_person(self):
        # someone who messaged first (unsaved, push-name) gets RENAMED to the book name on import
        jid = "915550001111@s.whatsapp.net"
        c = repo.get_or_default_contact(self.conn, jid, "Self Set Name")
        c.relationship = "wa_contact"; c.name_source = "push"; repo.upsert_contact(self.conn, c)
        repo.save_contacts_bulk(self.conn, [{"name": "Bestie", "phones": ["915550001111"]}])
        self.assertEqual(rq._live_name(self.conn, jid), ("Bestie", True))


class TestLidNumberBridge(unittest.TestCase):
    """L2: a relay-resolved @lid auto-links onto the address-book person."""

    def setUp(self):
        self.conn = db.open_db(":memory:")
        repo.save_contacts_bulk(self.conn, [{"name": "Simba", "phones": ["+91 87507 15626"]}])

    def test_lid_with_resolved_number_links_to_address_book_person(self):
        lid = "269419204890650@lid"
        # before: the @lid is its own unknown
        self.assertIsNone(repo.person_link_get(self.conn, lid))
        wa._bridge_lid_to_resolved_number(self.conn, lid, "+91 87507 15626")
        # after: the @lid now resolves to Simba's person
        pid = repo.person_link_get(self.conn, lid)
        self.assertIsNotNone(pid)
        self.assertEqual(pid, repo.person_link_get(self.conn, "918750715626@s.whatsapp.net"))
        self.assertEqual(rq._live_name(self.conn, lid), ("Simba", True))

    def test_bridge_never_steals_a_linked_lid(self):
        lid = "269419204890650@lid"
        repo.save_contact(self.conn, lid, "Already Named")   # @lid already belongs to someone
        before = repo.person_link_get(self.conn, lid)
        wa._bridge_lid_to_resolved_number(self.conn, lid, "+91 87507 15626")
        self.assertEqual(repo.person_link_get(self.conn, lid), before)  # unchanged


class TestReplyQuoteRendering(unittest.TestCase):
    def test_quote_rendered_as_context_not_repeatable(self):
        s = Settings()
        m = wa.normalize({"jid": "x@lid", "sender_jid": "x@lid", "body": "Club me",
                          "quoted_body": "where are you", "push_name": "Ankita"}, s)
        # the quote is framed as context with a do-not-repeat instruction, not "[replying to: X]"
        self.assertIn("context", m.body_text)
        self.assertIn("where are you", m.body_text)
        self.assertIn("Do not repeat", m.body_text)
        self.assertIn("Club me", m.body_text)
        self.assertNotIn("[replying to:", m.body_text)


if __name__ == "__main__":
    unittest.main()
