"""Daily metrics + per-call LLM cost log. ADDITIVE: own tables via CREATE IF NOT
EXISTS; never read during classification (write-only on the hot path). Stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from assistant.logging_setup import get_logger

log = get_logger("metrics")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics_daily (
    day TEXT PRIMARY KEY,                 -- YYYY-MM-DD (local)
    emails_processed INTEGER NOT NULL DEFAULT 0,
    auto_handled INTEGER NOT NULL DEFAULT 0,
    surfaced INTEGER NOT NULL DEFAULT 0,
    approved_no_edit INTEGER NOT NULL DEFAULT 0,
    approved_with_edit INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    false_archive_candidates INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    task TEXT, model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    message_id TEXT,
    fallback INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);
-- ── Response-time samples (P0e) — latency is a product metric ─────────────────
-- kind in {email_to_notification, tap_to_confirmation, draft_generation}.
CREATE TABLE IF NOT EXISTS response_times (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    kind TEXT NOT NULL,
    ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_response_times ON response_times(kind, ts);
-- ── Pre-aggregated metrics (P6) — nightly snapshot so the dashboard reads are O(1) ─
CREATE TABLE IF NOT EXISTS metrics_cache (
    cache_key   TEXT PRIMARY KEY,
    data_json   TEXT,
    computed_at TEXT
);
"""

# The response-time sample kinds we track.
RT_EMAIL_TO_NOTIFICATION = "email_to_notification"
RT_TAP_TO_CONFIRMATION = "tap_to_confirmation"
RT_DRAFT_GENERATION = "draft_generation"

_COUNTERS = {
    "emails_processed", "auto_handled", "surfaced", "approved_no_edit",
    "approved_with_edit", "skipped", "false_archive_candidates",
}


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _today(day: Optional[str]) -> str:
    if day:
        return day
    return time.strftime("%Y-%m-%d", time.localtime())


def bump(conn: sqlite3.Connection, field: str, n: int = 1, *, day: Optional[str] = None) -> None:
    """Increment a daily counter. Unknown field names are ignored (best-effort)."""
    if field not in _COUNTERS:
        log.debug("metrics.bump: unknown field %s", field)
        return
    try:
        ensure(conn)
        d = _today(day)
        conn.execute("INSERT OR IGNORE INTO metrics_daily (day) VALUES (?)", (d,))
        conn.execute(f"UPDATE metrics_daily SET {field} = {field} + ? WHERE day=?", (n, d))
    except Exception as exc:  # noqa: BLE001 - metrics must never break the pipeline
        log.debug("metrics.bump failed: %s", exc)


def record_llm_call(
    conn: sqlite3.Connection,
    *,
    task: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
    message_id: str = "",
    fallback: bool = False,
) -> None:
    try:
        ensure(conn)
        conn.execute(
            "INSERT INTO llm_calls (task, model, prompt_tokens, completion_tokens, cost, "
            " message_id, fallback) VALUES (?,?,?,?,?,?,?)",
            (task, model, prompt_tokens, completion_tokens, cost, message_id, 1 if fallback else 0),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("metrics.record_llm_call failed: %s", exc)


def make_sink(db_path: str):
    """Return a callable the LLMClient invokes after each call to log task/model/
    tokens/cost. It opens a short-lived connection (the client has no connection of
    its own) and writes one llm_calls row. Best-effort: never raises into the client.
    """
    from assistant.storage import db

    def sink(record: dict[str, Any]) -> None:
        try:
            conn = db.connect(db_path)
            try:
                record_llm_call(
                    conn,
                    task=record.get("task", ""),
                    model=record.get("model", ""),
                    prompt_tokens=int(record.get("prompt_tokens", 0) or 0),
                    completion_tokens=int(record.get("completion_tokens", 0) or 0),
                    cost=float(record.get("cost", 0.0) or 0.0),
                    message_id=record.get("message_id", "") or "",
                    fallback=bool(record.get("fallback", False)),
                )
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("metrics sink failed: %s", exc)

    return sink


def record_response_time(conn: sqlite3.Connection, kind: str, ms: int) -> None:
    """Log one latency sample (milliseconds). Best-effort; never raises into the
    caller — latency logging must never break the path it is measuring."""
    try:
        if ms < 0:
            ms = 0
        ensure(conn)
        conn.execute(
            "INSERT INTO response_times (kind, ms) VALUES (?, ?)", (kind, int(ms))
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("metrics.record_response_time failed: %s", exc)


def response_percentiles(
    conn: sqlite3.Connection, kind: str, since_epoch: int = 0
) -> dict[str, int]:
    """p50/p95 (and count) for one response-time kind. Pure-Python percentile so
    it works on any SQLite. Returns zeros when there are no samples."""
    try:
        ensure(conn)
        rows = list(conn.execute(
            "SELECT ms FROM response_times WHERE kind=? AND ts >= ? ORDER BY ms ASC",
            (kind, since_epoch),
        ))
    except Exception as exc:  # noqa: BLE001
        log.debug("metrics.response_percentiles failed: %s", exc)
        rows = []
    vals = [int(r["ms"]) for r in rows]
    n = len(vals)
    if n == 0:
        return {"p50": 0, "p95": 0, "count": 0}

    def _pct(p: float) -> int:
        # nearest-rank percentile
        idx = max(0, min(n - 1, int(round(p / 100.0 * (n - 1)))))
        return vals[idx]

    return {"p50": _pct(50), "p95": _pct(95), "count": n}


# ── read side (dashboard; never called during classification) ────────────────
def costs_by_task(conn: sqlite3.Connection, since_epoch: int) -> list[sqlite3.Row]:
    ensure(conn)
    return list(conn.execute(
        "SELECT task, model, COUNT(*) AS calls, SUM(cost) AS cost, "
        " SUM(prompt_tokens+completion_tokens) AS tokens "
        "FROM llm_calls WHERE ts >= ? GROUP BY task, model ORDER BY cost DESC",
        (since_epoch,),
    ))


def daily(conn: sqlite3.Connection, days: int = 14) -> list[sqlite3.Row]:
    ensure(conn)
    return list(conn.execute(
        "SELECT * FROM metrics_daily ORDER BY day DESC LIMIT ?", (days,)
    ))


# ── pre-aggregation cache (P6) ───────────────────────────────────────────────
def cache_set(conn: sqlite3.Connection, key: str, obj: Any) -> None:
    try:
        ensure(conn)
        conn.execute(
            "INSERT INTO metrics_cache (cache_key, data_json, computed_at) "
            "VALUES (?,?,strftime('%s','now')) "
            "ON CONFLICT(cache_key) DO UPDATE SET data_json=excluded.data_json, "
            " computed_at=excluded.computed_at",
            (key, json.dumps(obj)),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("metrics.cache_set failed: %s", exc)


def cache_get(conn: sqlite3.Connection, key: str,
              max_age_seconds: Optional[int] = None) -> Optional[Any]:
    """Read a cached aggregate. When max_age_seconds is given, a snapshot older than that is
    treated as a MISS (returns None) so the caller recomputes live — `computed_at` was always
    written but never read, which let the dashboard serve a day-stale snapshot as if current."""
    try:
        ensure(conn)
        row = conn.execute(
            "SELECT data_json, computed_at FROM metrics_cache WHERE cache_key=?", (key,)
        ).fetchone()
        if not row or not row["data_json"]:
            return None
        if max_age_seconds is not None:
            try:
                age = int(time.time()) - int(row["computed_at"] or 0)
                if age > int(max_age_seconds):
                    return None   # stale → caller recomputes + backfills
            except (TypeError, ValueError):
                pass
        return json.loads(row["data_json"])
    except Exception as exc:  # noqa: BLE001
        log.debug("metrics.cache_get failed: %s", exc)
        return None


def populate(conn: sqlite3.Connection) -> None:
    """Recompute the dashboard metric aggregates and store them in metrics_cache so
    the /metrics endpoints are O(1). Run nightly (and lazily on a cache miss)."""
    from assistant.storage import read_queries as rq

    cache_set(conn, "daily", rq.metrics_daily_breakdown(conn, 30))
    cache_set(conn, "accuracy", rq.metrics_accuracy(conn, 30))
    cache_set(conn, "costs", rq.metrics_costs(conn, 30))
    cache_set(conn, "response_times", rq.metrics_response_times(conn))
