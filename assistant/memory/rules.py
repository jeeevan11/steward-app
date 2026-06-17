"""Standing-rule helpers: fetch the rules relevant to a message and render them
for the prompt. Rule *storage* lives in repositories; this is the read/format side.
"""

from __future__ import annotations

import sqlite3

from assistant.models import Contact
from assistant.storage import repositories as repo


def relevant_rules(
    conn: sqlite3.Connection, contact: Contact, category: str
) -> list[str]:
    """Active rules that apply to this message, most-specific first, as readable
    strings. Only ACTIVE rules — 'proposed' inferred rules never silently apply."""
    rows = repo.get_active_rules(conn, contact_email=contact.email, category=category)
    out: list[str] = []
    for r in rows:
        scope = r["scope"]
        where = f" [{r['match_key']}]" if r["match_key"] else ""
        action = f" → {r['action']}" if r["action"] else ""
        out.append(f"({scope}{where}) {r['instruction']}{action}")
    return out
