"""Telegram inline-mode search handler.

User types "@stewardbot find rajesh term sheet" in ANY chat.
This module handles that query by searching Gmail and the WhatsApp message store.

All functions are async (python-telegram-bot v20+). Fail silently — returns
empty results on any error, never raises.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Optional

from telegram import InlineQueryResultArticle, InputTextMessageContent

from assistant.logging_setup import get_logger

log = get_logger("inline_search")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Gmail search
# ─────────────────────────────────────────────────────────────────────────────

def search_gmail(query_text: str, gmail_service: Any) -> list[dict]:
    """Search Gmail for messages matching query_text.

    Returns list of {id, subject, sender, date_str, snippet} dicts.
    Returns [] if gmail_service is None or on any error.
    """
    if gmail_service is None:
        return []
    try:
        resp = (
            gmail_service.users()
            .messages()
            .list(userId="me", q=query_text, maxResults=10)
            .execute()
        )
        messages = resp.get("messages") or []
        results: list[dict] = []
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id:
                continue
            try:
                detail = (
                    gmail_service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=msg_id,
                        format="metadata",
                        metadataHeaders=["Subject", "From", "Date"],
                    )
                    .execute()
                )
                headers = (detail.get("payload") or {}).get("headers") or []
                header_map: dict[str, str] = {}
                for h in headers:
                    name = (h.get("name") or "").lower()
                    value = h.get("value") or ""
                    header_map[name] = value
                results.append(
                    {
                        "id": msg_id,
                        "subject": header_map.get("subject", "(no subject)"),
                        "sender": header_map.get("from", ""),
                        "date_str": header_map.get("date", ""),
                        "snippet": detail.get("snippet", ""),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("inline_search.search_gmail: failed fetching message %s: %s", msg_id, exc)
                continue
        return results
    except Exception as exc:  # noqa: BLE001
        log.debug("inline_search.search_gmail: error: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 2. WhatsApp message-store search
# ─────────────────────────────────────────────────────────────────────────────

def search_wa(query_text: str, db: Optional[sqlite3.Connection]) -> list[dict]:
    """Search the wa_messages table for messages containing query_text.

    Returns list of {id, jid, sender_name, text, ts} dicts.
    Returns [] if db is None or on any error (table may not exist).
    """
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT * FROM wa_messages WHERE body LIKE ? ORDER BY ts DESC LIMIT 10",
            (f"%{query_text}%",),
        ).fetchall()
        results: list[dict] = []
        for row in rows:
            try:
                r = dict(row)
                results.append(
                    {
                        "id": r.get("message_id", ""),
                        "jid": r.get("jid", ""),
                        "sender_name": r.get("push_name", "") or r.get("jid", ""),
                        "text": r.get("body", ""),
                        "ts": r.get("ts", 0),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("inline_search.search_wa: failed converting row: %s", exc)
                continue
        return results
    except Exception as exc:  # noqa: BLE001
        log.debug("inline_search.search_wa: error: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 3. Build Telegram InlineQueryResultArticle objects
# ─────────────────────────────────────────────────────────────────────────────

def build_inline_results(
    gmail_results: list[dict], wa_results: list[dict]
) -> list[InlineQueryResultArticle]:
    """Create InlineQueryResultArticle objects from Gmail and WA search results.

    Gmail results come first, then WA results. Deduplication is by result id.
    """
    seen_ids: set[str] = set()
    results: list[InlineQueryResultArticle] = []

    for result in gmail_results:
        article_id = f"gmail_{result['id']}"
        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)
        results.append(
            InlineQueryResultArticle(
                id=article_id,
                title=f"\U0001f4e7 {result['subject'][:60]}",
                description=f"From: {result['sender'][:80]}",
                input_message_content=InputTextMessageContent(
                    f"\U0001f4e7 {result['subject']}\nFrom: {result['sender']}\n\n{result['snippet'][:300]}"
                ),
            )
        )

    for result in wa_results:
        article_id = f"wa_{result['id']}"
        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)
        display_name = result.get("sender_name") or result.get("jid", "?")
        results.append(
            InlineQueryResultArticle(
                id=article_id,
                title=f"\U0001f4ac {display_name[:60]}",
                description=result["text"][:100],
                input_message_content=InputTextMessageContent(
                    f"\U0001f4ac {display_name}: {result['text'][:400]}"
                ),
            )
        )

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 4. Authorization gate — owner-only
# ─────────────────────────────────────────────────────────────────────────────

def _configured_owner_id(context: Any, owner_id: Optional[Any]) -> Optional[str]:
    """Resolve the configured owner id as a string.

    Prefers an explicitly-passed owner_id (used by tests and any future caller);
    otherwise reads settings.telegram_chat_id off the bot's shared state. For a
    private one-owner bot the owner's Telegram user id equals their chat id.
    Returns None if no owner is configured (in which case we fail CLOSED).
    """
    if owner_id is not None and str(owner_id).strip():
        return str(owner_id).strip()
    try:
        settings = context.application.bot_data.get("settings")
        chat_id = getattr(settings, "telegram_chat_id", "") if settings else ""
        chat_id = str(chat_id).strip()
        return chat_id or None
    except Exception:  # noqa: BLE001
        return None


def _is_owner(update: Any, context: Any, owner_id: Optional[Any]) -> bool:
    """True only when the inline query was issued by the configured owner.

    Root cause (approval-telegram-3): inline search had NO authorization gate, so any
    Telegram user who could reach the bot could type "@bot find <anything>" and read the
    owner's private Gmail/WhatsApp content. Inline queries are not tied to a chat, so the
    chat-based _authorized() check the other handlers use does not apply here — the
    requester identity lives on update.inline_query.from_user.id. We compare THAT against
    the configured owner id and fail closed if either side is missing.
    """
    configured = _configured_owner_id(context, owner_id)
    if not configured:
        # No owner configured → cannot prove the requester is the owner → deny.
        return False
    try:
        requester = update.inline_query.from_user.id
    except Exception:  # noqa: BLE001
        requester = None
    if requester is None:
        return False
    return str(requester).strip() == configured


# ─────────────────────────────────────────────────────────────────────────────
# 5. Telegram InlineQueryHandler entry point
# ─────────────────────────────────────────────────────────────────────────────

async def handle_inline_query(
    update: Any,
    context: Any,
    gmail_service: Any,
    db: Optional[sqlite3.Connection],
    owner_id: Optional[Any] = None,
) -> None:
    """Handle a Telegram inline query.

    Registered as the InlineQueryHandler callback. AUTHORIZATION FIRST: only the
    configured owner may search; everyone else gets an empty result and nothing is read
    (approval-telegram-3). Then searches Gmail and the WA message store concurrently and
    answers with up to 10 results. ``owner_id`` may be passed explicitly (tests / future
    callers); otherwise it is resolved from settings on the bot's shared state.
    """
    try:
        # Gate BEFORE touching any private store. A non-owner never reaches search.
        if not _is_owner(update, context, owner_id):
            try:
                requester = getattr(getattr(update.inline_query, "from_user", None), "id", None)
            except Exception:  # noqa: BLE001
                requester = None
            log.warning(
                "inline_search: rejected non-owner inline query from telegram user %r", requester
            )
            await update.inline_query.answer([], cache_time=1)
            return

        query = (update.inline_query.query or "").strip()
        if not query or len(query) < 2:
            await update.inline_query.answer([], cache_time=1)
            return

        loop = asyncio.get_event_loop()
        gmail_res, wa_res = await asyncio.gather(
            loop.run_in_executor(None, search_gmail, query, gmail_service),
            loop.run_in_executor(None, search_wa, query, db),
        )

        results = build_inline_results(gmail_res, wa_res)
        await update.inline_query.answer(results[:10], cache_time=10)
    except Exception as exc:  # noqa: BLE001
        log.debug("handle_inline_query: error: %s", exc)
        try:
            await update.inline_query.answer([], cache_time=1)
        except Exception:  # noqa: BLE001
            pass
