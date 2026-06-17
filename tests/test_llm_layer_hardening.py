"""Regression tests for the whatsapp-llm cluster's LLM-layer findings:

  llm-layer-1  Flash fallback is now wired (RouteSpec.has_fallback + _chat fail-over),
               and a CRITICAL task that runs on the fallback is recorded fallback=True.
  llm-layer-3  Transient errors (timeout/5xx/429) are NOT mis-handled as 'reasoning
               rejected' — reasoning is preserved; only a real param rejection degrades.
  llm-layer-4  Daily-spend cap + 429 circuit breaker + media byte cap all fail safe.
  llm-layer-5  transcribe/describe_image route token usage through the metrics sink and
               enforce the input-media byte cap.
  llm-layer-6  complete_text attributes spend to a non-empty task label + message_id.

Stdlib only; fully fake OpenAI-compatible client (no network), mirroring the existing
tests/test_llm_truncation.py pattern.
"""

from __future__ import annotations

import unittest

from assistant.config import Settings
from assistant.llm import client as client_mod
from assistant.llm.client import LLMClient, LLMError, _SpendBreakerGuard, _looks_transient
from assistant.llm.router import CRITICAL_TASKS, Task, TaskRouter


# ── fakes ─────────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, content='{"ok": 1}', pt=100, ct=50):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": pt, "completion_tokens": ct})()


class _Completions:
    """Records each create() call and can be scripted to raise on the first N calls /
    on calls that target a specific model."""

    def __init__(self):
        self.calls: list[dict] = []
        self.raise_on_models: dict[str, Exception] = {}
        self.raise_first: list[Exception] = []  # pop one per call until empty

    def create(self, **kw):
        self.calls.append(kw)
        if self.raise_first:
            raise self.raise_first.pop(0)
        model = kw.get("model")
        if model in self.raise_on_models:
            raise self.raise_on_models[model]
        return _Resp()


class _FakeAPI:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _Completions()})()


def _settings():
    return Settings(openrouter_api_key="x", judge_model="google/gemini-2.5-flash",
                    noise_model="google/gemini-2.5-flash", draft_model="deepseek/deepseek-chat",
                    pro_model="google/gemini-2.5-pro")


def _client(sink=None):
    c = LLMClient(_settings(), metrics_sink=sink)
    c._client = _FakeAPI()
    return c


class _SinkRec:
    def __init__(self):
        self.records: list[dict] = []

    def __call__(self, rec):
        self.records.append(rec)


# Fake transient + parameter errors that carry a status_code (the SDK shape).
class _Err(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


# ── llm-layer-1: fallback wiring ───────────────────────────────────────────────
class TestRouterFallback(unittest.TestCase):
    def setUp(self):
        self.r = TaskRouter(_settings())

    def test_has_fallback_true_for_pro_tasks(self):
        spec = self.r.resolve(Task.JUDGE_CRITICAL)
        self.assertTrue(spec.has_fallback())
        self.assertEqual(spec.fallback_model, "google/gemini-2.5-flash")

    def test_has_fallback_false_when_primary_is_already_flash(self):
        # NOISE_FILTER runs on flash, which IS the fallback → nothing to fail over to.
        self.assertFalse(self.r.resolve(Task.NOISE_FILTER).has_fallback())

    def test_new_media_text_tasks_resolve(self):
        for t in (Task.TRANSCRIBE, Task.DESCRIBE_IMAGE, Task.TEXT):
            self.assertTrue(self.r.resolve(t).model)
            self.assertIn(t, Task.ALL)


class TestChatFallover(unittest.TestCase):
    def test_pro_outage_fails_over_to_flash_and_marks_fallback(self):
        sink = _SinkRec()
        c = _client(sink)
        comp = c._client.chat.completions
        # Pro is down for this model only; flash is healthy.
        comp.raise_on_models["google/gemini-2.5-pro"] = _Err("model unavailable", 503)
        out = c.classify(system_prefix="p", thread_text="t", schema={}, task=Task.JUDGE_CRITICAL)
        self.assertTrue(out)  # got a real answer, not a fail-safe
        # The fallback model was actually called.
        models = [call["model"] for call in comp.calls]
        self.assertIn("google/gemini-2.5-flash", models)
        # CRITICAL task ran on fallback → recorded fallback=True (never silent).
        self.assertTrue(sink.records[-1]["fallback"])
        self.assertEqual(sink.records[-1]["task"], "JUDGE_CRITICAL")

    def test_no_fallback_when_primary_is_flash_raises(self):
        c = _client()
        comp = c._client.chat.completions
        comp.raise_on_models["google/gemini-2.5-flash"] = _Err("boom", 503)
        # NOISE_FILTER is flash with no distinct fallback → must raise LLMError.
        with self.assertRaises(LLMError):
            c.noise_pass(system_prefix="p", thread_text="t", schema={})


# ── llm-layer-3: transient vs parameter rejection ──────────────────────────────
class TestTransientClassification(unittest.TestCase):
    def test_looks_transient_detects_timeouts_and_5xx_and_429(self):
        self.assertTrue(_looks_transient(_Err("request timed out", 504)))
        self.assertTrue(_looks_transient(_Err("rate limit exceeded", 429)))
        self.assertTrue(_looks_transient(_Err("upstream 503")))

    def test_param_rejection_is_not_transient(self):
        self.assertFalse(_looks_transient(_Err("bad request: unknown extra_body", 400)))
        self.assertFalse(_looks_transient(_Err("invalid response_format", 422)))

    def test_transient_keeps_reasoning_then_fails_over(self):
        # A transient error on the 'full' attempt must NOT strip reasoning; it should fail
        # over (or raise), never silently re-fire the SAME pro call with reasoning off.
        sink = _SinkRec()
        c = _client(sink)
        comp = c._client.chat.completions
        # First call (pro, full reasoning) hits a transient 503; fallback flash succeeds.
        comp.raise_first = [_Err("temporarily unavailable", 503)]
        c.classify(system_prefix="p", thread_text="t", schema={}, task=Task.JUDGE_CRITICAL)
        # The first (failed) call MUST have carried reasoning (extra_body) — not stripped.
        self.assertIn("extra_body", comp.calls[0])
        # No call ever re-fired pro WITHOUT reasoning (the llm-layer-3 bug).
        pro_calls_no_reasoning = [
            call for call in comp.calls
            if call["model"] == "google/gemini-2.5-pro" and "extra_body" not in call
        ]
        self.assertEqual(pro_calls_no_reasoning, [])

    def test_real_param_rejection_still_strips_reasoning(self):
        # A genuine 400 about extra_body SHOULD degrade by dropping reasoning (preserved
        # behavior). Make the pro call fail with reasoning, then succeed without it.
        c = _client()
        comp = c._client.chat.completions

        original = comp.create
        state = {"n": 0}

        def create(**kw):
            comp.calls.append(kw)
            state["n"] += 1
            if state["n"] == 1 and "extra_body" in kw:
                raise _Err("unsupported parameter: extra_body", 400)
            return _Resp()

        comp.create = create  # type: ignore[method-assign]
        out = c.classify(system_prefix="p", thread_text="t", schema={}, task=Task.JUDGE)
        self.assertTrue(out)
        # The retry dropped extra_body (param degradation path still works).
        self.assertNotIn("extra_body", comp.calls[-1])


# ── llm-layer-4: spend cap + breaker + media cap ───────────────────────────────
class TestSpendBreakerGuard(unittest.TestCase):
    def test_daily_cap_blocks_further_calls(self):
        g = _SpendBreakerGuard()
        g.check()  # fine at zero
        g.add_cost(client_mod._DAILY_SPEND_CAP_USD + 1.0)
        with self.assertRaises(LLMError):
            g.check()

    def test_breaker_trips_after_threshold_429s(self):
        g = _SpendBreakerGuard()
        for _ in range(client_mod._BREAKER_THRESHOLD):
            g.on_rate_limit()
        with self.assertRaises(LLMError):
            g.check()  # breaker open
        g.on_success()  # a success resets the breaker
        g.check()       # no longer raises

    def test_chat_refuses_when_cap_exceeded(self):
        c = _client()
        c._guard.add_cost(client_mod._DAILY_SPEND_CAP_USD + 5.0)
        with self.assertRaises(LLMError):
            c.classify(system_prefix="p", thread_text="t", schema={}, task=Task.JUDGE)
        # No model call was made — we refused before spending.
        self.assertEqual(c._client.chat.completions.calls, [])

    def test_media_too_large_is_rejected(self):
        # A base64 string larger than the cap is refused before any network call.
        big = "A" * (client_mod._MEDIA_MAX_BYTES * 2)
        c = _client()
        with self.assertRaises(LLMError):
            c.transcribe(audio_b64=big)
        self.assertEqual(c._client.chat.completions.calls, [])

    def test_media_under_cap_is_allowed(self):
        c = _client()
        out = c.transcribe(audio_b64="AAAA")  # tiny
        self.assertIsInstance(out, str)
        self.assertEqual(len(c._client.chat.completions.calls), 1)


# ── llm-layer-5: media metering ────────────────────────────────────────────────
class TestMediaMetering(unittest.TestCase):
    def test_transcribe_records_cost_to_sink(self):
        sink = _SinkRec()
        c = _client(sink)
        c.transcribe(audio_b64="AAAA", message_id="m1")
        self.assertEqual(len(sink.records), 1)
        self.assertEqual(sink.records[0]["task"], Task.TRANSCRIBE)
        self.assertEqual(sink.records[0]["message_id"], "m1")
        self.assertGreater(sink.records[0]["prompt_tokens"], 0)

    def test_describe_image_records_cost_to_sink(self):
        sink = _SinkRec()
        c = _client(sink)
        c.describe_image("AAAA", message_id="m2")
        self.assertEqual(len(sink.records), 1)
        self.assertEqual(sink.records[0]["task"], Task.DESCRIBE_IMAGE)
        self.assertEqual(sink.records[0]["message_id"], "m2")

    def test_describe_image_oversized_returns_none_no_call(self):
        c = _client()
        big = "A" * (client_mod._MEDIA_MAX_BYTES * 2)
        self.assertIsNone(c.describe_image(big))
        self.assertEqual(c._client.chat.completions.calls, [])


# ── llm-layer-6: complete_text attribution ─────────────────────────────────────
class TestCompleteTextAttribution(unittest.TestCase):
    def test_default_label_is_non_empty(self):
        sink = _SinkRec()
        c = _client(sink)
        c.complete_text(system_prefix="s", user_prompt="u")
        self.assertEqual(sink.records[0]["task"], Task.TEXT)
        self.assertNotEqual(sink.records[0]["task"], "")

    def test_explicit_task_and_message_id_threaded(self):
        sink = _SinkRec()
        c = _client(sink)
        c.complete_text(system_prefix="s", user_prompt="u", task="BRIEF", message_id="b7")
        self.assertEqual(sink.records[0]["task"], "BRIEF")
        self.assertEqual(sink.records[0]["message_id"], "b7")


if __name__ == "__main__":
    unittest.main()
