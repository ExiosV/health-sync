import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "get_google_token.py")


class GetGoogleTokenTest(unittest.TestCase):
    def setUp(self):
        fd, self.env_file = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write("GOOGLE_CLIENT_ID=fake-id\nGOOGLE_CLIENT_SECRET=fake-secret\n")
        self.addCleanup(os.remove, self.env_file)

    def run_script(self, *args):
        env = {**os.environ, "HEALTH_SYNC_ENV": self.env_file}
        return subprocess.run([sys.executable, SCRIPT, *args], env=env, capture_output=True, text=True, timeout=30)

    def test_gmail_url_asks_only_for_gmail_readonly(self):
        res = self.run_script("url", "gmail")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.readonly", res.stdout)
        self.assertIn("access_type=offline", res.stdout)
        self.assertNotIn("drive", res.stdout)

    def test_all_set_has_no_gmail(self):
        res = self.run_script("url", "all")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("gmail", res.stdout)

    def test_unknown_feature_lists_gmail(self):
        res = self.run_script("url", "nosuch")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("gmail", res.stderr)


if __name__ == "__main__":
    unittest.main()
