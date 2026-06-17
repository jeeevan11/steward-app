"""Opportunity radar: detects business opportunities in processed threads and
maintains a ranked pipeline.

Surfaces investors, partners, candidates, and suppliers worth pursuing, ranked
by value x probability x momentum. All operations are best-effort: failures are
swallowed and return safe defaults so this module never disrupts the main pipeline.

Stdlib only.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Optional

try:
    from assistant.storage import operating_state as os_store
except ImportError:
    os_store = None  # type: ignore[assignment]

if TYPE_CHECKING:
    import sqlite3

    from assistant.config import Settings
    from assistant.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

DETECT_PROMPT = """Analyze this email/message to determine if it is a business opportunity.

Types:
- investor: VC, angel, family office expressing interest in funding
- partner: company proposing integration, distribution, or strategic partnership
- candidate: job applicant or potential hire
- supplier: vendor, manufacturer, or service provider for the business

Subject: {subject}
Sender: {sender_name} ({sender_email})
Category: {category}
Tier: {tier}
Snippet: {snippet}

Return JSON only:
{{
  "is_opportunity": true or false,
  "type": "investor|partner|candidate|supplier|null",
  "stage": "identified|intro|diligence|term_sheet|closed|null",
  "value_est": <estimated dollar value, 0 if unknown>,
  "probability": <0.0 to 1.0>,
  "next_action": "<specific next step for Jatin, or null>",
  "confidence": <0.0 to 1.0>
}}

Only set is_opportunity=true if confidence >= 0.6."""


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def detect_opportunity(
    thread_id: str,
    subject: str,
    sender_name: str,
    sender_email: str,
    category: str,
    tier: str,
    snippet: str,
    db: "sqlite3.Connection",
    settings: "Settings",
    llm_client: "LLMClient",
) -> Optional[dict]:
    """Detect whether a thread contains a business opportunity.

    Returns a dict with opportunity fields and 'opportunity_id'/'thread_id' keys,
    or None if no opportunity detected or detection fails.
    """
    try:
        if not getattr(settings, "opportunity_detection_enabled", True):
            return None

        prompt = DETECT_PROMPT.format(
            subject=subject,
            sender_name=sender_name,
            sender_email=sender_email,
            category=category,
            tier=tier,
            snippet=snippet,
        )

        try:
            result_text = llm_client.complete_text(
                system_prefix="You are a business opportunity classifier. Return only valid JSON.",
                user_prompt=prompt,
                max_tokens=400,
                use_opus=False,
            )
        except Exception:
            return None

        # Extract the first {...} block from the response
        match = re.search(r"\{.*\}", result_text, re.DOTALL)
        if not match:
            return None

        try:
            result = json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            return None

        is_opportunity = result.get("is_opportunity", False)
        confidence = float(result.get("confidence", 0.0))

        if not is_opportunity or confidence < 0.6:
            return None

        opp_id: Optional[int] = None
        if os_store is not None:
            try:
                opp_id = os_store.create_opportunity(
                    db,
                    person_id=sender_email,
                    type=result.get("type"),
                    stage=result.get("stage"),
                    value_est=result.get("value_est", 0),
                    probability=result.get("probability", 0.0),
                    next_action=result.get("next_action"),
                )
            except Exception:
                opp_id = None

        return {
            **result,
            "opportunity_id": opp_id,
            "thread_id": thread_id,
        }

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pipeline retrieval
# ---------------------------------------------------------------------------

def get_opportunity_pipeline(
    db: "sqlite3.Connection",
    opp_type: Optional[str] = None,
) -> list[dict]:
    """Return the opportunity pipeline from storage, optionally filtered by type.

    Returns an empty list on any error or if storage is unavailable.
    """
    if os_store is None:
        return []
    try:
        return os_store.get_opportunity_pipeline(db, type=opp_type) or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_STAGE_EMOJI: dict[str, str] = {
    "identified": "⚪",   # white circle
    "intro": "\U0001f7e1",    # yellow circle
    "diligence": "\U0001f7e0", # orange circle
    "term_sheet": "\U0001f7e2", # green circle
    "closed": "✅",       # check mark
}


def format_pipeline_chat(
    opportunities: list[dict],
    opp_type: Optional[str] = None,
) -> str:
    """Format the opportunity pipeline as compact Telegram text (max 1200 chars).

    Header reflects type when filtered. Shows up to 5 opportunities with stage,
    probability, and next action.
    """
    n = len(opportunities)

    if opp_type == "investor":
        header = f"Investors - {n} live"
    else:
        header = f"Pipeline - {n} live"

    lines: list[str] = [header]

    for opp in opportunities[:5]:
        stage = (opp.get("stage") or "").lower()
        emoji = _STAGE_EMOJI.get(stage, "-")
        person = opp.get("person_id") or "unknown"
        probability = float(opp.get("probability") or 0.0)
        pct = int(probability * 100)

        stage_label = stage.upper() if stage else "UNKNOWN"
        lines.append(f"{emoji} {person} - {stage_label} - {pct}%")

        next_action = opp.get("next_action") or ""
        if next_action:
            lines.append(f"   next: {next_action[:60]}")

    lines.append("\n[Open full pipeline]")

    text = "\n".join(lines)

    # Trim to 1200 chars if needed
    if len(text) > 1200:
        text = text[:1197] + "..."

    return text
