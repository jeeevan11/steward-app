"""The `MailSource` abstraction.

A `MailSource` is the only thing the rest of the system knows about a channel.
It hides the Gmail (or future WhatsApp) API entirely and speaks in the shared
domain types from `assistant.models`. Every method that mutates remote state
returns an `undo_data` dict so the action layer can reverse it on request.

The Phase-1 implementation is `GmailSource` in `gmail_source.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from assistant.models import Thread


class MailSource(ABC):
    """Abstract channel adapter: read new mail, fetch threads, act, undo, send.

    Implementations must be safe to construct cheaply; expensive setup (network,
    auth, building the API client) belongs in `connect`.
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish the channel connection / build the API client.

        Also responsible for seeding any cursor the channel needs (e.g. an
        initial Gmail historyId) so the first `fetch_new_message_ids` has a
        starting point instead of replaying the whole mailbox.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_new_message_ids(self) -> list[str]:
        """Return NEW channel message ids since the stored cursor.

        Advances and persists the cursor (e.g. via `repo.set_last_history_id`).
        If the stored cursor has expired (Gmail returns 404 for a too-old
        historyId), the implementation resyncs from recent messages instead of
        failing.
        """
        raise NotImplementedError

    @abstractmethod
    def get_thread(self, message_id: str) -> Thread:
        """Fetch the full thread containing `message_id`, normalized.

        Messages are ordered oldest -> newest, bodies decoded to plain text, and
        any PDF attachments have their text extracted into `Attachment.extracted_text`.
        """
        raise NotImplementedError

    @abstractmethod
    def archive(self, message_id: str) -> dict:
        """Archive a message (remove it from the inbox).

        Returns an undo_data dict, e.g.
        ``{"op": "archive", "message_id": ..., "removed_labels": ["INBOX"]}``.
        """
        raise NotImplementedError

    @abstractmethod
    def apply_label(self, message_id: str, label: str) -> dict:
        """Apply a label to a message, creating it if it does not exist.

        Returns an undo_data dict describing how to reverse the change.
        """
        raise NotImplementedError

    @abstractmethod
    def undo(self, undo_data: dict) -> None:
        """Reverse a previously-performed reversible action from its undo_data."""
        raise NotImplementedError

    @abstractmethod
    def send_reply(
        self,
        *,
        thread_id: str,
        to: list[str],
        cc: list[str],
        subject: str,
        body: str,
        in_reply_to_gmail_id: str,
    ) -> str:
        """Send a reply within a thread. Returns the sent channel message id."""
        raise NotImplementedError
