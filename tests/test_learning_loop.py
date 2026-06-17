"""GAP 5 — learning loop: every signal carries a type; /api/learning aggregates them."""

from __future__ import annotations

import unittest

from assistant.config import Settings
from assistant.storage import db, decision_log, read_queries as rq
from assistant.storage import repositories as repo
from assistant.web import service


def _mkdb():
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _pending(conn, mid="m1", *, status="PENDING"):
    aid = repo.create_pending(
        conn, idempotency_key=f"{mid}:2", message_id=mid, thread_id="t",
        tier=2, kind="reply_draft", summary="s", draft_text="hi",
    )
    if status != "PENDING":
        conn.execute("UPDATE pending_actions SET status=? WHERE id=?", (status, aid))
    return aid


class TestEventTypesWritten(unittest.TestCase):
    def test_skip_writes_type_skip(self):
        conn = _mkdb()
        aid = _pending(conn)
        service.skip(conn, aid)
        rows = conn.execute("SELECT type FROM learning_events WHERE action_id=?", (aid,)).fetchall()
        self.assertTrue(rows)
        self.assertIn("skip", [r["type"] for r in rows])

    def test_approve_writes_type_approve(self):
        conn = _mkdb()
        aid = _pending(conn)
        settings = Settings(mode="dry_run")
        service.approve(conn, None, settings, None, aid)
        rows = conn.execute("SELECT type FROM learning_events WHERE action_id=?", (aid,)).fetchall()
        self.assertIn("approve", [r["type"] for r in rows])

    def test_edit_writes_type_edit(self):
        conn = _mkdb()
        aid = _pending(conn)
        service.edit(conn, aid, "edited text")
        rows = conn.execute("SELECT type FROM learning_events WHERE action_id=?", (aid,)).fetchall()
        self.assertIn("edit", [r["type"] for r in rows])

    def test_no_null_types(self):
        conn = _mkdb()
        # Defensive: record_event never writes a NULL/blank type.
        repo.record_event(conn, type="", message_id="x")
        row = conn.execute("SELECT type FROM learning_events WHERE message_id='x'").fetchone()
        self.assertTrue(row["type"])  # not None / not empty


class TestLearningSummary(unittest.TestCase):
    def test_counts_by_type(self):
        conn = _mkdb()
        a1 = _pending(conn, "m1")
        a2 = _pending(conn, "m2")
        a3 = _pending(conn, "m3")
        service.skip(conn, a1)
        service.skip(conn, a2)
        service.approve(conn, None, Settings(mode="dry_run"), None, a3)
        summary = rq.learning_summary(conn)
        self.assertGreaterEqual(summary["by_type"].get("skip", 0), 2)
        self.assertGreaterEqual(summary["by_type"].get("approve", 0), 1)
        self.assertIsInstance(summary["last_7_days"], list)

    def test_endpoint(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:  # noqa: BLE001
            self.skipTest("fastapi TestClient not installed")
        import tempfile
        from assistant.web import api as webapi
        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        orig = webapi._settings
        webapi._settings = Settings(db_path=tf.name)
        try:
            client = TestClient(webapi.app)
            r = client.get("/api/learning")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIn("by_type", body)
            self.assertIn("last_7_days", body)
        finally:
            webapi._settings = orig


if __name__ == "__main__":
    unittest.main()
