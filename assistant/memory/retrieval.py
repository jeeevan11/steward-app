"""Scoped retrieval: assemble ONLY the context relevant to this message.

Scoped to the contact first (their profile, your commitments to them, contact-
specific rules), then category rules, then a minimal global tail. Deliberately
does NOT dump the whole memory into the prompt.

Stdlib only — testable against an in-memory DB.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from assistant.memory import rules as rules_mod
from assistant.models import Contact, Thread
from assistant.storage import repositories as repo

# A relationship record older than this is treated as possibly out of date: the new
# message is trusted over it, and a stale "resolved" no longer suppresses (Memory Part D).
MEMORY_STALE_DAYS = 30
_PERSONAL_RELATIONSHIPS = frozenset({
    "personal", "family", "friend", "spouse", "partner", "parent", "sibling", "wife", "husband",
})


@dataclass
class MemorySignals:
    """Deterministic, memory-derived hints for the tier engine (Memory Part C).
    Computed from the relationship record + episodes; no LLM involved."""
    recently_skipped: bool = False    # this thread was surfaced + skipped within the cooldown
    situation_resolved: bool = False  # this thread's situation is already marked resolved
    is_personal: bool = False         # a personal/family contact (never auto-handled)
    relationship_type: str = "unknown"  # GAP 1 — inferred relationship_type for guardrail floors


@dataclass
class RetrievedContext:
    contact: Contact
    rules: list[str] = field(default_factory=list)        # most-specific first
    commitments: list[str] = field(default_factory=list)  # things you owe this person
    profile_summary: str = ""
    thread_stats: dict[str, int] = field(default_factory=dict)
    calendar_note: str = ""   # P4a — one-line calendar context, set by the caller
    person_id: str = ""        # Memory Part C — resolved cross-channel person
    memory_block: str = ""     # Memory Part C — what we already know about this person
    recent_conversation: str = ""  # Layer 1C — rolling recent chat history (WhatsApp)
    graph_block: str = ""      # Fix 4 — graph context: who is waiting on them, shared connections

    def render_for_prompt(self) -> str:
        """Compact text block for the classifier/drafter system prompt."""
        lines: list[str] = []
        lines.append(f"SENDER: {self.contact.name or self.contact.email} <{self.contact.email}>")
        # Memory first: read the new message in light of what we already know.
        if self.memory_block:
            lines.append(self.memory_block)
        if self.recent_conversation:
            lines.append(self.recent_conversation)
        if self.profile_summary:
            lines.append(self.profile_summary)
        if self.contact.flags:
            lines.append("Flags: " + ", ".join(sorted(self.contact.flags)))
        if self.commitments:
            lines.append("Your outstanding commitments to them:")
            lines.extend(f"  - {c}" for c in self.commitments)
        if self.rules:
            lines.append("Standing rules that apply (most specific first):")
            lines.extend(f"  - {r}" for r in self.rules)
        if self.graph_block:
            lines.append(self.graph_block)
        if self.calendar_note:
            lines.append(self.calendar_note)
        if not (self.profile_summary or self.commitments or self.rules or self.memory_block):
            lines.append("No prior memory for this sender (treat as a new contact).")
        return "\n".join(lines)


def _profile_summary(contact: Contact) -> str:
    bits: list[str] = []
    # Lead with an explicit recognition verdict so the judge never guesses "unknown sender"
    # for someone the owner has actually saved. A bare push-name is NOT saved (spoofable).
    if getattr(contact, "is_saved", False):
        disp = (contact.name or "").strip()
        bits.append(f"SAVED contact (a known person in your world){f', name: {disp}' if disp else ''}")
    else:
        bits.append("NOT a saved contact (unrecognized / unsaved sender)")
    if contact.relationship and contact.relationship not in ("wa_contact",):
        rel = "saved in contacts" if contact.relationship == "phone_contact" else contact.relationship
        bits.append(f"relationship: {rel}")
    if contact.importance:
        bits.append(f"importance: {contact.importance}/100")
    if contact.reply_rate:
        bits.append(f"you reply to ~{round(contact.reply_rate * 100)}% of their mail")
    if contact.msg_count:
        bits.append(f"{contact.msg_count} prior messages")
    return "Profile: " + "; ".join(bits)


def _commitments_from_notes(notes: str) -> list[str]:
    """Extract 'I owe them …' lines from the free-form notes field.

    Convention: lines beginning with 'COMMIT:' are outstanding commitments.
    """
    out: list[str] = []
    for line in (notes or "").splitlines():
        line = line.strip()
        if line.upper().startswith("COMMIT:"):
            out.append(line[len("COMMIT:"):].strip())
    return out


def get_context(
    conn: sqlite3.Connection, thread: Thread, contact: Contact, category: str = ""
) -> RetrievedContext:
    """Build the scoped context for one message/thread."""
    return RetrievedContext(
        contact=contact,
        rules=rules_mod.relevant_rules(conn, contact, category),
        commitments=_commitments_from_notes(contact.notes),
        profile_summary=_profile_summary(contact),
        thread_stats={
            "messages_in_thread": len(thread.messages),
            "participants": len(thread.participants()),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Memory layer (Memory Part C): build the MEMORY CONTEXT block, deterministic
# nudge signals, and the agent's episodic action log. All best-effort.
# ─────────────────────────────────────────────────────────────────────────────
def _is_stale(mem, now: float, days: int = MEMORY_STALE_DAYS) -> bool:
    last = getattr(mem, "last_distilled_at", None)
    return bool(last) and (now - float(last)) > days * 86400


def _fact_source_type(mem, key: str) -> str:
    """Provenance source_type for a fact key. Prefers the RelationshipMemory helper;
    falls back to reading a `provenance` dict; defaults to the most-distrusted
    ('claimed') when nothing is recorded. memory-knowledge-1."""
    helper = getattr(mem, "fact_source_type", None)
    if callable(helper):
        try:
            return helper(key)
        except Exception:  # noqa: BLE001
            pass
    prov = getattr(mem, "provenance", None)
    rec = prov.get(key) if isinstance(prov, dict) else None
    if isinstance(rec, dict):
        st = str(rec.get("source_type") or "").strip().lower()
        if st in ("claimed", "observed", "inferred", "verified"):
            return st
    return "claimed"


def _confidence_map(conn, person_id: str) -> dict:
    """memory-knowledge-2: per-fact confidence for the read-side gate, in one query.
    Returns {} (no gate) when conn/person_id are absent or governance is unavailable —
    so existing callers that pass neither keep the exact prior behavior. Never raises."""
    if conn is None or not person_id:
        return {}
    try:
        from assistant.memory import governance
        return governance.fact_confidences(conn, person_id)
    except Exception:  # noqa: BLE001 - gate is additive; absence means render unchanged
        return {}


def build_memory_block(
    mem, *, cap: int = 1800, now: float | None = None,
    conn=None, person_id: str = "",
) -> str:
    """Render a COMPACT, capped memory block for the THINK/JUDGE prompt. The newest
    message always wins on any conflict (stated in the block). Never dumps raw email.

    memory-knowledge-1: facts are split by provenance. ESTABLISHED facts (observed /
    inferred / verified) are presented as what we know; merely-CLAIMED facts (a
    counterparty's self-assertion) are listed SEPARATELY and explicitly labelled
    unverified, so the brain never treats a bare claim as established truth.

    memory-knowledge-2: when (conn, person_id) are supplied, governance is consulted on
    the READ path (it previously had zero readers). A fact whose tracked confidence has
    decayed below governance.EXPIRY_MIN_CONFIDENCE is DEMOTED out of the trusted "Facts:"
    line into a clearly-labelled low-confidence line, so a stale/decayed/poisoned fact no
    longer masquerades as established truth in the prompt. A fact with no metadata row is
    untracked and rendered unchanged (governance only ever demotes what it actually scored).
    The gate degrades to a no-op if conn/person_id are absent or governance is unavailable.

    memory-knowledge-7: the "do not re-open" decided block is gated on staleness — a stale
    record softens to "previously noted (may be outdated)" instead of an absolute
    instruction the brain must honor, so an aged (possibly non-literal) decision cannot
    permanently bias prioritization of a genuine later opportunity."""
    try:
        if mem is None or mem.is_empty():
            return ""
        now = now if now is not None else time.time()
        stale = _is_stale(mem, now)
        lines = [
            "=== WHAT YOU ALREADY KNOW ABOUT THIS PERSON (memory) ===",
            "Read the new message in this light. RECENCY RULE: if the latest message "
            "contradicts this memory on a plain fact, the latest message wins.",
        ]
        if stale:
            lines.append("(This memory may be out of date. If the new message implies "
                         "things have moved on, trust the message.)")
        if mem.summary:
            items = list(mem.summary.items())[:12]
            # memory-knowledge-2: confidence gate. A fact below the expiry floor is demoted
            # out of the trusted line regardless of provenance. Threshold + lookup come from
            # governance so this read gate and the write-side forgetting agree on the floor.
            conf = _confidence_map(conn, person_id)
            try:
                from assistant.memory import governance as _gov
                low_floor = _gov.EXPIRY_MIN_CONFIDENCE
            except Exception:  # noqa: BLE001
                low_floor = 0.25
            low_conf = [(k, v) for k, v in items
                        if (k in conf and conf[k] < low_floor)]
            low_keys = {k for k, _ in low_conf}
            kept = [(k, v) for k, v in items if k not in low_keys]
            established = [(k, v) for k, v in kept if _fact_source_type(mem, k) != "claimed"]
            claimed = [(k, v) for k, v in kept if _fact_source_type(mem, k) == "claimed"]
            if established:
                facts = "; ".join(f"{k}: {v}" for k, v in established)
                lines.append(f"Facts: {facts}")
            if claimed:
                # NEVER presented as truth: these are the counterparty's own claims.
                claims = "; ".join(f"{k}: {v}" for k, v in claimed)
                lines.append(
                    "Unverified claims by them (they asserted these about themselves; "
                    "do NOT treat as established fact without corroboration): " + claims
                )
            if low_conf:
                # Faded memories: rendered, but explicitly low-confidence so the brain
                # treats them as weak hints and the recency rule easily overrides them.
                faded = "; ".join(f"{k}: {v}" for k, v in low_conf)
                lines.append(
                    "Faded / low-confidence (unreinforced for a long time; treat as a weak "
                    "hint only, the new message easily overrides): " + faded
                )
        open_now = [s for s in mem.open_situations if s.get("status") != "resolved"]
        if open_now:
            lines.append("Open right now:")
            lines.extend(
                f"  - {s.get('situation', '')} (awaiting {s.get('awaiting', '?')})"
                for s in open_now[:6]
            )
        if mem.decided:
            # memory-knowledge-7: only a FRESH record gets the absolute "do not re-open"
            # framing. A stale record (or one with no last_distilled_at anchor) softens to
            # "previously noted" so an aged or possibly non-literal decision cannot
            # permanently suppress a genuine later opportunity.
            if stale:
                lines.append("Previously noted (may be outdated; if the new message "
                             "reopens this seriously, treat the new message as current):")
            else:
                lines.append("Already decided (do not re-open):")
            lines.extend(f"  - {d.get('decision', '')}" for d in mem.decided[-6:])
        recent = mem.episodes[-4:]
        if recent:
            lines.append("Recently you (the assistant):")
            lines.extend(
                f"  - {e.get('action', '')}"
                + (f" (tier {e.get('tier')})" if e.get("tier") is not None else "")
                for e in recent
            )
        return "\n".join(lines)[:cap]
    except Exception:  # noqa: BLE001 - never let block-building break the pipeline
        return ""


def _person_is_personal(conn: sqlite3.Connection, person_id: str, contact: Contact) -> bool:
    """True if this PERSON should never be auto-handled — the resolved contact is
    flagged personal, OR any identifier linked to the person is, OR the person's
    relationship is a family/personal one. Cross-channel by design."""
    if contact and contact.has_flag("personal"):
        return True
    if not person_id:
        return False
    try:
        p = repo.person_get(conn, person_id)
        if p is None:
            return False
        if (p["relationship"] or "").strip().lower() in _PERSONAL_RELATIONSHIPS:
            return True
        idents = json.loads(p["emails"] or "[]") + json.loads(p["phone_jids"] or "[]")
        for ident in idents:
            c = repo.get_contact(conn, ident)
            if c and c.has_flag("personal"):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def memory_signals(conn: sqlite3.Connection, person_id: str, thread: Thread, contact: Contact,
                   settings, *, mem=None, now: float | None = None) -> MemorySignals:
    """Deterministic hints for the tier engine. Never raises (returns empty signals)."""
    sig = MemorySignals(is_personal=_person_is_personal(conn, person_id, contact))
    try:
        sig.relationship_type = repo.person_relationship_type(conn, person_id)
    except Exception:  # noqa: BLE001 - additive signal
        sig.relationship_type = "unknown"
    try:
        if not person_id:
            return sig
        from assistant.memory import distill as distill_mod
        mem = mem if mem is not None else distill_mod.load_memory(conn, person_id)
        now = now if now is not None else time.time()
        cooldown = int(getattr(settings, "memory_nudge_cooldown_hours", 24)) * 3600
        tid = thread.id
        sig.recently_skipped = any(
            e.get("thread_id") == tid and e.get("action") == "skipped"
            and (now - float(e.get("ts") or 0)) < cooldown
            for e in mem.episodes
        )
        # A "resolved" situation only suppresses if the record is FRESH. If the memory
        # is stale, trust the new message instead of an outdated "resolved" (Part D).
        if not _is_stale(mem, now):
            sig.situation_resolved = any(
                s.get("thread_id") == tid and s.get("status") == "resolved"
                for s in mem.open_situations
            )
    except Exception:  # noqa: BLE001 - degrade to no suppression
        pass
    return sig


def record_episode(conn: sqlite3.Connection, person_id: str, *, action: str,
                   tier=None, thread_id: str = "", note: str = "") -> None:
    """Append one agent episode (surfaced/drafted/sent/skipped) to the person's record.
    Best-effort and capped; never raises."""
    if not person_id:
        return
    try:
        from assistant.memory import distill as distill_mod
        mem = distill_mod.load_memory(conn, person_id)
        mem.episodes.append({
            "action": action,
            "tier": (int(tier) if tier is not None else None),
            "ts": int(time.time()),
            "thread_id": thread_id,
            "note": note,
        })
        mem.episodes = mem.episodes[-distill_mod._MAX_EPISODES:]
        distill_mod.save_memory(conn, mem)
    except Exception:  # noqa: BLE001
        pass
