"""Few-shot voice examples are picked by SIMILARITY to the thread being replied to, so a
casual ping gets casual exemplars and a formal email gets formal ones — instead of just the
most recent. Recency stays the default when no thread context is supplied (back-compat)."""

from __future__ import annotations

import unittest

from assistant.storage import db
from assistant.storage import repositories as repo


class TestVoiceSampleSimilarity(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        # global samples: one formal/finance, one casual/dinner. Stamp explicit ts so the
        # recency-fallback is deterministic (dinner is the NEWEST).
        repo.add_voice_sample(
            self.conn, subject="Q3 financial report",
            body="Please find attached the quarterly audited financial statements for board review.")
        repo.add_voice_sample(
            self.conn, subject="dinner friday?",
            body="yo wanna grab dinner friday night thinking tacos lol")
        self.conn.execute("UPDATE voice_samples SET ts=1000 WHERE subject='Q3 financial report'")
        self.conn.execute("UPDATE voice_samples SET ts=2000 WHERE subject='dinner friday?'")
        self.conn.commit()

    def _pick(self, context):
        return repo.get_voice_samples(self.conn, "", limit=1, context_text=context)[0]["subject"]

    def test_casual_thread_picks_casual_sample(self):
        self.assertEqual(self._pick("hey still on for dinner tonight tacos?"), "dinner friday?")

    def test_formal_thread_picks_formal_sample(self):
        self.assertEqual(
            self._pick("attached please find the annual financial audit report figures"),
            "Q3 financial report")

    def test_no_context_falls_back_to_recency(self):
        # default path unchanged: newest sample first
        self.assertEqual(repo.get_voice_samples(self.conn, "", limit=1)[0]["subject"],
                         "dinner friday?")

    def test_empty_context_is_recency(self):
        self.assertEqual(
            repo.get_voice_samples(self.conn, "", limit=1, context_text="   ")[0]["subject"],
            "dinner friday?")


if __name__ == "__main__":
    unittest.main()
