"""Core domain models shared across every layer.

These are channel-agnostic on purpose: `Message`/`Thread` describe a unit of
communication, not "an email". When the WhatsApp relay is added later it produces
the same `Message` objects and the brain/action/control layers are unchanged.

Stdlib only — safe to import from tests and from the testable core.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Channels & tiers
# ─────────────────────────────────────────────────────────────────────────────
class Channel:
    GMAIL = "gmail"
    WHATSAPP = "whatsapp"  # Phase 2 — reserved, not implemented


class Tier(IntEnum):
    """How much human involvement an item needs. Lower = more autonomous."""

    SILENT = 0      # archive / label silently (reversible only)
    FYI = 1         # act + a one-line FYI to Telegram
    APPROVE = 2     # draft a reply, send to Telegram with Approve / Edit / Skip
    ASK = 3         # send context + suggestion and wait for the human

    @classmethod
    def clamp(cls, value: int) -> "Tier":
        return cls(min(3, max(0, int(value))))


class Stakes:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    ALL = (LOW, MEDIUM, HIGH)


class Reversibility:
    REVERSIBLE = "reversible"
    HARD = "hard_to_reverse"
    IRREVERSIBLE = "irreversible"
    ALL = (REVERSIBLE, HARD, IRREVERSIBLE)


# Closed set of categories the classifier may use. Kept small and stable so rules
# and guardrails can key off them deterministically.
CATEGORIES = (
    "spam_promotional",
    "newsletter",
    "automated_notification",
    "transactional_receipt",
    "social",
    "personal",
    "work_request",
    "scheduling",
    "financial",
    "legal",
    "investor",
    "other",
    "unknown",  # only ever set by the fail-safe path
)

# Categories that are inherently consequential — guardrails floor these to >= APPROVE.
CONSEQUENTIAL_CATEGORIES = frozenset({"financial", "legal", "investor"})


# ─────────────────────────────────────────────────────────────────────────────
# People
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Contact:
    """A resolved sender, backed by the contacts table (or a thin default)."""

    email: str
    name: str = ""
    relationship: str = ""           # e.g. "investor", "cofounder", "vendor", "friend"
    importance: int = 0              # 0..100; drives VIP floor
    # subset of {"investor","legal","vip","mute","personal","alumni"}
    flags: set[str] = field(default_factory=set)
    reply_rate: float = 0.0          # fraction of their mail you have replied to
    avg_response_seconds: Optional[float] = None
    msg_count: int = 0
    notes: str = ""                  # free-form: who they are, your commitments to them
    name_source: str = ""            # saved|business|manual|push|unknown — provenance of `name`

    def has_flag(self, name: str) -> bool:
        return name in self.flags

    @property
    def is_saved(self) -> bool:
        """A genuinely SAVED/known contact — never reads as 'unknown'. True when the name has
        trustworthy provenance (phone book / WA-verified / owner saved in-app), or there is an
        explicit relationship/flags signal. A bare push-name does NOT count (spoofable)."""
        return bool(
            self.name_source in ("saved", "business", "manual")
            or (self.relationship or "").strip() == "phone_contact"
            or self.flags
            or (self.notes or "").strip()
        )

    def is_vip(self, threshold: int) -> bool:
        """VIP = 'always-instant': bypasses the settling delay, never quieted, always
        floored to surface. Either an explicit flag or a high learned importance."""
        return self.importance >= threshold or "vip" in self.flags

    def is_muted(self) -> bool:
        """MUTE = 'never bother me': silently handled, never pings — but still subject
        to the hard guardrail floor, so a genuinely consequential message surfaces."""
        return "mute" in self.flags


# ─────────────────────────────────────────────────────────────────────────────
# Messages & threads
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Attachment:
    filename: str
    mime_type: str = ""
    size: int = 0
    extracted_text: str = ""   # best-effort text (e.g. from a PDF); may be empty


@dataclass
class Message:
    """A single inbound/outbound message, normalized from the channel."""

    id: str                          # channel message id (Gmail message id)
    thread_id: str
    channel: str = Channel.GMAIL
    sender_email: str = ""
    sender_name: str = ""
    recipients: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    reply_to: str = ""               # RFC 5322 Reply-To header; replies route here over From
    subject: str = ""
    body_text: str = ""
    snippet: str = ""
    timestamp: float = 0.0           # epoch seconds
    labels: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    from_me: bool = False            # true for messages you sent (voice mining)


@dataclass
class Thread:
    """A full conversation. The brain always reasons over the WHOLE thread."""

    id: str
    channel: str = Channel.GMAIL
    subject: str = ""
    messages: list[Message] = field(default_factory=list)

    @property
    def latest(self) -> Optional[Message]:
        return self.messages[-1] if self.messages else None

    @property
    def latest_inbound(self) -> Optional[Message]:
        for m in reversed(self.messages):
            if not m.from_me:
                return m
        return None

    def participants(self) -> set[str]:
        out: set[str] = set()
        for m in self.messages:
            if m.sender_email:
                out.add(m.sender_email.lower())
            out.update(r.lower() for r in m.recipients)
        return out

    def render_for_prompt(self, max_chars: int = 12000) -> str:
        """A compact, oldest→newest plaintext rendering for the LLM."""
        parts: list[str] = []
        for m in self.messages:
            who = "ME" if m.from_me else (m.sender_name or m.sender_email or "?")
            atts = ""
            if m.attachments:
                names = ", ".join(a.filename for a in m.attachments)
                atts = f"\n[attachments: {names}]"
            body = (m.body_text or m.snippet or "").strip()
            parts.append(f"From: {who}\nSubject: {m.subject}\n{body}{atts}")
        text = "\n\n---\n\n".join(parts)
        if len(text) > max_chars:
            # keep the most recent context (the tail) — that is what needs a reply
            text = "…[earlier truncated]…\n\n" + text[-max_chars:]
        return text


# ─────────────────────────────────────────────────────────────────────────────
# Brain output
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Decision:
    """The classifier's structured judgment about a thread. Schema-validated;
    an invalid model response becomes `Decision.failsafe(...)`."""

    category: str = "unknown"
    intent: str = ""
    sender_importance: int = 0       # model's estimate; memory overrides at tier time
    stakes: str = Stakes.MEDIUM
    reversibility: str = Reversibility.REVERSIBLE
    proposed_tier: int = Tier.ASK
    confidence: float = 0.0
    needs_reply: bool = False
    reasoning: str = ""
    suggested_action: str = ""       # short machine-ish hint: "archive" | "label:Newsletters" | "reply" | "fyi"
    one_line_summary: str = ""       # for FYIs / briefs
    is_failsafe: bool = False
    # Memory Part C: the new message contradicts remembered facts/decisions/commitments
    # on something consequential. Set by the memory-aware JUDGE; a hard guardrail then
    # surfaces it (never act on the assumption). Defaults False so non-memory paths are
    # unaffected.
    memory_conflict: bool = False

    @classmethod
    def failsafe(cls, reason: str) -> "Decision":
        """Construct the safe default: surface to the human, never act."""
        return cls(
            category="unknown",
            intent="unknown",
            sender_importance=0,
            stakes=Stakes.HIGH,
            reversibility=Reversibility.IRREVERSIBLE,
            proposed_tier=Tier.ASK,
            confidence=0.0,
            needs_reply=True,
            reasoning=f"FAIL-SAFE: {reason}",
            suggested_action="ask",
            one_line_summary="Could not classify confidently — needs your eyes.",
            is_failsafe=True,
        )


@dataclass
class FinalDecision:
    """The result of the tier engine: what the system will actually do, and why.

    `final_tier` is authoritative; `applied_floors`/`surfaced_reason` make the
    decision auditable (every autonomous action is explainable)."""

    final_tier: Tier
    base_tier: Tier
    confidence: float
    decision: Decision
    applied_floors: list[str] = field(default_factory=list)
    surfaced_reason: Optional[str] = None  # set when something forced this up to the human

    @property
    def is_autonomous(self) -> bool:
        return self.final_tier in (Tier.SILENT, Tier.FYI)

    @property
    def needs_human(self) -> bool:
        return self.final_tier in (Tier.APPROVE, Tier.ASK)
