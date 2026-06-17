"""Relationship-memory distill loop (Memory Part B) — extract-and-update, not store-raw.

After a real interaction, a cheap model pass reads the current relationship_memory plus
the thread and returns OPERATIONS (ADD/UPDATE/DELETE/NOOP) on facts, open_situations,
and decided. We apply them with **recency winning** (a contradicting fact supersedes the
old one, which is archived for audit and never used for decisions) and keep the record
**compact** (hard size caps; raw email text is never stored).

Everything is best-effort: a bad model response leaves the record unchanged, and the
caller wraps this so memory can never break classification or a send.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from assistant.config import Settings
from assistant.llm import prompts
from assistant.llm.router import Task
from assistant.logging_setup import get_logger
from assistant.models import Thread
from assistant.storage import repositories as repo

log = get_logger("distill")

# Categories that are pure noise — never worth distilling a relationship from.
NOISE_CATEGORIES = frozenset({
    "spam_promotional", "newsletter", "automated_notification",
    "transactional_receipt", "social",
})

# Compactness caps (the memory must recall the relevant thing, not store everything).
_MAX_FACTS = 25
_MAX_FACT_LEN = 200
_MAX_OPEN = 12
_MAX_DECIDED = 25
_MAX_EPISODES = 20
_MAX_SUPERSEDED = 30
MEMORY_CHAR_CAP = 1800   # hard cap on the rendered memory block (used by Part C)
_THREAD_CHARS_FOR_DISTILL = 6000

DISTILL_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "facts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"op": {"type": "string"}, "key": {"type": "string"},
                           "value": {"type": "string"}},
            "required": ["op", "key", "value"]}},
        "open_situations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"op": {"type": "string"}, "key": {"type": "string"},
                           "situation": {"type": "string"}, "awaiting": {"type": "string"},
                           "status": {"type": "string"}},
            "required": ["op", "key", "situation", "awaiting", "status"]}},
        "decided": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            # memory-knowledge-7: `key` is the optional retract target for DELETE/UPDATE
            # ops (the exact prior decision text being retracted/corrected).
            "properties": {"op": {"type": "string"}, "decision": {"type": "string"},
                           "key": {"type": "string"},
                           "source_message_id": {"type": "string"}},
            "required": ["op", "decision", "source_message_id"]}},
    },
    "required": ["facts", "open_situations", "decided"],
}


# ── memory-knowledge-1: MEMORY_PROVENANCE ────────────────────────────────────
# Every durable fact carries a provenance record so retrieval can tell an
# established truth from a counterparty's bare self-assertion. source_type is one
# of these; anything not in the set is treated as the most-distrusted ("claimed").
SOURCE_TYPES = ("claimed", "observed", "inferred", "verified")
# A merely-CLAIMED fact (the counterparty asserted it about themselves) must never
# be rendered to the brain as established truth.
UNTRUSTED_SOURCE_TYPES = frozenset({"claimed"})
# Trust ordering for the confidence/corroboration gate (memory-knowledge-3): a fact
# may only be SILENTLY superseded by a source at least as trusted as the incumbent.
_SOURCE_RANK = {"claimed": 0, "inferred": 1, "observed": 2, "verified": 3}

# Default source_type for a distilled fact when the caller does not classify it.
# The distill pass reads the counterparty's own message, so an unclassified fact is
# a self-assertion until something corroborates it: default to the distrusted tier.
DEFAULT_SOURCE_TYPE = "claimed"


def normalize_source_type(source_type: Any) -> str:
    """Coerce any input to a known source_type; unknown/empty -> most-distrusted."""
    st = str(source_type or "").strip().lower()
    return st if st in SOURCE_TYPES else DEFAULT_SOURCE_TYPE


def source_rank(source_type: Any) -> int:
    """Numeric trust rank (higher = more trusted). Unknown -> claimed's rank."""
    return _SOURCE_RANK.get(normalize_source_type(source_type), 0)


# ── memory-knowledge-7: non-literal gate ─────────────────────────────────────
# ROOT CAUSE: a `decided` op distilled from a joke/sarcasm/hypothetical ("haha so
# it's decided, we're never doing the deal") was appended as a permanent,
# un-deletable "do not re-open" decision. The model side is instructed (prompts/
# distill.md) not to record non-literal content; this is the BELT-AND-SUSPENDERS
# code-side gate so a slip never durably records a non-serious decision.
#
# Conservative by design: it only drops a `decided` ADD when the decision text itself
# carries an explicit non-literal/hypothetical/banter marker. It NEVER touches facts,
# situations, or a DELETE/UPDATE retraction, and a genuinely serious decision phrased
# plainly is untouched. When a marker is present the decision is skipped (and logged),
# so the worst failure mode is "did not record a banter line", never "recorded it".
_NONLITERAL_MARKERS = (
    "haha", "lol", "lmao", "jk", "just kidding", "kidding", "joking", "sarcas",
    "sarcastic", "hypothetical", "hypothetically", "for the meme", "/s", "rofl",
    "as a joke", "obviously joking", "not serious", "tongue in cheek",
)
# Conditional/hypothetical framings that mean a decision was not actually made.
_HYPOTHETICAL_PHRASES = (
    "if we ever", "what if", "imagine if", "in theory", "theoretically",
    "would have", "could have", "might have", "pretend",
)


def is_nonliteral_decision(text: Any) -> bool:
    """True if a decision string looks like banter/sarcasm/a hypothetical rather than a
    real, serious commitment. Used to gate `decided` ADDs (memory-knowledge-7). Pure +
    deterministic; defaults to False (record it) on anything it cannot classify."""
    s = str(text or "").lower()
    if not s.strip():
        return False
    for m in _NONLITERAL_MARKERS:
        if m in s:
            return True
    for p in _HYPOTHETICAL_PHRASES:
        if p in s:
            return True
    return False


def filter_nonliteral_decisions(ops: dict) -> dict:
    """Return `ops` with non-literal `decided` ADD ops removed (memory-knowledge-7).

    Only ADD ops are filtered — a DELETE/UPDATE retraction must always be honored even if
    it references banter, so a wrongly-recorded decision can still be removed. Best-effort:
    a malformed ops dict is returned unchanged. Mutates nothing the caller still owns; it
    builds a fresh `decided` list."""
    try:
        decided = ops.get("decided")
        if not isinstance(decided, list):
            return ops
        kept = []
        dropped = 0
        for op in decided:
            if (isinstance(op, dict)
                    and str(op.get("op", "ADD")).upper() == "ADD"
                    and is_nonliteral_decision(op.get("decision"))):
                dropped += 1
                continue
            kept.append(op)
        if dropped:
            log.info("distill gate: dropped %d non-literal decision ADD(s)", dropped)
        out = dict(ops)
        out["decided"] = kept
        return out
    except Exception:  # noqa: BLE001 - gate is additive
        return ops


@dataclass
class RelationshipMemory:
    person_id: str
    summary: dict = field(default_factory=dict)          # key -> short fact
    open_situations: list = field(default_factory=list)  # [{key, situation, awaiting, status, last_activity_ts}]
    decided: list = field(default_factory=list)          # [{decision, ts, source_message_id}]
    episodes: list = field(default_factory=list)         # [{action, tier, ts, note, thread_id}] (Part C)
    superseded: list = field(default_factory=list)       # [{fact, value, superseded_at, reason}]
    # memory-knowledge-1: key -> {source, source_type, ts}. Parallel to `summary`;
    # a fact missing here is treated as the most-distrusted (claimed) by retrieval.
    provenance: dict = field(default_factory=dict)
    last_distilled_at: Optional[int] = None
    version: int = 0

    def is_empty(self) -> bool:
        return not (self.summary or self.open_situations or self.decided or self.episodes)

    def fact_source_type(self, key: str) -> str:
        """Provenance source_type for a fact key (default 'claimed' when unknown)."""
        rec = self.provenance.get(key) if isinstance(self.provenance, dict) else None
        if isinstance(rec, dict):
            return normalize_source_type(rec.get("source_type"))
        return DEFAULT_SOURCE_TYPE

    def is_claimed(self, key: str) -> bool:
        """True if this fact is a merely-asserted (untrusted) counterparty claim."""
        return self.fact_source_type(key) in UNTRUSTED_SOURCE_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# Load / save
# ─────────────────────────────────────────────────────────────────────────────
def _loads(s: Any, default):
    try:
        v = json.loads(s) if s else default
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


def load_memory(conn: sqlite3.Connection, person_id: str) -> RelationshipMemory:
    """Always returns a RelationshipMemory (empty if none stored). Never raises."""
    row = None
    try:
        row = repo.relationship_memory_get(conn, person_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("relationship_memory_get failed: %s", exc)
    if row is None:
        return RelationshipMemory(person_id=person_id)
    return RelationshipMemory(
        person_id=person_id,
        summary=_loads(row["summary_json"], {}),
        open_situations=_loads(row["open_situations_json"], []),
        decided=_loads(row["decided_json"], []),
        episodes=_loads(row["episodes_json"], []),
        superseded=_loads(row["superseded_json"], []),
        provenance=_provenance_from_row(row),
        last_distilled_at=row["last_distilled_at"],
        version=int(row["version"] or 0),
    )


def _provenance_from_row(row: Any) -> dict:
    """Read the additive provenance_json column defensively. Pre-migration rows (no
    such column) and bad JSON both degrade to an empty provenance map."""
    try:
        keys = row.keys() if hasattr(row, "keys") else []
        if "provenance_json" in keys:
            return _loads(row["provenance_json"], {})
    except (TypeError, IndexError, KeyError):
        pass
    return {}


def save_memory(conn: sqlite3.Connection, mem: RelationshipMemory) -> None:
    repo.relationship_memory_upsert(
        conn, mem.person_id,
        summary_json=json.dumps(mem.summary),
        open_situations_json=json.dumps(mem.open_situations),
        decided_json=json.dumps(mem.decided),
        episodes_json=json.dumps(mem.episodes),
        superseded_json=json.dumps(mem.superseded),
        last_distilled_at=mem.last_distilled_at,
        version=mem.version,
    )
    # memory-knowledge-1: persist provenance into the additive column WITHOUT
    # touching repositories.relationship_memory_upsert (owned elsewhere). The upsert
    # above guarantees the row exists; this writes only the new column. Best-effort:
    # an older DB without the column degrades silently (provenance simply not stored).
    try:
        conn.execute(
            "UPDATE relationship_memory SET provenance_json=? WHERE person_id=?",
            (json.dumps(mem.provenance or {}), mem.person_id),
        )
    except sqlite3.OperationalError:
        log.debug("provenance_json column absent (pre-migration); skipping provenance persist")


# ─────────────────────────────────────────────────────────────────────────────
# Apply operations (pure, deterministic, defensive) — recency wins
# ─────────────────────────────────────────────────────────────────────────────
def parse_ops(raw: Any) -> dict:
    """Parse the model's operations. Bad input → empty ops (record stays unchanged)."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (ValueError, TypeError):
        return {"facts": [], "open_situations": [], "decided": []}
    if not isinstance(data, dict):
        return {"facts": [], "open_situations": [], "decided": []}
    return {
        "facts": data.get("facts") if isinstance(data.get("facts"), list) else [],
        "open_situations": data.get("open_situations") if isinstance(data.get("open_situations"), list) else [],
        "decided": data.get("decided") if isinstance(data.get("decided"), list) else [],
    }


def apply_ops(
    mem: RelationshipMemory, ops: dict, *, now: int, thread_id: Optional[str] = None,
    source_type: str = DEFAULT_SOURCE_TYPE, source: str = "",
    conn: Any = None,
) -> RelationshipMemory:
    """Apply ADD/UPDATE/DELETE/NOOP. A changed fact supersedes the old value (recency)
    SUBJECT to the confidence/corroboration gate below. When thread_id is given,
    situations touched here are stamped with it so the brain can later tell whether
    THIS thread's situation is already resolved.

    memory-knowledge-2: when `conn` is supplied, the over-cap eviction at the end consults
    governance confidence and drops the WEAKEST facts first (was oldest-inserted). Optional
    and best-effort — callers that pass no conn keep the prior insertion-order behavior.

    memory-knowledge-7: the `decided` list now honors DELETE/UPDATE (supersede/retract) ops
    in addition to ADD, so a decision recorded from non-literal text (a joke/hypothetical)
    or one that a later message reverses can be retracted into `superseded` rather than
    living forever as an un-deletable "do not re-open" instruction.

    memory-knowledge-1: each ADD/UPDATE stamps provenance {source, source_type, ts}
    on the fact so retrieval can tell a verified fact from a counterparty's claim.
    The default source_type is the most-distrusted ('claimed') because the distill
    pass reads the counterparty's own message; callers who KNOW a fact is observed
    from the owner's behaviour (or verified) pass that in.

    memory-knowledge-3: a single low-trust assertion must NOT silently overwrite a
    higher-trust established fact. When an UPDATE would change a fact whose recorded
    source is MORE trusted than the incoming source, we do NOT overwrite the
    established value; instead the disputed claim is parked (recorded in `superseded`
    as a 'disputed_claim') and the established fact stands. Equal-or-higher trust
    still wins by recency.
    """
    st = normalize_source_type(source_type)
    incoming_rank = source_rank(st)
    for op in ops.get("facts", []):
        if not isinstance(op, dict):
            continue
        action = str(op.get("op", "NOOP")).upper()
        key = str(op.get("key", "")).strip()
        if not key or action == "NOOP":
            continue
        if action in ("ADD", "UPDATE"):
            val = str(op.get("value", "")).strip()
            if not val:
                continue
            old = mem.summary.get(key)
            if old is not None and old != val:
                # memory-knowledge-3: gate the supersede on relative trust. A
                # less-trusted source (e.g. a counterparty CLAIM) cannot silently
                # bury a more-trusted established fact (observed/verified).
                incumbent_rank = source_rank(mem.fact_source_type(key))
                if incoming_rank < incumbent_rank:
                    mem.superseded.append({
                        "fact": key, "value": val, "superseded_at": now,
                        "reason": "disputed_claim",
                        "kept_value": old,
                        "claim_source_type": st,
                        "incumbent_source_type": mem.fact_source_type(key),
                    })
                    log.info(
                        "distill gate: kept higher-trust fact %r (%s) over %s claim",
                        key, mem.fact_source_type(key), st,
                    )
                    continue  # established fact stands; provenance unchanged
                mem.superseded.append({"fact": key, "value": old, "superseded_at": now, "reason": "updated"})
            mem.summary[key] = val
            _stamp_provenance(mem, key, source_type=st, source=source, now=now)
        elif action == "DELETE" and key in mem.summary:
            # memory-knowledge-3: a low-trust source cannot delete a higher-trust fact.
            incumbent_rank = source_rank(mem.fact_source_type(key))
            if incoming_rank < incumbent_rank:
                mem.superseded.append({
                    "fact": key, "value": mem.summary[key], "superseded_at": now,
                    "reason": "disputed_delete", "claim_source_type": st,
                    "incumbent_source_type": mem.fact_source_type(key),
                })
                continue
            mem.superseded.append({"fact": key, "value": mem.summary[key], "superseded_at": now, "reason": "deleted"})
            del mem.summary[key]
            mem.provenance.pop(key, None)

    for op in ops.get("open_situations", []):
        if not isinstance(op, dict):
            continue
        action = str(op.get("op", "NOOP")).upper()
        key = str(op.get("key", "")).strip()
        if not key or action == "NOOP":
            continue
        existing = next((s for s in mem.open_situations if s.get("key") == key), None)
        if action in ("ADD", "UPDATE"):
            rec = existing or {"key": key}
            if op.get("situation"):
                rec["situation"] = str(op["situation"])
            rec["awaiting"] = str(op.get("awaiting") or rec.get("awaiting", "nobody"))
            rec["status"] = str(op.get("status") or rec.get("status", "open"))
            rec["last_activity_ts"] = now
            if thread_id:
                rec["thread_id"] = thread_id
            if existing is None:
                mem.open_situations.append(rec)
        elif action == "DELETE":
            mem.open_situations = [s for s in mem.open_situations if s.get("key") != key]

    for op in ops.get("decided", []):
        if not isinstance(op, dict):
            continue
        action = str(op.get("op", "ADD")).upper()
        dec = str(op.get("decision", "")).strip()
        if action == "ADD":
            if dec and not any(d.get("decision") == dec for d in mem.decided):
                mem.decided.append({"decision": dec, "ts": now,
                                    "source_message_id": str(op.get("source_message_id", ""))})
        elif action in ("DELETE", "UPDATE"):
            # memory-knowledge-7: a decision is no longer append-only-forever. A later
            # DELETE retracts a prior decision (e.g. one distilled from a joke/hypothetical
            # that was never a real commitment); an UPDATE retracts the old one and records
            # the corrected decision. Retracted decisions are archived into `superseded`
            # (audit trail) and stop being rendered as "do not re-open". Match by an
            # explicit `key`/`target` if given, else by the decision text being removed.
            target = str(op.get("key") or op.get("target") or op.get("supersedes")
                         or op.get("old_decision") or "").strip()
            removed = []
            kept = []
            for d in mem.decided:
                dtext = str(d.get("decision", ""))
                hit = (dtext == target) if target else (dtext == dec)
                if hit:
                    removed.append(d)
                else:
                    kept.append(d)
            if removed:
                mem.decided = kept
                for d in removed:
                    mem.superseded.append({
                        "decision": d.get("decision", ""),
                        "superseded_at": now,
                        "reason": "decision_retracted" if action == "DELETE"
                                  else "decision_updated",
                    })
                log.info("retracted %d decision(s) via %s", len(removed), action)
            # UPDATE also records the corrected decision (when one is provided and new).
            if action == "UPDATE" and dec and not any(
                d.get("decision") == dec for d in mem.decided
            ):
                mem.decided.append({"decision": dec, "ts": now,
                                    "source_message_id": str(op.get("source_message_id", ""))})

    # memory-knowledge-2: build a governance confidence map (best-effort) so over-cap
    # eviction drops the weakest facts first. Absent conn -> None -> prior behavior.
    conf_map = None
    if conn is not None:
        try:
            from assistant.memory import governance
            conf_map = governance.fact_confidences(conn, mem.person_id)
        except Exception:  # noqa: BLE001 - eviction policy is additive
            conf_map = None
    _enforce_caps(mem, conf_map)
    return mem


def _stamp_provenance(
    mem: RelationshipMemory, key: str, *, source_type: str, source: str, now: int
) -> None:
    """Record/refresh the provenance for a fact key (memory-knowledge-1).

    Strengthening rule: re-seeing a fact NEVER downgrades its trust. If a fact was
    previously observed/verified and the same value is re-asserted by a mere claim,
    the recorded (higher) trust is kept — a counterparty repeating a claim does not
    turn it into an established truth, but neither does it erase prior corroboration.
    """
    if not isinstance(mem.provenance, dict):
        mem.provenance = {}
    st = normalize_source_type(source_type)
    prev = mem.provenance.get(key)
    if isinstance(prev, dict) and source_rank(prev.get("source_type")) > source_rank(st):
        # keep the higher-trust provenance; just refresh the timestamp
        prev["ts"] = now
        return
    mem.provenance[key] = {"source": str(source or ""), "source_type": st, "ts": now}


def _eviction_order(mem: RelationshipMemory, conf_map: Optional[dict]) -> list:
    """memory-knowledge-2: choose WHICH facts to drop when over the cap.

    ROOT CAUSE this fixes: the old policy dropped the OLDEST-INSERTED keys, so a
    stale/decayed/poisoned fact was preferentially KEPT and a newer, possibly
    higher-confidence one was evicted. When a governance confidence map is available, we
    instead evict the LOWEST-confidence (weakest) facts first, breaking ties toward
    insertion order (oldest first) — strong memories survive, weak ones make room. With no
    confidence map we fall back to the prior oldest-inserted order, so behavior is
    unchanged for callers that don't (or can't) supply governance state.

    Returns the keys in eviction priority order (drop from the FRONT of this list)."""
    keys = list(mem.summary)  # preserves insertion order (oldest first)
    if not conf_map:
        return keys
    # Stable sort by confidence ascending; an untracked key (no metadata) is treated as the
    # base/guess confidence so it is neither protected nor preferentially purged. Python's
    # sort is stable, so equal-confidence keys keep insertion order (oldest evicted first).
    default_conf = 0.5
    return sorted(keys, key=lambda k: conf_map.get(k, default_conf))


def _enforce_caps(mem: RelationshipMemory, conf_map: Optional[dict] = None) -> None:
    for k in list(mem.summary):
        v = str(mem.summary[k])
        if len(v) > _MAX_FACT_LEN:
            mem.summary[k] = v[:_MAX_FACT_LEN].rstrip() + "…"
    if len(mem.summary) > _MAX_FACTS:
        # memory-knowledge-2: evict weakest-by-confidence first (was oldest-inserted).
        order = _eviction_order(mem, conf_map)
        for k in order[: len(mem.summary) - _MAX_FACTS]:
            del mem.summary[k]
            mem.provenance.pop(k, None)
    mem.open_situations = sorted(
        mem.open_situations, key=lambda s: -(s.get("last_activity_ts") or 0)
    )[:_MAX_OPEN]
    mem.decided = mem.decided[-_MAX_DECIDED:]
    mem.episodes = mem.episodes[-_MAX_EPISODES:]
    mem.superseded = mem.superseded[-_MAX_SUPERSEDED:]
    # memory-knowledge-1: drop provenance entries with no surviving fact (keep maps aligned)
    if isinstance(mem.provenance, dict):
        for k in list(mem.provenance):
            if k not in mem.summary:
                mem.provenance.pop(k, None)


# ─────────────────────────────────────────────────────────────────────────────
# The distill pass
# ─────────────────────────────────────────────────────────────────────────────
def _mem_for_prompt(mem: RelationshipMemory) -> str:
    # memory-knowledge-1: split facts by provenance so the distill model does not
    # treat a counterparty's bare claim as an established fact when re-distilling.
    established = {k: v for k, v in mem.summary.items() if not mem.is_claimed(k)}
    claimed = {k: v for k, v in mem.summary.items() if mem.is_claimed(k)}
    return json.dumps({
        "facts": established,
        "claimed_by_them_unverified": claimed,
        "open_situations": [{k: s.get(k) for k in ("key", "situation", "awaiting", "status")}
                            for s in mem.open_situations],
        "decided": [d.get("decision") for d in mem.decided],
    })[:MEMORY_CHAR_CAP]


# ─────────────────────────────────────────────────────────────────────────────
# Relationship-type inference (GAP 1) — classify the relationship once we have signal
# ─────────────────────────────────────────────────────────────────────────────
RELATIONSHIP_TYPE_CHOICES = (
    "partner", "family", "investor", "mentor", "collaborator",
    "customer", "recruiter", "cold", "unknown",
)

# Minimum processed messages with a person before we ask the model to classify the
# relationship — below this there isn't enough signal and 'unknown' is the honest answer.
_MIN_MESSAGES_FOR_INFERENCE = 3


def _processed_message_count(conn: sqlite3.Connection, person_id: str) -> int:
    """How many of this person's identifiers' inbound messages we've processed. Counts
    decision_log rows whose sender_email maps to this person. Best-effort → 0."""
    try:
        p = repo.person_get(conn, person_id)
        if p is None:
            return 0
        idents = set()
        for col in ("emails", "phone_jids"):
            try:
                idents.update(e.lower() for e in json.loads(p[col] or "[]"))
            except (ValueError, TypeError, KeyError):
                pass
        if not idents:
            return 0
        placeholders = ",".join("?" for _ in idents)
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM decision_log WHERE lower(sender_email) IN ({placeholders})",
            tuple(idents),
        ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def infer_relationship_type(llm: Any, person: Any, recent_messages: str) -> str:
    """Ask the model to classify this person's relationship to the owner as exactly one
    of RELATIONSHIP_TYPE_CHOICES. Best-effort: any failure or unrecognized answer →
    'unknown' (never guesses)."""
    try:
        if isinstance(person, dict):
            name = person.get("display_name", "") or ""
            company = person.get("company", "") or ""
        else:
            name = (person["display_name"] if person is not None else "") or ""
            company = (person["company"] if person is not None else "") or ""
        choices = ", ".join(RELATIONSHIP_TYPE_CHOICES)
        system = (
            "You classify the relationship between the assistant's owner and a contact. "
            f"Respond with EXACTLY ONE word from this set: {choices}. "
            "Use 'partner' for a romantic partner/spouse, 'family' for relatives, "
            "'investor' for someone funding the owner's company, 'mentor' for an advisor, "
            "'collaborator' for a coworker/cofounder/peer working together, "
            "'customer' for a client/buyer, 'recruiter' for hiring outreach, "
            "'cold' for unsolicited strangers, and 'unknown' if there isn't enough signal. "
            "Output only the single word, lowercase, no punctuation."
        )
        user = (
            f"Contact name: {name or '(unknown)'}\n"
            f"Company/domain: {company or '(none)'}\n\n"
            f"Recent conversation context:\n{(recent_messages or '')[:3000]}"
        )
        raw = llm.complete_text(
            system_prefix=system, user_prompt=user, max_tokens=8, use_opus=False, effort="low",
        )
        word = (raw or "").strip().lower().split()[0].strip(".,!?\"'") if (raw or "").strip() else ""
        return word if word in RELATIONSHIP_TYPE_CHOICES else "unknown"
    except Exception as exc:  # noqa: BLE001 - inference is additive, never fatal
        log.debug("infer_relationship_type failed (non-fatal): %s", exc)
        return "unknown"


def maybe_infer_relationship_type(
    conn: sqlite3.Connection, llm: Any, person_id: str, thread: Thread,
) -> None:
    """If this person's relationship_type is still 'unknown' and we have enough message
    history (>3 processed), classify it once and persist. Best-effort; never raises."""
    try:
        if repo.person_relationship_type(conn, person_id) != "unknown":
            return
        if _processed_message_count(conn, person_id) <= _MIN_MESSAGES_FOR_INFERENCE:
            return
        person = repo.person_get(conn, person_id)
        if person is None:
            return
        context = thread.render_for_prompt(max_chars=_THREAD_CHARS_FOR_DISTILL) if thread else ""
        rel = infer_relationship_type(llm, person, context)
        if rel != "unknown":
            repo.set_person_relationship_type(conn, person_id, rel)
            log.info("inferred relationship_type=%s for person %s", rel, person_id)
            # Recalibrate importance for each of this person's identifiers now that we
            # know the relationship type (sets the floor). Best-effort.
            try:
                from assistant.memory import contacts as _contacts
                p = repo.person_get(conn, person_id)
                if p is not None:
                    for col in ("emails", "phone_jids"):
                        try:
                            for ident in json.loads(p[col] or "[]"):
                                _contacts.recompute_importance(conn, ident)
                        except (ValueError, TypeError, KeyError):
                            pass
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.debug("maybe_infer_relationship_type failed (non-fatal): %s", exc)


def distill(
    conn: sqlite3.Connection, llm: Any, settings: Settings, person_id: str, thread: Thread,
    *, now: Optional[int] = None,
) -> bool:
    """Update this person's relationship memory from the thread. Returns True if the
    record was written. Best-effort: any failure leaves memory untouched, never raises."""
    if not getattr(settings, "memory_distill_enabled", True) or not person_id or thread is None:
        return False
    now = now if now is not None else int(time.time())
    try:
        mem = load_memory(conn, person_id)
        system = prompts.load("distill", settings.prompts_dir)
        user = (
            "=== CURRENT MEMORY ===\n" + _mem_for_prompt(mem)
            + "\n\n=== LATEST CONVERSATION (oldest to newest) ===\n"
            + thread.render_for_prompt(max_chars=_THREAD_CHARS_FOR_DISTILL)
        )
        raw = llm.complete_json(
            task=Task.DISTILL, system_prefix=system, user_text=user, schema=DISTILL_JSON_SCHEMA,
        )
        ops = parse_ops(raw)
        # memory-knowledge-7: drop decisions distilled from non-literal banter/sarcasm/
        # hypotheticals BEFORE they are recorded (belt-and-suspenders to the prompt rule).
        ops = filter_nonliteral_decisions(ops)
        # memory-knowledge-1: facts distilled from the conversation are the
        # counterparty's own assertions until corroborated -> stamp them 'claimed'.
        # memory-knowledge-2: pass conn so over-cap eviction drops weakest facts first.
        apply_ops(mem, ops, now=now, thread_id=thread.id,
                  source_type=DEFAULT_SOURCE_TYPE, source=("thread:" + (thread.id or "")),
                  conn=conn)
        # Phase 6: record per-fact governance metadata (confidence/verification/timestamps)
        # so memories strengthen on repeat and fade without reinforcement. Best-effort.
        if getattr(settings, "memory_governance_enabled", True):
            try:
                from assistant.memory import governance
                for op in ops.get("facts", []):
                    if isinstance(op, dict) and str(op.get("op", "")).upper() in ("ADD", "UPDATE"):
                        k = str(op.get("key", "")).strip()
                        v = str(op.get("value", "")).strip()
                        if k and v:
                            state = governance.observe_fact(conn, person_id, k, v, now=now)
                            # memory-knowledge-3 corroboration: once the SAME value has
                            # been independently re-seen, a bare claim earns 'observed'
                            # trust (it is no longer a single unverified assertion).
                            if (k in mem.summary and mem.is_claimed(k)
                                    and int(state.get("verification_count") or 0) >= 1):
                                _stamp_provenance(mem, k, source_type="observed",
                                                  source="corroborated", now=now)
            except Exception:  # noqa: BLE001 - governance is additive
                pass
        mem.last_distilled_at = now
        mem.version += 1
        save_memory(conn, mem)
        # GAP 1: once a person has enough message history, classify their relationship
        # type (only when still 'unknown'). Additive; never affects the distill result.
        maybe_infer_relationship_type(conn, llm, person_id, thread)
        return True
    except Exception as exc:  # noqa: BLE001 - memory is additive; never break the caller
        log.warning("distill failed for person %s (non-fatal): %s", person_id, exc)
        return False


def distill_after_send(
    conn: sqlite3.Connection, llm: Any, settings: Settings, mail: Any, message_id: str,
) -> bool:
    """Distill after Jatin's reply is sent (post-card). Resolves the person from the
    message, re-fetches the thread, and distills. Fully best-effort."""
    try:
        if not getattr(settings, "memory_enabled", True) or mail is None or not message_id:
            return False
        from assistant.memory import identity
        from assistant.storage import decision_log

        d = decision_log.get(conn, message_id)
        sender = (d["sender_email"] if d is not None else "") or ""
        person_id = identity.person_id_for(conn, sender)
        if not person_id:
            return False
        src = mail.source_for(message_id) if hasattr(mail, "source_for") else mail
        thread = src.get_thread(message_id)
        return distill(conn, llm, settings, person_id, thread)
    except Exception as exc:  # noqa: BLE001
        log.warning("distill_after_send failed (non-fatal): %s", exc)
        return False
