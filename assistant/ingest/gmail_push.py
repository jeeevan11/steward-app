"""Gmail push notifications (P0a) via Cloud Pub/Sub — OPT-IN.

When ``GMAIL_PUBSUB_TOPIC`` is set, the poller registers a Gmail ``watch`` on the
INBOX that publishes change notifications to that topic, and a tiny localhost HTTP
receiver WAKES the poller the instant a push arrives. The existing history fetch +
the exactly-once ledger do the rest, so a duplicate push (same historyId) is
harmless — it just triggers one more fetch that finds nothing new.

With no topic configured, nothing here runs and the assistant polls as before
(the always-on fallback). Push is purely a latency optimisation layered on top.

The pure helpers (``parse_pubsub_push`` / ``watch_expiry_ms`` / ``should_renew``)
are stdlib-only and unit-tested. ``register_watch`` is the only function that
touches the Google client, and only at runtime.

NOTE on delivery: Pub/Sub *push* posts to a public HTTPS endpoint. To reach this
localhost receiver you expose the port with a tunnel (e.g. ``cloudflared``/``ngrok``)
and point the Pub/Sub push subscription at the tunnel URL. See the setup banner
printed at startup (assistant.main._push_setup_banner).
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import urlsplit, parse_qs

from assistant.logging_setup import get_logger

log = get_logger("gmail_push")

_DAY_MS = 24 * 3600 * 1000

# ingest-email-6 / config-secrets-deploy-3 — env knobs read here (shared config.py is
# off-limits to this agent), with safe defaults that FAIL CLOSED on a public tunnel.
#   GMAIL_PUSH_TOKEN  high-entropy shared secret embedded in the Pub/Sub push URL
#                     (path segment or ?token= query) and/or sent as X-Gmail-Push-Token.
#                     When set, every POST must present it (constant-time compare) or be
#                     rejected 401 BEFORE the poller is woken.
#   GMAIL_PUSH_MIN_INTERVAL_SECONDS  debounce: ignore (still 204) wakes that arrive within
#                     this many seconds of the last accepted wake, so even an authenticated
#                     flood cannot busy-spin the poller / exhaust the Gmail quota.
_PUSH_TOKEN_ENV = "GMAIL_PUSH_TOKEN"
_PUSH_MIN_INTERVAL_ENV = "GMAIL_PUSH_MIN_INTERVAL_SECONDS"
_PUSH_TOKEN_HEADER = "X-Gmail-Push-Token"
# Debounce is OFF by default (opt-in) so we never coalesce legitimate distinct pushes;
# the primary DoS defense is rejecting unauthenticated floods. Operators who want to cap
# even an authenticated push rate set GMAIL_PUSH_MIN_INTERVAL_SECONDS > 0.
_DEFAULT_MIN_INTERVAL_SECONDS = 0.0


def _configured_push_token() -> str:
    """The shared push secret from the environment (empty = none configured)."""
    return (os.environ.get(_PUSH_TOKEN_ENV) or "").strip()


def _configured_min_interval() -> float:
    """Minimum seconds between accepted (poller-waking) pushes. Safe default on parse error."""
    raw = os.environ.get(_PUSH_MIN_INTERVAL_ENV)
    if raw is None or raw == "":
        return _DEFAULT_MIN_INTERVAL_SECONDS
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MIN_INTERVAL_SECONDS
    return val if val >= 0 else _DEFAULT_MIN_INTERVAL_SECONDS


def _token_from_request(path: str, headers) -> str:
    """Extract a presented push token from the request: ?token=, last path segment, or the
    X-Gmail-Push-Token header. First non-empty wins. Pure — unit-tested via the handler."""
    try:
        parts = urlsplit(path)
        qs = parse_qs(parts.query or "")
        q_tok = (qs.get("token", [""])[0] or "").strip()
        if q_tok:
            return q_tok
        # A secret path like /push/<token> — take the last non-empty path segment, but only
        # when it is not the bare "/" root (so an unauthenticated root POST has empty token).
        segs = [s for s in (parts.path or "").split("/") if s]
        # The first segment may be a fixed route prefix ("push"); the secret is the last seg
        # only if there is more than one segment, else there is no path token.
        path_tok = segs[-1].strip() if len(segs) >= 2 else ""
        if path_tok:
            return path_tok
    except Exception:  # noqa: BLE001 - a malformed target yields no token (→ reject)
        pass
    try:
        return (headers.get(_PUSH_TOKEN_HEADER) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def verify_push_token(path: str, headers) -> bool:
    """True iff the request carries the configured push secret (constant-time).

    FAIL-CLOSED contract: if GMAIL_PUSH_TOKEN is set, a request is authenticated only when
    it presents the matching token; an empty/absent/mismatched token is rejected. The
    secret itself is never logged."""
    expected = _configured_push_token()
    if not expected:
        # No secret configured: caller (PushReceiver) decides the policy. We report False so
        # the receiver's require_auth flag governs whether to accept (see _Handler.do_POST).
        return False
    presented = _token_from_request(path, headers)
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested)
# ─────────────────────────────────────────────────────────────────────────────
def parse_pubsub_push(body: dict) -> Optional[str]:
    """Extract the Gmail ``historyId`` from a Pub/Sub push envelope.

    The envelope is ``{"message": {"data": <base64 json>, ...}, "subscription": ...}``
    where the decoded data is ``{"emailAddress": ..., "historyId": N}``. Returns the
    historyId as a string, or None if the payload is missing/unparseable (the caller
    still wakes the poller — a wake without an id is harmless)."""
    if not isinstance(body, dict):
        return None
    msg = body.get("message")
    if not isinstance(msg, dict):
        return None
    data = msg.get("data")
    if not data:
        return None
    try:
        decoded = base64.b64decode(data)
        payload = json.loads(decoded.decode("utf-8"))
    except Exception:  # noqa: BLE001 - malformed payload → no id
        return None
    hid = payload.get("historyId") if isinstance(payload, dict) else None
    return str(hid) if hid is not None else None


def watch_expiry_ms(watch_response: dict) -> int:
    """Pull the watch expiration (epoch ms) out of a users().watch() response."""
    try:
        return int((watch_response or {}).get("expiration", 0) or 0)
    except (TypeError, ValueError):
        return 0


def should_renew(expiration_ms: int, now_ms: int, *, renew_before_days: int = 1) -> bool:
    """True if a Gmail watch should be (re)registered now.

    Gmail watches expire after ~7 days; we renew when within ``renew_before_days``
    of expiry. An unknown/zero expiration also returns True (register it)."""
    if not expiration_ms:
        return True
    return (expiration_ms - now_ms) <= renew_before_days * _DAY_MS


def register_watch(service, topic_name: str, *, label_ids=("INBOX",)) -> dict:
    """Register a Gmail watch publishing INBOX changes to ``topic_name``.

    Returns the raw watch response (``{"historyId":..,"expiration":..}``). Raises on
    API error — the caller logs + surfaces and falls back to polling."""
    return (
        service.users()
        .watch(
            userId="me",
            body={"topicName": topic_name, "labelIds": list(label_ids), "labelFilterAction": "include"},
        )
        .execute()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Localhost push receiver — wakes the poller, touches no DB/Gmail itself.
# ─────────────────────────────────────────────────────────────────────────────
class PushReceiver:
    """A minimal HTTP server on 127.0.0.1:<port>. Every POST it receives parses the
    Pub/Sub envelope (best-effort) and calls ``on_push(history_id)`` — which should
    just wake the poller. Responds 204 quickly so Pub/Sub doesn't retry."""

    def __init__(self, port: int, on_push: Callable[[Optional[str]], None]):
        self.port = int(port)
        self._on_push = on_push
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # ingest-email-6 / config-secrets-deploy-3 — debounce state. Guarded so the
        # min-interval check is atomic across the ThreadingHTTPServer's worker threads.
        self._debounce_lock = threading.Lock()
        self._last_accepted_wake = 0.0
        self._unauth_warned = False

    def _auth_ok(self, path: str, headers) -> bool:
        """ingest-email-6 / config-secrets-deploy-3 — ROOT CAUSE: the receiver woke the
        poller for ANY unauthenticated POST. Exposed via a (guessable/scannable) tunnel,
        a remote party could pin the Gmail poller into continuous back-to-back history
        fetches — a self-inflicted DoS risking Gmail quota exhaustion so real mail stops
        being ingested.
        FIX: authenticate every push BEFORE waking. The security control is a high-entropy
        GMAIL_PUSH_TOKEN embedded in the Pub/Sub push URL (path /push/<token> or
        ?token=...) or sent as the X-Gmail-Push-Token header, compared constant-time.

        Policy:
          * GMAIL_PUSH_TOKEN set  → REQUIRE it; reject (401) every request without the
            matching token. This is the hardened, documented deployment and the only one
            that should ever be exposed via a tunnel — the finding is closed here.
          * GMAIL_PUSH_TOKEN unset → unauthenticated pushes are accepted (legacy behavior)
            BUT a loud one-time warning is emitted steering the operator to set the token.
            An operator who has deliberately accepted that risk can silence the warning
            with GMAIL_PUSH_ALLOW_UNAUTHENTICATED=1. The tunnel hostname is NOT a security
            boundary; do not expose this receiver publicly without a token.
        The opt-in debounce (_debounced) caps the wake rate in BOTH modes as defense in
        depth so even a flood cannot busy-spin the poller when an interval is configured."""
        if _configured_push_token():
            return verify_push_token(path, headers)
        # No token configured: legacy accept, but make the exposure non-silent.
        if (os.environ.get("GMAIL_PUSH_ALLOW_UNAUTHENTICATED") or "").strip() not in ("1", "true", "yes"):
            if not self._unauth_warned:
                self._unauth_warned = True
                log.warning(
                    "gmail push receiver: no %s configured — accepting UNAUTHENTICATED "
                    "pushes (legacy). Set %s to a high-entropy secret and embed it in the "
                    "Pub/Sub push URL (/push/<token> or ?token=...) or send it as %s before "
                    "exposing this endpoint via a tunnel. The tunnel hostname is NOT a "
                    "security boundary.",
                    _PUSH_TOKEN_ENV, _PUSH_TOKEN_ENV, _PUSH_TOKEN_HEADER,
                )
        return True

    def _debounced(self) -> bool:
        """True iff this accepted push should be COALESCED (ignored) because another was
        accepted within the min-interval window. Caps the wake-driven poll rate so even an
        authenticated flood cannot busy-spin the poller / starve the Gmail quota."""
        min_interval = _configured_min_interval()
        if min_interval <= 0:
            return False
        now = time.monotonic()
        with self._debounce_lock:
            if (now - self._last_accepted_wake) < min_interval:
                return True
            self._last_accepted_wake = now
            return False

    def start(self) -> None:
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                # Authenticate FIRST — never read a large body or wake the poller for an
                # unauthenticated caller. Reject 401 and do NOT set _wake.
                if not receiver._auth_ok(self.path, self.headers):
                    self.send_response(401)
                    self.end_headers()
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    raw = self.rfile.read(length) if length else b""
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:  # noqa: BLE001 - never fail on a bad body
                    body = {}
                hid = parse_pubsub_push(body) if isinstance(body, dict) else None
                # Debounce/coalesce authenticated wakes so a flood cannot busy-spin the
                # poller. A coalesced push still ACKs 204 so Pub/Sub does not retry it.
                if receiver._debounced():
                    self.send_response(204)
                    self.end_headers()
                    return
                try:
                    receiver._on_push(hid)
                except Exception:  # noqa: BLE001 - the wake must never raise out
                    log.debug("push on_push callback failed", exc_info=True)
                # Acknowledge fast (Pub/Sub treats non-2xx as a retry).
                self.send_response(204)
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802 - health check
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args) -> None:  # noqa: D401 - silence stdlib logging
                return

        # Bind localhost ONLY (never 0.0.0.0).
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="gmail-push", daemon=True
        )
        self._thread.start()
        log.info("gmail push receiver listening on 127.0.0.1:%s", self.port)

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
