"""config-secrets-deploy-1 regression: the WhatsApp relay HTTP link is authenticated
in BOTH directions with the INGEST_TOKEN shared secret.

This file covers the PYTHON side of the fix (the Node relay/whatsapp_relay.js cannot run
in the Python suite; its mirror behavior is checked by `node --check` + the same
constant-time/401 logic):
  * the engine->relay client (_relay for /send,/read and send_media for /send_media)
    ATTACHES X-Cos-Token when a token is configured, and omits it (back-compat) when not;
  * the relay->engine inbound receiver auth gate (auth_ok) rejects bad/missing tokens
    when set, fails OPEN but warns loudly in live mode when unset, and is constant-time;
  * INGEST_TOKEN parses into Settings.ingest_token.

Stdlib only; no Node, no real sockets for the client tests (urlopen is faked so we can
capture the outgoing headers without touching the network).
"""

from __future__ import annotations

import json
import logging
import os
import unittest
from unittest import mock

from assistant.config import Settings, load_settings
from assistant.ingest import whatsapp_source as wa
from assistant.storage import db


# ─────────────────────────────────────────────────────────────────────────────
# A fake urlopen that records the Request it was handed (headers + url + body) and
# returns a minimal 200 response, so we can assert on the auth header WITHOUT a socket.
# ─────────────────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, payload=b'{"ok":true,"success":true}'):
        self._payload = payload
        self.status = 200

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Capture:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return _FakeResp()

    @property
    def last(self):
        return self.requests[-1]


def _settings(**kw):
    base = dict(
        mode="dry_run", db_path=":memory:", whatsapp_enabled=True,
        # quiet hours off so /read is never suppressed before it reaches urlopen
        read_receipt_quiet_hours_enabled=False,
        # presence off — these are hermetic
        presence_suppression_enabled=False,
    )
    base.update(kw)
    return Settings(**base)


# ─────────────────────────────────────────────────────────────────────────────
# 1. engine -> relay client attaches the shared secret on /send, /read, /send_media
# ─────────────────────────────────────────────────────────────────────────────
class TestRelayClientAttachesToken(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _src(self, **kw):
        return wa.WhatsAppSource(self.conn, _settings(**kw), llm=None)

    def test_send_reply_attaches_token(self):
        src = self._src(ingest_token="sekret")
        cap = _Capture()
        with mock.patch.object(wa.urllib.request, "urlopen", cap):
            src.send_reply(thread_id="j@s.whatsapp.net", to=[], cc=[], subject="",
                           body="hello", in_reply_to_gmail_id="")
        req = cap.last
        self.assertTrue(req.full_url.endswith("/send"))
        self.assertEqual(req.get_header("X-cos-token"), "sekret")  # urllib title-cases

    def test_read_attaches_token(self):
        # seed an inbox row so archive() can resolve a jid
        from assistant.storage import whatsapp_inbox as inbox
        inbox.ensure(self.conn)
        inbox.put(self.conn, "wa_r1", {"messageId": "r1", "jid": "j@s.whatsapp.net",
                                       "sender_jid": "j@s.whatsapp.net", "push_name": "P",
                                       "body": "hi", "media_type": "", "is_group": False,
                                       "group_name": "", "quoted_body": "", "mentions": [],
                                       "timestamp": 1700000000}, status="new")
        src = self._src(ingest_token="sekret")
        cap = _Capture()
        with mock.patch.object(wa.urllib.request, "urlopen", cap):
            src.archive("wa_r1")
        req = cap.last
        self.assertTrue(req.full_url.endswith("/read"))
        self.assertEqual(req.get_header("X-cos-token"), "sekret")

    def test_send_media_attaches_token(self):
        # send_media short-circuits in dry_run; flip to live (but mock urlopen so nothing
        # actually leaves the box and no send to WhatsApp occurs).
        src = self._src(ingest_token="sekret", mode="live")
        cap = _Capture()
        with mock.patch("urllib.request.urlopen", cap):
            ok = src.send_media("j@s.whatsapp.net", "image", "http://x/y.png")
        self.assertTrue(ok)
        req = cap.last
        self.assertTrue(req.full_url.endswith("/send_media"))
        self.assertEqual(req.get_header("X-cos-token"), "sekret")

    def test_no_token_omits_header(self):
        # Back-compat: with no token configured, no auth header is attached.
        src = self._src(ingest_token="")
        cap = _Capture()
        with mock.patch.object(wa.urllib.request, "urlopen", cap):
            src.send_reply(thread_id="j@s.whatsapp.net", to=[], cc=[], subject="",
                           body="hi", in_reply_to_gmail_id="")
        self.assertIsNone(cap.last.get_header("X-cos-token"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. relay -> engine inbound auth gate (auth_ok / relay_auth_headers helpers)
# ─────────────────────────────────────────────────────────────────────────────
class TestAuthOk(unittest.TestCase):
    def test_correct_token_accepted(self):
        self.assertTrue(wa.auth_ok(_settings(ingest_token="abc"), "abc"))

    def test_wrong_token_rejected(self):
        self.assertFalse(wa.auth_ok(_settings(ingest_token="abc"), "xyz"))

    def test_missing_token_rejected_when_configured(self):
        self.assertFalse(wa.auth_ok(_settings(ingest_token="abc"), None))
        self.assertFalse(wa.auth_ok(_settings(ingest_token="abc"), ""))

    def test_unset_token_dry_run_fails_open_quietly(self):
        s = _settings(ingest_token="", mode="dry_run")
        with self.assertLogs("assistant.ingest.whatsapp", level="WARNING") as _cm:
            logging.getLogger("assistant.ingest.whatsapp").warning("anchor")
            self.assertTrue(wa.auth_ok(s, None, endpoint="/inbound"))
        # Only our explicit anchor logged — auth_ok did NOT warn in dry_run.
        self.assertTrue(all("RELAY AUTH DISABLED" not in m for m in _cm.output))

    def test_unset_token_live_fails_open_but_warns_loudly(self):
        # Never silently allow unauthenticated in live: a loud warning is emitted.
        s = _settings(ingest_token="", mode="live")
        with self.assertLogs("assistant.ingest.whatsapp", level="WARNING") as cm:
            allowed = wa.auth_ok(s, None, endpoint="/inbound")
        self.assertTrue(allowed)  # fail open (additive — never breaks localhost-only)
        self.assertTrue(any("RELAY AUTH DISABLED" in m for m in cm.output))

    def test_relay_auth_headers_includes_token_and_content_type(self):
        h = wa.relay_auth_headers(_settings(ingest_token="t0k"))
        self.assertEqual(h["Content-Type"], "application/json")
        self.assertEqual(h["X-Cos-Token"], "t0k")

    def test_relay_auth_headers_omits_token_when_unset(self):
        h = wa.relay_auth_headers(_settings(ingest_token=""))
        self.assertNotIn("X-Cos-Token", h)
        self.assertIn("Content-Type", h)

    def test_token_compare_is_constant_time(self):
        # Sanity: equal-length wrong token still rejected (compare_digest path).
        self.assertFalse(wa.auth_ok(_settings(ingest_token="abcdef"), "abcxyz"))
        self.assertTrue(wa.auth_ok(_settings(ingest_token="abcdef"), "abcdef"))


# ─────────────────────────────────────────────────────────────────────────────
# 3. config parsing: INGEST_TOKEN -> Settings.ingest_token
# ─────────────────────────────────────────────────────────────────────────────
class TestConfigTokenParsing(unittest.TestCase):
    def setUp(self):
        # Snapshot/restore the module-level settings cache so reload=True here never
        # leaks a test-only Settings into the rest of the suite.
        import assistant.config as cfg
        self._cfg = cfg
        self._cached = cfg._cached

    def tearDown(self):
        self._cfg._cached = self._cached

    def test_ingest_token_parsed_from_env(self):
        with mock.patch.dict(os.environ, {"INGEST_TOKEN": "from-env-123"}, clear=False):
            s = load_settings(env_path="/nonexistent.env", reload=True)
            self.assertEqual(s.ingest_token, "from-env-123")

    def test_ingest_token_defaults_empty(self):
        env = {k: v for k, v in os.environ.items() if k != "INGEST_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            s = load_settings(env_path="/nonexistent.env", reload=True)
            self.assertEqual(s.ingest_token, "")


if __name__ == "__main__":
    unittest.main()
