"""Google credentials: user OAuth refresh token (preferred) or a service account."""
from __future__ import annotations

import json
import os

from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def credentials() -> Credentials:
    return Credentials(
        None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )


def has_oauth() -> bool:
    return all(os.getenv(k) for k in ("GOOGLE_REFRESH_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"))


def service_account_info() -> dict | None:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    return json.loads(raw) if raw else None
