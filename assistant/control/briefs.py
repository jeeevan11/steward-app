"""Morning / evening briefs.

A brief is a short, human-readable digest of what the assistant did and what
still needs you: how many messages were processed (by ledger state), what's
sitting in the pending queue awaiting your decision, and a recap of the
autonomous actions taken in the window.

The LLM turns the gathered facts into prose; if the model is unavailable we fall
back to a plain templated summary so the brief NEVER crashes — a missing brief is
acceptable, a crash in the control loop is not.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime

from assistant.config import Settings
from assistant.llm.client import LLMClient, LLMError
from assistant.llm import prompts
from assistant.logging_setup import get_logger
from assistant.storage import ledger
from assistant.storage import repositories as repo

log = get_logger("briefs")

_HOUR = 3600
_TIER_NAMES = {0: "silent", 1: "fyi", 2: "approve", 3: "ask"}
_TIER_EMOJI = {0: "⚪", 1: "🔵", 2: "🟡", 3: "🔴"}
_MAX_BRIEF_ITEMS = 5

# When nothing needs the human, silence beats noise. /brief shows this; the
# SCHEDULED brief is skipped entirely (see main.maybe_send_briefs).
EMPTY_BRIEF = "🟢 All quiet. Nothing needs you."


def is_empty(facts: dict) -> bool:
    """True when there is genuinely nothing to report — no pending decisions, no
    actions taken, and nothing processed in the window."""
    return (not facts.get("pending") and not facts.get("actions")
            and not facts.get("counts") and not facts.get("commitments"))


def since_epoch_for(kind: str, settings: Settings) -> int:
    """Epoch seconds marking the start of the brief window.

    morning → roughly the last 16h (covers overnight since the evening brief).
    evening → since this morning (~12h ago).
    Simple wall-clock math; timezone niceties are intentionally out of scope —
    the window only needs to be 'recent enough'.
    """
    now = int(time.time())
    if kind == "evening":
        return now - 12 * _HOUR
    # default / morning
    return now - 16 * _HOUR


def _gather(conn: sqlite3.Connection, since: int) -> dict:
    """Collect the raw facts for a brief from the ledger + repositories."""
    counts = ledger.counts_since(conn, since)
    pending = repo.open_pending(conn)
    actions = repo.recent_actions(conn, since)

    # Tally autonomous actions by kind for the recap.
    action_tally: dict[str, int] = {}
    for a in actions:
        action_tally[a["kind"]] = action_tally.get(a["kind"], 0) + 1

    return {
        "counts": counts,
        "pending": pending,
        "actions": actions,
        "action_tally": action_tally,
        # Bug 3 fix: a quiet inbox is NOT an empty brief if commitments are due.
        "commitments": _commitment_bullets(conn, int(time.time())),
    }


def _render_facts(kind: str, facts: dict) -> str:
    """Compact plaintext block of the gathered facts (fed to the LLM, and reused
    verbatim in the templated fallback)."""
    counts = facts["counts"]
    pending = facts["pending"]
    tally = facts["action_tally"]

    lines: list[str] = [f"Brief kind: {kind}"]

    if counts:
        c = ", ".join(f"{state.lower()}={n}" for state, n in sorted(counts.items()))
        lines.append(f"Messages by state: {c}")
    else:
        lines.append("Messages by state: none in window")

    if tally:
        t = ", ".join(f"{kind_}={n}" for kind_, n in sorted(tally.items()))
        lines.append(f"Actions taken: {t}")
    else:
        lines.append("Actions taken: none")

    commitments = facts.get("commitments") or []
    if commitments:
        lines.append(f"Commitments due soon: {len(commitments)}")
        for c in commitments[:_MAX_BRIEF_ITEMS]:
            lines.append(f"  • {c.get('text', 'a commitment')} (due {c.get('due', '')})")

    lines.append(f"Pending your decision: {len(pending)}")
    # Most consequential first (Tier 3 before Tier 2), capped to the top few.
    ranked = sorted(pending, key=lambda p: -int(p["tier"] or 0))
    for p in ranked[:_MAX_BRIEF_ITEMS]:
        emoji = _TIER_EMOJI.get(p["tier"], "•")
        summary = (p["summary"] or p["kind"] or "").strip()
        lines.append(f"  {emoji} #{p['id']} {summary}")
    if len(pending) > _MAX_BRIEF_ITEMS:
        lines.append(f"  …and {len(pending) - _MAX_BRIEF_ITEMS} more")

    return "\n".join(lines)


def _fallback(kind: str, facts: dict) -> str:
    """Plain templated brief used when the LLM is unavailable."""
    label = "Morning brief" if kind == "morning" else "Evening brief"
    return f"{label}\n\n{_render_facts(kind, facts)}"


def generate_brief(
    conn: sqlite3.Connection, settings: Settings, llm: LLMClient, kind: str
) -> str:
    """Build a morning/evening brief. ``kind`` in {"morning","evening"}.

    Never raises: on any LLM failure (or a missing prompt) we return a plain
    templated summary instead.
    """
    kind = (kind or "morning").strip().lower()
    if kind not in ("morning", "evening"):
        kind = "morning"

    # ROOT CAUSE (control-state-presence-1): a paused agent (owner turned it OFF) still
    # generated and PUSHED the scheduled morning/evening brief, because pause only gated
    # inbound processing in main.poll_and_process. The scheduled brief is proactive output
    # the owner did not ask for, so it must go quiet while paused. Returning EMPTY_BRIEF
    # makes main.maybe_send_briefs skip the send (it only sends when text != EMPTY_BRIEF).
    # An on-demand /brief while paused thus also reads as "all quiet" rather than leaking
    # a digest. Best-effort: if pause state is unreadable, behave as before.
    try:
        if repo.is_paused(conn):
            log.info("paused — suppressing %s brief", kind)
            return EMPTY_BRIEF
    except Exception:  # noqa: BLE001
        pass

    since = since_epoch_for(kind, settings)
    facts = _gather(conn, since)

    # Nothing happened and nothing needs you → the one-line quiet state.
    if is_empty(facts):
        return EMPTY_BRIEF

    facts_text = _render_facts(kind, facts)

    try:
        system_prefix = prompts.load("brief", settings.prompts_dir)
    except FileNotFoundError:
        log.warning("brief prompt missing; using templated fallback")
        return _fallback(kind, facts)

    user_prompt = (
        f"Write the {kind} brief from these facts. Be concise, scannable, and "
        f"lead with anything that needs my decision.\n\n{facts_text}"
    )
    try:
        text = llm.complete_text(system_prefix=system_prefix, user_prompt=user_prompt)
        text = (text or "").strip()
        return text or _fallback(kind, facts)
    except LLMError as exc:
        log.warning("brief LLM call failed (%s); using templated fallback", exc)
        return _fallback(kind, facts)


# ═══════════════════════════════════════════════════════════════════════════════
# GAP 4 — structured morning brief (for GET /api/brief)
# ═══════════════════════════════════════════════════════════════════════════════
_BRIEF_CACHE_KEY = "brief_today"
_BRIEF_CACHE_TTL = 6 * _HOUR

# "awaiting" strings (written by the distill LLM) that refer to the OWNER — i.e. the
# situation is waiting on Jatin, not the other party.
_OWNER_AWAITING = frozenset({"owner", "me", "user", "owner", "you"})

_ATTENTION_TYPES = frozenset({"partner", "family", "investor", "mentor"})


def _commitment_bullets(conn: sqlite3.Connection, now: int) -> list[dict]:
    """Open commitments due within 48h of now."""
    out: list[dict] = []
    try:
        from assistant.memory import commitments as C
        today = datetime.fromtimestamp(now).date()
        for r in C.open_commitments(conn):
            due = (r["due_date"] or "").strip()
            if not due:
                continue
            try:
                due_date = datetime.strptime(due, "%Y-%m-%d").date()
            except ValueError:
                continue
            if (due_date - today).days <= 2:  # within 48h (today, tomorrow, or overdue)
                out.append({
                    "type": "commitment",
                    "text": (r["commitment_text"] or "").strip() or "a commitment",
                    "due": due,
                })
    except Exception:  # noqa: BLE001
        log.debug("commitment bullets failed (non-fatal)", exc_info=True)
    return out


def _situation_bullets(conn: sqlite3.Connection, now: int) -> tuple[list[dict], list[dict]]:
    """Open situations awaiting the OWNER. Returns (open_situations, relationship_attention).
    open_situations: last_activity_ts older than 12h. relationship_attention: a contact
    with an important relationship_type whose situation is unanswered > 24h."""
    open_out: list[dict] = []
    attn_out: list[dict] = []
    twelve_h = now - 12 * _HOUR
    twenty_four_h = now - 24 * _HOUR
    try:
        rows = conn.execute("SELECT * FROM relationship_memory").fetchall()
    except sqlite3.Error:
        return open_out, attn_out
    for row in rows:
        try:
            situations = json.loads(row["open_situations_json"] or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(situations, list):
            continue
        person_id = row["person_id"]
        rel_type = repo.person_relationship_type(conn, person_id)
        person = repo.person_get(conn, person_id)
        who = (person["display_name"] if person else "") or person_id or "someone"
        for sit in situations:
            if not isinstance(sit, dict):
                continue
            if str(sit.get("status", "")).lower() == "resolved":
                continue
            awaiting = str(sit.get("awaiting", "")).strip().lower()
            if awaiting not in _OWNER_AWAITING:
                continue
            last_ts = int(sit.get("last_activity_ts") or now)
            text = str(sit.get("situation", "")).strip() or "an open situation"
            if last_ts <= twelve_h:
                open_out.append({"type": "open_situation", "text": text, "contact": who})
            if rel_type in _ATTENTION_TYPES and last_ts <= twenty_four_h:
                attn_out.append({
                    "type": "relationship_attention",
                    "text": f"{who} ({rel_type}) is still waiting on you: {text}",
                })
    return open_out, attn_out


def _risk_bullets(conn: sqlite3.Connection, now: int) -> list[dict]:
    """Risk bullets: open commitments that are already overdue (past their due date)."""
    out: list[dict] = []
    try:
        from assistant.memory import commitments as C
        today = datetime.fromtimestamp(now).date()
        for r in C.open_commitments(conn):
            due = (r["due_date"] or "").strip()
            if not due:
                continue
            try:
                due_date = datetime.strptime(due, "%Y-%m-%d").date()
            except ValueError:
                continue
            if (due_date - today).days < 0:
                out.append({
                    "type": "risk",
                    "text": f"Overdue: {(r['commitment_text'] or '').strip()} (was due {due})",
                })
    except Exception:  # noqa: BLE001
        log.debug("risk bullets failed (non-fatal)", exc_info=True)
    return out


def _pick_top_priority(bullets: list[dict]) -> str:
    """Single most urgent line: the soonest-due commitment, else the first relationship
    attention / open situation, else any bullet, else a quiet note."""
    commitments = [b for b in bullets if b.get("type") == "commitment" and b.get("due")]
    if commitments:
        commitments.sort(key=lambda b: b.get("due") or "9999-99-99")
        return commitments[0]["text"]
    for t in ("risk", "relationship_attention", "open_situation"):
        for b in bullets:
            if b.get("type") == t:
                return b["text"]
    return bullets[0]["text"] if bullets else "Nothing urgent right now."


def generate_structured_brief(conn: sqlite3.Connection, *, now: int = 0) -> dict:
    """Build the structured morning brief (GAP 4) fresh from commitments + situations.
    Pure read; no LLM, no caching here (caching is handled by build_or_get_brief)."""
    now = now or int(time.time())
    bullets: list[dict] = []
    bullets.extend(_commitment_bullets(conn, now))
    open_sit, attn = _situation_bullets(conn, now)
    bullets.extend(open_sit)
    bullets.extend(attn)
    bullets.extend(_risk_bullets(conn, now))
    return {
        "generated_at": now,
        "bullets": bullets,
        "top_priority": _pick_top_priority(bullets),
    }


def build_or_get_brief(conn: sqlite3.Connection, *, now: int = 0, force: bool = False) -> dict:
    """Return the structured brief, served from the KV cache (`brief_today`) when it is
    fresh (< 6h old). Otherwise regenerate, store in KV as JSON, and return it."""
    now = now or int(time.time())
    if not force:
        try:
            cached_raw = repo.kv_get(conn, _BRIEF_CACHE_KEY)
            if cached_raw:
                cached = json.loads(cached_raw)
                gen = int(cached.get("generated_at") or 0)
                if gen and (now - gen) < _BRIEF_CACHE_TTL:
                    return cached
        except (ValueError, TypeError):
            pass
    brief = generate_structured_brief(conn, now=now)
    try:
        repo.kv_set(conn, _BRIEF_CACHE_KEY, json.dumps(brief))
    except Exception:  # noqa: BLE001 - caching is best-effort
        pass
    return brief
