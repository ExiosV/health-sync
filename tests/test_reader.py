"""Tests for health_sync.scale.reader idle-exit logic."""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

try:  # bleak is only installed where the reader runs (Windows host); the tests replace the scanner anyway
    import bleak  # noqa: F401
except ImportError:
    import sys
    import types
    sys.modules["bleak"] = types.SimpleNamespace(BleakScanner=None)

from health_sync.scale.reader import (  # noqa: E402
    IDLE_EXIT_CODE, SETTLE_S, is_idle, load_pending, post, prune, run, save_pending_file,
)


class IdleLogicTest(unittest.TestCase):
    def test_is_idle_disabled_when_zero(self):
        self.assertFalse(is_idle(0.0, 100.0, 0))
        self.assertFalse(is_idle(0.0, 100.0, -1))

    def test_is_idle_false_when_recent(self):
        self.assertFalse(is_idle(50.0, 100.0, 60))  # 50s ago < 60s

    def test_is_idle_true_when_expired(self):
        self.assertTrue(is_idle(30.0, 100.0, 60))   # 70s ago > 60s

    def test_is_idle_exact_boundary(self):
        self.assertFalse(is_idle(40.0, 100.0, 60))  # exactly 60s ago -> not idle yet
        self.assertTrue(is_idle(39.9, 100.0, 60))   # 60.1s ago -> idle


class PostTest(unittest.TestCase):
    def test_post_headers_and_call(self):
        with patch("urllib.request.urlopen") as mock_open, \
             patch("urllib.request.Request") as mock_req_class:
            mock_req = MagicMock()
            mock_req_class.return_value = mock_req
            post("http://example.com/scale", "mytoken", {"weight_kg": 70.0}, "myhost")
            mock_req_class.assert_called_once()
            call_args, call_kwargs = mock_req_class.call_args
            self.assertEqual(call_args[0], "http://example.com/scale")
            # Request signature: Request(url, data=None, headers={}, ...)
            self.assertEqual(call_args[2]["Authorization"], "Bearer mytoken")
            self.assertEqual(call_args[2]["Host"], "myhost")
            self.assertEqual(call_args[2]["Content-Type"], "application/json")
            mock_open.assert_called_once_with(mock_req, timeout=20)


class SimClock:
    """Fake clock: time() is frozen, the patched asyncio.sleep advances it and fires scheduled events."""

    def __init__(self, start: float = 1000.0):
        self.start = start
        self.now = start
        self.events: list[tuple[float, object]] = []  # (offset from start, callable)

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        due = [e for e in self.events if self.start + e[0] <= self.now]
        self.events = [e for e in self.events if e not in due]
        for _, fn in due:
            fn()


class FakeScanner:
    """Stands in for BleakScanner: captures the on_adv callback and records enter/exit."""

    instances: list["FakeScanner"] = []

    def __init__(self, callback, **kwargs):
        self.callback = callback
        self.entered = False
        self.exited = False
        FakeScanner.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return None


class SimulatedRunTest(unittest.TestCase):
    def setUp(self):
        FakeScanner.instances = []

    def _run(self, clock: SimClock, **kwargs) -> int:
        args = dict(address="AA:BB:CC:DD:EE:FF", scan=None, url=None, token="", host=None,
                    pending_file=None, idle_exit=5)
        args.update(kwargs)
        with patch("health_sync.scale.reader.BleakScanner", FakeScanner), \
             patch("health_sync.scale.reader.time.time", side_effect=clock.time), \
             patch("health_sync.scale.reader.asyncio.sleep", side_effect=clock.sleep):
            return asyncio.run(run(**args))

    def test_idle_exit_without_adverts_closes_scanner(self):
        clock = SimClock()
        code = self._run(clock)
        self.assertEqual(code, IDLE_EXIT_CODE)
        self.assertEqual(clock.now - clock.start, 6)  # 6 s of silence > 5 s
        self.assertEqual(len(FakeScanner.instances), 1)
        self.assertTrue(FakeScanner.instances[0].entered)
        self.assertTrue(FakeScanner.instances[0].exited)

    def test_scan_timeout_before_idle_returns_0(self):
        clock = SimClock()
        code = self._run(clock, scan=5, idle_exit=10)
        self.assertEqual(code, 0)
        self.assertEqual(clock.now - clock.start, 5)
        self.assertTrue(FakeScanner.instances[0].exited)

    def test_advert_from_any_device_resets_idle_timer(self):
        clock = SimClock()
        fire = lambda: FakeScanner.instances[0].callback(  # noqa: E731
            SimpleNamespace(address="11:22:33:44:55:66"), SimpleNamespace(service_data={}))
        clock.events = [(4, fire), (8, fire)]
        code = self._run(clock)
        self.assertEqual(code, IDLE_EXIT_CODE)
        self.assertEqual(clock.now - clock.start, 14)  # last advert at +8, idle after 6 more s
        self.assertTrue(FakeScanner.instances[0].exited)

    def test_measurement_goes_to_pending_file_when_upload_fails(self):
        clock = SimClock()
        m = SimpleNamespace(timestamp=SimpleNamespace(isoformat=lambda: "2026-10-26T08:00:00"),
                            weight_kg=80.5, impedance=500, stabilized=True, removed=False)
        fire = lambda: FakeScanner.instances[0].callback(  # noqa: E731
            SimpleNamespace(address="aa:bb:cc:dd:ee:ff"),
            SimpleNamespace(service_data={"svc": b"\x01"}))
        clock.events = [(1, fire)]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pending.jsonl")
            with patch("health_sync.scale.reader.SERVICE_UUID", "svc"), \
                 patch("health_sync.scale.reader.parse", return_value=m), \
                 patch("health_sync.scale.reader.post", side_effect=OSError("down")):
                code = self._run(clock, url="http://x/scale", pending_file=path, scan=SETTLE_S + 20, idle_exit=0)
            self.assertEqual(code, 0)
            rows = load_pending(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["weight_kg"], 80.5)
            self.assertFalse(os.path.exists(path + ".tmp"))


class PruneTest(unittest.TestCase):
    def test_best_older_than_max_age_is_dropped(self):
        best = {"old": (0.0, None), "new": (3000.0, None)}
        prune(best, set(), now=3700.0)
        self.assertEqual(set(best), {"new"})

    def test_sent_is_capped_to_newest(self):
        sent = {f"2026-10-{d:02d}T08:00:00" for d in range(1, 11)}
        prune({}, sent, now=0.0, max_sent=3)
        self.assertEqual(sent, {"2026-10-08T08:00:00", "2026-10-09T08:00:00", "2026-10-10T08:00:00"})

    def test_sent_under_cap_untouched(self):
        sent = {"a", "b"}
        prune({}, sent, now=0.0, max_sent=3)
        self.assertEqual(sent, {"a", "b"})


class PendingFileTest(unittest.TestCase):
    def test_save_is_atomic_via_replace(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pending.jsonl")
            with open(path, "w") as f:
                f.write('{"old": 1}\n')
            with patch("health_sync.scale.reader.os.replace", side_effect=OSError("power cut")):
                with self.assertRaises(OSError):
                    save_pending_file(path, [{"new": 2}])
            with open(path) as f:
                self.assertEqual(f.read(), '{"old": 1}\n')  # original intact until the replace
            save_pending_file(path, [{"a": 1}, {"b": 2}])
            self.assertFalse(os.path.exists(path + ".tmp"))
            self.assertEqual(load_pending(path), [{"a": 1}, {"b": 2}])

    def test_load_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pending.jsonl")
            with open(path, "wb") as f:
                f.write(b'{"a": 1}\n\n{"b": 2}\n{"c": 3, "wei\xd0')  # truncated last line
            with patch("builtins.print") as p:
                rows = load_pending(path)
            self.assertEqual(rows, [{"a": 1}, {"b": 2}])
            self.assertTrue(any("skipping unparsable line 4" in str(c) for c in p.call_args_list))

    def test_load_missing_file(self):
        self.assertEqual(load_pending("/nonexistent/pending.jsonl"), [])

    def test_run_survives_corrupt_pending_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pending.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps({"x": 1}) + "\n" + '{"trunc')
            clock = SimClock()
            FakeScanner.instances = []
            with patch("health_sync.scale.reader.BleakScanner", FakeScanner), \
                 patch("health_sync.scale.reader.time.time", side_effect=clock.time), \
                 patch("health_sync.scale.reader.asyncio.sleep", side_effect=clock.sleep), \
                 patch("builtins.print"):
                code = asyncio.run(run("AA:BB:CC:DD:EE:FF", None, None, "", None, path, 5))
            self.assertEqual(code, IDLE_EXIT_CODE)


if __name__ == "__main__":
    unittest.main()
