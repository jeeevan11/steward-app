"""Phase 9 — proactive chief-of-staff sweep.

Exercises each pure selection function against synthetic decision_log / pending_actions
/ learning_events / commitments rows, then the digest composer and the once-a-day
deduped orchestration. In-memory SQLite only; no network, no live DB."""

from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta

from assistant.config import Settings
from assistant.control import proactive
from assistant.memory import commitments as C
from assistant.storage import db
from assistant.storage import decision_log
from assistant.storage import repositories as repo


def _settings(**kw) -> Settings:
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com",
                telegram_chat_id="1")
    base.update(kw)
    return Settings(**base)


class _SettingsView:
    """Wraps a real Settings and overlays proactive_* attrs the frozen dataclass does
    not (yet) declare. Mirrors how run_sweep reads them via getattr with defaults."""

    def __init__(self, base: Settings, **overrides):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self.__dict__.get("_overrides", {}):
            return self._overrides[name]
        return getattr(self._base, name)


class FakeNotifier:
    """Captures every send so tests can assert what (if anything) went out."""

    def __init__(self):
        self.texts: list[str] = []

    def send_text(self, text: str) -> str:
        self.texts.append(text)
        return "msg-id"


def _log_decision(conn, *, message_id, email, name="", subject="", category="personal",
                  final_tier=2, ts=None):
    """Insert a decision_log row directly (bypasses the LLM path)."""
    decision_log.ensure(conn)
    conn.execute(
        "INSERT INTO decision_log (message_id, sender_email, sender_name, subject, "
        " category, final_tier, base_tier, ts) VALUES (?,?,?,?,?,?,?,?)",
        (message_id, email.lower(), name, subject, category, final_tier, 0,
         int(ts if ts is not None else time.time())),
    )


def _mk_vip(conn, email, importance=90):
    repo.upsert_contact(conn, repo.get_or_default_contact(conn, email))
    conn.execute("UPDATE contacts SET importance=? WHERE email=?", (importance, email.lower()))


# ── unanswered_important ──────────────────────────────────────────────────────
class TestUnansweredImportant(unittest.TestCase):
    def test_detects_unanswered_vip(self):
        conn = db.open_db(":memory:")
        try:
            _mk_vip(conn, "vip@x.com")
            _log_decision(conn, message_id="m1", email="vip@x.com", name="Vippy",
                          subject="Need your sign-off", category="work_request", final_tier=3)
            items = proactive.unanswered_important(conn, _settings())
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["kind"], "unanswered_important")
            self.assertEqual(items[0]["contact"], "vip@x.com")
            self.assertIn("Vippy", items[0]["summary"])
        finally:
            conn.close()

    def test_detects_unanswered_personal(self):
        conn = db.open_db(":memory:")
        try:
            # No VIP flag, but category=personal still counts as important.
            _log_decision(conn, message_id="m1", email="friend@x.com", subject="hi",
                          category="personal", final_tier=2)
            items = proactive.unanswered_important(conn, _settings())
            self.assertEqual(len(items), 1)
        finally:
            conn.close()

    def test_resolved_via_sent_pending_is_excluded(self):
        conn = db.open_db(":memory:")
        try:
            _mk_vip(conn, "vip@x.com")
            _log_decision(conn, message_id="m1", email="vip@x.com", final_tier=2)
            # A SENT pending action means it was handled → not surfaced.
            repo.create_pending(conn, idempotency_key="k1", message_id="m1", thread_id="t1",
                                tier=2, kind="reply_draft")
            conn.execute("UPDATE pending_actions SET status='SENT' WHERE message_id='m1'")
            self.assertEqual(proactive.unanswered_important(conn, _settings()), [])
        finally:
            conn.close()

    def test_open_pending_is_still_unanswered(self):
        conn = db.open_db(":memory:")
        try:
            _mk_vip(conn, "vip@x.com")
            _log_decision(conn, message_id="m1", email="vip@x.com", final_tier=2)
            repo.create_pending(conn, idempotency_key="k1", message_id="m1", thread_id="t1",
                                tier=2, kind="reply_draft")  # status PENDING
            items = proactive.unanswered_important(conn, _settings())
            self.assertEqual(len(items), 1)
        finally:
            conn.close()

    def test_resolved_via_approve_event_is_excluded(self):
        conn = db.open_db(":memory:")
        try:
            _mk_vip(conn, "vip@x.com")
            _log_decision(conn, message_id="m1", email="vip@x.com", final_tier=2)
            repo.record_event(conn, type="approve", message_id="m1", contact_email="vip@x.com")
            self.assertEqual(proactive.unanswered_important(conn, _settings()), [])
        finally:
            conn.close()

    def test_low_importance_non_personal_is_ignored(self):
        conn = db.open_db(":memory:")
        try:
            _log_decision(conn, message_id="m1", email="rando@x.com", category="newsletter",
                          final_tier=2)
            self.assertEqual(proactive.unanswered_important(conn, _settings()), [])
        finally:
            conn.close()

    def test_old_items_outside_window_ignored(self):
        conn = db.open_db(":memory:")
        try:
            _mk_vip(conn, "vip@x.com")
            old = time.time() - 30 * 86400
            _log_decision(conn, message_id="m1", email="vip@x.com", final_tier=2, ts=old)
            self.assertEqual(proactive.unanswered_important(conn, _settings()), [])
        finally:
            conn.close()


# ── at_risk_commitments (reuses commitments.due_commitments) ──────────────────
class TestAtRiskCommitments(unittest.TestCase):
    def test_detects_overdue_commitment(self):
        conn = db.open_db(":memory:")
        try:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            C.add_commitment(conn, message_id="m", contact_email="a@x.com",
                             commitment_text="send the deck", due_date=yesterday)
            items = proactive.at_risk_commitments(conn, _settings())
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["kind"], "at_risk_commitment")
            self.assertIn("send the deck", items[0]["summary"])
        finally:
            conn.close()

    def test_far_future_commitment_not_surfaced(self):
        conn = db.open_db(":memory:")
        try:
            future = (datetime.now() + timedelta(days=40)).strftime("%Y-%m-%d")
            C.add_commitment(conn, message_id="m", contact_email="a@x.com",
                             commitment_text="annual review", due_date=future)
            self.assertEqual(proactive.at_risk_commitments(conn, _settings()), [])
        finally:
            conn.close()


# ── stalled_conversations (reuses commitments.stale_threads) ──────────────────
class TestStalledConversations(unittest.TestCase):
    def test_detects_stalled_thread(self):
        conn = db.open_db(":memory:")
        try:
            # A tier-2 decision + a send audit row 10 days ago, no newer activity.
            sent_ts = int((datetime.now() - timedelta(days=10)).timestamp())
            _log_decision(conn, message_id="m1", email="quiet@x.com", subject="Proposal",
                          final_tier=2, ts=sent_ts)
            conn.execute(
                "UPDATE decision_log SET ts=? WHERE message_id='m1'", (sent_ts,)
            )
            conn.execute(
                "INSERT INTO audit_log (kind, message_id, ts) VALUES ('send','m1',?)",
                (sent_ts,),
            )
            items = proactive.stalled_conversations(conn, _settings())
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["kind"], "stalled_conversation")
            self.assertEqual(items[0]["contact"], "quiet@x.com")
        finally:
            conn.close()


# ── recurring_requests ────────────────────────────────────────────────────────
class TestRecurringRequests(unittest.TestCase):
    def test_detects_recurring_same_contact_category(self):
        conn = db.open_db(":memory:")
        try:
            for i in range(3):
                _log_decision(conn, message_id=f"m{i}", email="boss@x.com", name="Boss",
                              category="scheduling", final_tier=1)
            items = proactive.recurring_requests(conn)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["kind"], "recurring_request")
            self.assertEqual(items[0]["contact"], "boss@x.com")
            self.assertIn("3 times", items[0]["detail"])
        finally:
            conn.close()

    def test_below_threshold_not_flagged(self):
        conn = db.open_db(":memory:")
        try:
            for i in range(2):
                _log_decision(conn, message_id=f"m{i}", email="boss@x.com",
                              category="scheduling")
            self.assertEqual(proactive.recurring_requests(conn), [])
        finally:
            conn.close()


# ── build_digest ──────────────────────────────────────────────────────────────
class TestBuildDigest(unittest.TestCase):
    def test_empty_in_empty_out(self):
        self.assertEqual(proactive.build_digest([]), "")

    def test_items_with_no_summary_yield_empty(self):
        self.assertEqual(proactive.build_digest([{"kind": "x", "summary": "  "}]), "")

    def test_produces_grouped_text(self):
        items = [
            {"kind": "unanswered_important", "summary": "Vippy is still waiting on you",
             "contact": "vip@x.com", "detail": "Need sign-off"},
            {"kind": "at_risk_commitment", "summary": "You promised: send the deck",
             "contact": "a@x.com", "detail": "due 2026-06-14"},
        ]
        text = proactive.build_digest(items)
        self.assertTrue(text)
        self.assertIn("Still waiting on you", text)
        self.assertIn("Promises coming due", text)
        self.assertIn("send the deck", text)

    def test_length_capped(self):
        items = [
            {"kind": "recurring_request", "summary": "x" * 500, "detail": "y" * 500}
            for _ in range(20)
        ]
        text = proactive.build_digest(items)
        self.assertLessEqual(len(text), proactive._MAX_DIGEST_CHARS)


# ── run_sweep orchestration ───────────────────────────────────────────────────
class TestRunSweep(unittest.TestCase):
    def _seed_one_item(self, conn):
        _mk_vip(conn, "vip@x.com")
        _log_decision(conn, message_id="m1", email="vip@x.com", name="Vippy",
                      subject="sign off", final_tier=3)

    def test_sends_once_then_deduped(self):
        conn = db.open_db(":memory:")
        try:
            self._seed_one_item(conn)
            notifier = FakeNotifier()
            now = datetime.now().replace(hour=10)
            first = proactive.run_sweep(conn, _settings(), notifier, now=now)
            self.assertTrue(first)
            self.assertEqual(len(notifier.texts), 1)
            # Second call same day: deduped → nothing sent.
            second = proactive.run_sweep(conn, _settings(), notifier, now=now)
            self.assertEqual(second, "")
            self.assertEqual(len(notifier.texts), 1)
        finally:
            conn.close()

    def test_disabled_flag_is_noop(self):
        conn = db.open_db(":memory:")
        try:
            self._seed_one_item(conn)
            notifier = FakeNotifier()
            now = datetime.now().replace(hour=10)
            settings = _SettingsView(_settings(), proactive_enabled=False)
            out = proactive.run_sweep(conn, settings, notifier, now=now)
            self.assertEqual(out, "")
            self.assertEqual(notifier.texts, [])
            # And no stamp was written, so it could still run later if re-enabled.
            self.assertIsNone(repo.kv_get(conn, proactive._STAMP_KEY))
        finally:
            conn.close()

    def test_before_hour_is_noop(self):
        conn = db.open_db(":memory:")
        try:
            self._seed_one_item(conn)
            notifier = FakeNotifier()
            now = datetime.now().replace(hour=6)  # before default proactive_hour=9
            out = proactive.run_sweep(conn, _settings(), notifier, now=now)
            self.assertEqual(out, "")
            self.assertEqual(notifier.texts, [])
        finally:
            conn.close()

    def test_nothing_to_surface_sends_nothing_but_stamps(self):
        conn = db.open_db(":memory:")
        try:
            notifier = FakeNotifier()
            now = datetime.now().replace(hour=10)
            out = proactive.run_sweep(conn, _settings(), notifier, now=now)
            self.assertEqual(out, "")
            self.assertEqual(notifier.texts, [])
            # Stamped so we don't re-scan repeatedly the same day.
            self.assertEqual(repo.kv_get(conn, proactive._STAMP_KEY),
                             now.strftime("%Y-%m-%d"))
        finally:
            conn.close()

    def test_never_raises_on_bad_conn(self):
        # A broken notifier must not bubble out of the sweep.
        conn = db.open_db(":memory:")
        try:
            self._seed_one_item(conn)

            class BoomNotifier:
                def send_text(self, text):
                    raise RuntimeError("telegram down")

            now = datetime.now().replace(hour=10)
            # Should swallow the notifier error and return "".
            out = proactive.run_sweep(conn, _settings(), BoomNotifier(), now=now)
            self.assertEqual(out, "")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
