"""BLE reader for the Mi Body Composition Scale 2. Runs natively on the machine with Bluetooth (e.g. a Windows PC).

  python reader.py --address AA:BB:CC:DD:EE:FF --scan 60            # print what is heard, no upload
  python reader.py --address ... --url http://127.0.0.1/scale --host health-sync.example.local --token-file token.txt
  python reader.py --address ... --idle-exit 600                    # exit 3 if no advertisement for 600 s (wrapper restarts)

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
IDLE_EXIT_CODE = 3  # the wrapper (run-scale-reader.cmd) restarts the reader on any exit


def post(url: str, token: str, payload: dict, host: str | None = None) -> None:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if host:
        headers["Host"] = host  # reach the ingress on 127.0.0.1 without a hosts-file entry
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers)
    urllib.request.urlopen(req, timeout=20).read()


def is_idle(last_adv_time: float, now: float, idle_exit: int) -> bool:
    """True if no advertisement has been received for `idle_exit` seconds.

    Pure (time is passed in) so the idle logic can be tested without bleak hardware.
    An idle_exit of 0 disables the check.
    At the exact boundary, the reader is still considered active (not idle).
    """
    if idle_exit <= 0:
        return False
    return (now - last_adv_time) > idle_exit


async def run(
    address: str,
    scan: int | None,
    url: str | None,
    token: str,
    host: str | None,
    pending_file: str | None,
    idle_exit: int = 600,
) -> int:
    best: dict[str, tuple[float, object]] = {}  # measurement time -> (first heard at, best Measurement)
    sent: set[str] = set()
    pending: list[dict] = []
    last_try = 0.0
    last_adv_time = time.time()  # last advertisement from ANY device, not just the scale
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
        nonlocal last_adv_time
        last_adv_time = time.time()  # any advertisement proves the watcher is alive
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
            now = time.time()
            if is_idle(last_adv_time, now, idle_exit):
                print(f"no advertisement for {idle_exit} s, exiting so the wrapper restarts the BLE watcher", flush=True)
                return IDLE_EXIT_CODE
            for key, (t0, m) in list(best.items()):
                if now - t0 >= SETTLE_S and key not in sent:
                    sent.add(key)
                    payload = {"timestamp": key, "weight_kg": m.weight_kg, "impedance": m.impedance,
                               "received_at": time.strftime("%Y-%m-%dT%H:%M:%S")}  # the scale clock is unreliable
                    print("measurement", payload, flush=True)
                    if url:
                        pending.append(payload)
                        save_pending()
            if url and pending and now - last_try >= 15:
                last_try = now
                flush()
            # Forget measurements older than 1 h and cap `sent` (ISO strings sort chronologically).
            cutoff = now - 3600
            for key in [k for k, (t0, _) in best.items() if t0 < cutoff]:
                del best[key]
            if len(sent) > 500:
                for key in sorted(sent)[:-500]:
                    sent.discard(key)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", required=True)
    ap.add_argument("--scan", type=int, help="seconds to listen, then exit (default: forever)")
    ap.add_argument("--url")
    ap.add_argument("--token-file")
    ap.add_argument("--host", help="Host header for the ingress, e.g. health-sync.prod.local")
    ap.add_argument("--pending-file", help="queue of measurements not yet uploaded (survives restarts)")
    ap.add_argument("--idle-exit", type=int, default=600,
                    help="exit with code 3 if no advertisement is received for this many seconds (0 disables)")
    a = ap.parse_args()
    token = open(a.token_file).read().strip() if a.token_file else ""
    code = asyncio.run(run(a.address, a.scan, a.url, token, a.host, a.pending_file, a.idle_exit))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
