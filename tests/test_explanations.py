"""Phase 2 — the explainability engine: structured 'why' per decision.

Covers: building an Explanation from a real tiering result (guardrails extracted,
signals captured, the full floor chain preserved), the human why-summary for
raised/lowered/unchanged tiers, and persistent round-trip."""

from __future__ import annotations

import unittest

from assistant.brain.tiers import decide
from assistant.models import Reversibility, Stakes, Tier
from assistant.storage import db, explanations
from tests.helpers import make_contact, make_decision, make_message, make_thread


class _Mem:
    recently_skipped = True
    situation_resolved = False
    is_personal = False


class TestWhySummary(unittest.TestCase):
    def test_raised_explains_surfacing(self):
        # money keyword → guardrail floors a SILENT proposal up to APPROVE
        thread = make_thread(make_message("please wire the payment / invoice today"))
        d = make_decision(proposed_tier=Tier.SILENT, stakes=Stakes.LOW,
                          reversibility=Reversibility.REVERSIBLE, confidence=0.95)
        final = decide(thread, d, make_contact())
        expl = explanations.build("m1", thread, make_contact(), d, final)
        self.assertGreater(int(final.final_tier), int(final.base_tier))
        self.assertTrue(any("money" in g for g in expl.guardrails))
        self.assertIn("because", expl.summary.lower())

    def test_lowered_explains_suppression(self):
        d = make_decision(proposed_tier=Tier.APPROVE, stakes=Stakes.MEDIUM,
                          reversibility=Reversibility.REVERSIBLE, needs_reply=True, confidence=0.95)
        final = decide(make_thread(), d, make_contact(), suppress_active=True)
        expl = explanations.build("m2", make_thread(), make_contact(), d, final,
                                  suppress_active=True)
        self.assertLess(int(final.final_tier), int(final.base_tier))
        self.assertIn("kept quiet", expl.summary.lower())
        self.assertTrue(expl.suppression_signals["presence_silenced"])

    def test_unchanged_uses_topic(self):
        d = make_decision(proposed_tier=Tier.FYI, stakes=Stakes.LOW,
                          reversibility=Reversibility.REVERSIBLE, needs_reply=False,
                          confidence=0.95, one_line_summary="weekly newsletter")
        final = decide(make_thread(), d, make_contact())
        s = explanations.why_summary(d, final)
        self.assertIn("newsletter", s)


class TestSignalsCaptured(unittest.TestCase):
    def test_memory_and_floors_captured(self):
        d = make_decision(proposed_tier=Tier.APPROVE, stakes=Stakes.LOW,
                          reversibility=Reversibility.REVERSIBLE, needs_reply=False, confidence=0.95)
        final = decide(make_thread(), d, make_contact(), memory=_Mem())
        expl = explanations.build("m3", make_thread(), make_contact(), d, final, memory=_Mem())
        self.assertTrue(expl.memory_signals["recently_skipped"])
        self.assertEqual(expl.model_verdict["category"], d.category)
        self.assertEqual(expl.applied_floors, final.applied_floors)  # full chain preserved


class TestPersistence(unittest.TestCase):
    def test_record_and_get_roundtrip(self):
        conn = db.open_db(":memory:")
        try:
            thread = make_thread(make_message("wire the payment now"))
            d = make_decision(proposed_tier=Tier.SILENT, confidence=0.9)
            final = decide(thread, d, make_contact())
            expl = explanations.build("m1", thread, make_contact(), d, final)
            explanations.record(conn, expl)
            got = explanations.get(conn, "m1")
            self.assertIsNotNone(got)
            self.assertEqual(got["final_tier"], int(final.final_tier))
            self.assertIsInstance(got["guardrails"], list)       # JSON decoded
            self.assertIsInstance(got["suppression_signals"], dict)
            self.assertTrue(got["summary"])
            # upsert: re-record doesn't duplicate
            explanations.record(conn, expl)
            n = conn.execute("SELECT COUNT(*) AS n FROM decision_explanations").fetchone()["n"]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    def test_get_missing_returns_none(self):
        conn = db.open_db(":memory:")
        try:
            self.assertIsNone(explanations.get(conn, "nope"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
