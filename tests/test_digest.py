import unittest

from health_sync.digest import build_month


class DigestTest(unittest.TestCase):
    def test_month_digest(self):
        daily = [{"date": "2026-10-16", "sleep_total_h": 5.95, "sleep_score": 71, "sleep_start": "01:41", "sleep_end": "08:03",
                  "resting_hr": 47, "steps": 968, "hrv_last_night": 48, "hrv_status": "BALANCED", "avg_stress": 22,
                  "body_battery_low": 27, "body_battery_high": 85, "readiness_score": 48, "readiness_level": "LOW"},
                 {"date": "2026-09-30", "sleep_total_h": 7}]
        bp = [{"time": "2026-10-16 09:07:28", "systolic": 151, "diastolic": 98, "pulse": 61, "category": "GRADE_1"}]
        weight = [{"time": "2026-10-17 17:13:18", "weight_kg": 73.34, "body_fat_pct": 19.4}]
        acts = [{"start": "2026-10-16 09:54:26", "type": "gravel_cycling", "name": "Ride", "distance_km": "15.97",
                 "duration_min": "49.24", "avg_hr": "129"}]
        md = build_month("2026-10", daily, bp, weight, acts)
        self.assertIn("Данных по дням: 1", md)
        self.assertIn("151/98", md)
        self.assertIn("73.34 кг", md)
        self.assertIn("gravel_cycling 1 шт. 16.0 км", md)
        self.assertNotIn("2026-09-30", md)
        self.assertIn("- 2026-10-16: сон 5.95 ч (оценка 71, 01:41–08:03)", md)

    def test_empty_month(self):
        self.assertIn("Данных по дням: 0", build_month("2026-08", [], [], [], []))


if __name__ == "__main__":
    unittest.main()
