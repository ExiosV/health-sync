"""Garmin source for the calendar: every dated health record, shaped for the Calendar sink.

The health pipeline (`garmin-health-to-drive`) already pulls activities, sleep, weight, blood
pressure and the daily summary. This source reuses it and adds the activities, so the calendar
gets activities plus the point-in-time measurements and one all-day summary per day.
"""
from __future__ import annotations

from typing import Iterable

from health_sync.connectors.garmin_health import GarminHealthSource, normalize_sleep
from health_sync.core.model import Record

__all__ = ["GarminCalendarSource", "normalize_sleep"]


class GarminCalendarSource(GarminHealthSource):
    """Activities + daily/sleep/weight/blood-pressure records since the cursor."""

    name = "garmin"
    default_days = 7  # first run, when there is no cursor yet
    days_env = "GARMIN_CALENDAR_DAYS"

    def fetch(self, since: str | None = None) -> Iterable[Record]:
        yield from self._activities(since)
        yield from super().fetch(since)
