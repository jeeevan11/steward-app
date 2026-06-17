"""Phase 6 — memory governance: confidence, decay, contradiction, and graceful forgetting.

The relationship record (Memory Part B) keeps a flat `summary` dict of facts (key -> short
string). That structure is great for recall but holds NO governance metadata — the Phase 1
audit flagged that a fact, once written, lives forever with no notion of how sure we are, how
often it was seen, when it was last confirmed, or whether a later observation contradicted it.

This module is an ADDITIVE SIDECAR. It does NOT change the flat `summary` structure. It keeps
a parallel `fact_metadata` table keyed by (person_id, fact_key) holding exactly the governance
fields the audit found missing: a confidence, created/updated/last-verified timestamps, a
verification count, a source count, and a value hash (so a changed value reads as a
contradiction rather than a silent overwrite).

GOVERNANCE PRINCIPLE
    Weak memories naturally fade and strong memories strengthen, with no permanent assumptions
    that were never reinforced:
      * first sight of a fact -> a modest base confidence (it is a guess until repeated);
      * the SAME value seen again -> verification_count++ and confidence rises asymptotically
        toward ~0.98 (a repeatedly observed fact is trustworthy);
      * a DIFFERENT value for the same key -> a contradiction: confidence is reset/lowered and
        the value hash updated (recency wins, but we no longer pretend to be sure);
      * time without reinforcement -> exponential half-life decay of confidence;
      * decayed-AND-stale facts -> reported by `expired_facts` as safe to forget (the caller,
        not this module, decides whether to prune them from the summary).

SAFETY-FLOOR NOTE
    Governance here only affects MEMORY confidence. It does NOT participate in tiering and it
    never lowers a guardrail floor. Personal/family facts may decay in confidence, but that
    decay can never weaken the surface-only safety floor (that floor is driven by the
    person/contact tiering in retrieval.py, not by these numbers). Decaying a memory's
    confidence to zero is not the same as forgetting that a contact is family.

Reuses the established storage idiom (own table via `ensure()`; best-effort; never raises) —
the same one `decision_explanations` and `confidence_calibration` use. Stdlib + sqlite3 only.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from typing import Any, Optional

from assistant.logging_setup import get_logger

log = get_logger("governance")

# Confidence band. A first sighting is a guess; repeated agreement strengthens toward the
# ceiling; an explicit verify() jumps to a high (but not absolute) confidence.
BASE_CONFIDENCE = 0.5
MAX_CONFIDENCE = 0.98
CONTRADICTION_CONFIDENCE = 0.3   # a freshly contradicted fact: recency wins but we are unsure
VERIFY_CONFIDENCE = 0.95         # explicit human/agent confirmation

# Default forgetting knobs (caller-overridable per call).
DEFAULT_HALF_LIFE_DAYS = 30
EXPIRY_MIN_CONFIDENCE = 0.25
EXPIRY_STALE_DAYS = 120

_DAY = 86400.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_metadata (
    person_id          TEXT NOT NULL,
    fact_key           TEXT NOT NULL,
    confidence         REAL NOT NULL DEFAULT 0.5,
    value_hash         TEXT,
    verification_count INTEGER NOT NULL DEFAULT 0,
    source_count       INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at         INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    last_verified_at   INTEGER,
    PRIMARY KEY (person_id, fact_key)
);
CREATE INDEX IF NOT EXISTS idx_factmeta_person ON fact_metadata(person_id);
CREATE INDEX IF NOT EXISTS idx_factmeta_conf ON fact_metadata(confidence);
"""


def ensure(conn: sqlite3.Connection) -> None:
    """Create the table if it doesn't exist (idempotent)."""
    conn.executescript(_SCHEMA)


# ── helpers ────────────────────────────────────────────────────────────────────
def _now(now: Optional[int]) -> int:
    return int(now if now is not None else time.time())


def _hash(value: Any) -> str:
    """Stable hash of a fact value, so a changed value reads as a contradiction."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _strengthen(confidence: float) -> float:
    """Asymptotic move toward MAX_CONFIDENCE — each repeat closes ~40% of the gap.

    Repeated observation of the SAME value strengthens the memory without ever claiming
    absolute certainty.
    """
    return confidence + (MAX_CONFIDENCE - confidence) * 0.4


def _clamp(c: float) -> float:
    if c < 0.0:
        return 0.0
    if c > 1.0:
        return 1.0
    return c


# ── observe / verify ───────────────────────────────────────────────────────────
def observe_fact(
    conn: sqlite3.Connection, person_id: str, key: str, value: Any, *,
    source_message_id: Optional[str] = None, now: Optional[int] = None,
) -> dict[str, Any]:
    """Record one observation of a fact and return its governance state.

    Three cases:
      * first sight        -> BASE_CONFIDENCE, created_at/updated_at set, source_count 1;
      * repeat, same value -> verification_count++, confidence strengthens (asymptotic),
                              last_verified_at + source_count bumped;
      * repeat, different   -> contradiction: confidence reset/lowered, value_hash + updated_at
                              bumped, verification_count reset (the streak is broken).

    Returns {confidence, verification_count, contradicted}. Best-effort; on any failure
    returns a degraded dict and never raises.
    """
    result = {"confidence": BASE_CONFIDENCE, "verification_count": 0, "contradicted": False}
    if not person_id or not key:
        return result
    try:
        ensure(conn)
        ts = _now(now)
        new_hash = _hash(value)
        row = conn.execute(
            "SELECT confidence, value_hash, verification_count, source_count "
            "FROM fact_metadata WHERE person_id=? AND fact_key=?",
            (person_id, key),
        ).fetchone()

        if row is None:
            # First sight: a modest base confidence — it is a guess until repeated.
            conn.execute(
                "INSERT INTO fact_metadata "
                "(person_id, fact_key, confidence, value_hash, verification_count, "
                " source_count, created_at, updated_at, last_verified_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (person_id, key, BASE_CONFIDENCE, new_hash, 0, 1, ts, ts, ts),
            )
            return {"confidence": BASE_CONFIDENCE, "verification_count": 0, "contradicted": False}

        old_conf = float(row["confidence"] if row["confidence"] is not None else BASE_CONFIDENCE)
        old_vc = int(row["verification_count"] or 0)
        old_sc = int(row["source_count"] or 0)

        if row["value_hash"] == new_hash:
            # Same value seen again -> strengthen.
            new_conf = _clamp(_strengthen(old_conf))
            new_vc = old_vc + 1
            conn.execute(
                "UPDATE fact_metadata SET confidence=?, verification_count=?, "
                " source_count=?, updated_at=?, last_verified_at=? "
                "WHERE person_id=? AND fact_key=?",
                (new_conf, new_vc, old_sc + 1, ts, ts, person_id, key),
            )
            return {"confidence": new_conf, "verification_count": new_vc, "contradicted": False}

        # Different value -> contradiction. Recency wins on the value, but we are no longer
        # sure: reset confidence low and break the verification streak.
        conn.execute(
            "UPDATE fact_metadata SET confidence=?, value_hash=?, verification_count=?, "
            " source_count=?, updated_at=?, last_verified_at=? "
            "WHERE person_id=? AND fact_key=?",
            (CONTRADICTION_CONFIDENCE, new_hash, 0, old_sc + 1, ts, ts, person_id, key),
        )
        return {"confidence": CONTRADICTION_CONFIDENCE, "verification_count": 0, "contradicted": True}
    except Exception:  # noqa: BLE001 - governance must never break the pipeline
        log.debug("observe_fact failed (non-fatal)", exc_info=True)
        return result


def verify(
    conn: sqlite3.Connection, person_id: str, key: str, now: Optional[int] = None
) -> bool:
    """Explicit human/agent confirmation of a fact -> bump confidence high + refresh
    last_verified_at + verification_count. Returns True if a row was updated. Best-effort."""
    if not person_id or not key:
        return False
    try:
        ensure(conn)
        ts = _now(now)
        cur = conn.execute(
            "UPDATE fact_metadata SET confidence=?, last_verified_at=?, updated_at=?, "
            " verification_count=verification_count+1 "
            "WHERE person_id=? AND fact_key=?",
            (VERIFY_CONFIDENCE, ts, ts, person_id, key),
        )
        return cur.rowcount > 0
    except Exception:  # noqa: BLE001
        log.debug("verify failed (non-fatal)", exc_info=True)
        return False


# ── contradiction lookup ────────────────────────────────────────────────────────
def detect_contradiction(
    conn: sqlite3.Connection, person_id: str, key: str, new_value: Any
) -> bool:
    """True if a DIFFERENT value is already on record for this key (a contradiction). False
    when there is no record yet or the value matches. Best-effort; never raises."""
    if not person_id or not key:
        return False
    try:
        ensure(conn)
        row = conn.execute(
            "SELECT value_hash FROM fact_metadata WHERE person_id=? AND fact_key=?",
            (person_id, key),
        ).fetchone()
        if row is None or row["value_hash"] is None:
            return False
        return row["value_hash"] != _hash(new_value)
    except Exception:  # noqa: BLE001
        return False


def fact_confidence(
    conn: sqlite3.Connection, person_id: str, key: str
) -> Optional[float]:
    """Current stored confidence for a fact, or None if it is not tracked. Best-effort."""
    if not person_id or not key:
        return None
    try:
        ensure(conn)
        row = conn.execute(
            "SELECT confidence FROM fact_metadata WHERE person_id=? AND fact_key=?",
            (person_id, key),
        ).fetchone()
        if row is None or row["confidence"] is None:
            return None
        return float(row["confidence"])
    except Exception:  # noqa: BLE001
        return None


def fact_confidences(
    conn: sqlite3.Connection, person_id: str
) -> dict[str, float]:
    """memory-knowledge-2: ALL tracked confidences for one person in a single query.

    Root cause this serves: the read path (retrieval.build_memory_block) used to render
    facts with NO confidence gate because fact_confidence() had zero callers. A per-key
    lookup there would be N queries per memory block on the hot classify/draft path; this
    returns the whole {fact_key: confidence} map in one round-trip so the gate is cheap.

    A fact with no metadata row is simply absent from the map (caller treats absent as
    'untracked' and renders it unchanged — governance only ever DEMOTES, never invents
    confidence it doesn't have). Best-effort; on any failure returns {} (no gate applied).
    """
    if not person_id:
        return {}
    try:
        ensure(conn)
        rows = conn.execute(
            "SELECT fact_key, confidence FROM fact_metadata WHERE person_id=?",
            (person_id,),
        ).fetchall()
        out: dict[str, float] = {}
        for r in rows:
            if r["confidence"] is not None:
                out[r["fact_key"]] = float(r["confidence"])
        return out
    except Exception:  # noqa: BLE001
        return {}


# ── decay + expiry ───────────────────────────────────────────────────────────────
def decay(
    conn: sqlite3.Connection, *, half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
    now: Optional[int] = None,
) -> int:
    """Apply exponential half-life decay to every fact's confidence based on the time since
    its last_verified_at (falling back to updated_at). A fact unreinforced for one half-life
    keeps half its confidence; weak unreinforced memories thus fade on their own.

    Returns the number of rows whose confidence was lowered. Best-effort; never raises.

    NOTE: this only lowers MEMORY confidence. It never touches tiering or a guardrail floor —
    a personal/family contact's safety floor does not live in this number.
    """
    if half_life_days <= 0:
        return 0
    try:
        ensure(conn)
        ts = _now(now)
        rows = conn.execute(
            "SELECT person_id, fact_key, confidence, last_verified_at, updated_at "
            "FROM fact_metadata"
        ).fetchall()
        adjusted = 0
        half_life_secs = half_life_days * _DAY
        for r in rows:
            conf = float(r["confidence"] if r["confidence"] is not None else 0.0)
            if conf <= 0.0:
                continue
            anchor = r["last_verified_at"] if r["last_verified_at"] is not None else r["updated_at"]
            if anchor is None:
                continue
            elapsed = ts - float(anchor)
            if elapsed <= 0:
                continue
            factor = math.pow(0.5, elapsed / half_life_secs)
            new_conf = _clamp(conf * factor)
            if new_conf < conf:
                conn.execute(
                    "UPDATE fact_metadata SET confidence=? "
                    "WHERE person_id=? AND fact_key=?",
                    (new_conf, r["person_id"], r["fact_key"]),
                )
                adjusted += 1
        return adjusted
    except Exception:  # noqa: BLE001
        log.debug("decay failed (non-fatal)", exc_info=True)
        return 0


def expired_facts(
    conn: sqlite3.Connection, *, min_confidence: float = EXPIRY_MIN_CONFIDENCE,
    stale_days: int = EXPIRY_STALE_DAYS, now: Optional[int] = None,
) -> list[tuple[str, str]]:
    """List (person_id, fact_key) for facts that are SAFE TO FORGET: confidence has decayed
    below the floor AND the fact has gone stale (not verified within stale_days). Both
    conditions must hold — a low-confidence-but-recent fact is still given a chance.

    This only REPORTS candidates. The caller decides whether to prune them from the flat
    summary; this module never deletes a fact on its own. Best-effort; never raises.
    """
    try:
        ensure(conn)
        ts = _now(now)
        cutoff = ts - stale_days * _DAY
        rows = conn.execute(
            "SELECT person_id, fact_key, confidence, last_verified_at, updated_at "
            "FROM fact_metadata WHERE confidence < ?",
            (min_confidence,),
        ).fetchall()
        out: list[tuple[str, str]] = []
        for r in rows:
            anchor = r["last_verified_at"] if r["last_verified_at"] is not None else r["updated_at"]
            if anchor is None or float(anchor) <= cutoff:
                out.append((r["person_id"], r["fact_key"]))
        return out
    except Exception:  # noqa: BLE001
        log.debug("expired_facts failed (non-fatal)", exc_info=True)
        return []


def drop_metadata(conn: sqlite3.Connection, person_id: str, key: str) -> None:
    """Delete the governance metadata row for one fact (after the caller has removed the
    fact from the flat summary). Best-effort; never raises."""
    if not person_id or not key:
        return
    try:
        ensure(conn)
        conn.execute(
            "DELETE FROM fact_metadata WHERE person_id=? AND fact_key=?",
            (person_id, key),
        )
    except Exception:  # noqa: BLE001
        log.debug("drop_metadata failed (non-fatal)", exc_info=True)


# ── read-side enforcement: actually FORGET expired facts ─────────────────────────
def forget_expired_facts(
    conn: sqlite3.Connection, *, distill_mod: Any = None,
    min_confidence: float = EXPIRY_MIN_CONFIDENCE, stale_days: int = EXPIRY_STALE_DAYS,
    now: Optional[int] = None, record_event: Any = None,
) -> int:
    """memory-knowledge-2: the WRITE half of wiring governance into the running pipeline.

    ROOT CAUSE this closes: `expired_facts()` had zero callers, so a fact whose confidence
    decayed below the floor AND went stale was reported as forgettable but NEVER actually
    removed — it kept being rendered into the THINK/JUDGE + drafting prompts forever, and a
    prompt-injection-planted fact lingered indefinitely. This function consumes
    expired_facts() and graceful-forgets each one: it deletes the key from the person's flat
    `summary` (and its provenance) via load/save, then deletes the fact_metadata row so the
    fact stops appearing on every future message.

    SAFETY: this only touches MEMORY facts. It never deletes a person, a contact, a rule,
    open_situations, decided, the ledger, or any safety floor — a personal/family tier is
    driven by tiering in retrieval.py, not by these confidences (see SAFETY-FLOOR NOTE atop
    this module). `distill_mod` is injected so tests can pass a fake and so we avoid a hard
    import cycle (memory.distill imports nothing from here at module load).

    Returns the number of facts forgotten. Best-effort; never raises.
    """
    forgotten = 0
    try:
        if distill_mod is None:
            from assistant.memory import distill as distill_mod  # local import: no cycle
        candidates = expired_facts(
            conn, min_confidence=min_confidence, stale_days=stale_days, now=now
        )
        if not candidates:
            return 0
        # Group by person so we load/save each person's record exactly once.
        by_person: dict[str, list[str]] = {}
        for person_id, key in candidates:
            by_person.setdefault(person_id, []).append(key)
        for person_id, keys in by_person.items():
            try:
                mem = distill_mod.load_memory(conn, person_id)
            except Exception:  # noqa: BLE001 - a bad record for one person never blocks others
                continue
            changed: list[str] = []
            for key in keys:
                if key in mem.summary:
                    del mem.summary[key]
                    if isinstance(getattr(mem, "provenance", None), dict):
                        mem.provenance.pop(key, None)
                    changed.append(key)
                # Even if the fact was already gone from the summary, clear its stranded
                # metadata so the row does not resurface as a candidate forever.
                drop_metadata(conn, person_id, key)
            if changed:
                try:
                    distill_mod.save_memory(conn, mem)
                    forgotten += len(changed)
                    # Observability: a silent forget is exactly what the audit flagged.
                    if callable(record_event):
                        try:
                            record_event(
                                conn, type="memory_fact_forgotten",
                                detail={"person_id": person_id, "keys": changed,
                                        "reason": "decayed_and_stale"},
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    log.info(
                        "forgot %d expired fact(s) for person %s: %s",
                        len(changed), person_id, changed,
                    )
                except Exception:  # noqa: BLE001
                    log.debug("save after forget failed (non-fatal)", exc_info=True)
        return forgotten
    except Exception:  # noqa: BLE001
        log.debug("forget_expired_facts failed (non-fatal)", exc_info=True)
        return forgotten
