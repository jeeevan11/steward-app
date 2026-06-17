#!/usr/bin/env python3
"""Phase 9 — performance profiling for Steward's scaling-critical paths.

Steward is a local-first SQLite app, so the operations that degrade over YEARS (the
scaling-time-* findings) are DB-bound: the live-queue render, the dashboard learning
reads, retention pruning, and the WhatsApp settle planner. This harness seeds each table
to a realistic multi-year volume and reports P50/P90/P99 latency, plus an index ON/OFF
comparison that proves the added indexes matter.

What it does NOT measure: real LLM/network latency (classification, drafting, transcription)
is dominated by the OpenRouter/Gmail round-trip and cannot be benchmarked offline. Those
stages are bounded instead by the LLM_COST_BOUNDED guard (daily cap + 429 breaker), not by
local compute. We measure the local compute they DO own (the settle planner) for reference.

Run:  .venv/bin/python scripts/profile_pipeline.py [--write]
Writes PERF_PROFILE.md when --write is passed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant.config import Settings
from assistant.storage import db, decision_log, ledger, metrics, read_queries as rq, retention
from assistant.storage import repositories as repo  # noqa: F401 (kept for ad-hoc probing)

N_PENDING = 10_000        # ~years of queued/handled decisions
N_EVENTS = 50_000         # ~years of approve/skip/edit learning events
N_RETENTION = 20_000      # old high-volume rows to prune in one pass
DAY = 86_400


def pct(samples_ms: list[float], q: float) -> float:
    s = sorted(samples_ms)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


def bench(fn, n: int) -> dict:
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000.0)
    return {
        "p50": pct(times, 0.50), "p90": pct(times, 0.90),
        "p99": pct(times, 0.99), "max": max(times), "n": n,
    }


def _seed(conn):
    decision_log.ensure(conn)   # on-demand tables (created lazily in production)
    metrics.ensure(conn)
    now = int(time.time())
    # pending_actions + a matching decision_log row (what get_queue reads).
    for i in range(N_PENDING):
        mid = f"m{i}"
        st = "SENT" if i < N_PENDING - 60 else "PENDING"   # ~60 live, rest history
        conn.execute(
            "INSERT INTO pending_actions (idempotency_key, message_id, thread_id, tier, "
            "kind, summary, draft_text, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"k{i}", mid, f"t{i}", 2 + (i % 2), "reply_draft", "s", "d", st, now - (i * 60)),
        )
        conn.execute(
            "INSERT INTO decision_log (message_id, thread_id, ts, sender_email, final_tier) "
            "VALUES (?,?,?,?,?)",
            (mid, f"t{i}", now - (i * 60), f"person{i % 500}@ex.com", 2),
        )
    # learning_events spanning ~2 years (exercises the ts/type range index).
    typ = ("approve", "skip", "edit", "surface")
    conn.executemany(
        "INSERT INTO learning_events (ts, type, message_id, contact_email) VALUES (?,?,?,?)",
        [(now - (i % (730 * DAY)), typ[i % 4], f"m{i}", f"person{i % 500}@ex.com")
         for i in range(N_EVENTS)],
    )
    # old high-volume rows for the retention prune bench.
    old = now - 200 * DAY
    conn.executemany("INSERT INTO audit_log (ts, kind, summary, undone) VALUES (?,?,?,0)",
                     [(old, "surface", "x") for _ in range(N_RETENTION)])
    conn.executemany("INSERT INTO llm_calls (ts, task, model) VALUES (?,?,?)",
                     [(old, "JUDGE", "m") for _ in range(N_RETENTION)])


def _settle_rows(n):
    now = int(time.time())
    return [{"message_id": f"w{i}", "jid": f"{i % 200}@s.whatsapp.net",
             "is_group": 0, "ts": now - i, "created_at": now - i} for i in range(n)]


def main() -> int:
    write = "--write" in sys.argv
    path = os.path.join(tempfile.mkdtemp(), "perf.db")
    conn = db.open_db(path)
    print(f"seeding: {N_PENDING:,} pending_actions, {N_EVENTS:,} learning_events, "
          f"{N_RETENTION * 2:,} prunable rows ...")
    t0 = time.perf_counter()
    _seed(conn)
    conn.commit()
    print(f"seeded in {time.perf_counter() - t0:.1f}s\n")

    rq.ensure_perf_indexes(conn)
    from assistant.ingest.whatsapp_source import plan_settling

    rows = []  # (label, scale, result)

    rows.append(("live-queue render  get_queue()", f"{N_PENDING:,} rows", bench(lambda: rq.get_queue(conn), 200)))
    rows.append(("dashboard  learning_summary()", f"{N_EVENTS:,} events", bench(lambda: rq.learning_summary(conn), 100)))
    rows.append(("dashboard  metrics_accuracy()", f"{N_EVENTS:,} events", bench(lambda: rq.metrics_accuracy(conn), 100)))
    rows.append(("settle planner  plan_settling()", "1,000 msgs", bench(
        lambda: plan_settling(_settle_rows(1000), now=int(time.time()), settle=75, max_hold=900,
                              group_settle=300, group_max_hold=3600), 200)))
    rows.append(("ledger  mark_seen() (dedup gate)", "single", bench(
        lambda: ledger.mark_seen(conn, f"probe{time.perf_counter_ns()}"), 2000)))

    # Index ON/OFF proof for the live-queue per-message lookup (scaling-time-1).
    on = bench(lambda: rq.get_queue(conn), 100)
    conn.execute("DROP INDEX IF EXISTS idx_pa_message_id")
    conn.execute("DROP INDEX IF EXISTS idx_learning_events_ts_type")
    off = bench(lambda: rq.get_queue(conn), 100)
    rq.ensure_perf_indexes(conn)  # restore

    # Retention prune (storage-persistence-7 batching / scaling-time-5 checkpoint).
    prune_res = bench(lambda: retention.prune(conn, Settings()), 1)

    def fmt(r):
        return f"{r['p50']:.2f} / {r['p90']:.2f} / {r['p99']:.2f} / {r['max']:.2f}"

    lines = []
    lines.append("# PERF_PROFILE.md\n")
    lines.append("Phase 9 performance profile of Steward's scaling-critical, DB-bound paths, "
                 "seeded to a realistic multi-year volume on a WAL SQLite file. Latencies in "
                 "milliseconds. Generated by `scripts/profile_pipeline.py`.\n")
    lines.append(f"**Scale:** {N_PENDING:,} pending_actions, {N_EVENTS:,} learning_events, "
                 f"{N_RETENTION * 2:,} prunable rows.\n")
    lines.append("| Operation | Scale | P50 | P90 | P99 | Max |")
    lines.append("|---|---|---|---|---|---|")
    for label, scale, r in rows:
        lines.append(f"| {label} | {scale} | {r['p50']:.2f} | {r['p90']:.2f} | {r['p99']:.2f} | {r['max']:.2f} |")
    lines.append(f"| retention.prune() (one pass) | {N_RETENTION * 2:,} del | {prune_res['p50']:.1f} | — | — | {prune_res['max']:.1f} |")
    lines.append("")
    lines.append("## Index ON/OFF — live-queue render at scale (scaling-time-1/3)\n")
    lines.append("| idx_pa_message_id + idx_learning_events_ts_type | P50 | P90 | P99 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| WITH indexes | {on['p50']:.2f} | {on['p90']:.2f} | {on['p99']:.2f} |")
    lines.append(f"| WITHOUT (pre-fix) | {off['p50']:.2f} | {off['p90']:.2f} | {off['p99']:.2f} |")
    speedup = (off['p50'] / on['p50']) if on['p50'] else 1.0
    lines.append(f"\n**Speedup from the added indexes: ~{speedup:.1f}x at P50** "
                 f"({off['p50']:.2f}ms → {on['p50']:.2f}ms).\n")
    lines.append("## Notes\n")
    lines.append("- LLM/network stages (classification, drafting, transcription) are network-bound "
                 "and not benchmarkable offline; they are governed by LLM_COST_BOUNDED (daily cap + "
                 "429 breaker + media byte cap), verified in `test_llm_layer_hardening`.")
    lines.append("- `plan_settling` is the only material local compute on the WhatsApp path and is "
                 "sub-millisecond at 1,000 queued messages.")
    lines.append("- retention now batches deletes and checkpoints the WAL (storage-persistence-7 / "
                 "scaling-time-5), so a large first prune no longer takes one giant lock.")

    report = "\n".join(lines)
    print("\n" + report)
    if write:
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "PERF_PROFILE.md")
        open(out, "w").write(report + "\n")
        print(f"\nwrote {out}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
