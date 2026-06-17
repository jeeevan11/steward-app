"""memory-governance cluster — regression tests for the four cluster findings.

Each finding has at least one test that FAILS against the pre-fix behavior and passes
after the additive fix:

  memory-knowledge-2  Governance read-side is wired:
      * build_memory_block demotes a low-confidence fact out of the trusted "Facts:" line
        when (conn, person_id) are supplied (and is a no-op when they are not).
      * forget_expired_facts actually deletes decayed-AND-stale facts from the summary AND
        the fact_metadata row (relationship_memory ROW is never deleted).
      * _enforce_caps evicts the LOWEST-confidence fact (was oldest-inserted) when a
        confidence map is available.

  memory-knowledge-7  Non-literal decisions:
      * a decision distilled from banter/sarcasm/hypothetical is dropped by the gate;
      * a later DELETE/UPDATE op retracts a prior decision into `superseded`;
      * build_memory_block softens "do not re-open" to "previously noted" once stale.

  scaling-time-5  retention.reclaim_disk runs incremental_vacuum + wal_checkpoint(TRUNCATE)
      without raising, and prune() shrinks the on-disk file when auto_vacuum=INCREMENTAL.

  storage-persistence-7  prune batches deletes (commits between chunks), yields to an
      in-flight send, and never touches the ledger / pending / memory tables.

In-memory / temp-file SQLite only; stdlib + injected fakes. Never touches the live DB.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest

from assistant.config import Settings
from assistant.memory import distill, governance, retrieval
from assistant.memory.distill import RelationshipMemory, apply_ops
from assistant.storage import db
from assistant.storage import metrics
from assistant.storage import repositories as repo
from assistant.storage import retention

_DAY = 86400
_PID = "person:alex"


def _settings(**kw) -> Settings:
    base = dict(mode="dry_run", prompts_dir="./prompts",
                gmail_address="me@x.com", telegram_chat_id="1")
    base.update(kw)
    return Settings(**base)


# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-2 — READ-side confidence gate in build_memory_block
# ─────────────────────────────────────────────────────────────────────────────
class TestReadSideConfidenceGate(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _mem_with_fact(self) -> RelationshipMemory:
        mem = RelationshipMemory(person_id=_PID)
        mem.summary["employer"] = "OldCorp"
        # mark it 'observed' so it lands in the trusted "Facts:" line (not "claimed").
        mem.provenance["employer"] = {"source": "x", "source_type": "observed", "ts": 1}
        mem.last_distilled_at = int(time.time())  # fresh, so not stale-softened
        return mem

    def test_no_conn_renders_fact_unchanged(self):
        # The gate must be a strict no-op for the existing callers that pass no conn.
        block = retrieval.build_memory_block(self._mem_with_fact())
        self.assertIn("Facts: employer: OldCorp", block)
        self.assertNotIn("Faded", block)

    def test_low_confidence_fact_is_demoted(self):
        mem = self._mem_with_fact()
        # Drive its tracked confidence below the expiry floor.
        governance.observe_fact(self.conn, _PID, "employer", "OldCorp")
        self.conn.execute(
            "UPDATE fact_metadata SET confidence=? WHERE person_id=? AND fact_key=?",
            (governance.EXPIRY_MIN_CONFIDENCE - 0.1, _PID, "employer"),
        )
        block = retrieval.build_memory_block(mem, conn=self.conn, person_id=_PID)
        # No longer presented as an established fact ...
        self.assertNotIn("Facts: employer: OldCorp", block)
        # ... but surfaced as an explicit low-confidence hint (not silently dropped here).
        self.assertIn("Faded", block)
        self.assertIn("employer: OldCorp", block)

    def test_high_confidence_fact_stays_in_facts_line(self):
        mem = self._mem_with_fact()
        governance.observe_fact(self.conn, _PID, "employer", "OldCorp")
        self.conn.execute(
            "UPDATE fact_metadata SET confidence=0.9 WHERE person_id=? AND fact_key=?",
            (_PID, "employer"),
        )
        block = retrieval.build_memory_block(mem, conn=self.conn, person_id=_PID)
        self.assertIn("Facts: employer: OldCorp", block)
        self.assertNotIn("Faded", block)

    def test_untracked_fact_rendered_unchanged(self):
        # A fact with no metadata row must not be demoted (governance only demotes what it
        # actually scored).
        mem = self._mem_with_fact()
        block = retrieval.build_memory_block(mem, conn=self.conn, person_id=_PID)
        self.assertIn("Facts: employer: OldCorp", block)


# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-2 — forget_expired_facts actually removes decayed+stale facts
# ─────────────────────────────────────────────────────────────────────────────
class TestForgetExpiredFacts(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _plant_expired_fact(self, now: int) -> None:
        mem = RelationshipMemory(person_id=_PID)
        mem.summary["old_city"] = "Berlin"
        mem.summary["current_role"] = "CTO"
        mem.provenance["old_city"] = {"source": "x", "source_type": "claimed", "ts": now}
        distill.save_memory(self.conn, mem)
        # old_city: low confidence AND stale (verified long ago) -> expired
        governance.observe_fact(self.conn, _PID, "old_city", "Berlin", now=now - 200 * _DAY)
        self.conn.execute(
            "UPDATE fact_metadata SET confidence=? WHERE person_id=? AND fact_key=?",
            (0.05, _PID, "old_city"),
        )
        # current_role: fresh + strong -> must survive
        governance.observe_fact(self.conn, _PID, "current_role", "CTO", now=now)
        self.conn.execute(
            "UPDATE fact_metadata SET confidence=0.9 WHERE person_id=? AND fact_key=?",
            (_PID, "current_role"),
        )

    def test_expired_fact_is_forgotten_strong_kept(self):
        now = int(time.time())
        self._plant_expired_fact(now)
        forgotten = governance.forget_expired_facts(self.conn, now=now)
        self.assertEqual(forgotten, 1)
        mem = distill.load_memory(self.conn, _PID)
        self.assertNotIn("old_city", mem.summary)        # forgotten from summary
        self.assertNotIn("old_city", mem.provenance)     # provenance cleared too
        self.assertIn("current_role", mem.summary)       # strong fact survives
        # metadata row for the forgotten fact is gone
        row = self.conn.execute(
            "SELECT 1 FROM fact_metadata WHERE person_id=? AND fact_key=?",
            (_PID, "old_city"),
        ).fetchone()
        self.assertIsNone(row)

    def test_relationship_memory_row_is_never_deleted(self):
        now = int(time.time())
        self._plant_expired_fact(now)
        governance.forget_expired_facts(self.conn, now=now)
        # The ROW must still exist (guarantee: never prune memory rows, only expired keys).
        self.assertIsNotNone(repo.relationship_memory_get(self.conn, _PID))

    def test_forget_records_observability_event(self):
        now = int(time.time())
        self._plant_expired_fact(now)
        seen = {}

        def fake_record(conn, *, type, detail=None, **kw):
            seen["type"] = type
            seen["detail"] = detail

        governance.forget_expired_facts(self.conn, now=now, record_event=fake_record)
        self.assertEqual(seen.get("type"), "memory_fact_forgotten")
        self.assertIn("old_city", (seen.get("detail") or {}).get("keys", []))

    def test_recent_low_confidence_fact_is_given_a_chance(self):
        # Low confidence but NOT stale -> not forgotten (both conditions must hold).
        now = int(time.time())
        mem = RelationshipMemory(person_id=_PID)
        mem.summary["x"] = "y"
        distill.save_memory(self.conn, mem)
        governance.observe_fact(self.conn, _PID, "x", "y", now=now)
        self.conn.execute(
            "UPDATE fact_metadata SET confidence=0.05 WHERE person_id=? AND fact_key=?",
            (_PID, "x"),
        )
        forgotten = governance.forget_expired_facts(self.conn, now=now)
        self.assertEqual(forgotten, 0)
        self.assertIn("x", distill.load_memory(self.conn, _PID).summary)


# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-2 — cap eviction prefers lowest-confidence, not oldest-inserted
# ─────────────────────────────────────────────────────────────────────────────
class TestConfidenceAwareEviction(unittest.TestCase):
    def test_eviction_order_drops_weakest_first(self):
        mem = RelationshipMemory(person_id=_PID)
        # insertion order: a (oldest), b, c (newest)
        mem.summary["a"] = "1"
        mem.summary["b"] = "2"
        mem.summary["c"] = "3"
        # 'a' is the OLDEST but has the HIGHEST confidence; 'b' is weakest.
        conf = {"a": 0.9, "b": 0.1, "c": 0.5}
        order = distill._eviction_order(mem, conf)
        self.assertEqual(order[0], "b")          # weakest evicted first
        self.assertEqual(order[-1], "a")         # strongest evicted last

    def test_eviction_falls_back_to_insertion_order_without_conf(self):
        mem = RelationshipMemory(person_id=_PID)
        mem.summary["a"] = "1"
        mem.summary["b"] = "2"
        self.assertEqual(distill._eviction_order(mem, None), ["a", "b"])

    def test_enforce_caps_keeps_strongest_over_cap(self):
        mem = RelationshipMemory(person_id=_PID)
        # Build more than _MAX_FACTS facts; make the OLDEST one the strongest.
        n = distill._MAX_FACTS + 3
        conf = {}
        for i in range(n):
            k = f"k{i}"
            mem.summary[k] = str(i)
            conf[k] = 0.9 if i == 0 else 0.05  # k0 (oldest) is strongest
        distill._enforce_caps(mem, conf)
        self.assertEqual(len(mem.summary), distill._MAX_FACTS)
        # Old behavior would have dropped k0 (oldest-inserted). New behavior keeps it.
        self.assertIn("k0", mem.summary)


# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-7 — non-literal decision gate + retract path + staleness gate
# ─────────────────────────────────────────────────────────────────────────────
class TestNonLiteralDecisions(unittest.TestCase):
    def test_nonliteral_classifier(self):
        self.assertTrue(distill.is_nonliteral_decision("haha so it's decided, no deal"))
        self.assertTrue(distill.is_nonliteral_decision("just kidding, we cancel"))
        self.assertTrue(distill.is_nonliteral_decision("hypothetically we walk away"))
        self.assertTrue(distill.is_nonliteral_decision("if we ever do this, we'd say no"))
        self.assertFalse(distill.is_nonliteral_decision("declined the partnership offer"))
        self.assertFalse(distill.is_nonliteral_decision(""))
        self.assertFalse(distill.is_nonliteral_decision(None))

    def test_filter_drops_nonliteral_add_keeps_serious(self):
        ops = {"facts": [], "open_situations": [], "decided": [
            {"op": "ADD", "decision": "haha so it's decided, never doing the deal"},
            {"op": "ADD", "decision": "agreed to send the contract Monday"},
        ]}
        out = distill.filter_nonliteral_decisions(ops)
        texts = [d["decision"] for d in out["decided"]]
        self.assertNotIn("haha so it's decided, never doing the deal", texts)
        self.assertIn("agreed to send the contract Monday", texts)

    def test_filter_never_drops_a_delete_op(self):
        # A DELETE that references banter text must still be honored (so a bad decision
        # can be retracted).
        ops = {"decided": [{"op": "DELETE", "decision": "haha that joke decision"}]}
        out = distill.filter_nonliteral_decisions(ops)
        self.assertEqual(len(out["decided"]), 1)

    def test_apply_ops_delete_retracts_decision_into_superseded(self):
        mem = RelationshipMemory(person_id=_PID)
        apply_ops(mem, {"decided": [{"op": "ADD", "decision": "do not do the deal"}]}, now=1)
        self.assertEqual(len(mem.decided), 1)
        apply_ops(mem, {"decided": [{"op": "DELETE", "decision": "do not do the deal"}]}, now=2)
        self.assertEqual(mem.decided, [])
        self.assertTrue(any(s.get("reason") == "decision_retracted" for s in mem.superseded))

    def test_apply_ops_update_retracts_old_and_records_new(self):
        mem = RelationshipMemory(person_id=_PID)
        apply_ops(mem, {"decided": [{"op": "ADD", "decision": "old decision"}]}, now=1)
        apply_ops(mem, {"decided": [
            {"op": "UPDATE", "key": "old decision", "decision": "corrected decision"}
        ]}, now=2)
        texts = [d["decision"] for d in mem.decided]
        self.assertNotIn("old decision", texts)
        self.assertIn("corrected decision", texts)
        self.assertTrue(any(s.get("reason") == "decision_updated" for s in mem.superseded))

    def test_stale_memory_softens_do_not_reopen(self):
        mem = RelationshipMemory(person_id=_PID)
        mem.decided.append({"decision": "declined the deal", "ts": 1})
        now = time.time()
        # fresh -> absolute instruction
        mem.last_distilled_at = int(now)
        fresh = retrieval.build_memory_block(mem, now=now)
        self.assertIn("Already decided (do not re-open)", fresh)
        # stale -> softened to "previously noted"
        mem.last_distilled_at = int(now - (retrieval.MEMORY_STALE_DAYS + 5) * _DAY)
        old = retrieval.build_memory_block(mem, now=now)
        self.assertNotIn("do not re-open", old)
        self.assertIn("Previously noted", old)


# ─────────────────────────────────────────────────────────────────────────────
# scaling-time-5 — reclaim disk: incremental_vacuum + wal_checkpoint(TRUNCATE)
# ─────────────────────────────────────────────────────────────────────────────
class TestReclaimDisk(unittest.TestCase):
    def test_reclaim_disk_never_raises_on_memory_db(self):
        conn = db.open_db(":memory:")
        try:
            retention.reclaim_disk(conn)  # must be a guarded no-op, not an exception
        finally:
            conn.close()

    def test_incremental_vacuum_shrinks_file(self):
        # On a DB created with auto_vacuum=INCREMENTAL, prune's reclaim step returns freed
        # pages to the OS so the file shrinks after a large delete.
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = tmp.name
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE llm_calls (ts INTEGER, blob TEXT)")
            old = int(time.time()) - 365 * _DAY
            big = "x" * 2000
            conn.executemany(
                "INSERT INTO llm_calls (ts, blob) VALUES (?,?)",
                [(old, big) for _ in range(4000)],
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            size_before = os.path.getsize(path)
            retention.prune(conn, _settings(retention_days=90))
            conn.commit()
            size_after = os.path.getsize(path)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0], 0
            )
            self.assertLess(size_after, size_before)  # disk actually reclaimed
            conn.close()
        finally:
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except OSError:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# storage-persistence-7 — batched deletes, send-gate, guarantees intact
# ─────────────────────────────────────────────────────────────────────────────
class TestBatchedPrune(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _seed_old_llm_calls(self, n: int) -> None:
        metrics.ensure(self.conn)
        old = int(time.time()) - 365 * _DAY
        self.conn.executemany(
            "INSERT INTO llm_calls (ts, task, model) VALUES (?,?,?)",
            [(old, "DISTILL", "m") for _ in range(n)],
        )
        self.conn.commit()

    def test_batched_delete_removes_all_in_chunks(self):
        self._seed_old_llm_calls(25)
        cutoff = int(time.time())
        n = retention._batched_delete(
            self.conn, "llm_calls", "ts < ?", (cutoff,), chunk=10
        )
        self.assertEqual(n, 25)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0], 0
        )

    def test_chunk_size_is_clamped(self):
        os.environ["RETENTION_DELETE_CHUNK"] = "5"      # below floor 100
        try:
            self.assertEqual(retention._delete_chunk_size(), 100)
        finally:
            del os.environ["RETENTION_DELETE_CHUNK"]
        os.environ["RETENTION_DELETE_CHUNK"] = "999999"  # above ceiling
        try:
            self.assertEqual(retention._delete_chunk_size(), 50000)
        finally:
            del os.environ["RETENTION_DELETE_CHUNK"]

    def test_prune_yields_when_send_in_flight(self):
        # A SENDING action blocks retention this tick (yield to the human approve path).
        self.conn.execute(
            "INSERT INTO pending_actions (idempotency_key, message_id, tier, kind, status) "
            "VALUES ('k1','m1',2,'reply','SENDING')"
        )
        self._seed_old_llm_calls(5)
        self.conn.commit()
        out = retention.prune(self.conn, _settings())
        self.assertEqual(out, {})
        # nothing was deleted while a send was in flight
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0], 5
        )

    def test_prune_proceeds_when_no_send_in_flight(self):
        self._seed_old_llm_calls(7)
        out = retention.prune(self.conn, _settings(retention_days=90))
        self.assertEqual(out.get("llm_calls"), 7)

    def test_prune_never_touches_ledger_or_pending_or_memory(self):
        # Seed protected rows that are OLD enough to be deleted IF the policy were wrong.
        old = int(time.time()) - 365 * _DAY
        self.conn.execute(
            "INSERT INTO processed_messages (message_id) VALUES ('keep-me')"
        )
        self.conn.execute(
            "INSERT INTO pending_actions (idempotency_key, message_id, tier, kind, status) "
            "VALUES ('kp','p',2,'reply','PENDING')"
        )
        mem = RelationshipMemory(person_id=_PID)
        mem.summary["k"] = "v"
        distill.save_memory(self.conn, mem)
        self._seed_old_llm_calls(3)
        retention.prune(self.conn, _settings(retention_days=90))
        # All three protected stores survive.
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id='keep-me'"
            ).fetchone()
        )
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM pending_actions WHERE message_id='p'"
            ).fetchone()
        )
        self.assertIsNotNone(repo.relationship_memory_get(self.conn, _PID))

    def test_prune_forgets_expired_facts_end_to_end(self):
        # The daily prune path actually forgets a decayed+stale fact (memory-knowledge-2
        # write-side wired through retention).
        now = int(time.time())
        mem = RelationshipMemory(person_id=_PID)
        mem.summary["stale_job"] = "AcmeCorp"
        distill.save_memory(self.conn, mem)
        governance.observe_fact(self.conn, _PID, "stale_job", "AcmeCorp",
                                now=now - 300 * _DAY)
        self.conn.execute(
            "UPDATE fact_metadata SET confidence=0.02 WHERE person_id=? AND fact_key=?",
            (_PID, "stale_job"),
        )
        out = retention.prune(self.conn, _settings())
        self.assertEqual(out.get("memory_facts_forgotten"), 1)
        self.assertNotIn("stale_job", distill.load_memory(self.conn, _PID).summary)


if __name__ == "__main__":
    unittest.main()
