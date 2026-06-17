"""Phase 12 — health checks + diagnostics export.

The central guarantee under test: the diagnostics bundle is SAFE TO SHARE. Even when
Settings carry fake secrets, no secret substring may appear in collect()'s output or
in the exported file. We also assert health_check never raises and returns sane
values, secrets_present returns booleans only, and format_health returns text.

Stdlib + in-memory SQLite + temp paths only — never touches the live DB or services.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from assistant import diagnostics
from assistant.config import Settings
from assistant.storage import db


# A fake secret we plant in Settings; the bundle must never contain this literal.
FAKE_KEY = "sk-SECRET123456789abcdef"
FAKE_TG_TOKEN = "1234567890:AAFAKETELEGRAMTOKENvalue0987654321xyz"


def _settings(**over) -> Settings:
    base = dict(
        openrouter_api_key=FAKE_KEY,
        telegram_bot_token=FAKE_TG_TOKEN,
        telegram_chat_id="999",
        mode="dry_run",
        db_path=":memory:",
        log_path="./data/assistant.log",
        relay_status_path="relay/status.json",
    )
    base.update(over)
    return Settings(**base)


def _seed_ledger(conn) -> None:
    """A few processed_messages rows in mixed states + a pending action."""
    now = int(time.time())
    rows = [
        ("m1", "DONE", now - 100),
        ("m2", "DONE", now - 200),
        ("m3", "FAILED", now - 300),
        ("m4", "SEEN", now - 5000),       # an old unprocessed one
        ("m5", "PROCESSING", now - 50),
    ]
    for mid, state, updated in rows:
        conn.execute(
            "INSERT INTO processed_messages (message_id, state, created_at, updated_at) "
            "VALUES (?,?,?,?)",
            (mid, state, updated, updated),
        )
    conn.execute(
        "INSERT INTO pending_actions (idempotency_key, message_id, tier, kind, "
        "summary, status) VALUES (?,?,?,?,?,?)",
        ("k1", "m1", 2, "ask", "please confirm", "PENDING"),
    )


class TestHealthCheck(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        _seed_ledger(self.conn)
        self.settings = _settings()

    def tearDown(self):
        self.conn.close()

    def test_returns_all_keys_and_never_raises(self):
        h = diagnostics.health_check(self.conn, self.settings)
        for key in (
            "engine_heartbeat_fresh", "relay_connected", "db_ok", "db_size_bytes",
            "pending_count", "last_24h_counts", "ledger_failed_count",
            "email_enabled", "whatsapp_enabled", "mode", "dry_run",
            "oldest_unprocessed_age_seconds",
        ):
            self.assertIn(key, h)

    def test_counts_are_sane(self):
        h = diagnostics.health_check(self.conn, self.settings)
        self.assertTrue(h["db_ok"])
        self.assertEqual(h["pending_count"], 1)
        self.assertEqual(h["ledger_failed_count"], 1)
        # m4 is ~5000s old and still SEEN.
        self.assertIsInstance(h["oldest_unprocessed_age_seconds"], (int, float))
        self.assertGreater(h["oldest_unprocessed_age_seconds"], 1000)
        self.assertEqual(h["dry_run"], True)
        self.assertEqual(h["mode"], "dry_run")

    def test_missing_files_degrade_not_raise(self):
        # Point status/relay paths at non-existent files; should be False/unknown.
        s = _settings(relay_status_path="/nonexistent/relay.json",
                      db_path="/nonexistent/dir/assistant.db")
        h = diagnostics.health_check(self.conn, s)
        self.assertFalse(h["engine_heartbeat_fresh"])
        self.assertFalse(h["relay_connected"])
        # db_ok is still True (the conn is live) but size is unknown (path missing).
        self.assertEqual(h["db_size_bytes"], "unknown")

    def test_relay_connected_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            relay = Path(d) / "status.json"
            relay.write_text(json.dumps(
                {"connected": True, "updated_at": now - 10, "messages_today": 3}),
                encoding="utf-8")
            s = _settings(relay_status_path=str(relay))
            h = diagnostics.health_check(self.conn, s, now=now)
            self.assertTrue(h["relay_connected"])

    def test_heartbeat_fresh_from_status_json(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            (Path(d) / "status.json").write_text(
                json.dumps({"heartbeat_ts": now - 5}), encoding="utf-8")
            s = _settings(db_path=str(Path(d) / "assistant.db"))
            h = diagnostics.health_check(self.conn, s, now=now)
            self.assertTrue(h["engine_heartbeat_fresh"])


class TestSecretsPresent(unittest.TestCase):
    def test_returns_booleans_only(self):
        s = _settings()
        out = diagnostics.secrets_present(s)
        self.assertTrue(len(out) > 0)
        for k, v in out.items():
            self.assertIsInstance(v, bool, f"{k} must be a bool, got {type(v)}")
        # The actual values must NOT appear anywhere in the output.
        blob = json.dumps(out)
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn(FAKE_TG_TOKEN, blob)

    def test_reflects_configuration(self):
        present = diagnostics.secrets_present(_settings())
        self.assertTrue(present["openrouter_key_set"])
        self.assertTrue(present["telegram_token_set"])

        absent = diagnostics.secrets_present(
            _settings(openrouter_api_key="", telegram_bot_token=""))
        self.assertFalse(absent["openrouter_key_set"])
        self.assertFalse(absent["telegram_token_set"])


class TestCollectRedaction(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        _seed_ledger(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_collect_has_no_raw_secret(self):
        s = _settings()
        bundle = diagnostics.collect(self.conn, s)
        blob = json.dumps(bundle, default=str)
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn(FAKE_TG_TOKEN, blob)
        # Config still records that the secret IS set, just redacted.
        self.assertEqual(bundle["config"]["openrouter_api_key"], "***set***")
        self.assertEqual(bundle["config"]["telegram_bot_token"], "***set***")

    def test_collect_structure(self):
        bundle = diagnostics.collect(self.conn, _settings())
        for key in ("app", "health", "secrets_present", "config",
                    "table_counts", "log_tail"):
            self.assertIn(key, bundle)
        self.assertEqual(bundle["table_counts"]["processed_messages"], 5)
        self.assertEqual(bundle["table_counts"]["pending_actions"], 1)

    def test_log_tail_is_scrubbed(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "assistant.log"
            log.write_text(
                "INFO starting\n"
                f"DEBUG using api_key={FAKE_KEY}\n"
                f"DEBUG token {FAKE_TG_TOKEN}\n"
                "INFO ready\n",
                encoding="utf-8")
            s = _settings(log_path=str(log))
            bundle = diagnostics.collect(self.conn, s)
            blob = json.dumps(bundle["log_tail"])
            self.assertNotIn(FAKE_KEY, blob)
            self.assertNotIn(FAKE_TG_TOKEN, blob)
            self.assertTrue(any("starting" in ln for ln in bundle["log_tail"]))


class TestExport(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        _seed_ledger(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_export_writes_file_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "diag.json"
            s = _settings()
            path = diagnostics.export(self.conn, s, out_path=str(out), stamp="test")
            self.assertEqual(path, str(out))
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn(FAKE_TG_TOKEN, text)
            # It's valid, pretty JSON.
            parsed = json.loads(text)
            self.assertIn("health", parsed)

    def test_export_default_path_uses_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            s = _settings(db_path=str(Path(d) / "assistant.db"))
            path = diagnostics.export(self.conn, s, stamp="abc/123")
            # Stamp is sanitized into the filename and the file exists.
            self.assertTrue(Path(path).exists())
            self.assertIn("diagnostics-abc_123.json", path)

    def test_export_scrubs_leaked_literal(self):
        # Even if a secret somehow lands in the log tail, export must scrub it.
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "assistant.log"
            log.write_text(f"oops leaked {FAKE_KEY} into the log\n", encoding="utf-8")
            out = Path(d) / "diag.json"
            s = _settings(log_path=str(log))
            diagnostics.export(self.conn, s, out_path=str(out), stamp="t")
            self.assertNotIn(FAKE_KEY, out.read_text(encoding="utf-8"))


class TestFormatHealth(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        _seed_ledger(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_returns_text(self):
        h = diagnostics.health_check(self.conn, _settings())
        out = diagnostics.format_health(h)
        self.assertIsInstance(out, str)
        self.assertIn("health check", out.lower())
        self.assertIn("database", out.lower())

    def test_handles_garbage_input(self):
        self.assertIsInstance(diagnostics.format_health({}), str)
        self.assertIsInstance(diagnostics.format_health(None), str)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
