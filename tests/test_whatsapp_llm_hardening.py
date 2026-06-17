"""Regression tests for the whatsapp-llm cluster's ingest/send findings:

  ingest-whatsapp-2  Sender-controlled `ts` can no longer choose the burst representative
                     or reorder context — ordering is created_at-primary, ts clamped to a
                     plausible window around the receive clock and used only as tiebreaker.
  ingest-whatsapp-6  VIP 'instant' jids use a tiny settle window so a multi-poll burst
                     coalesces into one card instead of fragmenting per poll.
  failure-recovery-5 send_reply no longer asserts delivery on a bare HTTP-200 with no
                     receipt: it uses a real relay-supplied id when present, records a
                     wa_send_unconfirmed event otherwise, and (strict mode) raises so the
                     action goes SEND_AMBIGUOUS instead of terminal SENT.
  config-secrets-deploy-4  the /poll receiver requires INGEST_TOKEN (when set), rejects
                     cross-site requests, and debounces rapid forced polls.

Stdlib only; open_db(":memory:"), fake injected relay round-trips, plain-dict rows for the
pure planner. Mirrors tests/test_whatsapp_settle.py + tests/test_whatsapp_source.py.
"""

from __future__ import annotations

import unittest

from assistant.config import Settings
from assistant.ingest import whatsapp_source as W
from assistant.ingest.whatsapp_source import (
    WhatsAppSendUnconfirmed, WhatsAppSource, _clamped_ts, _order_key, plan_settling,
)
from assistant.storage import db
from assistant.storage import repositories as repo


def _row(mid, jid, ts, created_at, is_group=0):
    """A plain-dict 'row' the pure planner accepts (subscriptable by column name)."""
    return {"message_id": mid, "jid": jid, "ts": ts, "created_at": created_at,
            "is_group": is_group}


# ── ingest-whatsapp-2: ts clamp + created_at-primary ordering ──────────────────
class TestTimestampSkewClamp(unittest.TestCase):
    def test_clamp_replaces_future_ts_with_receive_clock(self):
        recv = 1_700_000_000
        # A year-2099 ts is implausible → clamped to the receive clock.
        self.assertEqual(_clamped_ts(4_000_000_000, recv), recv)
        # A far-past ts (before the 1-day window) is also clamped.
        self.assertEqual(_clamped_ts(1, recv), recv)
        # A plausible ts (a few seconds of drift) is kept.
        self.assertEqual(_clamped_ts(recv + 60, recv), recv + 60)

    def test_future_ts_does_not_become_representative(self):
        recv = 1_700_000_000
        # Line A arrives first (real latest by receive clock); line B carries a forged
        # year-2099 ts to try to become the representative.
        rows = [
            _row("a_latest", "jid1", ts=recv + 5, created_at=recv + 5),
            _row("b_spoof", "jid1", ts=4_000_000_000, created_at=recv + 2),
        ]
        plan = plan_settling(rows, now=recv + 1000, settle=10, max_hold=900,
                             group_settle=300, group_max_hold=3600)
        self.assertEqual(len(plan), 1)
        rep, members = plan[0]
        # The genuinely-latest RECEIVED line is the representative, not the spoofed-ts one.
        self.assertEqual(rep, "a_latest")
        self.assertIn("b_spoof", members)

    def test_order_key_is_created_at_primary(self):
        recv = 1_700_000_000
        r_old = _row("old", "j", ts=4_000_000_000, created_at=recv)        # spoofed-future ts
        r_new = _row("new", "j", ts=1, created_at=recv + 10)               # spoofed-past ts
        self.assertLess(_order_key(r_old), _order_key(r_new))  # ordered by receive clock


# ── ingest-whatsapp-6: VIP instant burst coalescing ───────────────────────────
class TestVipInstantCoalesce(unittest.TestCase):
    def test_instant_settle_holds_active_burst_then_releases_once_quiet(self):
        # A VIP burst still arriving (last line 2s ago) must NOT release while inside the
        # instant settle window — it would fragment. With instant_settle=10 and now-last=2,
        # nothing releases yet.
        now = 1_000_000
        rows = [
            _row("l1", "vip", ts=now - 8, created_at=now - 8),
            _row("l2", "vip", ts=now - 2, created_at=now - 2),
        ]
        plan = plan_settling(rows, now=now, settle=75, max_hold=900, group_settle=300,
                             group_max_hold=3600, instant_jids={"vip"}, instant_settle=10)
        self.assertEqual(plan, [])  # held — burst is still active within the window

    def test_instant_settle_releases_as_one_card_when_quiet(self):
        now = 1_000_000
        rows = [
            _row("l1", "vip", ts=now - 30, created_at=now - 30),
            _row("l2", "vip", ts=now - 20, created_at=now - 20),
        ]
        plan = plan_settling(rows, now=now, settle=75, max_hold=900, group_settle=300,
                             group_max_hold=3600, instant_jids={"vip"}, instant_settle=10)
        self.assertEqual(len(plan), 1)              # ONE card, not one per line
        rep, members = plan[0]
        self.assertEqual(rep, "l2")
        self.assertEqual(members, ["l1"])

    def test_instant_settle_zero_preserves_legacy_immediate_release(self):
        now = 1_000_000
        rows = [_row("l1", "vip", ts=now, created_at=now)]
        plan = plan_settling(rows, now=now, settle=75, max_hold=900, group_settle=300,
                             group_max_hold=3600, instant_jids={"vip"}, instant_settle=0)
        self.assertEqual(len(plan), 1)  # released immediately, old behavior intact

    def test_instant_max_hold_never_starves_a_steady_typer(self):
        # A VIP who keeps typing past max_hold is still released (cap honored).
        now = 1_000_000
        rows = [
            _row("l1", "vip", ts=now - 1000, created_at=now - 1000),  # first_seen old
            _row("l2", "vip", ts=now - 1, created_at=now - 1),        # still active
        ]
        plan = plan_settling(rows, now=now, settle=75, max_hold=300, group_settle=300,
                             group_max_hold=3600, instant_jids={"vip"}, instant_settle=10)
        self.assertEqual(len(plan), 1)  # capped at max_hold despite ongoing activity


# ── failure-recovery-5: send confirmation ─────────────────────────────────────
class _FakeSource(WhatsAppSource):
    """A WhatsAppSource whose relay round-trip is injected (no socket)."""

    def __init__(self, conn, settings, relay_response):
        super().__init__(conn, settings)
        self._relay_response = relay_response  # (status, parsed_body)

    def _relay_with_body(self, path, payload):  # type: ignore[override]
        return self._relay_response


def _settings(**kw):
    base = dict(ingest_token="", whatsapp_send_port=0)
    base.update(kw)
    return Settings(**base)


class TestSendConfirmation(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_real_delivery_id_yields_confirmed_sent(self):
        src = _FakeSource(self.conn, _settings(), (200, {"ok": True, "message_id": "ABC123"}))
        out = src.send_reply(thread_id="jid@s.whatsapp.net", to=[], cc=[], subject="",
                             body="hi", in_reply_to_gmail_id="wa_m1")
        self.assertEqual(out, "wa_ABC123")  # used the real relay id

    def test_nested_key_id_is_extracted(self):
        src = _FakeSource(self.conn, _settings(), (200, {"ok": True, "key": {"id": "K9"}}))
        out = src.send_reply(thread_id="jid", to=[], cc=[], subject="", body="hi",
                             in_reply_to_gmail_id="wa_m1")
        self.assertEqual(out, "wa_K9")

    def test_bare_ok_records_unconfirmed_event_lenient_default(self):
        # Default (non-strict): preserve terminal-SENT behavior but record the gap.
        src = _FakeSource(self.conn, _settings(), (200, {"ok": True}))
        out = src.send_reply(thread_id="vip@s.whatsapp.net", to=[], cc=[], subject="",
                             body="hi", in_reply_to_gmail_id="wa_m1")
        self.assertEqual(out, "wa_sent")
        n = repo.count_events(self.conn, type="wa_send_unconfirmed")
        self.assertEqual(n, 1)

    def test_strict_mode_raises_so_consumer_marks_ambiguous(self):
        import os
        os.environ["WHATSAPP_REQUIRE_DELIVERY_CONFIRMATION"] = "1"
        try:
            src = _FakeSource(self.conn, _settings(), (200, {"ok": True}))
            with self.assertRaises(WhatsAppSendUnconfirmed):
                src.send_reply(thread_id="vip", to=[], cc=[], subject="", body="hi",
                               in_reply_to_gmail_id="wa_m1")
            # Even when raising, the gap is recorded for observability.
            self.assertEqual(repo.count_events(self.conn, type="wa_send_unconfirmed"), 1)
        finally:
            del os.environ["WHATSAPP_REQUIRE_DELIVERY_CONFIRMATION"]

    def test_delivery_id_from_handles_non_dict(self):
        src = _FakeSource(self.conn, _settings(), (200, None))
        self.assertEqual(src._delivery_id_from(None), "")
        self.assertEqual(src._delivery_id_from({"ok": True}), "")
        self.assertEqual(src._delivery_id_from({"id": "x"}), "x")


# ── config-secrets-deploy-4: /poll auth + CSRF + debounce ─────────────────────
class _FakeHeaders:
    def __init__(self, d):
        self._d = d

    def get(self, k, default=None):
        return self._d.get(k, default)


class _FakeServer:
    pass


class _PollHandler(W._InboundHandler):
    """Drive do_POST without a socket: capture _reply, script path + headers."""

    def __init__(self, settings, path="/poll", headers=None):
        self.path = path
        self.headers = _FakeHeaders(headers or {})
        self.server = _FakeServer()
        self.server.cos_settings = settings  # type: ignore[attr-defined]
        self.replies = []

    def _reply(self, code, obj):  # override the wire write
        self.replies.append((code, obj))


class TestPollAuth(unittest.TestCase):
    def setUp(self):
        # Reset the class-level debounce clock so tests don't interfere.
        W._InboundHandler._last_poll_at = 0.0

    def test_poll_rejected_without_token_when_token_set(self):
        s = _settings(ingest_token="secret", mode="live")
        h = _PollHandler(s, headers={})  # no X-Cos-Token
        h.do_POST()
        self.assertEqual(h.replies[-1][0], 401)

    def test_poll_rejected_cross_site_even_with_token(self):
        s = _settings(ingest_token="secret", mode="live")
        h = _PollHandler(s, headers={W.AUTH_HEADER: "secret",
                                     "Sec-Fetch-Site": "cross-site"})
        h.do_POST()
        self.assertEqual(h.replies[-1][0], 403)

    def test_poll_debounced_on_rapid_second_call(self):
        s = _settings(ingest_token="secret", mode="live")
        hdrs = {W.AUTH_HEADER: "secret"}  # same-origin/non-browser caller
        # First call wakes (trigger_poll import may fail in test harness, but the auth +
        # debounce gates run first and the handler still replies 200).
        h1 = _PollHandler(s, headers=hdrs)
        h1.do_POST()
        self.assertEqual(h1.replies[-1][0], 200)
        # Immediate second call within the debounce window is coalesced.
        h2 = _PollHandler(s, headers=hdrs)
        h2.do_POST()
        self.assertEqual(h2.replies[-1][0], 200)
        self.assertTrue(h2.replies[-1][1].get("debounced"))

    def test_poll_allows_authenticated_non_cross_site(self):
        s = _settings(ingest_token="secret", mode="live")
        h = _PollHandler(s, headers={W.AUTH_HEADER: "secret", "Sec-Fetch-Site": "same-origin"})
        h.do_POST()
        # Not 401/403 — auth + CSRF passed (200 woke or 200 debounced).
        self.assertEqual(h.replies[-1][0], 200)


if __name__ == "__main__":
    unittest.main()
