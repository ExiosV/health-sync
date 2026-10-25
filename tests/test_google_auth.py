import os
import unittest
from unittest import mock

from health_sync.connectors import google_auth

ENV = {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret", "GOOGLE_REFRESH_TOKEN": "drive-token"}


class GoogleAuthTest(unittest.TestCase):
    def test_no_scopes_requested_on_refresh(self):
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.assertIsNone(google_auth.credentials().scopes)

    def test_calendar_uses_its_own_token_when_present(self):
        with mock.patch.dict(os.environ, {**ENV, "GOOGLE_CALENDAR_REFRESH_TOKEN": "cal-token"}, clear=True):
            self.assertEqual(google_auth.credentials("calendar").refresh_token, "cal-token")
            self.assertEqual(google_auth.credentials("drive").refresh_token, "drive-token")

    def test_calendar_falls_back_to_the_common_token(self):
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(google_auth.credentials("calendar").refresh_token, "drive-token")

    def test_calendar_auth_and_drive_auth_share_the_token_rules(self):
        with mock.patch.dict(os.environ, {**ENV, "GOOGLE_CALENDAR_REFRESH_TOKEN": "cal-token"}, clear=True):
            self.assertEqual(google_auth.auth("calendar").refresh_token, "cal-token")
            self.assertEqual(google_auth.auth().refresh_token, "drive-token")
            self.assertEqual(google_auth.auth().token_uri, "https://oauth2.googleapis.com/token")


if __name__ == "__main__":
    unittest.main()
