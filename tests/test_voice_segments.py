"""P5a — segmented voice profiles: detection, per-segment drafting, global fallback,
and a non-destructive rebuild."""

from __future__ import annotations

import json
import unittest

from assistant.action import voice
from assistant.config import Settings
from assistant.memory import contacts as memory_contacts
from assistant.models import Contact
from assistant.storage import db
from assistant.storage import repositories as repo


def _settings(**kw) -> Settings:
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com", telegram_chat_id="1")
    base.update(kw)
    return Settings(**base)


class FakeLLM:
    def complete_text(self, *, system_prefix, user_prompt, **kw) -> str:
        return "DISTILLED VOICE for this segment."


class TestSegmentDetection(unittest.TestCase):
    def test_investor_domain(self):
        conn = db.open_db(":memory:")
        try:
            self.assertEqual(memory_contacts.detect_segment(conn, "partner@a venture firm.com"), "investor")
        finally:
            conn.close()

    def test_team_flag(self):
        conn = db.open_db(":memory:")
        try:
            repo.upsert_contact(conn, Contact(email="cofounder@startup.com", flags={"team"}))
            self.assertEqual(memory_contacts.detect_segment(conn, "cofounder@startup.com"), "team")
        finally:
            conn.close()

    def test_customer_flag(self):
        conn = db.open_db(":memory:")
        try:
            repo.upsert_contact(conn, Contact(email="buyer@corp.com", flags={"customer"}))
            self.assertEqual(memory_contacts.detect_segment(conn, "buyer@corp.com"), "customer")
        finally:
            conn.close()

    def test_unknown_is_external(self):
        conn = db.open_db(":memory:")
        try:
            self.assertEqual(memory_contacts.detect_segment(conn, "rando@gmail.com"), "external")
        finally:
            conn.close()


class TestSegmentDrafting(unittest.TestCase):
    def test_uses_segment_profile_when_rich(self):
        conn = db.open_db(":memory:")
        try:
            repo.upsert_voice_profile(
                conn, "investor", json.dumps({"summary": "INVESTOR VOICE", "examples": []}), 8
            )
            prefix = voice.voice_prefix(conn, "partner@a venture firm.com", _settings())
            self.assertIn("INVESTOR VOICE", prefix)
            self.assertIn("investor", prefix)
        finally:
            conn.close()

    def test_falls_back_to_global_when_thin(self):
        conn = db.open_db(":memory:")
        try:
            repo.kv_set(conn, "voice_profile", "GLOBAL VOICE")
            # investor profile exists but is below the 5-sample threshold → ignored
            repo.upsert_voice_profile(
                conn, "investor", json.dumps({"summary": "THIN INVESTOR", "examples": []}), 2
            )
            prefix = voice.voice_prefix(conn, "partner@a venture firm.com", _settings())
            self.assertIn("GLOBAL VOICE", prefix)
            self.assertNotIn("THIN INVESTOR", prefix)
        finally:
            conn.close()


class TestRebuild(unittest.TestCase):
    def test_rebuild_is_non_destructive_and_writes_profile(self):
        conn = db.open_db(":memory:")
        try:
            # 6 investor-domain samples → enough to build an 'investor' profile.
            for i in range(6):
                repo.add_voice_sample(conn, body=f"Quarterly update {i}.", contact_email="gp@sequoia.com")
            before = repo.voice_sample_count(conn)
            rebuilt = voice.build_segment_profiles(conn, FakeLLM(), _settings())
            self.assertIn("investor", rebuilt)
            self.assertEqual(rebuilt["investor"], 6)
            # samples are NOT consumed by the rebuild
            self.assertEqual(repo.voice_sample_count(conn), before)
            row = repo.get_voice_profile(conn, "investor")
            self.assertIsNotNone(row)
            self.assertEqual(row["sample_count"], 6)
            self.assertIn("DISTILLED VOICE", json.loads(row["profile_json"])["summary"])
        finally:
            conn.close()

    def test_thin_segment_skipped(self):
        conn = db.open_db(":memory:")
        try:
            for i in range(2):
                repo.add_voice_sample(conn, body=f"hi {i}", contact_email="rando@gmail.com")
            rebuilt = voice.build_segment_profiles(conn, FakeLLM(), _settings())
            self.assertNotIn("external", rebuilt)  # < 5 samples → skipped
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
