"""Unit tests for assistant/memory/opportunities.py.

Uses stdlib only (unittest + sqlite3 in-memory DB). No network, no LLM calls.
"""

from __future__ import annotations

import sqlite3
import unittest

from assistant.memory import opportunities as OPP
from assistant.storage import operating_state as os_store


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSettings:
    def __init__(self, opportunity_detection_enabled: bool = True):
        self.opportunity_detection_enabled = opportunity_detection_enabled


class FakeLLMClient:
    """Records whether it was called."""

    def __init__(self):
        self.called = False

    def complete_text(self, **kwargs):
        self.called = True
        return '{"is_opportunity": false, "confidence": 0.0}'


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestFormatPipelineChatEmpty(unittest.TestCase):
    """format_pipeline_chat([]) must return a non-empty string without crashing."""

    def test_format_pipeline_chat_empty(self):
        result = OPP.format_pipeline_chat([])
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)


class TestFormatPipelineChatItems(unittest.TestCase):
    """format_pipeline_chat with a term_sheet investor must contain stage and pct."""

    def test_format_pipeline_chat_items(self):
        opps = [
            {
                "type": "investor",
                "stage": "term_sheet",
                "person_id": "rajesh@horizonvc.com",
                "probability": 0.7,
                "value_est": 500000,
                "next_action": "review terms",
            }
        ]
        result = OPP.format_pipeline_chat(opps)
        self.assertIn("TERM_SHEET", result.upper().replace(" ", "_").replace("-", "_"))
        # The stage label is stage.upper() == "TERM_SHEET"
        self.assertIn("TERM_SHEET", result)
        self.assertIn("70%", result)


class TestGetOpportunityPipelineSorted(unittest.TestCase):
    """get_opportunity_pipeline must return rows sorted by value_est*probability DESC."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        os_store.ensure_tables(self.db)

    def tearDown(self):
        self.db.close()

    def test_get_opportunity_pipeline_sorted(self):
        # Insert 3 opportunities with different expected values:
        #   A: 100 * 0.5 = 50
        #   B: 200 * 0.9 = 180  <- highest
        #   C: 150 * 0.3 = 45   <- lowest
        os_store.create_opportunity(
            self.db,
            person_id="alice@example.com",
            type="investor",
            stage="intro",
            value_est=100,
            probability=0.5,
            next_action="follow up",
        )
        os_store.create_opportunity(
            self.db,
            person_id="bob@example.com",
            type="partner",
            stage="diligence",
            value_est=200,
            probability=0.9,
            next_action="send deck",
        )
        os_store.create_opportunity(
            self.db,
            person_id="carol@example.com",
            type="supplier",
            stage="identified",
            value_est=150,
            probability=0.3,
            next_action="intro call",
        )

        pipeline = OPP.get_opportunity_pipeline(self.db)

        self.assertEqual(len(pipeline), 3)
        # Verify descending order by value_est * probability
        expected_order = ["bob@example.com", "alice@example.com", "carol@example.com"]
        actual_order = [row["person_id"] for row in pipeline]
        self.assertEqual(actual_order, expected_order)


class TestDetectOpportunitySkippedWhenDisabled(unittest.TestCase):
    """detect_opportunity must return None and never call the LLM when disabled."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        os_store.ensure_tables(self.db)

    def tearDown(self):
        self.db.close()

    def test_detect_opportunity_skipped_when_disabled(self):
        settings = FakeSettings(opportunity_detection_enabled=False)
        llm = FakeLLMClient()

        result = OPP.detect_opportunity(
            thread_id="thread-001",
            subject="Investment opportunity",
            sender_name="Rajesh Kumar",
            sender_email="rajesh@horizonvc.com",
            category="finance",
            tier="vip",
            snippet="We are interested in leading your Series A.",
            db=self.db,
            settings=settings,
            llm_client=llm,
        )

        self.assertIsNone(result)
        self.assertFalse(llm.called, "LLM must not be called when detection is disabled")


if __name__ == "__main__":
    unittest.main()
