"""The exactly-once processed-message ledger.

State machine per message id:

    (absent) --mark_seen--> SEEN --claim--> PROCESSING --complete--> DONE
                                              │
                                              └----fail--------------> FAILED

Guarantees:
  * `mark_seen` is the dedup gate — INSERT OR IGNORE on the PRIMARY KEY means a
    message id is recorded at most once. A second sighting returns False.
  * `claim` atomically transitions SEEN/PROCESSING → PROCESSING; only the caller
    that flips the row owns it (single-writer; rowcount == 1).
  * "done" is set ONLY after the work is fully finished. A crash before that
    leaves the row PROCESSING; `recover_stale` re-queues it on next startup.
    Re-processing is safe because the autonomous pipeline performs only
    reversible/idempotent actions — the one irreversible action (sending a reply)
    happens exclusively through pending_actions on your explicit approval.

Stdlib only.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

# State constants
SEEN = "SEEN"
PROCESSING = "PROCESSING"
DONE = "DONE"
FAILED = "FAILED"


def mark_seen(conn: sqlite3.Connection, message_id: str, thread_id: str = "") -> bool:
    """Record a message id for the first time. Returns True iff newly inserted.

    This is the exactly-once gate: a message already in the ledger returns False
    and must not be processed again.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO processed_messages (message_id, thread_id, state) "
        "VALUES (?, ?, 'SEEN')",
        (message_id, thread_id),
    )
    return cur.rowcount == 1


def claim(conn: sqlite3.Connection, message_id: str) -> bool:
    """Atomically take ownership of a message for processing.

    Transitions SEEN or PROCESSING → PROCESSING and bumps the attempt counter.
    Returns True iff this call performed the transition (i.e. we own it now).
    DONE/FAILED rows cannot be claimed.
    """
    cur = conn.execute(
        "UPDATE processed_messages "
        "SET state='PROCESSING', attempts=attempts+1, updated_at=strftime('%s','now') "
        "WHERE message_id=? AND state IN ('SEEN','PROCESSING')",
        (message_id,),
    )
    return cur.rowcount == 1


def complete(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    tier: Optional[int] = None,
    category: Optional[str] = None,
    confidence: Optional[float] = None,
    dry_run: bool = True,
) -> None:
    """Mark a message fully handled. Call only after ALL side effects are done."""
    conn.execute(
        "UPDATE processed_messages "
        "SET state='DONE', tier=?, category=?, confidence=?, dry_run=?, "
        "    last_error=NULL, updated_at=strftime('%s','now') "
        "WHERE message_id=?",
        (tier, category, confidence, 1 if dry_run else 0, message_id),
    )


def fail(conn: sqlite3.Connection, message_id: str, error: str) -> None:
    """Mark a message failed. The caller MUST have surfaced the error to the human
    first (fail safe — never a silent failure)."""
    conn.execute(
        "UPDATE processed_messages "
        "SET state='FAILED', last_error=?, updated_at=strftime('%s','now') "
        "WHERE message_id=?",
        (error[:2000], message_id),
    )


def record_skipped(
    conn: sqlite3.Connection, message_id: str, thread_id: str = "", category: str = "group_skipped"
) -> None:
    """Record a message as handled-and-done in ONE atomic statement (never passing
    through a claimable SEEN state). Used for intake-time skips (e.g. group messages
    that don't mention the user) so a concurrent poller can never grab them in the
    window between 'seen' and 'done'."""
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages "
        "(message_id, thread_id, state, tier, category, confidence, dry_run) "
        "VALUES (?, ?, 'DONE', 0, ?, 1.0, 1)",
        (message_id, thread_id, category),
    )


def recover_stale(conn: sqlite3.Connection, max_attempts: int = 5) -> int:
    """Re-queue rows stuck in PROCESSING (e.g. crash mid-task).

    Rows that have already been attempted too many times are marked FAILED instead
    so a poison message can't loop forever. Returns the number re-queued.
    """
    # Park chronically-failing rows so they surface rather than spin.
    conn.execute(
        "UPDATE processed_messages "
        "SET state='FAILED', last_error='exceeded max attempts during recovery', "
        "    updated_at=strftime('%s','now') "
        "WHERE state='PROCESSING' AND attempts >= ?",
        (max_attempts,),
    )
    cur = conn.execute(
        "UPDATE processed_messages "
        "SET state='SEEN', updated_at=strftime('%s','now') "
        "WHERE state='PROCESSING'"
    )
    return cur.rowcount


def list_pending(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Messages that still need work, oldest first."""
    return list(
        conn.execute(
            "SELECT * FROM processed_messages "
            "WHERE state IN ('SEEN','PROCESSING') ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
    )


def get(conn: sqlite3.Connection, message_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM processed_messages WHERE message_id=?", (message_id,)
    )
    return cur.fetchone()


def is_done(conn: sqlite3.Connection, message_id: str) -> bool:
    row = get(conn, message_id)
    return bool(row and row["state"] == DONE)


def counts_since(conn: sqlite3.Connection, since_epoch: int) -> dict[str, int]:
    """Tier/state tallies for briefs."""
    out: dict[str, int] = {}
    for row in conn.execute(
        "SELECT state, COUNT(*) AS n FROM processed_messages "
        "WHERE updated_at >= ? GROUP BY state",
        (since_epoch,),
    ):
        out[row["state"]] = row["n"]
    return out
