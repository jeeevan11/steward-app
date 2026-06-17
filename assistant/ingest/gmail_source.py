"""`GmailSource` — the Phase-1 Gmail implementation of `MailSource`.

Responsibilities:
  * incremental fetch via the Gmail History API (with a 404/expired fallback to
    a recent-messages query)
  * full-thread fetch + normalization (delegated to `normalize.build_thread`)
  * the reversible side effects (archive, label, undo) with undo_data dicts
  * sending RFC2822 replies threaded correctly via In-Reply-To/References + threadId

The Google client libraries are imported at module top; this module is only
imported at runtime, never by the unit tests.
"""

from __future__ import annotations

import base64
import sqlite3
from email.message import EmailMessage
from typing import Any, Callable, Optional

from googleapiclient.errors import HttpError

from assistant.config import Settings
from assistant.ingest import gmail_auth
from assistant.ingest.base import MailSource
from assistant.ingest.normalize import build_thread, message_from_gmail
from assistant.logging_setup import get_logger
from assistant.models import Message, Thread
from assistant.storage import ledger
from assistant.storage import repositories as repo

log = get_logger("ingest.gmail")

_INBOX = "INBOX"


class GmailSource(MailSource):
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings
        self.service = None  # built in connect()
        self._label_cache: dict[str, str] = {}  # label name -> label id
        # NO_SILENT_LOSS: the poller injects a callback so a history-gap resync can warn
        # the owner that mail older than the resync window may need a manual look. Left
        # None in tests / status reads (resync still records a metric + logs).
        self.on_coverage_gap: Optional[Callable[[str], None]] = None

    def _resync_query(self) -> str:
        days = max(1, int(getattr(self.settings, "gmail_resync_days", 7) or 7))
        return f"in:inbox newer_than:{days}d"

    # -- lifecycle ------------------------------------------------------------
    def connect(self) -> None:
        """Build the Gmail service and seed a starting historyId if none stored."""
        self.service = gmail_auth.build_service(self.settings)
        if repo.get_last_history_id(self.conn) is None:
            history_id = self._current_history_id()
            if history_id:
                repo.set_last_history_id(self.conn, history_id)
                log.info("Seeded starting Gmail historyId=%s", history_id)

    def _require_service(self):
        if self.service is None:
            self.connect()
        return self.service

    def _current_history_id(self) -> str:
        """The mailbox's current historyId from the user profile."""
        svc = self._require_service()
        profile = svc.users().getProfile(userId="me").execute()
        return str(profile.get("historyId", "") or "")

    # -- incremental fetch ----------------------------------------------------
    def fetch_new_message_ids(self) -> list[str]:
        """Collect message ids added to INBOX since the stored historyId.

        Updates the stored historyId to the newest seen. On a 404 (expired
        historyId) falls back to a recent-messages query and stores the current
        profile historyId.
        """
        svc = self._require_service()
        start = repo.get_last_history_id(self.conn)
        if not start:
            # No cursor at all: first-run seed (not a gap — don't alarm the owner).
            return self._resync_recent(gap=False)

        new_ids: list[str] = []
        seen: set[str] = set()
        newest_history = str(start)
        page_token: str | None = None
        try:
            while True:
                # ingest-email-5 — ROOT CAUSE: requesting only "messageAdded" history
                # misses mail that becomes INBOX-resident LATER. A delivery-time Gmail
                # filter that "Skips the Inbox" (or routes to a category/label) produces a
                # messageAdded record WITHOUT the INBOX label; when the owner — or a rule,
                # or moving from the Promotions/Updates tab to Primary — later moves it INTO
                # the inbox, that is a "labelAdded" record (INBOX gained), a history type the
                # old code never requested. With a valid (non-expired) cursor, the resync
                # fallback never runs, so such mail was silently never enumerated, never
                # ledgered, never surfaced (violating NO_SILENT_LOSS).
                # FIX: also request "labelAdded" and treat any message that GAINS the INBOX
                # label as a new-to-inbox event. labelId=INBOX still scopes both record types
                # to INBOX-touching changes. Dedup is the ledger's job (mark_seen below is
                # idempotent), so a message that both arrives in AND is later re-labeled to
                # the inbox is collected at most once and never double-processed.
                kwargs: dict[str, Any] = {
                    "userId": "me",
                    "startHistoryId": start,
                    "historyTypes": ["messageAdded", "labelAdded"],
                    "labelId": _INBOX,
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = svc.users().history().list(**kwargs).execute()

                if resp.get("historyId"):
                    newest_history = str(resp["historyId"])

                for hist in resp.get("history", []) or []:
                    # (a) messages that ARRIVED already in the inbox.
                    for added in hist.get("messagesAdded", []) or []:
                        msg = added.get("message", {}) or {}
                        mid = msg.get("id")
                        labels = msg.get("labelIds", []) or []
                        if not mid or mid in seen:
                            continue
                        # Only items actually in the inbox (not drafts/sent/etc.).
                        if _INBOX not in labels:
                            continue
                        seen.add(mid)
                        new_ids.append(mid)
                    # (b) messages that GAINED the INBOX label after delivery. Gmail keys
                    #     this as "labelsAdded" (plural) with the labelIds that were added;
                    #     we only care when INBOX is among them.
                    for labeled in hist.get("labelsAdded", []) or []:
                        added_label_ids = labeled.get("labelIds", []) or []
                        if _INBOX not in added_label_ids:
                            continue
                        msg = labeled.get("message", {}) or {}
                        mid = msg.get("id")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        new_ids.append(mid)

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as exc:
            if getattr(exc, "resp", None) is not None and exc.resp.status == 404:
                log.warning("Gmail historyId %s expired; resyncing from recent messages.", start)
                return self._resync_recent(gap=True)
            raise

        # Durably record the work in the ledger BEFORE advancing the cursor. If we
        # advanced historyId first and crashed before these ids were recorded, the
        # next poll would start past them and they'd be silently skipped. Marking
        # them seen first means the ledger (the source of truth for "to process")
        # already owns them, so advancing the cursor can never drop a message.
        for mid in new_ids:
            ledger.mark_seen(self.conn, mid)
        repo.set_last_history_id(self.conn, newest_history)
        return new_ids

    def _resync_recent(self, *, gap: bool) -> list[str]:
        """Fallback: list recent inbox messages and store the current historyId.

        `gap=True` means we got here because the incremental historyId EXPIRED (an outage
        longer than Gmail's retained-history window), so there is a real risk that inbox
        mail older than the resync window was never seen. We rescan `GMAIL_RESYNC_DAYS`
        (default 7, was a silently-lossy hardcoded 1 day — finding `ingest-email-1`),
        record a metric, and surface a coverage-gap warning to the owner. `gap=False` is
        a benign first-run seed — no alarm.
        """
        svc = self._require_service()
        query = self._resync_query()
        ids: list[str] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"userId": "me", "q": query}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = svc.users().messages().list(**kwargs).execute()
            for m in resp.get("messages", []) or []:
                mid = m.get("id")
                if mid:
                    ids.append(mid)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        # Record the ids in the ledger before advancing the cursor (durable record first,
        # then move the cursor). mark_seen is idempotent: already-processed ids return
        # False, so a wide rescan re-marks nothing twice. Count the genuinely-new ones —
        # those are messages the gap would otherwise have dropped.
        recovered = 0
        for mid in ids:
            if ledger.mark_seen(self.conn, mid):
                recovered += 1
        # Store the mailbox's current historyId so the next poll is incremental.
        current = self._current_history_id()
        if current:
            repo.set_last_history_id(self.conn, current)

        if gap:
            self._on_gap_resync(scanned=len(ids), recovered=recovered)
        return ids

    def _on_gap_resync(self, *, scanned: int, recovered: int) -> None:
        """Observability + owner notification for a history-expiry (gap) resync.

        NO_SILENT_LOSS: a gap means we cannot prove mail older than the resync window was
        triaged, so we never stay silent. We always record a metric/log, and we tell the
        owner so they can eyeball older unread mail."""
        days = max(1, int(getattr(self.settings, "gmail_resync_days", 7) or 7))
        try:
            repo.record_event(
                self.conn, type="gmail_gap_resync",
                detail={"scanned": scanned, "recovered": recovered, "days": days},
            )
        except Exception:  # noqa: BLE001 - observability is best-effort
            log.debug("gmail gap-resync event failed", exc_info=True)
        log.warning(
            "Gmail history gap: resynced last %sd (scanned=%s, recovered=%s newly-seen).",
            days, scanned, recovered,
        )
        if self.on_coverage_gap is not None:
            recovered_note = (
                f" I recovered {recovered} message(s) that were about to be missed."
                if recovered else ""
            )
            try:
                self.on_coverage_gap(
                    f"⚠️ Reconnected after a Gmail history gap. I re-scanned the "
                    f"last {days} days of your inbox to catch up.{recovered_note} If you "
                    f"were offline longer than {days} days, please skim your inbox for "
                    f"older unread mail — I can't guarantee I saw it."
                )
            except Exception:  # noqa: BLE001 - never let a notify failure break ingest
                log.debug("coverage-gap notify failed", exc_info=True)

    # -- reads ----------------------------------------------------------------
    def _get_message(self, message_id: str) -> Message:
        svc = self._require_service()
        raw = (
            svc.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        return message_from_gmail(raw, self.settings, svc)

    def get_thread(self, message_id: str) -> Thread:
        """Fetch the full thread that contains `message_id`."""
        svc = self._require_service()
        # Resolve the thread id from the message (cheap metadata fetch).
        meta = (
            svc.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata", metadataHeaders=["Subject"])
            .execute()
        )
        thread_id = meta.get("threadId") or message_id
        return build_thread(svc, thread_id, self.settings)

    # -- labels ---------------------------------------------------------------
    def _ensure_label_id(self, label: str) -> str:
        """Return the Gmail label id for `label`, creating the label if needed."""
        svc = self._require_service()
        if label in self._label_cache:
            return self._label_cache[label]
        resp = svc.users().labels().list(userId="me").execute()
        for lab in resp.get("labels", []) or []:
            self._label_cache[lab.get("name", "")] = lab.get("id", "")
        if label in self._label_cache:
            return self._label_cache[label]
        created = (
            svc.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": label,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        label_id = created.get("id", "")
        self._label_cache[label] = label_id
        return label_id

    # -- mutations ------------------------------------------------------------
    def archive(self, message_id: str) -> dict:
        """Remove INBOX from a message (archive). Returns undo_data."""
        svc = self._require_service()
        svc.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": [_INBOX]}
        ).execute()
        return {"op": "archive", "message_id": message_id, "removed_labels": [_INBOX]}

    def apply_label(self, message_id: str, label: str) -> dict:
        """Apply a label (creating it if needed). Returns undo_data."""
        label_id = self._ensure_label_id(label)
        svc = self._require_service()
        svc.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label_id]}
        ).execute()
        return {
            "op": "label",
            "message_id": message_id,
            "label": label,
            "added_label_ids": [label_id],
        }

    def undo(self, undo_data: dict) -> None:
        """Reverse a previously-performed reversible action."""
        if not undo_data:
            return
        svc = self._require_service()
        op = undo_data.get("op")
        message_id = undo_data.get("message_id")
        if not message_id:
            return
        if op == "archive":
            removed = undo_data.get("removed_labels", [_INBOX]) or [_INBOX]
            svc.users().messages().modify(
                userId="me", id=message_id, body={"addLabelIds": removed}
            ).execute()
        elif op == "label":
            added = undo_data.get("added_label_ids")
            if not added:
                label = undo_data.get("label")
                added = [self._ensure_label_id(label)] if label else []
            if added:
                svc.users().messages().modify(
                    userId="me", id=message_id, body={"removeLabelIds": added}
                ).execute()
        else:
            log.warning("undo: unknown op %r; nothing to do.", op)

    # -- sending --------------------------------------------------------------
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
        """Build and send an RFC2822 reply threaded to the original. Returns sent id."""
        svc = self._require_service()

        msg = EmailMessage()
        if self.settings.gmail_address:
            msg["From"] = self.settings.gmail_address
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        subj = subject or ""
        if subj and not subj.lower().startswith("re:"):
            subj = f"Re: {subj}"
        msg["Subject"] = subj

        # Thread the reply: look up the original's RFC822 Message-Id header.
        rfc_message_id = self._rfc822_message_id(in_reply_to_gmail_id)
        if rfc_message_id:
            msg["In-Reply-To"] = rfc_message_id
            msg["References"] = rfc_message_id

        msg.set_content(body or "")

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        # Replies pass a real threadId (thread continuity). A compose passes "" — omit the
        # key entirely so Gmail starts a NEW thread instead of rejecting an empty threadId.
        send_body: dict[str, Any] = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id
        sent = (
            svc.users()
            .messages()
            .send(userId="me", body=send_body)
            .execute()
        )
        return str(sent.get("id", ""))

    def _rfc822_message_id(self, gmail_message_id: str) -> str:
        """Fetch the RFC822 Message-Id header of a Gmail message (for threading)."""
        if not gmail_message_id:
            return ""
        svc = self._require_service()
        try:
            meta = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=gmail_message_id,
                    format="metadata",
                    metadataHeaders=["Message-Id"],
                )
                .execute()
            )
            for h in (meta.get("payload", {}) or {}).get("headers", []) or []:
                if (h.get("name") or "").lower() == "message-id":
                    return h.get("value", "") or ""
        except Exception as exc:  # noqa: BLE001 - threading is best-effort
            log.debug("Could not fetch Message-Id for %s: %s", gmail_message_id, exc)
        return ""
