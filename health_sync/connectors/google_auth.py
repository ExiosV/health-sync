"""Google credentials: a user OAuth refresh token per feature (least privilege), or a service account.

Two features, two tokens, so a problem or a narrow grant in one cannot break the other:
  * `drive`    GOOGLE_REFRESH_TOKEN           Drive + Sheets (the Sheets API accepts the Drive scope)
  * `calendar` GOOGLE_CALENDAR_REFRESH_TOKEN  Calendar events + the calendar list (falls back to GOOGLE_REFRESH_TOKEN)

Refreshes deliberately do NOT request scopes: Google then issues an access token for exactly what the user granted, so a
token that lacks a newer scope keeps working for everything else instead of failing at refresh.
"""
from __future__ import annotations

import json
import os

from google.oauth2.credentials import Credentials

DRIVE = "https://www.googleapis.com/auth/drive"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_LIST = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
# Scope sets requested at consent time (scripts/get_google_token.py), by feature.
SCOPE_SETS = {
    "drive": [DRIVE],
    "calendar": [CALENDAR_EVENTS, CALENDAR_LIST],
}
TOKEN_URI = "https://oauth2.googleapis.com/token"


def credentials(feature: str = "drive") -> Credentials:
    token = (os.getenv("GOOGLE_CALENDAR_REFRESH_TOKEN") if feature == "calendar" else None) or os.environ["GOOGLE_REFRESH_TOKEN"]
    return Credentials(
        None,
        refresh_token=token,
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri=TOKEN_URI,
    )


def has_oauth() -> bool:
    return all(os.getenv(k) for k in ("GOOGLE_REFRESH_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"))


def service_account_info() -> dict | None:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    return json.loads(raw) if raw else None
