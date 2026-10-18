"""Google Calendar sink: Garmin activities and sleep intervals as calendar events.

One event per source record, keyed by a *deterministic* event id derived from the canonical
dedup key, so re-runs update the same event instead of creating duplicates. The target
calendar is never hard-coded: it comes from `GOOGLE_CALENDAR_ID`, or is looked up by name
(`GOOGLE_CALENDAR_NAME`, e.g. "Дневник"), and falls back to `primary`. Events are tagged in
`extendedProperties.private` so they can be told apart from anything a person created.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Iterable
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession

from health_sync.connectors import google_auth
from health_sync.core.model import Record

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3"


def event_id(source: str, kind: str, external_id: str) -> str:
    """Deterministic Calendar event id ([0-9a-f]) from the canonical dedup key.

    Calendar ids must be 5-1024 chars of [a-v0-9], so a sha256 hex digest is stable across
    runs and safe to use as an upsert key.
    """
    return hashlib.sha256(f"{source}|{kind}|{external_id}".encode()).hexdigest()


def _parse_dt(value: Any) -> datetime | None:
    """Parse a Garmin/local timestamp ('YYYY-MM-DD HH:MM:SS' or ISO) into a naive datetime."""
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _pace(minutes_per_km: float) -> str:
    if not minutes_per_km:
        return ""
    total = round(minutes_per_km * 60)
    return f"{total // 60}:{total % 60:02d}"


def _activity_description(p: dict[str, Any]) -> str:
    lines = []
    if p.get("type"):
        lines.append(f"Тип: {p['type']}")
    if p.get("distance_km"):
        lines.append(f"Дистанция: {p['distance_km']} км")
    if p.get("duration_min"):
        lines.append(f"Длительность: {p['duration_min']} мин")
    heart = ", ".join(x for x in (f"средний {p.get('avg_hr')}" if p.get("avg_hr") else "",
                                  f"макс {p.get('max_hr')}" if p.get("max_hr") else "") if x)
    if heart:
        lines.append(f"Пульс: {heart}")
    if p.get("pace_min_km"):
        lines.append(f"Темп: {_pace(p['pace_min_km'])} /км")
    if p.get("calories"):
        lines.append(f"Калории: {p['calories']}")
    if p.get("te_aerobic") or p.get("te_anaerobic"):
        lines.append(f"Training Effect: {p.get('te_aerobic')} / {p.get('te_anaerobic')} {p.get('te_label', '')}".rstrip())
    return "\n".join(lines)


def _sleep_description(p: dict[str, Any]) -> str:
    lines = []
    if p.get("total_h"):
        lines.append(f"Длительность: {p['total_h']} ч")
    if p.get("score") not in ("", None):
        lines.append(f"Оценка: {p['score']}")
    stages = ", ".join(x for x in (f"глубокий {p['deep_h']} ч" if p.get("deep_h") else "",
                                   f"лёгкий {p['light_h']} ч" if p.get("light_h") else "",
                                   f"REM {p['rem_h']} ч" if p.get("rem_h") else "",
                                   f"бодрствование {p['awake_h']} ч" if p.get("awake_h") else "") if x)
    if stages:
        lines.append(f"Стадии: {stages}")
    return "\n".join(lines)


def build_event(record: Record, *, timezone: str | None = None, prefix: str = "",
                visibility: str | None = None, transparency: str | None = None) -> dict[str, Any] | None:
    """Map one Record to a Calendar event body, or None when it has no usable time span.

    Times are written as local wall time (no offset); when `timezone` is empty Google Calendar
    resolves them in the target calendar's own timezone, which keeps the sync portable between
    people who keep their calendars in different zones.
    """
    p = record.payload
    if record.kind == "sleep":
        start = _parse_dt(p.get("start") or record.start)
        end = _parse_dt(p.get("end"))
        if start and not end and p.get("total_h"):
            end = start + timedelta(hours=float(p["total_h"]))
        summary, description = "Сон", _sleep_description(p)
    else:
        start = _parse_dt(record.start)
        end = start + timedelta(minutes=float(p.get("duration_min") or 0)) if start else None
        summary = p.get("name") or p.get("type") or "Активность"
        description = _activity_description(p)
    if not start or not end or end <= start:
        return None
    event: dict[str, Any] = {
        "id": event_id(record.source, record.kind, record.external_id),
        "summary": f"{prefix}{summary}",
        "description": description,
        "start": {"dateTime": _iso(start)},
        "end": {"dateTime": _iso(end)},
        "extendedProperties": {"private": {
            "healthSyncSource": record.source, "healthSyncKind": record.kind,
            "healthSyncId": record.external_id,
        }},
    }
    if timezone:
        event["start"]["timeZone"] = event["end"]["timeZone"] = timezone
    if visibility:
        event["visibility"] = visibility
    if transparency:
        event["transparency"] = transparency
    return event


def _env_kinds() -> set[str]:
    return {k.strip() for k in os.getenv("CALENDAR_KINDS", "activity,sleep").split(",") if k.strip()}


def _session() -> AuthorizedSession:
    return AuthorizedSession(google_auth.credentials())


def list_calendars(session: AuthorizedSession | None = None) -> list[dict[str, Any]]:
    """All calendars visible to the account (id, summary, timeZone) — used to find a name's id."""
    s = session or _session()
    r = s.get(f"{API}/users/me/calendarList",
              params={"maxResults": 250, "fields": "items(id,summary,timeZone,primary)"})
    r.raise_for_status()
    return r.json().get("items", [])


class GoogleCalendarSink:
    """Writes records of the configured kinds as events in one calendar; idempotent upsert.

    Target calendar (first match wins): `calendar_id`, else `calendar_name` looked up in the
    account's calendar list, else `primary`. All other knobs come from env so a hosted run can
    be configured per user without code changes.
    """

    name = "gcal"

    def __init__(self, kinds: Iterable[str] | None = None, calendar_id: str | None = None,
                 calendar_name: str | None = None, session: AuthorizedSession | None = None):
        self.kinds = set(kinds) if kinds is not None else _env_kinds()
        self.calendar_id = calendar_id or os.getenv("GOOGLE_CALENDAR_ID") or ""
        self.calendar_name = calendar_name if calendar_name is not None else os.getenv("GOOGLE_CALENDAR_NAME", "")
        self.timezone = os.getenv("CALENDAR_TIMEZONE") or None
        self.prefix = os.getenv("CALENDAR_SUMMARY_PREFIX", "")
        self.visibility = "private" if os.getenv("CALENDAR_PRIVATE", "true").lower() in ("1", "true", "yes") else None
        self.transparency = os.getenv("CALENDAR_TRANSPARENCY", "transparent") or None
        self._s = session
        self._calendar: str | None = None

    def _session(self) -> AuthorizedSession:
        if self._s is None:
            self._s = _session()
        return self._s

    def calendar(self) -> str:
        """Resolve the target calendar id once (id -> name lookup -> 'primary')."""
        if self._calendar:
            return self._calendar
        if self.calendar_id:
            self._calendar = self.calendar_id
        elif self.calendar_name:
            wanted = self.calendar_name.strip().casefold()
            for c in list_calendars(self._session()):
                if str(c.get("summary", "")).strip().casefold() == wanted:
                    self._calendar = c["id"]
                    break
            if not self._calendar:
                raise KeyError(f"calendar {self.calendar_name!r} not found; list them with `calendar-ls`")
        else:
            self._calendar = "primary"
        return self._calendar

    def _upsert(self, calendar: str, event: dict[str, Any]) -> None:
        s = self._session()
        url = f"{API}/calendars/{quote(calendar, safe='')}/events"
        r = s.post(url, json=event, params={"sendUpdates": "none"})
        if r.status_code == 409:  # deterministic id already exists -> update in place
            body = {k: v for k, v in event.items() if k != "id"}
            r = s.patch(f"{url}/{event['id']}", json=body, params={"sendUpdates": "none"})
        r.raise_for_status()

    def write(self, records: Iterable[Record]) -> int:
        events = [e for r in records if r.kind in self.kinds
                  if (e := build_event(r, timezone=self.timezone, prefix=self.prefix,
                                       visibility=self.visibility, transparency=self.transparency))]
        if not events:
            return 0
        calendar = self.calendar()
        for event in events:
            self._upsert(calendar, event)
        log.info("%s: %d events in %s", self.name, len(events), calendar)
        return len(events)
