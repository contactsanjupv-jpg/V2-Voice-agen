"""
Structured business hours: the ONE place that decides "is this business open at this instant?".

Shape (stored in businesses.hours_schedule):
    {"enabled": true,
     "weekly": {"mon": [["09:00", "12:00"], ["13:00", "17:00"]], "tue": [...], ...},   # missing day = closed
     "exceptions": {"2026-12-25": [], "2026-12-24": [["09:00", "12:00"]]}}              # a date replaces that weekday

Rules:
- Times are the business's wall clock in its IANA timezone (services/business_time.py), so daylight saving is handled
  by the tz database. A window is [start, end): open at start, closed at end. "24:00" is allowed as an end. A window
  that crosses midnight is written as two windows (22:00-24:00 today, 00:00-02:00 tomorrow).
- No holiday library: closed dates and special hours are entered by the owner as exceptions.
- evaluate() never raises and never guesses: anything it cannot positively evaluate is "unknown", and callers
  treat "unknown" exactly like "closed". Only a definite "open" may route a call to the normal receptionist.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from app.services.business_time import is_valid_timezone, to_local

Status = Literal["open", "closed", "unknown"]

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_WINDOWS_PER_DAY = 4
MAX_EXCEPTIONS = 366
_TIME = re.compile(r"^(\d{2}):(\d{2})$")


def _minutes(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError("times must be text like 09:00")
    m = _TIME.match(value)
    if not m:
        raise ValueError(f"'{value}' is not a time like 09:00")
    h, mi = int(m.group(1)), int(m.group(2))
    if mi > 59 or h > 24 or (h == 24 and mi != 0):
        raise ValueError(f"'{value}' is not a valid time")
    return h * 60 + mi


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _windows(raw: object, label: str) -> list[list[str]]:
    if not isinstance(raw, list):
        raise ValueError(f"{label}: expected a list of [start, end] windows")
    if len(raw) > MAX_WINDOWS_PER_DAY:
        raise ValueError(f"{label}: at most {MAX_WINDOWS_PER_DAY} windows per day")
    parsed: list[tuple[int, int]] = []
    for w in raw:
        if not isinstance(w, (list, tuple)) or len(w) != 2:
            raise ValueError(f"{label}: each window must be [start, end]")
        start, end = _minutes(w[0]), _minutes(w[1])
        if start >= end:
            raise ValueError(
                f"{label}: {w[0]}-{w[1]} must end after it starts (split overnight hours across two days)"
            )
        parsed.append((start, end))
    parsed.sort()
    for (_, e1), (s2, _) in zip(parsed, parsed[1:]):
        if s2 < e1:
            raise ValueError(f"{label}: windows must not overlap")
    return [[_fmt(s), _fmt(e)] for s, e in parsed]


def validate_schedule(raw: object) -> dict | None:
    """Normalised copy of a schedule, None for 'no schedule', ValueError for anything malformed."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("hours schedule must be an object")
    unknown = set(raw) - {"enabled", "weekly", "exceptions"}
    if unknown:
        raise ValueError("hours schedule has unknown fields")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    weekly_raw = raw.get("weekly", {})
    if not isinstance(weekly_raw, dict) or set(weekly_raw) - set(DAYS):
        raise ValueError("weekly must use the days mon..sun")
    weekly = {d: _windows(w, d) for d, w in weekly_raw.items() if w}
    exc_raw = raw.get("exceptions", {})
    if not isinstance(exc_raw, dict) or len(exc_raw) > MAX_EXCEPTIONS:
        raise ValueError(f"exceptions must be an object with at most {MAX_EXCEPTIONS} dates")
    exceptions: dict[str, list[list[str]]] = {}
    for key, w in exc_raw.items():
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(key)):
                raise ValueError
            date.fromisoformat(key)
        except ValueError:
            raise ValueError(f"'{key}' is not a date like 2026-12-25") from None
        exceptions[key] = _windows(w, key)  # [] = closed all day
    if enabled and not weekly and not exceptions:
        # An enabled schedule with no hours anywhere would mean "always closed": almost certainly a mistake.
        raise ValueError("Add at least one opening window, or turn the schedule off")
    return {"enabled": enabled, "weekly": weekly, "exceptions": exceptions}


def is_enabled(schedule: object) -> bool:
    """The owner explicitly turned structured scheduling on. Everything else keeps today's behaviour."""
    return isinstance(schedule, dict) and schedule.get("enabled") is True


def evaluate(schedule: Any, tz_name: str | None, instant: datetime | None) -> Status:
    """open / closed for a schedule we fully understand, otherwise unknown. Never raises."""
    try:
        if not is_enabled(schedule) or not is_valid_timezone(tz_name) or not isinstance(instant, datetime):
            return "unknown"
        clean = validate_schedule(schedule)
        local = to_local(instant, tz_name)
        key = local.date().isoformat()
        windows = clean["exceptions"].get(key)
        if windows is None:
            windows = clean["weekly"].get(DAYS[local.weekday()], [])
        now = local.hour * 60 + local.minute
        return "open" if any(_minutes(s) <= now < _minutes(e) for s, e in windows) else "closed"
    except Exception:  # noqa: BLE001 - a schedule we cannot evaluate must read as unknown, never as open
        return "unknown"


def received_after_hours(schedule: Any, tz_name: str | None, started_at: datetime | None) -> bool | None:
    """The lead flag: True = the call began while closed, False = while open, None = not classifiable / not opted in."""
    status = evaluate(schedule, tz_name, started_at)
    return {"open": False, "closed": True}.get(status)
