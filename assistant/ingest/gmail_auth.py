"""Gmail OAuth (desktop installed-app flow) + service construction.

`get_credentials` runs the standard local desktop OAuth flow on first use,
caches the token JSON at `settings.gmail_token_path`, and silently refreshes an
expired token on subsequent runs. `build_service` wraps that into a ready-to-use
Gmail v1 service object.

These functions are only ever called at runtime (never by the unit tests), so
the Google client libraries are imported at module top.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from assistant.config import Settings
from assistant.logging_setup import get_logger

log = get_logger("ingest.gmail_auth")


def get_credentials(settings: Settings) -> Credentials:
    """Return valid Gmail OAuth credentials, running/refreshing the flow as needed.

    Order of operations:
      1. Load cached token from `settings.gmail_token_path` if present.
      2. If the token is expired but refreshable, refresh it.
      3. Otherwise run the InstalledAppFlow desktop flow against
         `settings.gmail_credentials_path`.
      4. Persist the (possibly refreshed/new) token back to disk.
    """
    scopes = list(settings.gmail_scopes)
    token_path = settings.gmail_token_path
    creds: Credentials | None = None

    if token_path and Path(token_path).exists():
        try:
            creds = Credentials.from_authorized_user_file(token_path, scopes)
        except Exception as exc:  # noqa: BLE001 - corrupt token file -> re-auth
            log.warning("Could not load cached Gmail token (%s); re-authenticating.", exc)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001 - refresh failed -> full flow
                log.warning("Gmail token refresh failed (%s); re-running OAuth flow.", exc)
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.gmail_credentials_path, scopes
            )
            creds = flow.run_local_server(port=0)

        # Persist the token for next time.
        try:
            Path(token_path).parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
            try:
                os.chmod(token_path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            log.warning("Could not persist Gmail token to %s: %s", token_path, exc)

    return creds


def _timed_http(creds, timeout: float = 30.0):
    """Wrap OAuth creds in an httplib2.Http with a TIMEOUT. The default httplib2 timeout
    is None, so a black-holed Gmail list/get/send would hang forever — and since those run
    on the bot's single-worker executor, one hang freezes every tap. Returns None if
    google_auth_httplib2 isn't available (caller falls back to the untimed default)."""
    try:
        import google_auth_httplib2
        import httplib2
        return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
    except Exception:  # noqa: BLE001 - degrade to the default (untimed) path
        return None


def build_service(settings: Settings):
    """Build and return an authenticated Gmail API v1 service object (timed HTTP)."""
    creds = get_credentials(settings)
    http = _timed_http(creds)
    if http is not None:
        return build("gmail", "v1", http=http, cache_discovery=False)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_calendar_service(settings: Settings):
    """Build a Google Calendar v3 service (read-only) reusing the same OAuth token (timed).

    Requires CALENDAR_ENABLED (which adds the calendar.readonly scope). If the cached
    token predates enabling calendar, get_credentials re-runs the consent flow to add
    the scope."""
    creds = get_credentials(settings)
    http = _timed_http(creds)
    if http is not None:
        return build("calendar", "v3", http=http, cache_discovery=False)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
