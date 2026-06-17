"""SQLite connection + schema.

Single file, WAL mode for crash safety (a half-written transaction is rolled back
cleanly on restart). All timestamps are epoch seconds (INTEGER) for portability.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = r"""
-- ── Exactly-once ledger ──────────────────────────────────────────────────────
-- One row per channel message id. The PRIMARY KEY + INSERT OR IGNORE is the
-- dedup gate. state advances SEEN → PROCESSING → DONE (or FAILED). A crash mid-
-- PROCESSING is recoverable because the autonomous pipeline only performs
-- reversible/idempotent actions; the single irreversible action (send) is gated
-- separately through pending_actions.
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   TEXT PRIMARY KEY,
    thread_id    TEXT,
    state        TEXT NOT NULL DEFAULT 'SEEN',  -- SEEN|PROCESSING|DONE|FAILED
    tier         INTEGER,
    category     TEXT,
    confidence   REAL,
    dry_run      INTEGER NOT NULL DEFAULT 1,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_pm_state ON processed_messages(state);

-- ── Pending actions awaiting your decision in Telegram ───────────────────────
-- idempotency_key is UNIQUE: the same (message, kind) is never queued twice.
-- The send path uses a compare-and-set on status (APPROVED → SENDING) so a
-- double tap / restart cannot double-send.
CREATE TABLE IF NOT EXISTS pending_actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key   TEXT UNIQUE NOT NULL,
    message_id        TEXT NOT NULL,
    thread_id         TEXT,
    tier              INTEGER NOT NULL,
    kind              TEXT NOT NULL,            -- reply_draft | fyi | ask
    summary           TEXT,
    draft_text        TEXT,
    status            TEXT NOT NULL DEFAULT 'PENDING',
                      -- PENDING|APPROVED|SENDING|SENT|EDITED|SKIPPED|SEND_FAILED|EXPIRED
                      -- |SEND_STUCK (crash mid-send, never resent)
                      -- |SEND_AMBIGUOUS (maybe-delivered, never auto-resent; EXACTLY_ONCE_SEND)
                      -- |SUPERSEDED (collapsed into the living conversation card)
                      -- |HANDLED_ELSEWHERE (owner replied on another device; closed, never sent)
    telegram_chat_id  TEXT,
    telegram_message_id TEXT,
    sent_gmail_id     TEXT,
    error             TEXT,
    criticality_signal TEXT DEFAULT NULL,        -- None | 🔴 | 🟡 (critical bypasses batching)
    batch_id          TEXT,                       -- FK to a message batch if batched
    folded_message_ids TEXT DEFAULT '[]',         -- JSON list of message ids folded into this card
    message_count     INTEGER NOT NULL DEFAULT 1, -- how many messages this card represents
    created_at        INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    decided_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pa_status ON pending_actions(status);
CREATE INDEX IF NOT EXISTS idx_pa_tg ON pending_actions(telegram_message_id);
-- Indexes on the migration-added columns (criticality_signal, batch_id) are created
-- by migrations.apply_all_migrations AFTER those columns are guaranteed to exist on a
-- legacy DB; keeping them out of this base SCHEMA prevents executescript from failing
-- on a live DB whose pending_actions predates those columns.

-- ── Audit log of every autonomous action (and surfaced item) ─────────────────
-- undo_data is JSON describing how to reverse a reversible action.
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    kind          TEXT NOT NULL,     -- archive|label|send|fyi|surface|undo|...
    message_id    TEXT,
    thread_id     TEXT,
    tier          INTEGER,
    summary       TEXT,
    reversible    INTEGER NOT NULL DEFAULT 0,
    undo_data     TEXT,
    undone        INTEGER NOT NULL DEFAULT 0,
    dry_run       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- ── Contacts (per-person profile / memory) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS contacts (
    email                TEXT PRIMARY KEY,
    name                 TEXT DEFAULT '',
    relationship         TEXT DEFAULT '',
    importance           INTEGER NOT NULL DEFAULT 0,
    flags                TEXT DEFAULT '',   -- comma-separated: investor,legal,vip,blocked
    reply_rate           REAL NOT NULL DEFAULT 0,
    avg_response_seconds REAL,
    msg_count            INTEGER NOT NULL DEFAULT 0,
    sent_to_count        INTEGER NOT NULL DEFAULT 0,
    received_count       INTEGER NOT NULL DEFAULT 0,
    notes                TEXT DEFAULT '',
    -- Provenance of `name`: decides how much we trust it as a real "saved contact" signal.
    --   saved=phone-book name (c.name) · business=WA verified · manual=owner saved in-app
    --   · push=sender's own display name (spoofable) · unknown=legacy/no provenance.
    name_source          TEXT NOT NULL DEFAULT 'unknown',
    updated_at           INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- ── Standing rules / preferences ─────────────────────────────────────────────
-- status='proposed' rules are inferred and must be confirmed before they apply.
CREATE TABLE IF NOT EXISTS rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scope          TEXT NOT NULL,     -- global | contact | category
    match_key      TEXT DEFAULT '',   -- email (contact), category name, or '' (global)
    instruction    TEXT NOT NULL,     -- natural-language guidance applied to matching mail
    action         TEXT DEFAULT '',   -- optional machine hint: archive|label:X|never_notify|...
    status         TEXT NOT NULL DEFAULT 'active',  -- active | proposed | retired
    source         TEXT NOT NULL DEFAULT 'user',    -- user | inferred
    confidence     REAL NOT NULL DEFAULT 1.0,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at     INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_rules_scope ON rules(scope, match_key, status);

-- ── Voice samples mined from your Sent mail (few-shot for drafting) ───────────
CREATE TABLE IF NOT EXISTS voice_samples (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_email  TEXT,              -- NULL = global sample
    subject        TEXT DEFAULT '',
    body           TEXT NOT NULL,
    ts             INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_voice_contact ON voice_samples(contact_email);

-- ── Learning events (every edit/skip/override/undo) ──────────────────────────
CREATE TABLE IF NOT EXISTS learning_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    type         TEXT NOT NULL,       -- approve|edit|skip|override|undo|pause
    message_id   TEXT,
    action_id    INTEGER,
    contact_email TEXT,
    detail       TEXT                 -- JSON blob
);

-- ── Key/value app state (last historyId, mode override, paused, etc.) ─────────
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ── Feedback-loop capture (P5c) — every human signal, stored immediately ──────
-- These are the raw evidence the learning layer (P5a/P5b) later analyses. Writes
-- are fire-and-forget from the recorder; a failure here never blocks an action.
CREATE TABLE IF NOT EXISTS draft_edits (
    id             TEXT PRIMARY KEY,
    message_id     TEXT,
    segment        TEXT,
    original_draft TEXT,
    final_draft    TEXT,
    diff           TEXT,
    created_at     INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_draft_edits_seg ON draft_edits(segment);

CREATE TABLE IF NOT EXISTS skip_log (
    id          TEXT PRIMARY KEY,
    message_id  TEXT,
    tier        INTEGER,
    summary     TEXT,
    reason      TEXT,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS proposed_rules (
    id               TEXT PRIMARY KEY,
    rule_text        TEXT,
    source           TEXT,      -- 'learned' | 'inferred'
    pattern_evidence TEXT,
    status           TEXT DEFAULT 'pending',  -- pending | confirmed | rejected
    created_at       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    resolved_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_proposed_rules_status ON proposed_rules(status);

-- ── Commitments you made (P4) — promises extracted from replies you sent ───────
-- e.g. "I'll send the deck by Friday". Surfaced proactively at the daily check so a
-- dropped ball becomes a nudge, not a missed obligation.
CREATE TABLE IF NOT EXISTS commitments (
    id             TEXT PRIMARY KEY,
    message_id     TEXT,
    contact_email  TEXT,
    person_id      TEXT DEFAULT '',   -- resolved cross-channel person (for dedup)
    commitment_text TEXT,
    due_date       TEXT,        -- YYYY-MM-DD or '' if unknown
    status         TEXT DEFAULT 'open',   -- open | done | snoozed | stale
    created_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    resolved_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status);

-- ── Cross-channel PERSON identity (Memory Part A) ─────────────────────────────
-- One PERSON entity above the per-identifier `contacts` table. A person aggregates
-- one or more identifiers (email addresses and/or WhatsApp JIDs). `contacts` is
-- untouched; persons sits on top for cross-channel recognition + relationship memory.
CREATE TABLE IF NOT EXISTS persons (
    id            TEXT PRIMARY KEY,
    display_name  TEXT DEFAULT '',
    emails        TEXT DEFAULT '[]',   -- JSON list of email identifiers
    phone_jids    TEXT DEFAULT '[]',   -- JSON list of WhatsApp JIDs
    company       TEXT DEFAULT '',
    role          TEXT DEFAULT '',
    segment       TEXT DEFAULT '',
    relationship  TEXT DEFAULT '',
    relationship_type TEXT NOT NULL DEFAULT 'unknown',  -- partner|family|investor|mentor|collaborator|customer|recruiter|cold|unknown
    -- 1 = the owner has SAVED this person (phone book / in-app Save Contact). Orthogonal to
    -- relationship_type, so a person can be both 'saved' AND e.g. 'investor'. A saved person
    -- never reads as "unknown".
    is_saved_contact INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at    INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- identifier (email or jid) → person. PRIMARY KEY gives instant, unique lookup.
-- Holds ACTIVE links only; weak/uncertain matches live in person_link_suggestions.
CREATE TABLE IF NOT EXISTS person_links (
    identifier  TEXT PRIMARY KEY,
    person_id   TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    source      TEXT DEFAULT '',     -- observed | strong | confirmed
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_person_links_pid ON person_links(person_id);

-- The "ask Jatin once" cross-channel merge flow. A weak (possible-but-not-certain)
-- match becomes a pending suggestion; rejections are REMEMBERED (status='rejected')
-- so the same pair is never asked again.
CREATE TABLE IF NOT EXISTS person_link_suggestions (
    id                   TEXT PRIMARY KEY,
    identifier_new       TEXT NOT NULL,
    candidate_person_id  TEXT NOT NULL,
    reason               TEXT DEFAULT '',
    confidence           REAL NOT NULL DEFAULT 0.0,
    status               TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | rejected
    created_at           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    resolved_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_link_sugg_status ON person_link_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_link_sugg_new ON person_link_suggestions(identifier_new);

-- ── Relationship memory (Memory Part B) — Layer 2, one curated record per person ──
-- Distilled, NOT raw: facts + what's open + what was decided + the agent's own recent
-- episodes + an audit of superseded facts. Kept compact (size-capped in distill.py);
-- raw emails are never stored here.
CREATE TABLE IF NOT EXISTS relationship_memory (
    person_id            TEXT PRIMARY KEY,
    summary_json         TEXT DEFAULT '{}',
    open_situations_json TEXT DEFAULT '[]',
    decided_json         TEXT DEFAULT '[]',
    episodes_json        TEXT DEFAULT '[]',
    superseded_json      TEXT DEFAULT '[]',
    last_distilled_at    INTEGER,
    version              INTEGER NOT NULL DEFAULT 0
);

-- ── Segmented voice profiles (P5a) — how you write to each audience ────────────
-- One row per segment (investor|customer|team|external). profile_json holds the
-- distilled style summary + a few example snippets. Rebuilt weekly from your
-- voice_samples; drafting falls back to the global kv profile when a segment is thin.
CREATE TABLE IF NOT EXISTS voice_profiles (
    segment      TEXT PRIMARY KEY,
    profile_json TEXT,
    sample_count INTEGER NOT NULL DEFAULT 0,
    last_rebuilt TEXT
);

-- ── Operating State spine (threads, projects, opportunities, risks) ───────────
CREATE TABLE IF NOT EXISTS threads (
    thread_id        TEXT PRIMARY KEY,
    channel          TEXT,
    status           TEXT,
    person_id        TEXT,
    subject          TEXT,
    project_id       TEXT,
    last_activity_ts INTEGER,
    created_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_threads_status  ON threads(status);
CREATE INDEX IF NOT EXISTS idx_threads_project ON threads(project_id);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    status      TEXT DEFAULT 'active',
    description TEXT,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS opportunities (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id        TEXT,
    type             TEXT,
    stage            TEXT,
    value_est        REAL DEFAULT 0.0,
    probability      REAL DEFAULT 0.0,
    next_action      TEXT,
    last_activity_ts INTEGER,
    created_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_opportunities_type ON opportunities(type);

CREATE TABLE IF NOT EXISTS risks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT,
    description TEXT,
    severity    TEXT,
    thread_id   TEXT,
    resolved_at INTEGER,
    created_at  INTEGER
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with crash-safe pragmas. Pass ':memory:' for tests."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the Telegram bot creates the connection on its main
    # thread but runs all DB work on a single-worker executor thread (serialized, so
    # never concurrent). The poller thread uses its OWN connection. WAL + busy_timeout
    # handle multi-connection access to the file.
    conn = sqlite3.connect(
        db_path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if absent (idempotent), then apply additive
    migrations so a live DB created by an older schema gets new columns automatically.
    Migrations run here (not only in open_db) so every code path that opens a
    connection — including the web console's get_conn and tests — sees the full
    current shape."""
    conn.executescript(SCHEMA)
    from assistant.storage import migrations
    migrations.apply_all_migrations(conn)


def open_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    init_db(conn)
    return conn
