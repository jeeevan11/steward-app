"""Phase 8 — improved commitment extraction (inbound + NL dates + lifecycle).

All pure paths are deterministic via a fixed `today` (2026-06-15, a Monday). The
LLM extractor is exercised with a fake client returning canned JSON for both
directions (owner_is_sender True/False) plus a malformed-output case. No network."""

from __future__ import annotations

import json
import unittest
from datetime import date

from assistant.memory import commitment_extract as CE


TODAY = date(2026, 6, 15)  # Monday


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────
class FakeLLM:
    """Returns canned JSON (dict -> json string) and records the system_prefix so a
    test can assert the right owner/counterparty prompt was selected."""

    def __init__(self, payload):
        self.payload = payload
        self.last_system = None

    def complete_json(self, *, task, system_prefix, user_text, schema, max_tokens=700, message_id=""):
        self.last_system = system_prefix
        return json.dumps(self.payload)


class RawLLM:
    """Returns an arbitrary raw string (for malformed-output tests)."""

    def __init__(self, raw):
        self.raw = raw

    def complete_json(self, **kw):
        return self.raw


class BoomLLM:
    def complete_json(self, **kw):
        raise RuntimeError("llm down")


class _Msg:
    def __init__(self, sender_email="", from_me=False):
        self.sender_email = sender_email
        self.from_me = from_me


class _Thread:
    """Minimal Thread-like object: render_for_prompt + latest/latest_inbound."""

    def __init__(self, text="some thread text", *, inbound_sender="alex@x.com"):
        self._text = text
        self.latest = _Msg(sender_email=inbound_sender)
        self.latest_inbound = _Msg(sender_email=inbound_sender)

    def render_for_prompt(self, max_chars=12000):
        return self._text


# ─────────────────────────────────────────────────────────────────────────────
# parse_nl_date — thorough, all deterministic via TODAY
# ─────────────────────────────────────────────────────────────────────────────
class TestParseNlDate(unittest.TestCase):
    def p(self, text):
        return CE.parse_nl_date(text, today=TODAY)

    def test_explicit_iso(self):
        self.assertEqual(self.p("2026-06-20"), "2026-06-20")

    def test_iso_embedded_in_phrase(self):
        self.assertEqual(self.p("by 2026-07-01 please"), "2026-07-01")

    def test_today_and_tonight_and_eod(self):
        self.assertEqual(self.p("today"), "2026-06-15")
        self.assertEqual(self.p("tonight"), "2026-06-15")
        self.assertEqual(self.p("EOD"), "2026-06-15")
        self.assertEqual(self.p("end of day"), "2026-06-15")

    def test_tomorrow(self):
        self.assertEqual(self.p("tomorrow"), "2026-06-16")

    def test_bare_weekday_next_occurrence(self):
        # TODAY is Monday; next Friday is the 19th.
        self.assertEqual(self.p("Friday"), "2026-06-19")
        self.assertEqual(self.p("by fri"), "2026-06-19")

    def test_this_weekday(self):
        # "this friday" -> the upcoming friday this week (19th).
        self.assertEqual(self.p("this friday"), "2026-06-19")

    def test_next_weekday_jumps_a_week(self):
        # TODAY Monday -> nearest upcoming Monday is the 22nd, "next monday" -> 29th.
        self.assertEqual(self.p("next monday"), "2026-06-29")

    def test_next_week(self):
        self.assertEqual(self.p("next week"), "2026-06-22")

    def test_next_month(self):
        self.assertEqual(self.p("next month"), "2026-07-15")

    def test_in_n_days(self):
        self.assertEqual(self.p("in 3 days"), "2026-06-18")

    def test_in_n_weeks(self):
        self.assertEqual(self.p("in 2 weeks"), "2026-06-29")

    def test_in_a_week(self):
        self.assertEqual(self.p("in a week"), "2026-06-22")

    def test_garbage_and_empty(self):
        self.assertEqual(self.p("whenever you get a chance"), "")
        self.assertEqual(self.p(""), "")
        self.assertEqual(self.p(None), "")
        self.assertEqual(self.p("soon-ish"), "")

    def test_malformed_isolike_falls_through(self):
        # Not a valid date and no other cue -> ''.
        self.assertEqual(self.p("2026-13-40"), "")

    def test_deterministic_no_wall_clock(self):
        # Same input + same `today` always gives the same answer.
        self.assertEqual(self.p("friday"), CE.parse_nl_date("friday", today=TODAY))


# ─────────────────────────────────────────────────────────────────────────────
# extract — both directions, counterparty/direction/due_date, malformed -> []
# ─────────────────────────────────────────────────────────────────────────────
class TestExtract(unittest.TestCase):
    def test_owner_outbound(self):
        llm = FakeLLM({"commitments": [
            {"text": "send the deck", "due_date_hint": "Friday",
             "counterparty": "alex@x.com", "direction": "outbound"},
        ]})
        thread = _Thread("I'll send the deck by Friday.")
        out = CE.extract(llm, thread, today=TODAY, owner_is_sender=True)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["owner"], "me")
        self.assertEqual(c["direction"], "outbound")
        self.assertEqual(c["counterparty"], "alex@x.com")
        self.assertEqual(c["due_date"], "2026-06-19")  # Friday parsed via TODAY
        self.assertIn("AUTHOR", llm.last_system)  # owner prompt selected

    def test_counterparty_inbound(self):
        llm = FakeLLM({"commitments": [
            {"text": "review the contract", "due_date_hint": "next week",
             "counterparty": "Dana", "direction": "inbound"},
        ]})
        thread = _Thread("I'll review the contract next week.", inbound_sender="dana@x.com")
        out = CE.extract(llm, thread, today=TODAY, owner_is_sender=False)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["owner"], "them")
        self.assertEqual(c["direction"], "inbound")
        self.assertEqual(c["counterparty"], "dana")
        self.assertEqual(c["due_date"], "2026-06-22")  # next week
        self.assertIn("OTHER PERSON", llm.last_system)  # counterparty prompt selected

    def test_counterparty_defaults_from_thread_when_omitted(self):
        llm = FakeLLM({"commitments": [
            {"text": "send invoice", "due_date_hint": None,
             "counterparty": None, "direction": None},
        ]})
        thread = _Thread("They will send the invoice.", inbound_sender="billing@x.com")
        out = CE.extract(llm, thread, today=TODAY, owner_is_sender=False)
        self.assertEqual(out[0]["counterparty"], "billing@x.com")
        self.assertEqual(out[0]["due_date"], "")          # null hint -> ''
        self.assertEqual(out[0]["direction"], "inbound")  # filled from owner_is_sender

    def test_malformed_llm_output_returns_empty(self):
        self.assertEqual(
            CE.extract(RawLLM("not json at all"), _Thread(), today=TODAY, owner_is_sender=True), []
        )
        self.assertEqual(
            CE.extract(RawLLM('{"unexpected": 1}'), _Thread(), today=TODAY, owner_is_sender=True), []
        )

    def test_llm_failure_returns_empty(self):
        self.assertEqual(
            CE.extract(BoomLLM(), _Thread(), today=TODAY, owner_is_sender=False), []
        )

    def test_empty_thread_returns_empty(self):
        self.assertEqual(
            CE.extract(FakeLLM({"commitments": []}), _Thread(""), today=TODAY, owner_is_sender=True), []
        )

    def test_blank_text_items_skipped(self):
        llm = FakeLLM({"commitments": [
            {"text": "  ", "due_date_hint": "today", "counterparty": "x", "direction": "inbound"},
            {"text": "real one", "due_date_hint": "today", "counterparty": "x", "direction": "inbound"},
        ]})
        out = CE.extract(llm, _Thread(), today=TODAY, owner_is_sender=False)
        self.assertEqual([c["text"] for c in out], ["real one"])


# ─────────────────────────────────────────────────────────────────────────────
# status_of — upcoming / approaching / overdue / forgotten
# ─────────────────────────────────────────────────────────────────────────────
class TestStatusOf(unittest.TestCase):
    def s(self, **c):
        return CE.status_of(c, today=TODAY)

    def test_upcoming(self):
        self.assertEqual(self.s(due_date="2026-06-25"), "upcoming")  # 10 days out

    def test_approaching_within_two_days(self):
        self.assertEqual(self.s(due_date="2026-06-15"), "approaching")  # due today
        self.assertEqual(self.s(due_date="2026-06-16"), "approaching")  # +1
        self.assertEqual(self.s(due_date="2026-06-17"), "approaching")  # +2

    def test_just_outside_approaching_is_upcoming(self):
        self.assertEqual(self.s(due_date="2026-06-18"), "upcoming")  # +3

    def test_overdue(self):
        self.assertEqual(self.s(due_date="2026-06-14"), "overdue")  # 1 day ago
        self.assertEqual(self.s(due_date="2026-06-08"), "overdue")  # 7 days ago (boundary)

    def test_forgotten_when_overdue_more_than_seven_days(self):
        self.assertEqual(self.s(due_date="2026-06-07"), "forgotten")  # 8 days ago
        self.assertEqual(self.s(due_date="2026-05-01"), "forgotten")

    def test_no_due_date_is_upcoming(self):
        self.assertEqual(self.s(due_date=""), "upcoming")
        self.assertEqual(self.s(), "upcoming")

    def test_no_due_date_but_old_creation_is_forgotten(self):
        self.assertEqual(self.s(due_date="", created="2026-06-01"), "forgotten")  # 14d old
        self.assertEqual(self.s(due_date="", created="2026-06-10"), "upcoming")   # 5d old

    def test_bad_input_defaults_upcoming(self):
        self.assertEqual(CE.status_of("not a dict", today=TODAY), "upcoming")
        self.assertEqual(self.s(due_date="garbage"), "upcoming")


if __name__ == "__main__":
    unittest.main()
