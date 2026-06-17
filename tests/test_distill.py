"""Memory Part B — relationship distill loop.

Pure apply-ops (ADD/UPDATE/DELETE/NOOP), recency supersede, size caps, defensive
parsing, load/save round-trip, and the distill() integration with a fake LLM
(including the fail-safe: a broken model leaves memory untouched)."""

from __future__ import annotations

import json
import unittest

from assistant.config import Settings
from assistant.memory import distill
from assistant.memory.distill import RelationshipMemory, apply_ops, parse_ops
from assistant.storage import db
from tests.helpers import make_message, make_thread


def _settings(**kw) -> Settings:
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com", telegram_chat_id="1")
    base.update(kw)
    return Settings(**base)


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete_json(self, *, task, system_prefix, user_text, schema, max_tokens=700, message_id=""):
        self.calls += 1
        return json.dumps(self.payload)


class BoomLLM:
    def complete_json(self, **kw):
        raise RuntimeError("model down")


class TestApplyOps(unittest.TestCase):
    def test_add_and_noop(self):
        mem = RelationshipMemory("p")
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "company", "value": "Acme"},
                                  {"op": "NOOP", "key": "x", "value": "y"}]}, now=1)
        self.assertEqual(mem.summary["company"], "Acme")
        self.assertNotIn("x", mem.summary)

    def test_update_supersedes_old_recency(self):
        mem = RelationshipMemory("p")
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "stage", "value": "seed"}]}, now=100)
        apply_ops(mem, {"facts": [{"op": "UPDATE", "key": "stage", "value": "series A"}]}, now=200)
        self.assertEqual(mem.summary["stage"], "series A")          # newer wins
        self.assertTrue(any(s["fact"] == "stage" and s["value"] == "seed"
                            for s in mem.superseded))               # old archived, not deleted

    def test_delete_archives(self):
        mem = RelationshipMemory("p", summary={"role": "CTO"})
        apply_ops(mem, {"facts": [{"op": "DELETE", "key": "role"}]}, now=5)
        self.assertNotIn("role", mem.summary)
        self.assertTrue(any(s["fact"] == "role" for s in mem.superseded))

    def test_open_situation_upsert_and_resolve(self):
        mem = RelationshipMemory("p")
        apply_ops(mem, {"open_situations": [{"op": "ADD", "key": "quote", "situation": "awaiting quote",
                                             "awaiting": "them", "status": "open"}]}, now=1)
        self.assertEqual(len(mem.open_situations), 1)
        apply_ops(mem, {"open_situations": [{"op": "UPDATE", "key": "quote", "situation": "quote received",
                                             "awaiting": "nobody", "status": "resolved"}]}, now=2)
        self.assertEqual(len(mem.open_situations), 1)               # updated in place, not duplicated
        self.assertEqual(mem.open_situations[0]["status"], "resolved")

    def test_decided_is_append_only_and_deduped(self):
        mem = RelationshipMemory("p")
        op = {"decided": [{"op": "ADD", "decision": "reconnect after launch", "source_message_id": "m1"}]}
        apply_ops(mem, op, now=1)
        apply_ops(mem, op, now=2)                                   # same decision again
        self.assertEqual(len(mem.decided), 1)

    def test_caps_enforced(self):
        mem = RelationshipMemory("p")
        many = {"decided": [{"op": "ADD", "decision": f"d{i}", "source_message_id": ""} for i in range(40)]}
        apply_ops(mem, many, now=1)
        self.assertLessEqual(len(mem.decided), 25)


class TestParse(unittest.TestCase):
    def test_bad_json_is_empty(self):
        self.assertEqual(parse_ops("not json"), {"facts": [], "open_situations": [], "decided": []})
        self.assertEqual(parse_ops({"facts": "nope"})["facts"], [])
        self.assertEqual(parse_ops(None)["decided"], [])


class TestDistillIntegration(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.thread = make_thread(make_message("Following up on the quote.", sender="v@acme.com"))

    def tearDown(self):
        self.conn.close()

    def test_load_empty_then_roundtrip(self):
        mem = distill.load_memory(self.conn, "p1")
        self.assertTrue(mem.is_empty())
        mem.summary["company"] = "Acme"
        mem.version = 1
        distill.save_memory(self.conn, mem)
        again = distill.load_memory(self.conn, "p1")
        self.assertEqual(again.summary["company"], "Acme")
        self.assertEqual(again.version, 1)

    def test_distill_updates_and_bumps_version(self):
        llm = FakeLLM({"facts": [{"op": "ADD", "key": "company", "value": "Acme"}],
                       "open_situations": [], "decided": []})
        ok = distill.distill(self.conn, llm, _settings(), "p1", self.thread, now=123)
        self.assertTrue(ok)
        mem = distill.load_memory(self.conn, "p1")
        self.assertEqual(mem.summary["company"], "Acme")
        self.assertEqual(mem.version, 1)
        self.assertEqual(mem.last_distilled_at, 123)

    def test_broken_llm_leaves_memory_untouched(self):
        ok = distill.distill(self.conn, BoomLLM(), _settings(), "p2", self.thread)
        self.assertFalse(ok)
        self.assertTrue(distill.load_memory(self.conn, "p2").is_empty())

    def test_disabled_is_noop(self):
        llm = FakeLLM({"facts": [{"op": "ADD", "key": "x", "value": "y"}], "open_situations": [], "decided": []})
        ok = distill.distill(self.conn, llm, _settings(memory_distill_enabled=False), "p3", self.thread)
        self.assertFalse(ok)
        self.assertEqual(llm.calls, 0)
        self.assertTrue(distill.load_memory(self.conn, "p3").is_empty())


if __name__ == "__main__":
    unittest.main()
