"""FastAPI endpoint tests. Skipped automatically if FastAPI isn't installed, so the
rest of the suite still runs with bare stdlib.

The required guard test: a CONSOLE-initiated approve on an already-SENT action must
return "already handled" and must NOT double-send — reusing the exact mark_approved /
begin_send guards.
"""

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


class FakeMail(MailSource):
    """Records sends so tests can assert nothing went out."""
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
class TestWebEndpoints(unittest.TestCase):
    def _client(self, *, mode="dry_run"):
        from assistant.web import api as webapi

        conn = db.open_db(":memory:")
        settings = Settings(openrouter_api_key="x", telegram_bot_token="x",
                            telegram_chat_id="1", gmail_address="me@x.com",
                            mode=mode, db_path=":memory:", prompts_dir="./prompts")
        mail = FakeMail()

        def _conn():
            yield conn  # shared, not closed between requests in the test

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
        except Exception:  # noqa: BLE001
            pass

    def test_status_and_queue(self):
        client, conn, _mail, _ = self._client()
        r = client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode_label"], "DRY-RUN")
        self.assertEqual(client.get("/api/queue").json(), {"items": []})

    def test_console_cannot_double_send_an_already_sent_action(self):
        """The required guard test."""
        client, conn, mail, _ = self._client(mode="live")
        aid = repo.create_pending(conn, idempotency_key="k1", message_id="m1", thread_id="t1",
                                  tier=2, kind="reply_draft", summary="s", draft_text="hi")
        repo.mark_approved(conn, aid)
        repo.begin_send(conn, aid)
        repo.mark_sent(conn, aid, "already-gone")   # action is now terminal: SENT

        r = client.post(f"/api/actions/{aid}/approve")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], "already")   # not "sent"
        self.assertEqual(mail.sent, [])                    # nothing went out
        self.assertEqual(repo.get_pending(conn, aid)["status"], "SENT")

    def test_dry_run_approve_sends_nothing_to_gmail(self):
        client, conn, mail, _ = self._client(mode="dry_run")
        aid = repo.create_pending(conn, idempotency_key="k2", message_id="m2", thread_id="t1",
                                  tier=2, kind="reply_draft", summary="s", draft_text="hi")
        r = client.post(f"/api/actions/{aid}/approve")
        body = r.json()
        self.assertEqual(body["result"], "sent")
        self.assertTrue(body["dry_run"])
        self.assertEqual(mail.sent, [])  # dry-run never touches Gmail
        self.assertEqual(repo.get_pending(conn, aid)["sent_gmail_id"], "DRYRUN")

    def test_skip_is_guarded(self):
        client, conn, _mail, _ = self._client()
        aid = repo.create_pending(conn, idempotency_key="k3", message_id="m3", thread_id="t1",
                                  tier=2, kind="reply_draft", summary="s", draft_text="hi")
        self.assertTrue(client.post(f"/api/actions/{aid}/skip").json()["ok"])
        # second skip on the now-terminal row is refused
        self.assertFalse(client.post(f"/api/actions/{aid}/skip").json()["ok"])

    # ── P6 dashboard endpoints ──
    def test_new_read_endpoints_shape(self):
        client, _conn, _mail, _ = self._client()
        for path in ("/api/pipeline/status", "/api/commitments", "/api/voice-profiles",
                     "/api/rules/proposed", "/api/audit-log", "/api/metrics/daily",
                     "/api/metrics/accuracy", "/api/metrics/costs",
                     "/api/metrics/response-times"):
            r = client.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_test_pipeline_has_zero_side_effects(self):
        client, conn, _mail, _ = self._client()
        before = conn.execute("SELECT COUNT(*) AS n FROM processed_messages").fetchone()["n"]

        class FakeLLM:
            def noise_pass(self, **kw):
                import json
                return json.dumps({"is_noise": False, "confidence": 0.0, "label": "", "reason": ""})

            def classify(self, **kw):
                import json
                return json.dumps({
                    "category": "work_request", "intent": "asks", "sender_importance": 10,
                    "stakes": "low", "reversibility": "reversible", "proposed_tier": 1,
                    "confidence": 0.9, "needs_reply": True, "reasoning": "r",
                    "suggested_action": "reply", "one_line_summary": "x",
                })

        from assistant.web import api as webapi
        webapi.app.dependency_overrides[webapi.get_llm] = lambda: FakeLLM()
        r = client.post("/api/test-pipeline", json={"sender": "a@x.com", "subject": "hi", "email_text": "yo"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("final_tier", r.json())
        after = conn.execute("SELECT COUNT(*) AS n FROM processed_messages").fetchone()["n"]
        self.assertEqual(before, after)  # nothing written to the real DB

    def test_commitment_and_rule_writes(self):
        client, conn, _mail, _ = self._client()
        from assistant.memory import commitments
        cid = commitments.add_commitment(conn, message_id="m", contact_email="a@x.com",
                                          commitment_text="ship", due_date="")
        self.assertTrue(client.post(f"/api/commitments/{cid}/done").json()["ok"])
        self.assertEqual(commitments.get_commitment(conn, cid)["status"], "done")

        rid = repo.add_proposed_rule(conn, rule_text="never ping me about newsletters")
        self.assertTrue(client.post(f"/api/rules/{rid}/confirm").json()["ok"])
        # confirming a learned rule promotes it to an active rule
        actives = [r for r in repo.list_rules(conn, status="active")]
        self.assertTrue(any("newsletters" in r["instruction"] for r in actives))

    def test_contact_update(self):
        client, conn, _mail, _ = self._client()
        r = client.post("/api/contacts/vip@x.com/update", json={"importance": 90, "flags": ["vip"]})
        body = r.json()
        self.assertEqual(body["importance"], 90)
        self.assertIn("vip", body["flags"])


if __name__ == "__main__":
    unittest.main()
