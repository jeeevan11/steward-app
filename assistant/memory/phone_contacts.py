"""Sync WhatsApp contact names into Steward's contacts DB.

WhatsApp's linked-device protocol does NOT expose the full phonebook. We can only
get names for contacts whose messages have been received. Sources (in priority order):

  1. relay/contact_cache.json  — names delivered via contacts.upsert events (phone-book
                                  name when available, push_name otherwise)
  2. whatsapp_inbox.push_name  — name the sender set on their own WhatsApp profile
  3. Inferred: if someone sent 3+ messages or received a reply → mark as 'seen' contact

We call `GET /contacts` on the relay (port 7998) for the live in-memory cache,
then also read the on-disk cache, then fill gaps from whatsapp_inbox.

For contacts whose WhatsApp name is blank/meaningless (like '..'), the display
falls back to their phone number extracted from the JID. The user can set a real
name manually via the Contacts tab.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request

from assistant.logging_setup import get_logger

log = get_logger("phone_contacts")

RELAY_SEND_PORT = 7998
_RELAY_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "relay")
CONTACT_CACHE_PATH = os.path.join(_RELAY_DIR, "contact_cache.json")


def _is_meaningful_name(name: str) -> bool:
    return len(re.sub(r"[^a-zA-Z0-9]", "", name or "")) >= 2


def _relay_auth_headers() -> dict[str, str]:
    """Shared-secret headers for relay HTTP calls (config-secrets-deploy-1).

    The relay (relay/whatsapp_relay.js) requires an ``X-Cos-Token`` matching the
    ``INGEST_TOKEN`` shared secret on its HTTP endpoints (including GET /contacts)
    when that secret is configured; without the header the request is rejected with
    a 401 and this sync would silently fall back to the stale on-disk cache.

    We read ``INGEST_TOKEN`` straight from the environment to mirror the relay
    (``process.env.INGEST_TOKEN``) and ``Settings.ingest_token`` — both derive from
    the same env var (load_settings() populates os.environ from .env). This keeps the
    public ``sync(conn)`` entry point free of a Settings argument. Empty token →
    no header (back-compat, localhost-only deployments)."""
    token = os.environ.get("INGEST_TOKEN", "")
    return {"X-Cos-Token": token} if token else {}


def _fetch_relay_live() -> tuple[dict[str, str], dict[str, str]]:
    """GET /contacts from the relay's live in-memory cache.
    Returns (jid→name, lid→phone_jid) maps."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{RELAY_SEND_PORT}/contacts",
            headers=_relay_auth_headers(),
        )
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read().decode())
        names = {c["jid"].lower(): c["name"] for c in data.get("contacts", []) if c.get("jid") and c.get("name")}
        lid_map = {k.lower(): v.lower() for k, v in data.get("lid_jid_map", {}).items() if k and v}
        return names, lid_map
    except Exception:  # noqa: BLE001
        return {}, {}


def _read_cache_file() -> dict[str, str]:
    """Read the persisted contact_cache.json from disk."""
    try:
        with open(CONTACT_CACHE_PATH, "r") as f:
            data = json.load(f)
        return {k.lower(): v for k, v in data.items() if k and v}
    except Exception:  # noqa: BLE001
        return {}


def _read_inbox_names(conn: sqlite3.Connection) -> dict[str, str]:
    """Get latest push_name for every JID from whatsapp_inbox."""
    rows = conn.execute(
        "SELECT jid, push_name FROM whatsapp_inbox WHERE jid IS NOT NULL GROUP BY jid HAVING push_name IS NOT NULL"
    ).fetchall()
    return {r[0].lower(): (r[1] or "").strip() for r in rows if r[1]}


# memory-identity-6: relationship values that confer "recognized" status.
# contacts.is_recognized() returns True for ANY non-empty relationship OR any
# importance>0. So a recognition floor = setting relationship='wa_contact' AND
# importance>=5. We grant that floor ONLY to phonebook-grade names (relay
# contacts.upsert: live cache + on-disk contact_cache.json), NEVER to a self-set,
# spoofable whatsapp_inbox.push_name. An inbox-only sender is recorded with
# relationship='' and importance=0 so is_recognized() stays False and the approval
# card renders the "not a saved contact" branch instead of "👤 <spoofed> · imp 5".
_RECOGNITION_REL = "wa_contact"
_RECOGNITION_FLOOR = 5


def _record_pushname_holdback(
    conn: sqlite3.Connection, jid: str, name: str
) -> None:
    """Observability for memory-identity-6: an inbox-only push_name was NOT promoted
    to a recognized contact. Without this the suppression would be silent. Best-effort."""
    try:
        from assistant.storage import repositories as _repo
        _repo.record_event(
            conn, type="contact_pushname_holdback", contact_email=jid,
            detail={"jid": jid, "push_name": name, "reason": "inbox_only_unsaved_sender"},
        )
    except Exception:  # noqa: BLE001 - audit is best-effort, never block sync
        log.debug("phone_contacts: push_name holdback audit failed", exc_info=True)


def sync(conn: sqlite3.Connection) -> dict[str, int]:
    """Merge all WhatsApp name sources into the Steward contacts DB.

    Returns {"matched": N, "skipped": M, "sources": {...}}."""
    live_names, lid_map = _fetch_relay_live()
    cached = _read_cache_file()
    inbox = _read_inbox_names(conn)

    # memory-identity-6: PHONEBOOK-GRADE names come from the relay's contacts.upsert
    # stream — the live in-memory cache (live_names) and its on-disk persistence
    # (contact_cache.json). These carry the phone-book name when the contact is saved
    # on the owner's phone, so they are a trustworthy "this is a real saved contact"
    # signal. whatsapp_inbox.push_name is the name the SENDER set on their OWN profile
    # — spoofable, and present for every stranger who messages once — so it must never
    # by itself confer recognition.
    phonebook: dict[str, str] = {}
    for jid, name in cached.items():
        if name:
            phonebook[jid] = name
    for jid, name in live_names.items():
        if name:
            phonebook[jid] = name  # live relay wins over the on-disk snapshot

    # Merge for the NAME label only: phonebook > inbox (best quality wins). The
    # provenance (phonebook vs inbox-only) is tracked separately to gate recognition.
    merged: dict[str, str] = {}
    for jid, name in inbox.items():
        merged[jid] = name
    for jid, name in phonebook.items():
        merged[jid] = name

    matched = 0
    skipped = 0
    pushname_held = 0

    for jid, name in merged.items():
        if not jid:
            skipped += 1
            continue
        name_ok = _is_meaningful_name(name)
        # Phonebook-grade iff this JID has a relay contacts.upsert name. An inbox-only
        # JID (push_name with no phonebook entry) does NOT earn the recognition floor.
        is_phonebook = jid in phonebook

        existing = conn.execute(
            "SELECT name, relationship, importance FROM contacts WHERE email=?", (jid,)
        ).fetchone()

        if existing:
            current_name = (existing[0] or "").strip()
            current_rel = (existing[1] or "").strip()
            current_imp = int(existing[2] or 0)
            current_meaningful = _is_meaningful_name(current_name)
            new_name = name if (name_ok and not current_meaningful) else current_name
            if is_phonebook:
                # Phonebook source: apply the recognition floor as before.
                new_rel = current_rel if (current_rel and current_rel != _RECOGNITION_REL) else _RECOGNITION_REL
                new_imp = max(current_imp, _RECOGNITION_FLOOR)
            else:
                # Inbox-only push_name: preserve whatever recognition the row ALREADY
                # earned (from a prior phonebook sync, a user edit, or activity), but
                # never RAISE relationship/importance off a spoofable self-set name.
                new_rel = current_rel
                new_imp = current_imp
                if not (current_rel or current_imp > 0):
                    pushname_held += 1
                    _record_pushname_holdback(conn, jid, name)
            conn.execute(
                "UPDATE contacts SET name=?, relationship=?, importance=? WHERE email=?",
                (new_name, new_rel, new_imp, jid),
            )
        else:
            if is_phonebook:
                conn.execute(
                    "INSERT OR IGNORE INTO contacts (email, name, relationship, importance) VALUES (?,?,?,?)",
                    (jid, name if name_ok else jid, _RECOGNITION_REL, _RECOGNITION_FLOOR),
                )
            else:
                # Inbox-only stranger: store the display label but with relationship=''
                # and importance=0 so is_recognized() stays False (no spoofed 👤 marker).
                conn.execute(
                    "INSERT OR IGNORE INTO contacts (email, name, relationship, importance) VALUES (?,?,?,?)",
                    (jid, name if name_ok else jid, "", 0),
                )
                pushname_held += 1
                _record_pushname_holdback(conn, jid, name)

        matched += 1

    # Resolve LID→phone JID: if relay gave us '9136083337274@lid' → '919164536565@s.whatsapp.net',
    # copy the name from the phone JID entry to the LID entry (and vice versa) so both keys
    # show the right name.
    #
    # memory-identity-6: the relay's lid_jid_map is phonebook-grade (contacts.upsert),
    # but the NAME we copy must itself be phonebook-grade to confer recognition — a
    # name that exists ONLY as an inbox push_name must not be laundered into a
    # recognized contact via this path. So gate the floor on a phonebook name.
    lid_resolved = 0
    for lid, phone_jid in lid_map.items():
        lid_name = phonebook.get(lid) or phonebook.get(phone_jid)
        lid_name_is_phonebook = bool(lid_name)
        if not lid_name:
            lid_name = merged.get(lid) or merged.get(phone_jid)
        if not lid_name or not _is_meaningful_name(lid_name):
            continue
        for key in (lid, phone_jid):
            existing = conn.execute(
                "SELECT relationship, importance FROM contacts WHERE email=?", (key,)
            ).fetchone()
            if lid_name_is_phonebook:
                if existing:
                    conn.execute(
                        "UPDATE contacts SET name=?, relationship=?, importance=MAX(importance,?) WHERE email=?",
                        (lid_name, _RECOGNITION_REL, _RECOGNITION_FLOOR, key),
                    )
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO contacts (email, name, relationship, importance) VALUES (?,?,?,?)",
                        (key, lid_name, _RECOGNITION_REL, _RECOGNITION_FLOOR),
                    )
            else:
                # Inbox-only name: set the display label but NEVER raise recognition.
                if existing:
                    # Only fill an empty/meaningless name; never raise relationship/importance.
                    row = conn.execute(
                        "SELECT name FROM contacts WHERE email=?", (key,)
                    ).fetchone()
                    if row and not _is_meaningful_name((row[0] or "").strip()):
                        conn.execute(
                            "UPDATE contacts SET name=? WHERE email=?", (lid_name, key)
                        )
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO contacts (email, name, relationship, importance) VALUES (?,?,?,?)",
                        (key, lid_name, "", 0),
                    )
        lid_resolved += 1

    conn.commit()
    log.info(
        "phone_contacts: sync done — %d contacts updated, %d LIDs resolved, "
        "%d inbox-only push_names held back from recognition "
        "(live=%d, lid_map=%d, cache=%d, inbox=%d)",
        matched, lid_resolved, pushname_held,
        len(live_names), len(lid_map), len(cached), len(inbox),
    )
    return {
        "matched": matched,
        "skipped": skipped,
        "lid_resolved": lid_resolved,
        "pushname_held": pushname_held,
        "relay_live": len(live_names),
        "from_cache": len(cached),
        "from_inbox": len(inbox),
    }
