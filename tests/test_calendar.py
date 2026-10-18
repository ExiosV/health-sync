import unittest
from datetime import datetime, timezone

from health_sync.connectors.garmin_calendar import normalize_sleep
from health_sync.connectors.gcal import GoogleCalendarSink, build_event, event_id
from health_sync.core.model import Record


class Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code, self._payload = status_code, payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def _call(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def patch(self, url, **kw):
        return self._call("PATCH", url, **kw)


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
        session = FakeSession([Resp(200, {"items": [{"id": "diary@group.calendar.google.com", "summary": "Дневник"}]}),
                               Resp(200)])
        sink = GoogleCalendarSink(kinds=("sleep",), calendar_name="дневник", session=session)
        self.assertEqual(sink.write([sleep_record()]), 1)
        self.assertEqual(sink.calendar(), "diary@group.calendar.google.com")
        method, url, kw = session.calls[-1]
        self.assertEqual(method, "POST")
        self.assertIn("/calendars/diary%40group.calendar.google.com/events", url)
        self.assertEqual(kw["json"]["id"], event_id("garmin", "sleep", "2026-10-16"))

    def test_existing_event_is_updated_not_duplicated(self):
        session = FakeSession([Resp(200, {"items": [{"id": "cal1", "summary": "Дневник"}]}),
                               Resp(409), Resp(200)])
        sink = GoogleCalendarSink(kinds=("sleep",), calendar_name="Дневник", session=session)
        self.assertEqual(sink.write([sleep_record()]), 1)
        self.assertEqual([c[0] for c in session.calls], ["GET", "POST", "PATCH"])
        self.assertNotIn("id", session.calls[-1][2]["json"])

    def test_other_kinds_are_ignored(self):
        sink = GoogleCalendarSink(kinds=("sleep",), calendar_id="cal1", session=FakeSession([]))
        self.assertEqual(sink.write([activity_record()]), 0)


if __name__ == "__main__":
    unittest.main()
