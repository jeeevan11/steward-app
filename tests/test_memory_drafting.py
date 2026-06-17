"""Fix: the drafter now sees relationship memory (the S4 finding). The memory block is
injected into the drafting system prompt so replies have continuity — gated on
memory_enabled, best-effort, never fabricating."""

from __future__ import annotations

import unittest

from assistant.action import drafting
from assistant.brain.tiers import TierConfig, decide
from assistant.config import Settings
from assistant.memory import distill
from assistant.memory.distill import RelationshipMemory
from assistant.models import Contact
from assistant.storage import db
from assistant.storage import repositories as repo
from tests.helpers import make_decision, make_message, make_thread


class CaptureLLM:
    def __init__(self):
        self.system = ""

    def draft(self, *, system_prefix, user_prompt, **kw):
        self.system = system_prefix
        return "Sounds good, will do."


def _settings(**kw):
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com",
                telegram_chat_id="1", memory_enabled=True)
    base.update(kw)
    return Settings(**base)


def _seed_person_with_memory(conn):
    repo.person_add(conn, person_id="P", display_name="Dana", emails=["dana@acme.com"])
    repo.person_link_set(conn, "dana@acme.com", "P")
    m = RelationshipMemory("P")
    m.summary = {"company": "Acme", "relationship": "customer"}
    m.open_situations = [{"key": "nda", "situation": "awaiting the signed NDA",
                          "awaiting": "them", "status": "open"}]
    distill.save_memory(conn, m)


def _final(thread, contact):
    return decide(thread, make_decision(proposed_tier=2, needs_reply=True), contact, TierConfig())


class TestMemoryAwareDrafting(unittest.TestCase):
    def test_memory_block_injected_into_draft_prompt(self):
        conn = db.open_db(":memory:")
        try:
            _seed_person_with_memory(conn)
            thread = make_thread(make_message("Any movement on the paperwork?", sender="dana@acme.com"))
            contact = Contact(email="dana@acme.com", name="Dana")
            cap = CaptureLLM()
            drafting.draft_reply(conn, cap, _settings(), thread, contact, _final(thread, contact))
            self.assertIn("awaiting the signed NDA", cap.system)   # open situation in the prompt
            self.assertIn("Acme", cap.system)                      # known fact in the prompt
            self.assertIn("continuity", cap.system.lower())        # the continuity instruction
        finally:
            conn.close()

    def test_no_memory_block_when_disabled(self):
        conn = db.open_db(":memory:")
        try:
            _seed_person_with_memory(conn)
            thread = make_thread(make_message("Any movement?", sender="dana@acme.com"))
            contact = Contact(email="dana@acme.com", name="Dana")
            cap = CaptureLLM()
            drafting.draft_reply(conn, cap, _settings(memory_enabled=False), thread, contact,
                                 _final(thread, contact))
            self.assertNotIn("awaiting the signed NDA", cap.system)
        finally:
            conn.close()

    def test_unknown_person_drafts_without_memory_and_does_not_crash(self):
        conn = db.open_db(":memory:")
        try:
            thread = make_thread(make_message("hello", sender="stranger@x.com"))
            contact = Contact(email="stranger@x.com")
            cap = CaptureLLM()
            out = drafting.draft_reply(conn, cap, _settings(), thread, contact, _final(thread, contact))
            self.assertTrue(out.strip())
            self.assertNotIn("awaiting the signed NDA", cap.system)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
