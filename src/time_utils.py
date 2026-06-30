"""Timezone helpers for UTC storage and Beijing-time display."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python always has zoneinfo here, but keep fallback safe.
    ZoneInfo = None


UTC = timezone.utc
if ZoneInfo is not None:
    try:
        BEIJING_TZ = ZoneInfo("Asia/Shanghai")
    except Exception:
        BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
else:
    BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_utc() -> datetime:
    """Return current UTC time as naive datetime for existing SQLite storage."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_beijing(dt: datetime | None) -> datetime | None:
    """Convert a stored UTC datetime to Beijing time.

    Existing database values are naive UTC datetimes, so naive inputs are treated
    as UTC for backward-compatible display.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BEIJING_TZ)


def now_beijing() -> datetime:
    """Return current Beijing time as timezone-aware datetime."""
    return datetime.now(UTC).astimezone(BEIJING_TZ)


def format_beijing(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M", fallback: str = "") -> str:
    """Format a datetime in Beijing time."""
    local_dt = to_beijing(dt)
    if local_dt is None:
        return fallback
    return local_dt.strftime(fmt)


def beijing_timestamp(fmt: str = "%Y%m%d_%H%M%S") -> str:
    """Return a Beijing-time timestamp string for filenames and job IDs."""
    return now_beijing().strftime(fmt)
