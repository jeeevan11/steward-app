"""Phase 11 — retention / pruning.

The audit found the high-volume append-only tables (llm_calls, response_times, audit_log,
decision_log, decision_explanations, wa_messages, processed whatsapp_inbox rows) have NO
pruning and grow unbounded on a daily-use local device. This trims them to configurable
windows.

SAFETY: deliberately does NOT touch the exactly-once ledger (`processed_messages`) — keeping
every message id forever preserves the dedup guarantee — nor `pending_actions`,
`commitments`, `relationship_memory`, `rules`, `contacts`, `persons`, `learning_events`
(small + valuable for calibration). Only old, already-processed, high-volume rows are removed.
Best-effort; never raises into the poller.
"""

from __future__ import annotations

import os
import sqlite3
import time

from assistant.config import Settings
from assistant.logging_setup import get_logger

log = get_logger("retention")

# storage-persistence-7: bound the write-lock hold per statement. The first prune on a
# years-old backlog used to DELETE several hundred thousand rows in ONE unbatched
# autocommit statement, holding the exclusive WAL write lock for seconds and making a
# concurrent human Approve (begin_send is a writer) hit 'database is locked'. We instead
# delete in bounded chunks so the send path can interleave and grab the lock between
# chunks. Overridable via env (no new config knob on the shared config.py).
_DEFAULT_DELETE_CHUNK = 5000


def _delete_chunk_size() -> int:
    """Per-statement delete budget. Read from env with a safe default; clamped to a sane
    floor so a misconfiguration can never make this a no-op or a single giant delete."""
    try:
        n = int(os.environ.get("RETENTION_DELETE_CHUNK", _DEFAULT_DELETE_CHUNK))
    except (TypeError, ValueError):
        return _DEFAULT_DELETE_CHUNK
    return max(100, min(n, 50000))


def _send_in_flight(conn: sqlite3.Connection) -> bool:
    """storage-persistence-7: True if any action is mid-send (status SENDING). Retention
    yields to the latency-sensitive human-approval send path: when a send is in flight we
    skip the prune this tick (the day-key gate in the caller retries tomorrow, and the
    backlog only shrinks). Best-effort: any error -> treat as 'not in flight' so retention
    is never permanently blocked by a probe failure."""
    try:
        row = conn.execute(
            "SELECT 1 FROM pending_actions WHERE status='SENDING' LIMIT 1"
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _batched_delete(
    conn: sqlite3.Connection, table: str, where: str, params: tuple, *, chunk: int
) -> int:
    """storage-persistence-7: DELETE rows matching `where` in bounded `chunk`-sized
    statements until none remain, committing between chunks so the write lock is released
    and a concurrent send can interleave. Portable LIMIT-by-rowid (no compile-time
    SQLITE_ENABLE_UPDATE_DELETE_LIMIT dependency). Returns total rows deleted.

    Each chunk is its own transaction: on any error we stop this table and return what was
    already deleted (committed), so a mid-loop failure never rolls back progress or blocks
    other tables. Never raises."""
    total = 0
    sql = (
        f"DELETE FROM {table} WHERE rowid IN "
        f"(SELECT rowid FROM {table} WHERE {where} LIMIT ?)"
    )
    while True:
        try:
            cur = conn.execute(sql, params + (chunk,))
            n = cur.rowcount or 0
            # Commit each chunk so the exclusive write lock is dropped between chunks,
            # giving begin_send a window to acquire it.
            try:
                conn.commit()
            except sqlite3.Error:
                pass
            total += max(0, n)
            if n < chunk:
                break
        except sqlite3.Error as exc:
            log.debug("retention: chunked delete on %s stopped (%s)", table, exc)
            break
    return total


def prune(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    """Delete rows older than the configured windows, in bounded chunks. Returns
    {table: rows_deleted}. Best-effort; never raises into the poller.

    NEVER prunes the exactly-once ledger (processed_messages), pending_actions,
    commitments, relationship_memory, fact_metadata, rules, contacts, persons, or
    learning_events — those guarantees are preserved (only old, already-processed,
    high-volume rows are removed)."""
    if not getattr(settings, "retention_enabled", True):
        return {}
    # storage-persistence-7: do not contend with an in-flight human-approved send.
    if _send_in_flight(conn):
        log.debug("retention: skipped (a send is in flight; will retry next cycle)")
        return {}
    now = int(time.time())
    days = max(1, int(getattr(settings, "retention_days", 90)))
    wa_days = max(1, int(getattr(settings, "retention_wa_history_days", 30)))
    cutoff = now - days * 86400
    wa_cutoff = now - wa_days * 86400
    chunk = _delete_chunk_size()

    # (table, where-clause, params) — each guarded so one failure doesn't block the rest.
    jobs: list[tuple[str, str, tuple]] = [
        ("llm_calls", "ts < ?", (cutoff,)),
        ("response_times", "ts < ?", (cutoff,)),
        ("audit_log", "ts < ? AND undone = 0", (cutoff,)),
        ("decision_log", "ts < ?", (cutoff,)),
        ("decision_explanations", "ts < ?", (cutoff,)),
        ("wa_messages", "ts < ?", (wa_cutoff,)),
        # processed/skipped/folded WhatsApp inbox rows (media already cleared); keep 'new'.
        ("whatsapp_inbox", "status IN ('queued','folded','skipped') AND created_at < ?", (wa_cutoff,)),
    ]
    deleted: dict[str, int] = {}
    for table, where, params in jobs:
        n = _batched_delete(conn, table, where, params, chunk=chunk)
        if n > 0:
            deleted[table] = n

    # GAP 6 — commitment staleness. Commitments are never DELETED (they're small + valuable),
    # but open ones long past their due date — or undated and very old — are marked 'stale'
    # so they stop being surfaced as live obligations.
    staled = mark_stale_commitments(conn)
    if staled:
        deleted["commitments_staled"] = staled

    # memory-knowledge-2: actually FORGET decayed-and-stale facts. The relationship_memory
    # ROW is never deleted (the guarantee holds) — only individual expired KEYS within a
    # person's summary are removed, plus their stranded fact_metadata rows. This is the
    # read-path consequence of decay: a faded fact stops being injected into prompts.
    forgotten = forget_expired_facts(conn)
    if forgotten:
        deleted["memory_facts_forgotten"] = forgotten

    if deleted:
        log.info("retention pruned: %s", deleted)

    # scaling-time-5: reclaim disk + truncate the WAL after the deletes. Without this the
    # freed pages stay on the free list (file never shrinks) and the WAL keeps growing.
    reclaim_disk(conn)
    return deleted


def reclaim_disk(conn: sqlite3.Connection) -> None:
    """scaling-time-5: return freed pages to the OS and bound the WAL file.

    ROOT CAUSE: retention DELETEd rows but nothing ever VACUUMed or checkpointed, so the
    on-disk DB stayed pinned at its all-time high-water mark and the -wal sidecar grew
    unbounded under sustained writes — defeating the purpose of retention.

    Two best-effort steps, each independently guarded so one failing never blocks the
    other and neither raises into the poller:
      1. PRAGMA incremental_vacuum — returns free-list pages to the OS WHEN the DB was
         created with auto_vacuum=INCREMENTAL. On a default (auto_vacuum=NONE) DB this is
         a harmless no-op; switching an existing DB to incremental needs a one-time full
         VACUUM, which is an integrator decision on db.py (see schema_or_config_needed) —
         we do not run a blanket full VACUUM here because on a large DB it rewrites the
         whole file under a lock, the exact contention storage-persistence-7 avoids.
      2. PRAGMA wal_checkpoint(TRUNCATE) — flushes committed WAL frames into the DB and
         truncates the -wal file back to zero, bounding its growth.
    """
    try:
        conn.execute("PRAGMA incremental_vacuum")
    except sqlite3.Error as exc:
        log.debug("retention: incremental_vacuum skipped (%s)", exc)
    try:
        # Commit any open state first so the checkpoint can flush a clean WAL.
        try:
            conn.commit()
        except sqlite3.Error:
            pass
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        log.debug("retention: wal_checkpoint skipped (%s)", exc)


def forget_expired_facts(conn: sqlite3.Connection) -> int:
    """memory-knowledge-2: forget decayed-AND-stale facts during the daily maintenance pass.

    Thin wrapper over governance.forget_expired_facts that injects repo.record_event so the
    forget is observable in the learning-events log (a silent forget is exactly what the
    audit flagged). Best-effort: any failure (e.g. governance unavailable) -> 0, never
    raises. NEVER deletes a relationship_memory row, a person, a contact, a rule, or the
    ledger — only individual expired fact KEYS within a person's summary."""
    try:
        from assistant.memory import governance
        from assistant.storage import repositories as repo
        return governance.forget_expired_facts(conn, record_event=repo.record_event)
    except Exception as exc:  # noqa: BLE001 - additive; never break retention
        log.debug("retention: forget_expired_facts skipped (%s)", exc)
        return 0


def mark_stale_commitments(conn: sqlite3.Connection) -> int:
    """GAP 6 — mark open commitments stale: a due date more than 90 days past, or no due
    date and created more than 180 days ago. Returns the number marked. Best-effort."""
    total = 0
    try:
        cur = conn.execute(
            "UPDATE commitments SET status='stale' "
            "WHERE status='open' AND due_date != '' AND due_date < date('now', '-90 days')"
        )
        total += cur.rowcount or 0
        cur = conn.execute(
            "UPDATE commitments SET status='stale' "
            "WHERE status='open' AND due_date = '' "
            "  AND created_at < unixepoch('now') - 15552000"  # 180 days in seconds
        )
        total += cur.rowcount or 0
    except sqlite3.Error as exc:
        log.debug("retention: commitment staleness skipped (%s)", exc)
    return total
