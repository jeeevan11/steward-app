"""Fix: reasoning-enabled LLM calls were truncating their JSON because max_tokens was
at/below the reasoning budget. Two guards:
  1. the client gives the completion headroom ON TOP OF the reasoning budget;
  2. the schema parser salvages a truncated-but-recoverable JSON object before failing
     safe (and still fails safe when too little was emitted).
"""

from __future__ import annotations

import unittest

from assistant.brain import schema
from assistant.config import Settings
from assistant.llm import client as client_mod
from assistant.llm.client import LLMClient


# ── fake OpenAI-compatible client that records the kwargs it was called with ──
class _Resp:
    def __init__(self):
        self.choices = [type("C", (), {"message": type("M", (), {"content": '{"ok": 1}'})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


class _Completions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kw):
        self.kwargs = kw
        return _Resp()


class _FakeAPI:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _Completions()})()


def _client():
    c = LLMClient(Settings(openrouter_api_key="x"))
    c._client = _FakeAPI()
    return c


class TestMaxTokensHeadroom(unittest.TestCase):
    def test_reasoning_call_gets_headroom_above_reasoning_budget(self):
        c = _client()
        c.classify(system_prefix="p", thread_text="t", schema={}, task="JUDGE")  # reasoning 2048
        kw = c._client.chat.completions.kwargs
        self.assertEqual(kw["extra_body"]["reasoning"]["max_tokens"], 2048)
        self.assertGreaterEqual(kw["max_tokens"], 2048 + client_mod._CONTENT_HEADROOM)

    def test_critical_judge_scales_with_big_reasoning(self):
        c = _client()
        c.classify(system_prefix="p", thread_text="t", schema={}, task="JUDGE_CRITICAL")  # 8192
        self.assertGreaterEqual(c._client.chat.completions.kwargs["max_tokens"],
                                8192 + client_mod._CONTENT_HEADROOM)

    def test_non_reasoning_call_is_unchanged(self):
        c = _client()
        c.noise_pass(system_prefix="p", thread_text="t", schema={})  # reasoning 0, max_tokens 400
        kw = c._client.chat.completions.kwargs
        self.assertNotIn("extra_body", kw)
        self.assertEqual(kw["max_tokens"], 400)


class TestSalvageParser(unittest.TestCase):
    _FULL = ('{"category":"investor","intent":"asks about runway","sender_importance":60,'
             '"stakes":"high","reversibility":"reversible","proposed_tier":3,"confidence":0.9,'
             '"needs_reply":true,"reasoning":"investor question","suggested_action":"ask",'
             '"one_line_summary":"investor asks about the runway situation heading into Q')

    def test_recovers_truncated_object_with_all_required_fields(self):
        # cut mid-way through the LAST field's string value → salvage closes it
        d = schema.parse_decision(self._FULL)
        self.assertFalse(d.is_failsafe)
        self.assertEqual(d.category, "investor")
        self.assertEqual(int(d.proposed_tier), 3)

    def test_fails_safe_when_too_little_emitted(self):
        d = schema.parse_decision('{"category":"investor","inte')
        self.assertTrue(d.is_failsafe)
        self.assertEqual(int(d.proposed_tier), 3)  # Tier.ASK

    def test_plain_valid_json_still_parses(self):
        d = schema.parse_decision(self._FULL + 'uarter.\"}')
        self.assertFalse(d.is_failsafe)

    def test_garbage_fails_safe(self):
        self.assertTrue(schema.parse_decision("not json at all").is_failsafe)
        self.assertTrue(schema.parse_decision("").is_failsafe)


if __name__ == "__main__":
    unittest.main()
