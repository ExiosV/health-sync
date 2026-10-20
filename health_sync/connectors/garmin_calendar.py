"""Garmin source for the calendar: every dated health record, shaped for the Calendar sink.

The health pipeline (`garmin-health-to-drive`) already pulls activities, sleep, weight, blood
pressure and the daily summary. This source reuses it and adds the activities, so the calendar
gets activities plus the point-in-time measurements and one all-day summary per day.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta
from typing import Any, Callable, Iterable

from health_sync.connectors.garmin_health import GarminHealthSource, normalize_sleep
from health_sync.core.model import Record

log = logging.getLogger(__name__)

__all__ = ["GarminCalendarSource", "normalize_sleep"]


class GarminCalendarSource(GarminHealthSource):
    """Activities + daily/sleep/weight/blood-pressure records since the cursor."""

    name = "garmin"
    default_days = 7  # first run, when there is no cursor yet
    days_env = "GARMIN_CALENDAR_DAYS"

    def fetch(self, since: str | None = None) -> Iterable[Record]:
        yield from self._activities(self._cutoff(since))
        yield from super().fetch(since)
        yield from self._today_measurements()

    def _cutoff(self, since: str | None) -> str:
        """First-run window for activities, so a fresh install does not import the whole history."""
        return since or (date.today() - timedelta(days=int(os.getenv(self.days_env, self.default_days)))).isoformat()

    def _today_measurements(self) -> Iterable[Record]:
        """Weigh-ins and pressure readings of the unfinished day, so they appear the same day."""
        api = self._client()
        d = date.today().isoformat()
        calls: dict[str, Callable[[], Any]] = {
            "blood_pressure": lambda: api.get_blood_pressure_day(d),
            "weigh_ins": lambda: api.get_daily_weigh_ins(d),
        }
        raw: dict[str, Any] = {}
        for key, call in calls.items():
            try:
                raw[key] = call()
            except Exception as exc:  # a missing metric must not drop the run
                log.warning("%s %s: %s", d, key, str(exc)[:100])
            time.sleep(0.3)
        yield from self._measurements(raw, d)

    def cursor(self, newest: str) -> str:
        """Never move the cursor inside today: the unfinished day must be read again tomorrow."""
        return min(newest, (date.today() - timedelta(days=1)).isoformat())
