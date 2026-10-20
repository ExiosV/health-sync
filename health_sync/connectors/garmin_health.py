"""Garmin Connect daily health data: sleep, body battery, stress, HRV, readiness, blood pressure, weight."""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from health_sync.connectors.garmin import GarminSource
from health_sync.core.model import Record

log = logging.getLogger(__name__)


def _get(d: Any, *path: str, default: Any = "") -> Any:
    for key in path:
        if not isinstance(d, dict) or d.get(key) is None:
            return default
        d = d[key]
    return d


def _num(x: Any) -> bool:
    return isinstance(x, (int, float))


def _hours(seconds: Any) -> Any:
    return round(seconds / 3600, 2) if _num(seconds) else ""


def _local_hhmm(ms: Any) -> str:
    # *Local timestamps are epoch millis shifted to local wall time, so read them as UTC
    return datetime.utcfromtimestamp(ms / 1000).strftime("%H:%M") if _num(ms) else ""


def _local_dt(ms: Any) -> str:
    """`*TimestampLocal` (epoch millis shifted to local wall time) -> 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if _num(ms) else ""


def normalize_sleep(sleep: dict[str, Any]) -> dict[str, Any]:
    """Flatten a `get_sleep_data` response into the fields a calendar event needs."""
    dto = _get(sleep, "dailySleepDTO", default={}) or {}
    return {
        "start": _local_dt(dto.get("sleepStartTimestampLocal")),
        "end": _local_dt(dto.get("sleepEndTimestampLocal")),
        "total_h": _hours(dto.get("sleepTimeSeconds")),
        "deep_h": _hours(dto.get("deepSleepSeconds")),
        "light_h": _hours(dto.get("lightSleepSeconds")),
        "rem_h": _hours(dto.get("remSleepSeconds")),
        "awake_h": _hours(dto.get("awakeSleepSeconds")),
        "score": _get(dto, "sleepScores", "overall", "value"),
    }


def normalize_naps(sleep: dict[str, Any]) -> list[dict[str, Any]]:
    """Naps of one night's `get_sleep_data` response, in local wall time.

    Garmin puts them in `dailySleepDTO.dailyNapDTOS` with GMT timestamps plus a minute offset
    that can differ from the night's (travel/DST), so each nap carries its own offset.
    """
    dto = _get(sleep, "dailySleepDTO", default={}) or {}
    naps = []
    for nap in dto.get("dailyNapDTOS") or []:
        start, end = nap.get("napStartTimestampGMT"), nap.get("napEndTimestampGMT")
        if not start or not end:
            continue
        offset = timedelta(minutes=int(nap.get("napStartTimeOffset") or 0))
        begin = _parse_gmt(start) + offset
        finish = _parse_gmt(end) + offset
        naps.append({
            "start": begin.strftime("%Y-%m-%d %H:%M:%S"),
            "end": finish.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_min": round(nap.get("napTimeSec", 0) / 60) if _num(nap.get("napTimeSec")) else "",
            "feedback": nap.get("napFeedback") or "",
        })
    return naps


def _parse_gmt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "")[:19])


def normalize_daily(raw: dict[str, Any]) -> dict[str, Any]:
    u, sleep, hrv = raw.get("user_summary") or {}, raw.get("sleep") or {}, raw.get("hrv") or {}
    ready = raw.get("readiness") or {}
    dto = sleep.get("dailySleepDTO") or {}
    return {
        "steps": _get(u, "totalSteps"),
        "distance_km": round(u["totalDistanceMeters"] / 1000, 2) if _num(u.get("totalDistanceMeters")) else "",
        "active_kcal": _get(u, "activeKilocalories"),
        "resting_hr": _get(u, "restingHeartRate"),
        "min_hr": _get(u, "minHeartRate"),
        "max_hr": _get(u, "maxHeartRate"),
        "avg_stress": _get(u, "averageStressLevel"),
        "max_stress": _get(u, "maxStressLevel"),
        "body_battery_high": _get(u, "bodyBatteryHighestValue"),
        "body_battery_low": _get(u, "bodyBatteryLowestValue"),
        "body_battery_charged": _get(u, "bodyBatteryChargedValue"),
        "body_battery_drained": _get(u, "bodyBatteryDrainedValue"),
        "intensity_min_moderate": _get(u, "moderateIntensityMinutes"),
        "intensity_min_vigorous": _get(u, "vigorousIntensityMinutes"),
        "floors_up": _get(u, "floorsAscended"),
        "sleep_score": _get(dto, "sleepScores", "overall", "value"),
        "sleep_total_h": _hours(dto.get("sleepTimeSeconds")),
        "sleep_deep_h": _hours(dto.get("deepSleepSeconds")),
        "sleep_light_h": _hours(dto.get("lightSleepSeconds")),
        "sleep_rem_h": _hours(dto.get("remSleepSeconds")),
        "sleep_awake_h": _hours(dto.get("awakeSleepSeconds")),
        "sleep_start": _local_hhmm(dto.get("sleepStartTimestampLocal")),
        "sleep_end": _local_hhmm(dto.get("sleepEndTimestampLocal")),
        "nap_total_h": _hours(dto.get("napTimeSeconds")),
        "spo2_avg": _get(dto, "averageSpO2Value"),
        "spo2_low": _get(dto, "lowestSpO2Value"),
        "respiration_avg": _get(dto, "averageRespirationValue"),
        "hrv_last_night": _get(hrv, "hrvSummary", "lastNightAvg"),
        "hrv_weekly_avg": _get(hrv, "hrvSummary", "weeklyAvg"),
        "hrv_status": _get(hrv, "hrvSummary", "status"),
        "readiness_score": _get(ready, "score"),
        "readiness_level": _get(ready, "level"),
        "recovery_time_h": round(ready["recoveryTime"] / 60, 1) if _num(ready.get("recoveryTime")) else "",
    }


def _kg(grams: Any) -> Any:
    return round(grams / 1000, 2) if _num(grams) else ""


class GarminHealthSource(GarminSource):
    name = "garmin"
    default_days = 14  # first run, when there is no cursor yet
    days_env = "GARMIN_HEALTH_DAYS"

    def fetch(self, since: str | None = None) -> Iterable[Record]:
        api = self._client()
        today = date.today()
        if since:
            day = date.fromisoformat(since[:10]) + timedelta(days=1)
        else:
            day = today - timedelta(days=int(os.getenv(self.days_env, self.default_days)))
        while day < today:  # only finished days: today's numbers are still changing
            yield from self._day(api, day.isoformat())
            day += timedelta(days=1)

    def _day(self, api: Any, d: str) -> Iterable[Record]:
        calls: dict[str, Callable[[], Any]] = {
            "user_summary": lambda: api.get_user_summary(d),
            "sleep": lambda: api.get_sleep_data(d),
            "hrv": lambda: api.get_hrv_data(d),
            "readiness": lambda: api.get_morning_training_readiness(d),
            "stress": lambda: api.get_stress_data(d),
            "body_battery": lambda: api.get_body_battery(d, d),
            "spo2": lambda: api.get_spo2_data(d),
            "blood_pressure": lambda: api.get_blood_pressure_day(d),
            "weigh_ins": lambda: api.get_daily_weigh_ins(d),
        }
        raw: dict[str, Any] = {}
        for key, call in calls.items():
            try:
                raw[key] = call()
            except Exception as exc:  # one missing metric must not drop the day
                log.warning("%s %s: %s", d, key, str(exc)[:100])
            time.sleep(0.3)
        yield Record("daily", self.name, d, d, normalize_daily(raw), raw)
        sleep = normalize_sleep(raw.get("sleep") or {})
        if sleep["start"]:
            yield Record("sleep", self.name, d, sleep["start"], sleep, raw.get("sleep") or {})
        for i, nap in enumerate(normalize_naps(raw.get("sleep") or {})):
            yield Record("nap", self.name, f"{d}:{nap['start']}", nap["start"], nap, nap)
        yield from self._measurements(raw, d)

    def _measurements(self, raw: dict[str, Any], d: str) -> Iterable[Record]:
        for m in _get(raw.get("blood_pressure"), "bloodPressureMeasurements", default=[]):
            ts = m.get("measurementTimestampLocal") or d
            yield Record("blood_pressure", self.name, ts, ts[:19].replace("T", " "), {
                "systolic": m.get("systolic", ""), "diastolic": m.get("diastolic", ""),
                "pulse": m.get("pulse", ""), "category": m.get("categoryName", "")}, m)
        for w in _get(raw.get("weigh_ins"), "dateWeightList", default=[]):
            ts = datetime.utcfromtimestamp(w["date"] / 1000).strftime("%Y-%m-%d %H:%M:%S") if w.get("date") else d
            yield Record("weight", self.name, str(w.get("samplePk", ts)), ts, {
                "weight_kg": _kg(w.get("weight")), "bmi": w.get("bmi") or "", "body_fat_pct": w.get("bodyFat") or "",
                "body_water_pct": w.get("bodyWater") or "", "muscle_mass_kg": _kg(w.get("muscleMass")),
                "bone_mass_kg": _kg(w.get("boneMass"))}, w)
