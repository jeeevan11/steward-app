"""P5b — draft quality gate: silent auto-fixes (dashes, filler), flag-only checks
(fabrication, length). It never blocks: a draft always comes back."""

from __future__ import annotations

import unittest

from assistant.action import quality_gate as Q


class TestQualityGate(unittest.TestCase):
    def test_em_dash_autofixed(self):
        r = Q.check_and_fix("Let's meet — Tuesday works.", "external")
        self.assertNotIn("—", r.clean_draft)
        self.assertIn("removed em/en dashes", r.auto_fixed)
        self.assertFalse(r.needs_review)  # auto-fix alone is not a review flag

    def test_filler_phrases_removed(self):
        for phrase in ("I hope this email finds you well.", "Just following up.",
                       "Circling back.", "Please don't hesitate to ask."):
            draft = f"{phrase} Can we ship Friday?"
            r = Q.check_and_fix(draft, "external")
            self.assertNotIn(phrase.split(".")[0].lower(), r.clean_draft.lower(),
                             f"filler not removed: {phrase}")
            self.assertIn("removed AI filler phrases", r.auto_fixed)
            self.assertIn("ship Friday", r.clean_draft)

    def test_fabrication_flagged_but_draft_returned(self):
        # $50,000 and 2030 are not in the source → flagged, but the draft is still here.
        r = Q.check_and_fix("We'll commit $50,000 by 2030.", "external",
                            source_text="Can you invest in our round?")
        self.assertTrue(r.needs_review)
        self.assertTrue(any("fabrication" in f for f in r.flags))
        self.assertTrue(r.clean_draft.strip())

    def test_grounded_numbers_not_flagged(self):
        r = Q.check_and_fix("Yes, $50,000 works.", "external",
                            source_text="Would you put in $50,000?")
        self.assertFalse(any("fabrication" in f for f in r.flags))

    def test_length_flagged_but_draft_returned(self):
        long_draft = " ".join(["word"] * 130)  # team limit is 100
        r = Q.check_and_fix(long_draft, "team")
        self.assertTrue(r.needs_review)
        self.assertTrue(any("long for team" in f for f in r.flags))
        self.assertTrue(r.clean_draft.strip())

    def test_clean_draft_passes(self):
        r = Q.check_and_fix("Sounds good. See you Tuesday.", "external",
                            source_text="Tuesday?")
        self.assertEqual(r.flags, [])
        self.assertEqual(r.auto_fixed, [])
        self.assertFalse(r.needs_review)

    def test_needs_review_only_for_unfixable(self):
        # only an em-dash → auto-fixed, no review needed
        self.assertFalse(Q.check_and_fix("a — b", "external").needs_review)
        # a fabricated specific → review needed
        self.assertTrue(Q.check_and_fix("Pay $9,999 now", "external", source_text="hi").needs_review)


if __name__ == "__main__":
    unittest.main()
