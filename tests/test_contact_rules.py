"""Layer 1A — per-contact rules: VIP 'always-instant' and MUTE 'never'.

Differentiating "never bother me about this person" from "always tell me instantly, no
matter what they say" is, in the owner's words, one of the most important behaviours.

Covers (tier engine, pure): mute forces SILENT; mute still clamped to the hard
guardrail floor (a muted contact's money/legal message still surfaces); VIP wins over
mute; VIP is never quieted by nudge-suppression. And (DB): a VIP conversation skips the
settling delay while a non-VIP one is held; config seeds the flags."""

from __future__ import annotations

import time
import unittest

from assistant.brain.tiers import decide
from assistant.config import Settings
from assistant.ingest import whatsapp_source as wa
from assistant.models import Contact, Reversibility, Stakes, Tier
from assistant.storage import db
from assistant.storage import repositories as repo
from assistant.storage import whatsapp_inbox as inbox
from tests.helpers import make_contact, make_decision, make_message, make_thread


class _Mem:
    """Minimal MemorySignals stand-in: the situation was recently surfaced+skipped."""
    recently_skipped = True
    situation_resolved = False
    is_personal = False


class TestMuteRule(unittest.TestCase):
    def test_mute_forces_silent(self):
        contact = make_contact(flags={"mute"})
        d = make_decision(stakes=Stakes.MEDIUM, proposed_tier=Tier.FYI, confidence=0.95)
        final = decide(make_thread(), d, contact)
        self.assertEqual(final.final_tier, Tier.SILENT)
        self.assertTrue(any("muted" in f for f in final.applied_floors))

    def test_mute_cannot_drop_below_guardrail_floor(self):
        # A muted contact who suddenly sends a money message STILL surfaces — the hard
        # guardrail floor (money → APPROVE) wins over the mute.
        contact = make_contact(flags={"mute"})
        thread = make_thread(make_message("please pay the invoice, wire the payment today"))
        d = make_decision(stakes=Stakes.MEDIUM, proposed_tier=Tier.FYI, confidence=0.95)
        final = decide(thread, d, contact)
        self.assertGreaterEqual(int(final.final_tier), int(Tier.APPROVE))

    def test_vip_beats_mute(self):
        # If a JID is mis-listed as both, VIP wins: never silenced.
        contact = make_contact(flags={"vip", "mute"})
        d = make_decision(stakes=Stakes.LOW, reversibility=Reversibility.REVERSIBLE,
                          needs_reply=True, proposed_tier=Tier.SILENT, confidence=0.95)
        final = decide(make_thread(), d, contact)
        self.assertGreaterEqual(int(final.final_tier), int(Tier.FYI))


class TestVipNeverQuieted(unittest.TestCase):
    def test_non_vip_recently_skipped_is_suppressed(self):
        contact = make_contact()  # ordinary contact
        d = make_decision(stakes=Stakes.LOW, reversibility=Reversibility.REVERSIBLE,
                          needs_reply=False, proposed_tier=Tier.FYI, confidence=0.95)
        final = decide(make_thread(), d, contact, memory=_Mem())
        self.assertEqual(final.final_tier, Tier.SILENT)   # quieted (known + skipped)

    def test_vip_is_not_suppressed(self):
        contact = make_contact(flags={"vip"})
        d = make_decision(stakes=Stakes.LOW, reversibility=Reversibility.REVERSIBLE,
                          needs_reply=False, proposed_tier=Tier.FYI, confidence=0.95)
        final = decide(make_thread(), d, contact, memory=_Mem())
        self.assertGreaterEqual(int(final.final_tier), int(Tier.FYI))  # always heard


class TestVipBypassesSettling(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.settings = Settings()  # settling ON

    def tearDown(self):
        self.conn.close()

    def _insert(self, mid, jid, age_seconds):
        inbox.put(self.conn, mid, {"messageId": mid, "jid": jid, "sender_jid": jid,
                                   "body": "yo", "is_group": False, "ts": 1})
        ca = int(time.time()) - age_seconds
        self.conn.execute("UPDATE whatsapp_inbox SET created_at=?, ts=? WHERE message_id=?",
                          (ca, ca, mid))

    def test_vip_released_immediately_others_held(self):
        vip_jid = "vip@s.whatsapp.net"
        repo.upsert_contact(self.conn, Contact(email=vip_jid, name="VIP", flags={"vip"}))
        self._insert("wa_vip", vip_jid, 2)          # 2s old → would be held if not VIP
        self._insert("wa_other", "other@s.whatsapp.net", 2)  # ordinary → held

        src = wa.WhatsAppSource(self.conn, self.settings)
        ids = src.fetch_new_message_ids()
        self.assertEqual(ids, ["wa_vip"])           # VIP jumps the settling queue
        self.assertEqual(inbox.get(self.conn, "wa_other")["status"], "new")  # still held


class TestStampRuleFlags(unittest.TestCase):
    def test_config_seeds_vip_and_mute(self):
        conn = db.open_db(":memory:")
        try:
            s = Settings(vip_jids=("v@s.whatsapp.net",), mute_jids=("m@s.whatsapp.net",),
                         personal_jids=("p@s.whatsapp.net",))
            wa.stamp_rule_flags(conn, "v@s.whatsapp.net", "V", s)
            wa.stamp_rule_flags(conn, "m@s.whatsapp.net", "M", s)
            wa.stamp_rule_flags(conn, "p@s.whatsapp.net", "P", s)
            self.assertIn("vip", repo.get_contact(conn, "v@s.whatsapp.net").flags)
            self.assertIn("mute", repo.get_contact(conn, "m@s.whatsapp.net").flags)
            self.assertIn("personal", repo.get_contact(conn, "p@s.whatsapp.net").flags)
        finally:
            conn.close()

    def test_vip_wins_when_listed_in_both(self):
        conn = db.open_db(":memory:")
        try:
            s = Settings(vip_jids=("x@s.whatsapp.net",), mute_jids=("x@s.whatsapp.net",))
            wa.stamp_rule_flags(conn, "x@s.whatsapp.net", "X", s)
            flags = repo.get_contact(conn, "x@s.whatsapp.net").flags
            self.assertIn("vip", flags)
            self.assertNotIn("mute", flags)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
