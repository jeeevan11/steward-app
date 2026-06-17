"""Tests for the Phase 4 evaluation framework.

Two layers:
  1. Pure-function tests for evaluation/metrics.py (no DB, no LLM, fully offline).
  2. A --no-llm runner smoke test on a tiny inline dataset, asserting the real brain
     runs end-to-end and produces well-formed records + metrics. Mirrors test_flow's
     FakeLLM mode so it needs no API key or network.

Run: .venv/bin/python -m unittest tests.test_evaluation
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from evaluation import metrics as metrics_mod
from evaluation import run_all, runner


# ─────────────────────────────────────────────────────────────────────────────
# 1) Pure metrics
# ─────────────────────────────────────────────────────────────────────────────
def _rec(**kw):
    base = dict(
        id="x", expected_tier=2, actual_tier=2, expected_category="work_request",
        actual_category="work_request", expected_suppressed=False, actual_suppressed=False,
        consequential=True, error=None,
    )
    base.update(kw)
    return base


class TestMetricsPure(unittest.TestCase):
    def test_tier_accuracy_all_hit(self):
        recs = [_rec(actual_tier=2, expected_tier=2), _rec(actual_tier=0, expected_tier=0)]
        self.assertEqual(metrics_mod.tier_accuracy(recs), 1.0)

    def test_tier_accuracy_half(self):
        recs = [_rec(actual_tier=2, expected_tier=2), _rec(actual_tier=1, expected_tier=3)]
        self.assertEqual(metrics_mod.tier_accuracy(recs), 0.5)

    def test_errors_excluded_from_accuracy(self):
        recs = [_rec(actual_tier=2, expected_tier=2), _rec(error="boom", actual_tier=None)]
        # only the one clean record counts -> 1.0, not 0.5
        self.assertEqual(metrics_mod.tier_accuracy(recs), 1.0)

    def test_suppression_accuracy(self):
        recs = [
            _rec(expected_suppressed=True, actual_suppressed=True),
            _rec(expected_suppressed=False, actual_suppressed=True),
        ]
        self.assertEqual(metrics_mod.suppression_accuracy(recs), 0.5)

    def test_escalation_accuracy_counts_only_consequential(self):
        recs = [
            _rec(consequential=True, actual_tier=3),    # surfaced -> good
            _rec(consequential=True, actual_tier=1),    # quiet -> miss
            _rec(consequential=False, actual_tier=0),   # not counted
        ]
        self.assertEqual(metrics_mod.escalation_accuracy(recs), 0.5)

    def test_false_positive_rate_surfaced_noise(self):
        recs = [
            _rec(expected_tier=0, actual_tier=2),   # noise surfaced -> FP
            _rec(expected_tier=0, actual_tier=0),   # noise stayed quiet -> ok
        ]
        self.assertEqual(metrics_mod.false_positive_rate(recs), 0.5)

    def test_false_negative_rate_silenced_important(self):
        recs = [
            _rec(expected_tier=3, actual_tier=1),   # silenced -> FN
            _rec(expected_tier=2, actual_tier=2),   # surfaced -> ok
        ]
        self.assertEqual(metrics_mod.false_negative_rate(recs), 0.5)

    def test_empty_slice_is_vacuously_perfect(self):
        # no expected-noise items -> FP rate 1.0 (ratio of an empty denominator)
        recs = [_rec(expected_tier=2, actual_tier=2)]
        self.assertEqual(metrics_mod.false_positive_rate(recs), 1.0)

    def test_compute_metrics_shape(self):
        recs = [_rec(), _rec(expected_tier=0, actual_tier=0, consequential=False)]
        m = metrics_mod.compute_metrics(recs)
        for key in ("n_total", "n_scored", "n_errors", "tier_accuracy",
                    "suppression_accuracy", "escalation_accuracy", "category_accuracy",
                    "false_positive_rate", "false_negative_rate", "draft_acceptance"):
            self.assertIn(key, m)
        self.assertEqual(m["n_total"], 2)
        self.assertEqual(m["n_errors"], 0)
        self.assertFalse(m["draft_acceptance"]["available"])

    def test_draft_acceptance_proxy_reads_learning_events(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE learning_events (id INTEGER PRIMARY KEY, type TEXT)")
        for t in ("approve", "approve", "approve", "edit"):
            conn.execute("INSERT INTO learning_events (type) VALUES (?)", (t,))
        out = metrics_mod.draft_acceptance_proxy(conn=conn)
        self.assertTrue(out["available"])
        self.assertEqual(out["approve"], 3)
        self.assertEqual(out["edit"], 1)
        self.assertEqual(out["acceptance"], 0.75)


# ─────────────────────────────────────────────────────────────────────────────
# 2) Runner smoke test (--no-llm)
# ─────────────────────────────────────────────────────────────────────────────
class TestRunnerNoLLM(unittest.TestCase):
    def setUp(self):
        self.env = runner.build_env(no_llm=True)

    def tearDown(self):
        try:
            self.env["mem"].close()
        except Exception:  # noqa: BLE001
            pass

    def test_investor_floor_escalates(self):
        # The investor flag + firm-domain + cap table keywords force ASK via the REAL
        # deterministic guardrails — independent of the canned FakeLLM judgment.
        scenario = {
            "id": "smoke-investor", "channel": "gmail", "sender": "priya@peakvc.com",
            "subject": "runway", "body": "thoughts on cap table and valuation?",
            "expected_tier": 3, "expected_category": "investor",
            "expected_suppressed": False, "flags": ["investor"],
        }
        rec = runner.run_scenario(self.env, scenario)
        self.assertIsNone(rec["error"])
        self.assertEqual(rec["actual_tier"], 3, rec.get("applied_floors"))

    def test_personal_floor_escalates(self):
        scenario = {
            "id": "smoke-personal", "channel": "whatsapp",
            "sender": "919999988888@s.whatsapp.net", "subject": "",
            "body": "coming home this weekend bhai?", "expected_tier": 3,
            "expected_category": "personal", "expected_suppressed": False,
            "flags": ["personal"],
        }
        rec = runner.run_scenario(self.env, scenario)
        self.assertIsNone(rec["error"])
        self.assertEqual(rec["actual_tier"], 3)

    def test_record_is_well_formed(self):
        scenario = {
            "id": "smoke-shape", "channel": "gmail", "sender": "a@b.com",
            "subject": "hi", "body": "just saying hi", "expected_tier": 2,
            "expected_category": "work_request", "expected_suppressed": False,
        }
        rec = runner.run_scenario(self.env, scenario)
        for key in ("id", "expected_tier", "actual_tier", "actual_category",
                    "actual_suppressed", "consequential", "error"):
            self.assertIn(key, rec)
        self.assertIsNone(rec["error"])
        self.assertIn(rec["actual_tier"], (0, 1, 2, 3))

    def test_run_dataset_and_metrics(self):
        scenarios = [
            {"id": "s1", "channel": "gmail", "sender": "priya@peakvc.com", "subject": "x",
             "body": "cap table and valuation", "expected_tier": 3,
             "expected_category": "investor", "expected_suppressed": False, "flags": ["investor"]},
            {"id": "s2", "channel": "whatsapp", "sender": "919999988888@s.whatsapp.net",
             "subject": "", "body": "home this weekend?", "expected_tier": 3,
             "expected_category": "personal", "expected_suppressed": False, "flags": ["personal"]},
        ]
        records = runner.run_dataset(self.env, scenarios)
        self.assertEqual(len(records), 2)
        m = metrics_mod.compute_metrics(records, conn=self.env["mem"])
        self.assertEqual(m["n_errors"], 0)
        # both labeled consequential and both correctly escalate -> 1.0
        self.assertEqual(m["escalation_accuracy"], 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# 3) run_all glue (stamp, report I/O, regression compare) — offline
# ─────────────────────────────────────────────────────────────────────────────
class TestRunAllGlue(unittest.TestCase):
    def test_make_stamp_is_filesystem_safe(self):
        self.assertEqual(run_all.make_stamp("my run/2026"), "my-run-2026")
        self.assertTrue(run_all.make_stamp())  # derived, non-empty

    def test_compare_regressions_detects_drop_and_rise(self):
        prev = {"stamp": "p", "metrics": {
            "tier_accuracy": 0.9, "suppression_accuracy": 1.0, "escalation_accuracy": 1.0,
            "category_accuracy": 1.0, "false_positive_rate": 0.0, "false_negative_rate": 0.0,
            "n_errors": 0}}
        cur = {"metrics": {
            "tier_accuracy": 0.8, "suppression_accuracy": 1.0, "escalation_accuracy": 1.0,
            "category_accuracy": 1.0, "false_positive_rate": 0.25, "false_negative_rate": 0.0,
            "n_errors": 1}}
        regs = run_all.compare_regressions(cur, prev)
        names = {r["metric"] for r in regs}
        self.assertIn("tier_accuracy", names)        # dropped
        self.assertIn("false_positive_rate", names)  # rose
        self.assertIn("n_errors", names)             # rose

    def test_compare_regressions_no_previous(self):
        self.assertEqual(run_all.compare_regressions({"metrics": {}}, None), [])

    def test_write_and_read_report_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = {"no_llm": True, "n_scenarios": 1, "datasets": {},
                      "metrics": {"tier_accuracy": 1.0, "n_errors": 0}}
            path = run_all.write_report(bundle, "20260101T000000", reports_dir=tmp)
            self.assertTrue(os.path.exists(path))
            prev = run_all.previous_report(reports_dir=tmp)
            self.assertEqual(prev["stamp"], "20260101T000000")
            self.assertEqual(prev["metrics"]["tier_accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
