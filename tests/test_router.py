import unittest

from assistant.config import Settings
from assistant.llm.router import Task, TaskRouter
from assistant.storage import db, metrics


def _settings():
    return Settings(judge_model="google/gemini-2.5-flash", noise_model="google/gemini-2.5-flash",
                    draft_model="deepseek/deepseek-chat", pro_model="google/gemini-2.5-pro")


class TestTaskRouter(unittest.TestCase):
    def setUp(self):
        self.r = TaskRouter(_settings())

    def test_every_task_maps(self):
        for t in Task.ALL:
            spec = self.r.resolve(t)
            self.assertTrue(spec.model)
            self.assertEqual(spec.task, t)

    def test_hybrid_judge_is_flash_critical_is_pro(self):
        self.assertEqual(self.r.resolve(Task.JUDGE).model, "google/gemini-2.5-flash")
        self.assertTrue(self.r.resolve(Task.JUDGE).thinking)
        self.assertEqual(self.r.resolve(Task.JUDGE_CRITICAL).model, "google/gemini-2.5-pro")
        self.assertGreaterEqual(self.r.resolve(Task.JUDGE_CRITICAL).reasoning_tokens, 8192)

    def test_noise_is_cheap_no_thinking(self):
        spec = self.r.resolve(Task.NOISE_FILTER)
        self.assertEqual(spec.model, "google/gemini-2.5-flash")
        self.assertFalse(spec.thinking)
        self.assertEqual(spec.reasoning_tokens, 0)

    def test_draft_uses_deepseek(self):
        self.assertEqual(self.r.resolve(Task.DRAFT).model, "deepseek/deepseek-chat")

    def test_critical_tasks_surface_on_fallback(self):
        self.assertTrue(self.r.resolve(Task.JUDGE).surface_on_fallback)
        self.assertTrue(self.r.resolve(Task.JUDGE_CRITICAL).surface_on_fallback)
        self.assertFalse(self.r.resolve(Task.NOISE_FILTER).surface_on_fallback)

    def test_fallback_is_flash(self):
        self.assertEqual(self.r.resolve(Task.JUDGE_CRITICAL).fallback_model, "google/gemini-2.5-flash")

    def test_unknown_task_falls_back(self):
        spec = self.r.resolve("NONSENSE")
        self.assertEqual(spec.model, "google/gemini-2.5-flash")

    def test_cost_estimate(self):
        c = TaskRouter.estimate_cost("google/gemini-2.5-pro", 1_000_000, 1_000_000)
        self.assertAlmostEqual(c, 1.25 + 10.00, places=2)
        # unknown model uses a default price, never zero
        self.assertGreater(TaskRouter.estimate_cost("mystery/model", 1000, 1000), 0)


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_bump_and_read(self):
        metrics.bump(self.conn, "emails_processed", 3, day="2026-06-15")
        metrics.bump(self.conn, "auto_handled", day="2026-06-15")
        rows = metrics.daily(self.conn)
        self.assertEqual(rows[0]["emails_processed"], 3)
        self.assertEqual(rows[0]["auto_handled"], 1)

    def test_unknown_counter_ignored(self):
        metrics.bump(self.conn, "not_a_field", 5)  # must not raise
        self.assertTrue(True)

    def test_record_llm_call_and_costs(self):
        metrics.record_llm_call(self.conn, task="JUDGE", model="google/gemini-2.5-flash",
                                prompt_tokens=100, completion_tokens=50, cost=0.001, message_id="m1")
        rows = metrics.costs_by_task(self.conn, 0)
        self.assertEqual(rows[0]["task"], "JUDGE")
        self.assertEqual(rows[0]["calls"], 1)

    def test_sink_writes(self):
        import tempfile, os
        p = tempfile.mktemp(suffix=".db")
        sink = metrics.make_sink(p)
        sink({"task": "DRAFT", "model": "deepseek/deepseek-chat",
              "prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001, "message_id": "x"})
        conn = db.open_db(p)
        n = conn.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()["n"]
        conn.close(); os.remove(p)
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
