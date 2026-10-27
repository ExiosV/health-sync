"""Tests for the read-only GET /activities endpoint in health_sync.scale.ingest."""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
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


class UploadApi:
    def __init__(self):
        self.uploads = []

    def add_body_composition(self, *args, **kwargs):
        self.uploads.append((args, kwargs))


class ServerCase(unittest.TestCase):
    def start(self, api, svc=None, token=TOKEN, reset=None):
        self.api = api
        self.feed = ingest.ActivitiesFeed(lambda: api, reset=reset)
        server = ThreadingHTTPServer(("127.0.0.1", 0), ingest.make_handler(token, svc, self.feed))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(5)))
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def request(self, path, token=TOKEN, method="GET", body=None):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def get(self, path, token=TOKEN):
        return self.request(path, token=token)


class AuthTest(ServerCase):
    def test_missing_and_wrong_bearer_are_401(self):
        self.start(FakeApi([activity(1, "2026-10-27T08:00:00")]))
        self.assertEqual(self.get("/activities", token=None)[0], 401)
        self.assertEqual(self.get("/activities", token="wrong")[0], 401)
        self.assertEqual(self.api.calls, 0)  # rejected before touching Garmin

    def test_empty_configured_token_fails_closed(self):
        self.start(FakeApi(), token="")
        # "Authorization: Bearer " with an empty configured token must not authenticate either endpoint.
        self.assertEqual(self.request("/activities", token="")[0], 401)
        self.assertEqual(self.request("/activities", token=None)[0], 401)
        self.assertEqual(self.request("/scale", token="", method="POST", body={})[0], 401)
        self.assertEqual(self.api.calls, 0)

    def test_health_check_stays_public(self):
        self.start(FakeApi())
        self.assertEqual(self.get("/", token=None), (200, {"ok": True}))

    def test_unknown_get_path_is_404(self):
        self.start(FakeApi())
        self.assertEqual(self.get("/nope")[0], 404)


class ShapeTest(ServerCase):
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
            "is_ride": True,
            "end_local": "2026-10-27T09:01:40",
            "end_gmt": "2026-10-27T06:01:40",
        }]})

    def test_optional_fields_and_end_fallback_to_duration(self):
        self.start(FakeApi([
            activity(102, "2026-10-27T07:00:00", duration=1800.0, type_key="running"),
        ]))
        status, body = self.get("/activities")
        self.assertEqual(status, 200)
        item = body["activities"][0]
        self.assertNotIn("start_gmt", item)
        self.assertNotIn("elapsed_s", item)
        self.assertNotIn("end_gmt", item)
        self.assertIsNone(item["utc_offset_min"])
        self.assertFalse(item["is_ride"])
        self.assertEqual(item["end_local"], "2026-10-27T07:30:00")  # start + duration_s

    def test_ride_type_flag(self):
        self.start(FakeApi([
            activity(1, "2026-10-27T08:00:00", type_key="e_bike_mountain"),
            activity(2, "2026-10-27T07:00:00", type_key="walking"),
        ]))
        body = self.get("/activities")[1]
        self.assertEqual([a["is_ride"] for a in body["activities"]], [True, False])


class FilterTest(ServerCase):
    ROWS = [
        activity(3, "2026-10-27T10:00:00", start_gmt="2026-10-27T07:00:00"),
        activity(2, "2026-10-27T08:00:00", start_gmt="2026-10-27T05:00:00"),
        activity(1, "2026-10-27T06:00:00", start_gmt="2026-10-27T03:00:00"),
    ]

    def ids(self, since):
        self.start(FakeApi(self.ROWS))
        status, body = self.get(f"/activities?since={quote(since)}")
        self.assertEqual(status, 200, body)
        return [a["activityId"] for a in body["activities"]]

    def test_naive_since_uses_local_wall_time(self):
        self.assertEqual(self.ids("2026-10-27T08:00:00"), [3, 2])

    def test_z_since_compares_in_utc(self):
        self.assertEqual(self.ids("2026-10-27T05:00:00Z"), [3, 2])
        self.assertEqual(self.ids("2026-10-27T06:00:00Z"), [3])  # 05:00 UTC is before this

    def test_offset_since_converts_to_utc(self):
        self.assertEqual(self.ids("2026-10-27T08:00:00+03:00"), [3, 2])  # == 05:00 UTC
        self.assertEqual(self.ids("2026-10-27T09:00:00+03:00"), [3])     # == 06:00 UTC

    def test_garbage_since_is_400(self):
        self.start(FakeApi(self.ROWS))
        for bad in ("yesterday", "2026-13-45T00:00:00", "2026-10-27T99:00:00"):
            self.assertEqual(self.get(f"/activities?since={bad}")[0], 400, bad)

    def test_default_limit_and_clamp(self):
        acts = [activity(i, f"2026-10-27T{10 - i // 10:02d}:{59 - i % 10:02d}:00") for i in range(40)]
        self.start(FakeApi(acts))
        self.assertEqual(len(self.get("/activities")[1]["activities"]), 10)  # default
        self.assertEqual(len(self.get("/activities?limit=5")[1]["activities"]), 5)
        self.assertEqual(len(self.get("/activities?limit=999")[1]["activities"]), 30)  # max 30

    def test_bad_limit_is_400(self):
        self.start(FakeApi())
        self.assertEqual(self.get("/activities?limit=abc")[0], 400)


class CacheTest(ServerCase):
    def test_two_requests_hit_garmin_once(self):
        self.start(FakeApi([activity(1, "2026-10-27T08:00:00")]))
        self.assertEqual(self.get("/activities")[0], 200)
        self.assertEqual(self.get("/activities?limit=5&since=2026-10-27T00:00:00")[0], 200)
        self.assertEqual(self.api.calls, 1)

    def test_success_cache_expires_after_120_s(self):
        api = FakeApi([activity(1, "2026-10-27T08:00:00")])
        feed = ingest.ActivitiesFeed(lambda: api)
        clock = [100.0]
        with patch("health_sync.scale.ingest.time.monotonic", side_effect=lambda: clock[0]):
            feed.get()
            clock[0] = 219.0
            feed.get()  # 119 s: cached
            self.assertEqual(api.calls, 1)
            clock[0] = 221.0
            feed.get()  # 121 s: refetched
            self.assertEqual(api.calls, 2)

    def test_failure_is_cached_for_30_s(self):
        api = FakeApi(error=RuntimeError("boom"))
        feed = ingest.ActivitiesFeed(lambda: api)
        clock = [0.0]
        with patch("health_sync.scale.ingest.time.monotonic", side_effect=lambda: clock[0]):
            for _ in range(2):
                with self.assertRaises(ingest.ActivitiesUnavailable):
                    feed.get()
            self.assertEqual(api.calls, 1)  # the second request is served from the error cache
            clock[0] = 31.0
            with self.assertRaises(ingest.ActivitiesUnavailable):
                feed.get()
            self.assertEqual(api.calls, 2)  # retried after ERROR_TTL_S


class ResetTest(unittest.TestCase):
    def run_feed(self, error):
        resets = []
        feed = ingest.ActivitiesFeed(lambda: FakeApi(error=error), reset=lambda: resets.append(True))
        with self.assertRaises(ingest.ActivitiesUnavailable):
            feed.get()
        return resets

    def test_auth_looking_failure_resets_the_shared_client(self):
        self.assertEqual(len(self.run_feed(RuntimeError("401 Unauthorized"))), 1)
        self.assertEqual(len(self.run_feed(RuntimeError("login failed, token expired"))), 1)

    def test_other_failure_keeps_the_client(self):
        self.assertEqual(self.run_feed(RuntimeError("connection reset by peer")), [])


class GarminErrorTest(ServerCase):
    def test_client_error_is_503_with_a_generic_body(self):
        self.start(FakeApi(error=RuntimeError("login failed for user@example.com")))
        status, body = self.get("/activities")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "garmin unavailable"})
        self.get("/activities")
        self.assertEqual(self.api.calls, 1)  # error cached for 30 s, not retried on every poll


class PostScaleTest(ServerCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ingest, "DATA_DIR", ingest.DATA_DIR)
        ingest.DATA_DIR = Path(self.tmp.name)
        for name in ("SCALE_PERSONS", "SCALE_GARMIN_PERSONS"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.uploader = UploadApi()
        self.start(FakeApi(), svc=ingest.ScaleIngest(lambda: self.uploader))

    def test_post_scale_still_works(self):
        self.assertEqual(self.request("/scale", token="wrong", method="POST",
                                      body={"weight_kg": 80.0})[0], 401)
        status, body = self.request("/scale", method="POST", body={"timestamp": "t1", "weight_kg": 80.0})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "uploaded")
        self.assertEqual(len(self.uploader.uploads), 1)


class ServeGuardTest(unittest.TestCase):
    def test_empty_token_refuses_to_start(self):
        with patch.dict(os.environ, {"SCALE_INGEST_TOKEN": ""}):
            with self.assertRaises(RuntimeError):
                ingest.serve(0)

    def test_missing_token_refuses_to_start(self):
        env = {k: v for k, v in os.environ.items() if k != "SCALE_INGEST_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                ingest.serve(0)


if __name__ == "__main__":
    unittest.main()
