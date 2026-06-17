"""Regression + failure-injection: `ingest-email-1` — on a Gmail historyId-expiry (404)
the resync rescanned a hardcoded `newer_than:1d` and jumped the cursor to "now", silently
dropping every inbox message older than 24h from any longer outage, with no owner notice.

Invariant: NO_SILENT_LOSS.

The fix: rescan `GMAIL_RESYNC_DAYS` (default 7), count newly-recovered ids, bump a
metric, and call `on_coverage_gap(...)` so the owner is warned that mail older than the
window may need a manual look. A benign first-run seed (no stored cursor) must NOT alarm.

Skipped if the Google client libraries aren't installed (the stdlib-only core suite).
"""

from __future__ import annotations

import importlib.util
import unittest

from assistant.config import Settings
from assistant.storage import db
from assistant.storage import ledger
from assistant.storage import repositories as repo

_HAS_GOOGLE = importlib.util.find_spec("googleapiclient") is not None


# ── a tiny fake Gmail client that mimics the chained .users().X().execute() shape ──
class _Req:
    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises

    def execute(self):
        if self._raises is not None:
            raise self._raises
        return self._result


class _History:
    def __init__(self, err):
        self._err = err

    def list(self, **kwargs):
        return _Req(raises=self._err)


class _Messages:
    def __init__(self, ids):
        self._ids = ids
        self.last_query = None

    def list(self, **kwargs):
        self.last_query = kwargs.get("q")
        return _Req(result={"messages": [{"id": i} for i in self._ids]})


class _Users:
    def __init__(self, history_err, ids, profile_hist):
        self._history = _History(history_err)
        self.messages_api = _Messages(ids)
        self._profile = profile_hist

    def history(self):
        return self._history

    def messages(self):
        return self.messages_api

    def getProfile(self, **kwargs):
        return _Req(result={"historyId": self._profile})


class _FakeGmail:
    def __init__(self, history_err, ids, profile_hist="999"):
        self._users = _Users(history_err, ids, profile_hist)

    def users(self):
        return self._users


def _http_404():
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 404
        reason = "historyId expired"

    return HttpError(_Resp(), b"{}")


@unittest.skipUnless(_HAS_GOOGLE, "google client libs not installed")
class TestGmailResync(unittest.TestCase):
    def setUp(self):
        from assistant.ingest.gmail_source import GmailSource

        self.conn = db.open_db(":memory:")
        self.settings = Settings(gmail_resync_days=7)
        self.mail = GmailSource(self.conn, self.settings)
        self.alerts: list[str] = []
        self.mail.on_coverage_gap = self.alerts.append

    def tearDown(self):
        self.conn.close()

    def test_history_gap_recovers_older_mail_and_warns_owner(self):
        # We have a cursor (we were running), then Gmail's history expired (404).
        repo.set_last_history_id(self.conn, "100")
        self.mail.service = _FakeGmail(history_err=_http_404(),
                                       ids=["old1", "old2", "old3"])

        ids = self.mail.fetch_new_message_ids()

        # All three older messages are recovered into the ledger (not dropped).
        self.assertEqual(set(ids), {"old1", "old2", "old3"})
        for mid in ids:
            self.assertIsNotNone(ledger.get(self.conn, mid))
        # The rescan reached back the configured window, not a hardcoded 1 day.
        self.assertEqual(self.mail.service.users().messages().last_query,
                         "in:inbox newer_than:7d")
        # The owner was warned (NO_SILENT_LOSS surfaces the uncertainty).
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("7 days", self.alerts[0])
        self.assertIn("3 message", self.alerts[0])
        # Observability event recorded.
        self.assertGreaterEqual(repo.count_events(self.conn, type="gmail_gap_resync"), 1)
        # Cursor advanced so the next poll is incremental again.
        self.assertEqual(repo.get_last_history_id(self.conn), "999")

    def test_first_run_seed_does_not_alarm(self):
        # No stored cursor → benign first-run seed → resync, but NO coverage-gap alert.
        self.mail.service = _FakeGmail(history_err=_http_404(), ids=["m1", "m2"])

        ids = self.mail.fetch_new_message_ids()

        self.assertEqual(set(ids), {"m1", "m2"})
        self.assertEqual(self.alerts, [])  # not a gap — owner not bothered
        self.assertEqual(repo.count_events(self.conn, type="gmail_gap_resync"), 0)

    def test_already_seen_ids_are_not_double_counted(self):
        repo.set_last_history_id(self.conn, "100")
        ledger.mark_seen(self.conn, "old1")  # pretend old1 was already handled
        self.mail.service = _FakeGmail(history_err=_http_404(),
                                       ids=["old1", "old2"])

        self.mail.fetch_new_message_ids()

        # Only the genuinely-new id counts as recovered.
        self.assertIn("1 message", self.alerts[0])


if __name__ == "__main__":
    unittest.main()
