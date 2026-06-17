"""P0 — speed/UX: notification format, response-time metrics, and draft
pre-computation (the draft is generated BEFORE the notification, and persists)."""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from assistant.brain.tiers import TierConfig, decide
from assistant.config import Settings
from assistant.control import notifier as N
from assistant.storage import db, metrics
from assistant.storage import repositories as repo
from assistant.models import Decision, Message, Reversibility, Stakes, Thread, Tier
from assistant.memory.contacts import resolve_sender


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeLLM:
    def draft(self, *, system_prefix: str, user_prompt: str, **kw) -> str:
        return "Hi Alex,\n\nThanks for the note. Yes, that works.\n\nBest"


class FakeNotifier:
    def __init__(self):
        self.approvals = []
        self.asks = []

    def send_approval(self, action_id, signal, draft, *, sender="", mail="", quote="", **kwargs):
        self.approvals.append({"id": action_id, "signal": signal, "draft": draft,
                               "sender": sender, "mail": mail, "quote": quote})
        return "tg-appr"

    def send_ask(self, action_id, signal, draft, *, sender="", mail="", quote="", **kwargs):
        self.asks.append({"id": action_id, "signal": signal, "draft": draft,
                          "sender": sender, "mail": mail, "quote": quote})
        return "tg-ask"

    def fyi(self, text):
        return "tg-fyi"


def _settings(**kw) -> Settings:
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com",
                telegram_chat_id="123")
    base.update(kw)
    return Settings(**base)


def _thread(ts_ago: float = 5.0) -> Thread:
    inbound = Message(
        id="m1", thread_id="t1", sender_email="a@x.com", sender_name="Alex",
        recipients=["me@x.com"], subject="Quick question", body_text="Does Tuesday work?",
        timestamp=time.time() - ts_ago,
    )
    return Thread(id="t1", subject="Quick question", messages=[inbound])


def _final(tier: int):
    dec = Decision(
        category="personal", intent="asks to meet", sender_importance=10,
        stakes=Stakes.LOW, reversibility=Reversibility.REVERSIBLE, proposed_tier=tier,
        confidence=0.95, needs_reply=True, suggested_action="reply",
        one_line_summary="Alex asks if Tuesday works",
    )
    thread = _thread()
    from assistant.models import Contact
    return thread, Contact(email="a@x.com", name="Alex"), decide(thread, dec, Contact(email="a@x.com", name="Alex"), TierConfig())


# ── notification format (P0c) ────────────────────────────────────────────────
class TestNotificationFormat(unittest.TestCase):
    def test_line1_is_signal_not_sender(self):
        body = N.format_card(tier=3, signal="term sheet discussion",
                             sender="John Park (a venture firm)", draft="ok")
        lines = body.splitlines()
        self.assertTrue(lines[0].startswith("🔴"))
        self.assertIn("term sheet", lines[0])
        self.assertNotIn("John", lines[0])           # sender NOT on line 1
        self.assertIn("John Park", lines[1])         # sender on line 2

    def test_non_draft_part_under_300_chars(self):
        body = N.format_card(tier=2, signal="x" * 500, sender="y" * 500, draft="z")
        lines = body.splitlines()
        head = lines[0] + lines[1]
        self.assertLess(len(head), 300)

    def test_draft_preview_caps_lines_and_marks_truncation(self):
        prev = N.draft_preview("l1\nl2\nl3\nl4\nl5")
        self.assertLessEqual(len(prev.splitlines()), 3)
        self.assertTrue(prev.endswith("…"))

    def test_tier_emoji_anchors(self):
        self.assertEqual(N.tier_emoji(3), "🔴")
        self.assertEqual(N.tier_emoji(2), "🟡")
        self.assertEqual(N.tier_emoji(1), "🔵")

    def test_empty_draft_shows_unavailable_note(self):
        body = N.format_card(tier=2, signal="hi", sender="A", draft="")
        self.assertIn("Draft unavailable", body)


# ── response-time metrics (P0e) ──────────────────────────────────────────────
class TestResponseMetrics(unittest.TestCase):
    def test_record_and_percentiles(self):
        conn = db.open_db(":memory:")
        try:
            for ms in (100, 200, 300, 400):
                metrics.record_response_time(conn, metrics.RT_DRAFT_GENERATION, ms)
            p = metrics.response_percentiles(conn, metrics.RT_DRAFT_GENERATION)
            self.assertEqual(p["count"], 4)
            self.assertGreaterEqual(p["p95"], p["p50"])
            self.assertEqual(metrics.response_percentiles(conn, "nope")["count"], 0)
        finally:
            conn.close()


# ── draft pre-computation (P0b) ──────────────────────────────────────────────
class TestPreComputation(unittest.TestCase):
    def test_approve_draft_generated_before_notification(self):
        from assistant.action import dispatcher
        conn = db.open_db(":memory:")
        try:
            settings = _settings()
            thread, contact, final = _final(int(Tier.APPROVE))
            note = FakeNotifier()
            dispatcher.dispatch(conn, settings, None, FakeLLM(), note, thread, contact, final, "m1")
            self.assertEqual(len(note.approvals), 1)
            self.assertTrue(note.approvals[0]["draft"].strip())          # draft attached
            self.assertIn("Alex", note.approvals[0]["sender"])           # sender line built
            # latency sample recorded (inbound timestamp → notification)
            self.assertGreaterEqual(
                metrics.response_percentiles(conn, metrics.RT_EMAIL_TO_NOTIFICATION)["count"], 1
            )
        finally:
            conn.close()

    def test_ask_pre_generates_suggested_reply(self):
        from assistant.action import dispatcher
        conn = db.open_db(":memory:")
        try:
            settings = _settings()
            thread, contact, final = _final(int(Tier.ASK))
            note = FakeNotifier()
            dispatcher.dispatch(conn, settings, None, FakeLLM(), note, thread, contact, final, "m1")
            self.assertEqual(len(note.asks), 1)
            self.assertTrue(note.asks[0]["draft"].strip())               # suggested reply ready
        finally:
            conn.close()

    def test_pre_generated_draft_survives_restart(self):
        from assistant.action import dispatcher
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            settings = _settings()
            thread, contact, final = _final(int(Tier.APPROVE))
            conn = db.open_db(path)
            dispatcher.dispatch(conn, settings, None, FakeLLM(), FakeNotifier(), thread, contact, final, "m1")
            conn.close()
            # Reopen a fresh connection (simulates a restart): the draft is still there.
            conn2 = db.open_db(path)
            row = conn2.execute(
                "SELECT draft_text FROM pending_actions WHERE message_id='m1'"
            ).fetchone()
            conn2.close()
            self.assertIsNotNone(row)
            self.assertTrue((row["draft_text"] or "").strip())
        finally:
            for p in (path, path + "-wal", path + "-shm"):
                try:
                    os.remove(p)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
