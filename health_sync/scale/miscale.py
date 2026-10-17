"""Xiaomi Mi Body Composition Scale 2 (XMTZC05HM): decode the BLE advertisement and estimate body composition.

Advertisement: service data of UUID 0x181B, 13 bytes:
  [0] control 0 (unit flags)  [1] control 1 (bit1 impedance present, bit5 stabilized, bit7 weight removed)
  [2:4] year LE  [4] month  [5] day  [6] hour  [7] minute  [8] second  [9:11] impedance LE  [11:13] weight LE
Weight is raw/200 kg (raw/100 lb). Format and formulas follow the public descriptions of this device
(e.g. lolouk44/xiaomi_mi_scale, MIT); written independently here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SERVICE_UUID = "0000181b-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class Measurement:
    timestamp: datetime
    weight_kg: float
    impedance: int | None
    stabilized: bool
    removed: bool


def parse(data: bytes) -> Measurement | None:
    if len(data) != 13:
        return None
    ctrl0, ctrl1 = data[0], data[1]
    year = int.from_bytes(data[2:4], "little")
    try:
        ts = datetime(year, data[4], data[5], data[6], data[7], data[8])
    except ValueError:
        return None
    raw_weight = int.from_bytes(data[11:13], "little")
    weight = raw_weight / 100 * 0.45359237 if ctrl0 & 0x01 else raw_weight / 200
    impedance = int.from_bytes(data[9:11], "little") if ctrl1 & 0x02 else None
    return Measurement(ts, round(weight, 2), impedance or None, bool(ctrl1 & 0x20), bool(ctrl1 & 0x80))


def body_fat_pct(weight_kg: float, impedance: int, height_cm: float, age: int, male: bool) -> float | None:
    """Body fat % from impedance (the common Xiaomi estimate). Returns None if the inputs look implausible."""
    if not impedance or impedance >= 3000 or not weight_kg:
        return None
    lbm = (height_cm * 9.058 / 100) * (height_cm / 100) + weight_kg * 0.32 + 12.226 - impedance * 0.0068 - age * 0.0542
    if male:
        const, coef = 0.8, (0.98 if weight_kg < 61 else 1.0)
    else:
        const = 9.25
        coef = 0.96 if weight_kg > 60 else (1.02 if weight_kg < 50 else 1.0)
        if height_cm > 160 and coef != 1.0:
            coef *= 1.03
    fat = (1.0 - ((lbm - const) * coef) / weight_kg) * 100
    return round(min(max(fat, 3.0), 63.0), 1)
