"""Phase 2 — the universal decision explanation framework.

Every processed message gets a structured, persistent Explanation that answers, in plain
English, "why did the agent do this?". It consolidates the signals that were ALREADY
computed during classification/tiering (guardrail floors, memory signals, presence/feedback
suppression, model verdict, the full ordered `applied_floors` chain that the rest of the
system drops on write) into one queryable record.

Reuses the established storage idiom (own table via `ensure()`; best-effort; never raises),
the same one `decision_log` uses, and is keyed by `message_id` so it joins to `decision_log`
(raw THINK/JUDGE/CRITIQUE traces) and `llm_calls` (cost) without duplicating them.

ADDITIVE + FAIL-SAFE: any failure here must never affect classification, dispatch, or send.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from assistant.logging_setup import get_logger
from assistant.models import Decision, FinalDecision, Tier

log = get_logger("explanations")

_TIER_NAME = {0: "handled silently", 1: "FYI", 2: "sent for approval", 3: "asked you"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_explanations (
    message_id        TEXT PRIMARY KEY,
    thread_id         TEXT,
    source            TEXT,            -- channel (gmail/whatsapp)
    ts                INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    base_tier         INTEGER,
    final_tier        INTEGER,
    confidence        REAL,
    guardrails        TEXT,            -- JSON list of guardrail floors that fired
    memory_signals    TEXT,            -- JSON
    suppression_signals TEXT,          -- JSON
    presence_signals  TEXT,            -- JSON
    feedback_signals  TEXT,            -- JSON
    model_verdict     TEXT,            -- JSON (category/intent/reasoning/memory_conflict/...)
    applied_floors    TEXT,            -- JSON: the FULL ordered why-chain
    surfaced_reason   TEXT,
    summary           TEXT             -- the human one-line "why"
);
CREATE INDEX IF NOT EXISTS idx_dexpl_ts ON decision_explanations(ts);
CREATE INDEX IF NOT EXISTS idx_dexpl_final ON decision_explanations(final_tier);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


@dataclass
class Explanation:
    """A complete, structured 'why' for one decision."""
    message_id: str
    thread_id: str = ""
    source: str = ""
    base_tier: int = 0
    final_tier: int = 0
    confidence: float = 0.0
    guardrails: list[str] = field(default_factory=list)
    memory_signals: dict[str, Any] = field(default_factory=dict)
    suppression_signals: dict[str, Any] = field(default_factory=dict)
    presence_signals: dict[str, Any] = field(default_factory=dict)
    feedback_signals: dict[str, Any] = field(default_factory=dict)
    model_verdict: dict[str, Any] = field(default_factory=dict)
    applied_floors: list[str] = field(default_factory=list)
    surfaced_reason: str = ""
    summary: str = ""

    # ── derived human questions ──────────────────────────────────────────────
    def why_tier_changed(self) -> str:
        if self.final_tier > self.base_tier:
            return f"raised {_TIER_NAME.get(self.base_tier,'?')} → {_TIER_NAME.get(self.final_tier,'?')}: {self.surfaced_reason or _first(self.applied_floors)}"
        if self.final_tier < self.base_tier:
            return f"lowered {_TIER_NAME.get(self.base_tier,'?')} → {_TIER_NAME.get(self.final_tier,'?')}: {_lowering_reason(self.applied_floors)}"
        return "tier unchanged from the model's proposal"

    def to_row(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id, "thread_id": self.thread_id,
            "source": self.source, "base_tier": self.base_tier,
            "final_tier": self.final_tier, "confidence": self.confidence,
            "guardrails": json.dumps(self.guardrails),
            "memory_signals": json.dumps(self.memory_signals),
            "suppression_signals": json.dumps(self.suppression_signals),
            "presence_signals": json.dumps(self.presence_signals),
            "feedback_signals": json.dumps(self.feedback_signals),
            "model_verdict": json.dumps(self.model_verdict),
            "applied_floors": json.dumps(self.applied_floors),
            "surfaced_reason": self.surfaced_reason, "summary": self.summary,
        }


def _first(items: list[str]) -> str:
    return items[0] if items else "model proposal"


def _lowering_reason(floors: list[str]) -> str:
    for f in floors:
        low = f.lower()
        if any(k in low for k in ("muted", "handling", "skipped", "resolved",
                                  "deprioritized", "repeatedly")):
            return f
    return _first(floors)


def why_summary(decision: Decision, final: FinalDecision) -> str:
    """The one-line, human 'why' for this decision. Pure — used both to build the stored
    Explanation and to enrich the Telegram card, so the card and the record always agree."""
    base, fin = int(final.base_tier), int(final.final_tier)
    if fin > base:
        reason = final.surfaced_reason or _first([f for f in final.applied_floors])
        return f"{_TIER_NAME.get(fin,'surfaced').capitalize()} because {reason}"
    if fin < base:
        return f"Kept quiet because {_lowering_reason(final.applied_floors)}"
    topic = (decision.one_line_summary or decision.reasoning or decision.intent or "").strip()
    return f"{_TIER_NAME.get(fin,'handled').capitalize()}" + (f": {topic[:140]}" if topic else "")


def build(
    message_id: str, thread, contact, decision: Decision, final: FinalDecision, *,
    memory=None, suppress_active: bool = False, deprioritized: bool = False,
) -> Explanation:
    """Assemble a full Explanation from what tiering already computed. Pure (no I/O)."""
    floors = list(final.applied_floors or [])
    guardrails = [f.split("guardrail:", 1)[1].strip() for f in floors if f.startswith("guardrail:")]
    mem = {
        "recently_skipped": bool(getattr(memory, "recently_skipped", False)),
        "situation_resolved": bool(getattr(memory, "situation_resolved", False)),
        "is_personal": bool(getattr(memory, "is_personal", False)),
    } if memory is not None else {}
    suppression = {
        "nudge_suppressed": any(f.startswith("memory:") for f in floors),
        "presence_silenced": bool(suppress_active),
        "feedback_deprioritized": bool(deprioritized),
        "muted": any("muted" in f.lower() for f in floors),
    }
    return Explanation(
        message_id=str(message_id or ""),
        thread_id=str(getattr(thread, "id", "") or ""),
        source=str(getattr(thread, "channel", "") or ""),
        base_tier=int(final.base_tier), final_tier=int(final.final_tier),
        confidence=float(decision.confidence),
        guardrails=guardrails, memory_signals=mem,
        suppression_signals=suppression,
        presence_signals={"actively_handling": bool(suppress_active)},
        feedback_signals={"deprioritized": bool(deprioritized)},
        model_verdict={
            "category": decision.category, "intent": decision.intent,
            "proposed_tier": int(decision.proposed_tier),
            "memory_conflict": bool(getattr(decision, "memory_conflict", False)),
            "reasoning": (decision.reasoning or "")[:400],
            "is_failsafe": bool(decision.is_failsafe),
        },
        applied_floors=floors,
        surfaced_reason=final.surfaced_reason or "",
        summary=why_summary(decision, final),
    )


def record(conn: sqlite3.Connection, expl: Explanation) -> None:
    """Persist (upsert) the explanation. Best-effort — never raises into the pipeline."""
    try:
        ensure(conn)
        r = expl.to_row()
        conn.execute(
            "INSERT INTO decision_explanations "
            "(message_id, thread_id, source, base_tier, final_tier, confidence, guardrails, "
            " memory_signals, suppression_signals, presence_signals, feedback_signals, "
            " model_verdict, applied_floors, surfaced_reason, summary) "
            "VALUES (:message_id,:thread_id,:source,:base_tier,:final_tier,:confidence,:guardrails,"
            " :memory_signals,:suppression_signals,:presence_signals,:feedback_signals,"
            " :model_verdict,:applied_floors,:surfaced_reason,:summary) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            " thread_id=excluded.thread_id, source=excluded.source, base_tier=excluded.base_tier, "
            " final_tier=excluded.final_tier, confidence=excluded.confidence, "
            " guardrails=excluded.guardrails, memory_signals=excluded.memory_signals, "
            " suppression_signals=excluded.suppression_signals, presence_signals=excluded.presence_signals, "
            " feedback_signals=excluded.feedback_signals, model_verdict=excluded.model_verdict, "
            " applied_floors=excluded.applied_floors, surfaced_reason=excluded.surfaced_reason, "
            " summary=excluded.summary",
            r,
        )
    except Exception:  # noqa: BLE001 - explanation capture must never break the pipeline
        log.debug("explanation record failed (non-fatal)", exc_info=True)


def _loads(v: Any, default: Any) -> Any:
    try:
        return json.loads(v) if v else default
    except Exception:  # noqa: BLE001
        return default


def get(conn: sqlite3.Connection, message_id: str) -> Optional[dict[str, Any]]:
    """Read one explanation as a plain dict (JSON fields decoded). None if absent."""
    try:
        ensure(conn)
        row = conn.execute(
            "SELECT * FROM decision_explanations WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        for k, default in (("guardrails", []), ("memory_signals", {}),
                           ("suppression_signals", {}), ("presence_signals", {}),
                           ("feedback_signals", {}), ("model_verdict", {}),
                           ("applied_floors", [])):
            d[k] = _loads(d.get(k), default)
        return d
    except Exception:  # noqa: BLE001
        return None
