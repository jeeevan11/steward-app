"""Trust & Value metrics (PHASE 10 + 13) — what is the system doing, and is it
worth it?

This is the read-side aggregation that answers two questions for a given time
window:

  TRUST  — what did the assistant DO on your behalf (processed, suppressed,
           escalated, auto-handled, surfaced for approval), and how often were
           its surfaced suggestions accepted?
  VALUE  — was it worth it: how many decisions did you NOT have to make, and a
           best-effort estimate of the triage time that bought back.

It reads only — never writes, never touches the hot path — and is DEFENSIVE:
every table read is wrapped so a missing table or column degrades to zero rather
than raising. The live pipeline must never break because a dashboard query ran.

Sources (all optional, all best-effort):
  * processed_messages (ledger)        — tier/category/state per message
  * decision_log (base_tier/final_tier)— to detect SILENCED messages
  * pending_actions (status)           — surfaced approvals + draft outcomes
  * learning_events (type)             — approve/edit/skip signals
  * commitments (status)               — open obligations being tracked
  * relationship_memory (version)      — memory churn (distilled updates)
  * response_times (kind/ms)           — latency improvement, if recorded

Stdlib + sqlite3 only. No em-dashes in user-facing strings.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

# ── Time-saved heuristic (PHASE 13) ───────────────────────────────────────────
# These are deliberately conservative, documented constants so the "estimated
# time saved" number is auditable rather than magical. Each is minutes of human
# triage time the assistant is credited with avoiding:
#
#   * AUTO_HANDLED  — a tier 0/1 message the assistant filed or acknowledged
#                     without bothering you. ~2 min: open, read, decide, file.
#   * SUPPRESSED    — a message it actively SILENCED (would have notified, chose
#                     not to). ~1 min: the cost of a needless interruption.
#   * APPROVAL      — a tier 2/3 item it pre-triaged and drafted for you, so your
#                     decision is one tap instead of from-scratch triage. ~3 min
#                     of reading + drafting saved per surfaced approval.
#
# Total estimated minutes = auto_handled*2 + suppressed*1 + approvals*3.
MIN_PER_AUTO_HANDLED = 2.0
MIN_PER_SUPPRESSED = 1.0
MIN_PER_APPROVAL = 3.0

# Categories that are "noise by design": a tier-0 outcome for these is the
# expected quiet-filing, NOT an active suppression of something you'd have wanted.
_NOISE_CATEGORIES = {
    "spam_promotional",
    "newsletter",
    "automated_notification",
    "transactional_receipt",
    "social",
    "group_skipped",
}


def _now() -> int:
    return int(time.time())


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001 - metrics must never raise
        return False


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """Run a COUNT/SUM query, return an int, degrade to 0 on any error."""
    try:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return 0
        val = row[0]
        return int(val) if val is not None else 0
    except Exception:  # noqa: BLE001 - missing table/column => 0
        return 0


def _window_clause(column: str, since_epoch: int, until_epoch: Optional[int]) -> tuple[str, tuple]:
    """Build a '<col> >= ? [AND <col> <= ?]' fragment + its params."""
    clause = f"{column} >= ?"
    params: list[Any] = [int(since_epoch)]
    if until_epoch is not None:
        clause += f" AND {column} <= ?"
        params.append(int(until_epoch))
    return clause, tuple(params)


# ── Building blocks ───────────────────────────────────────────────────────────
def _processed_counts(
    conn: sqlite3.Connection, since_epoch: int, until_epoch: Optional[int]
) -> dict[str, int]:
    """Tier tallies from the ledger over the window (by updated_at)."""
    out = {"processed": 0, "escalated": 0, "auto_handled": 0, "approvals": 0}
    if not _table_exists(conn, "processed_messages"):
        return out
    where, params = _window_clause("updated_at", since_epoch, until_epoch)
    out["processed"] = _scalar(
        conn, f"SELECT COUNT(*) FROM processed_messages WHERE {where}", params
    )
    out["auto_handled"] = _scalar(
        conn,
        f"SELECT COUNT(*) FROM processed_messages WHERE {where} AND tier IN (0,1)",
        params,
    )
    out["escalated"] = _scalar(
        conn,
        f"SELECT COUNT(*) FROM processed_messages WHERE {where} AND tier = 3",
        params,
    )
    out["approvals"] = _scalar(
        conn,
        f"SELECT COUNT(*) FROM processed_messages WHERE {where} AND tier IN (2,3)",
        params,
    )
    return out


def _suppressed(
    conn: sqlite3.Connection, since_epoch: int, until_epoch: Optional[int]
) -> int:
    """Messages the assistant actively SILENCED.

    Best-effort via the decision_log: a message is "suppressed" when the brain's
    base_tier wanted to surface/notify but the FINAL tier dropped it below that
    (final_tier < base_tier), OR it landed at tier 0 from a non-noise category
    (a real message it chose to file quietly rather than something inherently
    noisy). Falls back to 0 when decision_log is absent.
    """
    if not _table_exists(conn, "decision_log"):
        return 0
    where, params = _window_clause("ts", since_epoch, until_epoch)
    dropped = _scalar(
        conn,
        f"SELECT COUNT(*) FROM decision_log WHERE {where} "
        "AND base_tier IS NOT NULL AND final_tier IS NOT NULL "
        "AND final_tier < base_tier",
        params,
    )
    # Tier-0 from a non-noise category (real mail filed quietly). Exclude the rows
    # already counted as "dropped" so we never double-count.
    placeholders = ",".join("?" for _ in _NOISE_CATEGORIES)
    quiet = _scalar(
        conn,
        f"SELECT COUNT(*) FROM decision_log WHERE {where} "
        f"AND final_tier = 0 "
        f"AND (category IS NULL OR category NOT IN ({placeholders})) "
        f"AND NOT (base_tier IS NOT NULL AND final_tier < base_tier)",
        params + tuple(_NOISE_CATEGORIES),
    )
    return dropped + quiet


def _learning_counts(
    conn: sqlite3.Connection, since_epoch: int, until_epoch: Optional[int]
) -> dict[str, int]:
    """approve / edit / skip tallies from learning_events over the window."""
    out = {"approve": 0, "edit": 0, "skip": 0}
    if not _table_exists(conn, "learning_events"):
        return out
    where, params = _window_clause("ts", since_epoch, until_epoch)
    for kind in ("approve", "edit", "skip"):
        out[kind] = _scalar(
            conn,
            f"SELECT COUNT(*) FROM learning_events WHERE {where} AND type = ?",
            params + (kind,),
        )
    return out


def _pending_action_counts(
    conn: sqlite3.Connection, since_epoch: int, until_epoch: Optional[int]
) -> dict[str, int]:
    """Outcome tallies from pending_actions over the window (by created_at).

    Used as a fallback / supplement to learning_events for approval and draft-
    acceptance rates: SENT/APPROVED ~ approve, EDITED ~ edit, SKIPPED ~ skip.
    """
    out = {"surfaced": 0, "approved": 0, "edited": 0, "skipped": 0}
    if not _table_exists(conn, "pending_actions"):
        return out
    where, params = _window_clause("created_at", since_epoch, until_epoch)
    out["surfaced"] = _scalar(
        conn, f"SELECT COUNT(*) FROM pending_actions WHERE {where}", params
    )
    out["approved"] = _scalar(
        conn,
        f"SELECT COUNT(*) FROM pending_actions WHERE {where} "
        "AND status IN ('APPROVED','SENDING','SENT')",
        params,
    )
    out["edited"] = _scalar(
        conn,
        f"SELECT COUNT(*) FROM pending_actions WHERE {where} AND status = 'EDITED'",
        params,
    )
    out["skipped"] = _scalar(
        conn,
        f"SELECT COUNT(*) FROM pending_actions WHERE {where} "
        "AND status IN ('SKIPPED','EXPIRED')",
        params,
    )
    return out


def _memory_updates(conn: sqlite3.Connection) -> int:
    """Memory churn: sum of relationship_memory.version (each distill bumps it).

    relationship_memory has no timestamp we can window cheaply other than
    last_distilled_at; we report the lifetime sum of versions as a best-effort
    "how much has the assistant learned" signal. Degrades to 0 when absent.
    """
    if not _table_exists(conn, "relationship_memory"):
        return 0
    return _scalar(conn, "SELECT COALESCE(SUM(version), 0) FROM relationship_memory")


def _commitments_open(conn: sqlite3.Connection) -> int:
    """Currently-open commitments (a live count, not windowed)."""
    if not _table_exists(conn, "commitments"):
        return 0
    return _scalar(
        conn, "SELECT COUNT(*) FROM commitments WHERE status = 'open'"
    )


def _response_time_reduction(
    conn: sqlite3.Connection, since_epoch: int, until_epoch: Optional[int]
) -> Optional[float]:
    """Best-effort response-time improvement in milliseconds.

    Compares the median email-to-notification latency in the window against the
    same metric in the equally-long window immediately before it. Positive =
    faster now than before. Returns None when response_times is absent or there
    is not enough data to compare.
    """
    if not _table_exists(conn, "response_times"):
        return None
    kind = "email_to_notification"

    def _median(lo: int, hi: Optional[int]) -> Optional[float]:
        where, params = _window_clause("ts", lo, hi)
        try:
            rows = list(
                conn.execute(
                    f"SELECT ms FROM response_times WHERE kind = ? AND {where} "
                    "ORDER BY ms ASC",
                    (kind,) + params,
                )
            )
        except Exception:  # noqa: BLE001
            return None
        vals = [int(r[0]) for r in rows if r[0] is not None]
        if not vals:
            return None
        n = len(vals)
        mid = n // 2
        if n % 2:
            return float(vals[mid])
        return (vals[mid - 1] + vals[mid]) / 2.0

    end = until_epoch if until_epoch is not None else _now()
    span = max(1, end - int(since_epoch))
    current = _median(int(since_epoch), end)
    prior = _median(int(since_epoch) - span, int(since_epoch))
    if current is None or prior is None:
        return None
    return round(prior - current, 1)


# ── Public API ────────────────────────────────────────────────────────────────
def compute(
    conn: sqlite3.Connection,
    *,
    since_epoch: int,
    until_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """Compute the full trust + value metric bundle for a time window.

    Every value is an int or float (or None for response_time_reduction when no
    data exists). NEVER raises: a missing table or column degrades to 0/None.
    """
    try:
        proc = _processed_counts(conn, since_epoch, until_epoch)
        suppressed = _suppressed(conn, since_epoch, until_epoch)
        learn = _learning_counts(conn, since_epoch, until_epoch)
        pend = _pending_action_counts(conn, since_epoch, until_epoch)

        # Approval rate: of the cards SURFACED for approval in the window, the fraction the
        # owner approved. Numerator AND denominator both come from pending_actions (same rows,
        # same created_at clock), so `approved` is a subset of `surfaced` and the rate can NEVER
        # exceed 100%. (Prior code divided learning_events 'approve' (by ts, +1 per re-approval)
        # by pending_actions 'surfaced' (a different table/clock), which let it read 104%+.)
        surfaced_n = pend["surfaced"]
        approved_n = min(pend["approved"], surfaced_n)
        approval_rate = round(approved_n / surfaced_n, 3) if surfaced_n else 0.0

        # Draft acceptance: approve vs approve+edit+skip (from learning_events,
        # falling back to pending_actions outcomes).
        acc = learn["approve"] or pend["approved"]
        edit_n = learn["edit"] or pend["edited"]
        skip_n = learn["skip"] or pend["skipped"]
        denom = acc + edit_n + skip_n
        draft_acceptance_rate = round(acc / denom, 3) if denom else 0.0

        auto_handled = proc["auto_handled"]
        approvals = proc["approvals"]
        decisions_avoided = auto_handled + suppressed
        estimated_time_saved_minutes = round(
            auto_handled * MIN_PER_AUTO_HANDLED
            + suppressed * MIN_PER_SUPPRESSED
            + approvals * MIN_PER_APPROVAL,
            1,
        )

        return {
            "since_epoch": int(since_epoch),
            "until_epoch": int(until_epoch) if until_epoch is not None else None,
            # TRUST — what it did
            "messages_processed": proc["processed"],
            "messages_suppressed": suppressed,
            "messages_escalated": proc["escalated"],
            "messages_auto_handled": auto_handled,
            "approvals_requested": approvals,
            "approval_rate": approval_rate,
            "draft_acceptance_rate": draft_acceptance_rate,
            "memory_updates": _memory_updates(conn),
            "commitments_open": _commitments_open(conn),
            # VALUE — was it worth it
            "decisions_avoided": decisions_avoided,
            "estimated_time_saved_minutes": estimated_time_saved_minutes,
            "response_time_reduction": _response_time_reduction(
                conn, since_epoch, until_epoch
            ),
        }
    except Exception:  # noqa: BLE001 - the whole bundle must never raise
        return {
            "since_epoch": int(since_epoch),
            "until_epoch": int(until_epoch) if until_epoch is not None else None,
            "messages_processed": 0,
            "messages_suppressed": 0,
            "messages_escalated": 0,
            "messages_auto_handled": 0,
            "approvals_requested": 0,
            "approval_rate": 0.0,
            "draft_acceptance_rate": 0.0,
            "memory_updates": 0,
            "commitments_open": 0,
            "decisions_avoided": 0,
            "estimated_time_saved_minutes": 0.0,
            "response_time_reduction": None,
        }


_PERIOD_SECONDS = {
    "daily": 86400,
    "weekly": 7 * 86400,
    "monthly": 30 * 86400,
}


def period(conn: sqlite3.Connection, which: str) -> dict[str, Any]:
    """Convenience wrapper: compute() over a named rolling window ending now.

    `which` is one of {"daily","weekly","monthly"}. An unknown value falls back
    to "daily" so callers never crash on a bad query parameter.
    """
    seconds = _PERIOD_SECONDS.get((which or "").lower(), _PERIOD_SECONDS["daily"])
    now = _now()
    bundle = compute(conn, since_epoch=now - seconds, until_epoch=now)
    bundle["period"] = (which or "daily").lower() if which in _PERIOD_SECONDS else "daily"
    return bundle
