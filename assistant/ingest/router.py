"""Route a control-surface action (approve/undo) to the MailSource for its channel.

The Telegram bot and the web console act on pending items from EITHER channel, but
each only has one DB connection. A `MailRouter` holds the per-channel sources and
picks the right one by the action's message id — `wa_*` ids go to the WhatsApp
source, everything else to Gmail. Without this, approving a WhatsApp reply would be
sent through the Gmail API (wrong channel, never delivered).

`execute_send` calls `source_for(message_id)` when present; `undo_last` calls
`undo(undo_data)` which routes on the embedded message id. A plain MailSource (no
`source_for`) is still accepted unchanged — that's how the existing single-channel
tests keep passing.
"""

from __future__ import annotations

from typing import Any

from assistant.logging_setup import get_logger
from assistant.models import Thread

log = get_logger("ingest.router")


def channel_of(message_id: str) -> str:
    return "whatsapp" if (message_id or "").startswith("wa_") else "gmail"


class MailRouter:
    def __init__(self, sources: dict[str, Any], default: str = "gmail"):
        self.sources = sources
        self.default = default

    def source_for(self, message_id: str):
        ch = channel_of(message_id)
        src = self.sources.get(ch)
        if src is not None:
            return src
        return self.sources.get(self.default) or next(iter(self.sources.values()))

    # -- the subset of MailSource the control/web path may call, routed by id ---
    def get_thread(self, message_id: str) -> Thread:
        return self.source_for(message_id).get_thread(message_id)

    def archive(self, message_id: str) -> dict:
        return self.source_for(message_id).archive(message_id)

    def apply_label(self, message_id: str, label: str) -> dict:
        return self.source_for(message_id).apply_label(message_id, label)

    def undo(self, undo_data: dict) -> None:
        mid = undo_data.get("message_id", "")
        return self.source_for(mid).undo(undo_data)
