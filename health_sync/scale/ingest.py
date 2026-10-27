"""HTTP endpoint (runs in the pod): receives scale measurements from the BLE reader and uploads them to Garmin.

POST /scale  Authorization: Bearer $SCALE_INGEST_TOKEN
  {"timestamp": "<scale clock, used as dedup key>", "received_at": "<local ISO time>", "weight_kg": 80.0, "impedance": 480}
Who is on the scale is decided by weight (see Persons); only the Garmin-enabled person is uploaded, others are saved. Body fat is estimated from impedance with the Garmin profile (height, birth date, sex). Without impedance (socks) only the
weight is uploaded. Every accepted measurement is also appended to $DATA_DIR/scale-log.jsonl.

GET /activities?since=<ISO-8601>&limit=<n>  Authorization: Bearer $SCALE_INGEST_TOKEN
  Read-only view of recent Garmin activities (newest first, limit 10 by default, at most 30) for other in-cluster
  services, e.g. the bike livetrack waiting for a ride to finish. `since` is compared in UTC when it carries an offset
  or a Z, and as local wall time (Garmin's startTimeLocal) when it does not. A success is cached for 120 s, a failure
  for 30 s. No sink or state is written.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
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
    """Bearer check shared by POST /scale and GET /activities. Fails closed when no token is configured."""
    if not token:
        return False
    supplied = headers.get("Authorization") or ""
    return hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode())


def parse_since(value: str) -> datetime:
    """ISO-8601 -> datetime. 'Z' means UTC; a value without an offset is local wall time (Garmin's startTimeLocal).
    Raises ValueError on anything unparsable."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _start_instant(activity: dict, utc: bool) -> datetime | None:
    """An activity's start as a comparable datetime, in UTC or in local wall time; None when it cannot be derived."""
    start = activity.get("start")
    if not start:
        return None
    try:
        local = datetime.fromisoformat(start)
    except ValueError:
        return None
    if not utc:
        return local if local.tzinfo is None else None
    if activity.get("start_gmt"):
        try:
            return datetime.fromisoformat(activity["start_gmt"]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    offset = activity.get("utc_offset_min")
    if offset is None:
        return None
    # Garmin gives local wall time; local minus the offset is the UTC instant.
    return local.replace(tzinfo=timezone.utc) - timedelta(minutes=offset)


def _after_since(activities: list[dict], since: datetime) -> list[dict]:
    """Keep activities starting at or after `since`: in UTC when `since` carries an offset, else in local wall time."""
    utc = since.tzinfo is not None
    if utc:
        since = since.astimezone(timezone.utc)
    return [a for a in activities if (start := _start_instant(a, utc)) is not None and start >= since]


RIDE_TYPES = frozenset({
    "cycling", "road_biking", "gravel_cycling", "mountain_biking", "e_bike_fitness", "e_bike_mountain",
    "virtual_ride", "indoor_cycling", "cyclocross",
})


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
    out["is_ride"] = norm["type"] in RIDE_TYPES
    seconds = out.get("elapsed_s", out["duration_s"])
    for key, target in (("start", "end_local"), ("start_gmt", "end_gmt")):
        text = out.get(key)
        if text:
            try:
                out[target] = (datetime.fromisoformat(text) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
            except ValueError:
                pass
    return out


class ActivitiesUnavailable(Exception):
    """Garmin could not be reached or rejected the session; the endpoint answers 503."""


_AUTH_HINTS = ("auth", "401", "403", "unauthorized", "forbidden", "login", "credential", "token")


def _looks_like_auth_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(hint in text for hint in _AUTH_HINTS)


class ActivitiesFeed:
    """Read-only, cached view of the newest Garmin activities for GET /activities.

    Polling clients (every 2 min) must not hit Garmin each time (Cloudflare/MFA risk): a success is cached for
    CACHE_TTL_S and a failure for ERROR_TTL_S. Fetches are single-flight: the state lock is never held across the
    network call, and a dedicated fetch lock keeps concurrent requests from logging in twice.
    """

    CACHE_TTL_S = 120.0
    ERROR_TTL_S = 30.0
    FETCH_MAX = 100
    DEFAULT_LIMIT, MAX_LIMIT = 10, 30

    def __init__(self, api_factory, reset=None):
        self._api_factory = api_factory
        self._reset = reset
        self._lock = threading.Lock()        # cached state only, never held during network I/O
        self._fetch_lock = threading.Lock()  # single-flight for the Garmin call
        self._cache: list[dict] | None = None
        self._cached_at = 0.0
        self._error_at: float | None = None

    def _fetch(self) -> list[dict]:
        api = self._api_factory()
        return [_summary(a) for a in api.get_activities(0, self.FETCH_MAX) or []]

    def _recent(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now - self._cached_at < self.CACHE_TTL_S:
                return self._cache
            if self._error_at is not None and now - self._error_at < self.ERROR_TTL_S:
                raise ActivitiesUnavailable()
        with self._fetch_lock:
            now = time.monotonic()
            with self._lock:
                if self._cache is not None and now - self._cached_at < self.CACHE_TTL_S:
                    return self._cache
                if self._error_at is not None and now - self._error_at < self.ERROR_TTL_S:
                    raise ActivitiesUnavailable()
            try:
                fetched = self._fetch()
            except Exception as exc:  # never leak the message (it can carry an email or a token)
                with self._lock:
                    self._error_at = time.monotonic()
                if self._reset is not None and _looks_like_auth_error(exc):
                    self._reset()
                raise ActivitiesUnavailable() from None
            with self._lock:
                self._cache, self._cached_at, self._error_at = fetched, time.monotonic(), None
            return fetched

    def get(self, since: datetime | None = None, limit: int = DEFAULT_LIMIT) -> list[dict]:
        limit = min(max(int(limit), 1), self.MAX_LIMIT)
        activities = self._recent()
        if since is not None:
            activities = _after_since(activities, since)
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
            since_raw = (params.get("since") or [None])[0]
            try:
                since = parse_since(since_raw) if since_raw else None
            except ValueError:
                return self._reply(400, {"error": "since must be ISO-8601"})
            try:
                activities = feed.get(since, limit)
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
    token = os.environ.get("SCALE_INGEST_TOKEN", "")
    if not token:  # fail closed: an empty token would accept "Authorization: Bearer "
        raise RuntimeError("SCALE_INGEST_TOKEN is empty: refusing to serve")
    source = GarminSource()  # one lazy client shared by the scale upload and the activities view
    ingest = ScaleIngest(source._client)
    feed = ActivitiesFeed(source._client, reset=source.reset_client)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(token, ingest, feed)).serve_forever()
