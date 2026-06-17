"""Presence: is the owner handling a conversation himself right now?

If so, the agent must NOT ping him about it (he's already in it) — but it still tracks
everything (see storage/wa_messages). Two signals, in priority order:

  1. Per-conversation (reliable): the owner sent his OWN message in this chat within
     the cooldown window. This is the precise "I'm talking to this person" signal and
     is unaffected by other chats — if someone else texts, that chat is NOT suppressed.
  2. App focus (best-effort): the native WhatsApp Mac app is frontmost, so he's
     actively in WhatsApp and will see things himself. Detected via `osascript`.
     NOTE: WhatsApp Web in a browser tab cannot be detected this way — the
     per-conversation signal is the dependable one.

Everything here is best-effort and fail-CLOSED toward *surfacing*: on any error we
return "not suppressed" so we never go silent by accident.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import time

from assistant.config import Settings
from assistant.logging_setup import get_logger
from assistant.storage import wa_messages

log = get_logger("presence")

# The native WhatsApp Mac app's process name(s) as seen by System Events.
_WHATSAPP_APP_NAMES = ("WhatsApp", "WhatsApp Messenger")

# Cache the frontmost-app probe briefly so we don't spawn osascript per message.
_FRONT_CACHE: dict[str, float | str] = {"value": "", "at": 0.0}
_FRONT_TTL = 5.0  # seconds


def _frontmost_app() -> str:
    """Name of the frontmost macOS app (""/best-effort). Cached for a few seconds."""
    now = time.time()
    if now - float(_FRONT_CACHE["at"]) < _FRONT_TTL:
        return str(_FRONT_CACHE["value"])
    name = ""
    try:
        osa = shutil.which("osascript")
        if osa:
            out = subprocess.run(
                [osa, "-e",
                 'tell application "System Events" to get name of first application '
                 'process whose frontmost is true'],
                capture_output=True, text=True, timeout=2,
            )
            name = (out.stdout or "").strip()
    except Exception:  # noqa: BLE001 - probing must never raise
        name = ""
    _FRONT_CACHE["value"] = name
    _FRONT_CACHE["at"] = now
    return name


def whatsapp_app_frontmost(settings: Settings) -> bool:
    if not getattr(settings, "presence_app_focus_enabled", True):
        return False
    try:
        return _frontmost_app() in _WHATSAPP_APP_NAMES
    except Exception:  # noqa: BLE001
        return False


# WhatsApp group chat JIDs end with this suffix (1:1 chats end '@s.whatsapp.net').
_GROUP_JID_SUFFIX = "@g.us"


def _is_group_jid(jid: str) -> bool:
    return bool(jid) and jid.endswith(_GROUP_JID_SUFFIX)


def is_actively_handling(
    conn: sqlite3.Connection,
    settings: Settings,
    jid: str,
    *,
    is_group: bool | None = None,
) -> bool:
    """True if the owner is handling THIS conversation himself right now → don't ping.
    Reliable per-conversation signal (his recent outbound) OR the native app being
    frontmost. Never raises (returns False → surface).

    control-state-presence-7 ROOT CAUSE FIX
    ───────────────────────────────────────
    The per-conversation outbound shortcut keys on `jid` and means, precisely, "the
    owner sent a message to THIS chat recently, so he is talking to this person." For a
    1:1 chat that inference is sound. For a GROUP chat it is wrong: the owner posting
    one line to a busy @g.us group does NOT mean he is watching for, and will handle,
    every OTHER member's subsequent reply. With the old code, any owner group post set
    last_outbound_ts(group_jid) and silenced every owner-mentioning group message for
    the full cooldown window (~5 min) by down-tiering it to SILENT — so a member who
    @mentions the owner right after he posts is never surfaced.

    Fix: detect group chats (jid endswith '@g.us', or an explicit `is_group` from the
    caller) and SKIP the outbound shortcut for them. A group is only ever treated as
    "actively handling" via the app-frontmost signal (the owner literally has WhatsApp
    open), never via a stale group post. 1:1 suppression is unchanged. `is_group`
    defaults to None → auto-detect from the JID suffix, so existing callers that pass
    `thread.id` get the correct behavior with no signature change."""
    if not getattr(settings, "presence_suppression_enabled", True):
        return False
    if is_group is None:
        is_group = _is_group_jid(jid)
    try:
        # The recent-outbound shortcut is a *1:1-only* signal. Never let one group post
        # silence unrelated owner-mentions in that group (control-state-presence-7).
        if jid and not is_group:
            cooldown = int(getattr(settings, "presence_outbound_cooldown_seconds", 300))
            last_out = wa_messages.last_outbound_ts(conn, jid)
            if last_out and (time.time() - last_out) <= cooldown:
                return True
        elif jid and is_group:
            # Observability: make the deliberate skip visible rather than silent, so a
            # "why wasn't this group message suppressed?" question is answerable.
            log.debug(
                "presence: skipping outbound-suppression shortcut for group jid %s "
                "(group posts do not imply handling every member reply)", jid,
            )
        return whatsapp_app_frontmost(settings)
    except Exception:  # noqa: BLE001 - fail toward surfacing
        return False
