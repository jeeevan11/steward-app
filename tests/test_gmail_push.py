"""P0a — Gmail push (Pub/Sub) helpers + localhost receiver.

Pure helpers and a real round-trip through the receiver. No Google client and no
network beyond localhost. Polling remains the always-on fallback, so these only
cover the opt-in push layer."""

from __future__ import annotations

import base64
import json
import time
import unittest
import urllib.request

from assistant.ingest import gmail_push
from assistant.storage import db, ledger


def _envelope(history_id) -> dict:
    data = base64.b64encode(
        json.dumps({"emailAddress": "me@x.com", "historyId": history_id}).encode()
    ).decode()
    return {"message": {"data": data, "messageId": "1"}, "subscription": "sub"}


class TestPushHelpers(unittest.TestCase):
    def test_parse_extracts_history_id(self):
        self.assertEqual(gmail_push.parse_pubsub_push(_envelope(98765)), "98765")

    def test_parse_handles_garbage(self):
        for bad in ({}, {"message": {}}, {"message": {"data": "!!!notbase64"}}, None, "x"):
            self.assertIsNone(gmail_push.parse_pubsub_push(bad))

    def test_watch_expiry_ms(self):
        self.assertEqual(gmail_push.watch_expiry_ms({"expiration": "1700000000000"}), 1700000000000)
        self.assertEqual(gmail_push.watch_expiry_ms({}), 0)

    def test_should_renew(self):
        now = 1_000_000_000_000
        self.assertTrue(gmail_push.should_renew(0, now))               # unknown → renew
        self.assertTrue(gmail_push.should_renew(now + 3600_000, now))  # <1d → renew
        self.assertFalse(gmail_push.should_renew(now + 5 * 86400_000, now))  # 5d → no

    def test_register_watch_targets_inbox_and_topic(self):
        captured = {}

        class _Watch:
            def watch(self, *, userId, body):
                captured["userId"] = userId
                captured["body"] = body
                return type("R", (), {"execute": lambda self: {"historyId": "5", "expiration": "9"}})()

        class _Svc:
            def users(self):
                return _Watch()

        resp = gmail_push.register_watch(_Svc(), "projects/p/topics/t")
        self.assertEqual(captured["userId"], "me")
        self.assertEqual(captured["body"]["topicName"], "projects/p/topics/t")
        self.assertIn("INBOX", captured["body"]["labelIds"])
        self.assertEqual(gmail_push.watch_expiry_ms(resp), 9)


class TestPushReceiver(unittest.TestCase):
    def test_receiver_invokes_callback_with_history_id(self):
        seen = []
        rx = gmail_push.PushReceiver(0, lambda hid: seen.append(hid))
        # bind to an ephemeral port for the test
        rx.port = _free_port()
        rx.start()
        try:
            _post(rx.port, _envelope(424242))
            _wait_for(lambda: seen)
            self.assertEqual(seen[-1], "424242")
            # A push with no decodable id still WAKES the poller (id=None).
            _post(rx.port, {"message": {}})
            _wait_for(lambda: len(seen) >= 2)
            self.assertIsNone(seen[-1])
        finally:
            rx.stop()


class TestPushDedupByLedger(unittest.TestCase):
    def test_duplicate_pushes_dedup_via_ledger(self):
        # Two pushes carry the same change; the ledger's mark_seen is the dedup gate,
        # so the same message id is only ever processed once.
        conn = db.open_db(":memory:")
        try:
            self.assertTrue(ledger.mark_seen(conn, "m-1"))   # first push → new
            self.assertFalse(ledger.mark_seen(conn, "m-1"))  # second push → ignored
        finally:
            conn.close()


# ── tiny helpers ─────────────────────────────────────────────────────────────
def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(port: int, body: dict) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def _wait_for(pred, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
