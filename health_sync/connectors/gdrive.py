"""Google Drive helper: google-kit's Drive wired to health-sync's default credentials."""
from __future__ import annotations

from google_kit import Drive as _Drive
from google_kit import GoogleAuth
from google_kit.drive import FOLDER, SHEET

from health_sync.connectors import google_auth

__all__ = ["Drive", "FOLDER", "SHEET"]


class Drive(_Drive):
    """A Drive using the `drive` feature token unless another GoogleAuth is supplied."""

    def __init__(self, auth: GoogleAuth | None = None):
        super().__init__(auth or google_auth.auth())
