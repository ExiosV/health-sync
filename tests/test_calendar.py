import unittest
from datetime import datetime, timezone

from google_kit import GoogleError

from health_sync.connectors.garmin_calendar import normalize_sleep
from health_sync.connectors.garmin_health import normalize_naps
from health_sync.connectors.gcal import GoogleCalendarSink, build_event, event_id
from health_sync.core.model import Record


class FakeCalendar:
    """Stand-in for google_kit.Calendar: records writes and raises queued errors."""

    def __init__(self, calendars=None, errors=None):
        self.calendars = calendars or []
        self.errors = list(errors or [])
        self.calls = []

    def list_calendars(self):
        return self.calendars

    def upsert_event(self, calendar_id, event, *, send_updates="none"):
        self.calls.append((calendar_id, event))
        if self.errors:
            error = self.errors.pop(0)
            if error:
                raise error
        return {"id": event["id"]}


def ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def sleep_record():
    payload = {"start": "2026-10-16 23:10:00", "end": "2026-10-17 07:00:00", "total_h": 7.5,
               "deep_h": 1.2, "rem_h": 1.5, "score": 80}
    return Record("sleep", "garmin", "2026-10-16", payload["start"], payload)


def activity_record():
    payload = {"name": "Утренний бег", "type": "running", "distance_km": 10.0, "duration_min": 50.0,
               "avg_hr": 150, "max_hr": 170, "pace_min_km": 5.0, "calories": 700, "te_aerobic": 3.2,
               "te_anaerobic": 0, "te_label": "TEMPO"}
    return Record("activity", "garmin", "12345", "2026-10-17 07:00:00", payload)


class EventIdTest(unittest.TestCase):
    def test_deterministic_and_calendar_safe(self):
        a = event_id("garmin", "sleep", "2026-10-16")
        self.assertEqual(a, event_id("garmin", "sleep", "2026-10-16"))
        self.assertNotEqual(a, event_id("garmin", "sleep", "2026-10-17"))
        self.assertGreaterEqual(len(a), 5)
        self.assertTrue(set(a) <= set("0123456789abcdefghijklmnopqrstuv"))


class BuildEventTest(unittest.TestCase):
    def test_sleep_event_span_and_tag(self):
        event = build_event(sleep_record(), prefix="Garmin: ")
        self.assertEqual(event["summary"], "Garmin: Сон")
        self.assertEqual(event["start"]["dateTime"], "2026-10-16T23:10:00")
        self.assertEqual(event["end"]["dateTime"], "2026-10-17T07:00:00")
        self.assertEqual(event["extendedProperties"]["private"]["healthSyncKind"], "sleep")
        self.assertIn("7.5 ч", event["description"])

    def test_activity_end_from_duration(self):
        event = build_event(activity_record())
        self.assertEqual(event["summary"], "Утренний бег")
        self.assertEqual(event["start"]["dateTime"], "2026-10-17T07:00:00")
        self.assertEqual(event["end"]["dateTime"], "2026-10-17T07:50:00")
        self.assertIn("running", event["description"])
        self.assertIn("https://connect.garmin.com/modern/activity/12345", event["description"])

    def test_timezone_and_flags_applied(self):
        event = build_event(activity_record(), timezone="Europe/Moscow", visibility="private",
                            transparency="transparent")
        self.assertEqual(event["start"]["timeZone"], "Europe/Moscow")
        self.assertEqual((event["visibility"], event["transparency"]), ("private", "transparent"))

    def test_record_without_time_is_skipped(self):
        self.assertIsNone(build_event(Record("activity", "garmin", "x", "", {"duration_min": 10})))


class BuildEventEnvTest(unittest.TestCase):
    def test_no_timezone_leaves_it_to_the_calendar(self):
        event = build_event(sleep_record())
        self.assertNotIn("timeZone", event["start"])


def weight_record(start="2026-10-17 07:05:00"):
    payload = {"weight_kg": 78.5, "bmi": 24.1, "body_fat_pct": 18.2, "body_water_pct": 55.0,
               "muscle_mass_kg": 60.1, "bone_mass_kg": 3.2}
    return Record("weight", "garmin", "w1", start, payload)


def pressure_record(start="2026-10-17 08:15:00"):
    payload = {"systolic": 122, "diastolic": 78, "pulse": 64, "category": "NORMAL"}
    return Record("blood_pressure", "garmin", "b1", start, payload)


def daily_record():
    payload = {"steps": 9123, "distance_km": 7.4, "resting_hr": 54, "min_hr": 48, "max_hr": 131,
               "avg_stress": 27, "max_stress": 61, "body_battery_high": 98, "body_battery_low": 44,
               "sleep_total_h": 7.5, "sleep_start": "23:10", "sleep_end": "07:00", "sleep_score": 80,
               "hrv_last_night": 62, "hrv_status": "BALANCED", "spo2_avg": 96, "spo2_low": 90,
               "respiration_avg": 14, "readiness_score": 81, "readiness_level": "HIGH",
               "recovery_time_h": 12.0, "floors_up": 11, "active_kcal": 640}
    return Record("daily", "garmin", "2026-10-17", "2026-10-17", payload)


class MeasurementsTest(unittest.TestCase):
    def test_weight_is_a_short_timed_event(self):
        event = build_event(weight_record(), timezone="Europe/Moscow")
        self.assertEqual(event["summary"], "Вес: 78.5 кг")
        self.assertEqual(event["start"]["dateTime"], "2026-10-17T07:05:00")
        self.assertEqual(event["end"]["dateTime"], "2026-10-17T07:15:00")
        self.assertEqual(event["start"]["timeZone"], "Europe/Moscow")
        self.assertIn("Жир: 18.2 %", event["description"])

    def test_blood_pressure(self):
        event = build_event(pressure_record(), point_minutes=5)
        self.assertEqual(event["summary"], "Давление: 122/78")
        self.assertEqual(event["end"]["dateTime"], "2026-10-17T08:20:00")
        self.assertIn("Пульс: 64", event["description"])

    def test_measurement_without_time_is_all_day(self):
        event = build_event(weight_record(start="2026-10-17"))
        self.assertEqual(event["start"], {"date": "2026-10-17"})
        self.assertEqual(event["end"], {"date": "2026-10-18"})
        self.assertNotIn("dateTime", event["start"])

    def test_daily_summary_is_all_day(self):
        event = build_event(daily_record())
        self.assertEqual(event["summary"], "Здоровье за день")
        self.assertEqual(event["start"], {"date": "2026-10-17"})
        self.assertEqual(event["end"], {"date": "2026-10-18"})
        self.assertEqual(event["extendedProperties"]["private"]["healthSyncKind"], "daily")
        for expected in ("Шаги: 9123", "HRV: 62", "Body Battery", "Готовность: 81", "SpO2: 96"):
            self.assertIn(expected, event["description"])

    def test_ids_differ_per_kind_and_source_but_are_stable(self):
        self.assertNotEqual(build_event(weight_record())["id"], build_event(pressure_record())["id"])
        self.assertEqual(build_event(weight_record())["id"], build_event(weight_record())["id"])

    def test_default_kinds_include_measurements(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALENDAR_KINDS", None)
            sink = GoogleCalendarSink(calendar_id="cal1", client=FakeCalendar())
        self.assertEqual(sink.kinds, {"activity", "sleep", "nap", "weight", "blood_pressure", "daily"})

    def test_sink_writes_measurements(self):
        sink = GoogleCalendarSink(kinds=("weight", "blood_pressure"), calendar_id="cal1",
                                  client=FakeCalendar())
        self.assertEqual(sink.write([weight_record(), pressure_record(), daily_record()]), 2)


def nap_record(start="2026-10-11 15:35:42", end="2026-10-11 16:35:42"):
    payload = {"start": start, "end": end, "duration_min": 60,
               "feedback": "LONG_DURATION_LOW_NEED"}
    return Record("nap", "garmin", f"2026-10-11:{start}", start, payload)


class NapTest(unittest.TestCase):
    def test_nap_is_a_timed_event(self):
        event = build_event(nap_record(), prefix="Garmin: ")
        self.assertEqual(event["summary"], "Garmin: Дневной сон")
        self.assertEqual(event["start"]["dateTime"], "2026-10-11T15:35:42")
        self.assertEqual(event["end"]["dateTime"], "2026-10-11T16:35:42")
        self.assertEqual(event["extendedProperties"]["private"]["healthSyncKind"], "nap")
        self.assertIn("60 мин", event["description"])
        self.assertIn("низкой потребности", event["description"])

    def test_nap_falls_back_to_duration(self):
        event = build_event(Record("nap", "garmin", "n1", "2026-10-11 15:35:00",
                                   {"duration_min": 30}))
        self.assertEqual(event["end"]["dateTime"], "2026-10-11T16:05:00")

    def test_normalize_naps_applies_its_own_offset(self):
        sleep = {"dailySleepDTO": {"napTimeSeconds": 3600, "dailyNapDTOS": [{
            "napStartTimestampGMT": "2026-10-11T12:35:42",
            "napEndTimestampGMT": "2026-10-11T13:35:42",
            "napTimeSec": 3600, "napStartTimeOffset": 180,
            "napFeedback": "LONG_DURATION_LOW_NEED"}]}}
        naps = normalize_naps(sleep)
        self.assertEqual(len(naps), 1)
        self.assertEqual((naps[0]["start"], naps[0]["end"]), ("2026-10-11 15:35:42", "2026-10-11 16:35:42"))
        self.assertEqual(naps[0]["duration_min"], 60)

    def test_nap_list_missing(self):
        self.assertEqual(normalize_naps({"dailySleepDTO": {}}), [])


class OffsetTest(unittest.TestCase):
    def test_timed_event_with_offset_is_exact_and_ignores_the_configured_zone(self):
        rec = activity_record()
        rec.payload["utc_offset_min"] = 120  # e.g. Cyprus in winter
        event = build_event(rec, timezone="Europe/Moscow")
        self.assertEqual(event["start"], {"dateTime": "2026-10-17T07:00:00+02:00"})
        self.assertEqual(event["end"], {"dateTime": "2026-10-17T07:50:00+02:00"})

    def test_negative_and_half_hour_offsets(self):
        from health_sync.core.timeutil import format_offset
        self.assertEqual(format_offset(-300), "-05:00")
        self.assertEqual(format_offset(330), "+05:30")
        self.assertEqual(format_offset(0), "+00:00")

    def test_sleep_nap_weight_pressure_use_their_offset(self):
        sleep = sleep_record()
        sleep.payload["utc_offset_min"] = 180
        self.assertTrue(build_event(sleep)["start"]["dateTime"].endswith("+03:00"))
        w = Record("weight", "garmin", "1", "2026-10-17 10:00:00", {"weight_kg": 80, "utc_offset_min": 120})
        self.assertEqual(build_event(w)["start"]["dateTime"], "2026-10-17T10:00:00+02:00")
        bp = Record("blood_pressure", "garmin", "t", "2026-10-17 10:00:00",
                    {"systolic": 120, "diastolic": 80, "utc_offset_min": -60})
        self.assertEqual(build_event(bp)["end"]["dateTime"], "2026-10-17T10:10:00-01:00")

    def test_no_offset_falls_back_to_the_zone(self):
        event = build_event(activity_record(), timezone="Europe/Moscow")
        self.assertEqual(event["start"], {"dateTime": "2026-10-17T07:00:00", "timeZone": "Europe/Moscow"})

    def test_activity_location_in_description(self):
        rec = activity_record()
        rec.payload["location"] = "Limassol"
        self.assertIn("Место: Limassol", build_event(rec)["description"])

    def test_offset_derived_from_local_and_gmt(self):
        from health_sync.core.timeutil import offset_minutes
        self.assertEqual(offset_minutes("2026-10-18 15:54:12", "2026-10-18 12:54:12"), 180)
        self.assertEqual(offset_minutes(1792373839000, 1792363039000), 180)
        self.assertEqual(offset_minutes("2026-10-19T09:22:02.0", "2026-10-19T06:22:02.0"), 180)
        self.assertEqual(offset_minutes("2026-01-05 08:00:00", "2026-01-05 06:00:00"), 120)  # winter vs summer differ
        self.assertIsNone(offset_minutes("", "2026-10-18 12:54:12"))
        self.assertIsNone(offset_minutes("2026-10-18 15:54:12", "2026-10-17 12:54:12"))  # not a zone offset


class TodaySleepTest(unittest.TestCase):
    def _source(self, sleep):
        from health_sync.connectors.garmin_calendar import GarminCalendarSource

        class Api:
            def get_sleep_data(self, d):
                return sleep

        src = GarminCalendarSource()
        src._api = Api()
        return src

    def test_finished_sleep_is_read_the_same_morning(self):
        import time
        now = int(time.time() * 1000)
        dto = {"sleepStartTimestampLocal": now - 8 * 3600_000, "sleepEndTimestampLocal": now - 1000,
               "sleepStartTimestampGMT": now - 8 * 3600_000 - 7200_000, "sleepEndTimestampGMT": now - 1000 - 7200_000,
               "sleepTimeSeconds": 28000}
        records = list(self._source({"dailySleepDTO": dto})._today_sleep())
        self.assertEqual([r.kind for r in records], ["sleep"])
        self.assertEqual(records[0].payload["utc_offset_min"], 120)

    def test_unfinished_or_missing_sleep_is_skipped(self):
        import time
        future = int(time.time() * 1000) + 3600_000
        self.assertEqual(list(self._source({"dailySleepDTO": {"sleepEndTimestampGMT": future}})._today_sleep()), [])
        self.assertEqual(list(self._source({})._today_sleep()), [])


class UpsertTest(unittest.TestCase):
    def test_event_is_upserted_under_its_deterministic_id(self):
        calendar = FakeCalendar()
        sink = GoogleCalendarSink(kinds=("sleep",), calendar_id="cal1", client=calendar)
        self.assertEqual(sink.write([sleep_record()]), 1)
        calendar_id, event = calendar.calls[0]
        self.assertEqual(calendar_id, "cal1")
        self.assertEqual(event["id"], event_id("garmin", "sleep", "2026-10-16"))

    def test_throttled_write_is_retried(self):
        from unittest import mock

        throttled = GoogleError("HTTP 403", status=403,
                                body='{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}')
        calendar = FakeCalendar(errors=[throttled])
        sink = GoogleCalendarSink(kinds=("sleep",), calendar_id="cal1", client=calendar)
        with mock.patch("health_sync.connectors.gcal.time.sleep") as slept:
            self.assertEqual(sink.write([sleep_record()]), 1)
        self.assertEqual(len(calendar.calls), 2)
        self.assertTrue(slept.called)

    def test_pacing_between_events(self):
        from unittest import mock

        calendar = FakeCalendar()
        sink = GoogleCalendarSink(kinds=("sleep",), calendar_id="cal1", client=calendar)
        with mock.patch("health_sync.connectors.gcal.time.sleep") as slept:
            self.assertEqual(sink.write([sleep_record(), sleep_record()]), 2)
        slept.assert_called_once_with(sink.write_delay)  # only between writes, not before the first

    def test_insert_failure_is_raised(self):
        calendar = FakeCalendar(errors=[GoogleError("HTTP 400 bad body", status=400, body="bad body")])
        sink = GoogleCalendarSink(kinds=("sleep",), calendar_id="cal1", client=calendar)
        with self.assertRaises(RuntimeError):
            sink.write([sleep_record()])


class CalendarCursorTest(unittest.TestCase):
    def test_cursor_never_moves_inside_today(self):
        from datetime import date, timedelta

        from health_sync.connectors.garmin_calendar import GarminCalendarSource

        source = GarminCalendarSource()
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(source.cursor(f"{today} 08:15:00"), yesterday)
        self.assertEqual(source.cursor("2026-01-01"), "2026-01-01")

    def test_first_run_window_limits_activities(self):
        import os
        from datetime import date, timedelta
        from unittest import mock

        from health_sync.connectors.garmin_calendar import GarminCalendarSource

        source = GarminCalendarSource()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GARMIN_CALENDAR_DAYS", None)
            os.environ.pop("GARMIN_CALENDAR_ACTIVITY_DAYS", None)
            self.assertEqual(source._cutoff(None), (date.today() - timedelta(days=7)).isoformat())
        with mock.patch.dict(os.environ, {"GARMIN_CALENDAR_ACTIVITY_DAYS": "0"}):
            self.assertIsNone(source._cutoff(None))  # the whole activity history
        self.assertEqual(source._cutoff("2026-01-01"), "2026-01-01")


class NormalizeSleepTest(unittest.TestCase):
    def test_flattens_dto(self):
        sleep = {"dailySleepDTO": {
            "sleepStartTimestampLocal": ms(datetime(2026, 10, 16, 23, 10)),
            "sleepEndTimestampLocal": ms(datetime(2026, 10, 17, 7, 0)),
            "sleepTimeSeconds": 27000, "deepSleepSeconds": 3600,
            "sleepScores": {"overall": {"value": 80}}}}
        p = normalize_sleep(sleep)
        self.assertEqual((p["start"], p["end"], p["total_h"], p["deep_h"], p["score"]),
                         ("2026-10-16 23:10:00", "2026-10-17 07:00:00", 7.5, 1.0, 80))

    def test_empty_sleep(self):
        self.assertEqual(normalize_sleep({})["start"], "")


class SinkTest(unittest.TestCase):
    def test_resolves_calendar_by_name_and_inserts(self):
        calendar = FakeCalendar(calendars=[{"id": "diary@group.calendar.google.com", "summary": "Дневник"}])
        sink = GoogleCalendarSink(kinds=("sleep",), calendar_name="дневник", client=calendar)
        self.assertEqual(sink.write([sleep_record()]), 1)
        self.assertEqual(sink.calendar(), "diary@group.calendar.google.com")
        calendar_id, event = calendar.calls[-1]
        self.assertEqual(calendar_id, "diary@group.calendar.google.com")
        self.assertEqual(event["id"], event_id("garmin", "sleep", "2026-10-16"))

    def test_other_kinds_are_ignored(self):
        sink = GoogleCalendarSink(kinds=("sleep",), calendar_id="cal1", client=FakeCalendar())
        self.assertEqual(sink.write([activity_record()]), 0)


if __name__ == "__main__":
    unittest.main()
