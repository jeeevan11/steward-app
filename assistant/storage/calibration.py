"""Phase 5 — confidence calibration: does the system's stated confidence match reality?

The brain attaches a `confidence` to every Decision and a tier to every message. We ALSO
capture what the human actually did (approve / edit / skip / override ...) as
`learning_events` keyed by `message_id`. This module joins those two facts and asks the
honest question: when the brain said "I'm 0.9 confident", was it right roughly 90% of the
time? A well-calibrated system has per-confidence-bin accuracy that tracks the bin's
predicted mean; an over-confident one claims 0.9 but is only right 0.6 of the time.

It bins decisions by predicted confidence into deciles, computes per-bin empirical accuracy,
an overall Brier-style score, and a simple (n-weighted) calibration error, then stores the
curve in its own `confidence_calibration` table. `calibrated()` lets the rest of the system
optionally map a raw confidence to the empirical accuracy of its bin.

Reuses the established storage idiom (own table via `ensure()`; best-effort; never raises) —
the same one `decision_log` and `decision_explanations` use. ADDITIVE + FAIL-SAFE: any
failure here degrades to an empty curve and must never affect classification or dispatch.

Stdlib + sqlite3 only.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from assistant.logging_setup import get_logger

log = get_logger("calibration")

# Human signals (learning_events.type) that count as a POSITIVE outcome for a surfaced
# item: the human accepted what we surfaced AS-IS (approved it without changes).
#
# ROOT CAUSE (learning-loop-3): `edit` used to live here, so a draft the owner had to
# REWRITE before approving was scored as a "correct surface" (outcome=1.0). That is the
# one signal that means "the draft missed the mark" — recorder.record_edit and the rule
# proposer both already treat `edit` as negative — yet calibration counted every
# correction as a win. The high-confidence buckets therefore reported accuracy ~1.0 and a
# near-zero calibration error precisely when the brain was over-confident, defeating the
# module's purpose and violating the metrics-honesty invariant. `edit` is now classified
# as a MIS-surface (negative) below, so a rewritten draft no longer inflates accuracy.
_POSITIVE_TYPES = ("approve",)
# Signals that count as a MIS-surface (we surfaced; the human had to wave it away OR
# rewrite the draft we proposed). `edit` is negative: a draft the owner rewrote was not a
# clean correct surface.
_NEGATIVE_TYPES = ("skip", "override", "edit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS confidence_calibration (
    bucket         TEXT PRIMARY KEY,    -- e.g. "0.7-0.8"
    n              INTEGER NOT NULL DEFAULT 0,
    correct        INTEGER NOT NULL DEFAULT 0,
    predicted_mean REAL NOT NULL DEFAULT 0,
    accuracy       REAL NOT NULL DEFAULT 0,
    updated_at     INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    """Create the table if it doesn't exist (idempotent)."""
    conn.executescript(_SCHEMA)


# ── binning ──────────────────────────────────────────────────────────────────
def _bucket_for(confidence: float) -> str:
    """Decile label for a confidence in [0,1]. 1.0 lands in the top bucket."""
    c = confidence
    if c < 0.0:
        c = 0.0
    if c > 1.0:
        c = 1.0
    lo = int(c * 10)
    if lo >= 10:          # 1.0 -> "0.9-1.0", not "1.0-1.1"
        lo = 9
    return f"{lo / 10:.1f}-{(lo + 1) / 10:.1f}"


def _decision_correct(final_tier: int, base_tier: int, events: dict[str, int]) -> Optional[bool]:
    """Was this decision the right call, judged against what the human did?

    Pragmatic definition:
      * surfaced item (tier 2/3) the human approved AS-IS     -> correct surface (True)
      * surfaced item the human skipped/overrode/EDITED       -> mis-surface (False)
      * surfaced item with NO human signal yet                -> unknown (None, excluded)
      * auto-handled item (tier 0/1) with a negative override -> incorrect (the human
        had to step in)                                       -> False
      * auto-handled item with no negative feedback           -> correct silence (True)
    """
    positive = any(events.get(t, 0) > 0 for t in _POSITIVE_TYPES)
    negative = any(events.get(t, 0) > 0 for t in _NEGATIVE_TYPES)
    if final_tier >= 2:                       # we surfaced it to the human
        # learning-loop-3: NEGATIVE wins over positive. An edit-then-approve (both events
        # present) means the owner had to rewrite the draft before sending — the draft
        # missed the mark, so it must NOT score as a clean correct surface just because an
        # approve row also exists. Checking negative first makes any rewrite/skip/override
        # dominate a co-occurring approve.
        if negative:
            return False
        if positive:
            return True
        return None                           # awaiting a verdict — don't score it
    # auto-handled (tier 0/1): right unless the human had to override/undo it
    if negative or events.get("undo", 0) > 0:
        return False
    return True


def _events_by_message(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """message_id -> {event_type: count} from learning_events. Best-effort."""
    out: dict[str, dict[str, int]] = {}
    try:
        rows = conn.execute(
            "SELECT message_id, type, COUNT(*) AS n FROM learning_events "
            "WHERE message_id IS NOT NULL AND message_id != '' "
            "GROUP BY message_id, type"
        ).fetchall()
    except Exception:  # noqa: BLE001 - table may be absent in a partial DB
        return out
    for r in rows:
        mid = r["message_id"]
        out.setdefault(mid, {})[r["type"]] = int(r["n"])
    return out


def _empty_curve() -> dict[str, Any]:
    return {"bins": [], "n": 0, "scored": 0, "brier": None, "calibration_error": None}


def compute(conn: sqlite3.Connection) -> dict[str, Any]:
    """Join decision_log decisions with human outcomes, bin by predicted confidence,
    compute per-bin accuracy + an overall Brier-style score + an n-weighted calibration
    error, upsert the bins, and return the curve. Best-effort: degrades to an empty
    curve and never raises."""
    try:
        ensure(conn)
        events = _events_by_message(conn)
        try:
            rows = conn.execute(
                "SELECT message_id, confidence, final_tier, base_tier FROM decision_log "
                "WHERE confidence IS NOT NULL"
            ).fetchall()
        except Exception:  # noqa: BLE001 - decision_log not present yet
            rows = []

        # accumulate per bucket
        acc: dict[str, dict[str, float]] = {}
        scored = 0
        brier_sum = 0.0
        for r in rows:
            conf = float(r["confidence"] if r["confidence"] is not None else 0.0)
            ft = int(r["final_tier"] if r["final_tier"] is not None else 0)
            bt = int(r["base_tier"] if r["base_tier"] is not None else ft)
            verdict = _decision_correct(ft, bt, events.get(r["message_id"], {}))
            if verdict is None:
                continue                       # excluded: no human signal on a surfaced item
            scored += 1
            outcome = 1.0 if verdict else 0.0
            brier_sum += (conf - outcome) ** 2
            b = _bucket_for(conf)
            slot = acc.setdefault(b, {"n": 0.0, "correct": 0.0, "conf_sum": 0.0})
            slot["n"] += 1
            slot["correct"] += outcome
            slot["conf_sum"] += conf

        bins: list[dict[str, Any]] = []
        cal_err_weighted = 0.0
        for bucket in sorted(acc.keys()):
            slot = acc[bucket]
            n = int(slot["n"])
            correct = int(slot["correct"])
            predicted_mean = slot["conf_sum"] / n if n else 0.0
            accuracy = correct / n if n else 0.0
            cal_err_weighted += n * abs(predicted_mean - accuracy)
            bins.append({
                "bucket": bucket, "n": n, "correct": correct,
                "predicted_mean": round(predicted_mean, 4),
                "accuracy": round(accuracy, 4),
            })

        brier = round(brier_sum / scored, 4) if scored else None
        calibration_error = round(cal_err_weighted / scored, 4) if scored else None

        # ROOT CAUSE (learning-loop-6): the loop below only ever UPSERTed the freshly
        # computed buckets and there was no DELETE, so a decile that stopped accruing
        # scored decisions (pattern changed, or its decision_log rows aged past
        # retention) was never cleared. get_curve()/the console then displayed that
        # stale bin — its old n/accuracy/updated_at — alongside genuinely current bins,
        # an internally-inconsistent reliability curve that breaks metrics-honesty.
        #
        # Fix: replace the WHOLE curve atomically inside this transaction — DELETE every
        # stored bucket first, then INSERT the freshly computed ones. With the table
        # cleared first a plain INSERT suffices and a bucket absent from this run's
        # `bins` cannot linger. Guarded: a failure here degrades to the previously stored
        # curve and never raises into the daily calibration run. We only clear when we
        # actually have a fresh curve to write (scored > 0) so a transient empty recompute
        # — e.g. all surfaced items still awaiting a verdict — never wipes a good curve.
        if scored > 0:
            try:
                conn.execute("DELETE FROM confidence_calibration")
                for b in bins:
                    conn.execute(
                        "INSERT INTO confidence_calibration "
                        "(bucket, n, correct, predicted_mean, accuracy, updated_at) "
                        "VALUES (?,?,?,?,?, strftime('%s','now'))",
                        (b["bucket"], b["n"], b["correct"],
                         b["predicted_mean"], b["accuracy"]),
                    )
            except Exception:  # noqa: BLE001
                log.debug("calibration curve replace failed (non-fatal)", exc_info=True)

        return {
            "bins": bins,
            "n": len(rows),
            "scored": scored,
            "brier": brier,
            "calibration_error": calibration_error,
        }
    except Exception:  # noqa: BLE001 - calibration must never break the pipeline
        log.debug("calibration.compute failed (non-fatal)", exc_info=True)
        return _empty_curve()


def get_curve(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read the stored bins as a list of plain dicts, ordered by bucket. Empty on any error."""
    try:
        ensure(conn)
        rows = conn.execute(
            "SELECT bucket, n, correct, predicted_mean, accuracy, updated_at "
            "FROM confidence_calibration ORDER BY bucket"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def calibrated(conn: sqlite3.Connection, raw_confidence: float) -> float:
    """Map a raw confidence to the empirical accuracy of its bin. Falls back to the raw
    value when that bin has no stored data (so it is safe to call before any compute())."""
    try:
        c = float(raw_confidence)
    except (TypeError, ValueError):
        return 0.0
    try:
        ensure(conn)
        bucket = _bucket_for(c)
        row = conn.execute(
            "SELECT accuracy, n FROM confidence_calibration WHERE bucket=?", (bucket,)
        ).fetchone()
        if row is not None and int(row["n"]) > 0:
            n = int(row["n"])
            acc = float(row["accuracy"])
            # Shrinkage smoothing (empirical-Bayes): blend the bin's empirical accuracy toward
            # the model's own raw confidence with PSEUDO pseudo-observations as the prior. This
            # kills the metrics-honesty bug where an n=1 bin reads 0% or 100% and snaps the live
            # autonomy/surface gate on a single (possibly mislabeled) sample. Large n → empirical
            # accuracy dominates; small n → stays near the raw confidence. Result stays in [0,1].
            PSEUDO = 5.0
            return (acc * n + c * PSEUDO) / (n + PSEUDO)
    except Exception:  # noqa: BLE001
        pass
    return c
