"""Voice: how YOU sound in writing, distilled for the drafter.

Two responsibilities:
  * `voice_prefix` — assemble the per-draft voice block appended to the drafting
    system prompt: a stored global voice-profile summary plus up to 5 few-shot
    samples preferring mail you wrote to *this* contact.
  * `build_voice_profile` — summarize your global Sent samples into a compact
    style description and cache it under the kv key "voice_profile".

Stdlib + repo + the LLM client only. Never fabricates; it only reflects samples
you actually wrote.
"""

from __future__ import annotations

import json
import sqlite3

from assistant.config import Settings
from assistant.llm import prompts
from assistant.llm.client import LLMClient, LLMError
from assistant.logging_setup import get_logger
from assistant.storage import repositories as repo

log = get_logger("voice")

# A segment needs at least this many samples before its own profile is trusted;
# below it we fall back to the global voice profile (P5a).
MIN_SEGMENT_SAMPLES = 5

_GENERIC_INSTRUCTION = (
    "VOICE GUIDE:\n"
    "No writing samples are available yet for this person. Write in a natural, "
    "concise, professional-but-warm tone. Be direct, avoid filler and corporate "
    "boilerplate, and keep it short. Do not invent facts, commitments, dates, or "
    "numbers — leave a clearly-marked [placeholder] if a specific detail is needed."
)

_MAX_SAMPLE_CHARS = 1200  # cap each few-shot sample so the prefix stays compact


def _format_sample(row: sqlite3.Row) -> str:
    subject = (row["subject"] or "").strip()
    body = (row["body"] or "").strip()
    if len(body) > _MAX_SAMPLE_CHARS:
        body = body[:_MAX_SAMPLE_CHARS].rstrip() + " …"
    header = f"Subject: {subject}\n" if subject else ""
    return f"{header}{body}".strip()


def voice_prefix(conn: sqlite3.Connection, contact_email: str, settings: Settings) -> str:
    """Build the voice block appended to the drafting system prompt.

    Combines the stored global voice-profile summary (kv key "voice_profile") with
    up to 5 few-shot samples, preferring ones written to this contact. Returns a
    generic instruction when nothing is available.
    """
    parts: list[str] = []

    # P5a: prefer this audience's segment profile; fall back to the global one when
    # the segment is thin (< MIN_SEGMENT_SAMPLES) or absent.
    profile, seg = _segment_profile(conn, contact_email)
    if not profile:
        profile = (repo.kv_get(conn, "voice_profile") or "").strip()
        seg = ""
    if profile:
        label = f"HOW I WRITE to {seg} contacts" if seg else "HOW I WRITE (voice profile)"
        parts.append(f"{label}:\n{profile}")

    samples: list[sqlite3.Row] = []
    try:
        samples = repo.get_voice_samples(conn, contact_email or "", limit=5)
    except sqlite3.Error as exc:  # defensive: a DB hiccup must not block drafting
        log.warning("could not load voice samples: %s", exc)

    if samples:
        rendered = [s for s in (_format_sample(r) for r in samples) if s]
        if rendered:
            parts.append(
                "EXAMPLES OF HOW I ACTUALLY WRITE (mimic this tone, structure, "
                "sign-off and level of formality — do NOT copy their content):\n\n"
                + "\n\n--- sample ---\n\n".join(rendered)
            )

    if not parts:
        return _GENERIC_INSTRUCTION

    parts.append(
        "Match the voice above. Stay concise and direct. Never fabricate facts, "
        "names, dates, figures, or commitments — use a [placeholder] for anything "
        "you cannot ground in the thread."
    )
    return "\n\n".join(parts)


def build_voice_profile(conn: sqlite3.Connection, llm: LLMClient, settings: Settings) -> str:
    """Summarize the global voice samples into a reusable style description.

    Reads up to a handful of global samples, asks the model (via the voice_profile
    prompt) to distill the writing style, stores the result under kv "voice_profile",
    and returns it. On no samples or LLM failure, returns/caches a safe generic
    description rather than raising.
    """
    samples = repo.get_voice_samples(conn, "", limit=5)
    if not samples:
        log.info("no voice samples available; storing generic voice profile")
        repo.kv_set(conn, "voice_profile", _GENERIC_INSTRUCTION)
        return _GENERIC_INSTRUCTION

    corpus = "\n\n--- sample ---\n\n".join(
        s for s in (_format_sample(r) for r in samples) if s
    )

    try:
        system = prompts.load("voice_profile", settings.prompts_dir)
        summary = llm.complete_text(
            system_prefix=system,
            user_prompt=(
                "Here are samples of emails I have written. Summarize my writing "
                "voice and style so another writer could imitate it:\n\n" + corpus
            ),
        ).strip()
    except (LLMError, FileNotFoundError, OSError) as exc:
        log.warning("voice profile build failed (%s); keeping generic profile", exc)
        repo.kv_set(conn, "voice_profile", _GENERIC_INSTRUCTION)
        return _GENERIC_INSTRUCTION

    if not summary:
        summary = _GENERIC_INSTRUCTION
    repo.kv_set(conn, "voice_profile", summary)
    log.info("voice profile rebuilt (%d chars)", len(summary))
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Segmented voice profiles (P5a)
# ─────────────────────────────────────────────────────────────────────────────
def _segment_profile(conn: sqlite3.Connection, contact_email: str) -> tuple[str, str]:
    """Return (summary, segment) for this contact's segment profile, or ('', '') when
    the segment is thin/absent."""
    try:
        from assistant.memory import contacts as memory_contacts

        seg = memory_contacts.detect_segment(conn, contact_email)
        row = repo.get_voice_profile(conn, seg)
        if row and int(row["sample_count"] or 0) >= MIN_SEGMENT_SAMPLES:
            summary = json.loads(row["profile_json"] or "{}").get("summary", "").strip()
            if summary:
                return summary, seg
    except Exception as exc:  # noqa: BLE001 - never block drafting on profile lookup
        log.debug("segment profile lookup failed: %s", exc)
    return "", ""


def build_segment_profiles(conn: sqlite3.Connection, llm: LLMClient, settings: Settings) -> dict[str, int]:
    """Rebuild per-segment voice profiles from voice_samples (bucketed by the sender's
    segment). A segment with < MIN_SEGMENT_SAMPLES is skipped (keeps its old profile).
    Returns {segment: sample_count} for segments rebuilt. Best-effort per segment."""
    from assistant.memory import contacts as memory_contacts

    buckets: dict[str, list[sqlite3.Row]] = {s: [] for s in memory_contacts.SEGMENTS}
    for row in repo.all_voice_samples(conn):
        email = row["contact_email"] or ""
        seg = memory_contacts.detect_segment(conn, email) if email else "external"
        buckets.setdefault(seg, []).append(row)

    rebuilt: dict[str, int] = {}
    for seg, samples in buckets.items():
        if len(samples) < MIN_SEGMENT_SAMPLES:
            log.info("segment %s has %d samples (< %d) — skipping rebuild",
                     seg, len(samples), MIN_SEGMENT_SAMPLES)
            continue
        corpus = "\n\n--- sample ---\n\n".join(
            s for s in (_format_sample(r) for r in samples[:12]) if s
        )
        try:
            system = prompts.load("voice_profile", settings.prompts_dir)
            summary = llm.complete_text(
                system_prefix=system,
                user_prompt=(
                    f"Here are emails I have written to {seg} contacts. Summarize my "
                    f"writing voice and style for this audience:\n\n{corpus}"
                ),
            ).strip()
        except (LLMError, FileNotFoundError, OSError) as exc:
            log.warning("segment %s profile build failed (%s); skipping", seg, exc)
            continue
        examples = [(_format_sample(r) or "")[:400] for r in samples[:3]]
        repo.upsert_voice_profile(
            conn, seg, json.dumps({"summary": summary, "examples": examples}), len(samples)
        )
        rebuilt[seg] = len(samples)
        log.info("rebuilt %s voice profile (%d samples)", seg, len(samples))
    return rebuilt
