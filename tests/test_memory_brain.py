"""Memory Part C — wiring memory into the decision path.

Holds the three hard lines:
  1. Nudge suppression can ONLY lower, never below a guardrail floor, never on an
     irreversible item, never turning a Tier-3 into a silent action.
  2. memory_conflict on a consequential item SURFACES (ASK) — never acts on the
     assumption — even if memory says otherwise.
  3. Recency: the latest message wins (prompt-enforced; asserted present).
"""

from __future__ import annotations

import unittest

from assistant.brain import guardrails
from assistant.brain.tiers import TierConfig, decide
from assistant.memory.distill import RelationshipMemory
from assistant.memory.retrieval import (
    MemorySignals, build_memory_block, memory_signals, record_episode,
)
from assistant.models import Reversibility, Stakes, Tier
from assistant.storage import db
from tests.helpers import make_contact, make_decision, make_message, make_thread


class TestNudgeSuppression(unittest.TestCase):
    def test_suppression_blocked_by_guardrail_floor(self):
        """THE floor test: a money thread floors to APPROVE; even though memory shows
        Jatin recently skipped this, suppression must NOT drop it below that floor."""
        thread = make_thread(make_message("Please pay the invoice via wire transfer by Friday."))
        dec = make_decision(category="personal", stakes=Stakes.LOW,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.APPROVE,
                            needs_reply=False, confidence=0.95)
        sig = MemorySignals(recently_skipped=True)
        floor = guardrails.evaluate(thread, dec, make_contact()).floor
        final = decide(thread, dec, make_contact(), TierConfig(), memory=sig)
        self.assertEqual(floor, Tier.APPROVE)               # money → guardrail floor
        self.assertEqual(final.final_tier, Tier.APPROVE)    # suppression clamped to it
        self.assertGreaterEqual(int(final.final_tier), int(floor))

    def test_suppression_lowers_when_genuinely_safe(self):
        # Same decision, but a benign thread (no guardrail floor) → suppression lowers it.
        thread = make_thread(make_message("just checking in, no rush at all"))
        dec = make_decision(category="personal", stakes=Stakes.LOW,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.APPROVE,
                            needs_reply=False, confidence=0.95)
        final = decide(thread, dec, make_contact(), TierConfig(), memory=MemorySignals(recently_skipped=True))
        self.assertLess(int(final.final_tier), int(Tier.APPROVE))   # quieted down

    def test_tier3_suppressed_never_to_silent(self):
        # A soft Tier-3 (model ambiguity, no guardrail floor) may be quieted, but a
        # suppressed Tier-3 can never become a silent action — at most FYI.
        thread = make_thread(make_message("circling back on that earlier thing"))
        dec = make_decision(category="personal", stakes=Stakes.LOW,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.ASK,
                            needs_reply=False, confidence=0.95)
        final = decide(thread, dec, make_contact(), TierConfig(), memory=MemorySignals(recently_skipped=True))
        self.assertEqual(final.final_tier, Tier.FYI)
        self.assertNotEqual(final.final_tier, Tier.SILENT)

    def test_irreversible_never_suppressed(self):
        thread = make_thread(make_message("can you confirm the transfer?"))
        dec = make_decision(category="personal", stakes=Stakes.LOW,
                            reversibility=Reversibility.IRREVERSIBLE, proposed_tier=Tier.APPROVE,
                            needs_reply=False, confidence=0.95)
        final = decide(thread, dec, make_contact(), TierConfig(), memory=MemorySignals(recently_skipped=True))
        self.assertGreaterEqual(int(final.final_tier), int(Tier.APPROVE))

    def test_no_memory_means_no_suppression(self):
        thread = make_thread(make_message("hello"))
        dec = make_decision(category="personal", stakes=Stakes.LOW,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.APPROVE,
                            needs_reply=False, confidence=0.95)
        final = decide(thread, dec, make_contact(), TierConfig(), memory=None)
        self.assertEqual(final.final_tier, Tier.APPROVE)   # unchanged from today's behavior


class TestMemoryConflict(unittest.TestCase):
    def test_conflict_on_consequential_surfaces_not_acts(self):
        # Memory said the investor agreed; the new message reverses it. memory_conflict
        # is set → must surface (ASK), never act on the old assumption, even though the
        # model itself proposed to handle it silently.
        thread = make_thread(make_message(
            "Actually we're passing on the round, ignore my earlier commitment."))
        dec = make_decision(category="investor", stakes=Stakes.HIGH,
                            reversibility=Reversibility.IRREVERSIBLE, proposed_tier=Tier.SILENT,
                            needs_reply=True, confidence=0.95, memory_conflict=True)
        final = decide(thread, dec, make_contact(), TierConfig())
        self.assertEqual(final.final_tier, Tier.ASK)
        self.assertNotEqual(final.final_tier, Tier.SILENT)

    def test_conflict_never_silent_even_low_stakes(self):
        thread = make_thread(make_message("wait, that's not what we agreed"))
        dec = make_decision(category="personal", stakes=Stakes.LOW,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.SILENT,
                            needs_reply=False, confidence=0.95, memory_conflict=True)
        final = decide(thread, dec, make_contact(), TierConfig())
        self.assertGreaterEqual(int(final.final_tier), int(Tier.APPROVE))

    def test_conflict_not_suppressed_even_if_recently_skipped(self):
        thread = make_thread(make_message("this contradicts what you sent the lawyer"))
        dec = make_decision(category="legal", stakes=Stakes.HIGH,
                            reversibility=Reversibility.IRREVERSIBLE, proposed_tier=Tier.SILENT,
                            needs_reply=True, confidence=0.95, memory_conflict=True)
        final = decide(thread, dec, make_contact(), TierConfig(),
                       memory=MemorySignals(recently_skipped=True, situation_resolved=True))
        self.assertEqual(final.final_tier, Tier.ASK)   # conflict overrides suppression

    def test_guardrail_conflict_floor_direct(self):
        thread = make_thread(make_message("x"))
        consequential = make_decision(category="investor", memory_conflict=True, proposed_tier=Tier.SILENT)
        self.assertEqual(guardrails.evaluate(thread, consequential, make_contact()).floor, Tier.ASK)
        benign = make_decision(category="personal", stakes=Stakes.LOW,
                               reversibility=Reversibility.REVERSIBLE, memory_conflict=True,
                               proposed_tier=Tier.SILENT)
        self.assertGreaterEqual(guardrails.evaluate(thread, benign, make_contact()).floor, Tier.APPROVE)


class TestMemoryBlockAndSignals(unittest.TestCase):
    def test_block_compact_capped_and_recency_stated(self):
        mem = RelationshipMemory("p", summary={f"k{i}": "v" * 50 for i in range(40)},
                                 open_situations=[{"situation": "awaiting quote", "awaiting": "them",
                                                   "status": "open"}],
                                 decided=[{"decision": "reconnect after launch"}],
                                 episodes=[{"action": "surfaced", "tier": 2}])
        block = build_memory_block(mem, cap=1800)
        self.assertTrue(block)
        self.assertLessEqual(len(block), 1800)              # capped
        self.assertIn("RECENCY", block)                     # latest-message-wins stated
        self.assertIn("awaiting quote", block)

    def test_empty_memory_block_is_blank(self):
        self.assertEqual(build_memory_block(RelationshipMemory("p")), "")

    def test_signals_recently_skipped_respects_cooldown(self):
        import time as _t
        from assistant.config import Settings
        from assistant.memory import distill
        conn = db.open_db(":memory:")
        try:
            settings = Settings(memory_nudge_cooldown_hours=24)
            thread = make_thread(make_message("hi"))   # thread.id == "t1"
            mem = distill.load_memory(conn, "p")
            mem.episodes = [{"action": "skipped", "tier": 2, "ts": int(_t.time()), "thread_id": "t1"}]
            distill.save_memory(conn, mem)
            sig = memory_signals(conn, "p", thread, make_contact(), settings)
            self.assertTrue(sig.recently_skipped)
            # an old skip (2 days ago) is outside the cooldown
            mem.episodes = [{"action": "skipped", "tier": 2, "ts": int(_t.time()) - 2 * 86400, "thread_id": "t1"}]
            distill.save_memory(conn, mem)
            self.assertFalse(memory_signals(conn, "p", thread, make_contact(), settings).recently_skipped)
        finally:
            conn.close()

    def test_record_episode_appends(self):
        conn = db.open_db(":memory:")
        try:
            record_episode(conn, "p", action="surfaced", tier=2, thread_id="t1")
            from assistant.memory import distill
            self.assertEqual(len(distill.load_memory(conn, "p").episodes), 1)
        finally:
            conn.close()


class TestRecencyPromptsPresent(unittest.TestCase):
    def test_prompts_state_recency_rule(self):
        with open("prompts/classifier.md", encoding="utf-8") as f:
            classifier = f.read()
        with open("prompts/think.md", encoding="utf-8") as f:
            think = f.read()
        self.assertIn("RECENCY", classifier)
        self.assertIn("memory_conflict", classifier)
        self.assertIn("latest message always wins", think.lower())


if __name__ == "__main__":
    unittest.main()
