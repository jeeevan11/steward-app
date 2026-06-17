import unittest

from assistant.brain.tiers import TierConfig, decide
from assistant.models import Reversibility, Stakes, Tier
from tests.helpers import make_contact, make_decision, make_message, make_thread

CFG = TierConfig(
    vip_importance_threshold=70,
    surface_confidence_threshold=0.75,
    autonomy_confidence_threshold=0.85,
    conservative=True,
)


class TestTierEngine(unittest.TestCase):
    def test_confident_low_stakes_noise_stays_silent(self):
        thread = make_thread(make_message("weekly digest"))
        dec = make_decision(category="newsletter", stakes=Stakes.LOW,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.SILENT,
                            confidence=0.95, needs_reply=False)
        fd = decide(thread, dec, make_contact(), CFG)
        self.assertEqual(fd.final_tier, Tier.SILENT)
        self.assertTrue(fd.is_autonomous)

    def test_vip_needing_reply_goes_to_approve(self):
        thread = make_thread(make_message("can we meet next week?"))
        dec = make_decision(category="scheduling", stakes=Stakes.MEDIUM,
                            proposed_tier=Tier.FYI, confidence=0.95, needs_reply=True)
        fd = decide(thread, dec, make_contact(importance=85), CFG)
        self.assertGreaterEqual(fd.final_tier, Tier.APPROVE)

    def test_low_confidence_consequential_surfaces(self):
        thread = make_thread(make_message("thoughts?"))
        dec = make_decision(category="personal", stakes=Stakes.HIGH,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.FYI,
                            confidence=0.5, needs_reply=True)
        fd = decide(thread, dec, make_contact(), CFG)
        self.assertEqual(fd.final_tier, Tier.ASK)
        self.assertIsNotNone(fd.surfaced_reason)

    def test_conservative_calibration_blocks_uncertain_autonomy(self):
        # medium-stakes, low confidence, model wanted to handle silently → bump to APPROVE
        thread = make_thread(make_message("quick request"))
        dec = make_decision(category="work_request", stakes=Stakes.MEDIUM,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.SILENT,
                            confidence=0.6, needs_reply=False)
        fd = decide(thread, dec, make_contact(), CFG)
        self.assertEqual(fd.final_tier, Tier.APPROVE)

    def test_guardrail_floor_beats_model(self):
        # model says silent + very confident, but money keywords floor it.
        thread = make_thread(make_message("please wire the payment for invoice #22"))
        dec = make_decision(category="personal", stakes=Stakes.LOW,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.SILENT,
                            confidence=0.99, needs_reply=False)
        fd = decide(thread, dec, make_contact(), CFG)
        self.assertGreaterEqual(fd.final_tier, Tier.APPROVE)

    def test_failsafe_always_ask(self):
        from assistant.models import Decision
        thread = make_thread(make_message("???"))
        fd = decide(thread, Decision.failsafe("boom"), make_contact(), CFG)
        self.assertEqual(fd.final_tier, Tier.ASK)

    def test_memory_importance_overrides_model_low_estimate(self):
        thread = make_thread(make_message("can you review this?"))
        dec = make_decision(category="personal", sender_importance=5, stakes=Stakes.MEDIUM,
                            proposed_tier=Tier.SILENT, confidence=0.95, needs_reply=True)
        fd = decide(thread, dec, make_contact(importance=90), CFG)
        self.assertGreaterEqual(fd.final_tier, Tier.APPROVE)

    def test_never_lowers_below_base(self):
        thread = make_thread(make_message("fyi"))
        dec = make_decision(category="personal", stakes=Stakes.LOW,
                            reversibility=Reversibility.REVERSIBLE, proposed_tier=Tier.ASK,
                            confidence=0.99, needs_reply=True)
        fd = decide(thread, dec, make_contact(), CFG)
        self.assertEqual(fd.final_tier, Tier.ASK)


if __name__ == "__main__":
    unittest.main()
