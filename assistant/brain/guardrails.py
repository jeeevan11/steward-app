"""Hard guardrails that wrap the model.

These are deterministic rules that compute a *minimum* tier (a floor). They can
only ever RAISE the level of human involvement, never lower it. The model's
output cannot override them.

Non-negotiable invariants enforced here:
  * Mail from a contact flagged investor/legal, or matching money/legal keywords,
    can NEVER drop below Tier 2 (APPROVE) regardless of model output.
  * Anything the model marks irreversible (or hard-to-reverse) is floored so it is
    never acted on autonomously.
  * Consequential categories (financial/legal/investor) are floored to >= APPROVE.

Pure functions, stdlib only — heavily unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from assistant.models import (
    CONSEQUENTIAL_CATEGORIES,
    Contact,
    Decision,
    Reversibility,
    Stakes,
    Thread,
    Tier,
)

# Whole-word-ish keyword matching. Tuned to catch money/legal topics that must
# never be auto-handled. Intentionally broad — false positives just mean "ask me",
# which is the safe direction.
MONEY_KEYWORDS = (
    "invoice", "payment", "wire", "wire transfer", "bank", "iban", "swift",
    "routing number", "account number", "refund", "deposit", "salary", "payroll",
    "purchase order", "po number", "remittance", "ach", "credit card",
    "tax", "w-9", "w9", "1099", "term sheet", "valuation", "cap table",
    "equity", "investment", "funding", "wire instructions", "balance due",
    "overdue", "past due", "transaction", "crypto", "bitcoin", "usdc", "usdt",
)

LEGAL_KEYWORDS = (
    "contract", "agreement", "nda", "non-disclosure", "lawsuit", "subpoena",
    "litigation", "settlement", "legal", "counsel", "attorney", "lawyer",
    "liability", "breach", "termination clause", "indemnif", "compliance",
    "cease and desist", "court", "deposition", "msa", "sow", "dpa",
    "governing law", "arbitration", "intellectual property", "patent",
    "trademark", "copyright infringement",
)

# Phrases that signal an attempt to redirect money / change banking details —
# the classic business-email-compromise pattern. Always surface these.
HIGH_RISK_PHRASES = (
    "change", "update", "new account", "different account", "remit to",
    "send the funds", "urgent payment", "gift card",
)

# ─────────────────────────────────────────────────────────────────────────────
# Jatin Chhanwal deployment-specific signals (additive — only ever RAISE involvement).
# Founder/CEO of Acme Inc (product: Acme). These floors protect the things a
# two-month-old hardware+AI startup must never let the assistant auto-handle.
# ─────────────────────────────────────────────────────────────────────────────
# Investor/fundraising terms → ASK (Tier 3). Ambiguous words ("safe", "board") use
# disambiguating phrases so they don't floor half the inbox.
INVESTOR_KEYWORDS = (
    "term sheet", "cap table", "valuation", "dilution", "due diligence", "data room",
    "lead round", "follow-on", "safe note", "safe agreement", "post-money safe",
    "convertible note", "liquidation preference", "pro rata", "board seat",
    "board meeting", "board member", "board observer",
)
# Substrings that, if present in a participant's email domain, mark an investor firm.
INVESTOR_FIRM_DOMAINS = (
    "a venture firm", "andreessen", "sequoia", "accel", "peak", "lightspeed", "bessemer",
    "matrix", "kalaari", "blume", "nexus", "elevation",
)
PRODUCT_TERMS = ("acme",)
HARDWARE_KEYWORDS = (
    "manufacturer", "supplier", "component", "bill of materials", "manufacturing quote",
    "lead time", "datasheet", "data sheet", "injection mold", "injection moulding",
    "enclosure", "pcb", "pcba", "fabrication", "unit price", "moq", "tooling",
    "prototype run",
)
MEDIA_KEYWORDS = (
    "press inquiry", "media inquiry", "journalist", "reporter", "interview request",
    "podcast", "embargo", "press release",
)
# Legal document terms — checked against ATTACHMENT FILENAMES specifically → ASK.
LEGAL_DOC_TERMS = ("contract", "nda", "agreement", "mou", "term sheet")


@dataclass
class GuardrailResult:
    floor: Tier
    reasons: list[str]


def _inbound_haystack(thread: Thread) -> str:
    """Lowercased subject+body+attachment-text of INBOUND messages only (your own
    sent text is trusted and never scanned for risk signals)."""
    parts: list[str] = []
    for m in thread.messages:
        if not m.from_me:
            parts.append(m.subject or "")
            parts.append(m.body_text or m.snippet or "")
            parts.extend(a.extracted_text for a in m.attachments)
    return " \n ".join(p for p in parts if p).lower()


def _attachment_haystack(thread: Thread) -> str:
    return " ".join(
        _normalize_filename(a.filename)
        for m in thread.messages if not m.from_me for a in m.attachments
    )


def is_critical(thread: Thread, contact: Contact) -> bool:
    """Deterministic (no LLM): does this thread warrant the heavyweight JUDGE_CRITICAL
    model + larger reasoning budget? True for the genuinely consequential surface a
    startup must never get wrong — investors, money, legal paper. Used by P3 to route
    the JUDGE step; mirrors the Jatin floors so routing and flooring agree."""
    if contact.has_flag("investor") or contact.has_flag("legal"):
        return True
    if contact.relationship.strip().lower() in {"investor", "legal", "lawyer", "counsel"}:
        return True
    domains_blob = " ".join(thread.participants()) + " " + (contact.email or "").lower()
    if any(f in domains_blob for f in INVESTOR_FIRM_DOMAINS):
        return True
    hay = _inbound_haystack(thread)
    if _contains_any(hay, INVESTOR_KEYWORDS):
        return True
    att = _attachment_haystack(thread)
    if att and _contains_any(att, LEGAL_DOC_TERMS):
        return True
    return False


def _build_haystack(thread: Thread, decision: Decision) -> str:
    parts = [decision.intent, decision.suggested_action, decision.one_line_summary]
    for m in thread.messages:
        parts.append(m.subject or "")
        # only scan inbound content for risk keywords; your own sent text is trusted
        if not m.from_me:
            parts.append(m.body_text or m.snippet or "")
            parts.extend(a.filename for a in m.attachments)
            parts.extend(a.extracted_text for a in m.attachments)
    return " \n ".join(p for p in parts if p).lower()


def _normalize_filename(name: str) -> str:
    """Make a filename word-matchable: split camelCase and turn separators into
    spaces, so "MutualNDA_v3.pdf" → "mutual nda v3 pdf" (matches \\bnda\\b) while
    "agenda.pdf" → "agenda pdf" (does NOT match \\bnda\\b)."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    s = re.sub(r"[^A-Za-z0-9]+", " ", s)
    return s.lower()


def _contains_any(haystack: str, needles: tuple[str, ...]) -> list[str]:
    hits = []
    for n in needles:
        # word-boundary where the keyword is alphanumeric; substring for phrases
        if re.search(r"\b" + re.escape(n) + r"\b", haystack) if n.isalnum() else (n in haystack):
            hits.append(n)
    return hits


def _finish_spam(thread, decision, contact, get_floor, reasons, bump, memory) -> GuardrailResult:
    """classifier-brain-1: hard floors that a confident-spam verdict must NEVER bypass.

    The confident-spam path skips ONLY the broad money/legal keyword + reversibility
    heuristics (designed for genuine financial correspondence, not for scam text that
    happens to mention "investment"/"crypto"). But the STRUCTURAL signals below are not
    keyword guesses — they are facts about who/what is involved, and a spam label can
    never be allowed to suppress them. So a phishing message that arrives from an actual
    investor-firm domain, carries a legal-document attachment, comes from a VIP/flagged/
    personal contact, conflicts with memory, or is itself a fail-safe still surfaces.
    The flag/relationship floors (section 1, 1b, 1c) already ran in evaluate() before we
    got here, so `floor` already reflects them."""
    # Investor-firm domain on any participant (or the resolved contact) → ASK. This is a
    # domain fact, not a keyword in attacker-controlled body text.
    domains_blob = " ".join(thread.participants()) + " " + (contact.email or "").lower()
    firm_hit = next((f for f in INVESTOR_FIRM_DOMAINS if f in domains_blob), None)
    if firm_hit:
        bump(Tier.ASK, f"investor-firm domain ({firm_hit}) — spam label cannot bypass")
    # Legal document as an ATTACHMENT → ASK (structural: a real file is attached).
    att_names = _attachment_haystack(thread)
    if att_names and _contains_any(att_names, LEGAL_DOC_TERMS):
        bump(Tier.ASK, "legal document attached — spam label cannot bypass")
    # IIT alumni contact flag → at least APPROVE.
    if contact.has_flag("alumni"):
        bump(Tier.APPROVE, "IIT alumni contact — spam label cannot bypass")
    # Personal/family PERSON across channels → ASK (mirrors the non-spam personal floor).
    if memory is not None and getattr(memory, "is_personal", False):
        bump(Tier.ASK, "personal/family person — spam label cannot bypass")
    # Memory conflict → at least APPROVE (never act on a contradicted assumption).
    if getattr(decision, "memory_conflict", False):
        bump(Tier.APPROVE, "memory conflict — spam label cannot bypass")
    # A fail-safe decision is always ASK, even if a (wrong) spam category slipped through.
    if decision.is_failsafe:
        bump(Tier.ASK, "classifier fail-safe — spam label cannot bypass")
    # Read the floor AFTER the bumps above (get_floor reflects the live nonlocal floor in
    # evaluate(); taking it by value here would miss the bumps this function just made).
    return GuardrailResult(floor=get_floor(), reasons=reasons)


def evaluate(thread: Thread, decision: Decision, contact: Contact, memory=None) -> GuardrailResult:
    """Compute the minimum tier this item is allowed to drop to.

    ``memory`` (optional MemorySignals) is accepted for memory-aware floors; the
    memory-conflict floor below reads ``decision.memory_conflict`` (set by the
    memory-aware JUDGE) so the rule stays deterministic."""
    floor = Tier.SILENT
    reasons: list[str] = []

    def bump(to: Tier, why: str) -> None:
        nonlocal floor
        if to > floor:
            floor = to
        reasons.append(why)

    # 1) Contact flags: investor / legal contacts are always at least APPROVE.
    if contact.has_flag("investor"):
        bump(Tier.APPROVE, "contact flagged investor")
    if contact.has_flag("legal"):
        bump(Tier.APPROVE, "contact flagged legal")
    if contact.relationship.strip().lower() in {"investor", "legal", "lawyer", "counsel"}:
        bump(Tier.APPROVE, f"relationship={contact.relationship}")

    # 1b) Personal contacts (e.g. family/close friends, set via PERSONAL_JIDS on
    #     WhatsApp) are ALWAYS surfaced and never auto-handled — floor to ASK (Tier 3).
    #     Same flag-based pattern as the investor floor; channel-agnostic.
    if contact.has_flag("personal"):
        bump(Tier.ASK, "personal contact — always surfaced, never auto-handled")

    # 1c) GAP 1 — relationship_type floors (from the inferred cross-channel person type).
    #     These supersede the flag heuristics when a type has been classified; the flag-
    #     and relationship-string-based logic above remains as a fallback when the type is
    #     still 'unknown' (backwards compatible). Only ever RAISES involvement.
    rel_type = (getattr(memory, "relationship_type", "unknown") or "unknown") if memory is not None else "unknown"
    if rel_type in ("partner", "family"):
        bump(Tier.ASK, f"relationship_type={rel_type} — personal, always surfaced")
    if rel_type == "investor":
        bump(Tier.APPROVE, "relationship_type=investor")

    # 2) Consequential categories.
    if decision.category in CONSEQUENTIAL_CATEGORIES:
        bump(Tier.APPROVE, f"category={decision.category}")

    # ── classifier-brain-1 ROOT CAUSE FIX ────────────────────────────────────
    # A high-confidence spam_promotional verdict (e.g. the keyword scam pre-filter
    # at confidence 0.98, or an attacker who steered the cheap model into a
    # "confident spam" verdict) used to `return GuardrailResult(...)` HERE, before
    # the money/legal/investor/VIP keyword floors below ever ran. That is a
    # guardrail-LOWERING shortcut: it let a single spam verdict suppress the floors
    # that protect real investor/legal/hardware correspondence. Guardrails may only
    # ever RAISE involvement, never bypass a floor.
    #
    # Replacement: a confident-spam verdict no longer short-circuits. It only narrows
    # which *heuristic* floors are skipped — and even then ONLY the broad money/legal
    # keyword + reversibility heuristics that exist to catch real financial language.
    # The HARD floors (VIP, personal, investor-firm domain, legal attachment, Acme/
    # hardware/media, memory-conflict, fail-safe) ALWAYS run and can never be bypassed
    # by a spam label. So a scam-keyword body claiming to be from an investor firm, or
    # sent by a VIP/flagged contact, still surfaces.
    _is_confident_spam = (
        decision.category == "spam_promotional" and decision.confidence >= 0.90
    )

    # 3) Keyword scan over the thread. Skipped only for confident spam (these broad
    #    money/legal keyword floors exist for genuine financial correspondence, not
    #    for scam text that merely mentions "investment"/"crypto"). The hard floors
    #    below STILL run for spam.
    if _is_confident_spam:
        return _finish_spam(thread, decision, contact, lambda: floor, reasons, bump, memory)

    # (non-spam path continues with the full keyword/reversibility heuristics)
    haystack = _build_haystack(thread, decision)
    money_hits = _contains_any(haystack, MONEY_KEYWORDS)
    legal_hits = _contains_any(haystack, LEGAL_KEYWORDS)
    if money_hits:
        bump(Tier.APPROVE, f"money keywords: {', '.join(money_hits[:5])}")
    if legal_hits:
        bump(Tier.APPROVE, f"legal keywords: {', '.join(legal_hits[:5])}")

    # 4) Reversibility: never auto-act on irreversible/hard-to-reverse items.
    if decision.reversibility == Reversibility.IRREVERSIBLE:
        bump(Tier.APPROVE, "action is irreversible")
    elif decision.reversibility == Reversibility.HARD:
        bump(Tier.FYI, "action is hard to reverse")

    # 5) High-risk money-redirection pattern → push all the way to ASK.
    if money_hits and _contains_any(haystack, HIGH_RISK_PHRASES):
        bump(Tier.ASK, "possible payment/account-change request — verify manually")

    # ── Jatin-specific floors (additive; only raise) ─────────────────────────
    # Investor/fundraising terms anywhere in the thread → ASK.
    inv_hits = _contains_any(haystack, INVESTOR_KEYWORDS)
    if inv_hits:
        bump(Tier.ASK, f"investor/fundraising terms: {', '.join(inv_hits[:4])}")
    # Investor-firm domain on any participant (or the resolved contact) → ASK.
    domains_blob = " ".join(thread.participants()) + " " + (contact.email or "").lower()
    firm_hit = next((f for f in INVESTOR_FIRM_DOMAINS if f in domains_blob), None)
    if firm_hit:
        bump(Tier.ASK, f"investor-firm domain ({firm_hit})")
    # Legal document as an ATTACHMENT → ASK (stronger than the base legal-keyword floor).
    att_names = _attachment_haystack(thread)
    if att_names and _contains_any(att_names, LEGAL_DOC_TERMS):
        bump(Tier.ASK, "legal document attached")
    # Product (Acme), hardware/supplier, media/press, IIT alumni → at least APPROVE.
    if _contains_any(haystack, PRODUCT_TERMS):
        bump(Tier.APPROVE, "mentions Acme (the product)")
    hw_hits = _contains_any(haystack, HARDWARE_KEYWORDS)
    if hw_hits:
        bump(Tier.APPROVE, f"hardware/supplier: {', '.join(hw_hits[:4])}")
    if _contains_any(haystack, MEDIA_KEYWORDS):
        bump(Tier.APPROVE, "media/press inquiry")
    if contact.has_flag("alumni"):
        bump(Tier.APPROVE, "IIT alumni contact")

    # ── Personal/family PERSON floor (Memory Part D) — a hard floor nothing lowers ──
    # The per-identifier `personal` contact flag is handled above; this catches the
    # PERSON being personal across channels (any linked identifier flagged, or a
    # family relationship). Always surfaced, never auto-handled — not by suppression,
    # not by confidence, not by rich memory.
    if memory is not None and getattr(memory, "is_personal", False):
        bump(Tier.ASK, "personal/family person — always surfaced, never auto-handled")

    # ── Memory-conflict floor (Memory Part C) — the safety-critical rule ──────
    # When the new message contradicts remembered facts/decisions/commitments, NEVER
    # act on the assumption: at least draft-for-approval, and ASK if it's consequential
    # (money/legal/investor/commitment/irreversible). A confidently-wrong memory is
    # worse than no memory, so this floor can only RAISE and is never suppressed.
    if getattr(decision, "memory_conflict", False):
        consequential = (
            decision.category in CONSEQUENTIAL_CATEGORIES
            or decision.stakes == Stakes.HIGH
            or decision.reversibility != Reversibility.REVERSIBLE
            or bool(money_hits) or bool(legal_hits) or bool(inv_hits)
            or contact.has_flag("investor") or contact.has_flag("legal")
        )
        bump(Tier.APPROVE, "memory conflict — do not act on the assumption")
        if consequential:
            bump(Tier.ASK, "memory conflict on a consequential item — verify, don't assume")

    # ── GAP 8 — unprocessable media floor ───────────────────────────────────
    # Media we can't READ (a voice note we couldn't transcribe, an image/video/document/
    # location/contact/poll/unknown type) must NOT be handled silently — the owner should at
    # least know something arrived that Steward couldn't judge. Floor to APPROVE so it surfaces.
    # The relay emits these exact placeholders (whatsapp_relay.js classifyMessage). We DELIBER-
    # ATELY exclude stickers and gifs: those are unambiguous trivial reactions and flooring them
    # would spam the owner. (was: only 'could not transcribe' + a bare '[image]', so every other
    # media placeholder bypassed the floor and an unviewable share could be filed silently.)
    inbound_bodies = " ".join(
        (m.body_text or m.snippet or "") for m in thread.messages if not m.from_me
    ).lower()
    _opaque_media = re.compile(
        r"\[(image|video|document|location|contact|poll|unsupported)\b"
    )
    if "could not transcribe" in inbound_bodies or _opaque_media.search(inbound_bodies):
        bump(Tier.APPROVE, "media arrived but could not be processed — surfacing")

    # 6) A fail-safe decision is always ASK.
    if decision.is_failsafe:
        bump(Tier.ASK, "classifier fail-safe")

    return GuardrailResult(floor=floor, reasons=reasons)
