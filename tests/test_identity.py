"""Memory Part A — cross-channel person identity resolution.

Covers: new-person creation, existing-link reuse, the three STRONG auto-link signals,
weak matches becoming a one-time suggestion (never a silent merge), rejection being
remembered, confirmation merging, and graceful handling of empty input."""

from __future__ import annotations

import unittest

from assistant.memory import identity
from assistant.models import Channel, Message
from assistant.storage import db
from assistant.storage import repositories as repo


def _email(addr, name="", body=""):
    return Message(id="m", thread_id="t", channel=Channel.GMAIL,
                   sender_email=addr, sender_name=name, body_text=body)


def _wa(jid, name="", body=""):
    return Message(id="wa_m", thread_id=jid, channel=Channel.WHATSAPP,
                   sender_email=jid, sender_name=name, body_text=body)


class TestResolution(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_lid_jid_recognized_as_whatsapp(self):
        # WhatsApp's newer @lid identifiers must be treated as JIDs (phone side),
        # not mis-classified as emails.
        import json
        self.assertTrue(identity.is_jid("100000000000001@lid"))
        self.assertFalse(identity.is_email("100000000000001@lid"))
        r = identity.resolve(self.conn, _wa("100000000000001@lid", "Alex Rivera"))
        p = repo.person_get(self.conn, r.person_id)
        self.assertIn("100000000000001@lid", json.loads(p["phone_jids"]))
        self.assertEqual(json.loads(p["emails"]), [])

    def test_new_identifier_creates_person(self):
        r = identity.resolve(self.conn, _email("alice@acme.com", "Alice Roy"))
        self.assertTrue(r.created)
        self.assertTrue(r.person_id)
        self.assertEqual(repo.person_link_get(self.conn, "alice@acme.com"), r.person_id)

    def test_existing_link_returns_same_person(self):
        r1 = identity.resolve(self.conn, _email("alice@acme.com", "Alice Roy"))
        r2 = identity.resolve(self.conn, _email("alice@acme.com", "Alice Roy"))
        self.assertEqual(r1.person_id, r2.person_id)
        self.assertFalse(r2.created)
        self.assertIsNone(r2.suggestion)

    def test_lookup_only_person_id_for(self):
        r = identity.resolve(self.conn, _email("alice@acme.com", "Alice"))
        self.assertEqual(identity.person_id_for(self.conn, "alice@acme.com"), r.person_id)
        self.assertIsNone(identity.person_id_for(self.conn, "nobody@x.com"))

    # ── strong auto-links ──
    def test_email_in_whatsapp_body_strong_links(self):
        p = identity.resolve(self.conn, _email("alice@acme.com", "Alice")).person_id
        # later, a WhatsApp message whose body contains that exact email
        r = identity.resolve(self.conn, _wa("919812345678@s.whatsapp.net", "Alice",
                                            body="hey it's me, alice@acme.com"))
        self.assertEqual(r.person_id, p)   # linked, not a new person
        self.assertFalse(r.created)

    def test_phone_in_email_signature_strong_links(self):
        p = identity.resolve(self.conn, _wa("919876543210@s.whatsapp.net", "Bob")).person_id
        r = identity.resolve(self.conn, _email("bob@globex.com", "Bob",
                                               body="Regards,\nBob\n+91 98765 43210"))
        self.assertEqual(r.person_id, p)
        self.assertFalse(r.created)

    def test_fullname_plus_company_is_suggestion_not_silent_merge(self):
        # memory-identity-4: name + shared corporate domain is SPOOFABLE/shared (every
        # employee + role mailboxes share the domain, and From-names are forgeable), so
        # it must be a confirm-once SUGGESTION, never a silent auto-merge.
        p = identity.resolve(self.conn, _email("asha@acme.com", "Asha Rao")).person_id
        r = identity.resolve(self.conn, _email("asha.rao@acme.com", "Asha Rao"))
        self.assertTrue(r.created)                       # a NEW person, not fused
        self.assertNotEqual(r.person_id, p)
        self.assertIsNotNone(r.suggestion)               # but we ask once
        self.assertEqual(r.suggestion["candidate_person_id"], p)

    def test_fullname_on_free_domain_does_not_autolink(self):
        # gmail.com is not a company signal → must NOT strong-link on name+domain.
        identity.resolve(self.conn, _email("asha@gmail.com", "Asha Rao"))
        r = identity.resolve(self.conn, _email("asha.rao@gmail.com", "Asha Rao"))
        self.assertTrue(r.created)  # separate person (free-mail domain is not a company)

    # ── weak → suggestion, never a silent merge ──
    def test_ambiguous_name_creates_suggestion_not_merge(self):
        p_email = identity.resolve(self.conn, _email("john@acme.com", "John Smith")).person_id
        r = identity.resolve(self.conn, _wa("911111111111@s.whatsapp.net", "John Smith"))
        self.assertTrue(r.created)                 # a NEW person, not merged
        self.assertNotEqual(r.person_id, p_email)
        self.assertIsNotNone(r.suggestion)         # but we ask once
        self.assertEqual(r.suggestion["candidate_person_id"], p_email)

    def test_single_token_name_makes_no_suggestion(self):
        identity.resolve(self.conn, _email("j@acme.com", "John"))
        r = identity.resolve(self.conn, _wa("912222222222@s.whatsapp.net", "John"))
        self.assertIsNone(r.suggestion)            # too noisy to ask on a first name

    def test_rejected_suggestion_is_remembered(self):
        p_email = identity.resolve(self.conn, _email("john@acme.com", "John Smith")).person_id
        jid = "911111111111@s.whatsapp.net"
        r = identity.resolve(self.conn, _wa(jid, "John Smith"))
        self.assertIsNotNone(r.suggestion)
        self.assertTrue(identity.reject_suggestion(self.conn, r.suggestion["id"]))
        # simulate the same identifier coming up the weak path again (drop its link)
        self.conn.execute("DELETE FROM person_links WHERE identifier=?", (jid,))
        self.conn.execute("DELETE FROM persons WHERE id=?", (r.person_id,))
        r2 = identity.resolve(self.conn, _wa(jid, "John Smith"))
        self.assertIsNone(r2.suggestion)           # never asks the rejected pair again
        self.assertTrue(repo.suggestion_exists(self.conn, jid, p_email))

    def test_confirm_suggestion_merges(self):
        p_email = identity.resolve(self.conn, _email("john@acme.com", "John Smith")).person_id
        jid = "911111111111@s.whatsapp.net"
        r = identity.resolve(self.conn, _wa(jid, "John Smith"))
        self.assertTrue(identity.confirm_suggestion(self.conn, r.suggestion["id"]))
        # the jid now resolves to the email person; the throwaway person is gone
        self.assertEqual(identity.person_id_for(self.conn, jid), p_email)
        self.assertIsNone(repo.person_get(self.conn, r.person_id))

    # ── safety / robustness ──
    def test_empty_identifier_is_graceful(self):
        r = identity.resolve(self.conn, _email("", "Nobody"))
        self.assertEqual(r.person_id, "")
        self.assertFalse(r.created)

    def test_double_resolve_does_not_duplicate_suggestion(self):
        identity.resolve(self.conn, _email("john@acme.com", "John Smith"))
        jid = "911111111111@s.whatsapp.net"
        identity.resolve(self.conn, _wa(jid, "John Smith"))     # creates suggestion + person
        r2 = identity.resolve(self.conn, _wa(jid, "John Smith"))  # now has a link → early return
        self.assertIsNone(r2.suggestion)
        self.assertFalse(r2.created)


if __name__ == "__main__":
    unittest.main()
