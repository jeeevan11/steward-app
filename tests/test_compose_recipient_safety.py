"""drafting-safety-3 (NO_WRONG_RECIPIENT): a compose addressed to one person must resolve to
a single unambiguous recipient. Any ambiguity surfaces for the owner to pick exactly one,
rather than emailing every fuzzy match (Samuel/Samantha/Samir).

Stdlib + in-memory contacts table, mirroring tests/test_compose.
"""

from __future__ import annotations

import sqlite3
import unittest

from assistant.action import compose


class _FakeLLM:
    def complete_text(self, **kw):
        return "Hi, the term sheet is signed."


def _settings(dry_run=False):
    class S:
        pass
    s = S()
    s.dry_run = dry_run
    return s


def _db_with_contacts(rows):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE contacts (email TEXT PRIMARY KEY, name TEXT, phone TEXT, channel TEXT)")
    for email, name in rows:
        conn.execute("INSERT INTO contacts (email, name, phone, channel) VALUES (?,?,?,?)",
                     (email, name, "", ""))
    return conn


class TestComposeAmbiguityGate(unittest.TestCase):
    def test_three_fuzzy_matches_need_clarification(self):
        conn = _db_with_contacts([
            ("samuel@bigco.com", "Samuel Roy"),
            ("samantha@competitor.com", "Samantha Lee"),
            ("samir@vendor.com", "Samir Shah"),
        ])
        result = compose.compose_and_queue("email Sam that the term sheet is signed",
                                           "gmail", conn, _settings(), _FakeLLM())
        # Was '> 3' (silently sent to 3). Now ANY ambiguity blocks and asks the owner.
        self.assertEqual(result["status"], "needs_clarification")
        self.assertGreaterEqual(len(result["options"]), 2)

    def test_two_fuzzy_matches_need_clarification(self):
        conn = _db_with_contacts([
            ("chris.a@x.com", "Chris Allen"),
            ("chris.b@y.com", "Chris Baker"),
        ])
        result = compose.compose_and_queue("email Chris about the deck",
                                           "gmail", conn, _settings(), _FakeLLM())
        self.assertEqual(result["status"], "needs_clarification")

    def test_single_match_is_ready(self):
        conn = _db_with_contacts([("rajesh@x.com", "Rajesh Kumar")])
        result = compose.compose_and_queue("email Rajesh that the deck slips",
                                           "gmail", conn, _settings(), _FakeLLM())
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["recipients"]), 1)

    def test_exact_name_match_collapses_fuzzy_set(self):
        """An exact first-name match ('Sam') is preferred over Samuel/Samantha, so it is a
        single unambiguous recipient and proceeds."""
        conn = _db_with_contacts([
            ("sam@x.com", "Sam"),
            ("samuel@bigco.com", "Samuel Roy"),
            ("samantha@competitor.com", "Samantha Lee"),
        ])
        result = compose.compose_and_queue("email Sam that the term sheet is signed",
                                           "gmail", conn, _settings(), _FakeLLM())
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["recipients"]), 1)
        self.assertEqual(result["recipients"][0]["email"], "sam@x.com")

    def test_no_match_is_not_found(self):
        conn = _db_with_contacts([("alice@x.com", "Alice Smith")])
        result = compose.compose_and_queue("email Rajesh about the demo",
                                           "gmail", conn, _settings(), _FakeLLM())
        self.assertEqual(result["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
