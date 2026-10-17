import os
import unittest
from datetime import datetime

from health_sync.scale.miscale import body_fat_pct, parse


def frame(weight_raw, impedance=0, ctrl0=0x02, ctrl1=0x26):
    return (bytes([ctrl0, ctrl1]) + (2026).to_bytes(2, "little") + bytes([10, 17, 8, 30, 5])
            + impedance.to_bytes(2, "little") + weight_raw.to_bytes(2, "little"))


class MiScaleTest(unittest.TestCase):
    def test_parse_kg_with_impedance(self):
        m = parse(frame(14000, 480))  # 14000 / 200 = 70.0 kg
        self.assertEqual((m.weight_kg, m.impedance, m.stabilized, m.removed), (70.0, 480, True, False))
        self.assertEqual(m.timestamp, datetime(2026, 10, 17, 8, 30, 5))

    def test_parse_without_impedance_and_bad_input(self):
        self.assertIsNone(parse(frame(14000, 0, ctrl1=0x20)).impedance)
        self.assertIsNone(parse(b"\x00" * 5))

    def test_lbs(self):
        self.assertAlmostEqual(parse(frame(15432, ctrl0=0x03)).weight_kg, 70.0, places=1)

    def test_body_fat_plausible(self):
        fat = body_fat_pct(75.0, 480, 178, 40, True)
        self.assertTrue(10 < fat < 35)
        self.assertIsNone(body_fat_pct(75.0, 0, 178, 40, True))


class IngestTest(unittest.TestCase):
    def test_upload_once_and_duplicate(self):
        import tempfile
        from datetime import date
        from pathlib import Path

        from health_sync.scale import ingest

        calls = []

        class Api:
            def get_user_profile(self):
                return {"userData": {"birthDate": "1985-05-05", "height": 178.0, "gender": "MALE"}}

            def add_body_composition(self, ts, weight, **kw):
                calls.append((ts, weight, kw))

        with tempfile.TemporaryDirectory() as d:
            ingest.DATA_DIR = Path(d)
            svc = ingest.ScaleIngest(lambda: Api())  # single-user mode
            payload = {"timestamp": "t1", "received_at": "2026-10-17T17:05:30", "weight_kg": 80.0, "impedance": 480}
            self.assertEqual(svc.handle(payload)["status"], "uploaded")
            self.assertEqual(svc.handle(payload)["status"], "duplicate")
            self.assertEqual(len(calls), 1)
            self.assertTrue(10 < calls[0][2]["percent_fat"] < 35)
            self.assertEqual(svc.handle({"timestamp": "t2", "weight_kg": 79.4})["status"], "uploaded")
            self.assertIsNone(calls[1][2]["percent_fat"])

    def test_other_person_is_saved_not_uploaded(self):
        import json
        import tempfile
        from pathlib import Path

        from health_sync.scale import ingest

        calls = []

        class Api:
            def add_body_composition(self, *a, **kw):
                calls.append(a)

        with tempfile.TemporaryDirectory() as d:
            ingest.DATA_DIR = Path(d)
            os.environ["SCALE_PERSONS"], os.environ["SCALE_GARMIN_PERSONS"] = "alice=80,bob=76", "alice"
            self.addCleanup(os.environ.pop, "SCALE_PERSONS")
            self.addCleanup(os.environ.pop, "SCALE_GARMIN_PERSONS")
            svc = ingest.ScaleIngest(lambda: Api())
            self.assertEqual(svc.handle({"timestamp": "w1", "weight_kg": 76.4, "impedance": 500})["person"], "bob")
            self.assertEqual(svc.handle({"timestamp": "u1", "weight_kg": 78.0})["person"], "unknown")  # between the two
            self.assertEqual(calls, [])
            rows = [json.loads(x) for x in (Path(d) / "scale-log.jsonl").read_text().splitlines()]
            self.assertEqual([(r["person"], r["synced"]) for r in rows], [("bob", False), ("unknown", False)])
            self.assertEqual(svc.handle({"timestamp": "i1", "weight_kg": 80.2})["status"], "uploaded")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
