"""Pure metric functions for the evaluation framework (Phase 4).

These compute aggregate quality numbers from a list of per-scenario result records.
They are deliberately I/O-free and stdlib-only so they are trivially unit-testable
and stable across runs. The runner (runner.py) produces the per-scenario records;
this module turns them into a metrics dict that run_all.py reports and persists.

A per-scenario record (a plain dict) is expected to carry at least:
    {
      "id": str,
      "expected_tier": int,          # 0..3, the human label
      "actual_tier": int,            # 0..3, what the brain decided
      "expected_category": str,
      "actual_category": str,
      "expected_suppressed": bool,
      "actual_suppressed": bool,
      "consequential": bool,         # label: this item must reach ASK/APPROVE
      "error": str | None,           # set if the scenario blew up
    }

Tier semantics (see assistant/models.Tier):
    0 SILENT, 1 FYI, 2 APPROVE, 3 ASK   (>= 2 means "surfaced to the human")
"""

from __future__ import annotations

from typing import Any, Iterable

SURFACE_TIER = 2  # APPROVE and above are "surfaced to the human"


def _ratio(num: int, den: int) -> float:
    """Safe ratio: 0 cases -> 1.0 (vacuously perfect) so an empty slice never drags
    the aggregate down. Rounded to 4 dp for stable report diffs."""
    if den <= 0:
        return 1.0
    return round(num / den, 4)


def _surfaced(tier: int) -> bool:
    return int(tier) >= SURFACE_TIER


def _scored(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only records that ran cleanly (no error) count toward accuracy."""
    return [r for r in records if not r.get("error")]


def tier_accuracy(records: Iterable[dict[str, Any]]) -> float:
    """Fraction of scenarios whose final tier exactly matches the label."""
    scored = _scored(records)
    hits = sum(1 for r in scored if int(r.get("actual_tier", -1)) == int(r.get("expected_tier", -2)))
    return _ratio(hits, len(scored))


def suppression_accuracy(records: Iterable[dict[str, Any]]) -> float:
    """Fraction whose suppressed/not-suppressed outcome matches the label.

    Only counted over scenarios that actually carry an ``expected_suppressed`` label
    (all of them do here, but stay defensive)."""
    scored = [r for r in _scored(records) if r.get("expected_suppressed") is not None]
    hits = sum(
        1 for r in scored
        if bool(r.get("actual_suppressed")) == bool(r.get("expected_suppressed"))
    )
    return _ratio(hits, len(scored))


def escalation_accuracy(records: Iterable[dict[str, Any]]) -> float:
    """Of the items labeled consequential (must reach APPROVE/ASK), the fraction that
    actually surfaced. This is the safety-critical number: a miss here is a
    consequential item the assistant tried to handle quietly."""
    scored = [r for r in _scored(records) if r.get("consequential")]
    hits = sum(1 for r in scored if _surfaced(r.get("actual_tier", 0)))
    return _ratio(hits, len(scored))


def false_positive_rate(records: Iterable[dict[str, Any]]) -> float:
    """Surfaced noise: items the label says should stay quiet (expected SILENT, tier 0)
    but that the system surfaced (>= APPROVE). Lower is better. Denominator = the
    should-be-quiet items."""
    scored = [r for r in _scored(records) if int(r.get("expected_tier", -1)) == 0]
    bad = sum(1 for r in scored if _surfaced(r.get("actual_tier", 0)))
    return _ratio(bad, len(scored))


def false_negative_rate(records: Iterable[dict[str, Any]]) -> float:
    """Silenced something that should surface: items the label says should reach the
    human (expected tier >= APPROVE) that the system handled quietly (< APPROVE).
    Lower is better. Denominator = the should-surface items."""
    scored = [r for r in _scored(records) if int(r.get("expected_tier", -1)) >= SURFACE_TIER]
    bad = sum(1 for r in scored if not _surfaced(r.get("actual_tier", 0)))
    return _ratio(bad, len(scored))


def category_accuracy(records: Iterable[dict[str, Any]]) -> float:
    """Fraction whose category matches the label. Informational (the LLM sets this;
    in --no-llm mode it is canned, so the runner flags category metrics as advisory)."""
    scored = [r for r in _scored(records) if r.get("expected_category")]
    hits = sum(
        1 for r in scored
        if str(r.get("actual_category", "")).strip() == str(r.get("expected_category", "")).strip()
    )
    return _ratio(hits, len(scored))


def draft_acceptance_proxy(conn=None, records: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Hook for a real draft-acceptance signal.

    The honest proxy for "are our drafts good?" is the human feedback already captured
    in ``learning_events`` (approve / edit / skip). This reads those counts when a DB
    connection is supplied; offline/synthetic runs have none, so it returns a neutral
    stub with ``available: False``. Wiring a real inbox's DB in here later turns this
    into a live metric with zero code change elsewhere.

    acceptance = approves / (approves + edits + skips of drafted items).
    """
    out: dict[str, Any] = {
        "available": False,
        "approve": 0,
        "edit": 0,
        "skip": 0,
        "acceptance": None,
    }
    if conn is None:
        return out
    try:
        counts: dict[str, int] = {}
        for t in ("approve", "edit", "skip"):
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM learning_events WHERE type=?", (t,)
            ).fetchone()
            counts[t] = int(row["n"]) if row is not None else 0
        total = counts["approve"] + counts["edit"] + counts["skip"]
        out.update(
            available=total > 0,
            approve=counts["approve"],
            edit=counts["edit"],
            skip=counts["skip"],
            acceptance=(round(counts["approve"] / total, 4) if total > 0 else None),
        )
    except Exception:  # noqa: BLE001 - the proxy is best-effort; never break a report
        pass
    return out


def compute_metrics(
    records: Iterable[dict[str, Any]], *, conn=None
) -> dict[str, Any]:
    """Aggregate every metric into one dict. ``records`` is the per-scenario list from
    the runner. Pass ``conn`` to populate the draft-acceptance proxy from a real DB.

    Returned keys are flat floats (plus a nested ``draft_acceptance`` block and run
    counts) so run_all.py can diff them numerically for regression detection."""
    records = list(records)
    scored = _scored(records)
    return {
        "n_total": len(records),
        "n_scored": len(scored),
        "n_errors": len(records) - len(scored),
        "tier_accuracy": tier_accuracy(records),
        "suppression_accuracy": suppression_accuracy(records),
        "escalation_accuracy": escalation_accuracy(records),
        "category_accuracy": category_accuracy(records),
        "false_positive_rate": false_positive_rate(records),
        "false_negative_rate": false_negative_rate(records),
        "draft_acceptance": draft_acceptance_proxy(conn=conn, records=records),
    }


# Metrics where a HIGHER value is better (accuracy-style). Used by run_all.py to
# decide the direction of a regression. Anything not listed (the *_rate metrics) is
# "lower is better".
HIGHER_IS_BETTER = (
    "tier_accuracy",
    "suppression_accuracy",
    "escalation_accuracy",
    "category_accuracy",
)
LOWER_IS_BETTER = (
    "false_positive_rate",
    "false_negative_rate",
)
