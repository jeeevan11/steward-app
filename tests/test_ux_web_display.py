"""ux-web-display cluster — regression tests for the MEDIUM/LOW findings owned here.

Covers (Python-side fixes only; the Swift edits are parse-verified separately):

  * ingest-email-2  — Reply-To preferred over From in the reply recipients.
  * ingest-email-7  — inbound Cc is capped + sanitized; no unbounded fan-out.
  * control-state-presence-2 — the relationship-reminder sweep creates ZERO cards while paused.
  * control-state-presence-4 — the proactive sweep gates/dedups on settings.timezone, not the
                               host system tz.
  * ux-trust-4 — reminder cards carry real provenance (sender + quote + WhatsApp channel) so a
                 tier-3 nudge is verifiable, not an empty "someone · Other · Email" card.
  * ux-trust-5 — a blank/whitespace-only draft is refused at the send path AND in service.edit;
                 it is never transmitted.
  * ux-trust-6 — Clear all is scoped (keeps tier-3 by default) and recoverable (undo restores
                 the skipped decisions to PENDING).
  * web-security-5 — handler errors return a generic body; the raw exception text never leaks.

In-memory SQLite only; injected fakes; stdlib + the project's own deps. No network, no live DB.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime
from unittest import mock

from assistant.action import gmail_actions
from assistant.config import Settings
from assistant.control import proactive
from assistant.models import Channel, Message, Thread
from assistant.storage import db, decision_log
from assistant.storage import repositories as repo
from assistant.web import service


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────
def _mkdb() -> sqlite3.Connection:
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _settings(**kw) -> Settings:
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com",
                telegram_chat_id="1")
    base.update(kw)
    return Settings(**base)


class _SettingsView:
    """Overlay proactive_*/timezone attrs the frozen Settings dataclass may not declare,
    mirroring how run_sweep reads them via getattr. (Same pattern as test_proactive.)"""

    def __init__(self, base: Settings, **overrides):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self.__dict__.get("_overrides", {}):
            return self._overrides[name]
        return getattr(self._base, name)


class FakeNotifier:
    def __init__(self):
        self.texts: list[str] = []
        self.errors: list[str] = []

    def send_text(self, text: str) -> str:
        self.texts.append(text)
        return "msg-id"

    def error(self, text: str) -> None:
        self.errors.append(text)


class FakeMail:
    """A MailSource that returns a fixed thread and records every send_reply call."""

    def __init__(self, thread: Thread):
        self.thread = thread
        self.sent: list[dict] = []

    def get_thread(self, message_id: str) -> Thread:
        return self.thread

    def send_reply(self, *, thread_id, to, cc, subject, body, in_reply_to_gmail_id):
        self.sent.append(dict(thread_id=thread_id, to=to, cc=cc, subject=subject,
                              body=body, in_reply_to_gmail_id=in_reply_to_gmail_id))
        return "sent-1"


def _approved_reply(conn, *, key="k1", message_id="m1", thread_id="t",
                    draft="hello there", kind="reply_draft") -> int:
    aid = repo.create_pending(conn, idempotency_key=key, message_id=message_id,
                              thread_id=thread_id, tier=2, kind=kind, summary="s",
                              draft_text=draft)
    repo.mark_approved(conn, aid)  # PENDING -> APPROVED (sendable)
    return aid


# ─────────────────────────────────────────────────────────────────────────────
# ingest-email-2 — Reply-To preferred over From
# ─────────────────────────────────────────────────────────────────────────────
class ReplyToRoutingTest(unittest.TestCase):
    def _thread_with(self, *, sender, reply_to=None, cc=None):
        m = Message(id="m1", thread_id="t", channel=Channel.GMAIL,
                    sender_email=sender, sender_name="Acme Support",
                    subject="Ticket", cc=cc or [])
        if reply_to is not None:
            # Message has no declared reply_to field; ingestion attaches it. A dataclass
            # instance accepts the attribute, which is exactly how the integrator wires it.
            m.reply_to = reply_to
        return Thread(id="t", subject="Ticket", messages=[m])

    def test_reply_to_wins_over_from(self):
        s = _settings()
        th = self._thread_with(sender="noreply@acme.com", reply_to="tickets@acme.com")
        to, cc = gmail_actions._reply_recipients(th, s)
        self.assertEqual(to, ["tickets@acme.com"])  # NOT the no-reply From address

    def test_reply_to_with_display_name_is_parsed(self):
        s = _settings()
        th = self._thread_with(sender="noreply@acme.com",
                               reply_to="Acme Support <tickets@acme.com>")
        to, _ = gmail_actions._reply_recipients(th, s)
        self.assertEqual(to, ["tickets@acme.com"])

    def test_falls_back_to_from_when_no_reply_to(self):
        s = _settings()
        th = self._thread_with(sender="alice@acme.com", reply_to=None)
        to, _ = gmail_actions._reply_recipients(th, s)
        self.assertEqual(to, ["alice@acme.com"])

    def test_reply_to_equal_to_owner_is_ignored(self):
        s = _settings()  # gmail_address = me@x.com
        th = self._thread_with(sender="alice@acme.com", reply_to="me@x.com")
        to, _ = gmail_actions._reply_recipients(th, s)
        self.assertEqual(to, ["alice@acme.com"])  # never reply to ourselves

    def test_send_path_routes_to_reply_to_and_records_event(self):
        conn = _mkdb()
        th = self._thread_with(sender="noreply@acme.com", reply_to="tickets@acme.com")
        mail = FakeMail(th)
        aid = _approved_reply(conn)
        ok = gmail_actions.execute_send(conn, mail, _settings(mode="live"), aid,
                                        notifier=FakeNotifier())
        self.assertTrue(ok)
        self.assertEqual(mail.sent[0]["to"], ["tickets@acme.com"])
        # Observability: the reply-routed-to-replyto event is recorded (not silent).
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM learning_events WHERE type='reply_routed_to_replyto'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)


# ─────────────────────────────────────────────────────────────────────────────
# ingest-email-7 — Cc cap + sanitize
# ─────────────────────────────────────────────────────────────────────────────
class ReplyCcCapTest(unittest.TestCase):
    def test_cc_is_capped(self):
        s = _settings()
        many = [f"a{i}@x.com" for i in range(300)]
        m = Message(id="m1", thread_id="t", channel=Channel.GMAIL,
                    sender_email="alice@acme.com", cc=many)
        th = Thread(id="t", messages=[m])
        _, cc = gmail_actions._reply_recipients(th, s)
        self.assertLessEqual(len(cc), gmail_actions._MAX_REPLY_CC)
        self.assertEqual(len(cc), gmail_actions._MAX_REPLY_CC)

    def test_malformed_cc_tokens_are_dropped(self):
        s = _settings()
        m = Message(id="m1", thread_id="t", channel=Channel.GMAIL,
                    sender_email="alice@acme.com",
                    cc=["good@x.com", "not-an-address", "evil\nBcc: x@y.com", "", "  "])
        th = Thread(id="t", messages=[m])
        _, cc = gmail_actions._reply_recipients(th, s)
        self.assertEqual(cc, ["good@x.com"])

    def test_owner_and_dupes_filtered(self):
        s = _settings()  # me@x.com
        m = Message(id="m1", thread_id="t", channel=Channel.GMAIL,
                    sender_email="alice@acme.com",
                    cc=["me@x.com", "bob@x.com", "Bob <bob@x.com>"])
        th = Thread(id="t", messages=[m])
        _, cc = gmail_actions._reply_recipients(th, s)
        self.assertEqual(cc, ["bob@x.com"])


# ─────────────────────────────────────────────────────────────────────────────
# ux-trust-5 — blank/whitespace-only draft is never sent
# ─────────────────────────────────────────────────────────────────────────────
class BlankSendGuardTest(unittest.TestCase):
    def _thread(self):
        m = Message(id="m1", thread_id="t", sender_email="alice@acme.com")
        return Thread(id="t", messages=[m])

    def test_whitespace_only_draft_is_refused(self):
        conn = _mkdb()
        mail = FakeMail(self._thread())
        notifier = FakeNotifier()
        aid = _approved_reply(conn, draft="   \n\t  ")
        ok = gmail_actions.execute_send(conn, mail, _settings(mode="live"), aid,
                                        notifier=notifier)
        self.assertFalse(ok)
        self.assertEqual(mail.sent, [])  # nothing transmitted
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SEND_BLOCKED")
        self.assertTrue(notifier.errors)  # owner was told (fail loud, never silent)
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM learning_events WHERE type='send_blocked_blank'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_nonblank_draft_still_sends(self):
        conn = _mkdb()
        mail = FakeMail(self._thread())
        aid = _approved_reply(conn, draft="real reply")
        ok = gmail_actions.execute_send(conn, mail, _settings(mode="live"), aid,
                                        notifier=FakeNotifier())
        self.assertTrue(ok)
        self.assertEqual(mail.sent[0]["body"], "real reply")

    def test_service_edit_rejects_whitespace(self):
        conn = _mkdb()
        aid = _approved_reply(conn, draft="original")
        out = service.edit(conn, aid, "   \n  ")
        self.assertFalse(out["ok"])
        # The original draft is untouched (the empty edit did not persist).
        self.assertEqual(repo.get_pending(conn, aid)["draft_text"], "original")

    def test_service_edit_accepts_real_text(self):
        conn = _mkdb()
        aid = _approved_reply(conn, draft="original")
        out = service.edit(conn, aid, "a new reply")
        self.assertTrue(out["ok"])
        self.assertEqual(repo.get_pending(conn, aid)["draft_text"], "a new reply")


# ─────────────────────────────────────────────────────────────────────────────
# control-state-presence-2 — no reminder cards while paused
# ─────────────────────────────────────────────────────────────────────────────
class PausedReminderSweepTest(unittest.TestCase):
    def _seed_owner_awaiting_situation(self, conn):
        repo.person_add(conn, person_id="p1", display_name="Sam", relationship="partner")
        repo.set_person_relationship_type(conn, "p1", "partner")
        old = repo.now_epoch() - 9 * 3600  # quiet for 9h (past the 4h partner threshold)
        situations = (
            '[{"key":"venue","awaiting":"owner","situation":"confirm the venue booking",'
            f'"last_activity_ts":{old},"thread_id":"1234567890@s.whatsapp.net"}}]'
        )
        repo.relationship_memory_upsert(
            conn, person_id="p1", summary_json="{}", open_situations_json=situations,
            decided_json="[]", episodes_json="[]", superseded_json="[]",
            last_distilled_at=None, version=1,
        )

    def test_paused_creates_zero_reminder_cards(self):
        conn = _mkdb()
        self._seed_owner_awaiting_situation(conn)
        repo.set_paused(conn, True)
        created = proactive._relationship_reminder_sweep(conn, _settings())
        self.assertEqual(created, 0)
        self.assertEqual(len(repo.open_pending(conn)), 0)

    def test_resumed_creates_the_reminder(self):
        conn = _mkdb()
        self._seed_owner_awaiting_situation(conn)
        repo.set_paused(conn, False)
        created = proactive._relationship_reminder_sweep(conn, _settings())
        self.assertEqual(created, 1)


# ─────────────────────────────────────────────────────────────────────────────
# control-state-presence-4 — sweep uses settings.timezone, not host tz
# ─────────────────────────────────────────────────────────────────────────────
class ProactiveTimezoneTest(unittest.TestCase):
    def test_today_uses_configured_timezone(self):
        # Pick an instant where the configured-tz date differs from UTC: 23:30 UTC in
        # Tokyo (UTC+9) is already the NEXT calendar day. _today must return the Tokyo date.
        s = _SettingsView(_settings(), timezone="Asia/Tokyo")
        fixed_utc = datetime(2026, 6, 16, 23, 30, tzinfo=__import__("datetime").timezone.utc)

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz is not None else fixed_utc.replace(tzinfo=None)

        with mock.patch.object(proactive, "datetime", _FixedDateTime):
            self.assertEqual(proactive._today(s), "2026-06-17")  # Tokyo already next day

    def test_run_sweep_hour_gate_uses_configured_tz(self):
        # On a host whose wall clock is 02:00 (system tz), but TIMEZONE puts the owner at a
        # local hour >= proactive_hour, the digest should be eligible to fire by the owner's
        # clock — proving the gate reads settings.timezone, not naive now().
        conn = _mkdb()
        s = _SettingsView(_settings(), timezone="Asia/Tokyo", proactive_hour=9,
                          proactive_enabled=True)
        # 01:00 UTC == 10:00 Tokyo → past the 9am gate in the configured tz.
        fixed_utc = datetime(2026, 6, 16, 1, 0, tzinfo=__import__("datetime").timezone.utc)

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz is not None else fixed_utc.replace(tzinfo=None)

        notifier = FakeNotifier()
        with mock.patch.object(proactive, "datetime", _FixedDateTime):
            proactive.run_sweep(conn, s, notifier)
        # The once-a-day stamp was written using the Tokyo date (10:00 JST on 2026-06-16).
        self.assertEqual(repo.kv_get(conn, proactive._STAMP_KEY), "2026-06-16")


# ─────────────────────────────────────────────────────────────────────────────
# ux-trust-4 — reminder provenance (sender + quote + WhatsApp channel)
# ─────────────────────────────────────────────────────────────────────────────
class ReminderProvenanceTest(unittest.TestCase):
    def test_channel_for_identifier(self):
        self.assertEqual(repo.channel_for_identifier("1234567890@s.whatsapp.net"), "WhatsApp")
        self.assertEqual(repo.channel_for_identifier("12345@g.us"), "WhatsApp")
        self.assertEqual(repo.channel_for_identifier("wa_abc123"), "WhatsApp")
        self.assertEqual(repo.channel_for_identifier("19a8b@lid"), "WhatsApp")
        self.assertEqual(repo.channel_for_identifier("CABC123gmailid"), "Email")
        self.assertEqual(repo.channel_for_identifier(""), "Email")

    def test_sweep_stamps_meta_for_reminder(self):
        conn = _mkdb()
        repo.person_add(conn, person_id="p1", display_name="Sam", relationship="partner")
        repo.set_person_relationship_type(conn, "p1", "partner")
        old = repo.now_epoch() - 9 * 3600
        jid = "1234567890@s.whatsapp.net"
        situations = (
            '[{"key":"venue","awaiting":"me","situation":"confirm the venue booking",'
            f'"last_activity_ts":{old},"thread_id":"{jid}"}}]'
        )
        repo.relationship_memory_upsert(
            conn, person_id="p1", summary_json="{}", open_situations_json=situations,
            decided_json="[]", episodes_json="[]", superseded_json="[]",
            last_distilled_at=None, version=1,
        )
        created = proactive._relationship_reminder_sweep(conn, _settings())
        self.assertEqual(created, 1)
        meta = repo.get_reminder_meta(conn, "reminder:p1:venue")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["sender_name"], "Sam")
        self.assertEqual(meta["channel"], "WhatsApp")
        self.assertIn("venue", meta["quote"])


# ─────────────────────────────────────────────────────────────────────────────
# ux-trust-6 — scoped + recoverable bulk skip
# ─────────────────────────────────────────────────────────────────────────────
class BulkSkipUndoTest(unittest.TestCase):
    def _pending(self, conn, *, key, tier):
        return repo.create_pending(conn, idempotency_key=key, message_id=f"m_{key}",
                                   thread_id=f"t_{key}", tier=tier, kind="reply_draft",
                                   summary="s", draft_text="d")

    def test_restore_brings_skipped_back_to_pending(self):
        conn = _mkdb()
        a = self._pending(conn, key="a", tier=2)
        b = self._pending(conn, key="b", tier=2)
        repo.mark_skipped(conn, a)
        repo.mark_skipped(conn, b)
        repo.record_bulk_skip(conn, "batch1", a, "PENDING")
        repo.record_bulk_skip(conn, "batch1", b, "PENDING")
        restored = repo.restore_bulk_skip(conn, "batch1")
        self.assertEqual(restored, 2)
        self.assertEqual(repo.get_pending(conn, a)["status"], "PENDING")
        self.assertEqual(repo.get_pending(conn, b)["status"], "PENDING")

    def test_restore_skips_rows_already_rehandled(self):
        conn = _mkdb()
        a = self._pending(conn, key="a", tier=2)
        repo.mark_skipped(conn, a)
        repo.record_bulk_skip(conn, "batch1", a, "PENDING")
        # Owner re-handles it (e.g. it got SENT) before undoing — must NOT be revived.
        conn.execute("UPDATE pending_actions SET status='SENT' WHERE id=?", (a,))
        restored = repo.restore_bulk_skip(conn, "batch1")
        self.assertEqual(restored, 0)
        self.assertEqual(repo.get_pending(conn, a)["status"], "SENT")

    def test_restore_respects_grace_window(self):
        conn = _mkdb()
        a = self._pending(conn, key="a", tier=2)
        repo.mark_skipped(conn, a)
        repo.record_bulk_skip(conn, "batch1", a, "PENDING")
        # Age the journal row beyond the window.
        conn.execute("UPDATE bulk_skip_undo SET created_at=created_at-100000 WHERE batch_id='batch1'")
        restored = repo.restore_bulk_skip(conn, "batch1", within_seconds=600)
        self.assertEqual(restored, 0)
        self.assertEqual(repo.get_pending(conn, a)["status"], "SKIPPED")


# ─────────────────────────────────────────────────────────────────────────────
# ux-trust-6 + ux-trust-4 + web-security-5 — exercised through the FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
def _client():
    """A TestClient bound to a fresh in-memory DB via the get_conn override. Skips the
    whole module cleanly if FastAPI's test deps are unavailable."""
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:  # noqa: BLE001
        raise unittest.SkipTest(f"fastapi TestClient unavailable: {exc}")
    from assistant.web import api

    conn = _mkdb()

    def _override_conn():
        try:
            yield conn
        finally:
            pass

    api.app.dependency_overrides[api.get_conn] = _override_conn
    client = TestClient(api.app)
    return api, client, conn


class DecisionsClearEndpointTest(unittest.TestCase):
    def setUp(self):
        self.api, self.client, self.conn = _client()

    def tearDown(self):
        self.api.app.dependency_overrides.clear()
        self.conn.close()

    def _pending(self, *, key, tier):
        return repo.create_pending(self.conn, idempotency_key=key, message_id=f"m_{key}",
                                   thread_id=f"t_{key}", tier=tier, kind="reply_draft",
                                   summary="s", draft_text="d")

    def test_clear_keeps_urgent_by_default(self):
        self._pending(key="a", tier=2)
        self._pending(key="b", tier=3)  # needs you soon — must be kept
        r = self.client.post("/api/decisions/clear", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["cleared"], 1)
        self.assertEqual(body["kept_urgent"], 1)
        # The tier-3 item is still open; the tier-2 item is skipped.
        statuses = {row["idempotency_key"]: row["status"]
                    for row in self.conn.execute("SELECT idempotency_key, status FROM pending_actions")}
        self.assertEqual(statuses["a"], "SKIPPED")
        self.assertEqual(statuses["b"], "PENDING")

    def test_clear_include_urgent_and_undo(self):
        self._pending(key="a", tier=2)
        self._pending(key="b", tier=3)
        r = self.client.post("/api/decisions/clear", json={"include_urgent": True})
        body = r.json()
        self.assertEqual(body["cleared"], 2)
        self.assertEqual(body["kept_urgent"], 0)
        batch = body["batch_id"]
        # Undo restores both decisions to PENDING.
        r2 = self.client.post("/api/decisions/clear/undo", json={"batch_id": batch})
        self.assertEqual(r2.json()["restored"], 2)
        statuses = {row["idempotency_key"]: row["status"]
                    for row in self.conn.execute("SELECT idempotency_key, status FROM pending_actions")}
        self.assertEqual(statuses["a"], "PENDING")
        self.assertEqual(statuses["b"], "PENDING")

    def test_decisions_reminder_card_has_channel_and_quote(self):
        # A reminder row keyed by a WhatsApp JID, with stamped provenance, renders verifiably.
        jid = "1234567890@s.whatsapp.net"
        repo.create_pending(self.conn, idempotency_key="reminder:p1:venue", message_id=jid,
                            thread_id=jid, tier=3, kind="reminder",
                            summary="Still waiting on your response: confirm the venue", draft_text="")
        repo.set_reminder_meta(self.conn, "reminder:p1:venue", sender_name="Sam",
                               quote="confirm the venue booking", channel="WhatsApp", thread_id=jid)
        r = self.client.get("/api/decisions")
        items = r.json()["items"]
        card = next(i for i in items if i["kind"] == "reminder")
        self.assertEqual(card["channel"], "WhatsApp")   # not the wrong "Email"
        self.assertEqual(card["sender"], "Sam")          # not empty / "someone"
        self.assertIn("venue", card["quote"])            # a real quoted line


class GenericErrorLeakTest(unittest.TestCase):
    def setUp(self):
        self.api, self.client, self.conn = _client()

    def tearDown(self):
        self.api.app.dependency_overrides.clear()
        self.conn.close()

    def test_compose_500_does_not_leak_exception_text(self):
        # Force the compose handler to raise with a secret-looking message; the client must
        # receive only the generic body, never the raw exception text.
        secret = "OperationalError: no such table: /Users/owner/data/assistant.db"

        class _BoomCompose:
            @staticmethod
            def compose_and_queue(*a, **k):
                raise RuntimeError(secret)

        with mock.patch.object(self.api, "_compose", _BoomCompose):
            r = self.client.post("/compose", json={"intent": "hi", "channel": "auto"})
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json()["detail"], self.api._GENERIC_ERROR_DETAIL)
        self.assertNotIn("assistant.db", r.text)
        self.assertNotIn("OperationalError", r.text)

    def test_state_snapshot_error_is_generic(self):
        class _BoomState:
            @staticmethod
            def get_state_snapshot(conn):
                raise RuntimeError("secret SQL fragment: SELECT * FROM kv at ./data/assistant.db")

        with mock.patch.object(self.api, "_state_engine", _BoomState):
            r = self.client.get("/state/snapshot")
        body = r.json()
        self.assertEqual(body.get("error"), self.api._GENERIC_ERROR_DETAIL)
        self.assertNotIn("assistant.db", r.text)


if __name__ == "__main__":
    unittest.main()
