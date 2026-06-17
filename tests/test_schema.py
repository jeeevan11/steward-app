import json
import unittest

from assistant.brain import schema
from assistant.models import Reversibility, Stakes, Tier


VALID = {
    "category": "work_request",
    "intent": "requests a decision",
    "sender_importance": 60,
    "stakes": "high",
    "reversibility": "reversible",
    "proposed_tier": 2,
    "confidence": 0.8,
    "needs_reply": True,
    "reasoning": "needs an answer",
    "suggested_action": "reply",
    "one_line_summary": "Alex needs a decision on X",
}


class TestParseDecision(unittest.TestCase):
    def test_valid(self):
        d = schema.parse_decision(json.dumps(VALID))
        self.assertFalse(d.is_failsafe)
        self.assertEqual(d.category, "work_request")
        self.assertEqual(d.stakes, Stakes.HIGH)
        self.assertEqual(d.proposed_tier, 2)

    def test_accepts_dict(self):
        d = schema.parse_decision(dict(VALID))
        self.assertFalse(d.is_failsafe)

    def test_bad_json_failsafe(self):
        d = schema.parse_decision("{not json")
        self.assertTrue(d.is_failsafe)
        self.assertEqual(d.proposed_tier, Tier.ASK)

    def test_not_object_failsafe(self):
        self.assertTrue(schema.parse_decision("[1,2,3]").is_failsafe)

    def test_bad_enum_failsafe(self):
        bad = dict(VALID, category="totally_made_up")
        self.assertTrue(schema.parse_decision(bad).is_failsafe)
        bad2 = dict(VALID, stakes="extreme")
        self.assertTrue(schema.parse_decision(bad2).is_failsafe)

    def test_missing_field_failsafe(self):
        bad = dict(VALID)
        del bad["confidence"]
        self.assertTrue(schema.parse_decision(bad).is_failsafe)

    def test_out_of_range_clamped(self):
        d = schema.parse_decision(dict(VALID, confidence=2.5, sender_importance=999, proposed_tier=3))
        self.assertLessEqual(d.confidence, 1.0)
        self.assertLessEqual(d.sender_importance, 100)

    def test_failsafe_is_irreversible_high_stakes_ask(self):
        d = schema.parse_decision("garbage")
        self.assertEqual(d.stakes, Stakes.HIGH)
        self.assertEqual(d.reversibility, Reversibility.IRREVERSIBLE)
        self.assertEqual(d.proposed_tier, Tier.ASK)


class TestParseNoise(unittest.TestCase):
    def test_valid(self):
        n = schema.parse_noise(json.dumps(
            {"is_noise": True, "confidence": 0.9, "label": "Newsletters", "reason": "bulk"}))
        self.assertTrue(n["is_noise"])
        self.assertEqual(n["label"], "Newsletters")

    def test_parse_error_is_not_noise(self):
        # A parse failure must NOT be treated as noise (we don't silently archive).
        n = schema.parse_noise("nope")
        self.assertFalse(n["is_noise"])


if __name__ == "__main__":
    unittest.main()
