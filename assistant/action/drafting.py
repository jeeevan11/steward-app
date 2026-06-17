"""Draft a reply to a thread, in your voice.

The system prompt is the editable `drafting` prompt plus the assembled voice prefix
(profile summary + few-shot samples). The user turn is the rendered thread plus a
short note of what the reply should accomplish (from the Decision). On any LLM
failure we return a SAFE holding draft made of [placeholder]s — never a fabricated
reply — and log it, so the human always has something to edit rather than nothing.
"""

from __future__ import annotations

import re
import sqlite3
import time

from assistant.action import voice
from assistant.config import Settings
from assistant.llm import prompts
from assistant.llm.client import LLMClient, LLMError
from assistant.logging_setup import get_logger
from assistant.models import Channel, Contact, FinalDecision, Thread
from assistant.storage import metrics
from assistant.storage import repositories as repo

log = get_logger("drafting")


# drafting-safety-5 (ROOT CAUSE): strip_dashes only re.sub'd the literal U+2014 (—) and
# U+2013 (–). An LLM frequently substitutes other Unicode dashes — the horizontal bar
# U+2015 (―), figure dash U+2012 (‒), minus sign U+2212 (−), or the two-/three-em dashes
# U+2E3A/U+2E3B (⸺/⸻) — none of which were matched, so they survived into a card the
# owner approved, silently breaking the hard "no em-dash in generated text" invariant.
# Fix: classify the FULL Unicode dash class in one shared place and map each glyph to the
# correct ASCII replacement. EM-class dashes (em / horizontal bar / 2-em / 3-em) act as a
# clause break → ", "; the narrower dashes (figure / en / minus) are range/compound
# hyphens → "-". Both quality_gate and compose import these so the whole draft path agrees.

# EM-class: clause-break dashes → replaced with ", ".
EM_DASH_CLASS = "—―⸺⸻"  # — ―  ⸺  ⸻
# HYPHEN-class: narrow dashes that read as a hyphen/range → replaced with "-".
HYPHEN_DASH_CLASS = "‒–−"     # ‒  –  −
# Any Unicode dash we normalize (used for cheap membership tests / triggers).
ALL_DASH_CLASS = EM_DASH_CLASS + HYPHEN_DASH_CLASS

_EM_DASH_RE = re.compile(r"\s*[" + EM_DASH_CLASS + r"]\s*")
_HYPHEN_DASH_RE = re.compile(r"\s*[" + HYPHEN_DASH_CLASS + r"]\s*")


def has_unicode_dash(text: str) -> bool:
    """True iff ``text`` contains any Unicode dash we normalize (drafting-safety-5)."""
    return any(ch in (text or "") for ch in ALL_DASH_CLASS)


def strip_dashes(text: str) -> str:
    """Safety net: remove the full Unicode dash class even if the model ignores the
    instruction (drafting-safety-5 widened this beyond just U+2014/U+2013).

    EM-class dashes (em —, horizontal bar ―, 2-/3-em ⸺ ⸻) → a comma (their usual job is a
    clause break); narrow dashes (figure ‒, en –, minus −) → a hyphen (keeps ranges like
    5-10 readable). Then tidy up doubled punctuation/spaces.
    """
    text = _EM_DASH_RE.sub(", ", text)        # em-class → ", "
    text = _HYPHEN_DASH_RE.sub("-", text)      # narrow dash → "-"
    text = text.replace(",,", ",")
    # only collapse a comma+space artifact left dangling before a newline; do NOT
    # touch a legitimate "Hi John,\n" (comma immediately before the newline).
    text = re.sub(r",[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def _intent_note(final: FinalDecision) -> str:
    """A short instruction describing what this reply needs to do."""
    d = final.decision
    bits: list[str] = []
    if d.intent:
        bits.append(f"Their intent: {d.intent}.")
    if d.one_line_summary:
        bits.append(f"Summary: {d.one_line_summary}.")
    if not bits:
        bits.append("Reply appropriately to the latest message in this thread.")
    return " ".join(bits)


def _holding_draft(thread: Thread, final: FinalDecision) -> str:
    """A safe, fabrication-free placeholder draft used when the LLM is unavailable."""
    who = ""
    inbound = thread.latest_inbound
    if inbound is not None:
        who = inbound.sender_name or inbound.sender_email or ""
    greeting = f"Hi {who.split()[0]}," if who else "Hi,"
    summary = (final.decision.one_line_summary or "your message").strip()
    # drafting-safety-5 (defense-in-depth): a contact's own name in `summary` could carry a
    # Unicode dash; normalize the assembled holding draft so even this branch never emits one.
    # (In the default config the quality gate also strips it, but a future caller might not.)
    return strip_dashes(
        f"{greeting}\n\n"
        f"[Could not auto-draft a reply, please write the response.]\n"
        f"[Re: {summary}]\n"
        f"[Key points to cover: PLACEHOLDER]\n\n"
        f"Best,\n[your name]"
    )


def _is_group_thread(thread: Thread) -> bool:
    """True for a WhatsApp GROUP chat (its jid / thread id ends in '@g.us'). A group reply
    is visible to every participant, so a sender's private 1:1 relationship memory must
    never be injected into a group draft. Email and 1:1 WhatsApp are not groups."""
    return thread.channel == Channel.WHATSAPP and (thread.id or "").endswith("@g.us")


def _reply_recipient_count(thread: Thread, settings: Settings) -> int:
    """How many DISTINCT external recipients the eventual reply would go to (To + Cc, minus
    our own address) — computed the SAME way execute_send computes the send target via
    gmail_actions._reply_recipients. Used to decide whether a draft is effectively 1:1.

    Returns 1 on any failure so the caller falls back to the existing 1:1 behavior (the
    privacy guard below is additive: it only ever WITHHOLDS memory on a clear multi-recipient
    signal, never injects more than before)."""
    try:
        from assistant.action.gmail_actions import _reply_recipients
        to, cc = _reply_recipients(thread, settings)
        me = (getattr(settings, "gmail_address", "") or "").lower()
        seen = {a.strip().lower() for a in (list(to) + list(cc))
                if a and a.strip() and a.strip().lower() != me}
        return len(seen) or 1
    except Exception:  # noqa: BLE001 - never break drafting on a recipient-count error
        return 1


def _is_multi_recipient_email(thread: Thread, settings: Settings) -> bool:
    """drafting-safety-2 (privacy): True when an EMAIL reply would reach more than one
    external participant (multiple To/Cc, i.e. reply-all). The WhatsApp-only group guard
    missed this, so private 1:1 relationship memory leaked into reply-all/CC'd email drafts
    and out to unrelated third parties. Treat such a thread like a group for memory
    injection: do not inject 1:1 relationship memory."""
    if thread.channel != Channel.GMAIL:
        return False
    return _reply_recipient_count(thread, settings) > 1


# drafting-safety-4 (ROOT CAUSE): the quality gate's fabrication check was handed ONLY
# thread.render_for_prompt() as `source_text`, but the drafter grounds its reply in far
# more than the thread render — the WhatsApp 14-day recent_block, the relationship memory
# block, and the calendar drafting_note (open slots). So a perfectly grounded specific
# (e.g. "18:30" that lives in the recent_block, or a "15:00" slot the calendar note
# offered) appeared in the draft but not in `source_text`, and the gate cried "possible
# fabrication" on a correct reply — training the owner to ignore the flag, which then masks
# REAL fabrications. This helper assembles the SAME grounding the drafter saw, honoring the
# identical group / multi-recipient privacy guard, so the gate validates a specific against
# every source it could legitimately have come from. Best-effort per source: a failure in
# any one never breaks the gate (it just narrows the grounding, never widens a false flag
# into a missed one beyond the prior behavior).
def grounding_text(
    conn: sqlite3.Connection,
    settings: Settings,
    thread: Thread,
    contact: Contact,
) -> str:
    """Return the concatenated grounding the drafter had access to: the thread render plus
    (where applicable) the WhatsApp recent_block, the relationship memory block, and the
    calendar drafting_note. Used by the quality gate so its fabrication check sees the same
    sources the model did (drafting-safety-4). Never raises."""
    parts: list[str] = []
    try:
        parts.append(thread.render_for_prompt() or "")
    except Exception:  # noqa: BLE001
        pass

    # WhatsApp recent back-and-forth (the same block draft_reply injects into channel_note).
    if thread.channel == Channel.WHATSAPP:
        try:
            from assistant.ingest import wa_context
            recent = wa_context.recent_block(
                conn, thread.id,
                days=getattr(settings, "whatsapp_context_days", 14),
                me_jid=settings.wa_user_jid,
            )
            if recent:
                parts.append(recent)
        except Exception:  # noqa: BLE001
            pass

    # Relationship memory block — only when the drafter would actually have injected it
    # (memory enabled AND not a group / multi-recipient thread, mirroring draft_reply's
    # privacy guard). We must NOT widen grounding to memory the drafter never saw.
    try:
        private_ok = not _is_group_thread(thread) and not _is_multi_recipient_email(thread, settings)
        if getattr(settings, "memory_enabled", False) and private_ok:
            from assistant.memory import distill, identity, retrieval
            pid = identity.person_id_for(conn, contact.email)
            if pid:
                block = retrieval.build_memory_block(distill.load_memory(conn, pid))
                if block:
                    parts.append(block)
    except Exception:  # noqa: BLE001
        pass

    # Calendar open-slots note (the same drafting_note draft_reply appends).
    try:
        from assistant.memory import calendar_context
        cal = calendar_context.drafting_note(settings)
        if cal:
            parts.append(cal)
    except Exception:  # noqa: BLE001
        pass

    return "\n\n".join(p for p in parts if p)


def draft_reply(
    conn: sqlite3.Connection,
    llm: LLMClient,
    settings: Settings,
    thread: Thread,
    contact: Contact,
    final: FinalDecision,
) -> str:
    """Draft a reply to `thread` in your voice. Always returns a string.

    On LLMError (or a missing prompt) returns a safe holding draft of placeholders.
    """
    try:
        base_system = prompts.load_and_render(
            "drafting", settings.prompts_dir, owner_about=repo.get_owner_about(conn))
    except (FileNotFoundError, OSError) as exc:
        log.error("drafting prompt missing (%s); returning holding draft", exc)
        return _holding_draft(thread, final)

    system_prefix = base_system + "\n\n" + voice.voice_prefix(conn, contact.email, settings)

    # P4a: give the drafter real open slots so it can propose meeting times (opt-in;
    # empty when calendar is off/unavailable). Never allowed to break drafting.
    try:
        from assistant.memory import calendar_context
        cal = calendar_context.drafting_note(settings)
        if cal:
            system_prefix += "\n\n" + cal
    except Exception:  # noqa: BLE001
        pass

    # Memory-aware drafting: give the writer the relationship context so the reply has
    # continuity (don't reintroduce, pick up open threads, honor what's decided). Best-
    # effort and gated on memory_enabled; any failure leaves drafting exactly as before.
    # GROUP / MULTI-RECIPIENT GUARD (privacy): never inject a sender's private 1:1
    # relationship memory into a draft bound for a GROUP chat OR a multi-recipient / CC'd
    # (reply-all) EMAIL — those distilled facts/decisions would be visible to every
    # participant, including unrelated third parties (drafting-safety-2). The per-jid
    # recent-conversation block below is group-scoped and stays. A 1:1 email or 1:1
    # WhatsApp is unaffected.
    _private_ok = not _is_group_thread(thread) and not _is_multi_recipient_email(thread, settings)
    if getattr(settings, "memory_enabled", False) and _private_ok:
        try:
            from assistant.memory import distill, identity, retrieval
            pid = identity.person_id_for(conn, contact.email)
            if pid:
                block = retrieval.build_memory_block(distill.load_memory(conn, pid))
                if block:
                    system_prefix += (
                        "\n\n" + block
                        + "\n\nWrite with that continuity: you already know this person, so "
                        "don't reintroduce yourself or re-explain settled context, and pick up "
                        "any open thread naturally. Use only facts from the conversation and "
                        "this memory, never invent."
                    )
        except Exception:  # noqa: BLE001 - memory is additive; never break drafting
            pass

    channel_note = ""
    if thread.channel == Channel.WHATSAPP:
        channel_note = (
            "\n\nThis is a WhatsApp message — keep the reply short, casual and "
            "conversational, like a text (a sentence or two). No greeting/sign-off block."
        )
        # Layer 1D: write in his actual WhatsApp texting style (learned from his own
        # sent messages). Layer 1C: give the drafter the recent back-and-forth so the
        # reply has continuity. Both best-effort; never break drafting.
        try:
            from assistant.action import wa_style
            style = wa_style.wa_style_prefix(conn, settings)
            if style:
                system_prefix += "\n\n" + style
        except Exception:  # noqa: BLE001
            pass
        try:
            from assistant.ingest import wa_context
            recent = wa_context.recent_block(conn, thread.id,
                                             days=getattr(settings, "whatsapp_context_days", 14),
                                             me_jid=settings.wa_user_jid)
            if recent:
                channel_note += "\n\n" + recent
        except Exception:  # noqa: BLE001
            pass

    user_prompt = (
        thread.render_for_prompt()
        + "\n\n=== WHAT THIS REPLY SHOULD ACCOMPLISH ===\n"
        + _intent_note(final)
        + "\n\nWrite the reply body only (no subject line). Do not invent facts, "
        "dates, figures, or commitments not present in the thread — use a clearly "
        "marked [placeholder] for anything you cannot ground."
        + channel_note
    )

    t0 = time.monotonic()
    try:
        draft = llm.draft(system_prefix=system_prefix, user_prompt=user_prompt).strip()
    except LLMError as exc:
        log.error("draft_reply LLM failure (%s); returning holding draft", exc)
        return _holding_draft(thread, final)
    finally:
        try:
            metrics.record_response_time(
                conn, metrics.RT_DRAFT_GENERATION, int((time.monotonic() - t0) * 1000)
            )
        except Exception:  # noqa: BLE001 - latency logging never breaks drafting
            pass

    if not draft:
        log.warning("draft_reply produced empty text; returning holding draft")
        return _holding_draft(thread, final)
    return strip_dashes(draft)
