"""Time helpers: Garmin gives both local wall time and GMT for most records, so the UTC offset can be derived."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _ts(value: Any) -> float | None:
    """Epoch millis or 'YYYY-MM-DD[ T]HH:MM:SS[.f]' (read as if it were UTC) -> seconds, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000
    text = str(value or "").strip().replace("T", " ")[:19]
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() if len(text) >= 19 else None
    except ValueError:
        return None


def offset_minutes(local: Any, gmt: Any) -> int | None:
    """UTC offset in minutes (local - GMT) from a local and a GMT timestamp of the same instant."""
    a, b = _ts(local), _ts(gmt)
    if a is None or b is None:
        return None
    minutes = round((a - b) / 60)
    return minutes if abs(minutes) <= 14 * 60 else None  # not a real zone offset: ignore


def format_offset(minutes: int) -> str:
    """90 -> '+01:30', -300 -> '-05:00' (RFC 3339)."""
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(int(minutes)), 60)
    return f"{sign}{hours:02d}:{mins:02d}"
