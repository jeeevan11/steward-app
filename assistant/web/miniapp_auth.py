"""Telegram Mini App authentication for the Steward AI web console.

When the React dashboard is opened inside Telegram's Mini App, Telegram provides
a signed "initData" string. This module:

  1. Validates the HMAC-SHA256 signature (per Telegram's official spec).
  2. Issues a short-lived session token (HMAC-based, stdlib only — no PyJWT).
  3. Provides a FastAPI dependency (MiniAppAuth) for protecting Mini App routes.

Stdlib only: hashlib, hmac, time, json, base64, urllib.parse.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Optional

from fastapi import Header, HTTPException


# ── 1. Validate Telegram initData ────────────────────────────────────────────

def validate_init_data(init_data_str: str, bot_token: str) -> Optional[dict]:
    """Validate a Telegram Mini App initData string.

    Implements the official Telegram spec:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    Parameters
    ----------
    init_data_str:
        The raw ``window.Telegram.WebApp.initData`` string passed from the
        Mini App frontend.
    bot_token:
        The Telegram bot token (from Settings.telegram_bot_token).

    Returns
    -------
    dict | None
        ``{'user': {...}, 'auth_date': <int>}`` on success, ``None`` on any
        validation failure (missing fields, bad signature, expired data).
    """
    try:
        # a. Parse query-string; parse_qs returns lists — take [0] of each.
        parsed_qs = urllib.parse.parse_qs(init_data_str, keep_blank_values=True)
        parsed: dict[str, str] = {k: v[0] for k, v in parsed_qs.items()}

        # b. Extract hash field; absent → invalid.
        received_hash = parsed.get("hash")
        if not received_hash:
            return None

        # c. Build the check_string: all pairs except 'hash', sorted, joined '\n'.
        check_pairs = sorted(
            f"{k}={v}" for k, v in parsed.items() if k != "hash"
        )
        check_string = "\n".join(check_pairs)

        # d. Derive secret key using "WebAppData" as the HMAC key.
        secret_key = hmac.new(
            b"WebAppData", bot_token.encode(), hashlib.sha256
        ).digest()

        # e. Compute the expected hash.
        computed_hash = hmac.new(
            secret_key, check_string.encode(), hashlib.sha256
        ).hexdigest()

        # f. Constant-time comparison to prevent timing attacks.
        if not hmac.compare_digest(computed_hash, received_hash):
            return None

        # g. Reject data older than 5 minutes (300 s).
        auth_date_str = parsed.get("auth_date")
        if not auth_date_str:
            return None
        auth_date = int(auth_date_str)
        if time.time() - auth_date > 300:
            return None

        # h. Parse the embedded user JSON (may be absent for non-user contexts).
        user_dict = json.loads(parsed.get("user", "{}"))

        # i. Return the validated payload.
        return {"user": user_dict, "auth_date": auth_date}

    except Exception:
        return None


# ── 2. Session token — create ─────────────────────────────────────────────────

def create_session_token(
    user_id: str,
    bot_token: str,
    expiry_seconds: int = 3600,
) -> str:
    """Issue a short-lived, HMAC-signed session token.

    The token is a URL-safe base64 encoding of ``<user_id>:<expiry_ts>:<sig>``
    where ``sig`` is an HMAC-SHA256 over the payload using the bot token as
    the signing key.

    Parameters
    ----------
    user_id:
        Telegram user ID (as a string).
    bot_token:
        Secret key — use Settings.telegram_bot_token (or miniapp_secret if set).
    expiry_seconds:
        Lifetime of the token in seconds (default 3600 = 1 hour).

    Returns
    -------
    str
        URL-safe base64-encoded token string.
    """
    payload = f"{user_id}:{int(time.time()) + expiry_seconds}"
    sig = hmac.new(
        bot_token.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()
    return token


# ── 3. Session token — verify ─────────────────────────────────────────────────

def verify_session_token(token: str, bot_token: str) -> Optional[str]:
    """Verify a session token produced by :func:`create_session_token`.

    Parameters
    ----------
    token:
        The URL-safe base64 token string to verify.
    bot_token:
        The same secret used when the token was created.

    Returns
    -------
    str | None
        The ``user_id`` string if the token is valid and not expired;
        ``None`` on any failure (malformed, bad signature, expired).
    """
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        # Split from the right so the first ':' in user_id (if any) is preserved.
        parts = decoded.rsplit(":", 1)
        if len(parts) != 2:
            return None
        payload, received_sig = parts[0], parts[1]

        # Recompute signature and compare in constant time.
        computed_sig = hmac.new(
            bot_token.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(computed_sig, received_sig):
            return None

        # payload = "<user_id>:<expiry_ts>"; split once from the right.
        payload_parts = payload.rsplit(":", 1)
        if len(payload_parts) != 2:
            return None
        user_id, expiry_str = payload_parts[0], payload_parts[1]

        if time.time() > int(expiry_str):
            return None

        return user_id

    except Exception:
        return None


# ── 4. FastAPI dependency ─────────────────────────────────────────────────────

class MiniAppAuth:
    """FastAPI dependency that enforces Mini App session-token authentication.

    Usage::

        from assistant.web.miniapp_auth import MiniAppAuth
        from assistant.config import load_settings

        _settings = load_settings()
        auth = MiniAppAuth(_settings)

        @app.get("/api/miniapp/me")
        async def me(user_id: str = Depends(auth)):
            return {"user_id": user_id}
    """

    def __init__(self, settings) -> None:
        self.settings = settings

    async def __call__(
        self,
        authorization: str = Header(default=None),
    ) -> str:
        """Extract and verify the Bearer token from the Authorization header.

        Raises
        ------
        HTTPException 401
            When the header is missing, malformed, or the token is invalid/
            expired.

        Returns
        -------
        str
            The authenticated Telegram user_id.
        """
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing token")

        token = authorization.removeprefix("Bearer ").strip()

        # Prefer a dedicated miniapp_secret; fall back to the bot token.
        secret = getattr(self.settings, "miniapp_secret", "") or self._bot_token()

        user_id = verify_session_token(token, secret)
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        return user_id

    def _bot_token(self) -> str:
        """Return the Telegram bot token from settings, trying common attribute names."""
        for attr in ("telegram_bot_token", "bot_token", "telegram_token"):
            val = getattr(self.settings, attr, None)
            if val:
                return val
        return ""
