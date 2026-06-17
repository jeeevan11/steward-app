"""Persisted record of every brain decision — powers the web console's detail
view and the "nearly filed but looked important" stat.

This is ADDITIVE: the core computes a Decision/FinalDecision per email and used to
discard everything but tier/category/confidence. This module stores the full
picture (sender/subject/snippet + reasoning/stakes/reversibility + base vs final
tier + why-it-was-surfaced) in its own table so a second front-end can explain a
decision in plain English without re-fetching mail or re-running the model.

`record()` is best-effort and NEVER raises — recording must not break processing.
Stdlib only.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

from assistant.logging_setup import get_logger
from assistant.models import Decision, FinalDecision, Message, Thread

log = get_logger("decision_log")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_log (
    message_id      TEXT PRIMARY KEY,
    thread_id       TEXT,
    ts              INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    sender_email    TEXT,
    sender_name     TEXT,
    subject         TEXT,
    snippet         TEXT,
    category        TEXT,
    stakes          TEXT,
    reversibility   TEXT,
    confidence      REAL,
    reasoning       TEXT,
    needs_reply     INTEGER,
    base_tier       INTEGER,
    final_tier      INTEGER,
    surfaced_reason TEXT,
    dry_run         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_decisionlog_ts ON decision_log(ts);
CREATE INDEX IF NOT EXISTS idx_decisionlog_final ON decision_log(final_tier);
"""


# Additive columns introduced by later phases. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so we check pragma table_info and ALTER what's missing (idempotent).
_EXTRA_COLUMNS = [
    ("think_output", "TEXT"),            # P3 step 1 (prep)
    ("judge_output", "TEXT"),            # P3 step 2 (the decision JSON)
    ("critique_output", "TEXT"),         # P3 step 3 (self-critique JSON)
    ("critique_adjustment", "INTEGER DEFAULT 0"),
    ("was_critical", "INTEGER DEFAULT 0"),
    ("quality_gate_result", "TEXT"),     # P5b
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    have = {row["name"] for row in conn.execute("PRAGMA table_info(decision_log)")}
    for col, decl in _EXTRA_COLUMNS:
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE decision_log ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # added concurrently / already exists


def ensure(conn: sqlite3.Connection) -> None:
    """Create the table if it doesn't exist + add any new columns (idempotent)."""
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)


def record_reasoning(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    thread_id: str = "",
    think_output: str = "",
    judge_output: str = "",
    critique_output: str = "",
    critique_adjustment: int = 0,
    was_critical: bool = False,
) -> None:
    """Persist the three reasoning steps for a message (P3). Best-effort and only
    touches the reasoning columns, so it composes with record() (which writes the
    decision columns) regardless of call order. NEVER raises."""
    if not message_id:
        return
    try:
        ensure(conn)
        conn.execute(
            "INSERT INTO decision_log (message_id, thread_id, think_output, judge_output, "
            " critique_output, critique_adjustment, was_critical) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            " think_output=excluded.think_output, judge_output=excluded.judge_output, "
            " critique_output=excluded.critique_output, "
            " critique_adjustment=excluded.critique_adjustment, "
            " was_critical=excluded.was_critical",
            (message_id, thread_id, think_output, judge_output, critique_output,
             int(critique_adjustment), 1 if was_critical else 0),
        )
    except Exception as exc:  # noqa: BLE001 - logging must never break processing
        log.warning("decision_log.record_reasoning failed (non-fatal): %s", exc)


def record(
    conn: sqlite3.Connection,
    *,
    message: Message,
    thread: Thread,
    decision: Decision,
    final: FinalDecision,
    dry_run: bool,
) -> None:
    """Persist one decision. Best-effort: logs and returns on any error."""
    try:
        ensure(conn)
        body = (message.body_text or message.snippet or "").strip()
        snippet = body[:800]
        conn.execute(
            "INSERT INTO decision_log (message_id, thread_id, ts, sender_email, "
            " sender_name, subject, snippet, category, stakes, reversibility, "
            " confidence, reasoning, needs_reply, base_tier, final_tier, "
            " surfaced_reason, dry_run) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            " ts=excluded.ts, sender_email=excluded.sender_email, "
            " sender_name=excluded.sender_name, subject=excluded.subject, "
            " snippet=excluded.snippet, category=excluded.category, stakes=excluded.stakes, "
            " reversibility=excluded.reversibility, confidence=excluded.confidence, "
            " reasoning=excluded.reasoning, needs_reply=excluded.needs_reply, "
            " base_tier=excluded.base_tier, final_tier=excluded.final_tier, "
            " surfaced_reason=excluded.surfaced_reason, dry_run=excluded.dry_run",
            (
                message.id,
                thread.id,
                int(message.timestamp) if message.timestamp else int(time.time()),
                (message.sender_email or "").lower(),
                message.sender_name or "",
                message.subject or thread.subject or "",
                snippet,
                decision.category,
                decision.stakes,
                decision.reversibility,
                float(decision.confidence),
                decision.reasoning or "",
                1 if decision.needs_reply else 0,
                int(final.base_tier),
                int(final.final_tier),
                final.surfaced_reason or "",
                1 if dry_run else 0,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - recording must never break processing
        log.warning("decision_log.record failed (non-fatal): %s", exc)


def set_quality_gate(conn: sqlite3.Connection, message_id: str, result_json: str) -> None:
    """Store the P5b quality-gate result for a message (best-effort, never raises)."""
    if not message_id:
        return
    try:
        ensure(conn)
        conn.execute(
            "INSERT INTO decision_log (message_id, quality_gate_result) VALUES (?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET quality_gate_result=excluded.quality_gate_result",
            (message_id, result_json),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("decision_log.set_quality_gate failed (non-fatal): %s", exc)


def get(conn: sqlite3.Connection, message_id: str) -> Optional[sqlite3.Row]:
    ensure(conn)
    return conn.execute(
        "SELECT * FROM decision_log WHERE message_id=?", (message_id,)
    ).fetchone()


def recent(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    ensure(conn)
    return list(
        conn.execute(
            "SELECT * FROM decision_log ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
        )
    )


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Headline counts for the console."""
    ensure(conn)
    handled = conn.execute(
        "SELECT COUNT(*) AS n FROM decision_log WHERE final_tier IN (0,1)"
    ).fetchone()["n"]
    flagged = conn.execute(
        "SELECT COUNT(*) AS n FROM decision_log WHERE final_tier IN (2,3)"
    ).fetchone()["n"]
    # "nearly filed but looked important": the brain wanted to auto-handle (base 0/1)
    # but a guardrail/VIP/confidence rule pushed it up to the human (final 2/3).
    near_misses = conn.execute(
        "SELECT COUNT(*) AS n FROM decision_log WHERE base_tier IN (0,1) AND final_tier IN (2,3)"
    ).fetchone()["n"]
    return {"handled_quietly": handled, "flagged_for_you": flagged, "near_misses": near_misses}
