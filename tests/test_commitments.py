"""P4 — commitment tracker + calendar context.

Extraction is best-effort (LLM); the query/CRUD layer and the calendar free-slot
math are pure and fully tested here. No network."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

from assistant.config import Settings
from assistant.memory import calendar_context as cal
from assistant.memory import commitments as C
from assistant.storage import db


def _settings(**kw) -> Settings:
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com",
                telegram_chat_id="1")
    base.update(kw)
    return Settings(**base)


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, *, task, system_prefix, user_text, schema, max_tokens=700, message_id=""):
        return json.dumps(self.payload)


class BoomLLM:
    def complete_json(self, **kw):
        raise RuntimeError("llm down")


# ── extraction ───────────────────────────────────────────────────────────────
class TestExtraction(unittest.TestCase):
    def test_extracts_explicit_promise(self):
        llm = FakeLLM({"commitments": [
            {"commitment_text": "send the deck", "due_date_hint": "2026-06-20", "contact_email": "a@x.com"}
        ]})
        out = C.extract_commitments(llm, _settings(), "I'll send the deck by Saturday.", "a@x.com")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["due_date"], "2026-06-20")
        self.assertEqual(out[0]["contact_email"], "a@x.com")

    def test_no_false_extraction(self):
        llm = FakeLLM({"commitments": []})
        self.assertEqual(C.extract_commitments(llm, _settings(), "Thanks, got it!", "a@x.com"), [])

    def test_llm_failure_returns_empty(self):
        self.assertEqual(C.extract_commitments(BoomLLM(), _settings(), "I'll follow up.", "a@x.com"), [])

    def test_due_date_normalization(self):
        from datetime import date
        self.assertEqual(C._normalize_date("2026-06-20"), "2026-06-20")
        self.assertEqual(C._normalize_date(None), "")
        self.assertEqual(C._normalize_date("absolutely not a date"), "")
        # Phase 8: natural-language hints now resolve (deterministic via an explicit today).
        self.assertEqual(C._normalize_date("Friday", today=date(2026, 6, 15)), "2026-06-19")
        self.assertEqual(C._normalize_date("tomorrow", today=date(2026, 6, 15)), "2026-06-16")


# ── CRUD + daily selection ───────────────────────────────────────────────────
class TestCommitmentStore(unittest.TestCase):
    def _insert(self, conn, *, email, text, due="", created_days_ago=0, ref=None):
        cid = C.add_commitment(conn, message_id="m", contact_email=email, commitment_text=text, due_date=due)
        if created_days_ago:
            # Age relative to ``ref`` (the same reference the test passes to due_commitments)
            # so the fixture is deterministic. Without this, _insert aged from the REAL clock
            # while due_commitments evaluated a fixed ``now``, so the aging math drifted by the
            # gap between the two and the test broke once the real date passed the fixture.
            base = ref or datetime.now()
            ts = int((base - timedelta(days=created_days_ago)).timestamp())
            conn.execute("UPDATE commitments SET created_at=? WHERE id=?", (ts, cid))
        return cid

    def test_done_and_snooze(self):
        conn = db.open_db(":memory:")
        try:
            cid = self._insert(conn, email="a@x.com", text="ship it")
            self.assertTrue(C.mark_done(conn, cid))
            self.assertEqual(C.get_commitment(conn, cid)["status"], "done")
            cid2 = self._insert(conn, email="a@x.com", text="call them")
            self.assertTrue(C.snooze(conn, cid2, days=2, now=datetime(2026, 6, 15)))
            self.assertEqual(C.get_commitment(conn, cid2)["due_date"], "2026-06-17")
        finally:
            conn.close()

    def test_due_within_one_day_surfaces(self):
        conn = db.open_db(":memory:")
        try:
            now = datetime(2026, 6, 15, 8, 0)
            self._insert(conn, email="a@x.com", text="due tomorrow", due="2026-06-16")
            self._insert(conn, email="a@x.com", text="due next month", due="2026-07-20")
            due = C.due_commitments(conn, now=now)
            texts = {r["commitment_text"] for r in due}
            self.assertIn("due tomorrow", texts)
            self.assertNotIn("due next month", texts)
        finally:
            conn.close()

    def test_vip_threshold_is_tighter(self):
        conn = db.open_db(":memory:")
        try:
            now = datetime(2026, 6, 15, 8, 0)
            # Both aged 4 days, no due date. VIP (3d) surfaces; standard (5d) doesn't.
            self._insert(conn, email="vip@x.com", text="vip aging", created_days_ago=4, ref=now)
            self._insert(conn, email="normal@x.com", text="normal aging", created_days_ago=4, ref=now)
            due = C.due_commitments(conn, now=now, vip_emails={"vip@x.com"})
            texts = {r["commitment_text"] for r in due}
            self.assertIn("vip aging", texts)
            self.assertNotIn("normal aging", texts)
        finally:
            conn.close()


# ── calendar context ─────────────────────────────────────────────────────────
class TestCalendar(unittest.TestCase):
    def setUp(self):
        cal._clear_cache()

    def test_unavailable_when_disabled(self):
        ctx = cal.get_calendar_context(_settings(calendar_enabled=False))
        self.assertFalse(ctx.available)
        self.assertEqual(ctx.free_slots, [])
        self.assertEqual(cal.prompt_note(_settings(calendar_enabled=False)), "")
        self.assertEqual(cal.drafting_note(_settings(calendar_enabled=False)), "")

    def test_free_slots_in_window(self):
        ws, we = datetime(2026, 6, 15, 9), datetime(2026, 6, 15, 18)
        busy = [(datetime(2026, 6, 15, 10), datetime(2026, 6, 15, 11))]
        slots = cal.free_slots_in_window(busy, ws, we, min_minutes=30)
        self.assertEqual(slots[0], (ws, datetime(2026, 6, 15, 10)))
        self.assertEqual(slots[1], (datetime(2026, 6, 15, 11), we))

    def test_free_slots_skips_short_gaps(self):
        ws, we = datetime(2026, 6, 15, 9), datetime(2026, 6, 15, 18)
        busy = [(datetime(2026, 6, 15, 9, 15), we)]  # only a 15-min gap at the start
        slots = cal.free_slots_in_window(busy, ws, we, min_minutes=30)
        self.assertEqual(slots, [])

    def test_working_windows_skips_weekends(self):
        sat = datetime(2026, 6, 13, 9)  # Saturday
        self.assertEqual(cal.working_windows(sat, 2), [])  # Sat + Sun → none
        mon = datetime(2026, 6, 15, 9)  # Monday
        self.assertEqual(len(cal.working_windows(mon, 1)), 1)


if __name__ == "__main__":
    unittest.main()
