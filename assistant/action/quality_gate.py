"""Draft quality gate (P5b) — a deterministic last check before a draft reaches you.

It NEVER blocks the approval flow: it always returns a draft. It silently auto-fixes
two style problems (em/en dashes, AI filler phrases) and FLAGS two it won't touch
(possible fabricated specifics, over-length for the segment). Flags are surfaced as a
warning on the Telegram card, never edited into the draft body.

Pure + stdlib only (no LLM, no DB) so it's fast on the hot path and fully unit-tested.
Restructuring after a removal is done with conservative whitespace/punctuation cleanup
rather than a second LLM call — latency is a product metric (see P0)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Per-segment word ceilings (flag only).
_LENGTH_LIMITS = {"investor": 150, "team": 100, "customer": 200, "external": 200}

# drafting-safety-6 (ROOT CAUSE): the old _remove_filler ran one unanchored, IGNORECASE
# `re.escape(phrase)` substitution for EACH phrase, deleting it ANYWHERE it appeared and
# only ever recording the edit in `auto_fixed` (which never raises needs_review). Two
# failure modes resulted: (a) meaning-bearing collocations like "Moving forward" /
# "Going forward" / "Circling back" were deleted mid-sentence, silently inverting the
# draft's meaning ("Moving forward the launch to March 3" -> "the launch to March 3");
# (b) even a legitimate strip was invisible to the owner because auto_fixed builds no card
# warning. Fix, two parts:
#   1. SAFE_OPENERS are pure greeting/boilerplate filler that only ever appears clause-
#      initially and carries no operative meaning — these are removed silently, but ONLY
#      when anchored to the start of the text or immediately after sentence-ending
#      punctuation / a newline (never mid-sentence).
#   2. The meaning-bearing collocations ("Moving forward", "Going forward", "Circling
#      back", "Touching base", "Just following up") are NO LONGER silently stripped. They
#      are removed only clause-initially AND that removal is surfaced as a flag (so
#      needs_review fires and the card warns the owner the draft was altered). A mid-
#      sentence occurrence is left untouched entirely — it is almost always load-bearing
#      ("moving forward the deadline").

# Greeting / boilerplate openers — safe to strip silently when clause-initial.
_SAFE_OPENERS = (
    "I hope this email finds you well",
    "I hope this finds you well",
    "Hope you're doing well",
    "Hope you are doing well",
    "As per our conversation",
    "Please don't hesitate",
    "Please do not hesitate",
    "I wanted to reach out",
)

# Meaning-bearing collocations — strip ONLY clause-initially, and FLAG the removal so the
# owner is told the draft was altered. Never touched mid-sentence (load-bearing there).
_RISKY_OPENERS = (
    "Just following up",
    "Circling back",
    "Touching base",
    "Moving forward",
    "Going forward",
)

# Back-compat: the full set some callers / tests may still reference.
_FILLER_PHRASES = _SAFE_OPENERS + _RISKY_OPENERS

# A removal only fires at clause-initial position: the very start of the text, or right
# after sentence-ending punctuation / a newline (optionally with surrounding whitespace).
_CLAUSE_START = r"(?:^|(?<=[.!?\n]))\s*"
# Trailing connective punctuation/space we also consume so the remaining clause reads
# cleanly (", " / ": " / "- " etc. left by the opener).
_OPENER_TAIL = r"[ \t]*[,.;:!\-]*[ \t]*"

# For a SAFE opener (greeting boilerplate), any trailing punctuation/space is fine.
# For a RISKY collocation, only treat it as a discourse-marker filler — and therefore
# strip-and-flag it — when it is IMMEDIATELY followed by a clause-ending mark: a comma /
# semicolon / colon, a sentence-ending '.'/'!' (then space or end), a newline, or the end
# of the text. That covers "Moving forward, we should ..." and a standalone "Circling
# back." while leaving a verb+object use ("Moving forward the launch to March 3")
# completely intact (it is followed by a word, not a clause-ending mark), so the old
# meaning-inverting deletion can no longer happen (drafting-safety-6). The matched
# punctuation + trailing whitespace is consumed so the remaining clause reads cleanly.
_RISKY_TAIL = r"(?:[ \t]*[,;:]|[ \t]*[.!](?=\s|$)|[ \t]*(?=\n)|[ \t]*$)[ \t]*"


@dataclass
class QualityResult:
    clean_draft: str
    flags: list[str] = field(default_factory=list)        # not auto-fixed; surfaced
    auto_fixed: list[str] = field(default_factory=list)    # silently corrected
    needs_review: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "flags": self.flags, "auto_fixed": self.auto_fixed,
            "needs_review": self.needs_review,
        })


# drafting-safety-5 (ROOT CAUSE): this gate's dash auto-fix (and its `if "—" in out` trigger
# in check_and_fix) only handled U+2014/U+2013, so a model that emitted any other Unicode
# dash (horizontal bar U+2015, minus U+2212, figure dash U+2012, 2-/3-em U+2E3A/U+2E3B)
# passed the gate untouched, shipping a dash-equivalent glyph on the approved card. Delegate
# to drafting.strip_dashes (the single shared normalizer over the full dash class) so the
# gate and the drafter strip exactly the same set. Fall back to the local narrow strip only
# if the import is unavailable, so the gate never hard-fails on an import error.
def _strip_dashes(text: str) -> str:
    try:
        from assistant.action.drafting import strip_dashes
        return strip_dashes(text)
    except Exception:  # noqa: BLE001 - never let a dash strip break the gate
        text = re.sub(r"\s*[—―⸺⸻]\s*", ", ", text)   # em-class → ", "
        text = re.sub(r"\s*[‒–−]\s*", "-", text)        # narrow dash → "-"
        text = text.replace(",,", ",")
        text = re.sub(r",[ \t]+\n", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text


def _has_unicode_dash(text: str) -> bool:
    """drafting-safety-5: whether the gate's dash auto-fix should fire — true for ANY
    Unicode dash we normalize, not just U+2014/U+2013."""
    try:
        from assistant.action.drafting import has_unicode_dash
        return has_unicode_dash(text)
    except Exception:  # noqa: BLE001
        return any(ch in (text or "") for ch in "—―⸺⸻‒–−")


def _clause_initial_re(phrase: str) -> "re.Pattern[str]":
    """Compile a regex that matches a SAFE opener ``phrase`` ONLY clause-initially (start of
    text or just after sentence-ending punctuation / a newline), plus its trailing
    connective punctuation. Case-insensitive but anchored, so it is never matched mid-
    sentence (drafting-safety-6)."""
    return re.compile(_CLAUSE_START + re.escape(phrase) + _OPENER_TAIL, flags=re.IGNORECASE)


def _risky_opener_re(phrase: str) -> "re.Pattern[str]":
    """Compile a regex for a RISKY collocation that matches ONLY when it is clause-initial
    AND used as a discourse marker — i.e. immediately followed by a comma / clause boundary
    ("Moving forward, ...") — so a verb+object use ("Moving forward the launch") is never
    deleted (drafting-safety-6)."""
    return re.compile(
        _CLAUSE_START + re.escape(phrase) + _RISKY_TAIL,
        flags=re.IGNORECASE,
    )


def _tidy(text: str) -> str:
    out = re.sub(r"[ \t]{2,}", " ", text)
    out = re.sub(r"\n[ \t]+", "\n", out)
    out = re.sub(r" +\n", "\n", out)
    return out.strip()


def _remove_filler(text: str) -> tuple[str, bool, list[str]]:
    """Conservatively strip AI/corporate filler (drafting-safety-6).

    Returns ``(cleaned_text, flagged, removed_phrases)`` where:
      * SAFE_OPENERS are removed silently when clause-initial.
      * RISKY collocations are removed only when clause-initial AND ``flagged`` is set True
        and the phrase is added to ``removed_phrases`` so the caller can surface a warning.
      * A risky collocation that appears ONLY mid-sentence is left completely untouched
        (it is almost always meaning-bearing there) — no silent rewrite, no flag.

    This replaces the previous unanchored IGNORECASE substitution that deleted phrases
    anywhere and could invert a draft's meaning with no signal to the owner.
    """
    out = text
    flagged = False
    removed: list[str] = []

    # Safe openers: silent, clause-initial only.
    for phrase in _SAFE_OPENERS:
        new = _clause_initial_re(phrase).sub("", out)
        if new != out:
            out = new

    # Risky collocations: clause-initial discourse-marker use only, and FLAG the change.
    # A verb+object use ("Moving forward the launch") fails the discourse-marker look-ahead
    # and is therefore left completely intact (no silent meaning inversion).
    for phrase in _RISKY_OPENERS:
        new = _risky_opener_re(phrase).sub("", out)
        if new != out:
            out = new
            flagged = True
            removed.append(phrase)

    return _tidy(out), flagged, removed


_SIGNIFICANT = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?"        # $ amounts
    r"|\d+(?:\.\d+)?\s?%"             # percentages
    r"|\b(?:19|20)\d{2}\b"            # years
    r"|\b\d{1,2}:\d{2}\b"            # times
    r"|\b\d{3,}\b"                    # large/specific numbers
)


def _significant_numbers(text: str) -> set[str]:
    return {m.group(0).replace(" ", "") for m in _SIGNIFICANT.finditer(text or "")}


def _fabricated_specifics(draft: str, source: str) -> list[str]:
    """Significant numbers/dates/amounts in the draft that don't appear in the source
    (the thread/context). Conservative: only digit-bearing specifics, so benign
    phrasing like 'in 2 weeks' is ignored."""
    src = _significant_numbers(source)
    return [n for n in _significant_numbers(draft) if n not in src]


# ─────────────────────────────────────────────────────────────────────────────
# Placeholder / holding-draft guard (drafting-safety-1 / NO_PLACEHOLDER_SENT)
#
# A holding draft (returned when the LLM is unavailable) and any draft that still
# carries an unresolved sentinel like [your name] / [PLACEHOLDER] / [Could not
# auto-draft ...] must NEVER be sent verbatim. This is a SEND-PATH check (not just a
# draft-time one): the send path calls placeholder_reason() right before transmission
# and refuses + surfaces if it returns non-empty.
# ─────────────────────────────────────────────────────────────────────────────

# Exact holding-draft sentinels emitted by drafting._holding_draft (and close variants).
_HOLDING_SENTINELS = (
    "could not auto-draft",
    "key points to cover: placeholder",
)

# Unresolved bracket placeholders we will not ship. Matched case-insensitively. These are
# the tokens a human is expected to fill before sending; shipping them is the failure.
_PLACEHOLDER_TOKENS = (
    "your name",
    "placeholder",
    "name here",
    "insert ",
    "tbd",
    "todo",
    "xxxx",
    "your company",
    "company name",
    "recipient name",
    "client name",
    "fill in",
    "to be filled",
)

# Any bracketed token whose inner text is ALL CAPS (>=3 chars), e.g. "[PLACEHOLDER]",
# "[YOUR NAME]", "[KEY POINTS]" — a strong, low-false-positive sentinel signal that the
# draft was never finished. A normal finished reply does not contain [ALL-CAPS] brackets.
_BRACKET_CAPS = re.compile(r"\[[^\]]*[A-Z]{3,}[^\]]*\]")


def placeholder_reason(body: str) -> str:
    """Return a short human-readable reason iff ``body`` still contains an unresolved
    placeholder / holding-draft sentinel, else "". Pure + stdlib so the send path can call
    it with no LLM/DB. Conservative: keyed on explicit sentinels and ALL-CAPS bracket tokens
    so it does not trip on ordinary bracketed asides like "[see attached]"."""
    text = (body or "")
    low = text.lower()
    for s in _HOLDING_SENTINELS:
        if s in low:
            return f"holding-draft sentinel present ({s!r})"
    # Bracketed placeholders: [...] containing a known placeholder token.
    for m in re.finditer(r"\[([^\]]+)\]", text):
        inner = m.group(1).strip().lower()
        for tok in _PLACEHOLDER_TOKENS:
            if tok in inner:
                return f"unresolved placeholder [{m.group(1).strip()}]"
    m = _BRACKET_CAPS.search(text)
    if m:
        return f"unresolved placeholder {m.group(0)}"
    return ""


def has_unresolved_placeholder(body: str) -> bool:
    """Boolean convenience wrapper over placeholder_reason()."""
    return bool(placeholder_reason(body))


def check_and_fix(draft_text: str, segment: str = "external", source_text: str = "") -> QualityResult:
    """Run the gate. Returns a QualityResult; the draft is always usable."""
    out = draft_text or ""
    auto_fixed: list[str] = []
    flags: list[str] = []

    # 1) em/en (and the wider Unicode dash class) — auto-fix. drafting-safety-5: trigger on
    # ANY normalizable Unicode dash, not just U+2014/U+2013, so a horizontal-bar / minus /
    # figure-dash substitute can't slip past the gate onto the approved card.
    if _has_unicode_dash(out):
        out = _strip_dashes(out)
        auto_fixed.append("removed em/en dashes")

    # 2) AI filler phrases — conservative, position-aware removal (drafting-safety-6).
    # Safe greeting openers are stripped silently (auto_fixed); meaning-bearing
    # collocations are stripped only clause-initially AND surfaced as a flag so the owner
    # is told the draft was altered (never a silent meaning change). A risky collocation
    # appearing only mid-sentence is left untouched.
    cleaned, filler_flagged, removed_phrases = _remove_filler(out)
    if cleaned != out:
        out = cleaned
        auto_fixed.append("removed AI filler phrases")
    if filler_flagged and removed_phrases:
        flags.append("edited: removed leading " + ", ".join(f"'{p}'" for p in removed_phrases))

    # 3) fabricated specifics — flag only
    fabs = _fabricated_specifics(out, source_text)
    if fabs:
        flags.append("possible fabrication: " + ", ".join(fabs[:5]))

    # 4) length sanity — flag only
    limit = _LENGTH_LIMITS.get(segment, 200)
    wc = len(out.split())
    if wc > limit:
        flags.append(f"long for {segment}: {wc} words (limit {limit})")

    return QualityResult(
        clean_draft=out, flags=flags, auto_fixed=auto_fixed, needs_review=bool(flags)
    )
