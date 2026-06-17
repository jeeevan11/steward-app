"""Regression: control-state-presence-1 — a paused agent (OFF) must go fully quiet.

Pause only gated inbound processing in main.poll_and_process; the proactive digest, the
relationship-reminder card sweep, and the scheduled brief still fired. When the owner turns
the agent off it must emit no proactive / brief output and create no new reminder cards.

In-memory SQLite only; a fake notifier captures any output that leaks out.
"""

from __future__ import annotations

import json
import time
import unittest
from datetime import datetime

from assistant.config import Settings
from assistant.control import briefs
from assistant.control import proactive
from assistant.storage import db
from assistant.storage import decision_log
from assistant.storage import repositories as repo


def _settings(**kw) -> Settings:
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com",
                telegram_chat_id="1")
    base.update(kw)
    return Settings(**base)


class FakeNotifier:
    def __init__(self):
        self.texts: list[str] = []

    def send_text(self, text: str) -> str:
        self.texts.append(text)
        return "msg-id"


def _seed_proactive_item(conn):
    """One VIP with a surfaced-but-unanswered decision → the sweep would normally fire."""
    repo.upsert_contact(conn, repo.get_or_default_contact(conn, "vip@x.com"))
    conn.execute("UPDATE contacts SET importance=? WHERE email=?", (90, "vip@x.com"))
    decision_log.ensure(conn)
    conn.execute(
        "INSERT INTO decision_log (message_id, sender_email, sender_name, subject, "
        " category, final_tier, base_tier, ts) VALUES (?,?,?,?,?,?,?,?)",
        ("m1", "vip@x.com", "Vippy", "sign off", "personal", 3, 0, int(time.time())),
    )


class TestPausedProactive(unittest.TestCase):
    def test_paused_emits_no_proactive_digest(self):
        conn = db.open_db(":memory:")
        try:
            _seed_proactive_item(conn)
            repo.set_paused(conn, True)
            notifier = FakeNotifier()
            now = datetime.now().replace(hour=10)  # past proactive_hour
            out = proactive.run_sweep(conn, _settings(), notifier, now=now)
            self.assertEqual(out, "")
            self.assertEqual(notifier.texts, [])
            # No daily stamp written while paused → a normal sweep can still run on resume.
            self.assertIsNone(repo.kv_get(conn, proactive._STAMP_KEY))
        finally:
            conn.close()

    def test_unpaused_still_emits_proactive_digest(self):
        # Control: with the agent ON, the same seed DOES surface (proves the gate, not a
        # blanket no-op, is what silences it).
        conn = db.open_db(":memory:")
        try:
            _seed_proactive_item(conn)
            notifier = FakeNotifier()
            now = datetime.now().replace(hour=10)
            out = proactive.run_sweep(conn, _settings(), notifier, now=now)
            self.assertTrue(out)
            self.assertEqual(len(notifier.texts), 1)
        finally:
            conn.close()

    def test_paused_creates_no_reminder_cards(self):
        conn = db.open_db(":memory:")
        try:
            # An open situation awaiting the owner that has gone quiet would normally create
            # a reminder pending card via the sweep.
            pid = "p1"
            repo.person_add(conn, person_id=pid, display_name="Bob", emails=[f"{pid}@x.com"])
            repo.set_person_relationship_type(conn, pid, "collaborator")
            old_ts = int(time.time()) - 48 * 3600
            sit = [{"key": "k1", "situation": "needs your reply", "awaiting": "owner",
                    "status": "open", "last_activity_ts": old_ts, "thread_id": "wa_123"}]
            repo.relationship_memory_upsert(
                conn, pid, summary_json="{}", open_situations_json=json.dumps(sit),
                decided_json="[]", episodes_json="[]", superseded_json="[]",
                last_distilled_at=old_ts, version=1,
            )
            repo.set_paused(conn, True)
            notifier = FakeNotifier()
            now = datetime.now().replace(hour=10)
            proactive.run_sweep(conn, _settings(), notifier, now=now)
            cards = conn.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE kind='reminder'"
            ).fetchone()[0]
            self.assertEqual(cards, 0)
        finally:
            conn.close()


class TestPausedBriefs(unittest.TestCase):
    class _NoLLM:
        """A brief should never reach the LLM while paused; calling it is a test failure."""
        def complete_text(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("paused brief must not call the LLM")

    def test_paused_brief_is_empty(self):
        conn = db.open_db(":memory:")
        try:
            # Seed something that would make a non-empty brief if not paused.
            repo.create_pending(conn, idempotency_key="k1", message_id="m1", thread_id="t1",
                                tier=3, kind="ask", summary="needs you")
            repo.set_paused(conn, True)
            out = briefs.generate_brief(conn, _settings(), self._NoLLM(), "morning")
            # EMPTY_BRIEF makes main.maybe_send_briefs skip the scheduled send.
            self.assertEqual(out, briefs.EMPTY_BRIEF)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
