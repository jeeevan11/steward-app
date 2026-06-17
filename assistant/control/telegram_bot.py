"""The interactive Telegram bot (python-telegram-bot v21, async).

This is the *inbound* control surface: it receives your taps and messages and
turns them into actions (approve a send, skip, edit a draft, run a command).

═══════════════════════════════════════════════════════════════════════════════
THREADING / SQLITE OWNERSHIP — READ THIS BEFORE TOUCHING `conn`
═══════════════════════════════════════════════════════════════════════════════
This bot owns the asyncio event loop and runs in the **MAIN thread** (via
``application.run_polling()``). The Gmail poller runs in a **separate worker
thread** with its OWN sqlite connection — sqlite connections are NOT safe to
share across threads.

Therefore every handler in this module uses the ``conn`` that was created on the
bot's (main) thread and passed into :func:`build_application` by ``main.py``.
Never reach for the poller's connection here, and never hand this ``conn`` to the
poller thread.

Handlers are ``async``. The DB/network work (``repo.*``, Gmail sends) is blocking
and would stall the event loop, so it is run via
``loop.run_in_executor(None, blocking_fn)``. Because that executor uses a thread
pool, the blocking functions still touch the SAME ``conn`` object from a
*different* thread than the loop. SQLite permits this only when serialized, so all
executor-bound DB work for this connection is funneled through a single-worker
executor (``_DB_EXECUTOR``) created per application — that guarantees no two
handlers hit ``conn`` concurrently. (The connection is also opened with
``check_same_thread=False`` semantics by the spine's WAL/busy-timeout config.)
═══════════════════════════════════════════════════════════════════════════════

python-telegram-bot is imported at module top; that is fine because this module
is only imported at runtime (main.py), never by the unit tests.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from assistant.config import Settings
from assistant.control import briefs, commands
from assistant.llm.client import LLMClient
from assistant.logging_setup import get_logger
from assistant.storage import metrics
from assistant.storage import repositories as repo

# One-directional imports (control → action/learning); no cycle.
from assistant.action import gmail_actions
from assistant.learning import recorder, updater

log = get_logger("telegram_bot")

try:
    from assistant.control import inline_search as _inline_search
except ImportError:
    _inline_search = None

try:
    from assistant.control import state_engine as _state_engine
except ImportError:
    _state_engine = None

try:
    from assistant.action import compose as _compose
except ImportError:
    _compose = None

try:
    from assistant.memory import opportunities as _opportunities
except ImportError:
    _opportunities = None

# Keys used to stash shared services on Application.bot_data.
_K_CONN = "conn"
_K_SETTINGS = "settings"
_K_LLM = "llm"
_K_MAIL = "mail"
_K_NOTIFIER = "notifier"
_K_DB_EXECUTOR = "db_executor"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _authorized(update: Update, settings: Settings) -> bool:
    """Only ever respond to YOUR chat. Everyone else is silently ignored."""
    chat = update.effective_chat
    if chat is None:
        return False
    return str(chat.id) == str(settings.telegram_chat_id)


async def _run_db(
    context: ContextTypes.DEFAULT_TYPE, fn: Callable[[], Any]
) -> Any:
    """Run a blocking DB/network callable off the event loop, serialized through
    this application's single-worker DB executor so the shared ``conn`` is never
    touched by two threads at once."""
    loop = asyncio.get_running_loop()
    executor: ThreadPoolExecutor = context.application.bot_data[_K_DB_EXECUTOR]
    return await loop.run_in_executor(executor, fn)


def _approve_markup(action_id: int, settings: Any = None, thread_id: str = "") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"appr:{action_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{action_id}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"skip:{action_id}"),
        ]
    ]
    try:
        miniapp_url = getattr(settings, 'miniapp_url', '') if settings else ''
        if miniapp_url and thread_id:
            open_btn = InlineKeyboardButton("Open full", url=f"{miniapp_url}?thread={thread_id}")
            rows.append([open_btn])
    except Exception:  # noqa: BLE001
        pass
    return InlineKeyboardMarkup(rows)


def _retry_markup(action_id: int) -> InlineKeyboardMarkup:
    """Shown after a failed send — tapping it re-runs the approve path (idempotent;
    the begin_send compare-and-set still prevents a double-send)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Retry", callback_data=f"appr:{action_id}")]]
    )


def _send_failure_kind(conn: sqlite3.Connection, action_id: int) -> str:
    """After execute_send returns False, distinguish a provably-not-sent failure (safe to
    offer a one-tap retry) from a maybe-delivered ambiguous send (must NOT be auto-resent
    — EXACTLY_ONCE_SEND). Returns 'ambiguous' or 'failed'."""
    row = repo.get_pending(conn, action_id)
    if row is not None and row["status"] == "SEND_AMBIGUOUS":
        return "ambiguous"
    return "failed"


def _parse_callback(data: str) -> tuple[str, int | None]:
    """Split callback_data 'verb:id' into (verb, id). id is None if unparseable."""
    verb, _, rest = (data or "").partition(":")
    try:
        return verb, int(rest)
    except (TypeError, ValueError):
        return verb, None


# ─────────────────────────────────────────────────────────────────────────────
# approval-telegram-5 ROOT CAUSE FIX — namespaced, card-bound pending-edit state
# ─────────────────────────────────────────────────────────────────────────────
# The old design stored ONE un-namespaced slot: context.user_data["awaiting_edit"] =
# action_id. Tapping Edit on a second card OVERWROTE that slot (last-writer-wins),
# silently abandoning the first edit. Worse, the owner's next plain-text message was
# then applied to whatever action_id last occupied the slot — so a private reply meant
# for card #5 (spouse) could land on, and be re-surfaced for, card #9 (a client), one
# Approve tap away from sending the wrong content to the wrong recipient.
#
# Fix: keep a PER-ACTION map of pending edits, each bound to the specific card's
# Telegram message id, and route an incoming text edit only to the card the owner is
# clearly editing — preferring an exact reply-to-card binding, accepting a lone pending
# edit, and REFUSING to guess when several edits are pending and the text isn't a reply.
# Stale entries (>15 min) expire so an abandoned Edit never silently captures later text.
#
# The state lives under this key as: { action_id(int): {"card_msg_id": int|None,
# "ts": float, "label": str} }. The functions below are PURE (dict in, decision out) so
# they are unit-testable without importing telegram.

_K_AWAITING = "awaiting_edit"          # legacy single-int slot (read for back-compat)
_K_AWAITING_MAP = "awaiting_edit_map"  # new per-action map
_AWAITING_TTL_SECONDS = 15 * 60        # abandon an un-acted Edit after 15 minutes


def _awaiting_map(user_data: dict) -> dict:
    """Return (creating if needed) the per-action pending-edit map, migrating any legacy
    single-int slot into it once. Never raises."""
    m = user_data.get(_K_AWAITING_MAP)
    if not isinstance(m, dict):
        m = {}
        user_data[_K_AWAITING_MAP] = m
    # One-time migration of a legacy un-namespaced slot written by an older build.
    legacy = user_data.pop(_K_AWAITING, None)
    if legacy is not None:
        try:
            aid = int(legacy)
            m.setdefault(aid, {"card_msg_id": None, "ts": time.time(), "label": ""})
        except (TypeError, ValueError):
            pass
    return m


def _prune_awaiting(m: dict, *, now: float | None = None, ttl: int = _AWAITING_TTL_SECONDS) -> list[int]:
    """Drop entries older than ttl. Returns the action_ids that were expired."""
    now = time.time() if now is None else now
    expired = [aid for aid, e in list(m.items())
               if (now - float((e or {}).get("ts", 0))) > ttl]
    for aid in expired:
        m.pop(aid, None)
    return expired


def _record_awaiting(
    m: dict, action_id: int, *, card_msg_id: int | None, label: str = "", now: float | None = None
) -> int | None:
    """Register a pending edit for action_id bound to its card message id. Returns the
    action_id of a DIFFERENT edit that was already pending (so the caller can warn the
    owner it was abandoned/coexists), or None."""
    now = time.time() if now is None else now
    _prune_awaiting(m, now=now)
    other = next((aid for aid in m if aid != action_id), None)
    m[action_id] = {"card_msg_id": card_msg_id, "ts": now, "label": label or ""}
    return other


def _resolve_awaiting(
    m: dict, *, reply_to_msg_id: int | None, now: float | None = None
) -> tuple[str, int | None]:
    """Decide which pending edit an incoming text belongs to.

    Returns (outcome, action_id) where outcome is one of:
      * "none"      — no pending edits; treat the text as a normal message.
      * "match"     — action_id is the unambiguous target (consume it).
      * "ambiguous" — several edits pending and the text wasn't a reply to any card;
                      do NOT guess — ask the owner to reply to the specific card.

    Priority: an exact reply-to-card binding wins even when several edits are pending
    (this is what makes interleaved multi-card editing safe). Absent a reply binding, a
    LONE pending edit is accepted (the overwhelmingly common single-card flow); two or
    more pending edits with no reply binding are ambiguous."""
    now = time.time() if now is None else now
    _prune_awaiting(m, now=now)
    if not m:
        return ("none", None)
    if reply_to_msg_id is not None:
        for aid, e in m.items():
            if (e or {}).get("card_msg_id") == reply_to_msg_id:
                return ("match", aid)
        # The owner replied, but not to a card we're editing. If exactly one edit is
        # pending, fall through to accept it (a reply to some other message shouldn't
        # block the only edit in flight); otherwise it's ambiguous.
    if len(m) == 1:
        return ("match", next(iter(m)))
    return ("ambiguous", None)


def _consume_awaiting(m: dict, action_id: int) -> None:
    m.pop(action_id, None)


async def _safe_edit(query, text: str, reply_markup=None) -> None:
    """Edit a callback message, swallowing transient Telegram API errors (expired
    query, 'message is not modified', etc.) so they can't bubble out of a handler."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:  # noqa: BLE001
        log.debug("edit_message_text failed (non-fatal)", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Slash command handlers
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    await update.effective_message.reply_text(
        "Your chief-of-staff is online. I'll surface what needs you and handle "
        "the noise. Commands: /status /pause /resume /brief /undo /declineall — "
        "or just talk to me."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    conn: sqlite3.Connection = context.application.bot_data[_K_CONN]

    def work() -> str:
        paused = repo.is_paused(conn)
        pending = repo.open_pending(conn)
        mode = "DRY-RUN" if settings.dry_run else "LIVE"
        state = "paused" if paused else "running"
        return f"Status: {state} · mode {mode} · {len(pending)} item(s) awaiting you."

    text = await _run_db(context, work)
    await update.effective_message.reply_text(text)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    conn: sqlite3.Connection = context.application.bot_data[_K_CONN]

    def work() -> None:
        repo.set_paused(conn, True)
        try:
            recorder.record_pause(conn, paused=True)
        except Exception:  # noqa: BLE001
            pass

    await _run_db(context, work)
    await update.effective_message.reply_text("Paused. I won't act until you resume.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    conn: sqlite3.Connection = context.application.bot_data[_K_CONN]

    def work() -> None:
        repo.set_paused(conn, False)
        try:
            recorder.record_pause(conn, paused=False)
        except Exception:  # noqa: BLE001
            pass

    await _run_db(context, work)
    await update.effective_message.reply_text("Resumed. Back to watching your inbox.")


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    conn: sqlite3.Connection = context.application.bot_data[_K_CONN]
    llm: LLMClient = context.application.bot_data[_K_LLM]

    # /brief [morning|evening] — default morning.
    kind = "morning"
    if context.args:
        candidate = context.args[0].strip().lower()
        if candidate in ("morning", "evening"):
            kind = candidate

    text = await _run_db(
        context, partial(briefs.generate_brief, conn, settings, llm, kind)
    )
    await update.effective_message.reply_text(text)


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    conn: sqlite3.Connection = context.application.bot_data[_K_CONN]
    mail = context.application.bot_data[_K_MAIL]

    text = await _run_db(
        context, partial(gmail_actions.undo_last, conn, mail, settings)
    )
    await update.effective_message.reply_text(text)


async def cmd_declineall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    conn: sqlite3.Connection = context.application.bot_data[_K_CONN]

    def work() -> int:
        open_items = repo.open_pending(conn)
        n = 0
        for row in open_items:
            if repo.mark_skipped(conn, row["id"]):
                recorder.record_skip(conn, row)
                n += 1
        return n

    n = await _run_db(context, work)
    await update.effective_message.reply_text(f"Declined {n} pending item(s).")


_WA_STATUS_PATH = "relay/status.json"


async def cmd_wastatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    if not settings.whatsapp_enabled:
        await update.effective_message.reply_text("WhatsApp is off (set WHATSAPP_ENABLED=true).")
        return
    try:
        data = json.loads(Path(_WA_STATUS_PATH).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - file may not exist yet / relay not running
        await update.effective_message.reply_text(
            "WhatsApp relay status unknown — the relay may not be running. "
            "Start it: `node relay/whatsapp_relay.js` (see docs/WHATSAPP.md)."
        )
        return

    connected = data.get("connected")
    state = "🟢 connected" if connected else "🔴 disconnected"
    age = data.get("session_age_seconds")
    age_str = f"{int(age) // 3600}h" if isinstance(age, (int, float)) else "?"
    last = data.get("last_message_ts")
    last_str = f"{int(time.time() - last) // 60}m ago" if isinstance(last, (int, float)) and last else "—"
    await update.effective_message.reply_text(
        f"WhatsApp relay: {state}\n"
        f"session age: {age_str}\n"
        f"messages today: {data.get('messages_today', 0)}\n"
        f"last message: {last_str}"
    )


async def _state_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    try:
        db = context.application.bot_data.get(_K_CONN)
        if _state_engine is None or db is None:
            await update.message.reply_text("State engine not available.")
            return
        snapshot = _state_engine.get_state_snapshot(db)
        text = _state_engine.format_state_chat(snapshot)
        miniapp_url = getattr(settings, 'miniapp_url', '') if settings else ''
        if miniapp_url:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open War Room", url=miniapp_url)]])
            await update.message.reply_text(text, reply_markup=kb)
        else:
            await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"Could not load state: {e}")


async def _inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _inline_search is None:
        return
    try:
        db = context.application.bot_data.get(_K_CONN)
        gmail_service = context.application.bot_data.get(_K_MAIL)
        await _inline_search.handle_inline_query(update, context, gmail_service, db)
    except Exception:
        try:
            await update.inline_query.answer([], cache_time=1)
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Inline-button callbacks
# ─────────────────────────────────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()  # stop the spinner on the tapped button
    except Exception:  # noqa: BLE001 - a transient ack error must not bubble out
        log.debug("query.answer() failed (non-fatal)", exc_info=True)

    conn: sqlite3.Connection = context.application.bot_data[_K_CONN]
    mail = context.application.bot_data[_K_MAIL]
    notifier = context.application.bot_data[_K_NOTIFIER]
    llm: LLMClient = context.application.bot_data[_K_LLM]

    # Commitment + link-suggestion buttons carry a STRING id (uuid hex), handled
    # before the int path.
    raw = query.data or ""
    cverb, _, cid = raw.partition(":")
    if cverb in ("cdone", "csnooze", "cdraft"):
        await _handle_commitment(update, context, conn, llm, cverb, cid)
        return
    if cverb in ("linkyes", "linkno"):
        await _handle_link_suggestion(update, context, conn, cverb, cid)
        return

    verb, action_id = _parse_callback(raw)
    if action_id is None:
        await _safe_edit(query, "Sorry — I couldn't read that button.")
        return

    if verb == "appr":
        await _handle_approve(update, context, conn, mail, settings, notifier, llm, action_id)
    elif verb == "skip":
        await _handle_skip(update, context, conn, action_id)
    elif verb == "edit":
        await _handle_edit(update, context, conn, action_id)
    elif verb == "draftit":
        await _handle_draftit(update, context, conn, action_id)
    elif verb == "ack":
        await _handle_ack(update, context, conn, action_id)
    else:
        await _safe_edit(query, "Unknown action.")


async def _handle_approve(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    mail: Any,
    settings: Settings,
    notifier: Any,
    llm: LLMClient,
    action_id: int,
) -> None:
    query = update.callback_query

    # Optimistic UI: acknowledge the tap instantly (the actual Gmail send happens
    # right after, off the event loop). The user sees a response in well under the
    # 500ms target; the message is corrected to its final state below.
    t0 = time.monotonic()
    await _safe_edit(query, f"⏳ Sending #{action_id}…")
    confirm_ms = int((time.monotonic() - t0) * 1000)

    def work() -> str:
        # tap→confirmation latency (P0e), recorded on the serialized DB executor.
        try:
            metrics.record_response_time(conn, metrics.RT_TAP_TO_CONFIRMATION, confirm_ms)
        except Exception:  # noqa: BLE001
            pass
        # Guarded claim: only a non-terminal, pre-send row becomes APPROVED. A stale
        # or re-delivered Approve tap on an already SENT/SENDING/SKIPPED row fails
        # here and never reaches execute_send — this, plus begin_send, makes a
        # double-send impossible.
        if not repo.mark_approved(conn, action_id, via="telegram"):
            return "already"
        row = repo.get_pending(conn, action_id)
        # Fix 4: a compose card is a fresh outbound (no inbound thread) — route it to
        # execute_compose_send (same begin_send double-send guard, still human-approved).
        if row is not None and (row["kind"] or "") == "compose":
            ok = gmail_actions.execute_compose_send(conn, mail, settings, action_id, notifier=notifier)
            if ok:
                try:
                    recorder.record_approve(conn, repo.get_pending(conn, action_id))
                except Exception:  # noqa: BLE001 - learning capture never blocks the send
                    log.debug("compose record_approve failed (non-fatal)", exc_info=True)
                return "sent"
            return _send_failure_kind(conn, action_id)
        ok = gmail_actions.execute_send(conn, mail, settings, action_id, notifier=notifier)
        if ok:
            recorder.record_approve(conn, repo.get_pending(conn, action_id))
            # P4b: capture any promises in the reply we just sent (no-op in dry-run).
            try:
                from assistant.memory import commitments
                commitments.capture_from_send(conn, llm, settings, row)
            except Exception:  # noqa: BLE001 - best-effort, never affects the send
                log.debug("commitment capture failed (non-fatal)", exc_info=True)
            # Memory Part B: refresh the relationship from the just-sent exchange.
            try:
                from assistant.memory import distill as distill_mod
                distill_mod.distill_after_send(
                    conn, llm, settings, mail, row["message_id"] if row else "")
            except Exception:  # noqa: BLE001 - best-effort, never affects the send
                log.debug("post-send distill failed (non-fatal)", exc_info=True)
            return "sent"
        return _send_failure_kind(conn, action_id)

    try:
        result = await _run_db(context, work)
    except Exception as exc:  # noqa: BLE001 - a handler must never bubble out
        log.exception("approve failed for #%s: %s", action_id, exc)
        result = "failed"

    if result == "sent":
        suffix = " (dry-run — not actually sent)" if settings.dry_run else ""
        await _safe_edit(query, f"✅ Sent — #{action_id}{suffix}")
    elif result == "already":
        await _safe_edit(query, f"#{action_id} was already handled — nothing sent.")
    elif result == "ambiguous":
        # EXACTLY_ONCE_SEND: a maybe-delivered reply is NEVER offered a one-tap resend —
        # that would risk a duplicate. The owner must verify the thread themselves.
        await _safe_edit(
            query,
            f"⚠️ #{action_id}: I couldn't confirm whether this was delivered, so I did "
            f"NOT resend it (to avoid a duplicate). Please check the conversation and "
            f"reply manually if it didn't arrive.",
        )
    else:
        await _safe_edit(
            query,
            f"⚠️ Couldn't send #{action_id} — I did not send anything. Tap to retry.",
            reply_markup=_retry_markup(action_id),
        )


async def _handle_skip(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    action_id: int,
) -> None:
    query = update.callback_query

    def work() -> str | None:
        row = repo.get_pending(conn, action_id)
        if not repo.mark_skipped(conn, action_id):
            return None  # already handled — nothing to learn from
        recorder.record_skip(conn, row)
        # Repeated skips for a sender/category may warrant a (proposed, not active)
        # rule — surfaced for you to confirm; never auto-applied.
        return updater.maybe_propose_rule(conn, row, "skip")

    proposal = await _run_db(context, work)
    await _safe_edit(query, f"⏭ Skipped #{action_id}.")
    if proposal and query.message is not None:
        try:
            await query.message.reply_text(
                "💡 I've noticed a pattern — proposed rule (not active until you say so):\n"
                f"{proposal}\n\nWant this? Tell me e.g. \"yes, never bother me about that\"."
            )
        except Exception:  # noqa: BLE001
            log.debug("could not deliver rule proposal", exc_info=True)


async def _handle_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    action_id: int,
) -> None:
    query = update.callback_query
    # approval-telegram-5: bind this edit to the SPECIFIC card's Telegram message id so
    # the owner's next text is routed to the right action — not a single shared slot.
    card_msg_id = None
    try:
        if query is not None and query.message is not None:
            card_msg_id = query.message.message_id
    except Exception:  # noqa: BLE001
        card_msg_id = None
    m = _awaiting_map(context.user_data)
    label = await _edit_card_label(context, action_id)
    other = _record_awaiting(m, action_id, card_msg_id=card_msg_id, label=label)
    note = ""
    if other is not None:
        # A second pending Edit now coexists — warn so the earlier one isn't silently lost.
        note = (
            f"\n(Note: you also have an unsent edit for #{other}. Reply to THIS card to "
            f"edit #{action_id}.)"
        )
    await _safe_edit(
        query,
        f"✏️ Editing #{action_id}{(' (' + label + ')') if label else ''}. Reply to THIS "
        f"card with the replacement reply text and I'll re-surface it for approval." + note,
    )


async def _edit_card_label(context: ContextTypes.DEFAULT_TYPE, action_id: int) -> str:
    """A short human label (sender/summary) for an action, used so edit prompts and the
    disambiguation message name the card. Best-effort; never raises."""
    try:
        conn: sqlite3.Connection = context.application.bot_data[_K_CONN]

        def fetch() -> str:
            row = repo.get_pending(conn, action_id)
            if row is None:
                return ""
            keys = row.keys()
            for k in ("sender", "summary", "subject"):
                if k in keys and row[k]:
                    return str(row[k])[:48]
            return ""

        return await _run_db(context, fetch)
    except Exception:  # noqa: BLE001
        return ""


async def _handle_draftit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    action_id: int,
) -> None:
    """ASK item: you asked me to turn the suggestion into a draft you can approve.

    The stored ask already carries a suggestion in draft_text; promote it into an
    Approve/Edit/Skip flow. If there's no suggestion yet, fall back to the edit
    flow so you can dictate the reply.
    """
    query = update.callback_query

    def fetch() -> sqlite3.Row | None:
        return repo.get_pending(conn, action_id)

    row = await _run_db(context, fetch)
    draft = (row["draft_text"] if row else "") or ""
    summary = (row["summary"] if row else "") or "reply"

    if not draft.strip():
        # approval-telegram-5: namespace this dictate-the-reply edit per action and bind
        # it to the card's message id, same as _handle_edit, so it can't be misrouted.
        card_msg_id = None
        try:
            if query is not None and query.message is not None:
                card_msg_id = query.message.message_id
        except Exception:  # noqa: BLE001
            card_msg_id = None
        m = _awaiting_map(context.user_data)
        label = await _edit_card_label(context, action_id)
        other = _record_awaiting(m, action_id, card_msg_id=card_msg_id, label=label)
        note = (f"\n(Note: you also have an unsent edit for #{other}. Reply to THIS card "
                f"to draft #{action_id}.)") if other is not None else ""
        await _safe_edit(
            query,
            f"✍️ Reply to THIS card with the reply text for #{action_id}"
            f"{(' (' + label + ')') if label else ''} and I'll queue it for approval." + note,
        )
        return

    await _safe_edit(
        query,
        f"📝 Draft reply — {summary}\n{'─' * 24}\n{draft}",
        reply_markup=_approve_markup(action_id),
    )


async def _handle_ack(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    action_id: int,
) -> None:
    query = update.callback_query

    def work() -> None:
        repo.mark_skipped(conn, action_id)  # guarded: won't touch an already-sent row

    await _run_db(context, work)
    await _safe_edit(query, "👍 Noted.")


async def _handle_link_suggestion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    verb: str,
    suggestion_id: str,
) -> None:
    """Cross-channel identity confirmation: ✅ Yes links the two; ❌ No is remembered
    so the pair is never asked again (Memory Part A)."""
    query = update.callback_query
    from assistant.memory import identity

    if verb == "linkyes":
        ok = await _run_db(context, lambda: identity.confirm_suggestion(conn, suggestion_id))
        await _safe_edit(query, "🔗 Linked — I'll treat them as one person." if ok
                         else "That suggestion was already handled.")
    else:
        ok = await _run_db(context, lambda: identity.reject_suggestion(conn, suggestion_id))
        await _safe_edit(query, "Got it — kept separate. I won't ask again." if ok
                         else "That suggestion was already handled.")


async def _handle_commitment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    llm: LLMClient,
    verb: str,
    commitment_id: str,
) -> None:
    """Daily-check buttons: ✅ Done / ⏰ Snooze 2d / ✍️ Draft follow-up (P4b)."""
    query = update.callback_query
    from assistant.memory import commitments

    if verb == "cdone":
        await _run_db(context, lambda: commitments.mark_done(conn, commitment_id))
        await _safe_edit(query, "✅ Marked done.")
        return

    if verb == "csnooze":
        await _run_db(context, lambda: commitments.snooze(conn, commitment_id, days=2))
        await _safe_edit(query, "⏰ Snoozed 2 days.")
        return

    # cdraft: generate a short follow-up and surface it as a normal approval card.
    def build() -> int | None:
        c = commitments.get_commitment(conn, commitment_id)
        if c is None:
            return None
        try:
            draft = llm.draft(
                system_prefix=(
                    "Write a brief, friendly follow-up message. Direct, no fluff, no "
                    "em-dashes. Do not invent facts; use [placeholder] for anything unknown."
                ),
                user_prompt=f"Follow up on this commitment I made: {c['commitment_text']}",
            ).strip()
        except Exception:  # noqa: BLE001 - fall back to a stub the user can edit
            draft = f"[Follow-up about: {c['commitment_text']}]"
        aid = repo.create_pending(
            conn, idempotency_key=f"commit:{commitment_id}",
            message_id=c["message_id"] or "", thread_id="", tier=2, kind="reply_draft",
            summary=f"Follow-up: {c['commitment_text']}", draft_text=draft,
        )
        return aid

    aid = await _run_db(context, build)
    if aid is None:
        await _safe_edit(query, "That follow-up is already queued (or the commitment is gone).")
        return
    row = await _run_db(context, lambda: repo.get_pending(conn, aid))
    draft = (row["draft_text"] if row else "") or ""
    await _safe_edit(
        query, f"📝 Follow-up draft\n{'─' * 24}\n{draft}", reply_markup=_approve_markup(aid)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Compose → approval card (Fix 4). Turns a compose 'ready' result into a normal
# PENDING pending_actions row (kind='compose'). NOTHING is sent here — the card needs
# a human Approve tap, exactly like every reply card. The channel is encoded in the
# message_id prefix (wa_compose_* -> WhatsApp; compose_* -> Gmail) so a MailRouter
# routes the eventual send correctly; the send target is stored in compose_meta.
# ─────────────────────────────────────────────────────────────────────────────
def _queue_compose_card(conn: sqlite3.Connection, settings: Settings, result: dict):
    """Create the PENDING compose card and return its action id (or None). DB-only;
    performs no send."""
    import json as _json
    import uuid as _uuid

    recipients = result.get("recipients", []) or []
    draft = result.get("draft", "") or ""
    channel = (result.get("channel") or "auto").lower()
    primary = recipients[0] if recipients else {}
    addr = (primary.get("email") or "").strip()
    name = primary.get("name") or addr or "them"
    is_wa_addr = addr.endswith("@s.whatsapp.net") or addr.endswith("@g.us")

    if channel == "whatsapp" or (channel == "auto" and is_wa_addr):
        send_channel = "whatsapp"
        jid = addr if is_wa_addr else (primary.get("phone") or "")
        meta = {"channel": "whatsapp", "jid": jid, "name": name}
        mid = f"wa_compose_{_uuid.uuid4().hex}"   # wa_ prefix -> routes to WhatsApp source
        thread_id = jid
    else:
        send_channel = "gmail"
        emails = [
            (r.get("email") or "") for r in recipients
            if "@" in (r.get("email") or "") and not (r.get("email") or "").endswith("@s.whatsapp.net")
        ]
        meta = {"channel": "gmail", "to": emails or ([addr] if addr else []),
                "subject": "(no subject)", "name": name}
        mid = f"compose_{_uuid.uuid4().hex}"      # no wa_ prefix -> routes to Gmail source
        thread_id = (meta["to"][0] if meta["to"] else "")

    aid = repo.create_pending(
        conn,
        idempotency_key=mid,
        message_id=mid,
        thread_id=thread_id,
        tier=2,
        kind="compose",
        summary=f"Compose to {name} ({send_channel})",
        draft_text=draft,
        telegram_chat_id=settings.telegram_chat_id,
    )
    if aid is None:
        return None
    try:
        conn.execute("UPDATE pending_actions SET compose_meta=? WHERE id=?",
                     (_json.dumps(meta), aid))
    except Exception:  # noqa: BLE001 - meta is best-effort; the card still exists
        log.debug("compose_meta store failed (non-fatal)", exc_info=True)
    return aid


# ─────────────────────────────────────────────────────────────────────────────
# Plain-text handler
# ─────────────────────────────────────────────────────────────────────────────
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data[_K_SETTINGS]
    if not _authorized(update, settings):
        return
    message = update.effective_message
    if message is None or not message.text:
        return
    text = message.text.strip()

    conn: sqlite3.Connection = context.application.bot_data[_K_CONN]

    # 1) If we're awaiting an edited draft, this text IS the new draft.
    #    approval-telegram-5: route it to the card the owner is actually editing.
    #    Prefer an exact reply-to-card binding; accept a lone pending edit; REFUSE to
    #    guess when several edits are pending and the text isn't a reply to a card.
    m = _awaiting_map(context.user_data)
    reply_to_id = None
    try:
        rt = getattr(message, "reply_to_message", None)
        if rt is not None:
            reply_to_id = rt.message_id
    except Exception:  # noqa: BLE001
        reply_to_id = None
    outcome, awaiting = _resolve_awaiting(m, reply_to_msg_id=reply_to_id)
    if outcome == "ambiguous":
        # Do NOT misroute private content. Name the pending cards and require the owner
        # to reply to the specific one (or resend it as a reply).
        pending_desc = ", ".join(
            f"#{aid}" + (f" ({(e or {}).get('label')})" if (e or {}).get("label") else "")
            for aid, e in sorted(m.items())
        )
        await message.reply_text(
            "You have more than one edit in progress (" + pending_desc + "). Reply "
            "directly to the card you want to edit so I send your text to the right one."
        )
        return
    if outcome == "match" and awaiting is not None:
        action_id = int(awaiting)
        _consume_awaiting(m, action_id)

        def work() -> bool:
            # Capture the pre-edit draft first so the feedback diff (P5c) is real.
            before = repo.get_pending(conn, action_id)
            original = (before["draft_text"] if before else "") or ""
            # Guarded: only edits a still-sendable row. If the action was already
            # sent/skipped this returns False and we do NOT re-show an Approve card
            # (which would otherwise let an already-sent reply be sent again).
            if not repo.set_pending_draft(conn, action_id, text):
                return False
            recorder.record_edit(
                conn, repo.get_pending(conn, action_id), new_text=text, original_text=original
            )
            return True

        ok = await _run_db(context, work)
        if ok:
            await message.reply_text(
                f"Updated draft for #{action_id}. Review:\n{'─' * 24}\n{text}",
                reply_markup=_approve_markup(action_id),
            )
        else:
            await message.reply_text(
                f"#{action_id} has already been handled — I didn't change anything."
            )
        return

    # 1b) "name: Alex" — user identifying an unknown contact from the card footer prompt.
    import re as _re
    # "name: Alex" or "name: Alex +15551234567" — identify an unknown contact
    _name_match = _re.match(r"^name\s*:\s*(.+)$", text, _re.IGNORECASE)
    if _name_match:
        rest = _name_match.group(1).strip()
        # Optional phone number at end: "Alex +15551234567"
        _phone_in_name = _re.search(r"(\+?\d[\d\s\-]{7,})", rest)
        phone_given = _re.sub(r"[\s\-]", "", _phone_in_name.group(1)) if _phone_in_name else None
        name_given = rest[:_phone_in_name.start()].strip() if _phone_in_name else rest
        if name_given:
            def _save_name() -> str:
                import os as _os, json as _json
                _path = _os.path.join(
                    _os.path.dirname(__file__), "..", "..", "data", "pending_identity.json"
                )
                try:
                    with open(_path) as _f:
                        _rec = _json.load(_f)
                    jid = _rec.get("jid", "")
                    asked_at = int(_rec.get("ts", 0))
                except Exception:
                    return "No recent unknown contact to name."
                if not jid:
                    return "No recent unknown contact to name."
                if int(time.time()) - asked_at > 86400:
                    return "That unknown-contact prompt is more than 24 h old — ignoring."
                # Update LID entry with name
                conn.execute(
                    """INSERT INTO contacts (email, name, relationship, importance)
                       VALUES (?, ?, 'phone_contact', 20)
                       ON CONFLICT(email) DO UPDATE SET
                         name=excluded.name,
                         relationship='phone_contact',
                         importance=MAX(importance, 20)""",
                    (jid, name_given),
                )
                msg = f"Saved: {name_given!r} for {jid}"
                # If phone number given, also create/update the phone JID entry and link both
                if phone_given:
                    clean = phone_given if phone_given.startswith("+") else f"+{phone_given}"
                    digits = _re.sub(r"\D", "", clean)
                    phone_jid = f"{digits}@s.whatsapp.net"
                    conn.execute(
                        """INSERT INTO contacts (email, name, relationship, importance)
                           VALUES (?, ?, 'phone_contact', 20)
                           ON CONFLICT(email) DO UPDATE SET
                             name=excluded.name,
                             relationship='phone_contact',
                             importance=MAX(importance, 20)""",
                        (phone_jid, name_given),
                    )
                    # Store LID→phone mapping in whatsapp_inbox for future resolution
                    conn.execute(
                        "UPDATE whatsapp_inbox SET phone_number=? WHERE jid=?",
                        (clean, jid),
                    )
                    msg += f" + phone JID {phone_jid}"
                conn.commit()
                try:
                    _os.remove(_path)
                except Exception:
                    pass
                return msg
            result = await _run_db(context, _save_name)
            await message.reply_text(f"Got it. {result}")
            return

    # 2) Compose intent ("email Rajesh that …"). Fix 4: instead of a text SEND/CANCEL
    #    flow (which used to stub out and send nothing), build a normal approval CARD —
    #    Jatin taps Approve to send, exactly like every reply. Nothing sends here.
    if _compose is not None:
        try:
            intent = _compose.detect_compose_intent(text)
            if intent is not None:
                settings_for_compose = context.application.bot_data[_K_SETTINGS]
                llm_for_compose = context.application.bot_data.get(_K_LLM)
                # compose_and_queue does an LLM call + DB read — run it on the single-worker
                # DB executor (NOT inline in the async handler) so it can't block the whole
                # Telegram event loop for up to the LLM timeout, and never races `conn`.
                result = await _run_db(context, partial(
                    _compose.compose_and_queue,
                    intent['intent_text'], intent['channel'],
                    conn, settings_for_compose, llm_for_compose))
                if result.get('status') == 'needs_clarification':
                    opts = ', '.join(r.get('name', '?') for r in result.get('options', [])[:4])
                    await message.reply_text(f"Which {opts}? Reply with the full name.")
                    return
                elif result.get('status') == 'ready':
                    recipients = result.get('recipients', [])
                    draft = result.get('draft', '')
                    to_str = ', '.join(r.get('name', '?') for r in recipients)
                    aid = await _run_db(
                        context, partial(_queue_compose_card, conn, settings, result))
                    if aid is None:
                        await message.reply_text("Couldn't queue that compose — try again.")
                        return
                    await message.reply_text(
                        f"📝 Draft to {to_str} — tap Approve to send, Edit to change, "
                        f"Skip to drop:\n{'─' * 24}\n{draft[:1200]}",
                        reply_markup=_approve_markup(aid, settings),
                    )
                    return
                elif result.get('status') == 'not_found':
                    await message.reply_text(
                        f"Could not find a contact matching: {result.get('query', '?')}")
                    return
        except Exception:  # noqa: BLE001
            pass  # fall through to existing command handling

    # 2c) Otherwise treat it as a free-text command.
    llm: LLMClient = context.application.bot_data[_K_LLM]
    mail = context.application.bot_data[_K_MAIL]
    notifier = context.application.bot_data[_K_NOTIFIER]
    reply = await _run_db(
        context,
        partial(commands.apply_command, conn, settings, llm, mail, notifier, text),
    )
    await message.reply_text(reply)


# ─────────────────────────────────────────────────────────────────────────────
# Error handler
# ─────────────────────────────────────────────────────────────────────────────
_conflict_alerted = False  # dedup the 409-Conflict alert so we don't spam Jatin


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    log.exception("telegram handler error: %s", err)
    # A 409 Conflict means a SECOND process is polling this same bot token (two engines)
    # — the classic "Steward silently goes dead" glitch. Surface it once so it's never
    # invisible. (The engine's single-instance lock should prevent this; this is the
    # belt-and-braces alert if one slips through, e.g. a stray manual run.py.)
    try:
        from telegram.error import Conflict
        if isinstance(err, Conflict):
            global _conflict_alerted
            if not _conflict_alerted:
                _conflict_alerted = True
                notifier = context.application.bot_data.get(_K_NOTIFIER)
                if notifier is not None:
                    notifier.error(
                        "⚠️ Another Steward instance is polling this bot token (Telegram 409). "
                        "One of them will stop receiving your messages. Quit the duplicate "
                        "(or restart Steward) so only one is running."
                    )
    except Exception:  # noqa: BLE001 - the error handler must never raise
        log.debug("on_error conflict alert failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Wiring
# ─────────────────────────────────────────────────────────────────────────────
def build_application(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: LLMClient,
    mail: Any,
    notifier: Any,
) -> Application:
    """Construct the polling Application with all handlers wired.

    ``conn`` MUST be the connection created on this (the bot's main) thread; see
    the module docstring on threading/sqlite ownership.
    """
    application = (
        Application.builder().token(settings.telegram_bot_token).build()
    )

    # Single-worker executor so all blocking DB work on the shared `conn` is
    # serialized (sqlite + one connection across threads must not run concurrently).
    db_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bot-db")

    application.bot_data[_K_CONN] = conn
    application.bot_data[_K_SETTINGS] = settings
    application.bot_data[_K_LLM] = llm
    application.bot_data[_K_MAIL] = mail
    application.bot_data[_K_NOTIFIER] = notifier
    application.bot_data[_K_DB_EXECUTOR] = db_executor

    # Slash commands.
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("brief", cmd_brief))
    application.add_handler(CommandHandler("undo", cmd_undo))
    application.add_handler(CommandHandler("declineall", cmd_declineall))
    application.add_handler(CommandHandler("wastatus", cmd_wastatus))
    application.add_handler(CommandHandler("state", _state_command))

    # Inline query (user types "@stewardbot <query>" in any chat).
    from telegram.ext import InlineQueryHandler
    application.add_handler(InlineQueryHandler(_inline_query_handler))

    # Inline-button taps.
    application.add_handler(CallbackQueryHandler(on_callback))

    # Plain text (anything that isn't a slash command).
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)
    )

    application.add_error_handler(on_error)
    return application


def run_bot(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: LLMClient,
    mail: Any,
    notifier: Any,
) -> None:
    """Build and run the bot via long polling (blocking; owns the main thread)."""
    application = build_application(conn, settings, llm, mail, notifier)
    log.info("telegram bot starting (long polling)…")
    # bootstrap_retries=-1: if Telegram is briefly unreachable at startup (network blip,
    # VPN, a transient api.telegram.org timeout), RETRY FOREVER instead of aborting. The
    # default (0) makes run_polling crash on the first timeout, which used to take down the
    # whole engine — including email/WhatsApp processing, which run in this same process.
    # Now the engine stays up and the bot reconnects on its own when Telegram returns.
    # drop_pending_updates discards the backlog queued while we were down (no stale flood).
    application.run_polling(drop_pending_updates=True, bootstrap_retries=-1)
