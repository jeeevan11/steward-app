"""Tests for assistant/storage/operating_state.py.

Uses an in-memory SQLite database created via operating_state.ensure_tables()
(not db.open_db) so these tests are self-contained and fast.
"""

import sqlite3
import unittest

from assistant.storage import operating_state


def _make_conn() -> sqlite3.Connection:
    """Open a bare in-memory connection that matches what ensure_tables expects."""
    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class TestEnsureTables(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        operating_state.ensure_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_ensure_tables(self):
        """ensure_tables creates all four base tables and expected indexes."""
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for expected in ("threads", "projects", "opportunities", "risks"):
            self.assertIn(expected, tables, f"missing table: {expected}")

        indexes = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        self.assertIn("idx_threads_status", indexes)
        self.assertIn("idx_threads_project_id", indexes)

    def test_ensure_tables_idempotent(self):
        """Calling ensure_tables twice must not raise."""
        operating_state.ensure_tables(self.conn)  # second call


class TestUpsertThreadAndGetByStatus(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        operating_state.ensure_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_upsert_thread_and_get_by_status(self):
        operating_state.upsert_thread(
            self.conn, "t1", "gmail", "awaiting_me", person_id="p1", subject="Hello"
        )
        operating_state.upsert_thread(
            self.conn, "t2", "whatsapp", "awaiting_them", person_id="p2", subject="World"
        )

        me_threads = operating_state.get_threads_by_status(self.conn, "awaiting_me")
        them_threads = operating_state.get_threads_by_status(self.conn, "awaiting_them")

        self.assertEqual(len(me_threads), 1)
        self.assertEqual(me_threads[0]["thread_id"], "t1")
        self.assertEqual(me_threads[0]["status"], "awaiting_me")

        self.assertEqual(len(them_threads), 1)
        self.assertEqual(them_threads[0]["thread_id"], "t2")
        self.assertEqual(them_threads[0]["status"], "awaiting_them")


class TestUpdateThreadStatus(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        operating_state.ensure_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_update_thread_status(self):
        operating_state.upsert_thread(
            self.conn, "t1", "gmail", "awaiting_me", subject="Initial"
        )
        self.assertEqual(
            operating_state.get_thread_status(self.conn, "t1"), "awaiting_me"
        )

        operating_state.update_thread_status(self.conn, "t1", "done")
        self.assertEqual(
            operating_state.get_thread_status(self.conn, "t1"), "done"
        )


class TestUpsertProject(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        operating_state.ensure_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_upsert_project(self):
        pid = operating_state.upsert_project(
            self.conn, "Alpha", status="active", description="First project"
        )
        self.assertIsNotNone(pid)
        self.assertIsInstance(pid, int)

        project = operating_state.get_project(self.conn, "Alpha")
        self.assertIsNotNone(project)
        self.assertEqual(project["name"], "Alpha")
        self.assertEqual(project["status"], "active")
        self.assertEqual(project["description"], "First project")

    def test_upsert_project_updates_on_conflict(self):
        operating_state.upsert_project(self.conn, "Beta", status="active")
        operating_state.upsert_project(self.conn, "Beta", status="blocked")

        project = operating_state.get_project(self.conn, "Beta")
        self.assertEqual(project["status"], "blocked")


class TestOpportunityPipelineSort(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        operating_state.ensure_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_opportunity_pipeline_sort(self):
        # Expected values (value_est * probability):
        #   low:    100 * 0.1  =  10
        #   medium: 200 * 0.5  = 100
        #   high:   500 * 0.8  = 400  <- highest expected value
        operating_state.create_opportunity(
            self.conn, "p_low", "sales", value_est=100.0, probability=0.1
        )
        operating_state.create_opportunity(
            self.conn, "p_high", "sales", value_est=500.0, probability=0.8
        )
        operating_state.create_opportunity(
            self.conn, "p_medium", "sales", value_est=200.0, probability=0.5
        )

        pipeline = operating_state.get_opportunity_pipeline(self.conn)
        self.assertEqual(len(pipeline), 3)
        person_ids = [o["person_id"] for o in pipeline]
        self.assertEqual(person_ids[0], "p_high")
        self.assertEqual(person_ids[1], "p_medium")
        self.assertEqual(person_ids[2], "p_low")


class TestCreateResolveRisk(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        operating_state.ensure_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_create_resolve_risk(self):
        rid = operating_state.create_risk(
            self.conn,
            type="security",
            description="Open port exposed",
            severity="high",
        )
        self.assertIsNotNone(rid)

        open_risks = operating_state.get_open_risks(self.conn)
        self.assertEqual(len(open_risks), 1)
        self.assertEqual(open_risks[0]["id"], rid)

        operating_state.resolve_risk(self.conn, rid)

        open_risks_after = operating_state.get_open_risks(self.conn)
        self.assertEqual(open_risks_after, [])


class TestSearchThreadsFts(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        operating_state.ensure_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_search_threads_fts(self):
        try:
            self.conn.execute("SELECT * FROM threads_fts LIMIT 0")
        except sqlite3.OperationalError:
            self.skipTest("FTS5 not available in this SQLite build")

        operating_state.upsert_thread(
            self.conn,
            "t_fts",
            "gmail",
            "awaiting_me",
            person_id="p1",
            subject="quarterly revenue forecast",
        )

        results = operating_state.search_threads_fts(self.conn, "revenue")
        found_ids = [r["thread_id"] for r in results]
        self.assertIn("t_fts", found_ids)

    def test_search_threads_fts_no_match(self):
        try:
            self.conn.execute("SELECT * FROM threads_fts LIMIT 0")
        except sqlite3.OperationalError:
            self.skipTest("FTS5 not available in this SQLite build")

        operating_state.upsert_thread(
            self.conn, "t_other", "gmail", "done", subject="coffee order"
        )

        results = operating_state.search_threads_fts(self.conn, "zebra")
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
