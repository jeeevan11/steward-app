"""Operating State spine: four SQLite tables (threads, projects, opportunities, risks)
with CRUD functions.

All functions accept an open sqlite3.Connection as their first argument; they never
open their own connection. WAL mode and isolation_level=None (autocommit) are assumed
to be set by the caller (see assistant/storage/db.py). Every DB call is wrapped in
try/except; on failure the error is logged with print() and an empty/None value is
returned. This is a best-effort side store: the main pipeline must survive any failure.

Stdlib only -- no external dependencies.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row, cursor: sqlite3.Cursor) -> dict:
    """Convert a sqlite3.Row to a plain dict using cursor.description."""
    if row is None:
        return {}
    if cursor.description is None:
        return dict(row)
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _rows_to_dicts(rows: list[sqlite3.Row], cursor: sqlite3.Cursor) -> list[dict]:
    return [_row_to_dict(r, cursor) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id           TEXT PRIMARY KEY,
    channel             TEXT,
    status              TEXT,
    person_id           TEXT,
    subject             TEXT,
    project_id          TEXT,
    last_activity_ts    INTEGER,
    created_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_threads_status     ON threads(status);
CREATE INDEX IF NOT EXISTS idx_threads_project_id ON threads(project_id);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    status      TEXT DEFAULT 'active',
    description TEXT,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS opportunities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id           TEXT,
    type                TEXT,
    stage               TEXT,
    value_est           REAL DEFAULT 0.0,
    probability         REAL DEFAULT 0.0,
    next_action         TEXT,
    last_activity_ts    INTEGER,
    created_at          INTEGER
);

CREATE TABLE IF NOT EXISTS risks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT,
    description TEXT,
    severity    TEXT,
    thread_id   TEXT,
    resolved_at INTEGER,
    created_at  INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS threads_fts
    USING fts5(thread_id UNINDEXED, subject, person_id,
               content='threads', content_rowid='rowid');
"""


def ensure_tables(db: sqlite3.Connection) -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS for all tables and indexes."""
    try:
        db.executescript(_SCHEMA)
    except Exception as exc:
        print(f"[operating_state] ensure_tables failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Threads
# ─────────────────────────────────────────────────────────────────────────────

def upsert_thread(
    db: sqlite3.Connection,
    thread_id: str,
    channel: str,
    status: str,
    person_id: Optional[str] = None,
    subject: Optional[str] = None,
    project_id: Optional[str] = None,
) -> None:
    """INSERT OR REPLACE a thread row, then sync the FTS index."""
    now = int(time.time())
    try:
        db.execute(
            "INSERT OR REPLACE INTO threads "
            "(thread_id, channel, status, person_id, subject, project_id, "
            " last_activity_ts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (thread_id, channel, status, person_id, subject, project_id, now, now),
        )
    except Exception as exc:
        print(f"[operating_state] upsert_thread insert failed: {exc}")
        return
    try:
        db.execute(
            "INSERT OR REPLACE INTO threads_fts (rowid, thread_id, subject, person_id) "
            "SELECT rowid, thread_id, subject, person_id FROM threads WHERE thread_id=?",
            (thread_id,),
        )
    except Exception as exc:
        print(f"[operating_state] upsert_thread fts sync failed: {exc}")


def get_thread_status(db: sqlite3.Connection, thread_id: str) -> Optional[str]:
    """Return the status string for the given thread, or None if not found."""
    try:
        row = db.execute(
            "SELECT status FROM threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        print(f"[operating_state] get_thread_status failed: {exc}")
        return None


def update_thread_status(db: sqlite3.Connection, thread_id: str, status: str) -> None:
    """Update the status and last_activity_ts of a thread."""
    now = int(time.time())
    try:
        db.execute(
            "UPDATE threads SET status=?, last_activity_ts=? WHERE thread_id=?",
            (status, now, thread_id),
        )
    except Exception as exc:
        print(f"[operating_state] update_thread_status failed: {exc}")


def get_threads_by_status(
    db: sqlite3.Connection, status: str, limit: int = 50
) -> list[dict]:
    """Return threads with the given status, sorted by last_activity_ts ASC."""
    try:
        cur = db.execute(
            "SELECT * FROM threads WHERE status=? ORDER BY last_activity_ts ASC LIMIT ?",
            (status, limit),
        )
        rows = cur.fetchall()
        return _rows_to_dicts(rows, cur)
    except Exception as exc:
        print(f"[operating_state] get_threads_by_status failed: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Projects
# ─────────────────────────────────────────────────────────────────────────────

def upsert_project(
    db: sqlite3.Connection,
    name: str,
    status: str = "active",
    description: Optional[str] = None,
) -> Optional[int]:
    """Insert or update a project by name. Returns the project id, or None on error."""
    now = int(time.time())
    try:
        cur = db.execute(
            "INSERT INTO projects (name, status, description, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET status=excluded.status, "
            "description=excluded.description",
            (name, status, description, now),
        )
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = db.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()
        return int(row[0]) if row else None
    except Exception as exc:
        print(f"[operating_state] upsert_project failed: {exc}")
        return None


def get_project(db: sqlite3.Connection, name: str) -> Optional[dict]:
    """Return a project row as a dict, or None if not found."""
    try:
        cur = db.execute("SELECT * FROM projects WHERE name=?", (name,))
        row = cur.fetchone()
        return _row_to_dict(row, cur) if row else None
    except Exception as exc:
        print(f"[operating_state] get_project failed: {exc}")
        return None


def get_all_projects(db: sqlite3.Connection) -> list[dict]:
    """Return all project rows."""
    try:
        cur = db.execute("SELECT * FROM projects ORDER BY id")
        return _rows_to_dicts(cur.fetchall(), cur)
    except Exception as exc:
        print(f"[operating_state] get_all_projects failed: {exc}")
        return []


def update_project_status(db: sqlite3.Connection, name: str, status: str) -> None:
    """Update the status of a project identified by name."""
    try:
        db.execute("UPDATE projects SET status=? WHERE name=?", (status, name))
    except Exception as exc:
        print(f"[operating_state] update_project_status failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Opportunities
# ─────────────────────────────────────────────────────────────────────────────

def create_opportunity(
    db: sqlite3.Connection,
    person_id: str,
    type: str,
    stage: str = "identified",
    value_est: float = 0.0,
    probability: float = 0.0,
    next_action: Optional[str] = None,
) -> Optional[int]:
    """Insert a new opportunity row. Returns the new id, or None on error."""
    now = int(time.time())
    try:
        cur = db.execute(
            "INSERT INTO opportunities "
            "(person_id, type, stage, value_est, probability, next_action, "
            " last_activity_ts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (person_id, type, stage, value_est, probability, next_action, now, now),
        )
        return int(cur.lastrowid) if cur.lastrowid else None
    except Exception as exc:
        print(f"[operating_state] create_opportunity failed: {exc}")
        return None


def update_opportunity(db: sqlite3.Connection, opp_id: int, **kwargs: Any) -> None:
    """UPDATE SET for any provided keyword arguments on the given opportunity."""
    if not kwargs:
        return
    allowed = {
        "person_id", "type", "stage", "value_est", "probability",
        "next_action", "last_activity_ts",
    }
    sets, params = [], []
    for col, val in kwargs.items():
        if col in allowed:
            sets.append(f"{col}=?")
            params.append(val)
    if not sets:
        return
    params.append(opp_id)
    try:
        db.execute(f"UPDATE opportunities SET {', '.join(sets)} WHERE id=?", params)
    except Exception as exc:
        print(f"[operating_state] update_opportunity failed: {exc}")


def get_opportunities(
    db: sqlite3.Connection,
    type: Optional[str] = None,
    stage: Optional[str] = None,
) -> list[dict]:
    """Return opportunities, optionally filtered by type and/or stage."""
    try:
        clauses, params = [], []
        if type is not None:
            clauses.append("type=?")
            params.append(type)
        if stage is not None:
            clauses.append("stage=?")
            params.append(stage)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = db.execute(f"SELECT * FROM opportunities {where} ORDER BY id", params)
        return _rows_to_dicts(cur.fetchall(), cur)
    except Exception as exc:
        print(f"[operating_state] get_opportunities failed: {exc}")
        return []


def get_opportunity_pipeline(
    db: sqlite3.Connection, type: Optional[str] = None
) -> list[dict]:
    """Return opportunities ordered by expected value (value_est * probability) DESC,
    then by last_activity_ts DESC. Optionally filtered by type."""
    try:
        if type is not None:
            cur = db.execute(
                "SELECT * FROM opportunities WHERE type=? "
                "ORDER BY (value_est * probability) DESC, last_activity_ts DESC",
                (type,),
            )
        else:
            cur = db.execute(
                "SELECT * FROM opportunities "
                "ORDER BY (value_est * probability) DESC, last_activity_ts DESC"
            )
        return _rows_to_dicts(cur.fetchall(), cur)
    except Exception as exc:
        print(f"[operating_state] get_opportunity_pipeline failed: {exc}")
        return []


def update_opportunity_stage(
    db: sqlite3.Connection, opp_id: int, new_stage: str
) -> bool:
    """Update the stage of an opportunity. Returns True iff a row was updated."""
    now = int(time.time())
    try:
        cur = db.execute(
            "UPDATE opportunities SET stage=?, last_activity_ts=? WHERE id=?",
            (new_stage, now, opp_id),
        )
        return cur.rowcount == 1
    except Exception as exc:
        print(f"[operating_state] update_opportunity_stage failed: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Risks
# ─────────────────────────────────────────────────────────────────────────────

def create_risk(
    db: sqlite3.Connection,
    type: str,
    description: str,
    severity: str = "medium",
    thread_id: Optional[str] = None,
) -> Optional[int]:
    """Insert a new risk row. Returns the new id, or None on error."""
    now = int(time.time())
    try:
        cur = db.execute(
            "INSERT INTO risks (type, description, severity, thread_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (type, description, severity, thread_id, now),
        )
        return int(cur.lastrowid) if cur.lastrowid else None
    except Exception as exc:
        print(f"[operating_state] create_risk failed: {exc}")
        return None


def resolve_risk(db: sqlite3.Connection, risk_id: int) -> None:
    """Set resolved_at to the current epoch time for the given risk."""
    now = int(time.time())
    try:
        db.execute("UPDATE risks SET resolved_at=? WHERE id=?", (now, risk_id))
    except Exception as exc:
        print(f"[operating_state] resolve_risk failed: {exc}")


# Severity ordering used when sorting open risks: critical > high > medium > low.
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def get_open_risks(db: sqlite3.Connection) -> list[dict]:
    """Return unresolved risks ordered by severity (critical first), then created_at ASC."""
    try:
        cur = db.execute(
            "SELECT * FROM risks WHERE resolved_at IS NULL ORDER BY created_at ASC"
        )
        rows = _rows_to_dicts(cur.fetchall(), cur)
        rows.sort(key=lambda r: _SEVERITY_ORDER.get(r.get("severity") or "low", 3))
        return rows
    except Exception as exc:
        print(f"[operating_state] get_open_risks failed: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# FTS5 full-text search over threads
# ─────────────────────────────────────────────────────────────────────────────

def search_threads_fts(db: sqlite3.Connection, query: str) -> list[dict]:
    """FTS5 full-text search over threads. Returns [] on any error (FTS5 may be
    unavailable in some SQLite builds)."""
    try:
        cur = db.execute(
            "SELECT t.* FROM threads t "
            "JOIN threads_fts fts ON t.rowid = fts.rowid "
            "WHERE threads_fts MATCH ? LIMIT 20",
            (query,),
        )
        return _rows_to_dicts(cur.fetchall(), cur)
    except Exception as exc:
        print(f"[operating_state] search_threads_fts failed: {exc}")
        return []
