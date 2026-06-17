"""Regression: `memory-identity-1` — substring phone matching silently auto-merged
unrelated people into one person at confidence 1.0 with no approval.

Invariants: IDENTITY_SAFETY, NO_AUTO_MERGE_HIGH_CONFIDENCE.

The fix replaced the `identifier LIKE '%<digits>%'` substring match with
`repo.phone_digits_match` (exact, or identical trailing >=10-digit national run) plus an
"exactly one candidate person" ambiguity gate. A phone digit-run that merely appears
*inside* an unrelated longer number must NOT link, and two equally-plausible candidates
must yield no auto-link at all.
"""

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


class TestPhoneMatchHelper(unittest.TestCase):
    def test_exact_equality_matches(self):
        self.assertTrue(repo.phone_digits_match("919876543210", "919876543210"))

    def test_trailing_national_run_matches_across_country_code(self):
        # signature gives the 10-digit national number; JID has the +91 prefix.
        self.assertTrue(repo.phone_digits_match("919876543210", "9876543210"))

    def test_substring_in_the_middle_does_not_match(self):
        # "919876543210" appears as a prefix-substring of the longer number but is a
        # DIFFERENT phone number — must not match (this was the bug).
        self.assertFalse(repo.phone_digits_match("9198765432109999", "919876543210"))

    def test_short_overlap_does_not_match(self):
        self.assertFalse(repo.phone_digits_match("12345678", "999912345678000"))

    def test_too_short_never_matches(self):
        self.assertFalse(repo.phone_digits_match("12345", "12345"))


class TestPhoneLinkByDigits(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _person_with_jid(self, jid, name):
        return identity.resolve(self.conn, _wa(jid, name)).person_id

    def test_substring_jid_is_not_matched(self):
        # A person whose number merely CONTAINS the query digits as a substring.
        other = self._person_with_jid("9198765432109999@s.whatsapp.net", "Stranger")
        self.assertIsNone(repo.person_link_by_phone_digits(self.conn, "919876543210"))
        # And the email never gets fused into that stranger.
        r = identity.resolve(
            self.conn,
            _email("bob@globex.com", "Bob", body="Regards,\nBob\n+91 98765 43210"),
        )
        self.assertTrue(r.created)                 # separate person, NOT merged
        self.assertNotEqual(r.person_id, other)

    def test_exact_number_still_strong_links(self):
        owner = self._person_with_jid("919876543210@s.whatsapp.net", "Bob")
        match = repo.person_link_by_phone_digits(self.conn, "919876543210")
        self.assertEqual(match, owner)

    def test_ambiguous_two_people_same_number_yields_no_link(self):
        # Two distinct persons each owning the same national number → ambiguous → None,
        # so a coincidence can never silently fuse the wrong pair.
        self._person_with_jid("919876543210@s.whatsapp.net", "Bob")
        self._person_with_jid("9876543210@s.whatsapp.net", "Bobby")
        self.assertIsNone(repo.person_link_by_phone_digits(self.conn, "919876543210"))

    def test_autolink_writes_audit_event(self):
        owner = self._person_with_jid("919876543210@s.whatsapp.net", "Bob")
        identity.resolve(
            self.conn,
            _email("bob@globex.com", "Bob", body="Regards,\nBob\n+91 98765 43210"),
        )
        n = repo.count_events(self.conn, type="identity_autolink")
        self.assertGreaterEqual(n, 1)
        # and the email is now linked to the same person (exact match still works).
        self.assertEqual(repo.person_link_get(self.conn, "bob@globex.com"), owner)


if __name__ == "__main__":
    unittest.main()
