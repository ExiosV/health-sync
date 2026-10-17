"""Garmin Connect source (uses the MIT-licensed `garminconnect` library)."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterable

from health_sync.core.model import Record

log = logging.getLogger(__name__)


def mfa_code() -> str:
    """MFA code from the terminal, or (headless pod) from the file named by GARMIN_MFA_FILE, waiting up to 10 min."""
    path = os.getenv("GARMIN_MFA_FILE")
    if not path:
        return input("Garmin MFA code: ").strip()
    log.info("waiting for the MFA code in %s", path)
    deadline = time.time() + 600
    while time.time() < deadline:
        if os.path.exists(path):
            code = Path(path).read_text().strip()
            os.remove(path)
            if code:
                return code
        time.sleep(3)
    raise TimeoutError("no MFA code provided")


SSO_EMBED = "https://sso.garmin.com/sso/embed"


def login_with_ticket(ticket: str, token_dir: str) -> None:
    """Exchange a one-time SSO service ticket (from a browser session already signed in at Garmin, widget flow with
    service=SSO_EMBED) for the library's long-lived tokens and save them. Uses a private method of garminconnect."""
    from garminconnect import Garmin

    api = Garmin()
    api.client._establish_session(ticket, service_url=SSO_EMBED)
    api.client.dump(str(Path(token_dir).expanduser()))


class GarminSource:
    name = "garmin"

    PAGE = 100

    def __init__(self, token_dir: str | None = None, activity_limit: int | None = None):
        self.token_dir = str(Path(token_dir or os.getenv("GARMINTOKENS", "~/.garminconnect")).expanduser())
        self.activity_limit = activity_limit or int(os.getenv("GARMIN_ACTIVITY_LIMIT", "100"))
        self._api = None

    def _client(self):
        if self._api is None:
            from garminconnect import Garmin  # imported lazily: heavy, and not needed to import the module

            api = Garmin(
                email=os.getenv("GARMIN_EMAIL"),
                password=os.getenv("GARMIN_PASSWORD"),
                prompt_mfa=mfa_code,
            )
            api.login(self.token_dir)  # reuses saved tokens, saves new ones
            self._api = api
        return self._api

    def fetch(self, since: str | None = None) -> Iterable[Record]:
        yield from self._activities(since)

    def _activities(self, since: str | None) -> Iterable[Record]:
        api = self._client()
        for a in self._pages(api, since):
            start = a["startTimeLocal"]
            try:
                zones = api.get_activity_hr_in_timezones(a["activityId"])
            except Exception as exc:  # zones are optional
                log.warning("no HR zones for %s: %s", a["activityId"], exc)
                zones = {}
            yield Record(
                kind="activity",
                source=self.name,
                external_id=str(a["activityId"]),
                start=start,
                payload=_normalize_activity(a, zones),
                raw=a,
            )


    def _pages(self, api, since: str | None) -> Iterable[dict]:
        """Newest-first activities, paged; stops at `since` or after activity_limit items."""
        for offset in range(0, self.activity_limit, self.PAGE):
            page = api.get_activities(offset, min(self.PAGE, self.activity_limit - offset))
            for a in page:
                if since and a["startTimeLocal"] <= since:
                    return
                yield a
            if len(page) < self.PAGE:
                return


def _normalize_activity(a: dict, zones: dict) -> dict:
    kind = a.get("activityType")
    speed = a.get("averageSpeed") or 0  # m/s
    return {
        "name": a.get("activityName", ""),
        "type": kind.get("typeKey", "") if isinstance(kind, dict) else (kind or ""),
        "distance_km": round((a.get("distance") or 0) / 1000, 2),
        "duration_min": round((a.get("duration") or 0) / 60, 2),
        "avg_hr": a.get("averageHR") or 0,
        "max_hr": a.get("maxHR") or 0,
        "pace_min_km": (1000 / 60) / speed if speed > 0 else 0,
        "cadence_avg": a.get("averageRunningCadenceInStepsPerMinute") or a.get("averageCadence") or 0,
        "cadence_max": a.get("maxRunningCadenceInStepsPerMinute") or a.get("maxCadence") or 0,
        "te_aerobic": a.get("aerobicTrainingEffect") or a.get("trainingEffect") or 0,
        "te_anaerobic": a.get("anaerobicTrainingEffect") or 0,
        "te_label": a.get("trainingEffectLabel", ""),
        "vo2max": a.get("vO2MaxValue") or a.get("vo2MaxValue") or 0,
        "calories": a.get("calories") or 0,
        "hr_zones": zones or {},
        "elevation_gain_m": round(a.get("elevationGain") or 0, 1),
        "elevation_loss_m": round(a.get("elevationLoss") or 0, 1),
    }
