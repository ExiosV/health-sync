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
        yield from self._activities(self._cutoff(since), with_zones=False)
        yield from super().fetch(since)
        yield from self._today_sleep()
        yield from self._today_measurements()

    def _cutoff(self, since: str | None) -> str | None:
        """First-run window for activities; `GARMIN_CALENDAR_ACTIVITY_DAYS=0` imports the history."""
        if since:
            return since
        days = int(os.getenv("GARMIN_CALENDAR_ACTIVITY_DAYS", os.getenv(self.days_env, self.default_days)))
        return (date.today() - timedelta(days=days)).isoformat() if days > 0 else None

    def _today_sleep(self) -> Iterable[Record]:
        """This morning's sleep (and naps) as soon as the watch has synced it, not a day later.

        Sleep belongs to the wake-up date; it is final once the person is awake, unlike the rest of the day.
        """
        api = self._client()
        d = date.today().isoformat()
        try:
            sleep = api.get_sleep_data(d)
        except Exception as exc:  # a missing night must not drop the run
            log.warning("%s sleep: %s", d, str(exc)[:100])
            return
        end = ((sleep or {}).get("dailySleepDTO") or {}).get("sleepEndTimestampGMT")
        if isinstance(end, (int, float)) and end <= time.time() * 1000:  # still asleep / not synced: skip
            yield from self._sleep_records(sleep, d)

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
