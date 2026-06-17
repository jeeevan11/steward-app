"""Memory Part D — the safety backstops.

  1. A personal/family person is floored to surface-only by a HARD floor that nothing
     can lower — not suppression, not high confidence, not a fully populated record.
  2. Staleness: an old record's "resolved" no longer suppresses (trust the new
     message), and the memory block says so.
"""

from __future__ import annotations

import time
import unittest

from assistant.brain import guardrails
from assistant.brain.tiers import TierConfig, decide
from assistant.config import Settings
from assistant.memory import distill, retrieval
from assistant.memory.distill import RelationshipMemory
from assistant.memory.retrieval import MemorySignals, build_memory_block, memory_signals
from assistant.models import Contact, Reversibility, Stakes, Tier
from assistant.storage import db
from assistant.storage import repositories as repo
from tests.helpers import make_contact, make_decision, make_message, make_thread


class TestPersonalHardFloor(unittest.TestCase):
    def test_personal_person_never_auto_handled_even_with_rich_memory(self):
        # Contact has NO flag here — the floor comes purely from the PERSON being
        # personal (memory.is_personal). Memory is fully populated AND shows the exact
        # signals that would normally suppress (recently skipped + resolved) plus the
        # model wants to handle it silently at high confidence. It must still ASK.
        thread = make_thread(make_message("haha yeah see you sunday"))
        dec = make_decision(category="personal", stakes=Stakes.LOW,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.SILENT,
                            needs_reply=False, confidence=0.99)
        sig = MemorySignals(is_personal=True, recently_skipped=True, situation_resolved=True)
        final = decide(thread, dec, make_contact(), TierConfig(), memory=sig)
        self.assertEqual(final.final_tier, Tier.ASK)
        self.assertNotIn(final.final_tier, (Tier.SILENT, Tier.FYI))

    def test_guardrail_personal_floor_direct(self):
        thread = make_thread(make_message("hi"))
        g = guardrails.evaluate(thread, make_decision(proposed_tier=Tier.SILENT),
                                make_contact(), memory=MemorySignals(is_personal=True))
        self.assertEqual(g.floor, Tier.ASK)

    def test_personal_detected_cross_channel(self):
        # A person whose WhatsApp identifier is flagged personal must read as personal
        # even when this message arrives on their (unflagged) email identifier.
        conn = db.open_db(":memory:")
        try:
            repo.person_add(conn, person_id="P", display_name="Mom",
                            emails=["mom@x.com"], phone_jids=["9111@s.whatsapp.net"])
            c = repo.get_or_default_contact(conn, "9111@s.whatsapp.net")
            c.flags.add("personal")
            repo.upsert_contact(conn, c)
            email_contact = repo.get_or_default_contact(conn, "mom@x.com")  # not flagged
            self.assertTrue(retrieval._person_is_personal(conn, "P", email_contact))
        finally:
            conn.close()


class TestStaleness(unittest.TestCase):
    def _save_resolved(self, conn, person_id, *, age_days):
        mem = RelationshipMemory(person_id)
        mem.open_situations = [{"key": "k", "situation": "done", "status": "resolved", "thread_id": "t1"}]
        mem.last_distilled_at = int(time.time()) - age_days * 86400
        distill.save_memory(conn, mem)

    def test_stale_resolved_does_not_suppress(self):
        conn = db.open_db(":memory:")
        try:
            self._save_resolved(conn, "p", age_days=60)              # old record
            sig = memory_signals(conn, "p", make_thread(make_message("x")), make_contact(),
                                 Settings())
            self.assertFalse(sig.situation_resolved)                 # stale → don't trust it
            dec = make_decision(category="personal", stakes=Stakes.LOW,
                                reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.APPROVE,
                                needs_reply=False, confidence=0.95)
            final = decide(make_thread(make_message("x")), dec, make_contact(), TierConfig(), memory=sig)
            self.assertEqual(final.final_tier, Tier.APPROVE)         # NOT suppressed
        finally:
            conn.close()

    def test_fresh_resolved_does_suppress(self):
        conn = db.open_db(":memory:")
        try:
            self._save_resolved(conn, "p", age_days=0)               # fresh record
            sig = memory_signals(conn, "p", make_thread(make_message("x")), make_contact(),
                                 Settings())
            self.assertTrue(sig.situation_resolved)
            dec = make_decision(category="personal", stakes=Stakes.LOW,
                                reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.APPROVE,
                                needs_reply=False, confidence=0.95)
            final = decide(make_thread(make_message("x")), dec, make_contact(), TierConfig(), memory=sig)
            self.assertLess(int(final.final_tier), int(Tier.APPROVE))  # suppressed (fresh)
        finally:
            conn.close()

    def test_block_marks_stale_record(self):
        mem = RelationshipMemory("p", summary={"company": "Acme"},
                                 last_distilled_at=int(time.time()) - 60 * 86400)
        block = build_memory_block(mem, now=time.time())
        self.assertIn("out of date", block.lower())


if __name__ == "__main__":
    unittest.main()
