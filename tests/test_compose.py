"""Tests for assistant/action/compose.py.

Uses only stdlib + unittest.mock; no external dependencies.
"""

import sqlite3
import unittest
from unittest.mock import MagicMock

from assistant.action.compose import detect_compose_intent, resolve_recipients


class TestDetectComposeIntentEmail(unittest.TestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_detect_compose_intent_email(self):
        result = detect_compose_intent("email rajesh that the deck slips to Friday")
        self.assertIsNotNone(result)
        self.assertEqual(result["channel"], "gmail")
        self.assertIn("intent_text", result)



class TestDetectComposeIntentText(unittest.TestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_detect_compose_intent_text(self):
        result = detect_compose_intent("text priya to confirm tomorrow's meeting")
        self.assertIsNotNone(result)
        self.assertEqual(result["channel"], "whatsapp")

    def test_detect_compose_intent_whatsapp(self):
        result = detect_compose_intent("whatsapp chen about the launch date")
        self.assertIsNotNone(result)
        self.assertEqual(result["channel"], "whatsapp")

    def test_detect_compose_intent_ping(self):
        result = detect_compose_intent("ping rahul and ask him to join the call")
        self.assertIsNotNone(result)
        self.assertEqual(result["channel"], "whatsapp")


class TestDetectComposeIntentNone(unittest.TestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_detect_compose_intent_none(self):
        result = detect_compose_intent("what time is it?")
        self.assertIsNone(result)

    def test_detect_compose_intent_none_empty(self):
        result = detect_compose_intent("")
        self.assertIsNone(result)

    def test_detect_compose_intent_none_question(self):
        result = detect_compose_intent("how are you doing today?")
        self.assertIsNone(result)


class TestDetectComposeIntentFollowup(unittest.TestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_detect_compose_intent_followup(self):
        result = detect_compose_intent("follow up with chen about the proposal")
        self.assertIsNotNone(result)
        self.assertIn("intent_text", result)

    def test_detect_compose_intent_reply_to(self):
        result = detect_compose_intent("reply to rajesh about the invoice")
        self.assertIsNotNone(result)
        self.assertIn("intent_text", result)

    def test_detect_compose_intent_reach_out(self):
        result = detect_compose_intent("reach out to priya with the schedule")
        self.assertIsNotNone(result)
        self.assertIn("intent_text", result)


class TestResolveRecipientsFound(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE contacts "
            "(email TEXT PRIMARY KEY, name TEXT, phone TEXT, channel TEXT)"
        )
        self.conn.execute(
            "INSERT INTO contacts (email, name, phone, channel) "
            "VALUES ('rajesh@example.com', 'Rajesh Kumar', '+91999', 'whatsapp')"
        )

    def tearDown(self):
        self.conn.close()

    def test_resolve_recipients_found(self):
        results = resolve_recipients(
            "email Rajesh that the deck is ready", self.conn
        )
        self.assertTrue(len(results) >= 1)
        emails = [r["email"] for r in results]
        self.assertIn("rajesh@example.com", emails)

    def test_resolve_recipients_returns_dict_with_expected_keys(self):
        results = resolve_recipients("email Rajesh about the update", self.conn)
        self.assertTrue(len(results) >= 1)
        record = results[0]
        for key in ("name", "email", "phone", "channel"):
            self.assertIn(key, record)


class TestResolveRecipientsEmpty(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE contacts "
            "(email TEXT PRIMARY KEY, name TEXT, phone TEXT, channel TEXT)"
        )
        # table exists but has no rows

    def tearDown(self):
        self.conn.close()

    def test_resolve_recipients_empty(self):
        results = resolve_recipients("email Rajesh about the pitch deck", self.conn)
        self.assertEqual(results, [])

    def test_resolve_recipients_no_matching_name(self):
        # Table has data but no match for the name in the intent
        self.conn.execute(
            "INSERT INTO contacts (email, name, phone, channel) "
            "VALUES ('other@example.com', 'Alice Smith', '', '')"
        )
        results = resolve_recipients("email Rajesh about the demo", self.conn)
        self.assertEqual(results, [])

    def test_resolve_recipients_no_contacts_table(self):
        """When the contacts table is missing entirely, return [] gracefully."""
        bare_conn = sqlite3.connect(":memory:", isolation_level=None)
        bare_conn.row_factory = sqlite3.Row
        try:
            results = resolve_recipients("email Rajesh the report", bare_conn)
            self.assertEqual(results, [])
        finally:
            bare_conn.close()


if __name__ == "__main__":
    unittest.main()
