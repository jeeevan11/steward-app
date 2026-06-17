"""Compose pipeline: natural-language intent -> resolved recipients -> drafted message.

Usage pattern:
  text = "email Rajesh that the deck slips to Friday"
  intent = detect_compose_intent(text)
  if intent:
      result = compose_and_queue(intent["intent_text"], intent["channel"], db, settings, llm)
  # result goes back to telegram_bot.py which handles approval queuing.

Nothing is ever sent without the founder approving. This module resolves + drafts only.
Stdlib only.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

# ---------------------------------------------------------------------------
# Channel inference helpers
# ---------------------------------------------------------------------------

_GMAIL_KEYWORDS = ("email", "mail")
_WHATSAPP_KEYWORDS = ("text", "whatsapp", "dm", "ping")

# Compose-intent trigger prefixes / phrases (checked on lowercased input).
_PREFIX_PATTERNS = (
    "email ",
    "text ",
    "message ",
    "write to ",
    "reach out to ",
    "send a note to ",
    "dm ",
    "whatsapp ",
    "ping ",
    "follow up with ",
    "reply to ",
)

# "tell <name>" / "let <name> know" are checked with a regex because the name
# follows immediately rather than the sentence ending with a keyword.
_TELL_RE = re.compile(r"^(tell|let)\s+\S", re.IGNORECASE)


def _infer_channel(text_lower: str) -> str:
    for kw in _GMAIL_KEYWORDS:
        if kw in text_lower.split():
            return "gmail"
        if text_lower.startswith(kw + " ") or text_lower.startswith(kw + ","):
            return "gmail"
    for kw in _WHATSAPP_KEYWORDS:
        if kw in text_lower.split():
            return "whatsapp"
        if text_lower.startswith(kw + " ") or text_lower.startswith(kw + ","):
            return "whatsapp"
    # check word presence anywhere for mail/email
    for kw in _GMAIL_KEYWORDS:
        if kw in text_lower:
            return "gmail"
    for kw in _WHATSAPP_KEYWORDS:
        if kw in text_lower:
            return "whatsapp"
    return "auto"


# ---------------------------------------------------------------------------
# 1. detect_compose_intent
# ---------------------------------------------------------------------------

def detect_compose_intent(text: str) -> dict[str, Any] | None:
    """Return {intent_text, channel} if text is a compose command, else None.

    Channel inference:
      "email"/"mail" -> 'gmail'
      "text"/"whatsapp"/"dm"/"ping" -> 'whatsapp'
      everything else -> 'auto'
    """
    if not text:
        return None
    stripped = text.strip()
    lower = stripped.lower()

    matched = False
    for prefix in _PREFIX_PATTERNS:
        if lower.startswith(prefix):
            matched = True
            break

    if not matched and _TELL_RE.match(stripped):
        matched = True

    if not matched:
        return None

    channel = _infer_channel(lower)
    return {"intent_text": stripped, "channel": channel}


# ---------------------------------------------------------------------------
# 2. resolve_recipients
# ---------------------------------------------------------------------------

def resolve_recipients(intent_text: str, db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Extract Title-Cased names from intent_text and look them up in contacts.

    Returns a list of {name, email, phone, channel} dicts, deduplicated by email.
    Returns [] on any error or when the contacts table does not exist.
    """
    if not intent_text:
        return []

    # Extract candidate names: Title-Cased words longer than 2 characters.
    # We exclude the very first word if it is one of our trigger verbs so that
    # "Email" or "Text" is not treated as a person's name.
    words = re.findall(r"[A-Z][a-z]+", intent_text)
    trigger_words = {
        "Email", "Text", "Message", "Write", "Reach", "Send", "Note",
        "Dm", "Whatsapp", "Ping", "Tell", "Let", "Follow", "Reply",
    }
    candidates = [w for w in words if len(w) > 2 and w not in trigger_words]

    if not candidates:
        return []

    seen_emails: set[str] = set()
    results: list[dict[str, Any]] = []

    try:
        for name in candidates:
            try:
                rows = db.execute(
                    "SELECT name, email, phone, channel FROM contacts "
                    "WHERE name LIKE ? OR email LIKE ? LIMIT 5",
                    (f"%{name}%", f"%{name}%"),
                ).fetchall()
            except sqlite3.OperationalError:
                # contacts table may not exist in a bare test DB
                return []

            # drafting-safety-3: prefer an EXACT name-token match before falling back to the
            # substring LIKE. "email Sam ..." should resolve to the contact literally named
            # "Sam" when one exists, instead of fuzzily matching Samuel/Samantha/Samir and
            # blasting the message to all three. Only when there is NO exact match do we keep
            # the (now ambiguity-gated) substring candidates.
            cand_l = name.lower()
            exact = [
                r for r in rows
                if cand_l in {t.lower() for t in re.split(r"\s+", (r["name"] or "").strip()) if t}
                or (r["email"] or "").lower().split("@")[0] == cand_l
            ]
            chosen = exact if exact else rows

            for row in chosen:
                email = (row["email"] or "").lower()
                if not email or email in seen_emails:
                    continue
                seen_emails.add(email)
                results.append({
                    "name": row["name"] or "",
                    "email": email,
                    "phone": row["phone"] if "phone" in row.keys() else "",
                    "channel": row["channel"] if "channel" in row.keys() else "",
                })
    except Exception:  # noqa: BLE001
        return []

    return results


# ---------------------------------------------------------------------------
# 3. compose_and_queue
# ---------------------------------------------------------------------------

def compose_and_queue(
    intent_text: str,
    channel: str,
    db: sqlite3.Connection,
    settings: Any,
    llm_client: Any,
) -> dict[str, Any]:
    """Resolve recipients, draft message, and (if not dry_run) prepare for queuing.

    Returns a result dict with 'status' key:
      'not_found'           - no matching contacts
      'needs_clarification' - more than one match (ambiguous → owner must pick exactly one)
      'ready'               - draft produced; includes 'draft', 'recipients', 'channel'
      'error'               - unexpected failure; includes 'error' key
    """
    try:
        recipients = resolve_recipients(intent_text, db)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"recipient resolution failed: {exc}"}

    if len(recipients) == 0:
        return {"status": "not_found", "query": intent_text}

    # drafting-safety-3 (NO_WRONG_RECIPIENT): an auto-filled compose addressed to ONE person
    # ("email Sam ...") must resolve to a single unambiguous recipient. The draft body is
    # written for recipients[0] only ("Hi Samuel, ..."), so sending it to every fuzzy match
    # (Samuel/Samantha/Samir) is a wrong-recipient send AND a confidential-content leak to
    # outsiders. Any ambiguity surfaces for the owner to pick exactly one, rather than
    # emailing everyone. (The gate was '> 3', which silently sent to 2-3 matches.)
    if len(recipients) > 1:
        return {
            "status": "needs_clarification",
            "options": recipients[:5],
            "query": intent_text,
        }

    # Exactly one recipient — write the draft for that single person.
    primary = recipients[0]
    recipient_name = primary.get("name") or primary.get("email") or "them"

    channel_label = channel if channel != "auto" else "message"

    prompt = (
        f"Draft a short {channel_label} from Jatin Chhanwal to {recipient_name} "
        f"based on this intent: {intent_text}. "
        f"Be direct and concise. Match his voice: no fluff, no em-dashes, plain language. "
        f"Return only the message body, nothing else."
    )

    try:
        draft = llm_client.complete_text(
            system_prefix="You are Jatin Chhanwal's writing assistant. Write in his voice.",
            user_prompt=prompt,
            max_tokens=400,
            use_opus=False,
            effort="medium",
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"draft generation failed: {exc}"}

    draft = (draft or "").strip()

    # Strip em-dashes using drafting.strip_dashes if available; fall back to
    # inline replacement so this module has no hard import dependency on drafting.
    # drafting-safety-5: the fallback must cover the FULL Unicode dash class (horizontal
    # bar U+2015, minus U+2212, figure dash U+2012, 2-/3-em U+2E3A/U+2E3B), not just
    # U+2014/U+2013, so a compose draft can never ship a non-U+2014 dash glyph either.
    try:
        from assistant.action.drafting import strip_dashes
        draft = strip_dashes(draft)
    except Exception:  # noqa: BLE001
        draft = re.sub(r"\s*[—―⸺⸻]\s*", ", ", draft)   # em-class dashes
        draft = re.sub(r"\s*[‒–−]\s*", "-", draft)        # narrow dashes

    if settings.dry_run:
        return {
            "status": "ready",
            "recipients": recipients,
            "draft": draft,
            "channel": channel,
            "dry_run": True,
        }

    return {
        "status": "ready",
        "recipients": recipients,
        "draft": draft,
        "channel": channel,
        "dry_run": False,
    }
