"""Phase 6 — memory governance tests.

Exercises the additive fact_metadata sidecar: first-sight base confidence; repeated
same-value strengthening (verification_count + rising confidence); a different value reading
as a contradiction (confidence drop); exponential half-life decay over simulated time;
expired_facts surfacing decayed-AND-stale facts; explicit verify() strengthening; and graceful
degradation on an empty/missing table.

In-memory SQLite only — never touches the live DB. Stdlib only.
"""

from __future__ import annotations

import sqlite3
import unittest

from assistant.memory import governance

_DAY = 86400
_PID = "person:alex"


def _fresh_conn() -> sqlite3.Connection:
    """In-memory DB. governance.ensure() creates its own table behind every call, so we
    deliberately do NOT pre-create it here (mirrors the real lazy-ensure idiom)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


class TestFirstSight(unittest.TestCase):
    def test_first_sight_sets_base_confidence(self):
        conn = _fresh_conn()
        res = governance.observe_fact(conn, _PID, "city", "Berlin")
        self.assertEqual(res["confidence"], governance.BASE_CONFIDENCE)
        self.assertEqual(res["verification_count"], 0)
        self.assertFalse(res["contradicted"])
        self.assertEqual(governance.fact_confidence(conn, _PID, "city"), governance.BASE_CONFIDENCE)


class TestStrengthen(unittest.TestCase):
    def test_repeated_same_value_strengthens(self):
        conn = _fresh_conn()
        governance.observe_fact(conn, _PID, "city", "Berlin")
        r2 = governance.observe_fact(conn, _PID, "city", "Berlin")
        r3 = governance.observe_fact(conn, _PID, "city", "Berlin")
        # confidence rises monotonically toward the ceiling
        self.assertGreater(r2["confidence"], governance.BASE_CONFIDENCE)
        self.assertGreater(r3["confidence"], r2["confidence"])
        self.assertLessEqual(r3["confidence"], governance.MAX_CONFIDENCE)
        # verification_count climbs with each agreeing observation
        self.assertEqual(r2["verification_count"], 1)
        self.assertEqual(r3["verification_count"], 2)
        self.assertFalse(r3["contradicted"])

    def test_strengthen_is_asymptotic(self):
        conn = _fresh_conn()
        governance.observe_fact(conn, _PID, "role", "CTO")
        for _ in range(50):
            res = governance.observe_fact(conn, _PID, "role", "CTO")
        self.assertLessEqual(res["confidence"], governance.MAX_CONFIDENCE)
        self.assertGreater(res["confidence"], 0.9)


class TestContradiction(unittest.TestCase):
    def test_different_value_is_contradiction_and_drops_confidence(self):
        conn = _fresh_conn()
        governance.observe_fact(conn, _PID, "city", "Berlin")
        strong = governance.observe_fact(conn, _PID, "city", "Berlin")
        self.assertGreater(strong["confidence"], governance.BASE_CONFIDENCE)
        # a new, different value contradicts the record
        res = governance.observe_fact(conn, _PID, "city", "Munich")
        self.assertTrue(res["contradicted"])
        self.assertEqual(res["verification_count"], 0)         # streak broken
        self.assertLess(res["confidence"], strong["confidence"])
        self.assertEqual(res["confidence"], governance.CONTRADICTION_CONFIDENCE)

    def test_detect_contradiction(self):
        conn = _fresh_conn()
        self.assertFalse(governance.detect_contradiction(conn, _PID, "city", "Berlin"))  # no record
        governance.observe_fact(conn, _PID, "city", "Berlin")
        self.assertFalse(governance.detect_contradiction(conn, _PID, "city", "Berlin"))  # same
        self.assertTrue(governance.detect_contradiction(conn, _PID, "city", "Paris"))    # different


class TestDecay(unittest.TestCase):
    def test_decay_lowers_confidence_over_time(self):
        conn = _fresh_conn()
        t0 = 1_000_000
        # build a strong fact, then verify at t0 so last_verified_at is pinned
        governance.observe_fact(conn, _PID, "city", "Berlin", now=t0)
        governance.observe_fact(conn, _PID, "city", "Berlin", now=t0)
        governance.verify(conn, _PID, "city", now=t0)
        before = governance.fact_confidence(conn, _PID, "city")
        # one half-life later -> roughly half the confidence
        later = t0 + 30 * _DAY
        adjusted = governance.decay(conn, half_life_days=30, now=later)
        self.assertEqual(adjusted, 1)
        after = governance.fact_confidence(conn, _PID, "city")
        self.assertLess(after, before)
        self.assertAlmostEqual(after, before * 0.5, places=2)

    def test_decay_no_time_no_change(self):
        conn = _fresh_conn()
        t0 = 1_000_000
        governance.observe_fact(conn, _PID, "city", "Berlin", now=t0)
        adjusted = governance.decay(conn, half_life_days=30, now=t0)
        self.assertEqual(adjusted, 0)


class TestExpiry(unittest.TestCase):
    def test_expired_facts_returns_decayed_and_stale(self):
        conn = _fresh_conn()
        t0 = 1_000_000
        future = t0 + 365 * _DAY
        governance.observe_fact(conn, _PID, "stale_fact", "old", now=t0)
        # a fresh, strong fact verified right before the evaluation -> must NOT expire
        recent = future - 5 * _DAY
        governance.observe_fact(conn, _PID, "fresh_fact", "new", now=recent)
        governance.observe_fact(conn, _PID, "fresh_fact", "new", now=recent)
        # decay everything as of `future`: the stale fact falls below the floor; the fresh
        # one was just reinforced 5 days ago so it stays high
        governance.decay(conn, half_life_days=30, now=future)
        expired = governance.expired_facts(
            conn, min_confidence=0.25, stale_days=120, now=future
        )
        keys = {k for (_pid, k) in expired}
        self.assertIn("stale_fact", keys)
        self.assertNotIn("fresh_fact", keys)

    def test_recent_low_confidence_fact_not_expired(self):
        conn = _fresh_conn()
        t0 = 1_000_000
        # low confidence (single sight = BASE 0.5, but drop the floor above it) yet recent
        governance.observe_fact(conn, _PID, "guess", "maybe", now=t0)
        expired = governance.expired_facts(
            conn, min_confidence=0.9, stale_days=120, now=t0 + 1
        )
        # below the 0.9 floor but NOT stale -> given a chance, not expired
        self.assertEqual(expired, [])


class TestVerify(unittest.TestCase):
    def test_verify_strengthens(self):
        conn = _fresh_conn()
        governance.observe_fact(conn, _PID, "city", "Berlin")
        ok = governance.verify(conn, _PID, "city")
        self.assertTrue(ok)
        self.assertEqual(governance.fact_confidence(conn, _PID, "city"), governance.VERIFY_CONFIDENCE)

    def test_verify_missing_fact_returns_false(self):
        conn = _fresh_conn()
        self.assertFalse(governance.verify(conn, _PID, "nope"))


class TestGracefulDegradation(unittest.TestCase):
    def test_missing_table_degrades(self):
        # a connection whose table was never created and queries that hit a broken conn
        conn = _fresh_conn()
        # fact_confidence on an empty (but ensured) DB -> None, no raise
        self.assertIsNone(governance.fact_confidence(conn, _PID, "anything"))
        self.assertFalse(governance.detect_contradiction(conn, _PID, "k", "v"))
        self.assertEqual(governance.expired_facts(conn), [])
        self.assertEqual(governance.decay(conn), 0)

    def test_empty_identifiers_degrade(self):
        conn = _fresh_conn()
        res = governance.observe_fact(conn, "", "", "v")
        self.assertFalse(res["contradicted"])
        self.assertIsNone(governance.fact_confidence(conn, "", ""))
        self.assertFalse(governance.verify(conn, "", ""))

    def test_closed_conn_never_raises(self):
        conn = _fresh_conn()
        conn.close()
        # every entry point must swallow the "Cannot operate on a closed database" error
        self.assertEqual(governance.observe_fact(conn, _PID, "k", "v")["confidence"],
                         governance.BASE_CONFIDENCE)
        self.assertIsNone(governance.fact_confidence(conn, _PID, "k"))
        self.assertFalse(governance.detect_contradiction(conn, _PID, "k", "v"))
        self.assertFalse(governance.verify(conn, _PID, "k"))
        self.assertEqual(governance.decay(conn), 0)
        self.assertEqual(governance.expired_facts(conn), [])


if __name__ == "__main__":
    unittest.main()
