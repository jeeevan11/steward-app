"""Unit tests for assistant/memory/calendar_actions.py.

Uses stdlib only (unittest + unittest.mock). No network, no Google API calls.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from assistant.memory import calendar_actions as CA


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSettings:
    def __init__(self, calendar_enabled: bool = True, dry_run: bool = False):
        self.calendar_enabled = calendar_enabled
        self.dry_run = dry_run


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestFormatTimeProposalsChat(unittest.TestCase):
    """format_time_proposals_chat must include all 3 labels and the reply instruction."""

    def test_format_time_proposals_chat(self):
        slots = [
            {"start": datetime(2026, 6, 17, 9, 0), "end": datetime(2026, 6, 17, 10, 0),
             "label": "Tue Jun 17, 9:00am - 10:00am IST"},
            {"start": datetime(2026, 6, 17, 11, 0), "end": datetime(2026, 6, 17, 12, 0),
             "label": "Tue Jun 17, 11:00am - 12:00pm IST"},
            {"start": datetime(2026, 6, 18, 14, 0), "end": datetime(2026, 6, 18, 15, 0),
             "label": "Wed Jun 18, 2:00pm - 3:00pm IST"},
        ]

        result = CA.format_time_proposals_chat(slots)

        self.assertIn("Tue Jun 17, 9:00am - 10:00am IST", result)
        self.assertIn("Tue Jun 17, 11:00am - 12:00pm IST", result)
        self.assertIn("Wed Jun 18, 2:00pm - 3:00pm IST", result)
        # Reply instruction must be present
        self.assertIn("Reply", result)


class TestProposeMeetingTimesNoService(unittest.TestCase):
    """propose_meeting_times must return [] when the calendar service is unavailable.

    build_calendar_service returning None causes get_freebusy to return [] (no
    busy slots), so we simulate a total service outage by patching get_freebusy
    to raise, which triggers the outer except block and returns [].
    """

    def test_propose_meeting_times_no_service(self):
        settings = FakeSettings(calendar_enabled=True)

        with patch.object(CA, "build_calendar_service", return_value=None):
            with patch.object(CA, "get_freebusy", side_effect=RuntimeError("no service")):
                result = CA.propose_meeting_times(
                    duration_minutes=60,
                    count=3,
                    settings=settings,
                )

        self.assertEqual(result, [])


class TestBuildCalendarServiceDisabled(unittest.TestCase):
    """build_calendar_service must return None when calendar_enabled is False."""

    def test_build_calendar_service_disabled(self):
        settings = FakeSettings(calendar_enabled=False)
        result = CA.build_calendar_service(settings)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
