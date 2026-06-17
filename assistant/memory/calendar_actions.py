"""calendar_actions.py -- Google Calendar read/write helpers for Steward AI.

Provides free/busy lookup, meeting-time proposals, and event creation.
Uses the same Google OAuth credentials as Gmail (gmail_token_path).

All functions fail silently: calendar is best-effort. If calendar_enabled is
False or any API call fails, the functions return [] / None rather than raising.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from assistant.config import Settings
from assistant.logging_setup import get_logger

log = get_logger("memory.calendar_actions")

# IST label used in all human-readable slot labels.
_TZ_LABEL = "IST"

# Working-hours window (local clock, not UTC -- slots are returned in naive dt).
_DAY_START_HOUR = 9
_DAY_END_HOUR = 18
_SLOT_MINUTES = 30


# ---------------------------------------------------------------------------
# 1. Service builder
# ---------------------------------------------------------------------------

def build_calendar_service(settings: Settings) -> Any | None:
    """Return an authenticated Google Calendar v3 service, or None.

    Reuses the OAuth token already on disk (same file as Gmail). If
    calendar_enabled is False, or any error occurs, returns None silently.
    """
    if not settings.calendar_enabled:
        return None
    try:
        from assistant.ingest import gmail_auth
        service = gmail_auth.build_calendar_service(settings)
        return service
    except Exception as exc:
        log.debug("build_calendar_service failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 2. Free/busy query
# ---------------------------------------------------------------------------

def get_freebusy(start_dt: datetime, end_dt: datetime, settings: Settings) -> list[dict]:
    """Return busy intervals for the primary calendar between start_dt and end_dt.

    Args:
        start_dt: Start of the query window (naive datetime, treated as UTC).
        end_dt:   End of the query window (naive datetime, treated as UTC).
        settings: App settings; calendar_enabled is checked inside build_calendar_service.

    Returns:
        List of {start: str, end: str} dicts (ISO 8601 strings from the API).
        Returns [] on any error or when calendar is disabled.
    """
    cal = build_calendar_service(settings)
    if not cal:
        return []
    try:
        body = {
            "timeMin": start_dt.isoformat() + "Z",
            "timeMax": end_dt.isoformat() + "Z",
            "items": [{"id": "primary"}],
        }
        result = cal.freebusy().query(body=body).execute()
        busy = result.get("calendars", {}).get("primary", {}).get("busy", [])
        return [{"start": b["start"], "end": b["end"]} for b in (busy or [])]
    except Exception as exc:
        log.debug("get_freebusy failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# 3. Propose meeting times
# ---------------------------------------------------------------------------

def propose_meeting_times(
    duration_minutes: int,
    count: int,
    settings: Settings,
) -> list[dict]:
    """Return up to {count} free meeting slots in the next 5 business days.

    Scans 9:00-18:00 in 30-minute steps, skipping weekends and any interval
    that overlaps a busy period from get_freebusy.

    Args:
        duration_minutes: Length of the desired meeting in minutes.
        count:            Maximum number of slots to return.
        settings:         App settings.

    Returns:
        List of {start: datetime, end: datetime, label: str} dicts.
        label uses the format "Mon Jun 16, 2:00pm - 3:00pm IST".
        Returns [] on any error.
    """
    try:
        now = datetime.utcnow()
        # Collect next 5 business days (Mon-Fri) starting from tomorrow.
        business_days: list[datetime] = []
        cursor = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        while len(business_days) < 5:
            if cursor.weekday() < 5:  # 0=Mon ... 4=Fri
                business_days.append(cursor)
            cursor += timedelta(days=1)

        if not business_days:
            return []

        window_start = business_days[0].replace(hour=_DAY_START_HOUR)
        window_end = business_days[-1].replace(hour=_DAY_END_HOUR)

        busy_slots = get_freebusy(window_start, window_end, settings)

        def _overlaps(slot_start: datetime, slot_end: datetime) -> bool:
            for b in busy_slots:
                try:
                    # API returns RFC3339 strings; strip trailing 'Z' for fromisoformat.
                    b_start_str = b["start"].rstrip("Z").split("+")[0]
                    b_end_str = b["end"].rstrip("Z").split("+")[0]
                    b_start = datetime.fromisoformat(b_start_str)
                    b_end = datetime.fromisoformat(b_end_str)
                    if slot_start < b_end and slot_end > b_start:
                        return True
                except Exception:
                    pass
            return False

        proposals: list[dict] = []
        duration = timedelta(minutes=duration_minutes)
        step = timedelta(minutes=_SLOT_MINUTES)

        for day in business_days:
            slot_start = day.replace(hour=_DAY_START_HOUR, minute=0, second=0, microsecond=0)
            day_end = day.replace(hour=_DAY_END_HOUR, minute=0, second=0, microsecond=0)
            while slot_start + duration <= day_end:
                slot_end = slot_start + duration
                if not _overlaps(slot_start, slot_end):
                    label = _format_slot_label(slot_start, slot_end)
                    proposals.append(
                        {"start": slot_start, "end": slot_end, "label": label}
                    )
                    if len(proposals) >= count:
                        return proposals
                slot_start += step

        return proposals
    except Exception as exc:
        log.debug("propose_meeting_times failed: %s", exc)
        return []


def _format_slot_label(start: datetime, end: datetime) -> str:
    """Format a slot label like 'Mon Jun 16, 2:00pm - 3:00pm IST'."""
    day_str = start.strftime("%a %b %-d")
    start_str = start.strftime("%-I:%M%p").lower()
    end_str = end.strftime("%-I:%M%p").lower()
    return f"{day_str}, {start_str} - {end_str} {_TZ_LABEL}"


# ---------------------------------------------------------------------------
# 4. Create calendar event
# ---------------------------------------------------------------------------

def create_calendar_event(
    summary: str,
    start_dt: datetime,
    end_dt: datetime,
    attendee_emails: list[str],
    settings: Settings,
) -> dict | None:
    """Create an event on the primary Google Calendar.

    Args:
        summary:         Event title.
        start_dt:        Start datetime (naive or tz-aware; sent as-is to the API).
        end_dt:          End datetime.
        attendee_emails: List of attendee email addresses; invitations are sent.
        settings:        App settings. If dry_run, no API call is made.

    Returns:
        The created event dict from the API, a stub dict in dry_run mode, or
        None on error / if calendar is disabled.
    """
    if settings.dry_run:
        return {"id": "dry_run", "summary": summary, "status": "dry_run"}

    cal = build_calendar_service(settings)
    if not cal:
        return None
    try:
        event = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Kolkata"},
            "attendees": [{"email": e} for e in attendee_emails],
            "sendUpdates": "all",
        }
        result = cal.events().insert(calendarId="primary", body=event).execute()
        return result
    except Exception as exc:
        log.debug("create_calendar_event failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 5. Format proposals for chat
# ---------------------------------------------------------------------------

def format_time_proposals_chat(slots: list[dict]) -> str:
    """Render up to 3 time proposals as a chat message for Telegram.

    Args:
        slots: List of slot dicts (as returned by propose_meeting_times).

    Returns:
        A multi-line string with numbered proposals and a reply prompt.
    """
    lines = ["Proposed times:"]
    for i, slot in enumerate(slots[:3], 1):
        lines.append(f"{i}. {slot['label']}")
    lines.append("(Reply with 1, 2 or 3 to confirm)")
    return "\n".join(lines)
