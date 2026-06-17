"""Phase 5 — confidence calibration tests.

Inserts synthetic decision_log + learning_events rows (well-calibrated vs over-confident
scenarios), runs compute(), and asserts the per-bin accuracy, overall Brier-style score,
and n-weighted calibration error are sensible. Also checks get_curve() round-trips the
stored bins and that an empty DB degrades gracefully (no raise, empty results).

In-memory SQLite only — never touches the live DB. Stdlib only.
"""

from __future__ import annotations

import unittest

from assistant.storage import calibration, db, decision_log


def _fresh_conn():
    """In-memory DB with the decision_log table created (it lives behind its own ensure())."""
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _add_decision(conn, *, message_id, confidence, final_tier, base_tier=None):
    base_tier = final_tier if base_tier is None else base_tier
    conn.execute(
        "INSERT INTO decision_log (message_id, confidence, final_tier, base_tier) "
        "VALUES (?,?,?,?)",
        (message_id, confidence, final_tier, base_tier),
    )


def _add_event(conn, *, message_id, type):
    conn.execute(
        "INSERT INTO learning_events (type, message_id) VALUES (?,?)",
        (type, message_id),
    )


class TestCalibration(unittest.TestCase):
    def test_empty_db_is_graceful(self):
        conn = _fresh_conn()
        try:
            curve = calibration.compute(conn)
            self.assertEqual(curve["bins"], [])
            self.assertEqual(curve["scored"], 0)
            self.assertIsNone(curve["brier"])
            self.assertIsNone(curve["calibration_error"])
            self.assertEqual(calibration.get_curve(conn), [])
            # calibrated() falls back to the raw value when there's no data
            self.assertAlmostEqual(calibration.calibrated(conn, 0.83), 0.83)
        finally:
            conn.close()

    def test_well_calibrated_scenario(self):
        """Surfaced (tier 3) items at ~0.9 confidence that the human approves 9/10 times
        should produce a 0.9-1.0 bin whose accuracy tracks its predicted mean."""
        conn = _fresh_conn()
        try:
            # 10 high-confidence surfaced items; 9 approved, 1 skipped => 0.9 accuracy
            for i in range(10):
                mid = f"hi-{i}"
                _add_decision(conn, message_id=mid, confidence=0.95, final_tier=3)
                _add_event(conn, message_id=mid, type="approve" if i < 9 else "skip")

            curve = calibration.compute(conn)
            bins = {b["bucket"]: b for b in curve["bins"]}
            self.assertIn("0.9-1.0", bins)
            top = bins["0.9-1.0"]
            self.assertEqual(top["n"], 10)
            self.assertEqual(top["correct"], 9)
            self.assertAlmostEqual(top["accuracy"], 0.9)
            self.assertAlmostEqual(top["predicted_mean"], 0.95)
            self.assertEqual(curve["scored"], 10)
            # well calibrated => small gap between predicted_mean and accuracy
            self.assertLess(curve["calibration_error"], 0.1)
            self.assertIsNotNone(curve["brier"])
        finally:
            conn.close()

    def test_overconfident_scenario_has_large_calibration_error(self):
        """Brain claims 0.95 but is only right ~0.3 of the time -> a big calibration gap
        and a worse (higher) Brier score than the well-calibrated case."""
        conn = _fresh_conn()
        try:
            for i in range(10):
                mid = f"oc-{i}"
                _add_decision(conn, message_id=mid, confidence=0.95, final_tier=3)
                # only 3 of 10 approved => 0.3 empirical accuracy vs 0.95 claimed
                _add_event(conn, message_id=mid, type="approve" if i < 3 else "skip")

            curve = calibration.compute(conn)
            bins = {b["bucket"]: b for b in curve["bins"]}
            top = bins["0.9-1.0"]
            self.assertEqual(top["n"], 10)
            self.assertEqual(top["correct"], 3)
            self.assertAlmostEqual(top["accuracy"], 0.3)
            # predicted_mean 0.95 vs accuracy 0.3 => gap ~0.65
            self.assertGreater(curve["calibration_error"], 0.5)
            # Brier is poor (predicting 0.95 when right 30% of the time)
            self.assertGreater(curve["brier"], 0.4)
        finally:
            conn.close()

    def test_auto_handled_items_are_correct_unless_overridden(self):
        """Tier 0/1 items with no negative feedback count as correct silence; an override
        on a low-tier item counts as incorrect (the human had to step in)."""
        conn = _fresh_conn()
        try:
            # 4 quietly-handled items at 0.8, no negative feedback => all correct
            for i in range(4):
                _add_decision(conn, message_id=f"q-{i}", confidence=0.85, final_tier=0)
            # 1 quietly-handled item the human had to override => incorrect
            _add_decision(conn, message_id="q-bad", confidence=0.85, final_tier=1)
            _add_event(conn, message_id="q-bad", type="override")

            curve = calibration.compute(conn)
            bins = {b["bucket"]: b for b in curve["bins"]}
            self.assertIn("0.8-0.9", bins)
            slot = bins["0.8-0.9"]
            self.assertEqual(slot["n"], 5)
            self.assertEqual(slot["correct"], 4)
            self.assertAlmostEqual(slot["accuracy"], 0.8)
        finally:
            conn.close()

    def test_surfaced_without_human_signal_is_excluded(self):
        """A surfaced (tier 2/3) item with no human verdict yet must NOT be scored."""
        conn = _fresh_conn()
        try:
            _add_decision(conn, message_id="pending", confidence=0.7, final_tier=3)
            # no learning_event for it
            curve = calibration.compute(conn)
            self.assertEqual(curve["scored"], 0)
            self.assertEqual(curve["bins"], [])
            # n counts all decisions; scored excludes the unverdicted one
            self.assertEqual(curve["n"], 1)
        finally:
            conn.close()

    def test_get_curve_round_trips_stored_bins(self):
        conn = _fresh_conn()
        try:
            for i in range(6):
                mid = f"r-{i}"
                _add_decision(conn, message_id=mid, confidence=0.75, final_tier=2)
                _add_event(conn, message_id=mid, type="approve" if i < 4 else "skip")

            computed = calibration.compute(conn)
            stored = calibration.get_curve(conn)
            self.assertEqual(len(stored), len(computed["bins"]))

            stored_by_bucket = {b["bucket"]: b for b in stored}
            for cb in computed["bins"]:
                sb = stored_by_bucket[cb["bucket"]]
                self.assertEqual(sb["n"], cb["n"])
                self.assertEqual(sb["correct"], cb["correct"])
                self.assertAlmostEqual(sb["accuracy"], cb["accuracy"])
                self.assertAlmostEqual(sb["predicted_mean"], cb["predicted_mean"])

            # 4/6 approved => 0.6667 accuracy in the 0.7-0.8 bin
            self.assertIn("0.7-0.8", stored_by_bucket)
            self.assertAlmostEqual(stored_by_bucket["0.7-0.8"]["accuracy"], round(4 / 6, 4))
        finally:
            conn.close()

    def test_calibrated_maps_to_empirical_accuracy(self):
        """After compute(), calibrated() returns the bin's empirical accuracy, not raw."""
        conn = _fresh_conn()
        try:
            for i in range(10):
                mid = f"c-{i}"
                _add_decision(conn, message_id=mid, confidence=0.95, final_tier=3)
                _add_event(conn, message_id=mid, type="approve" if i < 4 else "skip")
            calibration.compute(conn)
            # raw 0.97 lands in 0.9-1.0; empirical accuracy 0.4 is shrunk toward the raw
            # confidence by sample count (n=10, PSEUDO=5): (0.4*10 + 0.97*5)/15 ≈ 0.59.
            self.assertAlmostEqual(calibration.calibrated(conn, 0.97), (0.4 * 10 + 0.97 * 5) / 15)
            self.assertLess(calibration.calibrated(conn, 0.97), 0.97)   # empirical pulled it down
            self.assertGreater(calibration.calibrated(conn, 0.97), 0.4)  # smoothing keeps it above bare empirical
            # a bin with no data falls back to raw
            self.assertAlmostEqual(calibration.calibrated(conn, 0.15), 0.15)
        finally:
            conn.close()

    def test_calibrated_smoothing_resists_single_sample(self):
        """The metrics-honesty fix: an n=1 bin must NOT snap the gate to 0%/100%."""
        conn = _fresh_conn()
        try:
            _add_decision(conn, message_id="solo", confidence=0.95, final_tier=3)
            _add_event(conn, message_id="solo", type="skip")  # 1 sample → 0% bare empirical
            calibration.compute(conn)
            out = calibration.calibrated(conn, 0.95)
            self.assertGreater(out, 0.5)   # NOT snapped to ~0.0 on one sample
            self.assertLess(out, 0.95)     # but the lone miss did pull it below raw
        finally:
            conn.close()

    def test_compute_is_idempotent(self):
        """Re-running compute() replaces, not duplicates, the stored bins."""
        conn = _fresh_conn()
        try:
            for i in range(5):
                mid = f"i-{i}"
                _add_decision(conn, message_id=mid, confidence=0.65, final_tier=2)
                _add_event(conn, message_id=mid, type="approve")
            first = calibration.compute(conn)
            second = calibration.compute(conn)
            self.assertEqual(len(calibration.get_curve(conn)), len(first["bins"]))
            self.assertEqual(first["bins"], second["bins"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
