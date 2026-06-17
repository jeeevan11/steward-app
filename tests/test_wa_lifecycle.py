"""WhatsApp message-lifecycle tracking: delivery/read receipts → monotonic state on the
wa_messages row, the "not going / not coming back" silence sweep, and the prompt context tags.

Invariant focus: every new path is pure telemetry + read-only SQL + an informational FYI.
Nothing here may create a card or send a message (NO_AUTO_SEND), and a reply Steward sent on
the owner's behalf (is_agent) must never pollute presence or the style corpus.
"""

from __future__ import annotations

import unittest

from assistant import main as engine
from assistant.config import Settings
from assistant.ingest import wa_context
from assistant.ingest.whatsapp_source import ingest_receipt
from assistant.storage import db, wa_messages


def _settings(**kw) -> Settings:
    base = dict(mode="live", db_path=":memory:", whatsapp_silence_sweep_enabled=True,
                read_receipt_quiet_hours_enabled=False, wa_stuck_secs=120,
                wa_unanswered_out_secs=21600, wa_unanswered_in_secs=10800,
                wa_silence_sweep_interval_secs=1800)
    base.update(kw)
    return Settings(**base)


def _rec(conn, mid, jid, *, from_me, ts, body="hi", is_group=False, is_agent=False):
    wa_messages.record(conn, {"message_id": mid, "jid": jid, "body": body,
                              "is_group": is_group, "ts": ts}, from_me=from_me, is_agent=is_agent)


def _row(conn, mid):
    return conn.execute("SELECT * FROM wa_messages WHERE message_id=?", (mid,)).fetchone()


class _Notifier:
    def __init__(self):
        self.texts: list[str] = []
        self.cards: list = []   # any approval/ask/commitment = a violation here

    def send_text(self, text): self.texts.append(text); return "tg"
    def fyi(self, text): self.texts.append(text); return "tg"
    def send_approval(self, *a, **k): self.cards.append(("approval", a)); return "tg"
    def send_ask(self, *a, **k): self.cards.append(("ask", a)); return "tg"
    def send_commitment(self, *a, **k): self.cards.append(("commitment", a)); return "tg"
    def error(self, text): return "tg"


class TestApplyReceipt(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        wa_messages.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_monotonic_forward_only_and_write_once(self):
        _rec(self.conn, "wa_out_A", "x@s.whatsapp.net", from_me=True, ts=100)
        self.assertTrue(wa_messages.apply_receipt(self.conn, raw_id="A", status=2, ts=101))
        self.assertEqual(_row(self.conn, "wa_out_A")["status"], 2)
        # advance to READ → status 4, delivered_at + read_at stamped
        self.assertTrue(wa_messages.apply_receipt(self.conn, raw_id="A", status=4, ts=104))
        row = _row(self.conn, "wa_out_A")
        self.assertEqual(row["status"], 4)
        self.assertEqual(row["delivered_at"], 104)
        self.assertEqual(row["read_at"], 104)
        # a STALE lower ack arriving late is a no-op (never regresses)
        self.assertFalse(wa_messages.apply_receipt(self.conn, raw_id="A", status=3, ts=200))
        row2 = _row(self.conn, "wa_out_A")
        self.assertEqual(row2["status"], 4)
        self.assertEqual(row2["read_at"], 104)   # write-once: unchanged

    def test_no_row_is_silent_noop_never_stub_inserts(self):
        before = self.conn.execute("SELECT COUNT(*) AS n FROM wa_messages").fetchone()["n"]
        self.assertFalse(wa_messages.apply_receipt(self.conn, raw_id="GHOST", status=4))
        after = self.conn.execute("SELECT COUNT(*) AS n FROM wa_messages").fetchone()["n"]
        self.assertEqual(before, after)   # no stub row (would corrupt last_outbound_ts)

    def test_inbound_row_is_never_touched(self):
        _rec(self.conn, "wa_B", "x@s.whatsapp.net", from_me=False, ts=100)
        self.assertFalse(wa_messages.apply_receipt(self.conn, raw_id="B", status=4))
        self.assertEqual(_row(self.conn, "wa_B")["status"], 0)   # from_me=0 → untouched

    def test_group_receipt_is_per_recipient_and_caps_row_status(self):
        _rec(self.conn, "wa_out_G", "123@g.us", from_me=True, ts=100, is_group=True)
        wa_messages.apply_receipt(self.conn, raw_id="G", status=4, ts=105,
                                  remote_jid="123@g.us", participant="m1@s.whatsapp.net")
        rcpt = self.conn.execute(
            "SELECT * FROM wa_receipts WHERE message_id='wa_out_G' AND user_jid='m1@s.whatsapp.net'"
        ).fetchone()
        self.assertEqual(rcpt["status"], 4)            # per-recipient READ recorded
        row = _row(self.conn, "wa_out_G")
        self.assertEqual(row["status"], 3)             # row capped at DELIVERY_ACK
        self.assertIsNone(row["read_at"])              # one reader never marks the group read

    def test_ingest_receipt_parses_payload(self):
        _rec(self.conn, "wa_out_C", "x@s.whatsapp.net", from_me=True, ts=100)
        self.assertTrue(ingest_receipt(self.conn, {"id": "C", "status": 3, "ts": 101,
                                                   "remoteJid": "x@s.whatsapp.net", "fromMe": True}))
        self.assertEqual(_row(self.conn, "wa_out_C")["status"], 3)
        # malformed → no-op, no raise
        self.assertFalse(ingest_receipt(self.conn, {"status": "nope"}))


class TestIsAgentSeparation(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        wa_messages.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_agent_send_excluded_from_presence_and_style(self):
        jid = "x@s.whatsapp.net"
        _rec(self.conn, "wa_AG", jid, from_me=True, ts=500, is_agent=True, body="drafted reply")
        # presence: Steward replying is NOT the owner being present
        self.assertEqual(wa_messages.last_outbound_ts(self.conn, jid), 0)
        # style corpus: never Steward's words
        self.assertEqual(wa_messages.owner_outbound(self.conn), [])
        # the owner's OWN later message does count
        _rec(self.conn, "wa_out_OWN", jid, from_me=True, ts=600, body="my own words")
        self.assertEqual(wa_messages.last_outbound_ts(self.conn, jid), 600)
        self.assertEqual(len(wa_messages.owner_outbound(self.conn)), 1)

    def test_delayed_echo_of_agent_send_is_not_double_recorded(self):
        # A reply Steward sent (wa_<id>, is_agent=1) whose fromMe echo slips past the relay TTL
        # arrives at /outbound. It must NOT be re-recorded as wa_out_<id> (is_agent=0) — that
        # second copy would pollute presence (last_outbound_ts) + the style corpus.
        from assistant.ingest.whatsapp_source import ingest_outbound
        jid = "x@s.whatsapp.net"
        wa_messages.record(self.conn, {"message_id": "wa_ABC", "jid": jid, "body": "drafted"},
                           from_me=True, is_agent=True)
        ingest_outbound(self.conn, {"messageId": "ABC", "jid": jid, "body": "drafted"}, settings=None)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM wa_messages WHERE message_id='wa_out_ABC'").fetchone())
        self.assertEqual(wa_messages.last_outbound_ts(self.conn, jid), 0)   # presence clean
        self.assertEqual(wa_messages.owner_outbound(self.conn), [])         # style clean

    def test_contact_name_uses_inbound_never_owner(self):
        jid = "y@s.whatsapp.net"
        _rec(self.conn, "wa_in1", jid, from_me=False, ts=1000, body="hi")
        self.conn.execute("UPDATE wa_messages SET push_name='Arjun' WHERE message_id='wa_in1'")
        _rec(self.conn, "wa_out_o1", jid, from_me=True, ts=1001, body="reply")
        self.conn.execute("UPDATE wa_messages SET push_name='Jatin' WHERE message_id='wa_out_o1'")
        self.assertEqual(wa_messages.contact_name(self.conn, jid), "Arjun")   # never "Jatin"


class TestSweepQueries(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        wa_messages.ensure(self.conn)
        self.now = 1_000_000

    def tearDown(self):
        self.conn.close()

    def test_stuck_outbound(self):
        _rec(self.conn, "wa_out_S", "a@s.whatsapp.net", from_me=True, ts=self.now - 600)
        # status==1 (PENDING — reached our device, never got SERVER_ACK) and old → stuck
        wa_messages.apply_receipt(self.conn, raw_id="S", status=1, ts=self.now - 600)
        got = wa_messages.stuck_outbound(self.conn, stuck_secs=120, now=self.now)
        self.assertEqual([r["message_id"] for r in got], ["wa_out_S"])
        # once it reaches SERVER_ACK it is no longer "not going"
        wa_messages.apply_receipt(self.conn, raw_id="S", status=2, ts=self.now)
        self.assertEqual(wa_messages.stuck_outbound(self.conn, stuck_secs=120, now=self.now), [])

    def test_status_zero_is_unknown_not_stuck(self):
        # a pre-feature row (status 0 = no receipt data) must NEVER be flagged "not going".
        _rec(self.conn, "wa_out_OLD", "a@s.whatsapp.net", from_me=True, ts=self.now - 600)
        self.assertEqual(wa_messages.stuck_outbound(self.conn, stuck_secs=120, now=self.now), [])

    def test_newsletter_and_broadcast_jids_are_not_chats(self):
        # a channel/newsletter broadcast is not something you "reply to" — never surface it.
        _rec(self.conn, "wa_NL", "12345@newsletter", from_me=False, ts=self.now - 20000)
        _rec(self.conn, "wa_BC", "999@broadcast", from_me=False, ts=self.now - 20000)
        self.assertEqual(wa_messages.unanswered_inbound(self.conn, min_age_secs=10800, now=self.now), [])

    def test_recency_floor_skips_ancient_silences(self):
        _rec(self.conn, "wa_IN_OLD", "old@s.whatsapp.net", from_me=False, ts=self.now - 10 * 86400)
        # older than the 4-day default floor → not surfaced (no first-run burst on stale chats)
        self.assertEqual(wa_messages.unanswered_inbound(self.conn, min_age_secs=10800, now=self.now), [])

    def test_unanswered_outbound_only_when_owner_is_latest_and_delivered(self):
        jid = "b@s.whatsapp.net"
        _rec(self.conn, "wa_out_O", jid, from_me=True, ts=self.now - 30000)
        wa_messages.apply_receipt(self.conn, raw_id="O", status=4, ts=self.now - 30000)  # read
        got = wa_messages.unanswered_outbound(self.conn, min_age_secs=21600, now=self.now)
        self.assertEqual([r["jid"] for r in got], [jid])
        # they reply → no longer the latest → not surfaced
        _rec(self.conn, "wa_R", jid, from_me=False, ts=self.now - 10)
        self.assertEqual(wa_messages.unanswered_outbound(self.conn, min_age_secs=21600, now=self.now), [])

    def test_unanswered_outbound_excludes_agent_sends(self):
        jid = "c@s.whatsapp.net"
        _rec(self.conn, "wa_AGT", jid, from_me=True, ts=self.now - 30000, is_agent=True)
        wa_messages.apply_receipt(self.conn, raw_id="AGT", status=4, ts=self.now - 30000)
        self.assertEqual(wa_messages.unanswered_outbound(self.conn, min_age_secs=21600, now=self.now), [])

    def test_stuck_agent_send_safety_net(self):
        jid = "z@s.whatsapp.net"
        _rec(self.conn, "wa_AGS", jid, from_me=True, ts=self.now - 600, is_agent=True)
        # status 0 (no SERVER_ACK receipt) and old → an approved reply that may not have delivered
        got = wa_messages.stuck_agent_send(self.conn, stuck_secs=120, now=self.now)
        self.assertEqual([r["message_id"] for r in got], ["wa_AGS"])
        # once it reaches SERVER_ACK it is confirmed, no longer flagged
        wa_messages.apply_receipt(self.conn, raw_id="AGS", status=2, ts=self.now)
        self.assertEqual(wa_messages.stuck_agent_send(self.conn, stuck_secs=120, now=self.now), [])

    def test_unanswered_inbound_only_when_they_are_latest(self):
        jid = "d@s.whatsapp.net"
        _rec(self.conn, "wa_IN", jid, from_me=False, ts=self.now - 20000)
        got = wa_messages.unanswered_inbound(self.conn, min_age_secs=10800, now=self.now)
        self.assertEqual([r["jid"] for r in got], [jid])
        # owner replies (even via Steward) → chat answered → not surfaced
        _rec(self.conn, "wa_REPLY", jid, from_me=True, ts=self.now - 5, is_agent=True)
        self.assertEqual(wa_messages.unanswered_inbound(self.conn, min_age_secs=10800, now=self.now), [])


class TestSilenceSweep(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        wa_messages.ensure(self.conn)
        self.settings = _settings()
        self.notifier = _Notifier()
        self.now = int(__import__("time").time())

    def tearDown(self):
        self.conn.close()

    def _reset_throttle(self):
        from assistant.storage import repositories as repo
        repo.kv_set(self.conn, "last_wa_silence_sweep", "0")

    def test_surfaces_fyi_never_a_card(self):
        _rec(self.conn, "wa_out_Z", "z@s.whatsapp.net", from_me=True, ts=self.now - 600)
        wa_messages.apply_receipt(self.conn, raw_id="Z", status=1, ts=self.now - 600)  # stuck PENDING
        self._reset_throttle()
        engine.maybe_surface_wa_silence(self.conn, self.settings, self.notifier)
        self.assertTrue(any("hasn't been delivered" in t for t in self.notifier.texts))
        self.assertEqual(self.notifier.cards, [])   # NO approval/ask/commitment card

    def test_dedup_does_not_repeat_a_standing_silence(self):
        _rec(self.conn, "wa_out_Z", "z@s.whatsapp.net", from_me=True, ts=self.now - 600)
        self._reset_throttle()
        engine.maybe_surface_wa_silence(self.conn, self.settings, self.notifier)
        n1 = len(self.notifier.texts)
        self._reset_throttle()   # defeat the time throttle, but dedup should still hold
        engine.maybe_surface_wa_silence(self.conn, self.settings, self.notifier)
        self.assertEqual(len(self.notifier.texts), n1)   # same situation → not re-announced

    def test_paused_agent_emits_nothing(self):
        from assistant.storage import repositories as repo
        repo.set_paused(self.conn, True)
        _rec(self.conn, "wa_out_Z", "z@s.whatsapp.net", from_me=True, ts=self.now - 600)
        self._reset_throttle()
        engine.maybe_surface_wa_silence(self.conn, self.settings, self.notifier)
        self.assertEqual(self.notifier.texts, [])

    def test_muted_jid_is_skipped(self):
        s = _settings(mute_jids=["z@s.whatsapp.net"])
        _rec(self.conn, "wa_out_Z", "z@s.whatsapp.net", from_me=True, ts=self.now - 600)
        self._reset_throttle()
        engine.maybe_surface_wa_silence(self.conn, s, self.notifier)
        self.assertEqual(self.notifier.texts, [])

    def test_one_fyi_per_chat_per_pass(self):
        # A chat that matches TWO buckets (an old stuck owner send + a newer inbound that is now
        # the latest) must produce ONE FYI, not two.
        jid = "w@s.whatsapp.net"
        _rec(self.conn, "wa_out_OLD", jid, from_me=True, ts=self.now - 18000)
        wa_messages.apply_receipt(self.conn, raw_id="OLD", status=1, ts=self.now - 18000)  # stuck PENDING
        _rec(self.conn, "wa_IN_NEW", jid, from_me=False, ts=self.now - 14400, body="you there?")
        self._reset_throttle()
        engine.maybe_surface_wa_silence(self.conn, self.settings, self.notifier)
        self.assertEqual(len(self.notifier.texts), 1)


class TestContextTags(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        wa_messages.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_status_tags_and_agent_label(self):
        jid = "p@s.whatsapp.net"
        now = int(__import__("time").time())
        _rec(self.conn, "wa_IN", jid, from_me=False, ts=now - 50, body="you there?")
        _rec(self.conn, "wa_out_M", jid, from_me=True, ts=now - 40, body="yes hi")
        wa_messages.apply_receipt(self.conn, raw_id="M", status=4, ts=now - 39)  # read
        _rec(self.conn, "wa_AGT", jid, from_me=True, ts=now - 30, body="Steward note", is_agent=True)
        block = wa_context.recent_block(self.conn, jid, days=14)
        self.assertIn("[read]", block)                        # owner line tagged read
        self.assertIn("Steward (on your behalf):", block)     # agent reply labelled distinctly
        self.assertNotIn("Steward (on your behalf): yes hi", block)


if __name__ == "__main__":
    unittest.main()
