"""Regression tests for web-security-1.

The console CSRF guard used to act ONLY when an Origin header was present. A
state-changing browser request that arrived with NO Origin (a cross-site form
auto-submit, a navigation POST, a stripped-Origin request) fell straight through to
/api/actions/{id}/approve — which performs a REAL send in live mode. These tests lock
in the hardened guard:

  * a cross-site mutation (foreign Origin) is rejected,
  * a no-Origin browser mutation that carries Sec-Fetch-Site: cross-site is rejected
    (the exact hole — and it never reaches the send seam),
  * a cross-site Referer mutation is rejected, and
  * the legitimate same-origin SPA / non-browser local tool still works.

Skipped automatically when FastAPI isn't installed (matches the rest of the suite).
"""

from __future__ import annotations

import unittest

try:
    from fastapi.testclient import TestClient
    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False

from assistant.config import Settings
from assistant.ingest.base import MailSource
from assistant.models import Thread
from assistant.storage import db
from assistant.storage import repositories as repo


class _FakeMail(MailSource):
    """Records sends so a test can assert nothing went out across the wire."""

    def __init__(self):
        self.sent = []

    def connect(self): pass
    def fetch_new_message_ids(self): return []
    def get_thread(self, mid): return Thread(id="t1", subject="s", messages=[])
    def archive(self, mid): return {}
    def apply_label(self, mid, label): return {}
    def undo(self, undo_data): pass
    def send_reply(self, **kw): self.sent.append(kw); return "sent-id"


@unittest.skipUnless(_HAS_FASTAPI, "FastAPI not installed")
class TestConsoleCsrf(unittest.TestCase):
    def _client(self, *, mode="live"):
        from assistant.web import api as webapi

        conn = db.open_db(":memory:")
        settings = Settings(openrouter_api_key="x", telegram_bot_token="x",
                            telegram_chat_id="1", gmail_address="me@x.com",
                            mode=mode, db_path=":memory:", prompts_dir="./prompts")
        mail = _FakeMail()

        def _conn():
            yield conn

        # The middleware reads the module-level _settings (not the dependency), so the
        # console_token default ("") is in force — this isolates the CSRF behaviour.
        self._orig_settings = webapi._settings
        webapi._settings = settings

        webapi.app.dependency_overrides[webapi.get_settings] = lambda: settings
        webapi.app.dependency_overrides[webapi.get_conn] = _conn
        webapi.app.dependency_overrides[webapi.get_mail] = lambda: mail
        webapi.app.dependency_overrides[webapi.get_notifier] = lambda: None
        client = TestClient(webapi.app)
        return client, conn, mail, webapi

    def tearDown(self):
        try:
            from assistant.web import api as webapi
            webapi.app.dependency_overrides.clear()
            if hasattr(self, "_orig_settings"):
                webapi._settings = self._orig_settings
        except Exception:  # noqa: BLE001
            pass

    def _make_pending(self, conn):
        return repo.create_pending(conn, idempotency_key="k1", message_id="m1",
                                   thread_id="t1", tier=2, kind="reply_draft",
                                   summary="s", draft_text="hi")

    # ── the required failure-injection test ──────────────────────────────────
    def test_no_origin_cross_site_browser_approve_is_rejected(self):
        """A no-Origin POST that a browser tags as cross-site must be blocked BEFORE the
        send seam — this is the precise bypass in web-security-1."""
        client, conn, mail, _ = self._client(mode="live")
        aid = self._make_pending(conn)

        r = client.post(f"/api/actions/{aid}/approve",
                        headers={"Sec-Fetch-Site": "cross-site"})

        self.assertEqual(r.status_code, 403)
        self.assertEqual(mail.sent, [])  # nothing went out
        # The action is untouched — it never reached mark_approved/begin_send.
        self.assertEqual(repo.get_pending(conn, aid)["status"], "PENDING")

    def test_foreign_origin_mutation_is_rejected(self):
        client, conn, mail, _ = self._client(mode="live")
        aid = self._make_pending(conn)
        r = client.post(f"/api/actions/{aid}/approve",
                        headers={"Origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(mail.sent, [])
        self.assertEqual(repo.get_pending(conn, aid)["status"], "PENDING")

    def test_foreign_referer_no_origin_is_rejected(self):
        """A cross-site browser POST that omits Origin still carries a foreign Referer."""
        client, conn, mail, _ = self._client(mode="live")
        aid = self._make_pending(conn)
        r = client.post(f"/api/actions/{aid}/skip",
                        headers={"Referer": "https://evil.example.com/page"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "PENDING")

    def test_cross_site_edit_is_rejected(self):
        client, conn, _mail, _ = self._client(mode="live")
        aid = self._make_pending(conn)
        r = client.post(f"/api/actions/{aid}/edit",
                        json={"text": "evil"},
                        headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        # The draft was not mutated by the rejected request.
        self.assertEqual(repo.get_pending(conn, aid)["draft_text"], "hi")

    # ── the legitimate paths still work (additive, not a regression) ─────────
    def test_same_origin_spa_approve_still_works(self):
        client, conn, mail, _ = self._client(mode="live")
        aid = self._make_pending(conn)
        r = client.post(
            f"/api/actions/{aid}/approve",
            headers={"Origin": "http://127.0.0.1:5173", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], "sent")
        # The authorized same-origin approve reached the send seam (the legit path is
        # NOT collateral damage of the CSRF hardening). In live mode that is one send.
        self.assertEqual(len(mail.sent), 1)
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SENT")

    def test_localhost_origin_skip_still_works(self):
        client, conn, _mail, _ = self._client(mode="dry_run")
        aid = self._make_pending(conn)
        r = client.post(f"/api/actions/{aid}/skip",
                        headers={"Origin": "http://localhost:5173"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_non_browser_local_tool_no_headers_still_works(self):
        """A local non-browser tool sends no Origin/Referer/Sec-Fetch-* headers; it is
        the documented trusted caller and must keep working (the existing console tests
        rely on this)."""
        client, conn, _mail, _ = self._client(mode="dry_run")
        aid = self._make_pending(conn)
        r = client.post(f"/api/actions/{aid}/skip")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_reads_are_never_blocked_by_csrf(self):
        client, _conn, _mail, _ = self._client(mode="live")
        # Even with a hostile Origin, a read is allowed (no state change).
        r = client.get("/api/status", headers={"Origin": "https://evil.example.com",
                                               "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
