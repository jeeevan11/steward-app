"""Layer 1E — the feedback-derived 'deprioritize' signal computed from learning_events.
Repeated skips with ~no approvals → quietly surface less. VIP is exempt."""

from __future__ import annotations

import unittest

from assistant.config import Settings
from assistant.main import _feedback_deprioritized
from assistant.models import Contact
from assistant.storage import db
from assistant.storage import repositories as repo


class TestFeedbackDeprioritized(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.s = Settings(feedback_tuning_enabled=True, feedback_skip_threshold=3)

    def tearDown(self):
        self.conn.close()

    def _skip(self, email, n):
        for _ in range(n):
            repo.record_event(self.conn, type="skip", contact_email=email)

    def test_repeated_skips_deprioritize(self):
        c = Contact(email="noisy@x.com")
        self._skip("noisy@x.com", 4)
        self.assertTrue(_feedback_deprioritized(self.conn, self.s, c))

    def test_below_threshold_does_not(self):
        c = Contact(email="ok@x.com")
        self._skip("ok@x.com", 2)
        self.assertFalse(_feedback_deprioritized(self.conn, self.s, c))

    def test_approvals_offset_skips(self):
        c = Contact(email="mixed@x.com")
        self._skip("mixed@x.com", 4)
        for _ in range(4):
            repo.record_event(self.conn, type="approve", contact_email="mixed@x.com")
        self.assertFalse(_feedback_deprioritized(self.conn, self.s, c))

    def test_vip_is_exempt(self):
        c = Contact(email="vip@x.com", flags={"vip"})
        self._skip("vip@x.com", 9)
        self.assertFalse(_feedback_deprioritized(self.conn, self.s, c))

    def test_disabled(self):
        c = Contact(email="noisy@x.com")
        self._skip("noisy@x.com", 9)
        self.assertFalse(_feedback_deprioritized(self.conn, Settings(feedback_tuning_enabled=False), c))


if __name__ == "__main__":
    unittest.main()
