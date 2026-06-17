"""Phase 12 — health checks + a shareable diagnostics bundle.

This is the "is everything actually running?" layer for distribution: a one-shot
read of the live system that a `--doctor` CLI, a `GET /api/diagnostics` endpoint, or
the Mac app's "Export diagnostics" button can all call.

SAFETY (the whole point): the exported bundle MUST be safe to paste into a bug
report. It therefore NEVER contains a secret value. Config secrets are reduced to
booleans (`secrets_present`) or replaced with `***set***`/`***missing***` in the
config summary, and the log tail is run through a redaction regex before it leaves
this module. ``export`` re-asserts no leakage right before writing.

Everything here is best-effort: every check is wrapped so a missing file, a closed
DB, or a malformed status.json yields a clear "unknown"/False rather than raising.
Stdlib only (sqlite3, json, pathlib, time, re).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

try:  # version is nice-to-have, never required
    from assistant import __version__ as _APP_VERSION
except Exception:  # noqa: BLE001
    _APP_VERSION = "unknown"

# Tables we report row counts for. Kept as a fixed allow-list so the bundle is
# stable and never accidentally dumps an unexpected (possibly sensitive) table.
_COUNT_TABLES = (
    "processed_messages",
    "pending_actions",
    "audit_log",
    "contacts",
    "rules",
    "voice_samples",
    "learning_events",
    "persons",
    "person_links",
    "relationship_memory",
    "commitments",
)

# Heuristic patterns for things that look like a secret, used to scrub the log tail
# and as a final leak tripwire before export. Deliberately broad — a false positive
# (over-redaction) is fine; a leak is not.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),                 # OpenRouter / OpenAI style keys
    re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}\b"),        # Telegram bot token (id:hash)
    re.compile(r"ya29\.[A-Za-z0-9_\-]{10,}"),             # Google OAuth access token
    re.compile(r"\b1//[A-Za-z0-9_\-]{20,}\b"),            # Google OAuth refresh token
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),               # Google API key
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)"
               r"\s*[=:]\s*\S+"),                          # generic key=value / bearer
)

_REDACTED = "***redacted***"
_SET = "***set***"
_MISSING = "***missing***"


# ─────────────────────────────────────────────────────────────────────────────
# Small best-effort readers
# ─────────────────────────────────────────────────────────────────────────────
def _status_json_path(settings) -> Path:
    """data/status.json — written by main._write_heartbeat next to the DB."""
    try:
        return Path(getattr(settings, "db_path", "./data/assistant.db")).parent / "status.json"
    except Exception:  # noqa: BLE001
        return Path("data/status.json")


def _relay_status_path(settings) -> Path:
    try:
        return Path(getattr(settings, "relay_status_path", "relay/status.json"))
    except Exception:  # noqa: BLE001
        return Path("relay/status.json")


def _log_path(settings) -> Path:
    try:
        return Path(getattr(settings, "log_path", "./data/assistant.log"))
    except Exception:  # noqa: BLE001
        return Path("data/assistant.log")


def _read_json(path: Path) -> Optional[dict]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - malformed / unreadable → unknown
        return None


def _age_seconds(ts: Any, now: Optional[float] = None) -> Optional[float]:
    """Seconds since an epoch-seconds timestamp, or None if it isn't a number."""
    try:
        now = time.time() if now is None else now
        return max(0.0, float(now) - float(ts))
    except Exception:  # noqa: BLE001
        return None


def _table_count(conn: sqlite3.Connection, table: str) -> Any:
    try:
        cur = conn.execute(f"SELECT COUNT(*) AS n FROM {table}")  # table from fixed allow-list
        row = cur.fetchone()
        return int(row["n"]) if row is not None else "unknown"
    except Exception:  # noqa: BLE001 - table missing / db closed
        return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────
def health_check(conn: sqlite3.Connection, settings, *, now: Optional[float] = None,
                 fresh_seconds: int = 180) -> dict:
    """Per-component status snapshot. Every field is best-effort and degrades to a
    clear "unknown"/False; this function never raises.

    ``fresh_seconds`` is how recent the engine heartbeat must be to count as fresh
    (defaults to the relay stale threshold, ~3 min)."""
    now = time.time() if now is None else now
    health: dict[str, Any] = {
        "engine_heartbeat_fresh": False,
        "engine_heartbeat_age_seconds": "unknown",
        "relay_connected": False,
        "relay_age_seconds": "unknown",
        "db_ok": False,
        "db_size_bytes": "unknown",
        "pending_count": "unknown",
        "last_24h_counts": {},
        "ledger_failed_count": "unknown",
        "email_enabled": bool(getattr(settings, "email_enabled", False)),
        "whatsapp_enabled": bool(getattr(settings, "whatsapp_enabled", False)),
        "mode": getattr(settings, "mode", "unknown"),
        "dry_run": bool(getattr(settings, "dry_run", True)),
        "oldest_unprocessed_age_seconds": "unknown",
    }

    # Engine heartbeat freshness (data/status.json).
    try:
        st = _read_json(_status_json_path(settings))
        if st is not None:
            age = _age_seconds(st.get("heartbeat_ts"), now)
            if age is not None:
                health["engine_heartbeat_age_seconds"] = round(age, 1)
                health["engine_heartbeat_fresh"] = age <= float(fresh_seconds)
    except Exception:  # noqa: BLE001
        pass

    # Relay connectivity (relay/status.json).
    try:
        relay = _read_json(_relay_status_path(settings))
        if relay is not None:
            age = _age_seconds(relay.get("updated_at"), now)
            stale_cap = float(getattr(settings, "relay_stale_alert_seconds", 180) or 180)
            if age is not None:
                health["relay_age_seconds"] = round(age, 1)
            health["relay_connected"] = bool(relay.get("connected")) and (
                age is not None and age <= stale_cap)
    except Exception:  # noqa: BLE001
        pass

    # DB liveness + on-disk size.
    try:
        conn.execute("SELECT 1").fetchone()
        health["db_ok"] = True
    except Exception:  # noqa: BLE001
        health["db_ok"] = False
    try:
        db_path = getattr(settings, "db_path", "")
        if db_path and db_path != ":memory:":
            p = Path(db_path)
            if p.exists():
                health["db_size_bytes"] = p.stat().st_size
    except Exception:  # noqa: BLE001
        pass

    # Pending work + last-24h tallies + failed-ledger count (read straight from SQL
    # so we never import the poller, and a missing table just yields "unknown").
    try:
        from assistant.storage import repositories as repo
        health["pending_count"] = len(repo.open_pending(conn))
    except Exception:  # noqa: BLE001
        try:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM pending_actions "
                "WHERE status IN ('PENDING','APPROVED','EDITED')")
            health["pending_count"] = int(cur.fetchone()["n"])
        except Exception:  # noqa: BLE001
            health["pending_count"] = "unknown"

    try:
        from assistant.storage import ledger
        health["last_24h_counts"] = ledger.counts_since(conn, int(now) - 86400)
    except Exception:  # noqa: BLE001
        health["last_24h_counts"] = {}

    try:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM processed_messages WHERE state='FAILED'")
        health["ledger_failed_count"] = int(cur.fetchone()["n"])
    except Exception:  # noqa: BLE001
        health["ledger_failed_count"] = "unknown"

    # Oldest still-unprocessed message (SEEN/PROCESSING) — a growing number here means
    # the engine is wedged. created_at is epoch seconds.
    try:
        cur = conn.execute(
            "SELECT MIN(created_at) AS oldest FROM processed_messages "
            "WHERE state IN ('SEEN','PROCESSING')")
        row = cur.fetchone()
        oldest = row["oldest"] if row is not None else None
        if oldest is not None:
            age = _age_seconds(oldest, now)
            health["oldest_unprocessed_age_seconds"] = round(age, 1) if age is not None else "unknown"
        else:
            health["oldest_unprocessed_age_seconds"] = 0
    except Exception:  # noqa: BLE001
        health["oldest_unprocessed_age_seconds"] = "unknown"

    return health


# ─────────────────────────────────────────────────────────────────────────────
# Secrets — booleans ONLY, never values
# ─────────────────────────────────────────────────────────────────────────────
def secrets_present(settings) -> dict:
    """Which secrets are configured. Returns ONLY booleans — never a single byte of
    any secret value. Safe to log, export, or show in a UI."""
    out = {
        "openrouter_key_set": False,
        "telegram_token_set": False,
        "telegram_chat_id_set": False,
        "gmail_creds_present": False,
        "gmail_token_present": False,
    }
    try:
        out["openrouter_key_set"] = bool(getattr(settings, "openrouter_api_key", ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        out["telegram_token_set"] = bool(getattr(settings, "telegram_bot_token", ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        out["telegram_chat_id_set"] = bool(getattr(settings, "telegram_chat_id", ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        out["gmail_creds_present"] = Path(
            getattr(settings, "gmail_credentials_path", "")).exists()
    except Exception:  # noqa: BLE001
        pass
    try:
        out["gmail_token_present"] = Path(
            getattr(settings, "gmail_token_path", "")).exists()
    except Exception:  # noqa: BLE001
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Redaction helpers
# ─────────────────────────────────────────────────────────────────────────────
def _scrub(text: str) -> str:
    """Replace anything that looks like a key/token in free text with a marker."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out


def _config_summary(settings) -> dict:
    """Non-secret config, with EVERY secret field forced to ***set***/***missing***.

    We enumerate non-secret fields explicitly (allow-list) rather than dumping the
    dataclass, so a future secret field can never leak by being added upstream."""
    secret_fields = (
        "openrouter_api_key",
        "telegram_bot_token",
        "telegram_chat_id",
    )
    safe_fields = (
        "openrouter_base_url", "judge_model", "noise_model", "draft_model", "pro_model",
        "mode", "email_enabled", "whatsapp_enabled", "poll_interval_seconds",
        "surface_confidence_threshold", "autonomy_confidence_threshold",
        "vip_importance_threshold", "calendar_enabled", "memory_enabled",
        "proactive_enabled", "quality_gate_enabled", "retention_enabled",
        "retention_days", "relay_stale_alert_seconds", "timezone",
        "gmail_address", "gmail_credentials_path", "gmail_token_path",
        "db_path", "log_path", "prompts_dir", "relay_status_path",
        "gmail_pubsub_topic", "gmail_pubsub_port",
    )
    summary: dict[str, Any] = {}
    for f in safe_fields:
        try:
            val = getattr(settings, f, None)
            # gmail_address can be a real address — keep it (an email is not a secret,
            # and it's the owner's own), but scrub defensively in case anything odd.
            summary[f] = _scrub(val) if isinstance(val, str) else val
        except Exception:  # noqa: BLE001
            summary[f] = "unknown"
    for f in secret_fields:
        try:
            summary[f] = _SET if getattr(settings, f, "") else _MISSING
        except Exception:  # noqa: BLE001
            summary[f] = _MISSING
    # dry_run is a derived property, surface it explicitly.
    try:
        summary["dry_run"] = bool(getattr(settings, "dry_run", True))
    except Exception:  # noqa: BLE001
        summary["dry_run"] = True
    return summary


def _log_tail(settings, lines: int = 50) -> list[str]:
    """Last ~N lines of the app log, each scrubbed of anything resembling a secret.
    Best-effort — a missing/unreadable log yields an empty list."""
    try:
        p = _log_path(settings)
        if not p.exists():
            return []
        raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return [_scrub(line) for line in raw[-int(lines):]]
    except Exception:  # noqa: BLE001
        return []


def _table_counts(conn: sqlite3.Connection) -> dict:
    return {t: _table_count(conn, t) for t in _COUNT_TABLES}


# ─────────────────────────────────────────────────────────────────────────────
# Full bundle
# ─────────────────────────────────────────────────────────────────────────────
def collect(conn: sqlite3.Connection, settings, *, now: Optional[float] = None,
            log_lines: int = 50) -> dict:
    """Assemble the full, share-safe diagnostics bundle. No secret values inside."""
    now = time.time() if now is None else now
    bundle: dict[str, Any] = {
        "app": {
            "name": "local-first-assistant",
            "version": _APP_VERSION,
            "generated_at_epoch": int(now),
        },
        "health": {},
        "secrets_present": {},
        "config": {},
        "table_counts": {},
        "log_tail": [],
    }
    try:
        bundle["health"] = health_check(conn, settings, now=now)
    except Exception:  # noqa: BLE001
        bundle["health"] = {"error": "health_check failed"}
    try:
        bundle["secrets_present"] = secrets_present(settings)
    except Exception:  # noqa: BLE001
        bundle["secrets_present"] = {}
    try:
        bundle["config"] = _config_summary(settings)
    except Exception:  # noqa: BLE001
        bundle["config"] = {}
    try:
        bundle["table_counts"] = _table_counts(conn)
    except Exception:  # noqa: BLE001
        bundle["table_counts"] = {}
    try:
        bundle["log_tail"] = _log_tail(settings, lines=log_lines)
    except Exception:  # noqa: BLE001
        bundle["log_tail"] = []
    return bundle


def _assert_no_secret_leak(bundle: dict, settings) -> dict:
    """Defense in depth: walk the serialized bundle and (a) scrub any pattern-matched
    secret, and (b) scrub any literal known secret value from settings. Returns a
    cleaned bundle. Never raises."""
    try:
        text = json.dumps(bundle, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return bundle

    # Literal known secrets from settings — replace exact substrings if they somehow
    # made it in. Only non-empty, reasonably long values (avoid nuking common words).
    literals = []
    for f in ("openrouter_api_key", "telegram_bot_token"):
        try:
            v = getattr(settings, f, "")
            if isinstance(v, str) and len(v) >= 6:
                literals.append(v)
        except Exception:  # noqa: BLE001
            pass

    cleaned = text
    for v in literals:
        if v and v in cleaned:
            cleaned = cleaned.replace(v, _REDACTED)
    cleaned = _scrub(cleaned)

    if cleaned == text:
        return bundle  # nothing changed → original is clean
    try:
        return json.loads(cleaned)
    except Exception:  # noqa: BLE001
        # If re-parse fails for any reason, fall back to a minimal safe bundle.
        return {"app": bundle.get("app", {}), "note": "redacted bundle re-parse failed"}


def export(conn: sqlite3.Connection, settings, out_path: Optional[str] = None, *,
           stamp: Optional[str] = None, now: Optional[float] = None,
           log_lines: int = 50) -> str:
    """Write the diagnostics bundle as pretty JSON and return the path.

    Default destination is ``<db dir>/diagnostics-<stamp>.json``. ``stamp`` is an
    explicit, caller-supplied id (so tests are deterministic — no hidden Date.now);
    if omitted it falls back to the integer epoch from ``now``. The bundle is run
    through a final no-leak scrub immediately before writing."""
    bundle = collect(conn, settings, now=now, log_lines=log_lines)
    bundle = _assert_no_secret_leak(bundle, settings)

    if out_path is None:
        if stamp is None:
            stamp = str(int(time.time() if now is None else now))
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(stamp))
        try:
            base = Path(getattr(settings, "db_path", "./data/assistant.db")).parent
        except Exception:  # noqa: BLE001
            base = Path("data")
        out_path = str(base / f"diagnostics-{safe}.json")

    p = Path(out_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    p.write_text(json.dumps(bundle, indent=2, sort_keys=True, default=str) + "\n",
                 encoding="utf-8")
    return str(p)


# ─────────────────────────────────────────────────────────────────────────────
# Human summary (for --doctor and the Mac app)
# ─────────────────────────────────────────────────────────────────────────────
def _ok(flag: Any) -> str:
    if flag is True:
        return "OK"
    if flag is False:
        return "DOWN"
    return "unknown"


def format_health(health: dict) -> str:
    """A short, human-readable health summary. Pure formatting; never raises."""
    if not isinstance(health, dict):
        return "diagnostics: unavailable"
    g = health.get
    lines = [
        "Assistant health check",
        "─" * 32,
        f"  engine heartbeat : {_ok(g('engine_heartbeat_fresh'))} "
        f"(age {g('engine_heartbeat_age_seconds')}s)",
        f"  database         : {_ok(g('db_ok'))} "
        f"(size {g('db_size_bytes')} bytes)",
        f"  mode             : {g('mode')} (dry_run={g('dry_run')})",
        f"  email channel    : {'on' if g('email_enabled') else 'off'}",
        f"  whatsapp channel : {'on' if g('whatsapp_enabled') else 'off'}",
        f"  whatsapp relay   : {_ok(g('relay_connected'))} "
        f"(age {g('relay_age_seconds')}s)",
        f"  pending items    : {g('pending_count')}",
        f"  failed (ledger)  : {g('ledger_failed_count')}",
        f"  oldest unproc.   : {g('oldest_unprocessed_age_seconds')}s",
        f"  last 24h         : {g('last_24h_counts')}",
    ]
    return "\n".join(lines)
