"""Database migrations (schema evolution).

Applied on startup via init_db(). Idempotent and crash-safe: every migration checks
the current table shape before altering, and a column that already exists is a no-op.
Safe to re-run on every connection open.

This is the single place additive columns are added to the LIVE database — never drop
or rewrite a table here. Each migration is wrapped so one failure can't block the rest.
"""

from __future__ import annotations

import sqlite3

from assistant.logging_setup import get_logger

log = get_logger("migrations")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """ALTER TABLE ... ADD COLUMN, but only if the column is absent. ``ddl`` is the
    full column definition (e.g. "person_id TEXT DEFAULT ''")."""
    if column in _columns(conn, table):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        log.info("migration: added %s.%s", table, column)
    except sqlite3.OperationalError as exc:
        # Another connection raced us to it — that's fine.
        if "duplicate column" not in str(exc).lower():
            log.error("migration failed adding %s.%s: %s", table, column, exc)
            raise


# ── GAP 2 — conversation batching columns + indexes on pending_actions ─────────
def _migrate_pending_actions(conn: sqlite3.Connection) -> None:
    _add_column(conn, "pending_actions", "criticality_signal",
                "criticality_signal TEXT DEFAULT NULL")
    _add_column(conn, "pending_actions", "batch_id", "batch_id TEXT")
    _add_column(conn, "pending_actions", "folded_message_ids",
                "folded_message_ids TEXT DEFAULT '[]'")
    _add_column(conn, "pending_actions", "message_count",
                "message_count INTEGER NOT NULL DEFAULT 1")
    # Fix 4: compose cards (a brand-new outbound, not a reply) store their send target
    # here as JSON {channel, to:[...]|jid, subject, name}. NULL for ordinary reply cards.
    _add_column(conn, "pending_actions", "compose_meta", "compose_meta TEXT DEFAULT NULL")
    # Track where the human responded from (web | telegram | auto | direct)
    _add_column(conn, "pending_actions", "response_via", "response_via TEXT DEFAULT NULL")
    # Indexes that reference the migration-added columns live HERE (after the columns
    # are guaranteed) so init_db's executescript never trips over a not-yet-added column.
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_pa_criticality ON pending_actions(criticality_signal)",
        "CREATE INDEX IF NOT EXISTS idx_pa_batch ON pending_actions(batch_id)",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            log.debug("migration index skipped: %s", exc)


# ── GAP 1 — relationship_type on persons ───────────────────────────────────────
def _migrate_persons(conn: sqlite3.Connection) -> None:
    _add_column(conn, "persons", "relationship_type",
                "relationship_type TEXT NOT NULL DEFAULT 'unknown'")
    # Saved-contact recognition (orthogonal to relationship_type).
    _add_column(conn, "persons", "is_saved_contact",
                "is_saved_contact INTEGER NOT NULL DEFAULT 0")


# Name provenance on contacts — decides whether a stored name is a trustworthy saved
# contact (phone book / verified / in-app) or a spoofable self-set push name.
def _migrate_contacts_name_source(conn: sqlite3.Connection) -> None:
    _add_column(conn, "contacts", "name_source",
                "name_source TEXT NOT NULL DEFAULT 'unknown'")


# ── GAP 6 — person_id on commitments (for dedup keyed on the resolved person) ───
def _migrate_commitments(conn: sqlite3.Connection) -> None:
    _add_column(conn, "commitments", "person_id", "person_id TEXT DEFAULT ''")


# ── GAP 8 — media_type on wa_messages ──────────────────────────────────────────
def _migrate_wa_messages(conn: sqlite3.Connection) -> None:
    if "wa_messages" not in _existing_tables(conn):
        return
    _add_column(conn, "wa_messages", "media_type", "media_type TEXT DEFAULT ''")
    # Delivery lifecycle (status/delivered_at/read_at) for the message-lifecycle feature.
    # Additive nullable columns — ADD COLUMN with DEFAULT is O(1) metadata-only in SQLite,
    # instant on the live DB with no backfill (existing rows read status=0 = "unknown").
    _add_column(conn, "wa_messages", "status", "status INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "wa_messages", "delivered_at", "delivered_at INTEGER")
    _add_column(conn, "wa_messages", "read_at", "read_at INTEGER")
    _add_column(conn, "wa_messages", "is_agent", "is_agent INTEGER NOT NULL DEFAULT 0")
    # Index + the per-recipient group side table live HERE (after the columns are guaranteed
    # on the live table) so they never trip over a not-yet-added column.
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_wamsg_from_status ON wa_messages(from_me, status, ts)",
        "CREATE TABLE IF NOT EXISTS wa_receipts ("
        " message_id TEXT NOT NULL, user_jid TEXT NOT NULL, status INTEGER NOT NULL DEFAULT 0,"
        " ts INTEGER NOT NULL, PRIMARY KEY (message_id, user_jid))",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            log.debug("wa_messages migration index/table skipped: %s", exc)


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


# ── Operating State spine — threads, projects, opportunities, risks, FTS5 ──────
def _migrate_operating_state(conn: sqlite3.Connection) -> None:
    """Ensure the four operating-state tables, their indexes, and the FTS5 virtual
    table exist. All statements are idempotent (IF NOT EXISTS). The FTS5 CREATE is
    wrapped in its own try/except because SQLite builds without FTS5 would otherwise
    abort the whole migration."""
    stmts = [
        # tables
        """CREATE TABLE IF NOT EXISTS threads (
            thread_id        TEXT PRIMARY KEY,
            channel          TEXT,
            status           TEXT,
            person_id        TEXT,
            subject          TEXT,
            project_id       TEXT,
            last_activity_ts INTEGER,
            created_at       INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS projects (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            status      TEXT DEFAULT 'active',
            description TEXT,
            created_at  INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS opportunities (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id        TEXT,
            type             TEXT,
            stage            TEXT,
            value_est        REAL DEFAULT 0.0,
            probability      REAL DEFAULT 0.0,
            next_action      TEXT,
            last_activity_ts INTEGER,
            created_at       INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS risks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT,
            description TEXT,
            severity    TEXT,
            thread_id   TEXT,
            resolved_at INTEGER,
            created_at  INTEGER
        )""",
        # indexes
        "CREATE INDEX IF NOT EXISTS idx_threads_status  ON threads(status)",
        "CREATE INDEX IF NOT EXISTS idx_threads_project ON threads(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_opportunities_type ON opportunities(type)",
    ]
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            log.debug("migration operating_state skipped: %s", exc)

    # FTS5 virtual table — silently skipped if this SQLite build lacks FTS5
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS threads_fts "
            "USING fts5(thread_id UNINDEXED, subject, person_id, "
            "           content='threads', content_rowid='rowid')"
        )
    except sqlite3.OperationalError as exc:
        log.warning("migration: threads_fts (FTS5) not created — %s", exc)


# ── Fix 3: per-contact adaptive settling window ───────────────────────────────
def _migrate_contacts_response(conn: sqlite3.Connection) -> None:
    _add_column(conn, "contacts", "median_response_seconds",
                "median_response_seconds REAL")


# ── Fix 5: opaque attachment tracking on whatsapp_inbox ───────────────────────
# NOTE: the table is `whatsapp_inbox` (see whatsapp_inbox.py), not `wa_inbox` — the
# original guard checked the wrong name, so the column was never added and the runtime
# `UPDATE` silently no-op'd. Fixed to the real table name.
def _migrate_wa_inbox_opaque(conn: sqlite3.Connection) -> None:
    tables = _existing_tables(conn)
    if "whatsapp_inbox" in tables:
        _add_column(conn, "whatsapp_inbox", "opaque", "opaque INTEGER NOT NULL DEFAULT 0")


# ── Fix 7: turn_id on processed_messages ─────────────────────────────────────
def _migrate_turn_id(conn: sqlite3.Connection) -> None:
    _add_column(conn, "processed_messages", "turn_id",
                "turn_id TEXT DEFAULT ''")
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pm_turn ON processed_messages(turn_id)"
        )
    except sqlite3.OperationalError as exc:
        log.debug("migration idx_pm_turn skipped: %s", exc)


# ── approval-integrity cluster — WYSIWYG approval, send-target binding, fold-fold
#    fold-children index, and a dedicated send-start clock for the stuck-send reaper.
#    All ADDITIVE columns/indexes, appended at the END so existing migrations and
#    the base CREATE TABLE statements are never touched. ───────────────────────
def _migrate_approval_integrity(conn: sqlite3.Connection) -> None:
    # WYSIWYG_APPROVAL: the hash of EXACTLY the draft_text + recipients + thread_id the
    # owner saw when the card was last rendered/approved. begin_send re-derives it from the
    # live row and REFUSES to send on a mismatch (fail safe), so a fold that mutates the
    # draft under an already-rendered card can never be sent under the stale approval.
    _add_column(conn, "pending_actions", "approval_hash",
                "approval_hash TEXT DEFAULT NULL")
    # NO_WRONG_THREAD / NO_WRONG_RECIPIENT: the send target (thread_id + recipients) bound
    # to the approved draft. A fold that changes the target invalidates this and requires a
    # fresh approval, so a folded reply can never be misrouted into the original thread.
    _add_column(conn, "pending_actions", "send_target", "send_target TEXT DEFAULT NULL")
    # failure-recovery-2: a purpose-built send-start clock. begin_send stamps it inside the
    # same compare-and-set; the stuck-send reaper keys staleness on THIS, not created_at,
    # so a folded card (which resets created_at) and an aged-but-just-approved card are no
    # longer mis-aged.
    _add_column(conn, "pending_actions", "sending_started_at",
                "sending_started_at INTEGER DEFAULT NULL")
    # scaling-time-2: an indexed fold-membership table so the queue's folded-child lookup is
    # an O(1) point lookup instead of an unindexed leading-wildcard LIKE full-scan of the
    # never-pruned pending_actions table on every non-pending queue row.
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS folded_children ("
            "  child_message_id TEXT PRIMARY KEY,"
            "  parent_action_id INTEGER NOT NULL,"
            "  created_at       INTEGER NOT NULL DEFAULT (strftime('%s','now'))"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_folded_children_parent "
            "ON folded_children(parent_action_id)"
        )
    except sqlite3.OperationalError as exc:
        log.debug("migration folded_children skipped: %s", exc)


# ── memory-knowledge-1: MEMORY_PROVENANCE on relationship_memory ──────────────
# ROOT CAUSE: a distilled fact was stored as a bare key->value string in
# `summary_json` with NO record of WHERE it came from. A counterparty's
# self-assertion ("I'm the CFO", "we closed our Series B") was therefore later
# rendered to the brain as established truth, indistinguishable from something the
# owner or the agent had actually verified.
#
# FIX: carry a parallel provenance map keyed by the same fact key, holding
# {source, source_type, ts} for each fact. source_type is one of:
#   claimed   - the counterparty asserted it about themselves (UNTRUSTED)
#   observed  - the agent observed it from the owner's own behaviour/text
#   inferred  - the agent inferred it (a guess, not asserted by anyone)
#   verified  - the owner (a human) confirmed it
# Retrieval must never render a merely-"claimed" fact as verified truth.
# Additive: a brand-new column defaulting to '{}', read defensively everywhere.
def _migrate_relationship_provenance(conn: sqlite3.Connection) -> None:
    if "relationship_memory" not in _existing_tables(conn):
        return
    _add_column(conn, "relationship_memory", "provenance_json",
                "provenance_json TEXT DEFAULT '{}'")


# ── scaling-time-1 / scaling-time-3: indexes for the live-queue + dashboard reads ──
# The folded-child / per-message lookups scanned pending_actions, and /api/learning
# full-scanned learning_events. These indexes make both point/range lookups instead of
# O(n) scans on the never-pruned tables. CREATE INDEX IF NOT EXISTS is additive + idempotent.
def _migrate_perf_indexes(conn: sqlite3.Connection) -> None:
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_pa_message_id ON pending_actions(message_id)",
        "CREATE INDEX IF NOT EXISTS idx_learning_events_ts_type ON learning_events(ts, type)",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as exc:
            log.debug("perf index skipped: %s", exc)


_MIGRATIONS = (
    _migrate_pending_actions,
    _migrate_persons,
    _migrate_commitments,
    _migrate_wa_messages,
    _migrate_operating_state,
    _migrate_contacts_response,
    _migrate_contacts_name_source,
    _migrate_wa_inbox_opaque,
    _migrate_turn_id,
    _migrate_approval_integrity,
    _migrate_relationship_provenance,
    _migrate_perf_indexes,
)


def apply_all_migrations(conn: sqlite3.Connection) -> None:
    """Apply every migration. Each is independent and idempotent; one failure is logged
    and the rest still run."""
    for fn in _MIGRATIONS:
        try:
            fn(conn)
        except Exception as exc:  # noqa: BLE001 - one migration never blocks the rest
            log.error("migration %s failed (non-fatal): %s", fn.__name__, exc)
    try:
        conn.commit()
    except sqlite3.Error:
        pass
