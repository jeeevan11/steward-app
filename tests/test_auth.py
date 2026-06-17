"""Phase 11b — opt-in localhost auth. Empty token = current behavior (no enforcement);
a set token requires X-Cos-Token on web writes and on the WhatsApp ingest receiver."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from assistant.config import Settings

try:
    from fastapi.testclient import TestClient
    _HAVE_TC = True
except Exception:  # noqa: BLE001
    _HAVE_TC = False


@unittest.skipUnless(_HAVE_TC, "fastapi TestClient not installed")
class TestConsoleAuth(unittest.TestCase):
    def _client(self, token):
        from assistant.web import api as webapi
        self._orig = webapi._settings
        webapi._settings = Settings(console_token=token)
        return webapi, TestClient(webapi.app)

    def tearDown(self):
        from assistant.web import api as webapi
        if hasattr(self, "_orig"):
            webapi._settings = self._orig

    def test_write_blocked_without_token(self):
        _, client = self._client("secret")
        r = client.post("/api/actions/999999/skip")
        self.assertEqual(r.status_code, 401)

    def test_write_passes_middleware_with_token(self):
        _, client = self._client("secret")
        r = client.post("/api/actions/999999/skip", headers={"X-Cos-Token": "secret"})
        self.assertNotEqual(r.status_code, 401)   # past auth (may 4xx/5xx downstream)

    def test_no_token_means_no_enforcement(self):
        _, client = self._client("")
        r = client.post("/api/actions/999999/skip")
        self.assertNotEqual(r.status_code, 401)

    def test_reads_never_blocked(self):
        _, client = self._client("secret")
        self.assertNotEqual(client.get("/api/status").status_code, 401)


class TestIngestReceiverAuth(unittest.TestCase):
    """Round-trip the real WhatsApp inbound receiver with a token configured."""

    def _serve(self, token):
        from assistant.ingest.whatsapp_source import _InboundHandler
        self.dbfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _InboundHandler)
        srv.cos_settings = Settings(db_path=self.dbfile.name, ingest_token=token, whatsapp_enabled=True)
        srv.cos_db_path = self.dbfile.name
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, srv.server_address[1]

    def _post(self, port, headers):
        body = json.dumps({"messageId": "x1", "jid": "j@s.whatsapp.net", "body": "hi"}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/inbound", data=body,
                                     headers={"Content-Type": "application/json", **headers},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_rejects_without_token(self):
        srv, port = self._serve("sekret")
        try:
            self.assertEqual(self._post(port, {}), 401)
            self.assertEqual(self._post(port, {"X-Cos-Token": "sekret"}), 200)
        finally:
            srv.shutdown()

    def test_no_token_accepts(self):
        srv, port = self._serve("")
        try:
            self.assertEqual(self._post(port, {}), 200)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
