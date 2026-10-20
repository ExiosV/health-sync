"""Google Calendar sink: Garmin health records as calendar events.

One event per source record, keyed by a *deterministic* event id derived from the canonical
dedup key, so re-runs update the same event instead of creating duplicates. Activities and
sleep become timed events, weight/blood-pressure measurements become short timed events (or
all-day when Garmin only gives a date), and the daily summary becomes one all-day event. The
target calendar is never hard-coded: it comes from `GOOGLE_CALENDAR_ID`, or is looked up by
name (`GOOGLE_CALENDAR_NAME`, e.g. "Дневник"), and falls back to `primary`. Events are tagged
in `extendedProperties.private` so they can be told apart from anything a person created.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
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


def _activity_description(p: dict[str, Any], url: str = "") -> str:
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
    if url:
        lines.append(f"Garmin: {url}")
    return "\n".join(lines)


NAP_FEEDBACK = {
    "LONG_DURATION_LOW_NEED": "долгий сон при низкой потребности",
    "LONG_DURATION_HIGH_NEED": "долгий сон при высокой потребности",
    "OPTIMAL": "оптимально",
    "SHORT_DURATION": "короткий сон",
}


def _nap_description(p: dict[str, Any]) -> str:
    lines = []
    if p.get("duration_min") not in ("", None):
        lines.append(f"Длительность: {p['duration_min']} мин")
    if p.get("feedback"):
        lines.append(f"Оценка Garmin: {NAP_FEEDBACK.get(p['feedback'], p['feedback'])}")
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


def _weight_description(p: dict[str, Any]) -> str:
    lines = []
    if p.get("weight_kg"):
        lines.append(f"Вес: {p['weight_kg']} кг")
    if p.get("bmi"):
        lines.append(f"ИМТ: {p['bmi']}")
    if p.get("body_fat_pct"):
        lines.append(f"Жир: {p['body_fat_pct']} %")
    if p.get("body_water_pct"):
        lines.append(f"Вода: {p['body_water_pct']} %")
    if p.get("muscle_mass_kg"):
        lines.append(f"Мышцы: {p['muscle_mass_kg']} кг")
    if p.get("bone_mass_kg"):
        lines.append(f"Кости: {p['bone_mass_kg']} кг")
    return "\n".join(lines)


def _blood_pressure_description(p: dict[str, Any]) -> str:
    lines = []
    if p.get("systolic") and p.get("diastolic"):
        lines.append(f"Давление: {p['systolic']}/{p['diastolic']} мм рт. ст.")
    if p.get("pulse"):
        lines.append(f"Пульс: {p['pulse']}")
    if p.get("category"):
        lines.append(f"Категория: {p['category']}")
    return "\n".join(lines)


def _daily_description(p: dict[str, Any]) -> str:
    """One all-day summary: the metrics that have no single instant of their own."""
    lines: list[str] = []
    if p.get("steps") not in ("", None):
        distance = f" ({p['distance_km']} км)" if p.get("distance_km") not in ("", None) else ""
        lines.append(f"Шаги: {p['steps']}{distance}")
    if p.get("resting_hr") not in ("", None):
        heart = f"{p['resting_hr']}"
        if p.get("min_hr") not in ("", None) or p.get("max_hr") not in ("", None):
            heart += f" (мин {p.get('min_hr', '—')}, макс {p.get('max_hr', '—')})"
        lines.append(f"Пульс покоя: {heart}")
    if p.get("avg_stress") not in ("", None):
        stress = f"{p['avg_stress']}"
        if p.get("max_stress") not in ("", None):
            stress += f" (макс {p['max_stress']})"
        lines.append(f"Стресс: {stress}")
    if p.get("body_battery_high") not in ("", None):
        battery = f"{p.get('body_battery_low', '—')}–{p['body_battery_high']}"
        if p.get("body_battery_charged") not in ("", None):
            battery += f" (заряд {p['body_battery_charged']}, расход {p.get('body_battery_drained', '—')})"
        lines.append(f"Body Battery: {battery}")
    if p.get("sleep_total_h") not in ("", None):
        sleep = f"{p['sleep_total_h']} ч"
        if p.get("sleep_start") and p.get("sleep_end"):
            sleep += f" ({p['sleep_start']}–{p['sleep_end']})"
        if p.get("sleep_score") not in ("", None):
            sleep += f", оценка {p['sleep_score']}"
        lines.append(f"Сон: {sleep}")
    if p.get("nap_total_h") not in ("", None):
        lines.append(f"Дневной сон: {p['nap_total_h']} ч")
    if p.get("hrv_last_night") not in ("", None):
        hrv = f"{p['hrv_last_night']}"
        if p.get("hrv_status"):
            hrv += f" ({p['hrv_status']})"
        lines.append(f"HRV: {hrv}")
    if p.get("spo2_avg") not in ("", None):
        lines.append(f"SpO2: {p['spo2_avg']}" + (f" (мин {p['spo2_low']})" if p.get("spo2_low") not in ("", None) else ""))
    if p.get("respiration_avg") not in ("", None):
        lines.append(f"Дыхание: {p['respiration_avg']}")
    if p.get("readiness_score") not in ("", None):
        readiness = f"{p['readiness_score']}"
        if p.get("readiness_level"):
            readiness += f" ({p['readiness_level']})"
        if p.get("recovery_time_h") not in ("", None):
            readiness += f", восстановление {p['recovery_time_h']} ч"
        lines.append(f"Готовность: {readiness}")
    if p.get("intensity_min_moderate") not in ("", None) or p.get("intensity_min_vigorous") not in ("", None):
        lines.append(f"Интенсивность: {p.get('intensity_min_moderate', 0)} мин / {p.get('intensity_min_vigorous', 0)} мин")
    if p.get("floors_up") not in ("", None):
        lines.append(f"Этажи: {p['floors_up']}")
    if p.get("active_kcal") not in ("", None):
        lines.append(f"Активные калории: {p['active_kcal']}")
    return "\n".join(lines)


def _is_date_only(value: Any) -> bool:
    """True for a bare date ('YYYY-MM-DD'): no time of day to anchor a timed event to."""
    return len(str(value or "").strip()) <= 10


def _timed(start: datetime, end: datetime, timezone: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    first: dict[str, Any] = {"dateTime": _iso(start)}
    last: dict[str, Any] = {"dateTime": _iso(end)}
    if timezone:
        first["timeZone"] = last["timeZone"] = timezone
    return first, last


def _all_day(day: str) -> tuple[dict[str, Any], dict[str, Any]]:
    start = datetime.fromisoformat(day[:10])
    return ({"date": start.date().isoformat()},
            {"date": (start + timedelta(days=1)).date().isoformat()})


def _event(record: Record, summary: str, description: str, start: dict[str, Any],
           end: dict[str, Any], *, prefix: str, visibility: str | None,
           transparency: str | None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": event_id(record.source, record.kind, record.external_id),
        "summary": f"{prefix}{summary}",
        "description": description,
        "start": start,
        "end": end,
        "extendedProperties": {"private": {
            "healthSyncSource": record.source, "healthSyncKind": record.kind,
            "healthSyncId": record.external_id,
        }},
    }
    if visibility:
        event["visibility"] = visibility
    if transparency:
        event["transparency"] = transparency
    return event


def build_event(record: Record, *, timezone: str | None = None, prefix: str = "",
                visibility: str | None = None, transparency: str | None = None,
                point_minutes: int = 10) -> dict[str, Any] | None:
    """Map one Record to a Calendar event body, or None when it has no usable time span.

    Times are written as local wall time (no offset); when `timezone` is empty Google Calendar
    resolves them in the target calendar's own timezone, which keeps the sync portable between
    people who keep their calendars in different zones.

    Measurements with only a date (no time of day) and the daily summary become all-day
    events; everything with a real instant gets a short timed event of `point_minutes`.
    """
    p = record.payload
    common = {"prefix": prefix, "visibility": visibility, "transparency": transparency}

    if record.kind == "daily":
        start, end = _all_day(record.start)
        return _event(record, "Здоровье за день", _daily_description(p), start, end, **common)

    if record.kind == "weight":
        summary = f"Вес: {p['weight_kg']} кг" if p.get("weight_kg") else "Вес"
        description = _weight_description(p)
        if _is_date_only(record.start) or not _parse_dt(record.start):
            start, end = _all_day(record.start)
            return _event(record, summary, description, start, end, **common)
        start_dt = _parse_dt(record.start)
        start, end = _timed(start_dt, start_dt + timedelta(minutes=point_minutes), timezone)
        return _event(record, summary, description, start, end, **common)

    if record.kind == "blood_pressure":
        summary = (f"Давление: {p['systolic']}/{p['diastolic']}"
                   if p.get("systolic") and p.get("diastolic") else "Давление")
        description = _blood_pressure_description(p)
        if _is_date_only(record.start) or not _parse_dt(record.start):
            start, end = _all_day(record.start)
            return _event(record, summary, description, start, end, **common)
        start_dt = _parse_dt(record.start)
        start, end = _timed(start_dt, start_dt + timedelta(minutes=point_minutes), timezone)
        return _event(record, summary, description, start, end, **common)

    if record.kind == "nap":
        start_dt = _parse_dt(p.get("start") or record.start)
        end_dt = _parse_dt(p.get("end"))
        if start_dt and not end_dt and p.get("duration_min"):
            end_dt = start_dt + timedelta(minutes=float(p["duration_min"]))
        if not start_dt or not end_dt or end_dt <= start_dt:
            return None
        start, end = _timed(start_dt, end_dt, timezone)
        return _event(record, "Дневной сон", _nap_description(p), start, end, **common)

    if record.kind == "sleep":
        start_dt = _parse_dt(p.get("start") or record.start)
        end_dt = _parse_dt(p.get("end"))
        if start_dt and not end_dt and p.get("total_h"):
            end_dt = start_dt + timedelta(hours=float(p["total_h"]))
        if not start_dt or not end_dt or end_dt <= start_dt:
            return None
        start, end = _timed(start_dt, end_dt, timezone)
        return _event(record, "Сон", _sleep_description(p), start, end, **common)

    start_dt = _parse_dt(record.start)
    if not start_dt:
        return None
    end_dt = start_dt + timedelta(minutes=float(p.get("duration_min") or 0))
    if end_dt <= start_dt:
        return None
    start, end = _timed(start_dt, end_dt, timezone)
    return _event(record, p.get("name") or p.get("type") or "Активность",
                  _activity_description(p, f"https://connect.garmin.com/modern/activity/{record.external_id}"),
                  start, end, **common)


def _env_kinds() -> set[str]:
    default = "activity,sleep,nap,weight,blood_pressure,daily"
    return {k.strip() for k in os.getenv("CALENDAR_KINDS", default).split(",") if k.strip()}


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
        self.point_minutes = int(os.getenv("CALENDAR_POINT_MINUTES", "10"))
        self.write_delay = float(os.getenv("CALENDAR_WRITE_DELAY", "0.5"))
        self.retries = int(os.getenv("CALENDAR_WRITE_RETRIES", "6"))
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

    @staticmethod
    def _throttled(r: Any) -> bool:
        """Google answers a burst of writes with 403 rateLimitExceeded, not 429."""
        if r.status_code == 429 or r.status_code >= 500:
            return True
        if r.status_code == 403:
            text = (getattr(r, "text", "") or "").lower()
            return "ratelimit" in text or "quota" in text
        return False

    def _send(self, method: str, url: str, **kw: Any) -> Any:
        """One Calendar write, retried with backoff while Google is throttling or erroring."""
        s = self._session()
        call = getattr(s, method)
        for attempt in range(self.retries):
            r = call(url, **kw)
            if not self._throttled(r) or attempt == self.retries - 1:
                return r
            delay = float(getattr(r, "headers", {}).get("Retry-After", 0) or 0) or min(60, 2 ** attempt)
            log.warning("calendar write throttled (%d), retrying in %.0fs", r.status_code, delay)
            time.sleep(delay)
        return r  # pragma: no cover - the loop always returns

    def _upsert(self, calendar: str, event: dict[str, Any]) -> None:
        url = f"{API}/calendars/{quote(calendar, safe='')}/events"
        params = {"sendUpdates": "none"}
        r = self._send("post", url, json=event, params=params)
        if r.status_code == 409:  # deterministic id already exists -> update in place
            body = {k: v for k, v in event.items() if k != "id"}
            # A deleted event keeps a `cancelled` tombstone under the same id and rejects a plain
            # PATCH with 403, so ask explicitly to bring it back to `confirmed` in the same request.
            body["status"] = "confirmed"
            r = self._send("patch", f"{url}/{event['id']}", json=body, params=params)
        if r.status_code >= 400:
            log.error("%s -> %d %s", url, r.status_code, (getattr(r, "text", "") or "")[:300])
        r.raise_for_status()

    def write(self, records: Iterable[Record]) -> int:
        events = [e for r in records if r.kind in self.kinds
                  if (e := build_event(r, timezone=self.timezone, prefix=self.prefix,
                                       visibility=self.visibility, transparency=self.transparency,
                                       point_minutes=self.point_minutes))]
        if not events:
            return 0
        calendar = self.calendar()
        for i, event in enumerate(events):
            if i and self.write_delay > 0:  # a backfill of hundreds of events must not trip the quota
                time.sleep(self.write_delay)
            self._upsert(calendar, event)
        log.info("%s: %d events in %s", self.name, len(events), calendar)
        return len(events)
