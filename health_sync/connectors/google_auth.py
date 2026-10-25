"""Glue between google-kit (requests-based) and gspread (google-auth Credentials).

The refresh-token flow and env loading live in `google_kit`; this module only maps health-sync's
two feature tokens onto it and builds the `google.auth` object that gspread still requires.
google-kit deliberately does not port the service-account path, so that stays here.

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
from google_kit import GoogleAuth


def auth(feature: str = "drive") -> GoogleAuth:
    """google-kit credentials for one feature; `calendar` uses its own token when present."""
    if feature == "calendar":
        return GoogleAuth(
            os.environ["GOOGLE_CLIENT_ID"],
            os.environ["GOOGLE_CLIENT_SECRET"],
            os.getenv("GOOGLE_CALENDAR_REFRESH_TOKEN") or os.environ["GOOGLE_REFRESH_TOKEN"],
        )
    return GoogleAuth.from_env()


def credentials(feature: str = "drive") -> Credentials:
    """A google-auth Credentials object for gspread, built from the same google-kit env/token."""
    source = auth(feature)
    return Credentials(
        None,
        refresh_token=source.refresh_token,
        client_id=source.client_id,
        client_secret=source.client_secret,
        token_uri=source.token_uri,
    )


def service_account_info() -> dict | None:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    return json.loads(raw) if raw else None
