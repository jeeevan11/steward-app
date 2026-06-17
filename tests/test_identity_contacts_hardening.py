"""Regression tests for the identity-contacts hardening cluster.

Covers three MEDIUM findings (additive hardening on top of the existing identity
safety layer — exact phone match, name+domain as a suggestion, audit events):

  * memory-identity-3 — @lid WhatsApp JIDs are canonicalized to their phone JID
    BEFORE identity resolution, so email<->WhatsApp unification works for LID
    contacts instead of silently creating a duplicate person.
  * memory-identity-5 — a free-mail / no-domain person is NOT weak-matched (and so
    not offered for a one-tap merge) on a BARE common display name alone; a second
    corroborating cross-reference is required.
  * memory-identity-6 — phone_contacts.sync does NOT promote a spoofable
    whatsapp_inbox push_name to a recognized contact (relationship/importance floor);
    only a phonebook-grade (relay contacts.upsert) name confers recognition.

Stdlib only; open_db(":memory:"); fakes injected (LID map file, relay sources).
Mirrors tests/test_identity.py and tests/test_phone_contacts_relay_auth.py.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from assistant.memory import identity
from assistant.memory import phone_contacts as pc
from assistant.memory import contacts as contacts_mod
from assistant.models import Channel, Message
from assistant.storage import db
from assistant.storage import repositories as repo


def _email(addr, name="", body=""):
    return Message(id="m", thread_id="t", channel=Channel.GMAIL,
                   sender_email=addr, sender_name=name, body_text=body)


def _wa(jid, name="", body=""):
    return Message(id="wa_m", thread_id=jid, channel=Channel.WHATSAPP,
                   sender_email=jid, sender_name=name, body_text=body)


# ─────────────────────────────────────────────────────────────────────────────
# memory-identity-3 — @lid resolution
# ─────────────────────────────────────────────────────────────────────────────
class TestLidResolution(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        # A temp lid_jid_map.json the relay would have written.
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        self._patch = mock.patch.object(
            identity, "_LID_JID_MAP_PATH", self.tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.close()
        os.unlink(self.tmp.name)
        self.conn.close()

    def _write_map(self, mapping: dict):
        self.tmp.seek(0)
        self.tmp.truncate()
        json.dump(mapping, self.tmp)
        self.tmp.flush()

    def test_lid_digits_never_treated_as_phone(self):
        # The opaque LinkedID digits must not be exposed as a dialable number.
        self.assertEqual(identity._jid_digits("9136083337274@lid"), "")
        # Phone/group JIDs still yield digits.
        self.assertEqual(
            identity._jid_digits("919164536565@s.whatsapp.net"), "919164536565")
        self.assertTrue(identity.is_lid("9136083337274@lid"))
        self.assertFalse(identity.is_lid("919164536565@s.whatsapp.net"))

    def test_lid_folds_onto_existing_phone_person(self):
        # A person already known by their phone JID.
        phone = "919164536565@s.whatsapp.net"
        pid = identity.resolve(self.conn, _wa(phone, "Acme Rep")).person_id
        # The relay now reports this contact's LID alias.
        lid = "9136083337274@lid"
        self._write_map({lid: phone})
        r = identity.resolve(self.conn, _wa(lid, "Acme Rep"))
        # No duplicate person: the @lid folds onto the phone person.
        self.assertFalse(r.created)
        self.assertEqual(r.person_id, pid)
        # Both identifiers now resolve to the one human.
        self.assertEqual(identity.person_id_for(self.conn, lid), pid)
        self.assertEqual(identity.person_id_for(self.conn, phone), pid)
        # Observability event recorded.
        self.assertEqual(
            repo.count_events(self.conn, type="identity_lid_resolved"), 1)

    def test_lid_resolves_email_person_via_phone_unification(self):
        # memory-identity-3 core scenario: same human reached first by @lid, later by
        # an email signature carrying their real phone. Resolve the @lid to the phone
        # JID up front so the email's phone signature unifies onto ONE person.
        phone = "919164536565@s.whatsapp.net"
        lid = "9136083337274@lid"
        self._write_map({lid: phone})
        # First contact arrives as a @lid → canonicalized to the phone JID person.
        r1 = identity.resolve(self.conn, _wa(lid, "Asha Rao"))
        self.assertTrue(r1.created)
        # The person owns the canonical phone JID (not the @lid as a fake phone).
        p = repo.person_get(self.conn, r1.person_id)
        jids = json.loads(p["phone_jids"])
        self.assertIn(phone, jids)
        self.assertIn(lid, jids)  # alias also attached
        # Later: an email whose signature contains that exact phone number.
        r2 = identity.resolve(
            self.conn,
            _email("asha@corp-acme.com", "Asha Rao",
                   body="Regards,\nAsha\n+91 91645 36565"))
        # Unified onto the SAME person — no duplicate.
        self.assertFalse(r2.created)
        self.assertEqual(r2.person_id, r1.person_id)

    def test_unresolved_lid_does_not_phone_match(self):
        # With NO mapping available, an @lid stays its own identifier and its digits
        # are NOT compared against a real phone number that happens to share a prefix.
        self._write_map({})  # empty map
        # A real person on the phone side whose number is unrelated to the LID digits.
        phone_person = identity.resolve(
            self.conn, _wa("913608333727@s.whatsapp.net", "Someone")).person_id
        lid = "9136083337274@lid"  # leading digits overlap but it's an opaque alias
        r = identity.resolve(self.conn, _wa(lid, "Other Person"))
        # A NEW, separate person — never fused with the phone person on digit overlap.
        self.assertTrue(r.created)
        self.assertNotEqual(r.person_id, phone_person)

    def test_lid_resolves_via_already_linked_person_without_map(self):
        # Fallback path: even with no on-disk map, if a person already owns BOTH the
        # @lid and a phone JID, a later @lid message folds onto that phone person.
        phone = "919164536565@s.whatsapp.net"
        lid = "9136083337274@lid"
        self._write_map({lid: phone})
        first = identity.resolve(self.conn, _wa(lid, "Asha Rao")).person_id
        # Now the map disappears (relay restart / mid-write); link still reconciles.
        self._write_map({})
        r = identity.resolve(self.conn, _wa(lid, "Asha Rao"))
        self.assertFalse(r.created)
        self.assertEqual(r.person_id, first)


# ─────────────────────────────────────────────────────────────────────────────
# memory-identity-5 — free-mail bare-name weak match suppression
# ─────────────────────────────────────────────────────────────────────────────
class TestFreeMailWeakMatch(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_freemail_bare_common_name_makes_no_suggestion(self):
        # A free-mail (gmail) person 'Rahul Sharma' with company=''. A WhatsApp message
        # from a different human with the SAME push_name must NOT produce a merge
        # suggestion on the bare name alone.
        p = identity.resolve(self.conn, _email("rahul123@gmail.com", "Rahul Sharma")).person_id
        r = identity.resolve(self.conn, _wa("911111111111@s.whatsapp.net", "Rahul Sharma"))
        self.assertTrue(r.created)
        self.assertNotEqual(r.person_id, p)
        self.assertIsNone(r.suggestion)  # not asked to merge two strangers
        # Suppression is observable.
        self.assertGreaterEqual(
            repo.count_events(self.conn, type="identity_weaklink_suppressed"), 1)

    def test_corporate_person_still_suggested_on_name(self):
        # Regression guard: a CORPORATE-domain person (company set) keeps the existing
        # cross-channel name suggestion (the domain itself is corroboration).
        p = identity.resolve(self.conn, _email("john@acme.com", "John Smith")).person_id
        r = identity.resolve(self.conn, _wa("912222222222@s.whatsapp.net", "John Smith"))
        self.assertTrue(r.created)
        self.assertIsNotNone(r.suggestion)
        self.assertEqual(r.suggestion["candidate_person_id"], p)

    def test_freemail_with_corroborating_email_in_body_is_suggested(self):
        # A free-mail person, but the incoming WhatsApp body QUOTES that person's email
        # — a genuine cross-reference. The suggestion is allowed (with corroboration).
        p = identity.resolve(self.conn, _email("rahul123@gmail.com", "Rahul Sharma")).person_id
        r = identity.resolve(
            self.conn,
            _wa("911111111111@s.whatsapp.net", "Rahul Sharma",
                body="hey it's me — rahul123@gmail.com"))
        # NOTE: a quoted email that already belongs to a person is a STRONG link, so
        # this folds directly (no suggestion needed) — the strongest possible outcome.
        self.assertEqual(r.person_id, p)
        self.assertFalse(r.created)

    def test_freemail_phone_in_email_body_is_strong_link_not_just_suggestion(self):
        # A free-mail person who ALSO owns a phone JID. An incoming EMAIL whose body
        # carries that exact phone number is the STRONGEST signal (a known JID in the
        # signature) — it folds directly (better than a suggestion). Guards that the
        # free-mail hardening did not weaken the existing strong phone-signature path.
        p = identity.resolve(self.conn, _email("meera.k@gmail.com", "Meera Kapoor")).person_id
        identity._attach_identifier(
            self.conn, p, "919800011122@s.whatsapp.net", source="observed")
        r = identity.resolve(
            self.conn,
            _email("meerak.work@gmail.com", "Meera Kapoor",
                   body="call me on +91 98000 11122"))
        self.assertFalse(r.created)
        self.assertEqual(r.person_id, p)

    def test_freemail_jid_digit_overlap_corroborates_weak_suggestion(self):
        # Exercises the weak-path corroboration (_has_corroborating_signal case c):
        # the candidate is a free-mail person who also owns a phone JID, and the
        # incoming WhatsApp JID's digits match that JID (e.g. a country-code variant
        # the strong path did not auto-link). Same name + a real-number overlap is a
        # corroborated weak match → a suggestion (not suppressed, not a silent merge).
        p = identity.resolve(self.conn, _email("meera.k@gmail.com", "Meera Kapoor")).person_id
        identity._attach_identifier(
            self.conn, p, "919800011122@s.whatsapp.net", source="observed")
        # Incoming WhatsApp JID with the same 10-digit national number, no country code.
        r = identity.resolve(self.conn, _wa("9800011122@s.whatsapp.net", "Meera Kapoor"))
        self.assertTrue(r.created)
        self.assertIsNotNone(r.suggestion)
        self.assertEqual(r.suggestion["candidate_person_id"], p)

    def test_freemail_jid_no_overlap_is_suppressed(self):
        # Same free-mail candidate, but an incoming WhatsApp JID whose number does NOT
        # overlap → bare-name collision → suppressed (no suggestion).
        p = identity.resolve(self.conn, _email("meera.k@gmail.com", "Meera Kapoor")).person_id
        identity._attach_identifier(
            self.conn, p, "919800011122@s.whatsapp.net", source="observed")
        r = identity.resolve(self.conn, _wa("915550006666@s.whatsapp.net", "Meera Kapoor"))
        self.assertTrue(r.created)
        self.assertIsNone(r.suggestion)
        self.assertGreaterEqual(
            repo.count_events(self.conn, type="identity_weaklink_suppressed"), 1)

    def test_existing_strong_and_suggestion_behavior_unchanged(self):
        # Guard the broader contract: a corporate name+domain near-match is still a
        # suggestion (memory-identity-4), free-mail name+domain still does not autolink.
        identity.resolve(self.conn, _email("asha@gmail.com", "Asha Rao"))
        r = identity.resolve(self.conn, _email("asha.rao@gmail.com", "Asha Rao"))
        self.assertTrue(r.created)               # separate person
        self.assertIsNone(r.suggestion)          # free-mail name-only → no suggestion


# ─────────────────────────────────────────────────────────────────────────────
# memory-identity-6 — push_name not promoted to recognition
# ─────────────────────────────────────────────────────────────────────────────
class _FakeContact:
    """Minimal stand-in mirroring the fields contacts.is_recognized() reads."""

    def __init__(self, row):
        self.relationship = row["relationship"]
        self.flags = row["flags"] or ""
        self.importance = row["importance"]
        self.reply_rate = row["reply_rate"]
        self.notes = row["notes"]


def _contact_row(conn, email):
    return conn.execute(
        "SELECT relationship, flags, importance, reply_rate, notes FROM contacts WHERE email=?",
        (email,),
    ).fetchone()


class TestPushNameRecognition(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _sync(self, *, live=None, lid_map=None, cache=None, inbox=None):
        with mock.patch.object(pc, "_fetch_relay_live",
                               return_value=(live or {}, lid_map or {})), \
             mock.patch.object(pc, "_read_cache_file", return_value=(cache or {})), \
             mock.patch.object(pc, "_read_inbox_names", return_value=(inbox or {})):
            return pc.sync(self.conn)

    def test_inbox_only_pushname_is_not_recognized(self):
        # An unsaved/adversarial sender sets a trusted-looking push_name and sends once.
        jid = "918888800001@s.whatsapp.net"
        res = self._sync(inbox={jid: "Mom"})
        row = _contact_row(self.conn, jid)
        self.assertIsNotNone(row)
        # The recognition floor was NOT applied: relationship empty, importance 0.
        self.assertEqual((row["relationship"] or "").strip(), "")
        self.assertEqual(int(row["importance"] or 0), 0)
        # is_recognized() therefore returns False — card renders 'not a saved contact'.
        self.assertFalse(contacts_mod.is_recognized(_FakeContact(row)))
        self.assertGreaterEqual(res.get("pushname_held", 0), 1)
        # And the hold-back is observable.
        self.assertGreaterEqual(
            repo.count_events(self.conn, type="contact_pushname_holdback"), 1)

    def test_phonebook_name_confers_recognition(self):
        # A relay contacts.upsert (phonebook-grade) name DOES confer recognition.
        jid = "919999900002@s.whatsapp.net"
        self._sync(live={jid: "Real Saved Contact"})
        row = _contact_row(self.conn, jid)
        self.assertEqual((row["relationship"] or "").strip(), "wa_contact")
        self.assertGreaterEqual(int(row["importance"] or 0), 5)
        self.assertTrue(contacts_mod.is_recognized(_FakeContact(row)))

    def test_cache_name_is_phonebook_grade(self):
        # The on-disk contact_cache.json is also relay/contacts.upsert-sourced.
        jid = "919999900003@s.whatsapp.net"
        self._sync(cache={jid: "Cached Saved Contact"})
        row = _contact_row(self.conn, jid)
        self.assertEqual((row["relationship"] or "").strip(), "wa_contact")
        self.assertTrue(contacts_mod.is_recognized(_FakeContact(row)))

    def test_pushname_never_overwrites_saved_recognition(self):
        # A genuinely saved contact (phonebook) is later messaged; the inbox push_name
        # must not be able to LOWER or change the established recognition.
        jid = "919999900004@s.whatsapp.net"
        self._sync(live={jid: "Saved Name"})           # establishes recognition
        before = _contact_row(self.conn, jid)
        self.assertEqual((before["relationship"] or "").strip(), "wa_contact")
        # Now only an inbox push_name is present (relay forgot the contact this cycle).
        self._sync(inbox={jid: "Spoofy McSpoof"})
        after = _contact_row(self.conn, jid)
        # Recognition preserved; spoofy name did not overwrite the saved display name.
        self.assertEqual((after["relationship"] or "").strip(), "wa_contact")
        self.assertGreaterEqual(int(after["importance"] or 0), 5)
        self.assertTrue(contacts_mod.is_recognized(_FakeContact(after)))

    def test_phonebook_overrides_prior_inbox_only(self):
        # An inbox-only stranger is later actually saved (relay reports a name) — the
        # phonebook sync then promotes them to recognized.
        jid = "919999900005@s.whatsapp.net"
        self._sync(inbox={jid: "Maybe Real"})
        row = _contact_row(self.conn, jid)
        self.assertFalse(contacts_mod.is_recognized(_FakeContact(row)))
        self._sync(live={jid: "Now Saved"})
        row2 = _contact_row(self.conn, jid)
        self.assertTrue(contacts_mod.is_recognized(_FakeContact(row2)))

    def test_lid_inbox_only_not_recognized(self):
        # LID resolution path must also withhold recognition when the copied name is
        # only an inbox push_name (not phonebook-grade).
        lid = "9136083337274@lid"
        phone = "919164536565@s.whatsapp.net"
        self._sync(inbox={lid: "Stranger LID"}, lid_map={lid: phone})
        for key in (lid, phone):
            row = _contact_row(self.conn, key)
            if row is not None:
                self.assertFalse(
                    contacts_mod.is_recognized(_FakeContact(row)),
                    f"{key} should not be recognized from an inbox-only push_name")

    def test_lid_phonebook_name_recognized(self):
        # LID resolution with a phonebook-grade name DOES confer recognition on both keys.
        lid = "9136083337274@lid"
        phone = "919164536565@s.whatsapp.net"
        self._sync(live={phone: "Saved LID Contact"}, lid_map={lid: phone})
        for key in (lid, phone):
            row = _contact_row(self.conn, key)
            self.assertIsNotNone(row, f"{key} row should exist")
            self.assertTrue(
                contacts_mod.is_recognized(_FakeContact(row)),
                f"{key} should be recognized from a phonebook name")


if __name__ == "__main__":
    unittest.main()
