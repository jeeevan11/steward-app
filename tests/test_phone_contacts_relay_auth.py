"""config-secrets-deploy-1 regression: the contact-sync read of the relay's
GET /contacts endpoint ATTACHES the X-Cos-Token shared secret when INGEST_TOKEN is
configured, and omits it (back-compat / localhost-only) when not.

Without the header, the hardened relay answers 401 and `_fetch_relay_live()` silently
degrades to the stale on-disk contact_cache.json — so this guards the read path the
same way tests/test_relay_auth.py guards the /send,/read,/send_media write paths.

Stdlib only; urlopen is faked so we capture the outgoing Request headers WITHOUT a
socket (see tests/test_relay_auth.py for the same pattern).
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from assistant.memory import phone_contacts as pc


# A fake urlopen that records the Request it was handed and returns a minimal,
# valid /contacts response so json parsing in _fetch_relay_live succeeds.
class _FakeResp:
    def __init__(self, payload=b'{"contacts": [], "lid_jid_map": {}}'):
        self._payload = payload

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


class TestPhoneContactsRelayAuth(unittest.TestCase):
    def test_fetch_relay_live_attaches_token_when_set(self):
        cap = _Capture()
        with mock.patch.dict(os.environ, {"INGEST_TOKEN": "sekret"}, clear=False):
            with mock.patch.object(pc.urllib.request, "urlopen", cap):
                pc._fetch_relay_live()
        req = cap.last
        self.assertTrue(req.full_url.endswith("/contacts"))
        self.assertEqual(req.get_header("X-cos-token"), "sekret")  # urllib title-cases

    def test_fetch_relay_live_omits_token_when_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "INGEST_TOKEN"}
        cap = _Capture()
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(pc.urllib.request, "urlopen", cap):
                pc._fetch_relay_live()
        self.assertIsNone(cap.last.get_header("X-cos-token"))

    def test_relay_auth_headers_helper(self):
        with mock.patch.dict(os.environ, {"INGEST_TOKEN": "t0k"}, clear=False):
            self.assertEqual(pc._relay_auth_headers(), {"X-Cos-Token": "t0k"})
        env = {k: v for k, v in os.environ.items() if k != "INGEST_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(pc._relay_auth_headers(), {})


if __name__ == "__main__":
    unittest.main()
