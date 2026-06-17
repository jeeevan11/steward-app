"""The tier engine: combine the model's Decision, contact memory, and hard
guardrails into a single `FinalDecision`.

The decision is decomposed exactly as specified:
  (a) intent/category            — from the Decision
  (b) sender importance          — from memory (overrides the model upward)
  (c) stakes + reversibility     — from the Decision
  (d) final tier 0–3 + confidence

Ordering of forces (each can only RAISE involvement):
  1. base = model's proposed tier
  2. guardrail floor (investor/legal/money/irreversible) — see guardrails.py
  3. VIP/importance floor from memory
  4. confidence gate: low confidence on a CONSEQUENTIAL item ⇒ surface (ASK)
  5. conservative calibration: don't act autonomously on non-trivial items
     unless confidence is high — earn silent action with evidence

Pure function (no I/O); thresholds are passed in so tests need no Settings.
"""

from __future__ import annotations

from dataclasses import dataclass

from assistant.brain import guardrails
from assistant.models import (
    CONSEQUENTIAL_CATEGORIES,
    Contact,
    Decision,
    FinalDecision,
    Reversibility,
    Stakes,
    Thread,
    Tier,
)


@dataclass(frozen=True)
class TierConfig:
    vip_importance_threshold: int = 70
    surface_confidence_threshold: float = 0.75
    autonomy_confidence_threshold: float = 0.85
    conservative: bool = True

    @classmethod
    def from_settings(cls, settings) -> "TierConfig":  # type: ignore[no-untyped-def]
        return cls(
            vip_importance_threshold=settings.vip_importance_threshold,
            surface_confidence_threshold=settings.surface_confidence_threshold,
            autonomy_confidence_threshold=settings.autonomy_confidence_threshold,
            conservative=True,
        )


def _suppress_reason(active: bool, resolved: bool, skipped: bool, deprioritized: bool) -> str:
    if active:
        return "you're handling this conversation — staying silent (still tracked)"
    if resolved:
        return "memory: situation already resolved — not re-opening"
    if skipped:
        return "memory: recently surfaced and skipped — not nudging again"
    return "learned: you've repeatedly skipped this sender — surfacing more quietly"


def decide(
    thread: Thread,
    decision: Decision,
    contact: Contact,
    config: TierConfig | None = None,
    *,
    memory=None,
    suppress_active: bool = False,
    deprioritized: bool = False,
) -> FinalDecision:
    """``suppress_active`` (Layer 1B): the owner is handling this conversation himself
    right now → go silent (still tracked). ``deprioritized`` (Layer 1E): learned from
    repeated skips → surface more quietly. Both are LOWERING forces with the exact same
    safety clamps as memory nudge-suppression: never below the guardrail floor, never
    for VIP / personal / memory-conflict / high-stakes / irreversible items."""
    config = config or TierConfig()

    base = Tier.clamp(decision.proposed_tier)
    tier = base
    applied_floors: list[str] = []
    surfaced_reason: str | None = None

    def raise_to(level: Tier, why: str, *, is_surface: bool = False) -> None:
        nonlocal tier, surfaced_reason
        if level > tier:
            tier = level
            if is_surface and surfaced_reason is None:
                surfaced_reason = why
        applied_floors.append(why)

    # 2) Hard guardrails (cannot be overridden by the model).
    g = guardrails.evaluate(thread, decision, contact, memory=memory)
    if g.floor > base:
        for r in g.reasons:
            applied_floors.append(f"guardrail: {r}")
    if g.floor > tier:
        tier = g.floor

    # 3) VIP / importance floor from memory (memory overrides the model upward).
    effective_importance = max(decision.sender_importance, contact.importance)
    if contact.is_vip(config.vip_importance_threshold):
        # VIP contacts are NEVER auto-handled — always surface for a human tap.
        raise_to(Tier.APPROVE, "VIP sender — always requires approval")
    elif effective_importance >= config.vip_importance_threshold and decision.needs_reply:
        raise_to(Tier.APPROVE, f"high-importance sender ({effective_importance}) needs a reply")

    # 4) Confidence gate: low confidence on a consequential item ⇒ surface, don't act.
    consequential = (
        tier >= Tier.APPROVE
        or decision.stakes == Stakes.HIGH
        or decision.reversibility != Reversibility.REVERSIBLE
        or decision.category in CONSEQUENTIAL_CATEGORIES
    )
    if consequential and decision.confidence < config.surface_confidence_threshold:
        raise_to(
            Tier.ASK,
            f"low confidence ({decision.confidence:.2f}) on a consequential item",
            is_surface=True,
        )

    # 5) Conservative calibration: don't act autonomously on anything beyond
    #    clearly-low-stakes noise unless we're confident. Reversible low-stakes
    #    items (newsletters, promos) are still allowed through silently so we
    #    don't bury you in confirmations — that would defeat zero cognitive load.
    if (
        config.conservative
        and tier <= Tier.FYI
        and decision.stakes != Stakes.LOW
        and decision.confidence < config.autonomy_confidence_threshold
    ):
        raise_to(
            Tier.APPROVE,
            f"calibration: not yet confident enough ({decision.confidence:.2f}) "
            f"to act on a {decision.stakes}-stakes item autonomously",
            is_surface=True,
        )

    # A fail-safe decision is always ASK (guardrails already floor it; belt & braces).
    if decision.is_failsafe and tier < Tier.ASK:
        raise_to(Tier.ASK, "classifier fail-safe", is_surface=True)

    # 6) Memory-aware NUDGE SUPPRESSION (Memory Part C). This is the ONLY force that
    #    can LOWER the tier, and it is hemmed in hard:
    #      * only when memory shows the situation is known-and-handled (recently
    #        surfaced+skipped, or already resolved);
    #      * NEVER below the guardrail floor `g.floor`;
    #      * NEVER for an irreversible / high-stakes / memory-conflicting item;
    #      * NEVER turning a Tier-3 (ASK) item into a silent action (min FYI).
    #    This is how "knowing the context" REDUCES nudging without ever weakening safety.
    mem_skipped = memory is not None and getattr(memory, "recently_skipped", False)
    mem_resolved = memory is not None and getattr(memory, "situation_resolved", False)
    if mem_skipped or mem_resolved or suppress_active or deprioritized:
        safe_to_suppress = (
            not getattr(decision, "memory_conflict", False)
            and not (memory is not None and getattr(memory, "is_personal", False))  # personal never quieted
            and not contact.is_vip(config.vip_importance_threshold)  # VIP = always-instant
            and decision.stakes != Stakes.HIGH
            and decision.reversibility == Reversibility.REVERSIBLE
        )
        if safe_to_suppress and int(tier) > int(g.floor):
            # presence (you're handling it) → as silent as safety allows; a resolved/
            # skipped low-stakes no-reply item likewise; everything else (feedback
            # deprioritize, or a memory item that still wants a reply) → quiet FYI.
            if suppress_active or (
                (mem_skipped or mem_resolved) and decision.stakes == Stakes.LOW
                and not decision.needs_reply
            ):
                target, reason = Tier.SILENT, _suppress_reason(
                    suppress_active, mem_resolved, mem_skipped, deprioritized)
            else:
                target, reason = Tier.FYI, _suppress_reason(
                    suppress_active, mem_resolved, mem_skipped, deprioritized)
            min_allowed = int(g.floor)
            # CARDINAL RULE (needs-attention is never auto-handled): a message that wants a
            # reply is never silenced to a no-trace SILENT action — at most a quiet FYI the
            # owner can still see. Previously only a Tier-3 ASK was protected, so a Tier-2
            # needs-reply message (a known-but-not-yet-learned-VIP contact, or any 1:1 the owner
            # was recently active in) was demoted straight to SILENT — the live Maya/Sam
            # silencing. needs_reply generalizes the existing Tier-3 guard to that whole class.
            if int(tier) >= int(Tier.ASK) or decision.needs_reply:
                min_allowed = max(min_allowed, int(Tier.FYI))  # needs-reply / Tier-3 never silent
            lowered = max(int(target), min_allowed)
            if lowered < int(tier):
                tier = Tier.clamp(lowered)
                applied_floors.append(reason)

    # 7) Per-contact MUTE rule ("never bother me about this person"). Applied LAST so no
    #    earlier raise can re-open it — but, like every lowering force here, it can NEVER
    #    drop below the hard guardrail floor `g.floor`. So a muted spammer is silently
    #    handled, while a muted contact who suddenly sends something genuinely
    #    consequential (money/legal/irreversible) still surfaces. VIP always wins.
    if contact.is_muted() and not contact.is_vip(config.vip_importance_threshold):
        floored = max(int(Tier.SILENT), int(g.floor))
        if floored < int(tier):
            tier = Tier.clamp(floored)
            applied_floors.append("muted contact — silently handled (never pings)")

    return FinalDecision(
        final_tier=Tier.clamp(tier),
        base_tier=base,
        confidence=decision.confidence,
        decision=decision,
        applied_floors=applied_floors,
        surfaced_reason=surfaced_reason,
    )
