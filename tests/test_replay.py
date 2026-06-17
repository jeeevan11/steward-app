"""Phase 3 — audit trail & replay: prompt versioning + full reasoning reconstruction."""

from __future__ import annotations

import unittest

from assistant.config import Settings
from assistant.llm import prompts
from assistant.storage import db, decision_log, replay


class TestPromptVersioning(unittest.TestCase):
    def test_prompt_hash_stable_and_short(self):
        h1 = prompts.prompt_hash("classifier")
        h2 = prompts.prompt_hash("classifier")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 12)        # short content hash
        self.assertEqual(prompts.prompt_hash("does_not_exist"), "")

    def test_pipeline_versions_covers_steps(self):
        v = prompts.pipeline_versions()
        for name in prompts.PIPELINE_PROMPTS:
            self.assertIn(name, v)


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.settings = Settings()

    def tearDown(self):
        self.conn.close()

    def test_capture_and_reconstruct(self):
        replay.capture(self.conn, self.settings, "m1",
                       context_supplied="SENDER: a@x.com\nmemory...",
                       thread_snapshot="From: a@x.com\nbody")
        rec = replay.reconstruct(self.conn, "m1")
        self.assertIsNotNone(rec)
        self.assertIn("classifier", rec["prompt_versions"])
        self.assertIn("judge", rec["models"])
        self.assertEqual(rec["models"]["judge_critical"]["model"], self.settings.pro_model)
        self.assertIn("SENDER", rec["context_supplied"])

    def test_reconstruct_stitches_raw_reasoning(self):
        decision_log.record_reasoning(self.conn, message_id="m2", thread_id="t",
                                      think_output='{"t":1}', judge_output='{"j":2}',
                                      critique_output='{"c":3}', was_critical=True)
        replay.capture(self.conn, self.settings, "m2")
        rec = replay.reconstruct(self.conn, "m2")
        self.assertEqual(rec["raw_outputs"]["judge"], '{"j":2}')
        self.assertEqual(rec["raw_outputs"]["think"], '{"t":1}')

    def test_reconstruct_missing_returns_none(self):
        self.assertIsNone(replay.reconstruct(self.conn, "nope"))

    def test_render_produces_text(self):
        replay.capture(self.conn, self.settings, "m3")
        text = replay.render(replay.reconstruct(self.conn, "m3"))
        self.assertIn("DECISION REPLAY", text)
        self.assertIn("m3", text)


if __name__ == "__main__":
    unittest.main()
