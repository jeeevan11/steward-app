"""Phase 3 — audit trail & replay. "git blame for agent decisions."

Captures, per decision, the inputs needed to reconstruct exactly how it was reasoned:
prompt VERSIONS (content hashes), the models + reasoning budgets each step used, the
context supplied to the brain, and (by join) the raw THINK/JUDGE/CRITIQUE outputs already
stored in decision_log, the structured explanation, and the per-call cost in llm_calls.

`reconstruct(conn, message_id)` assembles the full picture from all of these — keyed by
message_id, so this module stores only the bits not already persisted (prompt versions,
route specs, context snapshot) and stitches the rest together at read time.

Own table via ensure(); best-effort; never raises into the pipeline.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from assistant.logging_setup import get_logger

log = get_logger("replay")

_SNAPSHOT_CAP = 8000  # cap stored context so the table stays small

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_replay (
    message_id      TEXT PRIMARY KEY,
    ts              INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    prompt_versions TEXT,     -- JSON {prompt_name: content_hash}
    models          TEXT,     -- JSON {step: {model, thinking, reasoning_tokens}}
    context_supplied TEXT,    -- the rendered context/memory block given to the brain
    thread_snapshot TEXT      -- the rendered thread the brain saw
);
CREATE INDEX IF NOT EXISTS idx_replay_ts ON decision_replay(ts);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def capture(
    conn: sqlite3.Connection, settings, message_id: str, *,
    context_supplied: str = "", thread_snapshot: str = "",
) -> None:
    """Record the replay inputs for one decision. Best-effort; pulls prompt versions and
    route specs from the live config so the record reflects exactly what ran."""
    try:
        ensure(conn)
        from assistant.llm import prompts
        from assistant.llm.router import TaskRouter, Task

        versions = prompts.pipeline_versions(getattr(settings, "prompts_dir", "./prompts"))
        router = TaskRouter(settings)
        steps = {
            "noise": Task.NOISE_FILTER, "think": Task.THINK,
            "judge": Task.JUDGE, "judge_critical": Task.JUDGE_CRITICAL,
            "self_critique": Task.SELF_CRITIQUE,
        }
        models = {}
        for label, task in steps.items():
            spec = router.resolve(task)
            models[label] = {"model": spec.model, "thinking": spec.thinking,
                             "reasoning_tokens": spec.reasoning_tokens}
        conn.execute(
            "INSERT INTO decision_replay (message_id, prompt_versions, models, "
            " context_supplied, thread_snapshot) VALUES (?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET prompt_versions=excluded.prompt_versions, "
            " models=excluded.models, context_supplied=excluded.context_supplied, "
            " thread_snapshot=excluded.thread_snapshot",
            (message_id, json.dumps(versions), json.dumps(models),
             (context_supplied or "")[:_SNAPSHOT_CAP], (thread_snapshot or "")[:_SNAPSHOT_CAP]),
        )
    except Exception:  # noqa: BLE001 - replay capture must never break the pipeline
        log.debug("replay capture failed (non-fatal)", exc_info=True)


def _loads(v: Any, default: Any) -> Any:
    try:
        return json.loads(v) if v else default
    except Exception:  # noqa: BLE001
        return default


def reconstruct(conn: sqlite3.Connection, message_id: str) -> Optional[dict[str, Any]]:
    """Reassemble the full reasoning path for a decision from every store that holds a
    piece of it. Returns None only if nothing at all is recorded for the id."""
    try:
        ensure(conn)
        out: dict[str, Any] = {"message_id": message_id}
        found = False

        row = conn.execute("SELECT * FROM decision_replay WHERE message_id=?", (message_id,)).fetchone()
        if row is not None:
            found = True
            out["prompt_versions"] = _loads(row["prompt_versions"], {})
            out["models"] = _loads(row["models"], {})
            out["context_supplied"] = row["context_supplied"]
            out["thread_snapshot"] = row["thread_snapshot"]

        # Raw reasoning outputs + the decision itself (decision_log).
        try:
            from assistant.storage import decision_log
            d = decision_log.get(conn, message_id)
            if d is not None:
                found = True
                out["decision"] = {
                    k: d[k] for k in d.keys()
                    if k in ("category", "stakes", "reversibility", "confidence", "reasoning",
                             "needs_reply", "base_tier", "final_tier", "surfaced_reason",
                             "was_critical", "critique_adjustment")
                }
                out["raw_outputs"] = {
                    "think": d["think_output"] if "think_output" in d.keys() else None,
                    "judge": d["judge_output"] if "judge_output" in d.keys() else None,
                    "critique": d["critique_output"] if "critique_output" in d.keys() else None,
                }
        except Exception:  # noqa: BLE001
            pass

        # Structured explanation (the "why").
        try:
            from assistant.storage import explanations
            expl = explanations.get(conn, message_id)
            if expl is not None:
                found = True
                out["explanation"] = expl
        except Exception:  # noqa: BLE001
            pass

        # Per-call cost/latency.
        try:
            calls = conn.execute(
                "SELECT task, model, prompt_tokens, completion_tokens, cost, fallback "
                "FROM llm_calls WHERE message_id=? ORDER BY ts", (message_id,)
            ).fetchall()
            if calls:
                found = True
                out["llm_calls"] = [dict(c) for c in calls]
        except Exception:  # noqa: BLE001
            pass

        return out if found else None
    except Exception:  # noqa: BLE001
        return None


def render(record: dict[str, Any]) -> str:
    """Human-readable replay (for the --replay CLI)."""
    if not record:
        return "(no replay record)"
    lines = [f"=== DECISION REPLAY: {record.get('message_id')} ==="]
    d = record.get("decision") or {}
    if d:
        lines.append(f"tier {d.get('base_tier')}→{d.get('final_tier')} | conf {d.get('confidence')} "
                     f"| {d.get('category')} | critical={d.get('was_critical')}")
    if record.get("explanation", {}).get("summary"):
        lines.append("WHY: " + record["explanation"]["summary"])
    if record.get("prompt_versions"):
        lines.append("prompt versions: " + json.dumps(record["prompt_versions"]))
    if record.get("models"):
        lines.append("models: " + json.dumps(record["models"]))
    raw = record.get("raw_outputs") or {}
    for step in ("think", "judge", "critique"):
        if raw.get(step):
            lines.append(f"--- {step} ---\n{str(raw[step])[:1500]}")
    if record.get("llm_calls"):
        lines.append("cost: " + json.dumps(record["llm_calls"]))
    if record.get("context_supplied"):
        lines.append("--- context supplied ---\n" + str(record["context_supplied"])[:1500])
    return "\n".join(lines)
