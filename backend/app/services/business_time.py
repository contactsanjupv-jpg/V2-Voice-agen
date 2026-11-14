"""
Business-local time. One place that knows how to turn "an instant" into "what the clock on the business's wall says".

Rules:
- A business timezone is an IANA name ("America/Chicago"), never a fixed offset, so daylight saving is handled by the
  tz database and not by us. Names are validated against the installed tz database before they are stored.
- Instants are stored and compared in UTC everywhere. Local time is only ever a presentation of an instant.
- When no timezone is known (older businesses, never set) we say so explicitly and show UTC, never guess a zone.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy.orm import Session

from app.db.models.business import Business

MAX_TZ_LEN = 64


@lru_cache(maxsize=1)
def _known() -> frozenset[str]:
    # "UTC"-style aliases ("EST", "Etc/GMT+5", "localtime") are real tz names but not a business's location, and the
    # fixed-offset ones silently ignore daylight saving. Only region/city names are accepted.
    return frozenset(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "posix/", "right/")))


def is_valid_timezone(name: object) -> bool:
    return isinstance(name, str) and 0 < len(name) <= MAX_TZ_LEN and name in _known()


def clean_timezone(name: str | None) -> str | None:
    """Stripped IANA name, None for blank, ValueError for anything that is not a real region/city zone."""
    if name is None:
        return None
    name = name.strip()
    if not name:
        return None
    if not is_valid_timezone(name):
        raise ValueError("Choose a valid timezone like America/Chicago")
    return name


def zone(name: str | None) -> ZoneInfo | None:
    return ZoneInfo(name) if is_valid_timezone(name) else None


def to_local(instant: datetime, tz_name: str | None) -> datetime:
    """The instant as a local wall-clock datetime. A naive datetime is taken to be UTC. Unknown zone -> UTC."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    z = zone(tz_name)
    return instant.astimezone(z if z is not None else timezone.utc)


def format_local(instant: datetime, tz_name: str | None) -> str:
    """'Sat Nov 7, 2026 3:30 PM CST'. The abbreviation reflects daylight saving at that instant (CST vs CDT)."""
    local = to_local(instant, tz_name)
    abbr = local.tzname() or "UTC"
    hour = local.hour % 12 or 12
    return f"{local:%a %b} {local.day}, {local.year} {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'} {abbr}"


def organization_timezone(db: Session, organization_id: uuid.UUID) -> str | None:
    """The timezone of the organization's (oldest) business that has one set."""
    return (
        db.query(Business.timezone)
        .filter(Business.organization_id == organization_id, Business.timezone.isnot(None))
        .order_by(Business.created_at, Business.id)
        .limit(1)
        .scalar()
    )
