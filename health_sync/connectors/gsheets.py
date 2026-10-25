"""Google Sheets sink (user OAuth or service account, via `gspread`)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

import gspread
from google_kit import has_oauth

from health_sync.connectors import google_auth
from health_sync.core.model import Record

log = logging.getLogger(__name__)


def format_pace(minutes_per_km: float) -> str:
    """5.37 -> '5:22'."""
    if not minutes_per_km:
        return "0:00"
    total = round(minutes_per_km * 60)
    return f"{total // 60}:{total % 60:02d}"


HEADER = [
    "synced_at", "start", "name", "type", "distance_km", "duration_min", "avg_hr", "max_hr", "pace_min_km",
    "cadence_avg", "cadence_max", "te_aerobic", "te_anaerobic", "hr_zones", "vo2max", "calories", "te_label",
    "elevation_gain_m", "elevation_loss_m",
]


def activity_row(r: Record, synced_at: str) -> list:
    p = r.payload
    return [
        synced_at, r.start, p["name"], p["type"], p["distance_km"], p["duration_min"],
        p["avg_hr"], p["max_hr"], format_pace(p["pace_min_km"]), round(p["cadence_avg"], 1),
        round(p["cadence_max"], 1), round(float(p["te_aerobic"]), 1), round(float(p["te_anaerobic"]), 1),
        json.dumps(p["hr_zones"]), round(float(p["vo2max"]), 1), p["calories"], p["te_label"],
        p["elevation_gain_m"], p["elevation_loss_m"],
    ]


@dataclass(frozen=True)
class Table:
    header: list[str]
    row: Callable[[Record, str], list]
    key_col: int  # column that identifies a row (used to skip rows already present)


def _daily_row(r: Record, _synced_at: str) -> list:
    p = r.payload
    return [r.start] + [p.get(c, "") for c in DAILY_HEADER[1:]]


def _bp_row(r: Record, _synced_at: str) -> list:
    p = r.payload
    return [r.start, p["systolic"], p["diastolic"], p["pulse"], p["category"]]


def _weight_row(r: Record, _synced_at: str) -> list:
    p = r.payload
    return [r.start] + [p.get(c, "") for c in WEIGHT_HEADER[1:]]


DAILY_HEADER = [
    "date", "steps", "distance_km", "active_kcal", "resting_hr", "min_hr", "max_hr", "avg_stress", "max_stress",
    "body_battery_high", "body_battery_low", "body_battery_charged", "body_battery_drained",
    "intensity_min_moderate", "intensity_min_vigorous", "floors_up",
    "sleep_score", "sleep_total_h", "sleep_deep_h", "sleep_light_h", "sleep_rem_h", "sleep_awake_h",
    "sleep_start", "sleep_end", "spo2_avg", "spo2_low", "respiration_avg",
    "hrv_last_night", "hrv_weekly_avg", "hrv_status", "readiness_score", "readiness_level", "recovery_time_h",
]
BP_HEADER = ["time", "systolic", "diastolic", "pulse", "category"]
WEIGHT_HEADER = ["time", "weight_kg", "bmi", "body_fat_pct", "body_water_pct", "muscle_mass_kg", "bone_mass_kg"]

TABLES = {
    "activity": Table(HEADER, activity_row, key_col=1),
    "daily": Table(DAILY_HEADER, _daily_row, key_col=0),
    "blood_pressure": Table(BP_HEADER, _bp_row, key_col=0),
    "weight": Table(WEIGHT_HEADER, _weight_row, key_col=0),
}


class SheetsSink:
    """Writes records of one kind into a worksheet (first one by default), newest on top, skipping known keys."""

    def __init__(self, kind: str = "activity", spreadsheet_id: str | None = None, worksheet: str | None = None):
        self.name = f"gsheets:{kind}"
        self.kind = kind
        self.table = TABLES[kind]
        self.spreadsheet_id = spreadsheet_id or os.environ["SPREADSHEET_ID"]
        self.worksheet = worksheet

    def _open(self):
        if has_oauth():
            gc = gspread.authorize(google_auth.credentials())
        elif (info := google_auth.service_account_info()):
            gc = gspread.service_account_from_dict(info)
        else:
            gc = gspread.service_account(filename=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"))
        sh = gc.open_by_key(self.spreadsheet_id)
        return sh.worksheet(self.worksheet) if self.worksheet else sh.sheet1

    def write(self, records: Iterable[Record]) -> int:
        records = [r for r in records if r.kind == self.kind]
        if not records:
            return 0
        ws = self._open()
        values = ws.get_all_values()
        if not values or not any(values[0]):  # a fresh sheet may come back as one empty row
            ws.update(range_name="A1", values=[self.table.header])
        col = self.table.key_col
        seen = {row[col] for row in values if len(row) > col}
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [self.table.row(r, now) for r in records if str(self.table.row(r, now)[col]) not in seen]
        if rows:
            ws.insert_rows(rows, row=2)  # newest on top, below the header
        log.info("%s: %d new rows", self.name, len(rows))
        return len(rows)
