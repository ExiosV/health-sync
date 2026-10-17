"""BLE reader for the Mi Body Composition Scale 2. Runs natively on the machine with Bluetooth (e.g. a Windows PC).

  python reader.py --address AA:BB:CC:DD:EE:FF --scan 60            # print what is heard, no upload
  python reader.py --address ... --url http://127.0.0.1/scale --host health-sync.example.local --token-file token.txt

Only needs `bleak` and the standard library (+ miscale.py next to it or the package installed).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.request

from bleak import BleakScanner

try:
    from health_sync.scale.miscale import SERVICE_UUID, parse
except ImportError:  # copied next to miscale.py
    from miscale import SERVICE_UUID, parse

SETTLE_S = 10  # wait for the impedance advert after the first stable weight


def post(url: str, token: str, payload: dict, host: str | None = None) -> None:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if host:
        headers["Host"] = host  # reach the ingress on 127.0.0.1 without a hosts-file entry
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers)
    urllib.request.urlopen(req, timeout=20).read()


async def run(address: str, scan: int | None, url: str | None, token: str, host: str | None, pending_file: str | None) -> None:
    best: dict[str, tuple[float, object]] = {}  # measurement time -> (first heard at, best Measurement)
    sent: set[str] = set()
    pending: list[dict] = []
    last_try = 0.0
    if pending_file and os.path.exists(pending_file):
        pending = [json.loads(line) for line in open(pending_file) if line.strip()]

    def save_pending() -> None:
        if pending_file:
            with open(pending_file, "w") as f:
                f.writelines(json.dumps(p) + "\n" for p in pending)

    def flush() -> None:
        for p in list(pending):
            try:
                post(url, token, p, host)
                pending.remove(p)
                print("uploaded", p, flush=True)
            except Exception as exc:
                print("upload failed, will retry:", exc, flush=True)
                break
        save_pending()

    def on_adv(device, adv) -> None:
        if device.address.upper() != address.upper():
            return
        data = adv.service_data.get(SERVICE_UUID)
        m = parse(bytes(data)) if data else None
        if m is None:
            return
        key = m.timestamp.isoformat()
        print(f"{time.strftime('%H:%M:%S')} heard {m.weight_kg} kg imp={m.impedance} stable={m.stabilized} removed={m.removed} raw={bytes(data).hex()}", flush=True)
        if m.stabilized and not m.removed and key not in sent:
            prev = best.get(key)
            if prev is None or (m.impedance and not prev[1].impedance):
                best[key] = (prev[0] if prev else time.time(), m)

    async with BleakScanner(on_adv, scanning_mode="active"):
        end = time.time() + scan if scan else None
        while end is None or time.time() < end:
            await asyncio.sleep(1)
            for key, (t0, m) in list(best.items()):
                if time.time() - t0 >= SETTLE_S and key not in sent:
                    sent.add(key)
                    payload = {"timestamp": key, "weight_kg": m.weight_kg, "impedance": m.impedance,
                               "received_at": time.strftime("%Y-%m-%dT%H:%M:%S")}  # the scale clock is unreliable
                    print("measurement", payload, flush=True)
                    if url:
                        pending.append(payload)
                        save_pending()
            if url and pending and time.time() - last_try >= 15:
                last_try = time.time()
                flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", required=True)
    ap.add_argument("--scan", type=int, help="seconds to listen, then exit (default: forever)")
    ap.add_argument("--url")
    ap.add_argument("--token-file")
    ap.add_argument("--host", help="Host header for the ingress, e.g. health-sync.prod.local")
    ap.add_argument("--pending-file", help="queue of measurements not yet uploaded (survives restarts)")
    a = ap.parse_args()
    token = open(a.token_file).read().strip() if a.token_file else ""
    asyncio.run(run(a.address, a.scan, a.url, token, a.host, a.pending_file))


if __name__ == "__main__":
    main()
