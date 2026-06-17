"""Universal context (the owner's rule: the agent always knows everything) + Layer 1C.

Covers: every inbound message is recorded to the history EVEN when it's a group message
we won't surface; the owner's own outbound is captured; last-outbound tracking; and the
rolling recent-conversation block."""

from __future__ import annotations

import time
import unittest

from assistant.config import Settings
from assistant.ingest import wa_context
from assistant.ingest import whatsapp_source as wa
from assistant.storage import db
from assistant.storage import wa_messages


def _settings():
    return Settings(db_path=":memory:", whatsapp_enabled=True, wa_user_jid="me@s.whatsapp.net")


def _dm(mid, body, jid="friend@s.whatsapp.net", **kw):
    base = dict(messageId=mid, jid=jid, sender_jid=jid, push_name="Friend", body=body,
                media_type="", is_group=False, group_name="", quoted_body="", mentions=[],
                timestamp=int(time.time()))
    base.update(kw)
    return base


class TestUniversalContext(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.s = _settings()

    def tearDown(self):
        self.conn.close()

    def test_inbound_is_recorded_to_history(self):
        wa.ingest_payload(self.conn, self.s, _dm("a1", "hi there"))
        rows = wa_messages.recent(self.conn, "friend@s.whatsapp.net",
                                  since_ts=int(time.time()) - 100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "hi there")
        self.assertEqual(rows[0]["from_me"], 0)

    def test_skipped_group_message_is_still_in_context(self):
        # A group message with no @mention/keyword is NOT surfaced (returns None)...
        gid = "grp@g.us"
        mid = wa.ingest_payload(self.conn, self.s,
                                _dm("g1", "random group chatter", jid=gid, is_group=True,
                                    group_name="Family"))
        self.assertIsNone(mid)  # not queued for processing
        # ...but the agent STILL knows it happened (context is decoupled from notifying).
        rows = wa_messages.recent(self.conn, gid, since_ts=int(time.time()) - 100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "random group chatter")

    def test_outbound_capture_and_last_outbound(self):
        jid = "friend@s.whatsapp.net"
        self.assertEqual(wa_messages.last_outbound_ts(self.conn, jid), 0)
        wa.ingest_outbound(self.conn, _dm("o1", "yeah I'll be there", jid=jid))
        rows = wa_messages.recent(self.conn, jid, since_ts=int(time.time()) - 100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["from_me"], 1)
        self.assertGreater(wa_messages.last_outbound_ts(self.conn, jid), 0)

    def test_recent_block_renders_conversation(self):
        jid = "friend@s.whatsapp.net"
        wa.ingest_payload(self.conn, self.s, _dm("a1", "you around?", jid=jid))
        wa.ingest_outbound(self.conn, _dm("o1", "yeah whats up", jid=jid))
        wa.ingest_payload(self.conn, self.s, _dm("a2", "call me", jid=jid))
        block = wa_context.recent_block(self.conn, jid, days=14, me_jid=self.s.wa_user_jid)
        self.assertIn("RECENT CONVERSATION", block)
        self.assertIn("you around?", block)
        self.assertIn("Me: yeah whats up", block)

    def test_recent_block_empty_for_single_message(self):
        jid = "friend@s.whatsapp.net"
        wa.ingest_payload(self.conn, self.s, _dm("a1", "first ever message", jid=jid))
        self.assertEqual(wa_context.recent_block(self.conn, jid, days=14), "")


if __name__ == "__main__":
    unittest.main()
