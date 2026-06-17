"""Regression: the metrics cache must not serve a stale snapshot as if it were current.
`cache_get(max_age_seconds=...)` treats an old snapshot as a MISS so the read recomputes.
"""

from __future__ import annotations

import unittest

from assistant.storage import db, metrics


class TestMetricsCacheTTL(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        metrics.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_fresh_cache_is_served(self):
        metrics.cache_set(self.conn, "k", {"v": 1})
        self.assertEqual(metrics.cache_get(self.conn, "k", max_age_seconds=3600), {"v": 1})

    def test_stale_cache_is_a_miss(self):
        metrics.cache_set(self.conn, "k", {"v": 1})
        # age the snapshot 2 hours
        self.conn.execute("UPDATE metrics_cache SET computed_at=computed_at-7200 WHERE cache_key='k'")
        self.assertIsNone(metrics.cache_get(self.conn, "k", max_age_seconds=3600))
        # without a TTL it is still returned (back-compat)
        self.assertEqual(metrics.cache_get(self.conn, "k"), {"v": 1})

    def test_missing_key_is_none(self):
        self.assertIsNone(metrics.cache_get(self.conn, "nope", max_age_seconds=3600))


if __name__ == "__main__":
    unittest.main()
