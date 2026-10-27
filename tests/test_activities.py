"""Tests for the read-only GET /activities endpoint in health_sync.scale.ingest."""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from health_sync.scale import ingest

TOKEN = "test-token"


def activity(i, start_local, start_gmt=None, duration=3600.0, elapsed=None, distance=25000.0,
             type_key="cycling"):
    a = {
        "activityId": i,
        "activityType": {"typeKey": type_key},
        "startTimeLocal": start_local,
        "duration": duration,
        "distance": distance,
    }
    if start_gmt:
        a["startTimeGMT"] = start_gmt
    if elapsed is not None:
        a["elapsedDuration"] = elapsed
    return a


class FakeApi:
    """Stands in for the garminconnect client: newest-first slices, records calls, can fail on demand."""

    def __init__(self, activities=None, error=None):
        self.activities = activities or []
        self.error = error
        self.calls = 0

    def get_activities(self, offset, size):
        self.calls += 1
        if self.error:
            raise self.error
        return self.activities[offset:offset + size]


class EndpointTest(unittest.TestCase):
    def start(self, api):
        self.api = api
        self.feed = ingest.ActivitiesFeed(lambda: api)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ingest.make_handler(TOKEN, None, self.feed))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def get(self, path, token=TOKEN):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        try:
            with urlopen(Request(self.base + path, headers=headers), timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()


class AuthTest(EndpointTest):
    def test_missing_and_wrong_bearer_are_401(self):
        self.start(FakeApi([activity(1, "2026-10-27T08:00:00")]))
        self.assertEqual(self.get("/activities", token=None)[0], 401)
        self.assertEqual(self.get("/activities", token="wrong")[0], 401)
        self.assertEqual(self.api.calls, 0)  # rejected before touching Garmin

    def test_health_check_stays_public(self):
        self.start(FakeApi())
        self.assertEqual(self.get("/", token=None), (200, {"ok": True}))


class ShapeTest(EndpointTest):
    def test_success_shape(self):
        self.start(FakeApi([activity(101, "2026-10-27T08:00:00", start_gmt="2026-10-27T05:00:00",
                                     duration=3600.0, elapsed=3700.0, distance=25000.0)]))
        status, body = self.get("/activities")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"activities": [{
            "activityId": 101,
            "type": "cycling",
            "start": "2026-10-27T08:00:00",
            "start_gmt": "2026-10-27T05:00:00",
            "duration_s": 3600,
            "elapsed_s": 3700,
            "distance_km": 25.0,
            "utc_offset_min": 180,
        }]})

    def test_optional_fields_omitted(self):
        self.start(FakeApi([activity(102, "2026-10-27T07:00:00")]))
        status, body = self.get("/activities")
        self.assertEqual(status, 200)
        item = body["activities"][0]
        self.assertNotIn("start_gmt", item)
        self.assertNotIn("elapsed_s", item)
        self.assertIsNone(item["utc_offset_min"])


class FilterTest(EndpointTest):
    def test_since_filter(self):
        self.start(FakeApi([
            activity(3, "2026-10-27T10:00:00"),
            activity(2, "2026-10-27T08:00:00"),
            activity(1, "2026-10-27T06:00:00"),
        ]))
        status, body = self.get("/activities?since=2026-10-27T08:00:00")
        self.assertEqual(status, 200)
        self.assertEqual([a["activityId"] for a in body["activities"]], [3, 2])

    def test_since_with_z_suffix(self):
        self.start(FakeApi([activity(1, "2026-10-27T06:00:00"), activity(2, "2026-10-27T10:00:00")]))
        status, body = self.get("/activities?since=2026-10-27T08:00:00Z")
        self.assertEqual(status, 200)
        self.assertEqual([a["activityId"] for a in body["activities"]], [2])

    def test_default_limit_and_clamp(self):
        acts = [activity(i, f"2026-10-27T{10 - i // 10:02d}:{59 - i % 10:02d}:00") for i in range(40)]
        self.start(FakeApi(acts))
        self.assertEqual(len(self.get("/activities")[1]["activities"]), 10)  # default
        self.assertEqual(len(self.get("/activities?limit=5")[1]["activities"]), 5)
        self.assertEqual(len(self.get("/activities?limit=999")[1]["activities"]), 30)  # max 30

    def test_bad_limit_is_400(self):
        self.start(FakeApi())
        self.assertEqual(self.get("/activities?limit=abc")[0], 400)


class CacheTest(EndpointTest):
    def test_two_requests_hit_garmin_once(self):
        self.start(FakeApi([activity(1, "2026-10-27T08:00:00")]))
        self.assertEqual(self.get("/activities")[0], 200)
        self.assertEqual(self.get("/activities?limit=5&since=2026-10-27T00:00:00")[0], 200)
        self.assertEqual(self.api.calls, 1)

    def test_cache_expires_after_ttl(self):
        api = FakeApi([activity(1, "2026-10-27T08:00:00")])
        feed = ingest.ActivitiesFeed(lambda: api)
        with patch("health_sync.scale.ingest.time.monotonic", side_effect=[100.0, 100.0, 161.0]):
            feed.get()
            feed.get()  # within 60 s: cached
            feed.get()  # after 61 s: refetched
        self.assertEqual(api.calls, 2)


class GarminErrorTest(EndpointTest):
    def test_client_error_is_503_and_not_cached(self):
        api = FakeApi(error=RuntimeError("login failed for user@example.com"))
        self.start(api)
        status, body = self.get("/activities")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "garmin unavailable"})
        self.get("/activities")
        self.assertEqual(api.calls, 2)  # a failure is retried, not served from cache


if __name__ == "__main__":
    unittest.main()
