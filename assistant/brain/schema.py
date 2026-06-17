"""Strict schema for the classifier's JSON output.

Two responsibilities:
  1. `DECISION_JSON_SCHEMA` — the JSON Schema handed to the Anthropic API via
     `output_config.format` so the model is constrained to the right shape.
  2. `parse_decision` — defensive parsing/validation of whatever actually comes
     back. ANY deviation (missing field, bad enum, out-of-range number, malformed
     JSON) results in `Decision.failsafe(...)` — we fail safe to the human, never
     to a silent autonomous action.

Stdlib only.
"""

from __future__ import annotations

import json
from typing import Any

from assistant.models import CATEGORIES, Decision, Reversibility, Stakes, Tier


def _close_open_json(s: str) -> str:
    """Best-effort completion of a TRUNCATED JSON object: close an unterminated string,
    then close any unbalanced braces/brackets (innermost first). Stdlib only.

    This never fixes structurally-broken JSON (a key with no value etc.) — those still
    fail to parse and fall through to the fail-safe. It only rescues output that was cut
    off after enough fields were already emitted (the reasoning-truncation symptom)."""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    out = s + ('"' if in_str else "")
    out = out.rstrip().rstrip(",")           # drop a dangling comma before closing
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def _loads_lenient(raw: Any) -> tuple[Any, bool]:
    """json.loads, but if that fails on a string, try salvaging a truncated object.

    Returns ``(parsed_object_or_None, was_salvaged)``. ``was_salvaged`` is True ONLY
    when the input did not parse cleanly and we recovered it via _close_open_json — i.e.
    the model output was truncated/over-length. Callers (llm-layer-2) must treat a
    salvaged decision as untrustworthy for a confident LOW-tier verdict and fail safe."""
    if not isinstance(raw, (str, bytes, bytearray)):
        return raw, False
    try:
        return json.loads(raw), False
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    start = text.find("{")
    if start == -1:
        return None, False
    try:
        # A clean re-parse (e.g. only a code fence was stripped) is NOT a truncation
        # salvage; only flag was_salvaged when the close-open repair actually changed it.
        stripped = text[start:]
        try:
            return json.loads(stripped), False
        except (json.JSONDecodeError, TypeError, ValueError):
            return json.loads(_close_open_json(stripped)), True
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, False

# JSON Schema for output_config.format. Note the API's structured-output limits:
# no min/max numeric constraints, additionalProperties must be false. We enforce
# ranges ourselves in parse_decision.
DECISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "intent": {"type": "string"},
        "sender_importance": {"type": "integer"},
        "stakes": {"type": "string", "enum": list(Stakes.ALL)},
        "reversibility": {"type": "string", "enum": list(Reversibility.ALL)},
        "proposed_tier": {"type": "integer", "enum": [0, 1, 2, 3]},
        "confidence": {"type": "number"},
        "needs_reply": {"type": "boolean"},
        "reasoning": {"type": "string"},
        "suggested_action": {"type": "string"},
        "one_line_summary": {"type": "string"},
        "memory_conflict": {"type": "boolean"},
    },
    "required": [
        "category",
        "intent",
        "sender_importance",
        "stakes",
        "reversibility",
        "proposed_tier",
        "confidence",
        "needs_reply",
        "reasoning",
        "suggested_action",
        "one_line_summary",
    ],
}


def _clamp_int(value: Any, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _clamp_float(value: Any, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def parse_decision(raw: Any) -> Decision:
    """Validate raw model output (str JSON or dict) into a Decision, or fail safe.

    This never raises — a bad input becomes a Tier-3 fail-safe Decision.
    """
    data, was_salvaged = _loads_lenient(raw)   # tolerates a truncated-but-recoverable object
    if data is None:
        return Decision.failsafe("unparseable model output (even after salvage)")

    if not isinstance(data, dict):
        return Decision.failsafe("model output was not a JSON object")

    try:
        category = str(data["category"])
        if category not in CATEGORIES:
            return Decision.failsafe(f"unknown category {category!r}")

        stakes = str(data["stakes"])
        if stakes not in Stakes.ALL:
            return Decision.failsafe(f"invalid stakes {stakes!r}")

        reversibility = str(data["reversibility"])
        if reversibility not in Reversibility.ALL:
            return Decision.failsafe(f"invalid reversibility {reversibility!r}")

        proposed_tier = _clamp_int(data["proposed_tier"], 0, 3)
        confidence = _clamp_float(data["confidence"], 0.0, 1.0)
        sender_importance = _clamp_int(data["sender_importance"], 0, 100)
        needs_reply = bool(data["needs_reply"])

        # ── llm-layer-2 ROOT CAUSE FIX ───────────────────────────────────────
        # A JUDGE response that was TRUNCATED (recovered only via the close-open salvage)
        # cannot be trusted to be a confident, complete LOW-tier verdict: the model never
        # finished emitting its judgment, so a salvaged tier-0/1 ("handle silently"/"FYI")
        # with thin or absent reasoning is exactly the silent-action trap. Fail safe:
        # escalate any salvaged decision that lands below APPROVE to a Tier-3 fail-safe so
        # a human sees it, rather than coercing truncated output into autonomous action.
        # A salvaged decision that already proposes APPROVE/ASK is left as-is (it is
        # already surfacing) but kept honest by the downstream guardrails.
        if was_salvaged and proposed_tier < int(Tier.APPROVE):
            return Decision.failsafe(
                "JUDGE output truncated/over-length and salvaged into a low tier "
                f"(proposed_tier={proposed_tier}); escalating instead of acting silently"
            )

        return Decision(
            category=category,
            intent=str(data.get("intent", "")),
            sender_importance=sender_importance,
            stakes=stakes,
            reversibility=reversibility,
            proposed_tier=proposed_tier,
            confidence=confidence,
            needs_reply=needs_reply,
            reasoning=str(data.get("reasoning", "")),
            suggested_action=str(data.get("suggested_action", "")),
            one_line_summary=str(data.get("one_line_summary", "")),
            is_failsafe=False,
            # optional (memory-aware JUDGE); absent on legacy/non-memory output → False
            memory_conflict=bool(data.get("memory_conflict", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return Decision.failsafe(f"missing/invalid field: {exc}")


# Schema for the cheap haiku noise pass (separate, tiny).
NOISE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_noise": {"type": "boolean"},
        "confidence": {"type": "number"},
        "label": {"type": "string"},  # suggested gmail label if noise, e.g. "Newsletters"
        "reason": {"type": "string"},
    },
    "required": ["is_noise", "confidence", "label", "reason"],
}


# ── THINK (P3 step 1): a cheap prep pass, never the decision ──────────────────
THINK_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "key_entities": {"type": "array", "items": {"type": "string"}},
        "relationship_context": {"type": "string"},
        "urgency_signals": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "preliminary_category": {"type": "string"},
    },
    "required": [
        "key_entities", "relationship_context", "urgency_signals",
        "ambiguities", "preliminary_category",
    ],
}


def parse_think(raw: Any) -> dict[str, Any]:
    """Parse the THINK prep object. On ANY failure returns an empty prep dict — the
    JUDGE step then runs without prep rather than crashing (prep is never the
    decision, only context)."""
    empty = {
        "key_entities": [], "relationship_context": "", "urgency_signals": [],
        "ambiguities": [], "preliminary_category": "",
    }
    try:
        data, _ = _loads_lenient(raw)   # tolerate truncation; recover prep when possible
        if not isinstance(data, dict):
            return empty
        def _strs(v: Any) -> list[str]:
            return [str(x) for x in v][:12] if isinstance(v, list) else []
        return {
            "key_entities": _strs(data.get("key_entities")),
            "relationship_context": str(data.get("relationship_context", "")),
            "urgency_signals": _strs(data.get("urgency_signals")),
            "ambiguities": _strs(data.get("ambiguities")),
            "preliminary_category": str(data.get("preliminary_category", "")),
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        return empty


# ── SELF_CRITIQUE (P3 step 3): can only RAISE the tier, never lower it ─────────
CRITIQUE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tier_adjustment": {"type": "integer", "enum": [0, 1, 2]},
        "reason": {"type": "string"},
    },
    "required": ["tier_adjustment", "reason"],
}


def parse_critique(raw: Any) -> dict[str, Any]:
    """Parse the SELF_CRITIQUE output. tier_adjustment is clamped to 0..2 (never
    negative — the critique can only raise involvement). On any failure returns
    adjustment 0 (keep the JUDGE decision unchanged)."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        if not isinstance(data, dict):
            return {"tier_adjustment": 0, "reason": "parse_error"}
        adj = _clamp_int(data.get("tier_adjustment", 0), 0, 2)
        return {"tier_adjustment": adj, "reason": str(data.get("reason", ""))}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"tier_adjustment": 0, "reason": "parse_error"}


def parse_noise(raw: Any) -> dict[str, Any]:
    """Parse the noise-pass output. On any failure returns is_noise=False (safe:
    a parse error means we do NOT silently archive — we fall through to full
    classification)."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return {
            "is_noise": bool(data.get("is_noise", False)),
            "confidence": _clamp_float(data.get("confidence", 0.0), 0.0, 1.0),
            "label": str(data.get("label", "")),
            "reason": str(data.get("reason", "")),
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"is_noise": False, "confidence": 0.0, "label": "", "reason": "parse_error"}
