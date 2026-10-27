"""HTTP endpoint (runs in the pod): receives scale measurements from the BLE reader and uploads them to Garmin.

POST /scale  Authorization: Bearer $SCALE_INGEST_TOKEN
  {"timestamp": "<scale clock, used as dedup key>", "received_at": "<local ISO time>", "weight_kg": 80.0, "impedance": 480}
Who is on the scale is decided by weight (see Persons); only the Garmin-enabled person is uploaded, others are saved. Body fat is estimated from impedance with the Garmin profile (height, birth date, sex). Without impedance (socks) only the
weight is uploaded. Every accepted measurement is also appended to $DATA_DIR/scale-log.jsonl.

GET /activities?since=<ISO-8601>&limit=<n>  Authorization: Bearer $SCALE_INGEST_TOKEN
  Read-only view of recent Garmin activities (newest first, limit 10 by default, at most 30) for other in-cluster
  services, e.g. the bike livetrack waiting for a ride to finish. The Garmin response is cached for 60 s. No sink or
  state is written.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from health_sync.connectors.garmin import GarminSource, _normalize_activity
from health_sync.scale.miscale import body_fat_pct

log = logging.getLogger(__name__)
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))


class Persons:
    """Tells who stepped on the scale by weight: nearest reference weight wins, ambiguous ones are 'unknown'.

    State in $DATA_DIR/scale-persons.json: {"<id>": {"ref": kg, "garmin": bool}}. Initial values come from SCALE_PERSONS
    ("alice=80,bob=65") and SCALE_GARMIN_PERSONS ("alice"): only these are uploaded to Garmin. The reference follows the
    person's weight (moving average), so slow weight changes are tracked. Without SCALE_PERSONS everything is treated as
    one person and uploaded.
    """

    MAX_DIST, MIN_MARGIN, ALPHA = 2.0, 1.0, 0.2

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            spec = os.getenv("SCALE_PERSONS", "")
            uploaders = {n.strip() for n in os.getenv("SCALE_GARMIN_PERSONS", "").split(",") if n.strip()}
            self.data = {}
            for item in filter(None, spec.split(",")):
                name, ref = item.split("=")
                self.data[name.strip()] = {"ref": float(ref), "garmin": name.strip() in uploaders}

    def classify(self, weight: float) -> str:
        if not self.data:
            return "owner"
        ranked = sorted(self.data, key=lambda n: abs(self.data[n]["ref"] - weight))
        best = ranked[0]
        dist = abs(self.data[best]["ref"] - weight)
        if dist > self.MAX_DIST:
            return "unknown"
        if len(ranked) > 1 and abs(self.data[ranked[1]]["ref"] - weight) - dist < self.MIN_MARGIN:
            return "unknown"
        return best

    def update(self, person: str, weight: float) -> None:
        if person in self.data:
            ref = self.data[person]["ref"]
            self.data[person]["ref"] = round(ref + self.ALPHA * (weight - ref), 2)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))

    def uploads(self, person: str) -> bool:
        return person == "owner" if not self.data else bool(self.data.get(person, {}).get("garmin"))


class ScaleIngest:
    def __init__(self, api_factory):
        self._api_factory, self._api, self._profile = api_factory, None, None
        self.seen_path, self.log_path = DATA_DIR / "scale-seen.json", DATA_DIR / "scale-log.jsonl"
        self.persons = Persons(DATA_DIR / "scale-persons.json")

    def _seen(self) -> set[str]:
        try:
            return set(json.loads(self.seen_path.read_text()))
        except (FileNotFoundError, ValueError):
            return set()

    def _log(self, row: dict) -> None:
        with self.log_path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def profile(self) -> dict:
        if self._profile is None:
            self._api = self._api or self._api_factory()
            ud = self._api.get_user_profile()
            ud = ud.get("userData", ud)
            born = date.fromisoformat(ud["birthDate"][:10])
            self._profile = {"height": float(ud["height"]), "male": str(ud["gender"]).upper().startswith("M"), "born": born}
        return self._profile

    def handle(self, p: dict) -> dict:
        key = str(p["timestamp"])
        seen = self._seen()
        if key in seen:
            return {"status": "duplicate"}
        weight = float(p["weight_kg"])
        if not 20 < weight < 300:
            raise ValueError(f"implausible weight {weight}")
        when = str(p.get("received_at") or datetime.now().isoformat(timespec="seconds"))
        person = self.persons.classify(weight)
        if not self.persons.uploads(person):  # other people / unknown: keep the data, never touch this Garmin account
            seen.add(key)
            self.seen_path.write_text(json.dumps(sorted(seen)))
            self.persons.update(person, weight)
            self._log({"key": key, "at": when, "person": person, "weight_kg": weight, "impedance": p.get("impedance"),
                       "synced": False})
            log.info("saved %.2f kg for %s (not uploaded)", weight, person)
            return {"status": "saved", "person": person}
        fat = bmi = None
        if p.get("impedance"):
            prof = self.profile()
            age = (date.today() - prof["born"]).days // 365
            fat = body_fat_pct(weight, int(p["impedance"]), prof["height"], age, prof["male"])
            bmi = round(weight / (prof["height"] / 100) ** 2, 1)
        self._api = self._api or self._api_factory()
        self._api.add_body_composition(when, weight, percent_fat=fat, bmi=bmi)
        seen.add(key)
        self.seen_path.write_text(json.dumps(sorted(seen)))
        self.persons.update(person, weight)
        self._log({"key": key, "at": when, "person": person, "weight_kg": weight, "impedance": p.get("impedance"),
                   "fat_pct": fat, "bmi": bmi, "synced": True})
        log.info("uploaded %.2f kg fat=%s at %s", weight, fat, when)
        return {"status": "uploaded", "person": person, "weight_kg": weight, "fat_pct": fat}


def authorized(headers, token: str) -> bool:
    """Bearer check shared by POST /scale and GET /activities."""
    return headers.get("Authorization") == f"Bearer {token}"


def _since_key(value: str) -> str:
    """ISO-8601 -> 'YYYY-MM-DDTHH:MM:SS', comparable with Garmin's local start strings (the offset, if any, is local
    wall time already, so it is dropped together with a trailing Z)."""
    text = value.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1]
    return text[:19] if len(text) >= 19 else text


def _summary(a: dict) -> dict:
    """One activity from the Garmin API in the small shape GET /activities returns."""
    norm = _normalize_activity(a, {})
    out = {
        "activityId": a.get("activityId"),
        "type": norm["type"],
        "start": a.get("startTimeLocal"),
    }
    if a.get("startTimeGMT"):
        out["start_gmt"] = a["startTimeGMT"]
    out["duration_s"] = round(a.get("duration") or 0)
    if a.get("elapsedDuration") is not None:
        out["elapsed_s"] = round(a["elapsedDuration"])
    out["distance_km"] = norm["distance_km"]
    out["utc_offset_min"] = norm["utc_offset_min"]
    return out


class ActivitiesFeed:
    """Read-only, 60 s-cached view of the newest Garmin activities for GET /activities.

    Polling clients (every 2 min) must not hit Garmin each time (Cloudflare/MFA risk), so the raw API response is
    fetched at most once per CACHE_TTL_S and filtered/limited in memory. Failures are not cached.
    """

    CACHE_TTL_S = 60.0
    FETCH_MAX = 100
    DEFAULT_LIMIT, MAX_LIMIT = 10, 30

    def __init__(self, api_factory):
        self._api_factory = api_factory
        self._lock = threading.Lock()
        self._cache: list[dict] | None = None
        self._cached_at = 0.0

    def _fetch(self) -> list[dict]:
        api = self._api_factory()
        return [_summary(a) for a in api.get_activities(0, self.FETCH_MAX) or []]

    def get(self, since: str | None = None, limit: int = DEFAULT_LIMIT) -> list[dict]:
        limit = min(max(int(limit), 1), self.MAX_LIMIT)
        now = time.monotonic()
        with self._lock:
            if self._cache is None or now - self._cached_at >= self.CACHE_TTL_S:
                self._cache = self._fetch()
                self._cached_at = now
            activities = self._cache
        if since:
            activities = [a for a in activities if a["start"] >= since]
        return activities[:limit]


def make_handler(token: str, ingest: ScaleIngest, feed: ActivitiesFeed) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/":  # health check, open like before
                return self._reply(200, {"ok": True})
            if url.path != "/activities":
                return self._reply(404, {"error": "not found"})
            if not authorized(self.headers, token):
                return self._reply(401, {"error": "unauthorized"})
            params = parse_qs(url.query)
            try:
                limit = int((params.get("limit") or [ActivitiesFeed.DEFAULT_LIMIT])[0])
            except ValueError:
                return self._reply(400, {"error": "limit must be an integer"})
            since = (params.get("since") or [None])[0]
            try:
                activities = feed.get(_since_key(since) if since else None, limit)
            except Exception as exc:  # Garmin is down or the login failed: never leak the message (tokens, email)
                log.warning("activities unavailable: %s", type(exc).__name__)
                return self._reply(503, {"error": "garmin unavailable"})
            self._reply(200, {"activities": activities})

        def do_POST(self) -> None:
            if self.path != "/scale" or not authorized(self.headers, token):
                return self._reply(401, {"error": "unauthorized"})
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self._reply(200, ingest.handle(payload))
            except Exception as exc:
                log.exception("scale ingest failed")
                self._reply(500, {"error": str(exc)[:200]})

        def log_message(self, *args) -> None:  # silence per-request noise
            pass

    return Handler


def serve(port: int = 8080) -> None:
    token = os.environ["SCALE_INGEST_TOKEN"]
    source = GarminSource()  # one lazy client shared by the scale upload and the activities view
    ingest = ScaleIngest(source._client)
    feed = ActivitiesFeed(source._client)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(token, ingest, feed)).serve_forever()
