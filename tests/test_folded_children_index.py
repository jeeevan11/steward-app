"""scaling-time-2: the queue's folded-child lookup is an indexed point lookup on the
folded_children table, not an unindexed leading-wildcard LIKE full-scan of the never-pruned
pending_actions table once per non-pending queue row.

We verify (a) folds populate the index, (b) _is_folded_child resolves via the index, (c) it
returns False for a non-folded id, and (d) the legacy JSON-scan fallback still works for rows
whose fold predates the index (the index emptied).
"""

from __future__ import annotations

import unittest

from assistant.storage import db, decision_log, read_queries
from assistant.storage import repositories as repo


def _mkdb():
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _create(conn, message_id):
    return repo.create_pending(
        conn, idempotency_key=f"{message_id}:2", message_id=message_id, thread_id="t",
        tier=2, kind="reply_draft", summary="s", draft_text="d")


class TestFoldedChildrenIndex(unittest.TestCase):
    def test_fold_populates_index(self):
        conn = _mkdb()
        aid = _create(conn, "parent_m1")
        repo.fold_message_into_action(conn, aid, "child_m2", "s2", "d2")
        rows = {r[0] for r in conn.execute(
            "SELECT child_message_id FROM folded_children").fetchall()}
        self.assertIn("parent_m1", rows)
        self.assertIn("child_m2", rows)

    def test_is_folded_child_true_via_index(self):
        conn = _mkdb()
        aid = _create(conn, "parent_m1")
        repo.fold_message_into_action(conn, aid, "child_m2", "s2", "d2")
        self.assertTrue(read_queries._is_folded_child(conn, "child_m2"))

    def test_is_folded_child_false_for_unfolded(self):
        conn = _mkdb()
        _create(conn, "solo_m1")
        self.assertFalse(read_queries._is_folded_child(conn, "never_seen"))
        self.assertFalse(read_queries._is_folded_child(conn, ""))

    def test_legacy_json_scan_fallback(self):
        """If the index row is missing (a fold from before the table existed), the lookup
        still finds the child via the anchored JSON scan, so behavior is unchanged."""
        conn = _mkdb()
        aid = _create(conn, "parent_m1")
        repo.fold_message_into_action(conn, aid, "child_m2", "s2", "d2")
        # Simulate legacy data: clear the index but keep folded_message_ids on the parent.
        conn.execute("DELETE FROM folded_children")
        self.assertTrue(read_queries._is_folded_child(conn, "child_m2"))

    def test_no_substring_collision(self):
        """An id that is a substring of a folded id but not an exact quoted match is NOT a
        folded child (the anchored fallback preserves this)."""
        conn = _mkdb()
        aid = _create(conn, "parent_m1")
        repo.fold_message_into_action(conn, aid, "child_m2_long", "s2", "d2")
        conn.execute("DELETE FROM folded_children")   # force the JSON-scan path
        self.assertFalse(read_queries._is_folded_child(conn, "child_m2"))


if __name__ == "__main__":
    unittest.main()
