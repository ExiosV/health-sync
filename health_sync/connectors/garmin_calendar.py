"""Garmin source for the calendar: activities and sleep intervals with precise start/end.

`GarminSource` already yields activities, but only their start and duration. Sleep needs a
real interval (the Garmin `*Local` timestamps), so this source adds a `sleep` record per
night on top of the activities and hands both to the Calendar sink.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from health_sync.connectors.garmin import GarminSource
from health_sync.connectors.garmin_health import _get, _hours, _num
from health_sync.core.model import Record

log = logging.getLogger(__name__)


def _local(ms: Any) -> str:
    """Garmin `*TimestampLocal` (epoch millis shifted to local wall time) -> 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if _num(ms) else ""


def normalize_sleep(sleep: dict[str, Any]) -> dict[str, Any]:
    """Flatten a `get_sleep_data` response into the fields the calendar event needs."""
    dto = _get(sleep, "dailySleepDTO", default={}) or {}
    return {
        "start": _local(dto.get("sleepStartTimestampLocal")),
        "end": _local(dto.get("sleepEndTimestampLocal")),
        "total_h": _hours(dto.get("sleepTimeSeconds")),
        "deep_h": _hours(dto.get("deepSleepSeconds")),
        "light_h": _hours(dto.get("lightSleepSeconds")),
        "rem_h": _hours(dto.get("remSleepSeconds")),
        "awake_h": _hours(dto.get("awakeSleepSeconds")),
        "score": _get(dto, "sleepScores", "overall", "value"),
    }


class GarminCalendarSource(GarminSource):
    """Activities + sleep intervals since the cursor, shaped for the Calendar sink."""

    name = "garmin"
    default_days = 14  # first run, when there is no cursor yet

    def fetch(self, since: str | None = None) -> Iterable[Record]:
        yield from self._activities(since)
        yield from self._sleep(since)

    def _sleep(self, since: str | None) -> Iterable[Record]:
        api = self._client()
        today = date.today()
        if since:
            day = date.fromisoformat(since[:10])
        else:
            day = today - timedelta(days=int(os.getenv("GARMIN_CALENDAR_DAYS", self.default_days)))
        while day < today:  # only finished nights: today's sleep is still being recorded
            d = day.isoformat()
            try:
                sleep = api.get_sleep_data(d)
            except Exception as exc:  # one missing night must not drop the rest
                log.warning("sleep %s: %s", d, str(exc)[:100])
                sleep = None
            payload = normalize_sleep(sleep or {})
            if payload["start"]:
                yield Record("sleep", self.name, d, payload["start"], payload, sleep or {})
            day += timedelta(days=1)
            time.sleep(0.3)
