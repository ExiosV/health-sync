import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

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
        query = urllib.parse.parse_qs(urllib.parse.urlparse(res.stdout.strip()).query)
        # exact match: an extra scope in the gmail set must fail the test
        self.assertEqual(query["scope"], ["https://www.googleapis.com/auth/gmail.readonly"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])

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
