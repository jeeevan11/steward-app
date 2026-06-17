"""Turn raw Gmail API message/thread dicts into channel-agnostic domain types.

The Gmail `messages.get`/`threads.get` payload is a nested MIME tree with
base64url-encoded part bodies and an attachment side-channel. This module flattens
all of that into the flat `Message`/`Thread` shape the brain understands:

  * From/To/Cc/Subject/Date headers parsed (addresses split, names kept)
  * the text/plain body preferred, falling back to a tag-stripped text/html body
  * attachments collected, and PDF text extracted inline
  * `from_me` set by comparing the sender to `settings.gmail_address`

Only the Gmail-shaped dicts and a service handle are Gmail-specific here; the
output is pure `assistant.models`.
"""

from __future__ import annotations

import base64
import html
import re
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from assistant.config import Settings
from assistant.ingest.attachments import extract_attachment_text
from assistant.logging_setup import get_logger
from assistant.models import Attachment, Channel, Message, Thread

log = get_logger("ingest.normalize")

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")

# Gmail system labels used for the ownership signal (ingest-email-3).
_SENT_LABEL = "SENT"
_INBOX_LABEL = "INBOX"


# ─────────────────────────────────────────────────────────────────────────────
# Low-level decoding helpers
# ─────────────────────────────────────────────────────────────────────────────
def _b64url_decode(data: str) -> bytes:
    """Decode a Gmail base64url body string. Tolerant of missing padding."""
    if not data:
        return b""
    try:
        s = data.replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        return base64.b64decode(s)
    except Exception as exc:  # noqa: BLE001
        log.debug("base64url decode failed: %s", exc)
        return b""


def _decode_text(data: str) -> str:
    """Decode a base64url part body to a unicode string (best effort)."""
    return _b64url_decode(data).decode("utf-8", errors="replace")


def _strip_html(raw: str) -> str:
    """Crudely turn an HTML body into readable plain text."""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
    return text.strip()


def _headers_map(payload: dict[str, Any]) -> dict[str, str]:
    """Case-insensitive header lookup map for a payload."""
    out: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        name = (h.get("name") or "").lower()
        if name and name not in out:
            out[name] = h.get("value", "") or ""
    return out


def _split_addresses(value: str) -> list[str]:
    """Parse a header value into a list of lowercased email addresses."""
    if not value:
        return []
    return [addr.lower() for _name, addr in getaddresses([value]) if addr]


def _parse_sender(value: str) -> tuple[str, str]:
    """Return (email, display_name) for a From header value."""
    if not value:
        return "", ""
    pairs = getaddresses([value])
    if not pairs:
        return "", ""
    name, addr = pairs[0]
    return addr.lower(), (name or "").strip()


def _parse_timestamp(headers: dict[str, str], gmail_msg: dict[str, Any]) -> float:
    """Best-effort epoch seconds: prefer Gmail's internalDate, fall back to Date header."""
    internal = gmail_msg.get("internalDate")
    if internal:
        try:
            return int(internal) / 1000.0
        except (TypeError, ValueError):
            pass
    date_hdr = headers.get("date", "")
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            if dt is not None:
                return dt.timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# MIME tree walking
# ─────────────────────────────────────────────────────────────────────────────
def _walk_parts(payload: dict[str, Any]):
    """Yield every part in the MIME tree (including the root payload)."""
    stack = [payload]
    while stack:
        part = stack.pop()
        if not isinstance(part, dict):
            continue
        yield part
        for child in part.get("parts", []) or []:
            stack.append(child)


def _extract_bodies(payload: dict[str, Any]) -> tuple[str, str]:
    """Return (text_plain, text_html) bodies gathered from the MIME tree."""
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    for part in _walk_parts(payload):
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        filename = part.get("filename") or ""
        if filename:
            # Attachments are handled separately; never treat as body text.
            continue
        data = body.get("data")
        if not data:
            continue
        if mime == "text/plain":
            plain_chunks.append(_decode_text(data))
        elif mime == "text/html":
            html_chunks.append(_decode_text(data))
    return "\n".join(plain_chunks).strip(), "\n".join(html_chunks).strip()


def _fetch_attachment_data(
    service, message_id: str, part: dict[str, Any]
) -> bytes:
    """Return the raw bytes for an attachment part (inline or via attachmentId)."""
    body = part.get("body") or {}
    data = body.get("data")
    if data:
        return _b64url_decode(data)
    attachment_id = body.get("attachmentId")
    if not attachment_id or service is None:
        return b""
    try:
        att = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        return _b64url_decode(att.get("data", ""))
    except Exception as exc:  # noqa: BLE001 - attachment fetch is best-effort
        log.debug("Attachment fetch failed for %s: %s", message_id, exc)
        return b""


def _collect_attachments(
    service, message_id: str, payload: dict[str, Any]
) -> list[Attachment]:
    """Collect attachments from the MIME tree, extracting text where possible."""
    out: list[Attachment] = []
    for part in _walk_parts(payload):
        filename = part.get("filename") or ""
        if not filename:
            continue
        mime = part.get("mimeType") or ""
        body = part.get("body") or {}
        size = int(body.get("size") or 0)
        data = _fetch_attachment_data(service, message_id, part)
        extracted = ""
        if data:
            try:
                extracted = extract_attachment_text(filename, mime, data)
            except Exception as exc:  # noqa: BLE001 - extraction is best-effort
                log.debug("Attachment text extraction failed (%s): %s", filename, exc)
                extracted = ""
        out.append(
            Attachment(
                filename=filename,
                mime_type=mime,
                size=size or len(data),
                extracted_text=extracted,
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Ownership / spoof observability (ingest-email-3)
# ─────────────────────────────────────────────────────────────────────────────
def _record_spoofed_owner_from(settings: Settings, message_id: str, labels: list[str]) -> None:
    """Observe a message whose From header CLAIMS the owner but that Gmail did not tag
    SENT — i.e. a delivered/INBOX message forging the owner's address.

    The from_me decision already refuses to honor it; this makes the attempt non-silent.
    message_from_gmail has no DB handle (its signature is shared by several callers), so
    observability is a structured WARNING log rather than a repo.record_event. Best-effort:
    a logging failure must never break ingestion."""
    try:
        log.warning(
            "ingest: From header claims owner (%s) but message %s is not SENT "
            "(labels=%s) — refusing from_me (possible spoof).",
            (settings.gmail_address or "").lower(),
            message_id or "?",
            ",".join(labels) or "-",
        )
    except Exception:  # noqa: BLE001 - observability must never break ingest
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Public: dict -> Message / Thread
# ─────────────────────────────────────────────────────────────────────────────
def message_from_gmail(
    gmail_msg: dict[str, Any], settings: Settings, service=None
) -> Message:
    """Normalize a single Gmail `messages.get(format='full')` dict into a Message."""
    payload = gmail_msg.get("payload", {}) or {}
    headers = _headers_map(payload)

    sender_email, sender_name = _parse_sender(headers.get("from", ""))
    recipients = _split_addresses(headers.get("to", ""))
    cc = _split_addresses(headers.get("cc", ""))
    subject = headers.get("subject", "") or ""

    plain, html_body = _extract_bodies(payload)
    body_text = plain or _strip_html(html_body)

    attachments = _collect_attachments(service, gmail_msg.get("id", ""), payload)

    labels = list(gmail_msg.get("labelIds", []) or [])

    # ingest-email-3 — ROOT CAUSE: deriving `from_me` purely from the (forgeable)
    # From header let an attacker spoof `From: <owner>` on an INBOX message and be
    # treated as owner-sent. That message is then (a) skipped by Thread.latest_inbound
    # (models.py) so the brain/approval card quote the WRONG message, and (b) rendered
    # as "From: ME" by Thread.render_for_prompt, injecting attacker text into the LLM
    # prompt as if the owner authored it. SPF/DKIM are never checked here.
    # FIX: trust Gmail's own ownership signal, not the header. A message is owner-sent
    # only when Gmail tagged it SENT, OR it matches the owner address AND is NOT a
    # delivered INBOX message (a real self-sent copy can carry both SENT and INBOX,
    # which the SENT branch already covers). A forged From on an INBOX-only message can
    # therefore never be mistaken for owner-sent. When the header CLAIMS to be the owner
    # but Gmail did not tag it SENT, record an observability event so the spoof attempt
    # is never silent.
    my = (settings.gmail_address or "").lower()
    is_sent = _SENT_LABEL in labels
    header_claims_owner = bool(my and sender_email == my)
    # A genuine self-sent message ALWAYS carries the SENT label, so that branch covers
    # every legitimate owner-sent case. The second clause is a defensive fallback for a
    # non-delivered owner-addressed message Gmail did not (yet) tag SENT, e.g. a DRAFT —
    # never an INBOX-resident delivered message, where a forged From would otherwise win.
    from_me = is_sent or (header_claims_owner and _INBOX_LABEL not in labels)

    if header_claims_owner and not from_me:
        # A header forged to look like the owner on a message Gmail did NOT mark SENT.
        # Surface it (best-effort) so the attempt is observable, never silently honored.
        _record_spoofed_owner_from(settings, gmail_msg.get("id", ""), labels)

    return Message(
        id=gmail_msg.get("id", ""),
        thread_id=gmail_msg.get("threadId", ""),
        channel=Channel.GMAIL,
        sender_email=sender_email,
        sender_name=sender_name,
        recipients=recipients,
        cc=cc,
        reply_to=headers.get("reply-to", "") or "",  # ingest-email-2: reply goes here over From
        subject=subject,
        body_text=body_text,
        snippet=gmail_msg.get("snippet", "") or "",
        timestamp=_parse_timestamp(headers, gmail_msg),
        labels=labels,
        attachments=attachments,
        from_me=from_me,
    )


def build_thread(service, thread_id: str, settings: Settings) -> Thread:
    """Fetch the full Gmail thread and normalize it into a Thread (oldest->newest)."""
    data = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    raw_messages = data.get("messages", []) or []
    messages = [message_from_gmail(m, settings, service) for m in raw_messages]
    # Gmail returns messages chronologically, but sort defensively oldest->newest.
    messages.sort(key=lambda m: m.timestamp)

    subject = ""
    for m in messages:
        if m.subject:
            subject = m.subject
            break

    return Thread(
        id=thread_id,
        channel=Channel.GMAIL,
        subject=subject,
        messages=messages,
    )
