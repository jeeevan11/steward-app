"""Layer 1D — learn the owner's WhatsApp talking style from his OWN sent messages.

WhatsApp voice is different from email (shorter, lowercase, casual), so it gets its own
profile, kept separate from the email voice profiles. We distill his recent outbound
WhatsApp messages (captured into wa_messages) into a compact style description cached
under kv "wa_voice_profile", and inject it into the WhatsApp drafting path so replies
sound like him. Never fabricates — it only reflects how he actually writes.
"""

from __future__ import annotations

import sqlite3

from assistant.config import Settings
from assistant.llm.client import LLMError
from assistant.logging_setup import get_logger
from assistant.storage import repositories as repo
from assistant.storage import wa_messages

log = get_logger("wa_style")

_KV_KEY = "wa_voice_profile"
_MIN_SAMPLES = 8        # need a real corpus before trusting a learned style
_MAX_SAMPLES = 40


def wa_style_prefix(conn: sqlite3.Connection, settings: Settings) -> str:
    """The cached WhatsApp-style guidance block for the drafter (or '')."""
    if not getattr(settings, "whatsapp_style_enabled", True):
        return ""
    try:
        prof = (repo.kv_get(conn, _KV_KEY) or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    if not prof:
        return ""
    return (
        "HOW I TEXT ON WHATSAPP (match this — my real style):\n" + prof
        + "\nKeep it that natural and that short. Never invent facts; use a "
        "[placeholder] for anything you cannot ground."
    )


def build_wa_style(conn: sqlite3.Connection, llm, settings: Settings) -> str:
    """Distill the owner's recent outbound WhatsApp messages into a style description and
    cache it. Returns the profile ('' when there aren't enough samples yet). Best-effort:
    an LLM failure leaves the previous profile untouched."""
    if not getattr(settings, "whatsapp_style_enabled", True):
        return ""
    try:
        rows = wa_messages.owner_outbound(conn, limit=_MAX_SAMPLES, group=False)
    except Exception:  # noqa: BLE001
        rows = []
    samples = [(r["body"] or "").strip() for r in rows if (r["body"] or "").strip()]
    if len(samples) < _MIN_SAMPLES:
        log.info("wa_style: only %d outbound samples (< %d) — not building yet",
                 len(samples), _MIN_SAMPLES)
        return repo.kv_get(conn, _KV_KEY) or ""

    corpus = "\n".join(f"- {s}" for s in samples[:_MAX_SAMPLES])
    try:
        summary = llm.complete_text(
            system_prefix=(
                "You analyze how someone texts so another writer can imitate them. "
                "Be concrete about tone, length, capitalization, punctuation, emoji "
                "use, greetings/sign-offs, and common phrasings. 4-6 short bullet points."
            ),
            user_prompt=(
                "Here are real WhatsApp messages I have sent. Describe my texting style "
                "so a writer could imitate it exactly:\n\n" + corpus
            ),
        ).strip()
    except (LLMError, Exception) as exc:  # noqa: BLE001 - keep old profile on failure
        log.warning("wa_style build failed (%s); keeping previous profile", exc)
        return repo.kv_get(conn, _KV_KEY) or ""

    if summary:
        repo.kv_set(conn, _KV_KEY, summary)
        log.info("wa_style rebuilt (%d samples, %d chars)", len(samples), len(summary))
    return summary or (repo.kv_get(conn, _KV_KEY) or "")
