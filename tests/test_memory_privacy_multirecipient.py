"""drafting-safety-2 (privacy): private 1:1 relationship memory must NOT be injected into a
reply-all / multi-recipient (CC'd) EMAIL draft. The original group guard only covered
WhatsApp @g.us, so a sender's distilled 1:1 facts/decisions leaked to unrelated CC'd third
parties. The guard is now recipient-aware: 1:1 memory is injected only when the reply would
reach exactly one external recipient.

Mirrors tests/test_memory_drafting (CaptureLLM grabs the assembled system prompt).
"""

from __future__ import annotations

import unittest

from assistant.action import drafting
from assistant.brain.tiers import TierConfig, decide
from assistant.config import Settings
from assistant.memory import distill
from assistant.memory.distill import RelationshipMemory
from assistant.models import Channel, Contact, Message, Thread
from assistant.storage import db
from assistant.storage import repositories as repo
from tests.helpers import make_decision


class CaptureLLM:
    def __init__(self):
        self.system = ""

    def draft(self, *, system_prefix, user_prompt, **kw):
        self.system = system_prefix
        return "Thanks, will follow up."


def _settings(**kw):
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com",
                telegram_chat_id="1", memory_enabled=True)
    base.update(kw)
    return Settings(**base)


def _seed_alice_memory(conn):
    repo.person_add(conn, person_id="P", display_name="Alice", emails=["alice@vc.com"])
    repo.person_link_set(conn, "alice@vc.com", "P")
    m = RelationshipMemory("P")
    m.summary = {"comp": "180k", "relationship": "investor"}
    m.open_situations = [{"key": "equity", "situation": "equity dispute over the SAFE",
                          "awaiting": "us", "status": "open"}]
    distill.save_memory(conn, m)


def _email_thread(cc):
    msg = Message(id="im1", thread_id="t1", channel=Channel.GMAIL, sender_email="alice@vc.com",
                  subject="Re: terms", body_text="thoughts?", recipients=["me@x.com"], cc=cc)
    return Thread(id="t1", channel=Channel.GMAIL, subject="Re: terms", messages=[msg])


def _final(thread, contact):
    return decide(thread, make_decision(proposed_tier=2, needs_reply=True), contact, TierConfig())


class TestMultiRecipientMemoryPrivacy(unittest.TestCase):
    def test_one_to_one_email_still_gets_memory(self):
        conn = db.open_db(":memory:")
        try:
            _seed_alice_memory(conn)
            thread = _email_thread(cc=[])           # only Alice → 1:1
            contact = Contact(email="alice@vc.com", name="Alice")
            cap = CaptureLLM()
            drafting.draft_reply(conn, cap, _settings(), thread, contact, _final(thread, contact))
            self.assertIn("equity dispute over the SAFE", cap.system)
        finally:
            conn.close()

    def test_reply_all_email_does_not_get_private_memory(self):
        conn = db.open_db(":memory:")
        try:
            _seed_alice_memory(conn)
            # Alice CCs two unrelated third parties → reply-all reaches 3 externals.
            thread = _email_thread(cc=["bob@partner.com", "carol@other.com"])
            contact = Contact(email="alice@vc.com", name="Alice")
            cap = CaptureLLM()
            drafting.draft_reply(conn, cap, _settings(), thread, contact, _final(thread, contact))
            # The private 1:1 facts/decisions must NOT be in the prompt that drafts a reply-all.
            self.assertNotIn("equity dispute over the SAFE", cap.system)
            self.assertNotIn("180k", cap.system)
        finally:
            conn.close()

    def test_own_address_in_cc_is_not_counted(self):
        """Our own address in CC does not make a 1:1 thread look multi-recipient."""
        conn = db.open_db(":memory:")
        try:
            _seed_alice_memory(conn)
            thread = _email_thread(cc=["me@x.com"])   # only us in cc → still 1:1 with Alice
            contact = Contact(email="alice@vc.com", name="Alice")
            cap = CaptureLLM()
            drafting.draft_reply(conn, cap, _settings(), thread, contact, _final(thread, contact))
            self.assertIn("equity dispute over the SAFE", cap.system)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
