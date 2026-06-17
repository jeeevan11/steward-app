"""First-run bootstrap.

`run_onboarding` reads the user's recent SENT mail directly from the Gmail API and
uses it to seed the assistant's memory. It is intentionally *best-effort*: every
step is independently guarded so a failure in one (e.g. the voice-profile LLM call)
still lets the others (e.g. VIP tallying) complete, and ANY unexpected error is
caught so onboarding can never crash startup. The function returns a human-readable
summary of what it managed to do.

The Gmail HTTP client (googleapiclient) and the sibling ``ingest``/``action``
packages are imported lazily inside the functions so this module imports cleanly
even when those optional dependencies / sibling modules are not yet present.
"""

from __future__ import annotations

import base64
import sqlite3
from email.utils import getaddresses, parseaddr
from typing import Any, Optional

from assistant.config import Settings
from assistant.logging_setup import get_logger
from assistant.storage import repositories as repo

log = get_logger("onboarding")

_ONBOARDED_KEY = "onboarded"

# How many sent messages count toward "you email them a lot" → likely VIP.
_VIP_MIN_SENT = 3
# How many top recipients to flag as inferred VIPs (kept small & conservative).
_VIP_MAX = 10
# A modest importance bump for inferred VIPs — below the default VIP floor so it is
# a hint, not an automatic promotion. The user/threshold still decides.
_VIP_IMPORTANCE = 55
# Min characters for a sent body to be worth keeping as a voice sample.
_VOICE_MIN_CHARS = 40
_VOICE_MAX_CHARS = 4000


# ─────────────────────────────────────────────────────────────────────────────
# Onboarded flag
# ─────────────────────────────────────────────────────────────────────────────
def is_onboarded(conn: sqlite3.Connection) -> bool:
    """True once a (possibly partial) onboarding run has completed."""
    return repo.kv_get_bool(conn, _ONBOARDED_KEY, False)


def mark_onboarded(conn: sqlite3.Connection) -> None:
    repo.kv_set_bool(conn, _ONBOARDED_KEY, True)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
def run_onboarding(
    conn: sqlite3.Connection,
    settings: Settings,
    mail: Any,
    llm: Any,
    notifier: Any = None,
    max_sent: int = 200,
) -> str:
    """Best-effort first-run sweep. Never raises; always returns a summary string.

    Steps (each independently guarded):
      1. Mine recent SENT messages → voice samples + recipient tallies.
      2. Build the voice profile summary from those samples.
      3. Flag inferred VIPs from the recipient tallies.
      4. Seed 1-2 conservative default rules.
      5. Mark onboarded so this never repeats.
    """
    parts: list[str] = []
    try:
        if is_onboarded(conn):
            return "Onboarding already completed earlier; skipping."

        service = _gmail_service(settings, mail)

        # 1) Mine sent mail.
        samples = 0
        recipients: dict[str, dict[str, Any]] = {}
        if service is not None:
            try:
                samples, recipients = _mine_sent(
                    conn, service, settings, max_sent=max_sent
                )
                parts.append(f"mined {samples} voice sample(s) from sent mail")
            except Exception as exc:  # noqa: BLE001
                log.warning("onboarding: mining sent mail failed: %s", exc)
                parts.append("could not read sent mail (skipped voice mining)")
        else:
            parts.append("Gmail service unavailable (skipped voice mining)")

        # 2) Build the voice profile (needs samples; tolerate LLM failure).
        try:
            profile = _build_voice_profile(conn, llm, settings)
            if profile:
                parts.append("built voice profile")
        except Exception as exc:  # noqa: BLE001
            log.warning("onboarding: building voice profile failed: %s", exc)

        # 3) Infer VIPs from recipient tallies.
        try:
            vips = _flag_vips(conn, recipients)
            if vips:
                parts.append(f"flagged {vips} likely VIP contact(s)")
        except Exception as exc:  # noqa: BLE001
            log.warning("onboarding: flagging VIPs failed: %s", exc)

        # 4) Seed conservative default rules.
        try:
            seeded = _seed_default_rules(conn)
            if seeded:
                parts.append(f"seeded {seeded} default rule(s)")
        except Exception as exc:  # noqa: BLE001
            log.warning("onboarding: seeding default rules failed: %s", exc)

        # 5) Mark done so we never repeat (even a partial run counts).
        mark_onboarded(conn)

        summary = "Onboarding complete: " + ("; ".join(parts) if parts else "nothing to do")
        log.info(summary)
        _notify(notifier, summary)
        return summary
    except Exception as exc:  # noqa: BLE001 - the whole thing is best-effort
        log.exception("onboarding failed (non-fatal): %s", exc)
        # Still mark onboarded so a hard failure doesn't make us retry every boot
        # against the same broken state; the user can re-run explicitly later.
        try:
            mark_onboarded(conn)
        except Exception:  # noqa: BLE001
            pass
        partial = "; ".join(parts) if parts else "no steps completed"
        msg = f"Onboarding hit an error and finished partially ({partial}): {exc}"
        _notify(notifier, msg)
        return msg


# ─────────────────────────────────────────────────────────────────────────────
# Gmail access
# ─────────────────────────────────────────────────────────────────────────────
def _gmail_service(settings: Settings, mail: Any) -> Optional[Any]:
    """Obtain a raw Gmail API service.

    Prefer an existing service hung off the GmailSource (``mail.service`` /
    ``mail.conn``); otherwise build one via ingest.gmail_auth.build_service. All
    failures degrade to None so onboarding can skip the mining step gracefully.
    """
    # Reuse whatever the live mail source already authenticated, if exposed.
    for attr in ("service", "_service", "conn", "_conn"):
        svc = getattr(mail, attr, None)
        if svc is not None and hasattr(svc, "users"):
            return svc
    try:
        from assistant.ingest import gmail_auth  # lazy: package may not exist yet

        return gmail_auth.build_service(settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("onboarding: could not build Gmail service: %s", exc)
        return None


def _mine_sent(
    conn: sqlite3.Connection,
    service: Any,
    settings: Settings,
    *,
    max_sent: int,
) -> tuple[int, dict[str, dict[str, Any]]]:
    """Walk recent SENT messages, storing voice samples and tallying recipients.

    Returns (sample_count, recipients) where recipients maps lowercased email →
    {"count": int, "name": str}.
    """
    me = (settings.gmail_address or "").lower()
    sample_count = 0
    recipients: dict[str, dict[str, Any]] = {}

    ids = _list_sent_ids(service, max_sent)
    for mid in ids:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=mid, format="full")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - skip any single bad message
            log.debug("onboarding: get message %s failed: %s", mid, exc)
            continue

        headers = _headers(msg)
        subject = headers.get("subject", "")
        to_field = " ".join(
            v for k, v in (headers_pairs(msg)) if k in ("to", "cc")
        )

        # Tally recipients (for VIP inference).
        for name, addr in getaddresses([to_field]):
            addr = (addr or "").lower().strip()
            if not addr or addr == me:
                continue
            slot = recipients.setdefault(addr, {"count": 0, "name": ""})
            slot["count"] += 1
            if name and not slot["name"]:
                slot["name"] = name
            # Keep contact stats fresh: we sent to them.
            try:
                repo.bump_contact_stats(conn, addr, sent_to=1, name=name or "")
            except Exception as exc:  # noqa: BLE001
                log.debug("onboarding: bump_contact_stats(%s) failed: %s", addr, exc)

        # Extract a plaintext body for the voice sample.
        body = _extract_plaintext(msg.get("payload", {}))
        body = (body or "").strip()
        if len(body) >= _VOICE_MIN_CHARS:
            # Attribute the sample to the first concrete recipient so future drafts
            # to that person can echo this person-specific voice.
            primary = next(
                (a for _, a in getaddresses([to_field]) if a and a.lower() != me),
                "",
            )
            try:
                repo.add_voice_sample(
                    conn,
                    body=body[:_VOICE_MAX_CHARS],
                    subject=subject,
                    contact_email=(primary or None),
                )
                sample_count += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("onboarding: add_voice_sample failed: %s", exc)

    return sample_count, recipients


def _list_sent_ids(service: Any, max_sent: int) -> list[str]:
    """Page through SENT label message ids up to ``max_sent``."""
    ids: list[str] = []
    page_token: Optional[str] = None
    remaining = max(0, int(max_sent))
    while remaining > 0:
        batch = min(100, remaining)
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                labelIds=["SENT"],
                maxResults=batch,
                pageToken=page_token,
            )
            .execute()
        )
        for m in resp.get("messages", []) or []:
            mid = m.get("id")
            if mid:
                ids.append(mid)
        remaining -= batch
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids[:max_sent]


# ─────────────────────────────────────────────────────────────────────────────
# Gmail payload helpers (stdlib only)
# ─────────────────────────────────────────────────────────────────────────────
def headers_pairs(msg: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for h in msg.get("payload", {}).get("headers", []) or []:
        name = (h.get("name") or "").lower()
        out.append((name, h.get("value") or ""))
    return out


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    """Lowercased header name → value (last wins)."""
    return {k: v for k, v in headers_pairs(msg)}


def _extract_plaintext(payload: dict[str, Any]) -> str:
    """Depth-first extraction of the best text/plain body from a Gmail payload."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    data = body.get("data")
    if mime == "text/plain" and data:
        return _b64(data)
    # Recurse into multiparts; prefer text/plain, fall back to stripped text/html.
    parts = payload.get("parts") or []
    html_fallback = ""
    for part in parts:
        text = _extract_plaintext(part)
        if part.get("mimeType") == "text/plain" and text:
            return text
        if part.get("mimeType") == "text/html" and not html_fallback:
            html_fallback = _strip_html(text or _b64((part.get("body") or {}).get("data")))
        elif text and not html_fallback:
            html_fallback = text
    if mime == "text/html" and data:
        return _strip_html(_b64(data))
    return html_fallback


def _b64(data: Optional[str]) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001
        return ""


def _strip_html(html: str) -> str:
    """Very small HTML→text reduction (stdlib only). Good enough for voice mining."""
    if not html:
        return ""
    import re

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Voice profile
# ─────────────────────────────────────────────────────────────────────────────
def _build_voice_profile(conn: sqlite3.Connection, llm: Any, settings: Settings) -> str:
    """Delegate to action.voice.build_voice_profile (the canonical implementation).

    Imported lazily; returns "" if the sibling module isn't available yet or the
    LLM call fails.
    """
    try:
        from assistant.action import voice  # lazy: sibling package may not exist yet
    except Exception as exc:  # noqa: BLE001
        log.warning("onboarding: action.voice unavailable: %s", exc)
        return ""
    try:
        return voice.build_voice_profile(conn, llm, settings) or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("onboarding: build_voice_profile failed: %s", exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# VIP inference
# ─────────────────────────────────────────────────────────────────────────────
def _flag_vips(conn: sqlite3.Connection, recipients: dict[str, dict[str, Any]]) -> int:
    """Promote the people you email most into conservative inferred VIPs.

    We only nudge importance (we never set the 'vip' flag automatically) and only
    for clear, repeated correspondents — so this stays a hint, not an auto-promotion.
    """
    if not recipients:
        return 0
    ranked = sorted(
        recipients.items(), key=lambda kv: kv[1].get("count", 0), reverse=True
    )
    flagged = 0
    for email, info in ranked:
        if flagged >= _VIP_MAX:
            break
        if info.get("count", 0) < _VIP_MIN_SENT:
            continue
        try:
            contact = repo.get_or_default_contact(conn, email, info.get("name", ""))
            # Only raise importance, never lower it; respect any user override.
            if contact.importance < _VIP_IMPORTANCE:
                contact.importance = _VIP_IMPORTANCE
            if info.get("name") and not contact.name:
                contact.name = info["name"]
            repo.upsert_contact(conn, contact)
            flagged += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("onboarding: flag VIP %s failed: %s", email, exc)
    return flagged


# ─────────────────────────────────────────────────────────────────────────────
# Default rules
# ─────────────────────────────────────────────────────────────────────────────
def _seed_default_rules(conn: sqlite3.Connection) -> int:
    """Seed 1-2 conservative, broadly-safe category rules.

    These are reversible filing rules only (never anything that sends or deletes),
    and we don't duplicate a rule the user may already have for the same category.
    They are created ACTIVE because they are hand-picked safe defaults (filing
    newsletters / promos), unlike *inferred* rules which must be confirmed.
    """
    seeded = 0
    defaults = [
        # (category, instruction, action)
        ("newsletter",
         "File newsletters quietly into a Newsletters label; don't notify me.",
         "label:Newsletters"),
        ("spam_promotional",
         "Archive obvious promotional mail quietly; don't notify me.",
         "archive"),
    ]
    for category, instruction, action in defaults:
        try:
            if _category_rule_exists(conn, category):
                continue
            repo.add_rule(
                conn,
                scope="category",
                match_key=category,
                instruction=instruction,
                action=action,
                status="active",
                source="user",   # treated as a curated default, not an inference
                confidence=1.0,
            )
            seeded += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("onboarding: seed rule for %s failed: %s", category, exc)

    # Jatin's standing rules (global, advisory to the classifier + shown in the
    # dashboard; the hard enforcement lives in brain/guardrails.py).
    owner_rules = [
        ("Recruiters and headhunters → always file silently (Tier 0), never surface.", "archive"),
        ("Hardware or manufacturing supplier → minimum a drafted reply for approval (Tier 2).", ""),
        ("Any legal document → always ask before acting (Tier 3).", ""),
        ("a venture firm network contact → always ask (Tier 3).", ""),
    ]
    for instruction, action in owner_rules:
        try:
            if _global_rule_exists(conn, instruction):
                continue
            repo.add_rule(
                conn, scope="global", match_key="", instruction=instruction,
                action=action, status="active", source="user", confidence=1.0,
            )
            seeded += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("onboarding: seed Jatin rule failed: %s", exc)
    return seeded


def _global_rule_exists(conn: sqlite3.Connection, instruction: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM rules WHERE scope='global' AND instruction=? "
        "AND status IN ('active','proposed') LIMIT 1",
        (instruction,),
    ).fetchone()
    return row is not None


def _category_rule_exists(conn: sqlite3.Connection, category: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM rules WHERE scope='category' AND match_key=? "
        "AND status IN ('active','proposed') LIMIT 1",
        (category.lower(),),
    ).fetchone()
    return row is not None


# ─────────────────────────────────────────────────────────────────────────────
# Notify
# ─────────────────────────────────────────────────────────────────────────────
def _notify(notifier: Any, text: str) -> None:
    if notifier is None:
        return
    try:
        notifier.fyi(text)
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
        log.debug("onboarding: notify failed: %s", exc)
