"""Tests for health_sync.scale.reader idle-exit logic."""

import unittest
from unittest.mock import patch, MagicMock, AsyncMock

from health_sync.scale.reader import is_idle, post, run


class FakeClock:
    """Monotonically increasing fake time source. Each call to time() advances by 1.0."""
    def __init__(self, start: float = 0.0):
        self._value = start - 1.0  # first call returns `start`

    def time(self) -> float:
        self._value += 1.0
        return self._value


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


class ReaderRunTest(unittest.TestCase):
    """Test run() logic without bleak hardware by patching BleakScanner."""

    def test_run_exits_on_idle(self):
        """run() should return IDLE_EXIT_CODE (3) when no advertisement arrives for idle_exit seconds."""
        import asyncio

        clock = FakeClock(0.0)

        async def test_coro():
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=None)

            with patch("health_sync.scale.reader.BleakScanner", return_value=mock_ctx), \
                 patch("time.time", side_effect=clock.time):
                code = await run(
                    address="AA:BB:CC:DD:EE:FF",
                    scan=None,
                    url=None,
                    token="",
                    host=None,
                    pending_file=None,
                    idle_exit=5,
                )
            return code

        code = asyncio.run(test_coro())
        self.assertEqual(code, 3)

    def test_run_scan_timeout_before_idle(self):
        """run() should exit 0 when scan timeout is reached before idle condition."""
        import asyncio

        clock = FakeClock(0.0)

        async def test_coro():
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=None)

            with patch("health_sync.scale.reader.BleakScanner", return_value=mock_ctx), \
                 patch("time.time", side_effect=clock.time):
                # scan=5, idle_exit=10 -> scan ends before idle would trigger
                # time sequence: 0 (last_adv), 1 (end=1+5=6), 2, 3, 4, 5, 6(while false)
                code = await run(
                    address="AA:BB:CC:DD:EE:FF",
                    scan=5,
                    url=None,
                    token="",
                    host=None,
                    pending_file=None,
                    idle_exit=10,
                )
            return code

        code = asyncio.run(test_coro())
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()