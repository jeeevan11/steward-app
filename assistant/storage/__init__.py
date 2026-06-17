"""Persistence layer: one local SQLite file.

  * db.py            — connection + schema (WAL, crash-safe)
  * ledger.py        — the exactly-once processed-message ledger (state machine)
  * repositories.py  — typed accessors for contacts, rules, pending actions,
                       the audit log, learning events, voice samples, and kv state

Stdlib only (sqlite3).
"""
