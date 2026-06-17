"""Telegram notifier — STDLIB ONLY (urllib), no python-telegram-bot dependency.

This is the *outbound* side of the control layer: it pushes messages from the
assistant to you. It is deliberately dependency-free so the autonomous poller
thread (which may run without the async bot ever being constructed) can always
reach you with an FYI or an error — even if python-telegram-bot is unavailable.

Every method is best-effort: it returns the Telegram ``message_id`` as a string
on success and ``""`` on any failure, and NEVER raises. A notifier that crashed
the pipeline would defeat the whole "never fail silently, but never fall over"
posture, so failures are logged and swallowed.

Design notes:
  * All sends are PLAIN text (no parse_mode). Telegram's Markdown/HTML parsers
    choke on unescaped ``_``, ``*``, ``[`` etc. that routinely appear in email
    bodies and drafts — sending plain avoids a whole class of escaping bugs.
  * Long text is truncated to ~3800 chars (Telegram's hard limit is 4096; we
    leave headroom for the inline-keyboard payload and an ellipsis).
  * Inline keyboards encode the action id in ``callback_data`` so the bot's
    CallbackQueryHandler can route a tap back to the right pending action.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

from assistant.config import Settings
from assistant.logging_setup import get_logger

log = get_logger("notifier")

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_MAX_LEN = 3800
_TIMEOUT = 15  # seconds


def _truncate(text: str, limit: int = _MAX_LEN) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ─────────────────────────────────────────────────────────────────────────────
# Notification formatting (P0c) — pure, unit-testable. The attention-economy rules:
#   line 1 = the SIGNAL (tier emoji + topic), never the sender; line 2 = who + context;
#   then a short draft preview (≤3 lines / ~60 words). Non-draft part stays < 300 chars
#   so it reads in the phone banner. Plain text only (no markdown) — email/draft bodies
#   routinely contain _ * [ ] that break Telegram's parsers; the emoji anchors carry
#   the emphasis instead.
# ─────────────────────────────────────────────────────────────────────────────
_TIER_EMOJI = {0: "⚪", 1: "🔵", 2: "🟡", 3: "🔴"}
_SIGNAL_MAX = 140
_SENDER_MAX = 120
_MAIL_MAX = 150
_QUOTE_MAX = 200
_SEP = "─" * 18


def tier_emoji(tier: int) -> str:
    return _TIER_EMOJI.get(int(tier), "ℹ️")


def draft_preview(text: str, *, max_lines: int = 3, max_words: int = 60) -> str:
    """First ≤max_lines lines and ≤max_words words of a draft, with a trailing … if
    anything was dropped. Empty in → empty out."""
    text = (text or "").strip()
    if not text:
        return ""
    lines = [ln for ln in text.splitlines()]
    truncated = len(lines) > max_lines
    preview = "\n".join(lines[:max_lines]).strip()
    words = preview.split()
    if len(words) > max_words:
        preview = " ".join(words[:max_words])
        truncated = True
    if truncated:
        preview = preview.rstrip() + " …"
    return preview


def format_card(*, tier: int, signal: str, sender: str = "", mail: str = "",
                quote: str = "", draft: str = "", context: str = "",
                unknown_contact: bool = False) -> str:
    """Build the notification body:
        {emoji} {signal}              line 1: the topic
        {sender}                      line 2: who (👤 known / 🆕 unsaved) + name
        {mail}                        line 3: source (📧 Email / 💬 WhatsApp + address/subject)
        "{quote}"                     line 4: a snippet of what they actually wrote
        {context}                     optional: recent conversation history block
        ──────
        {draft preview}
        👤 Unknown — reply "name: Name" to save   (only when unknown_contact=True)
    Empty lines are omitted."""
    lines = [f"{tier_emoji(tier)} {_truncate((signal or '').strip() or 'needs your attention', _SIGNAL_MAX)}"]
    if (sender or "").strip():
        lines.append(_truncate(sender.strip(), _SENDER_MAX))
    if (mail or "").strip():
        lines.append(_truncate(mail.strip(), _MAIL_MAX))
    if (quote or "").strip():
        lines.append('"' + _truncate(quote.strip(), _QUOTE_MAX) + '"')
    if (context or "").strip():
        lines.append(context.strip())
    preview = draft_preview(draft) or "[Draft unavailable — tap Edit to write manually]"
    card = "\n".join(lines) + f"\n{_SEP}\n{preview}"
    if unknown_contact:
        card += "\n👤 Unknown contact — reply \"name: Name\" to save"
    return card


def _card_markup(action_id: int, tier: int, source_url: str = "",
                 source_label: str = "") -> dict[str, Any]:
    approve_label = "✅ Send suggested" if int(tier) >= 3 else "✅ Approve"
    rows = [
        [
            {"text": approve_label, "callback_data": f"appr:{action_id}"},
            {"text": "✏️ Edit", "callback_data": f"edit:{action_id}"},
            {"text": "⏭ Skip", "callback_data": f"skip:{action_id}"},
        ]
    ]
    # Backtrack button: jumps straight to the original Gmail thread / WhatsApp chat.
    # Telegram only accepts http(s) URL buttons; we always build those.
    if source_url:
        rows.append([{"text": f"↗ {source_label or 'Open conversation'}", "url": source_url}])
    return {"inline_keyboard": rows}


class Notifier:
    """Best-effort, stdlib-only Telegram sender."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id

    # ── low-level transport ──────────────────────────────────────────────────
    def _post(self, method: str, payload: dict[str, Any]) -> str:
        """POST to the Telegram Bot API. Returns the resulting message_id as a
        string, or "" on any failure (logged, never raised)."""
        if not self.token or not self.chat_id:
            log.warning("notifier: missing telegram token/chat_id; cannot send %s", method)
            return ""

        url = _API_BASE.format(token=self.token, method=method)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            if not parsed.get("ok"):
                log.warning("notifier: telegram %s not ok: %s", method, parsed)
                return ""
            result = parsed.get("result") or {}
            mid = result.get("message_id")
            return str(mid) if mid is not None else ""
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            log.warning("notifier: telegram %s HTTP %s: %s", method, exc.code, detail)
            return ""
        except (urllib.error.URLError, OSError, ValueError) as exc:  # noqa: BLE001
            log.warning("notifier: telegram %s failed: %s", method, exc)
            return ""

    def _send_message(
        self, text: str, reply_markup: Optional[dict[str, Any]] = None
    ) -> str:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": _truncate(text),
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("sendMessage", payload)

    def _edit_message(
        self, message_id: str, text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> str:
        """Rewrite an already-delivered card in place (editMessageText). Used to keep the
        displayed approval card in sync with a folded/merged draft so the owner can never
        approve text they did not see (approval-telegram-1 / WYSIWYG_APPROVAL). Best-effort:
        returns the message_id on success, "" on any failure (e.g. message too old to edit)."""
        if not message_id:
            return ""
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": int(message_id) if str(message_id).isdigit() else message_id,
            "text": _truncate(text),
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("editMessageText", payload)

    # ── public API (matches the cross-module contract exactly) ───────────────
    def send_text(self, text: str) -> str:
        """Send a plain message. Returns the telegram message_id (or "")."""
        return self._send_message(text)

    def fyi(self, text: str) -> str:
        """Send a low-key 'for your information' line (tier 1)."""
        return self._send_message(f"ℹ️ {text}")

    def send_approval(
        self, action_id: int, signal: str, draft_text: str, *,
        sender: str = "", mail: str = "", quote: str = "", context: str = "",
        unknown_contact: bool = False, source_url: str = "", source_label: str = "",
    ) -> str:
        """Surface a pre-drafted reply for one-tap approval (tier 2).

        Keyboard: ✅ Approve / ✏️ Edit / ⏭ Skip (callback_data appr|edit|skip:{id}),
        plus an optional ↗ button that backtracks to the source conversation.
        The draft is already generated, so Approve sends with NO further LLM call.
        """
        body = format_card(tier=2, signal=signal, sender=sender, mail=mail, quote=quote,
                           draft=draft_text, context=context, unknown_contact=unknown_contact)
        return self._send_message(
            body, reply_markup=_card_markup(action_id, 2, source_url, source_label))

    def send_ask(
        self, action_id: int, signal: str, suggestion: str, *,
        sender: str = "", mail: str = "", quote: str = "", context: str = "",
        unknown_contact: bool = False, source_url: str = "", source_label: str = "",
    ) -> str:
        """Surface a consequential item with a pre-generated suggested reply (tier 3).

        Keyboard: ✅ Send suggested / ✏️ Edit / ⏭ Skip — same guarded send path as
        approval — plus an optional ↗ backtrack button to the source conversation.
        """
        body = format_card(tier=3, signal=signal, sender=sender, mail=mail, quote=quote,
                           draft=suggestion, context=context, unknown_contact=unknown_contact)
        return self._send_message(
            body, reply_markup=_card_markup(action_id, 3, source_url, source_label))

    def edit_approval(
        self, message_id: str, action_id: int, signal: str, draft_text: str, *,
        tier: int = 2, sender: str = "", mail: str = "", quote: str = "",
        context: str = "", source_url: str = "", source_label: str = "",
    ) -> str:
        """Re-render an already-delivered approval/ask card to a merged (folded) draft, in
        place. The owner then sees and approves EXACTLY the draft that will be sent
        (approval-telegram-1 / WYSIWYG_APPROVAL). Returns the message_id on success, "" on
        failure (caller treats a failed re-render as "card may be stale" and keeps the
        approval invalidated, so begin_send still refuses the unseen draft)."""
        body = format_card(tier=int(tier), signal=signal, sender=sender, mail=mail,
                           quote=quote, draft=draft_text, context=context)
        return self._edit_message(
            message_id, body,
            reply_markup=_card_markup(action_id, int(tier), source_url, source_label))

    def send_commitment(self, commitment_id: str, text: str) -> str:
        """Surface a due/stale commitment with Done / Snooze / Draft follow-up (P4b).

        callback_data carries the commitment's string id (cdone|csnooze|cdraft:{id})."""
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Done", "callback_data": f"cdone:{commitment_id}"},
                    {"text": "⏰ Snooze 2d", "callback_data": f"csnooze:{commitment_id}"},
                    {"text": "✍️ Draft follow-up", "callback_data": f"cdraft:{commitment_id}"},
                ]
            ]
        }
        return self._send_message(text, reply_markup=markup)

    def send_link_suggestion(self, suggestion: dict[str, Any]) -> str:
        """Ask once whether two identities are the same person (Memory Part A).

        callback_data carries the suggestion's string id (linkyes|linkno:{id}). A
        'No' is remembered so the pair is never asked again."""
        new = suggestion.get("identifier_new", "")
        cand = suggestion.get("candidate_name") or suggestion.get("candidate_person_id", "")
        body = (
            f"🔗 Same person?\n"
            f"Is {new}\nthe same person as {cand}?\n"
            f"({suggestion.get('reason', '')})"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Yes, link them", "callback_data": f"linkyes:{suggestion.get('id')}"},
                    {"text": "❌ No", "callback_data": f"linkno:{suggestion.get('id')}"},
                ]
            ]
        }
        return self._send_message(body, reply_markup=markup)

    def error(self, text: str) -> str:
        """Surface an error to you (fail loud, never silent)."""
        return self._send_message(f"⚠️ {text}")
