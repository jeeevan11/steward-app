"""P3 — three-step reasoning: THINK → JUDGE → SELF_CRITIQUE.

The fake LLM implements all three steps + the noise pass. Tests cover prep
injection, the raise-only critique, fail-safe on bad JUDGE output, critical routing,
and guardrails.is_critical. All run dry (classify only) — no Gmail is ever touched."""

from __future__ import annotations

import json
import unittest

from assistant.brain import classifier, guardrails
from assistant.memory import retrieval
from assistant.models import Attachment, Contact, Message, Thread
from assistant.storage import db

_VALID_JUDGE = {
    "category": "work_request", "intent": "asks for a call", "sender_importance": 30,
    "stakes": "medium", "reversibility": "reversible", "proposed_tier": 1,
    "confidence": 0.9, "needs_reply": True, "reasoning": "r",
    "suggested_action": "reply", "one_line_summary": "wants a call",
}


class ThreeStepLLM:
    """Records calls and returns canned step outputs (dict → json, or raw str)."""

    def __init__(self, *, judge=None, critique=None, noise=None, think=None):
        self.calls = {"think": 0, "judge": 0, "critique": 0}
        self.judge_task = None
        self.judge_user = None
        self._judge = _VALID_JUDGE if judge is None else judge
        self._critique = critique
        self._noise = noise or {"is_noise": False, "confidence": 0.0, "label": "", "reason": ""}
        self._think = think

    def noise_pass(self, *, system_prefix, thread_text, schema, message_id=""):
        return json.dumps(self._noise)

    def think(self, *, system_prefix, thread_text, schema, message_id=""):
        self.calls["think"] += 1
        payload = self._think or {
            "key_entities": ["a venture firm"], "relationship_context": "existing investor",
            "urgency_signals": [], "ambiguities": [], "preliminary_category": "investor",
        }
        return json.dumps(payload)

    def classify(self, *, system_prefix, thread_text, schema, task="JUDGE", message_id="",
                 effort="high", reasoning_override=None):
        self.calls["judge"] += 1
        self.judge_task = task
        self.judge_user = thread_text
        return self._judge if isinstance(self._judge, str) else json.dumps(self._judge)

    def self_critique(self, *, system_prefix, user_text, schema, message_id=""):
        self.calls["critique"] += 1
        if self._critique is None:
            return json.dumps({"tier_adjustment": 0, "reason": "safe"})
        return self._critique if isinstance(self._critique, str) else json.dumps(self._critique)


def _thread(*, sender="a@x.com", attachments=None):
    m = Message(id="m1", thread_id="t1", sender_email=sender, sender_name="Alex",
                recipients=["me@x.com"], subject="hi", body_text="can we talk?",
                attachments=attachments or [])
    return Thread(id="t1", subject="hi", messages=[m])


def _run(fake, *, thread=None, contact=None):
    conn = db.open_db(":memory:")
    try:
        thread = thread or _thread()
        contact = contact or Contact(email="a@x.com", name="Alex")
        ctx = retrieval.get_context(conn, thread, contact)
        return classifier.classify_thread(conn, fake, thread, ctx, prompts_dir="./prompts")
    finally:
        conn.close()


class TestThreeStep(unittest.TestCase):
    def test_all_three_steps_run(self):
        fake = ThreeStepLLM()
        _run(fake)
        self.assertEqual(fake.calls["think"], 1)
        self.assertEqual(fake.calls["judge"], 1)
        self.assertEqual(fake.calls["critique"], 1)

    def test_think_output_injected_into_judge(self):
        fake = ThreeStepLLM()
        _run(fake)
        self.assertIn("PREP", fake.judge_user)
        self.assertIn("existing investor", fake.judge_user)

    def test_self_critique_can_raise(self):
        fake = ThreeStepLLM(judge={**_VALID_JUDGE, "proposed_tier": 1},
                            critique={"tier_adjustment": 2, "reason": "money involved"})
        dec = _run(fake)
        self.assertEqual(dec.proposed_tier, 3)  # 1 + 2

    def test_self_critique_never_lowers(self):
        # A negative adjustment is clamped to 0 by parse_critique → tier unchanged.
        fake = ThreeStepLLM(judge={**_VALID_JUDGE, "proposed_tier": 2},
                            critique='{"tier_adjustment": -1, "reason": "relax"}')
        dec = _run(fake)
        self.assertEqual(dec.proposed_tier, 2)

    def test_invalid_judge_json_fails_safe(self):
        fake = ThreeStepLLM(judge="this is not json")
        dec = _run(fake)
        self.assertTrue(dec.is_failsafe)
        self.assertEqual(int(dec.proposed_tier), 3)  # Tier.ASK
        # critique must be SKIPPED on a fail-safe (already max involvement)
        self.assertEqual(fake.calls["critique"], 0)

    def test_invalid_critique_preserves_judge(self):
        fake = ThreeStepLLM(judge={**_VALID_JUDGE, "proposed_tier": 1},
                            critique="garbage not json")
        dec = _run(fake)
        self.assertEqual(dec.proposed_tier, 1)  # unchanged

    def test_judge_critical_routed_for_investor_contact(self):
        fake = ThreeStepLLM()
        _run(fake, contact=Contact(email="a@x.com", name="Alex", flags={"investor"}))
        self.assertEqual(fake.judge_task, "JUDGE_CRITICAL")

    def test_judge_standard_for_normal_contact(self):
        fake = ThreeStepLLM()
        _run(fake, contact=Contact(email="a@x.com", name="Alex"))
        self.assertEqual(fake.judge_task, "JUDGE")

    def test_critical_routing_persists_reasoning(self):
        # was_critical is recorded so the dashboard can show which path ran.
        conn = db.open_db(":memory:")
        try:
            thread = _thread(sender="partner@a venture firm.com")
            contact = Contact(email="partner@a venture firm.com", name="VC")
            ctx = retrieval.get_context(conn, thread, contact)
            classifier.classify_thread(conn, ThreeStepLLM(), thread, ctx, prompts_dir="./prompts")
            row = conn.execute(
                "SELECT was_critical, think_output, judge_output FROM decision_log WHERE message_id='m1'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["was_critical"], 1)
            self.assertTrue(row["think_output"])
            self.assertTrue(row["judge_output"])
        finally:
            conn.close()


class _RetryLLM:
    """JUDGE returns empty (reasoning-on) on the first call and a valid decision only
    when reasoning is forced off (the retry). Used to prove the no-reasoning retry."""

    def __init__(self, *, valid=None, always_fail=False):
        self.always_fail = always_fail
        self.valid = valid if valid is not None else _VALID_JUDGE
        self.judge_calls = []  # records reasoning_override per call

    def noise_pass(self, *, system_prefix, thread_text, schema, message_id=""):
        return json.dumps({"is_noise": False, "confidence": 0.0, "label": "", "reason": ""})

    def think(self, *, system_prefix, thread_text, schema, message_id=""):
        return json.dumps({"key_entities": [], "relationship_context": "", "urgency_signals": [],
                           "ambiguities": [], "preliminary_category": ""})

    def classify(self, *, system_prefix, thread_text, schema, task="JUDGE", message_id="",
                 effort="high", reasoning_override=None):
        self.judge_calls.append(reasoning_override)
        if self.always_fail:
            return ""                       # both attempts return empty → fail-safe
        if reasoning_override == 0:          # the retry (reasoning off) → clean JSON
            return json.dumps(self.valid)
        return ""                           # first attempt (reasoning on) → empty

    def self_critique(self, *, system_prefix, user_text, schema, message_id=""):
        return json.dumps({"tier_adjustment": 0, "reason": "ok"})


class TestNoReasoningRetry(unittest.TestCase):
    def test_empty_judge_retries_without_reasoning_and_recovers(self):
        fake = _RetryLLM(valid={**_VALID_JUDGE, "proposed_tier": 2, "category": "work_request"})
        dec = _run(fake)
        self.assertFalse(dec.is_failsafe)                 # recovered
        self.assertEqual(dec.proposed_tier, 2)
        self.assertEqual(dec.category, "work_request")
        self.assertEqual(fake.judge_calls, [None, 0])     # first reasoning-on, then off

    def test_both_attempts_fail_still_fails_safe(self):
        fake = _RetryLLM(always_fail=True)
        dec = _run(fake)
        self.assertTrue(dec.is_failsafe)
        self.assertEqual(int(dec.proposed_tier), 3)       # Tier.ASK
        self.assertEqual(fake.judge_calls, [None, 0])     # exactly one extra attempt


class TestIsCritical(unittest.TestCase):
    def test_investor_domain(self):
        self.assertTrue(
            guardrails.is_critical(_thread(sender="partner@a venture firm.com"),
                                   Contact(email="partner@a venture firm.com"))
        )

    def test_investor_terms(self):
        m = Message(id="m1", thread_id="t1", sender_email="x@y.com",
                    body_text="Sharing the term sheet for the round.")
        self.assertTrue(guardrails.is_critical(Thread(id="t1", messages=[m]), Contact(email="x@y.com")))

    def test_legal_attachment(self):
        t = _thread(attachments=[Attachment(filename="MutualNDA_v3.pdf", mime_type="application/pdf")])
        self.assertTrue(guardrails.is_critical(t, Contact(email="a@x.com")))

    def test_contact_flag(self):
        self.assertTrue(guardrails.is_critical(_thread(), Contact(email="a@x.com", flags={"legal"})))

    def test_benign_is_not_critical(self):
        self.assertFalse(guardrails.is_critical(_thread(), Contact(email="a@x.com")))


if __name__ == "__main__":
    unittest.main()
