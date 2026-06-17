"""Cross-channel PERSON identity resolution (Memory Part A).

Every inbound message has an `identifier` — a real email on Gmail, a WhatsApp JID on
WhatsApp (both live in `Message.sender_email`). This module maps that identifier to a
single PERSON entity so the agent treats one human as one ongoing relationship across
channels.

Resolution order:
  1. Existing active link → return that person.
  2. STRONG, unambiguous signal → auto-link to an existing person:
       a. an email literally written in a WhatsApp message body that already belongs
          to a known person;
       b. a phone number in an email signature that matches a known person's JID;
       c. an EXACT full-name (2+ tokens) + same non-generic company, matching exactly
          one existing person.
     Anything short of genuinely unambiguous is NOT a merge.
  3. Otherwise create a NEW person, and if there is a plausible-but-uncertain match
     (e.g. same full name on a different channel) record a one-time SUGGESTION for
     Jatin to confirm. Rejected suggestions are remembered and never re-asked.

A wrong auto-merge silently corrupts two people's memory, so the auto bar is strict
and the default is "separate persons". Every public function is defensive: callers
(process_one) wrap this so a failure degrades to thread-only classification.
"""

from __future__ import annotations

import json as _json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Optional

from assistant.logging_setup import get_logger
from assistant.models import Message
from assistant.storage import repositories as repo

log = get_logger("identity")

# Free-mail / generic domains are NOT an identity signal (everyone shares them).
_GENERIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com",
    "hotmail.com", "icloud.com", "proton.me", "protonmail.com", "aol.com",
    "live.com", "me.com", "msn.com", "ymail.com",
})

_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"\+?\d[\d\s\-().]{7,}\d")

# memory-identity-3: where the relay persists its LID→phone-JID map (written by
# relay/whatsapp_relay.js: scheduleLidMapSave -> LID_JID_MAP_PATH). Read-only here.
# Overridable via env for tests / non-default deployments; safe default constant.
_LID_JID_MAP_PATH = os.environ.get(
    "STEWARD_LID_JID_MAP_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "relay", "lid_jid_map.json"),
)


@dataclass
class Resolution:
    person_id: str
    created: bool = False
    suggestion: Optional[dict] = None  # {id, identifier_new, candidate_person_id, reason, ...}


# ─────────────────────────────────────────────────────────────────────────────
# Identifier helpers (pure)
# ─────────────────────────────────────────────────────────────────────────────
def is_jid(identifier: str) -> bool:
    i = (identifier or "").lower()
    # WhatsApp identifiers: phone-based (@s.whatsapp.net), groups (@g.us), and the
    # newer LinkedID form (@lid) that Baileys now returns for many contacts.
    return i.endswith("@s.whatsapp.net") or i.endswith("@g.us") or i.endswith("@lid")


def is_lid(identifier: str) -> bool:
    """True for a WhatsApp LinkedID JID (`...@lid`).

    memory-identity-3: a @lid 'number' is an OPAQUE LinkedID alias, NOT a phone
    number. Its leading digits must never be compared against real phone digits
    (that would fuse unrelated humans), and it must be resolved to its canonical
    `@s.whatsapp.net` JID before identity resolution so email<->WhatsApp unification
    works for LID contacts instead of silently creating a duplicate person.
    """
    return (identifier or "").lower().endswith("@lid")


def is_email(identifier: str) -> bool:
    i = (identifier or "").lower()
    return ("@" in i) and not is_jid(i)


def _company_from(identifier: str) -> str:
    """Company signal = the email domain, but only when it isn't a free-mail domain."""
    if not is_email(identifier):
        return ""
    domain = identifier.split("@", 1)[1].lower()
    return "" if domain in _GENERIC_DOMAINS else domain


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _jid_digits(identifier: str) -> str:
    # memory-identity-3: NEVER expose the leading digits of a @lid alias as a
    # phone number. A LinkedID's "digits" are an opaque alias, not a dialable
    # number, so phone-comparing them would silently fuse unrelated people. Only
    # phone-based (@s.whatsapp.net) and group (@g.us) JIDs carry real digits.
    if not is_jid(identifier) or is_lid(identifier):
        return ""
    return re.sub(r"\D", "", identifier.split("@", 1)[0])


# ─────────────────────────────────────────────────────────────────────────────
# memory-identity-3: LID -> canonical phone-JID resolution
# ─────────────────────────────────────────────────────────────────────────────
def _load_lid_jid_map() -> dict[str, str]:
    """Read the relay's on-disk LID->phone-JID map ({lid_lower: phone_jid_lower}).

    Best-effort and defensive: the file is owned by the relay; a missing/corrupt
    file (first run, mid-write) yields an empty map and resolution falls back to
    the DB (`_lid_to_phone_via_links`). Never raises into the caller.
    """
    try:
        with open(_LID_JID_MAP_PATH, "r") as f:
            data = _json.load(f)
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for lid, jid in data.items():
            if lid and jid and isinstance(lid, str) and isinstance(jid, str):
                lk, jk = lid.strip().lower(), jid.strip().lower()
                # Only trust a mapping whose value is a real phone JID.
                if lk.endswith("@lid") and jk.endswith("@s.whatsapp.net"):
                    out[lk] = jk
        return out
    except Exception:  # noqa: BLE001 - map is advisory; absence degrades gracefully
        return {}


def _lid_to_phone_via_links(conn: sqlite3.Connection, lid: str) -> Optional[str]:
    """Fallback resolution path: if a person ALREADY owns both this @lid and a
    phone JID (e.g. phone_contacts.sync reconciled them into one contact/person
    earlier), return that person's phone JID so the @lid folds onto it.

    This keeps persons/person_links reconciled (not just the contacts table) even
    when the on-disk lid_jid_map is unavailable. Best-effort; never raises."""
    try:
        pid = repo.person_link_get(conn, lid)
        if not pid:
            return None
        p = repo.person_get(conn, pid)
        if p is None:
            return None
        for j in _json.loads(p["phone_jids"] or "[]"):
            if isinstance(j, str) and j.lower().endswith("@s.whatsapp.net"):
                return j.lower()
    except Exception:  # noqa: BLE001 - advisory only
        return None
    return None


def resolve_lid_to_phone_jid(conn: sqlite3.Connection, identifier: str) -> Optional[str]:
    """Resolve a `...@lid` identifier to its canonical `...@s.whatsapp.net` JID.

    memory-identity-3: WhatsApp delivers many contacts as opaque LinkedID (@lid)
    JIDs. If a @lid reaches identity resolution unchanged, email<->WhatsApp
    unification fails (person_link_by_phone_digits only matches @s.whatsapp.net
    rows) and a SECOND, duplicate person is created for the same human. We resolve
    the @lid to its real phone JID FIRST — via the relay's lid_jid_map, then via
    an already-linked person — so matching/linking happen on the canonical key.

    Returns the lowercased phone JID, or None when the LID is not (yet) resolvable
    (in which case the @lid is kept as its own identifier and its digits are
    excluded from any phone comparison; see `_jid_digits`)."""
    if not is_lid(identifier):
        return None
    lid = identifier.lower()
    phone_jid = _load_lid_jid_map().get(lid)
    if phone_jid:
        return phone_jid
    return _lid_to_phone_via_links(conn, lid)


def _emails_in(text: str) -> list[str]:
    return [m.group(0).lower() for m in _EMAIL_RE.finditer(text or "")]


def _phones_in(text: str) -> list[str]:
    out = []
    for m in _PHONE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) >= 8:
            out.append(digits)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Candidate search
# ─────────────────────────────────────────────────────────────────────────────
def _strong_candidate(conn: sqlite3.Connection, message: Message, identifier: str) -> Optional[str]:
    """Return an existing person_id ONLY on a genuinely unambiguous signal, else None.

    memory-identity-4: a shared display name + shared corporate domain is NOT such a
    signal. Display names are trivially spoofable and a corporate domain is shared by
    every employee (and by shared mailboxes like sales@ / support@), so name+domain
    alone fused namesakes, role mailboxes, and spoofed senders into one trusted
    person. That case is now handled by _weak_candidate (a confirm-once SUGGESTION),
    NOT here. Only the two cryptographically-meaningful, self-asserted-by-the-real-
    -person signals remain strong: an exact email written into a WhatsApp body, and an
    exact phone in an email signature matching a known JID."""
    body = message.body_text or message.snippet or ""

    # (a) incoming WhatsApp message that literally contains a known person's email.
    if is_jid(identifier):
        for email in _emails_in(body):
            pid = repo.person_link_get(conn, email)
            if pid:
                return pid

    # (b) incoming email whose signature contains a phone matching a known JID.
    if is_email(identifier):
        for digits in _phones_in(body):
            pid = repo.person_link_by_phone_digits(conn, digits)
            if pid:
                return pid

    # (c) REMOVED — name+domain is no longer an auto-merge (see _weak_candidate).
    return None


def _person_is_freemail(person_row) -> bool:
    """True when a candidate person has NO corporate-domain signal (company==''),
    i.e. it was created from a free-mail address (gmail/yahoo/...) or from a JID.

    memory-identity-5: a free-mail person carries no company gate, so a bare
    display-name collision (extremely common on full names like 'Rahul Sharma')
    is the ONLY thing distinguishing it — and a display name is spoofable. Linking
    on name alone risks an irreversible one-tap merge of unrelated namesakes."""
    try:
        return not ((person_row["company"] or "").strip())
    except (KeyError, TypeError):
        return True


def _has_corroborating_signal(
    conn: sqlite3.Connection, message: Message, identifier: str, person_row
) -> bool:
    """memory-identity-5: a SECOND signal beyond the bare display name, proving the
    incoming sender and the candidate person are plausibly the same human. We accept
    any of these cross-references (all hard to fake on a common name):

      * the message body literally quotes an EMAIL the candidate already owns;
      * the message body quotes a PHONE whose digits match a JID the candidate owns;
      * the incoming JID's digits match a JID the candidate already owns
        (a near-miss the strong path didn't auto-link, e.g. country-code variance).

    A bare same-name collision with NONE of these returns False, so no suggestion is
    emitted for a free-mail namesake."""
    import json
    body = message.body_text or message.snippet or ""
    try:
        cand_emails = {e.lower() for e in json.loads(person_row["emails"] or "[]")}
        cand_jids = [j for j in json.loads(person_row["phone_jids"] or "[]")]
    except (ValueError, TypeError, KeyError):
        return False

    # (a) an email the candidate owns is quoted in the incoming body.
    if cand_emails and any(e in cand_emails for e in _emails_in(body)):
        return True

    # (b) a phone in the body matches one of the candidate's phone JIDs.
    body_phones = _phones_in(body)
    if body_phones:
        for j in cand_jids:
            jd = _jid_digits(j)  # '' for @lid / non-phone; never compared as a number
            if jd and any(repo.phone_digits_match(jd, p) for p in body_phones):
                return True

    # (c) the incoming JID's digits match a candidate JID (real-number overlap only).
    in_digits = _jid_digits(identifier)
    if in_digits:
        for j in cand_jids:
            jd = _jid_digits(j)
            if jd and repo.phone_digits_match(jd, in_digits):
                return True
    return False


def _weak_candidate(conn: sqlite3.Connection, message: Message, identifier: str) -> Optional[str]:
    """A plausible-but-uncertain match worth ASKING about once (a confirm suggestion,
    never a silent merge). Two conservative signals, each requiring a full name (2+
    tokens) and resolving to exactly ONE person:

      (1) cross-channel: the same full name on a person reachable on a DIFFERENT
          channel (e.g. a WhatsApp JID whose name matches an email person);
      (2) memory-identity-4: the same full name + the same non-generic corporate
          domain (downgraded from the old strong auto-merge). Display name + domain is
          spoofable / shared, so we ASK rather than fuse.

    Single-token / common first names are ignored (too noisy to ask).

    memory-identity-5: for path (1), a candidate with NO corporate domain
    (company==''; free-mail or JID-only) must NOT be suggested on a bare common-name
    match — that nudges the owner toward an irreversible one-tap merge of unrelated
    namesakes. Such a candidate is only offered when a SECOND signal corroborates the
    match (a shared email/phone cross-reference). Corporate-domain candidates keep the
    name-on-another-channel suggestion (the domain is the corroborating signal)."""
    import json
    name = _norm_name(message.sender_name)
    if len(name.split()) < 2:
        return None

    # (2) name + same corporate domain -> suggestion (was a strong auto-merge).
    company = _company_from(identifier)
    if company:
        nc = [p for p in repo.persons_by_name_company(conn, name, company)
              if not _person_owns(p, identifier)]
        if len(nc) == 1:
            return nc[0]["id"]

    # (1) cross-channel same-name match.
    incoming_jid = is_jid(identifier)
    candidates = []
    for p in repo.persons_by_name(conn, name):
        emails = json.loads(p["emails"] or "[]")
        jids = json.loads(p["phone_jids"] or "[]")
        other_channel = bool(emails) if incoming_jid else bool(jids)
        # don't suggest linking to a person who already owns this identifier
        if not (other_channel and not _person_owns(p, identifier)):
            continue
        # memory-identity-5: a bare common-name collision on a free-mail / no-domain
        # candidate needs a corroborating cross-reference; otherwise drop it so the
        # owner is never asked to merge two strangers who merely share a name.
        if _person_is_freemail(p) and not _has_corroborating_signal(conn, message, identifier, p):
            try:
                repo.record_event(
                    conn, type="identity_weaklink_suppressed", contact_email=identifier,
                    detail={"candidate_person_id": p["id"], "reason": "freemail_name_only",
                            "name": message.sender_name or ""},
                )
            except Exception:  # noqa: BLE001 - audit best-effort
                log.debug("identity: weaklink-suppress audit failed", exc_info=True)
            continue
        candidates.append(p["id"])
    return candidates[0] if len(candidates) == 1 else None


def _person_owns(person_row, identifier: str) -> bool:
    """True if this person already owns the given identifier (email or JID)."""
    import json
    ident = (identifier or "").lower()
    try:
        emails = [e.lower() for e in json.loads(person_row["emails"] or "[]")]
        jids = [j.lower() for j in json.loads(person_row["phone_jids"] or "[]")]
    except (ValueError, TypeError, KeyError):
        return False
    return ident in emails or ident in jids


# ─────────────────────────────────────────────────────────────────────────────
# Person creation / identifier attachment
# ─────────────────────────────────────────────────────────────────────────────
def _create_person(conn: sqlite3.Connection, message: Message, identifier: str) -> str:
    import json

    pid = uuid.uuid4().hex
    emails = [identifier] if is_email(identifier) else []
    jids = [identifier] if is_jid(identifier) else []
    # last-ditch: identifiers that are neither clean email nor jid still get stored as email-ish
    if not emails and not jids:
        emails = [identifier]
    repo.person_add(
        conn, person_id=pid, display_name=(message.sender_name or identifier),
        emails=emails, phone_jids=jids, company=_company_from(identifier),
    )
    repo.person_link_set(conn, identifier, pid, confidence=1.0, source="observed")
    # keep the JSON-import warning quiet for linters
    _ = json
    return pid


def _attach_identifier(
    conn: sqlite3.Connection, person_id: str, identifier: str, *, source: str = "strong", confidence: float = 1.0
) -> None:
    import json

    p = repo.person_get(conn, person_id)
    if p is None:
        repo.person_link_set(conn, identifier, person_id, confidence=confidence, source=source)
        return
    emails = json.loads(p["emails"] or "[]")
    jids = json.loads(p["phone_jids"] or "[]")
    if is_jid(identifier):
        if identifier not in [j.lower() for j in jids]:
            jids.append(identifier)
    else:
        if identifier not in [e.lower() for e in emails]:
            emails.append(identifier)
    repo.person_update(conn, person_id, emails=emails, phone_jids=jids)
    repo.person_link_set(conn, identifier, person_id, confidence=confidence, source=source)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def person_id_for(conn: sqlite3.Connection, identifier: str) -> Optional[str]:
    """Lookup-only resolution (no creation). Used by recorder/dispatcher."""
    if not identifier:
        return None
    return repo.person_link_get(conn, (identifier or "").lower())


def _record_lid_resolution(
    conn: sqlite3.Connection, lid: str, phone_jid: str, person_id: str, mode: str
) -> None:
    """Observability for memory-identity-3: a @lid alias was unified onto its
    canonical phone JID's person. Previously this unification silently FAILED
    (duplicate person), so make every success traceable/reviewable. Best-effort."""
    try:
        repo.record_event(
            conn, type="identity_lid_resolved", contact_email=lid,
            detail={"lid": lid, "phone_jid": phone_jid,
                    "person_id": person_id, "mode": mode},
        )
    except Exception:  # noqa: BLE001 - audit is best-effort, never block resolution
        log.debug("identity: lid-resolution audit event failed", exc_info=True)


def resolve(conn: sqlite3.Connection, message: Message) -> Resolution:
    """Resolve a message's sender to a person_id, creating/linking as needed.

    Returns a Resolution; if a one-time link SUGGESTION was created, it's attached so
    the caller can surface it to Jatin. Never raises on normal data; the caller still
    wraps this so any failure degrades to thread-only classification."""
    identifier = (message.sender_email or "").lower().strip()
    if not identifier:
        return Resolution(person_id="", created=False)

    # 1) existing active link (check the raw identifier — a @lid that was linked
    #    before, or any other identifier, short-circuits here).
    existing = repo.person_link_get(conn, identifier)
    if existing:
        return Resolution(person_id=existing, created=False)

    # 1b) memory-identity-3: canonicalize a @lid (WhatsApp LinkedID) to its real
    #     phone JID BEFORE any matching. Without this, the @lid reaches resolution
    #     UNRESOLVED: phone-digit unification (person_link_by_phone_digits matches
    #     only @s.whatsapp.net rows) silently fails and a duplicate person is born
    #     for a contact already known by email/phone. We resolve via the relay's
    #     lid_jid_map (or an already-linked person), then operate on the canonical
    #     JID — and we record BOTH the @lid alias and the phone JID on the person
    #     so future messages on either form resolve to one human.
    lid_alias = ""
    if is_lid(identifier):
        canonical = resolve_lid_to_phone_jid(conn, identifier)
        if canonical and canonical != identifier:
            lid_alias = identifier
            # If the canonical phone JID is already a known person, fold the @lid
            # alias onto it (the reconciliation confirm_suggestion would do, but
            # automatic here because the relay's mapping is an authoritative,
            # WhatsApp-asserted equivalence — not a spoofable display name).
            phone_person = repo.person_link_get(conn, canonical)
            if phone_person:
                _attach_identifier(conn, phone_person, lid_alias, source="lid_resolved")
                _record_lid_resolution(conn, lid_alias, canonical, phone_person, "fold")
                return Resolution(person_id=phone_person, created=False)
            # Not yet known by phone JID either: proceed with the canonical JID as
            # the identifier so creation/linking key on the dialable number.
            identifier = canonical
            try:
                message.sender_email = canonical
            except Exception:  # noqa: BLE001 - message may be frozen; matching still uses `identifier`
                pass

    # 2) strong, unambiguous auto-link
    strong = _strong_candidate(conn, message, identifier)
    if strong:
        _attach_identifier(conn, strong, identifier, source="strong")
        log.info("identity: strong-linked %s -> person %s", identifier, strong)
        # Audit trail: every auto-merge is recorded so a wrong link is reviewable/undoable
        # (IDENTITY_SAFETY observability — previously merges left no trace).
        try:
            repo.record_event(
                conn, type="identity_autolink", contact_email=identifier,
                detail={"person_id": strong, "signal": "strong",
                        "sender_name": message.sender_name or ""},
            )
        except Exception:  # noqa: BLE001 - audit is best-effort, never block resolution
            log.debug("identity: autolink audit event failed", exc_info=True)
        # memory-identity-3: also bind the original @lid alias to this person so a
        # later message arriving in @lid form resolves directly (no re-resolution).
        if lid_alias:
            _attach_identifier(conn, strong, lid_alias, source="lid_resolved")
            _record_lid_resolution(conn, lid_alias, identifier, strong, "strong")
        return Resolution(person_id=strong, created=False)

    # 3) create a new person; maybe record a one-time suggestion for a weak match
    weak = _weak_candidate(conn, message, identifier)
    new_pid = _create_person(conn, message, identifier)
    # memory-identity-3: bind the @lid alias to the freshly-created person too, so
    # both the LinkedID and the canonical phone JID point at one human.
    if lid_alias:
        _attach_identifier(conn, new_pid, lid_alias, source="lid_resolved")
        _record_lid_resolution(conn, lid_alias, identifier, new_pid, "new")
    suggestion = None
    if weak and not repo.suggestion_exists(conn, identifier, weak):
        sid = uuid.uuid4().hex
        # The match is by display name (cross-channel, or same corporate domain). Both
        # are plausible but spoofable, so we ASK rather than auto-merge.
        reason = f"same name '{message.sender_name}' as an existing contact"
        repo.suggestion_add(
            conn, suggestion_id=sid, identifier_new=identifier,
            candidate_person_id=weak, reason=reason, confidence=0.5,
        )
        cand = repo.person_get(conn, weak)
        suggestion = {
            "id": sid, "identifier_new": identifier, "candidate_person_id": weak,
            "reason": reason,
            "candidate_name": (cand["display_name"] if cand else "") or weak,
        }
        log.info("identity: suggested link %s ?= person %s", identifier, weak)
    return Resolution(person_id=new_pid, created=True, suggestion=suggestion)


def confirm_suggestion(conn: sqlite3.Connection, suggestion_id: str) -> bool:
    """Jatin said YES: merge identifier_new's person into the candidate person."""
    s = repo.suggestion_get(conn, suggestion_id)
    if s is None or s["status"] != "pending":
        return False
    if not repo.suggestion_set_status(conn, suggestion_id, "confirmed"):
        return False
    import json

    candidate = s["candidate_person_id"]
    ident = s["identifier_new"]
    old_pid = repo.person_link_get(conn, ident)
    _attach_identifier(conn, candidate, ident, source="confirmed", confidence=1.0)
    # Fold the just-created person's other identifiers into the candidate, then drop it.
    if old_pid and old_pid != candidate:
        old = repo.person_get(conn, old_pid)
        if old is not None:
            for e in json.loads(old["emails"] or "[]"):
                _attach_identifier(conn, candidate, e, source="confirmed")
            for j in json.loads(old["phone_jids"] or "[]"):
                _attach_identifier(conn, candidate, j, source="confirmed")
            # memory-identity-2: MIGRATE the merged-away person's accumulated memory
            # and person_id-keyed state to the survivor BEFORE deleting it. Without
            # this, person_delete silently destroyed the relationship_memory record
            # (and orphaned commitments/threads/opportunities) we built over time.
            _migrate_person_state(conn, from_pid=old_pid, to_pid=candidate)
            repo.person_delete(conn, old_pid)
    log.info("identity: confirmed link %s -> person %s", ident, candidate)
    return True


# Other person_id-keyed tables whose rows must follow the surviving person on a merge
# (best-effort: a table absent from this DB is skipped). person_links is handled by
# _attach_identifier; relationship_memory is merged specially (it has one row per
# person and must be combined, not blindly re-pointed onto a possibly-existing row).
_PERSON_ID_TABLES = ("commitments", "threads", "opportunities", "fact_metadata")


def _migrate_person_state(conn: sqlite3.Connection, *, from_pid: str, to_pid: str) -> None:
    """Move all person-keyed state from a merged-away person onto the survivor so no
    accumulated memory is lost on a confirm-merge. Best-effort and defensive: any one
    table failing never aborts the rest, and the function never raises into the caller.
    """
    if not from_pid or not to_pid or from_pid == to_pid:
        return
    # 1) Relationship memory: combine the two records (survivor wins on key conflicts,
    #    but the merged-away person's facts/situations/history are preserved).
    try:
        _merge_relationship_memory(conn, from_pid=from_pid, to_pid=to_pid)
    except Exception as exc:  # noqa: BLE001 - never lose the rest of the merge
        log.warning("identity: relationship_memory merge failed (non-fatal): %s", exc)
    # 2) Re-point the remaining person_id-keyed rows. UPDATE-by-key is safe even when
    #    the survivor already has rows (commitments/threads/opportunities allow many).
    for table in _PERSON_ID_TABLES:
        try:
            if table not in _table_names(conn):
                continue
            conn.execute(
                f"UPDATE {table} SET person_id=? WHERE person_id=?", (to_pid, from_pid)
            )
        except sqlite3.Error as exc:
            log.debug("identity: re-point %s skipped (non-fatal): %s", table, exc)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _merge_relationship_memory(conn: sqlite3.Connection, *, from_pid: str, to_pid: str) -> None:
    """Fold the merged-away person's relationship_memory into the survivor's, then
    delete the dead row. The survivor's facts win on a key conflict (it is the
    confirmed identity), but every fact, open situation, decision, episode, superseded
    entry, and provenance record from BOTH is preserved. Uses the distill load/save
    helpers (which already handle the additive provenance column)."""
    from assistant.memory import distill as distill_mod

    src = distill_mod.load_memory(conn, from_pid)
    if src.is_empty() and not (src.summary or src.provenance):
        return  # nothing to carry over
    dst = distill_mod.load_memory(conn, to_pid)

    # Facts + provenance: survivor wins on conflict; otherwise inherit from the dead one.
    for k, v in src.summary.items():
        if k not in dst.summary:
            dst.summary[k] = v
            src_prov = src.provenance.get(k) if isinstance(src.provenance, dict) else None
            if isinstance(src_prov, dict):
                if not isinstance(dst.provenance, dict):
                    dst.provenance = {}
                dst.provenance[k] = dict(src_prov)

    # Open situations: keep both, de-duped by key (survivor first).
    seen_keys = {s.get("key") for s in dst.open_situations}
    for s in src.open_situations:
        if s.get("key") not in seen_keys:
            dst.open_situations.append(s)
            seen_keys.add(s.get("key"))

    # Decisions: append-only, de-duped by decision text.
    seen_dec = {d.get("decision") for d in dst.decided}
    for d in src.decided:
        if d.get("decision") not in seen_dec:
            dst.decided.append(d)
            seen_dec.add(d.get("decision"))

    # Episodes + superseded audit: concatenate, then cap to the module limits so the
    # merged record stays compact (newest kept).
    dst.episodes = (dst.episodes + src.episodes)[-distill_mod._MAX_EPISODES:]
    dst.superseded = (dst.superseded + src.superseded)[-distill_mod._MAX_SUPERSEDED:]

    # Keep the freshest distilled-at and the higher version counter.
    dst.last_distilled_at = max(
        (x for x in (dst.last_distilled_at, src.last_distilled_at) if x is not None),
        default=dst.last_distilled_at,
    )
    dst.version = max(int(dst.version or 0), int(src.version or 0)) + 1

    distill_mod.save_memory(conn, dst)
    # Drop the now-merged source row so it can't be resurrected or double-counted.
    try:
        conn.execute("DELETE FROM relationship_memory WHERE person_id=?", (from_pid,))
    except sqlite3.Error:
        pass


def reject_suggestion(conn: sqlite3.Connection, suggestion_id: str) -> bool:
    """Jatin said NO: remember the rejection so this pair is never asked again."""
    ok = repo.suggestion_set_status(conn, suggestion_id, "rejected")
    if ok:
        log.info("identity: rejected link suggestion %s (remembered)", suggestion_id)
    return ok
