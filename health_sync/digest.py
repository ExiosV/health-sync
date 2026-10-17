"""Monthly markdown digest of Garmin health and activity data (readable by people and by a RAG)."""
from __future__ import annotations

from statistics import mean
from typing import Any


def _f(x: Any) -> float | None:
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _avg(rows: list[dict], col: str, nd: int = 1) -> str:
    vals = [v for v in (_f(r.get(col)) for r in rows) if v is not None]
    if not vals:
        return "н/д"
    return str(round(mean(vals))) if nd == 0 else str(round(mean(vals), nd))


def _day_line(r: dict) -> str:
    parts = []
    if r.get("sleep_total_h") not in ("", None):
        parts.append(f"сон {r['sleep_total_h']} ч (оценка {r.get('sleep_score') or 'н/д'}, {r.get('sleep_start')}–{r.get('sleep_end')})")
    for label, col in (("пульс покоя", "resting_hr"), ("стресс", "avg_stress"), ("шаги", "steps")):
        if r.get(col) not in ("", None):
            parts.append(f"{label} {r[col]}")
    if r.get("hrv_last_night") not in ("", None):
        parts.append(f"HRV {r['hrv_last_night']} ({r.get('hrv_status') or 'н/д'})")
    if r.get("body_battery_low") not in ("", None):
        parts.append(f"Body Battery {r['body_battery_low']}–{r.get('body_battery_high')}")
    if r.get("readiness_score") not in ("", None):
        parts.append(f"готовность {r['readiness_score']} ({r.get('readiness_level')})")
    return f"- {r['date']}: " + ", ".join(parts)


def build_month(month: str, daily: list[dict], bp: list[dict], weight: list[dict], acts: list[dict]) -> str:
    """`month` is YYYY-MM; each list holds dict rows (sheet header -> value). Newest first in, newest first out."""
    daily = sorted((r for r in daily if str(r.get("date", "")).startswith(month)), key=lambda r: r["date"], reverse=True)
    bp = sorted((r for r in bp if str(r.get("time", "")).startswith(month)), key=lambda r: r["time"], reverse=True)
    weight = sorted((r for r in weight if str(r.get("time", "")).startswith(month)), key=lambda r: r["time"], reverse=True)
    acts = sorted((r for r in acts if str(r.get("start", "")).startswith(month)), key=lambda r: r["start"], reverse=True)
    out = [f"# Garmin: здоровье и активность, {month}", ""]
    out.append(f"Данных по дням: {len(daily)}. Источник: Garmin Connect (часы, весы, тонометр), выгрузка health-sync.")
    if daily:
        out.append(f"Сводка: сон в среднем {_avg(daily, 'sleep_total_h')} ч (оценка {_avg(daily, 'sleep_score', 0)}), "
                   f"пульс покоя {_avg(daily, 'resting_hr', 0)}, HRV {_avg(daily, 'hrv_last_night', 0)}, "
                   f"стресс {_avg(daily, 'avg_stress', 0)}, шаги {_avg(daily, 'steps', 0)}, "
                   f"Body Battery макс {_avg(daily, 'body_battery_high', 0)} / мин {_avg(daily, 'body_battery_low', 0)}.")
    if bp:
        worst = max(bp, key=lambda r: _f(r.get("systolic")) or 0)
        out.append(f"Давление: {len(bp)} измерений, в среднем {_avg(bp, 'systolic', 0)}/{_avg(bp, 'diastolic', 0)}, "
                   f"максимум {worst['systolic']}/{worst['diastolic']} ({worst['time'][:10]}).")
    if weight:
        first, last = weight[-1], weight[0]
        fat = f", жир {last['body_fat_pct']} %" if last.get("body_fat_pct") not in ("", None) else ""
        out.append(f"Вес: {first['time'][:10]} {first['weight_kg']} кг → {last['time'][:10]} {last['weight_kg']} кг{fat}.")
    if acts:
        by: dict[str, list[float]] = {}
        for a in acts:
            by.setdefault(a.get("type") or "другое", []).append(_f(a.get("distance_km")) or 0.0)
        kinds = ", ".join(f"{t} {len(v)} шт. {round(sum(v), 1)} км" for t, v in sorted(by.items(), key=lambda kv: -len(kv[1])))
        out.append(f"Активности: {len(acts)} ({kinds}).")
    if daily:
        out += ["", "## По дням", *[_day_line(r) for r in daily]]
    if bp:
        out += ["", "## Давление", *[f"- {r['time'][:16]}: {r['systolic']}/{r['diastolic']}, пульс {r['pulse']} ({r['category']})" for r in bp]]
    if weight:
        out += ["", "## Вес", *[f"- {r['time'][:16]}: {r['weight_kg']} кг" + (f", жир {r['body_fat_pct']} %" if r.get("body_fat_pct") not in ("", None) else "") for r in weight]]
    if acts:
        out += ["", "## Активности", *[f"- {a['start'][:16]} {a.get('type')} «{a.get('name')}»: {a.get('distance_km')} км, {a.get('duration_min')} мин, ср. пульс {a.get('avg_hr')}" for a in acts]]
    return "\n".join(out) + "\n"
