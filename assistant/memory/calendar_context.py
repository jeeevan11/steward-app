"""Calendar context (P4a) — OPT-IN, read-only, always degrades to empty.

When CALENDAR_ENABLED is set (which adds the calendar.readonly scope), this fetches
the principal's free/busy and next few events so the classifier knows roughly how
slammed the week is and the drafter can propose real open slots. If the calendar is
disabled or unreachable, every accessor returns an empty/unavailable result — the
assistant never blocks or crashes on the calendar.

The Google client is imported lazily inside the fetch. The free-slot math and the
prompt formatting are pure and unit-tested. Results are cached for 5 minutes so we
don't hit the API on every email.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from assistant.config import Settings
from assistant.logging_setup import get_logger

log = get_logger("calendar")

_CACHE_TTL = 300  # seconds
_cache: dict = {}  # key -> (epoch, CalendarContext)


@dataclass
class CalendarContext:
    free_slots: list[tuple[datetime, datetime]] = field(default_factory=list)
    upcoming_events: list[dict] = field(default_factory=list)  # {title, start}
    busy_participant_emails: set[str] = field(default_factory=set)
    available: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested)
# ─────────────────────────────────────────────────────────────────────────────
def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def free_slots_in_window(
    busy: list[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
    *,
    min_minutes: int = 30,
) -> list[tuple[datetime, datetime]]:
    """Gaps of at least min_minutes inside [window_start, window_end] not covered by
    any busy interval. Pure datetime arithmetic."""
    if window_end <= window_start:
        return []
    min_delta = timedelta(minutes=min_minutes)
    clipped = [
        (max(s, window_start), min(e, window_end))
        for s, e in busy
        if e > window_start and s < window_end
    ]
    slots: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for s, e in _merge(clipped):
        if s - cursor >= min_delta:
            slots.append((cursor, s))
        cursor = max(cursor, e)
    if window_end - cursor >= min_delta:
        slots.append((cursor, window_end))
    return slots


def working_windows(
    start: datetime, days: int, *, start_h: int = 9, end_h: int = 18
) -> list[tuple[datetime, datetime]]:
    """Per-day working-hour windows [start_h, end_h] for `days` days from `start`,
    each clipped so it never begins in the past relative to `start`. Skips weekends."""
    out: list[tuple[datetime, datetime]] = []
    day0 = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(days):
        day = day0 + timedelta(days=i)
        if day.weekday() >= 5:  # Sat/Sun
            continue
        ws = day.replace(hour=start_h)
        we = day.replace(hour=end_h)
        ws = max(ws, start)
        if we > ws:
            out.append((ws, we))
    return out


def _fmt_slot(slot: tuple[datetime, datetime]) -> str:
    s, _ = slot
    return s.strftime("%a %-I%p").replace("AM", "am").replace("PM", "pm")


def compute_free_slots(
    busy: list[tuple[datetime, datetime]], now: datetime, lookahead_days: int = 7
) -> list[tuple[datetime, datetime]]:
    """All working-hour free slots across the lookahead window."""
    slots: list[tuple[datetime, datetime]] = []
    for ws, we in working_windows(now, lookahead_days):
        slots.extend(free_slots_in_window(busy, ws, we))
    return slots


# ─────────────────────────────────────────────────────────────────────────────
# Fetch (runtime only) + cached accessors
# ─────────────────────────────────────────────────────────────────────────────
def _tz(settings: Settings):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        return timezone.utc


def get_calendar_context(
    settings: Settings, *, lookahead_days: int = 7, now: Optional[datetime] = None
) -> CalendarContext:
    """Fetch (free/busy + next events), cached 5 min. Returns an unavailable context
    when the calendar is disabled or anything goes wrong — never raises."""
    if not settings.calendar_enabled:
        return CalendarContext(available=False)

    key = f"cal:{lookahead_days}"
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]

    try:
        ctx = _fetch(settings, lookahead_days, now)
    except Exception as exc:  # noqa: BLE001 - calendar is best-effort
        log.warning("calendar fetch failed (%s); treating as unavailable", exc)
        ctx = CalendarContext(available=False)
    _cache[key] = (time.time(), ctx)
    return ctx


def _fetch(settings: Settings, lookahead_days: int, now: Optional[datetime]) -> CalendarContext:
    from assistant.ingest import gmail_auth

    tz = _tz(settings)
    now = now or datetime.now(tz)
    end = now + timedelta(days=lookahead_days)
    service = gmail_auth.build_calendar_service(settings)

    fb = service.freebusy().query(body={
        "timeMin": now.isoformat(), "timeMax": end.isoformat(),
        "items": [{"id": "primary"}],
    }).execute()
    busy_raw = (fb.get("calendars", {}).get("primary", {}) or {}).get("busy", []) or []
    busy: list[tuple[datetime, datetime]] = []
    for b in busy_raw:
        try:
            busy.append((datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])))
        except Exception:  # noqa: BLE001
            continue

    ev_resp = service.events().list(
        calendarId="primary", timeMin=now.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=5,
    ).execute()
    upcoming: list[dict] = []
    busy_emails: set[str] = set()
    for ev in ev_resp.get("items", []) or []:
        start = (ev.get("start", {}) or {}).get("dateTime") or (ev.get("start", {}) or {}).get("date", "")
        upcoming.append({"title": ev.get("summary", "(busy)"), "start": start})
        for att in ev.get("attendees", []) or []:
            email = (att.get("email") or "").lower()
            if email:
                busy_emails.add(email)

    return CalendarContext(
        free_slots=compute_free_slots(busy, now, lookahead_days),
        upcoming_events=upcoming,
        busy_participant_emails=busy_emails,
        available=True,
    )


def prompt_note(settings: Settings) -> str:
    """One line for the classifier's standing context (empty when unavailable)."""
    ctx = get_calendar_context(settings)
    if not ctx.available:
        return ""
    nxt = _fmt_slot(ctx.free_slots[0]) if ctx.free_slots else "none found"
    return f"Calendar: {len(ctx.upcoming_events)} events this week. Next free slot: {nxt}."


def drafting_note(settings: Settings) -> str:
    """One line for the drafter (empty when unavailable)."""
    ctx = get_calendar_context(settings)
    if not ctx.available:
        return ""
    slots = ", ".join(_fmt_slot(s) for s in ctx.free_slots[:3]) or "check calendar"
    return f"Available meeting slots: {slots}."


def _clear_cache() -> None:  # test hook
    _cache.clear()
