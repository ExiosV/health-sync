import unittest

from health_sync.connectors.garmin_health import normalize_daily
from health_sync.connectors.gsheets import DAILY_HEADER, HEADER, TABLES, activity_row, format_pace
from health_sync.core.model import Record


class SheetsTest(unittest.TestCase):
    def test_pace(self):
        self.assertEqual(format_pace(5.37), "5:22")
        self.assertEqual(format_pace(0), "0:00")
        self.assertEqual(format_pace(5.999), "6:00")

    def test_row_layout(self):
        p = dict(name="Run", type="running", distance_km=10.0, duration_min=50.0, avg_hr=150, max_hr=170,
                 pace_min_km=5.0, cadence_avg=170, cadence_max=180, te_aerobic=3.2, te_anaerobic=0,
                 te_label="TEMPO", vo2max=50, calories=700, hr_zones={}, elevation_gain_m=10, elevation_loss_m=12)
        row = activity_row(Record("activity", "garmin", "1", "2026-01-01 07:00:00", p), "now")
        self.assertEqual(row[1], "2026-01-01 07:00:00")
        self.assertEqual(len(row), 19)
        self.assertEqual(len(row), len(HEADER))

    def test_daily_row_matches_header(self):
        p = normalize_daily({"user_summary": {"totalSteps": 9000, "restingHeartRate": 55},
                             "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27000, "sleepScores": {"overall": {"value": 80}}}},
                             "hrv": {"hrvSummary": {"lastNightAvg": 60, "status": "BALANCED"}}})
        row = TABLES["daily"].row(Record("daily", "garmin", "2026-10-16", "2026-10-16", p), "now")
        self.assertEqual(len(row), len(DAILY_HEADER))
        self.assertEqual((row[0], row[1], row[4], row[16], row[17]), ("2026-10-16", 9000, 55, 80, 7.5))
        self.assertEqual(normalize_daily({})["steps"], "")


if __name__ == "__main__":
    unittest.main()
