"""Best-effort attachment text extraction.

The only extractor that matters for Phase 1 is PDF, via `pypdf`. The import is
guarded so a machine without `pypdf` installed simply gets empty text rather
than crashing the ingest pipeline — attachments are an enrichment, never a
hard dependency.
"""

from __future__ import annotations

import io

from assistant.logging_setup import get_logger

log = get_logger("ingest.attachments")

# Guarded import: a missing pypdf degrades gracefully to "" extracted text.
try:  # pragma: no cover - exercised only when pypdf is installed
    import pypdf  # type: ignore

    _HAVE_PYPDF = True
except Exception:  # noqa: BLE001 - any import failure means "no PDF text"
    pypdf = None  # type: ignore
    _HAVE_PYPDF = False


def extract_pdf_text(data: bytes, max_chars: int = 20000) -> str:
    """Extract text from raw PDF bytes, truncated to `max_chars`.

    Returns "" if pypdf is unavailable, the bytes are empty, or extraction
    fails for any reason (encrypted, malformed, etc.). Never raises.
    """
    if not data or not _HAVE_PYPDF:
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - skip unreadable pages
                text = ""
            if not text:
                continue
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
        out = "\n".join(parts).strip()
        return out[:max_chars]
    except Exception as exc:  # noqa: BLE001 - corrupt/encrypted PDFs etc.
        log.debug("PDF extraction failed: %s", exc)
        return ""


def extract_attachment_text(filename: str, mime_type: str, data: bytes) -> str:
    """Dispatch to the right extractor based on mime type / filename.

    Currently only PDFs yield text; everything else returns "". Never raises.
    """
    name = (filename or "").lower()
    mime = (mime_type or "").lower()
    if mime == "application/pdf" or mime.endswith("/pdf") or name.endswith(".pdf"):
        return extract_pdf_text(data)
    return ""
