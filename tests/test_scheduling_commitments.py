"""Regression tests for the scheduling-commitments cluster (MEDIUM/LOW findings).

Covered findings:
  * control-state-presence-5 — morning/evening briefs gated behind `if mail is not None`
    so a WhatsApp-only deploy never got briefs. Now decoupled from mail presence.
  * control-state-presence-6 — briefs / commitment sweep / state update used exact-hour
    equality (a sleep spanning the target hour silently skipped the day). Now a
    >=-with-window catch-up gate keyed off the existing per-day kv stamp.
  * failure-recovery-4 — a crash between dispatch and ledger.complete replayed
    non-idempotent side effects (episodic double-record + duplicate distill/opportunity
    LLM calls). Now a per-message_id marker makes the replay skip those side effects.
  * memory-knowledge-5 — commitment dedup merged CONTRADICTING promises (no negation
    awareness) and only ever pulled due dates earlier. Now polarity-aware + high-
    confidence-gated due adjustment that also accepts corrected later dates.
  * memory-knowledge-6 — inbound commitment dates anchored on processing wall-clock,
    not the message's send date, so a backlog mis-dated relative/weekday deadlines.
    Now anchored on the inbound Message.timestamp.

Stdlib-only. open_db(":memory:"). Fakes injected; no live engine, no network.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime

from assistant import main
from assistant.config import Settings
from assistant.memory import commitments as C
from assistant.models import Message, Thread, Channel
from assistant.storage import db
from assistant.storage import repositories as repo


def _mkdb():
    return db.open_db(":memory:")


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────
class _FakeNotifier:
    def __init__(self):
        self.texts: list[str] = []
        self.commitments: list[tuple] = []
        self.errors: list[str] = []

    def send_text(self, text):
        self.texts.append(text)

    def send_commitment(self, cid, line):
        self.commitments.append((cid, line))

    def error(self, text):
        self.errors.append(text)


class _FrozenDateTime:
    """A drop-in for the `datetime` name inside main.py whose `.now()` returns a fixed
    instant, so we can drive the hour-gated schedulers deterministically."""

    _fixed: datetime = datetime(2026, 6, 17, 9, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


# ─────────────────────────────────────────────────────────────────────────────
# control-state-presence-6 — catch-up window helper
# ─────────────────────────────────────────────────────────────────────────────
class TestCatchUpWindow(unittest.TestCase):
    def test_before_hour_does_not_fire(self):
        now = datetime(2026, 6, 17, 7, 30)
        self.assertFalse(main._due_today(now, 8))

    def test_exact_hour_fires(self):
        now = datetime(2026, 6, 17, 8, 5)
        self.assertTrue(main._due_today(now, 8))

    def test_late_wake_within_window_fires(self):
        # The bug: a sleep from 07:50-09:10 meant the loop never saw hour==8. A wake at
        # 09:10 must still fire the 08:00 job (catch-up).
        now = datetime(2026, 6, 17, 9, 10)
        self.assertTrue(main._due_today(now, 8))

    def test_very_late_wake_past_window_does_not_fire(self):
        # An upper bound keeps a midnight wake from firing an 18:00 evening brief in the
        # dead of night.
        now = datetime(2026, 6, 18, 0, 30)
        self.assertFalse(main._due_today(now, 18))

    def test_window_boundary_is_exclusive_on_upper(self):
        # default window = 6h; hour 14 is exactly target(8)+6 -> excluded.
        self.assertFalse(main._due_today(datetime(2026, 6, 17, 14, 0), 8))
        self.assertTrue(main._due_today(datetime(2026, 6, 17, 13, 59), 8))


# ─────────────────────────────────────────────────────────────────────────────
# control-state-presence-6 — schedulers fire on a late wake and dedup via the stamp
# ─────────────────────────────────────────────────────────────────────────────
class TestBriefCatchUp(unittest.TestCase):
    def setUp(self):
        self._real_dt = main.datetime
        self._real_gen = None

    def tearDown(self):
        main.datetime = self._real_dt
        if self._real_gen is not None:
            from assistant.control import briefs
            briefs.generate_brief = self._real_gen

    def _patch_brief(self, text):
        from assistant.control import briefs
        self._real_gen = briefs.generate_brief
        briefs.generate_brief = lambda conn, settings, llm, kind: text

    def test_late_wake_still_sends_morning_brief_once(self):
        conn = _mkdb()
        settings = Settings(morning_brief_hour=8, evening_brief_hour=18)
        notifier = _FakeNotifier()
        self._patch_brief("Good morning brief")

        # Wake at 09:10 — AFTER the 08:00 hour. Old exact-hour gate would skip the day.
        _FrozenDateTime._fixed = datetime(2026, 6, 17, 9, 10)
        main.datetime = _FrozenDateTime

        main.maybe_send_briefs(conn, settings, llm=None, notifier=notifier)
        self.assertIn("Good morning brief", notifier.texts)
        self.assertEqual(repo.kv_get(conn, "last_brief_morning"), "2026-06-17")

        # A second poll the same day no-ops via the per-day stamp (no double brief).
        notifier.texts.clear()
        main.maybe_send_briefs(conn, settings, llm=None, notifier=notifier)
        self.assertEqual(notifier.texts, [])

    def test_before_hour_stays_silent(self):
        conn = _mkdb()
        settings = Settings(morning_brief_hour=8, evening_brief_hour=18)
        notifier = _FakeNotifier()
        self._patch_brief("Good morning brief")

        _FrozenDateTime._fixed = datetime(2026, 6, 17, 6, 30)  # before 08:00
        main.datetime = _FrozenDateTime
        main.maybe_send_briefs(conn, settings, llm=None, notifier=notifier)
        self.assertEqual(notifier.texts, [])
        self.assertIsNone(repo.kv_get(conn, "last_brief_morning"))


class TestCommitmentSweepCatchUp(unittest.TestCase):
    def setUp(self):
        self._real_dt = main.datetime

    def tearDown(self):
        main.datetime = self._real_dt

    def test_late_wake_still_runs_commitment_sweep_once(self):
        conn = _mkdb()
        # stale_threads queries decision_log (created by its own migration in prod).
        from assistant.storage import decision_log
        decision_log.ensure(conn)
        settings = Settings(commitment_check_hour=8)
        notifier = _FakeNotifier()
        # A commitment due today so the sweep has something to surface.
        today = date.today().strftime("%Y-%m-%d")
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="finish the report", due_date=today)

        _FrozenDateTime._fixed = datetime(2026, 6, 17, 9, 30)  # past the 08:00 hour
        main.datetime = _FrozenDateTime
        main.maybe_surface_commitments(conn, settings, llm=None, notifier=notifier)
        self.assertEqual(repo.kv_get(conn, "last_commitment_check"), "2026-06-17")
        self.assertGreaterEqual(len(notifier.commitments), 1)

        # Stamp dedups a repeat poll the same day.
        notifier.commitments.clear()
        main.maybe_surface_commitments(conn, settings, llm=None, notifier=notifier)
        self.assertEqual(notifier.commitments, [])


class TestStateUpdateCatchUp(unittest.TestCase):
    def setUp(self):
        self._real_dt = main.datetime

    def tearDown(self):
        main.datetime = self._real_dt

    def test_late_wake_still_stamps_state_update_once(self):
        conn = _mkdb()
        settings = Settings(state_update_hour=7)
        _FrozenDateTime._fixed = datetime(2026, 6, 17, 10, 0)  # well past 07:00
        main.datetime = _FrozenDateTime
        main.maybe_daily_state_update(conn, settings)
        self.assertEqual(repo.kv_get(conn, "last_state_update"), "2026-06-17")

    def test_before_hour_does_not_stamp(self):
        conn = _mkdb()
        settings = Settings(state_update_hour=7)
        _FrozenDateTime._fixed = datetime(2026, 6, 17, 5, 0)  # before 07:00
        main.datetime = _FrozenDateTime
        main.maybe_daily_state_update(conn, settings)
        self.assertIsNone(repo.kv_get(conn, "last_state_update"))


# ─────────────────────────────────────────────────────────────────────────────
# control-state-presence-5 — briefs decoupled from mail presence (source check)
# ─────────────────────────────────────────────────────────────────────────────
class TestBriefsDecoupledFromMail(unittest.TestCase):
    def test_brief_scheduler_runs_outside_mail_block(self):
        # Structural guard: maybe_send_briefs must NOT sit inside the `if mail is not
        # None:` block of the poller loop, else a WhatsApp-only deploy (mail=None) never
        # gets briefs. We assert it is invoked at the channel-agnostic indent level,
        # right after the commitments comment that documents the same Phase 11 fix.
        import inspect
        src = inspect.getsource(main._poller_loop)
        lines = src.splitlines()
        # Find the brief call and the mail-gate line.
        brief_idx = next(i for i, l in enumerate(lines)
                         if "maybe_send_briefs(conn" in l)
        commit_idx = next(i for i, l in enumerate(lines)
                          if "maybe_surface_commitments(conn" in l)
        mail_gate_idx = next(i for i, l in enumerate(lines)
                             if l.strip().startswith("if mail is not None:"))

        def indent(s):
            return len(s) - len(s.lstrip())

        # The brief call sits at the SAME indent as the (channel-agnostic) commitments
        # call, and at a SHALLOWER indent than the body under `if mail is not None:`.
        self.assertEqual(indent(lines[brief_idx]), indent(lines[commit_idx]))
        self.assertLess(indent(lines[brief_idx]),
                        indent(lines[mail_gate_idx]) + 4)
        # And it comes AFTER the mail-gate block opener (decoupled, not inside it).
        self.assertGreater(brief_idx, mail_gate_idx)


# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-5 — negation/polarity-aware dedup
# ─────────────────────────────────────────────────────────────────────────────
def _count_open(conn, contact):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM commitments WHERE status='open' AND contact_email=?",
        (contact,),
    ).fetchone()["n"]


class TestNegationAwareDedup(unittest.TestCase):
    def test_contradicting_promise_is_not_merged(self):
        conn = _mkdb()
        cid = C.add_commitment(
            conn, message_id="m1", contact_email="a@x.com",
            commitment_text="I will send the report by Aug 30", due_date="2026-08-30")
        # The cancelling/contradicting item must NOT dedup-merge and must NOT move the
        # original due date earlier.
        ret = C.add_commitment(
            conn, message_id="m2", contact_email="a@x.com",
            commitment_text="I will not send the report by Aug 20 (deal dead)",
            due_date="2026-08-20")
        self.assertNotEqual(ret, cid)                 # a distinct row, not a merge
        self.assertEqual(_count_open(conn, "a@x.com"), 2)
        # Original due date untouched.
        self.assertEqual(C.get_commitment(conn, cid)["due_date"], "2026-08-30")

    def test_polarity_helpers(self):
        self.assertTrue(C._has_negation("I will not send it"))
        self.assertTrue(C._has_negation("won't send the deck"))
        self.assertTrue(C._has_negation("cancel the order"))
        self.assertFalse(C._has_negation("I will send the deck"))
        self.assertTrue(C._polarity_disagrees("will send X", "won't send X"))
        self.assertFalse(C._polarity_disagrees("will send X", "will send X today"))

    def test_same_polarity_still_dedups(self):
        conn = _mkdb()
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="send the quarterly report to the team")
        C.add_commitment(conn, message_id="m2", contact_email="a@x.com",
                         commitment_text="send the quarterly report to the team today")
        self.assertEqual(_count_open(conn, "a@x.com"), 1)

    def test_two_negated_items_still_dedup(self):
        # Two retractions of the same thing are still duplicates of each other (same
        # negative polarity -> they DO merge).
        conn = _mkdb()
        C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                         commitment_text="will not deliver the annual budget forecast")
        C.add_commitment(conn, message_id="m2", contact_email="a@x.com",
                         commitment_text="won't deliver the annual budget forecast")
        self.assertEqual(_count_open(conn, "a@x.com"), 1)

    def test_corrected_later_date_is_accepted_on_strong_match(self):
        # A re-stated commitment with a CORRECTED LATER deadline must update (the old
        # version silently ignored later corrections).
        conn = _mkdb()
        cid = C.add_commitment(conn, message_id="m1", contact_email="a@x.com",
                               commitment_text="send the deck", due_date="2026-08-10")
        ret = C.add_commitment(conn, message_id="m2", contact_email="a@x.com",
                               commitment_text="send the deck", due_date="2026-08-20")
        self.assertEqual(ret, cid)  # merged (same text, same polarity)
        self.assertEqual(C.get_commitment(conn, cid)["due_date"], "2026-08-20")

    def test_weak_overlap_does_not_rewrite_due_date(self):
        # A borderline (0.6 < sim < 0.8) topical overlap deduplicates but must NOT
        # silently move the surviving deadline (sim 0.75 here < the 0.8 adjust gate).
        conn = _mkdb()
        cid = C.add_commitment(
            conn, message_id="m1", contact_email="a@x.com",
            commitment_text="prepare the budget forecast review",
            due_date="2026-08-30")
        before = C.get_commitment(conn, cid)["due_date"]
        ret = C.add_commitment(
            conn, message_id="m2", contact_email="a@x.com",
            commitment_text="prepare the budget forecast",
            due_date="2026-08-01")
        self.assertEqual(ret, cid)  # still a dedup-merge (sim 0.75 > 0.6)
        self.assertEqual(C.get_commitment(conn, cid)["due_date"], before)  # date untouched


# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-6 — anchor inbound commitment dates on the message timestamp
# ─────────────────────────────────────────────────────────────────────────────
class TestMessageDateAnchor(unittest.TestCase):
    def test_anchor_prefers_message_timestamp(self):
        # Message sent Thursday 2026-06-18; processed "today" is the following Monday.
        sent = datetime(2026, 6, 18, 14, 0)  # a Thursday
        msg = Message(id="x", thread_id="t", sender_email="a@x.com",
                      timestamp=sent.timestamp(), from_me=False)
        run_day = date(2026, 6, 22)  # Monday, several days later (backlog catch-up)
        anchor = C._anchor_date(msg, run_day)
        self.assertEqual(anchor, date(2026, 6, 18))

    def test_anchor_falls_back_to_now_without_timestamp(self):
        msg = Message(id="x", thread_id="t", sender_email="a@x.com",
                      timestamp=0.0, from_me=False)
        run_day = date(2026, 6, 22)
        self.assertEqual(C._anchor_date(msg, run_day), run_day)

    def test_anchor_handles_datetime_now(self):
        msg = Message(id="x", thread_id="t", timestamp=0.0, from_me=False)
        run_dt = datetime(2026, 6, 22, 9, 0)
        self.assertEqual(C._anchor_date(msg, run_dt), date(2026, 6, 22))

    def test_anchor_none_message_uses_today(self):
        # No inbound message at all -> wall-clock today (never raises).
        self.assertEqual(C._anchor_date(None, None), date.today())

    def test_capture_from_inbound_uses_message_date_for_weekday(self):
        # End-to-end: "Friday" in a message sent on a Thursday must resolve to THAT
        # Thursday's upcoming Friday, even when processed days later. We inject a fake
        # extractor LLM that emits a weekday hint and assert the stored due date is the
        # Friday of the message's week, not a Friday in the processing week.
        conn = _mkdb()
        settings = Settings()

        sent = datetime(2026, 6, 18, 10, 0)  # Thursday
        msg = Message(id="wa_in1", thread_id="t1", channel=Channel.WHATSAPP,
                      sender_email="b@x.com", sender_name="Bob",
                      body_text="I'll send it Friday", timestamp=sent.timestamp(),
                      from_me=False)
        thread = Thread(id="t1", channel=Channel.WHATSAPP, subject="", messages=[msg])

        class _WeekdayLLM:
            def complete_json(self, **kwargs):
                return {"commitments": [
                    {"text": "send it", "due_date_hint": "Friday",
                     "counterparty": "b@x.com", "direction": "inbound"}]}

        n = C.capture_from_inbound(conn, _WeekdayLLM(), settings, thread)
        self.assertEqual(n, 1)
        row = conn.execute(
            "SELECT due_date FROM commitments WHERE contact_email='b@x.com'"
        ).fetchone()
        # Thursday 2026-06-18 -> next Friday is 2026-06-19 (the SAME week), not a later one.
        self.assertEqual(row["due_date"], "2026-06-19")


# ─────────────────────────────────────────────────────────────────────────────
# failure-recovery-4 — replay guard for post-dispatch side effects
# ─────────────────────────────────────────────────────────────────────────────
class TestSideEffectReplayGuard(unittest.TestCase):
    def test_marker_roundtrip(self):
        conn = _mkdb()
        self.assertFalse(main._side_effects_already_done(conn, "m1"))
        main._mark_side_effects_done(conn, "m1")
        self.assertTrue(main._side_effects_already_done(conn, "m1"))
        # Idempotent: a second stamp is a no-op, still exactly one row.
        main._mark_side_effects_done(conn, "m1")
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM process_side_effects WHERE message_id='m1'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_empty_message_id_is_safe(self):
        conn = _mkdb()
        # No marker created for a blank id; treated as "not done" (fail-open to run-once).
        main._mark_side_effects_done(conn, "")
        self.assertFalse(main._side_effects_already_done(conn, ""))

    def test_distinct_ids_independent(self):
        conn = _mkdb()
        main._mark_side_effects_done(conn, "m1")
        self.assertTrue(main._side_effects_already_done(conn, "m1"))
        self.assertFalse(main._side_effects_already_done(conn, "m2"))


if __name__ == "__main__":
    unittest.main()
