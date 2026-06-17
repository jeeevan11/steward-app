"""WhatsApp settling / debounce gate.

People text line-by-line. The gate holds a conversation's burst until it goes quiet,
then collapses it into a single representative (earlier lines folded in) so the brain
sees the whole burst and the owner gets ONE card, not a ping per line.

Covers: the pure planner (active vs quiet vs max-hold cap, group windows, rep/member
selection), the end-to-end fetch (folding + exactly-once), full-burst reassembly in
get_thread, the disable switch, and that an active conversation is held."""

from __future__ import annotations

import time
import unittest

from assistant.config import Settings
from assistant.ingest import whatsapp_source as wa
from assistant.storage import db
from assistant.storage import whatsapp_inbox as inbox


def _row(mid, jid, ts, created_at, is_group=0):
    return {"message_id": mid, "jid": jid, "ts": ts, "created_at": created_at,
            "is_group": is_group}


WINDOWS = dict(settle=75, max_hold=900, group_settle=300, group_max_hold=10800)


class TestPlanSettling(unittest.TestCase):
    def test_active_conversation_is_held(self):
        now = 1000.0
        rows = [_row("wa_1", "a@s.whatsapp.net", 980, 980)]  # 20s ago < 75s window
        self.assertEqual(wa.plan_settling(rows, now, **WINDOWS), [])

    def test_quiet_conversation_is_released_latest_is_rep(self):
        now = 1000.0
        rows = [
            _row("wa_1", "a@s.whatsapp.net", 1, 900),
            _row("wa_2", "a@s.whatsapp.net", 2, 905),
            _row("wa_3", "a@s.whatsapp.net", 3, 910),  # 90s of silence ≥ 75
        ]
        plan = wa.plan_settling(rows, now, **WINDOWS)
        self.assertEqual(len(plan), 1)
        rep, members = plan[0]
        self.assertEqual(rep, "wa_3")               # latest message represents the burst
        self.assertEqual(sorted(members), ["wa_1", "wa_2"])

    def test_max_hold_cap_releases_a_still_trickling_chat(self):
        now = 1000.0
        # last message only 5s ago (window NOT met) but burst started 950s ago (> 900 cap)
        rows = [
            _row("wa_1", "a@s.whatsapp.net", 1, 50),
            _row("wa_2", "a@s.whatsapp.net", 2, 995),
        ]
        plan = wa.plan_settling(rows, now, **WINDOWS)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][0], "wa_2")

    def test_group_uses_longer_window(self):
        now = 1000.0
        # 100s of silence: settled for a 1:1 (75s) but NOT for a group (300s)
        rows = [_row("g1", "x@g.us", 1, 900, is_group=1)]
        self.assertEqual(wa.plan_settling(rows, now, **WINDOWS), [])
        # same timing as a 1:1 → released
        rows2 = [_row("p1", "p@s.whatsapp.net", 1, 900, is_group=0)]
        self.assertEqual(len(wa.plan_settling(rows2, now, **WINDOWS)), 1)

    def test_one_settled_one_active_only_settled_released(self):
        now = 1000.0
        rows = [
            _row("wa_a", "a@s.whatsapp.net", 1, 900),   # quiet 100s → settled
            _row("wa_b", "b@s.whatsapp.net", 1, 990),   # quiet 10s → active
        ]
        plan = wa.plan_settling(rows, now, **WINDOWS)
        self.assertEqual([rep for rep, _ in plan], ["wa_a"])


class TestFetchAndReassemble(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.settings = Settings()  # defaults: settling on, 75s / 15min / 5min / 3h

    def tearDown(self):
        self.conn.close()

    def _insert(self, mid, jid, ts, age_seconds, body="hi", is_group=0):
        inbox.put(self.conn, mid, {
            "messageId": mid, "jid": jid, "sender_jid": jid, "body": body,
            "is_group": bool(is_group), "ts": ts,
        })
        ca = int(time.time()) - age_seconds
        self.conn.execute(
            "UPDATE whatsapp_inbox SET created_at=?, ts=? WHERE message_id=?",
            (ca, ts, mid))

    def test_settled_burst_folds_and_returns_one_representative(self):
        jid = "a@s.whatsapp.net"
        self._insert("wa_1", jid, 1, 200, body="hey")
        self._insert("wa_2", jid, 2, 200, body="you around?")
        self._insert("wa_3", jid, 3, 200, body="call me")
        src = wa.WhatsAppSource(self.conn, self.settings)

        ids = src.fetch_new_message_ids()
        self.assertEqual(ids, ["wa_3"])                      # one card, not three
        self.assertEqual(inbox.get(self.conn, "wa_3")["status"], "queued")
        for m in ("wa_1", "wa_2"):
            row = inbox.get(self.conn, m)
            self.assertEqual(row["status"], "folded")
            self.assertEqual(row["folded_into"], "wa_3")

        # the representative's thread carries the WHOLE burst, in order
        thread = src.get_thread("wa_3")
        self.assertEqual([m.body_text for m in thread.messages],
                         ["hey", "you around?", "call me"])

        # exactly-once: nothing left to fetch on the next pass
        self.assertEqual(src.fetch_new_message_ids(), [])

    def test_active_conversation_is_not_fetched(self):
        jid = "a@s.whatsapp.net"
        self._insert("wa_1", jid, 1, 5)   # 5s old → still active
        self._insert("wa_2", jid, 2, 5)
        src = wa.WhatsAppSource(self.conn, self.settings)
        self.assertEqual(src.fetch_new_message_ids(), [])
        # still 'new', held for a later pass
        self.assertEqual(inbox.get(self.conn, "wa_1")["status"], "new")

    def test_disabled_returns_everything_immediately(self):
        settings = Settings(whatsapp_settle_enabled=False)
        jid = "a@s.whatsapp.net"
        self._insert("wa_1", jid, 1, 1)
        self._insert("wa_2", jid, 2, 1)
        src = wa.WhatsAppSource(self.conn, settings)
        ids = src.fetch_new_message_ids()
        self.assertEqual(sorted(ids), ["wa_1", "wa_2"])      # legacy per-message path

    def test_single_message_thread_unchanged(self):
        self._insert("wa_solo", "z@s.whatsapp.net", 1, 200, body="just one")
        src = wa.WhatsAppSource(self.conn, self.settings)
        self.assertEqual(src.fetch_new_message_ids(), ["wa_solo"])
        thread = src.get_thread("wa_solo")
        self.assertEqual(len(thread.messages), 1)
        self.assertEqual(thread.messages[0].body_text, "just one")


if __name__ == "__main__":
    unittest.main()
