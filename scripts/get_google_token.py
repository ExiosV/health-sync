"""One-off, stdlib only: get a Google refresh token (Drive + Sheets) and store it in the health-sync secret file
on the machine where this runs ($HEALTH_SYNC_ENV, default ~/.config/health-sync/secrets.env), without printing it.

  python3 get_google_token.py url                 prints the consent URL (sign in as the Drive owner and allow)
  python3 get_google_token.py exchange '<URL>'    <URL> = the address the browser ends up on after consent
                                                  (http://127.0.0.1:1/?code=... fails to load; that is expected)

GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must already be in the env file. The OAuth project needs the Google
Drive API and Google Sheets API enabled.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

ENV_FILE = os.path.expanduser(os.environ.get("HEALTH_SYNC_ENV", "~/.config/health-sync/secrets.env"))
SCOPES = "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/spreadsheets"
REDIRECT = "http://127.0.0.1:1/"


def read_env() -> dict:
    with open(ENV_FILE) as f:
        return dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line and not line.startswith("#"))


def write_env(key: str, value: str) -> None:
    lines = [ln for ln in open(ENV_FILE) if not ln.startswith(key + "=")]
    lines.append(f"{key}={value}\n")
    with open(ENV_FILE, "w") as f:
        f.writelines(lines)
    os.chmod(ENV_FILE, 0o600)


env = read_env()
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "url":
    print("https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode({
        "client_id": env["GOOGLE_CLIENT_ID"], "redirect_uri": REDIRECT, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent"}))
elif cmd == "exchange" and len(sys.argv) == 3:
    code = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[2]).query)["code"][0]
    data = urllib.parse.urlencode({
        "code": code, "client_id": env["GOOGLE_CLIENT_ID"], "client_secret": env["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": REDIRECT, "grant_type": "authorization_code"}).encode()
    tok = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data))
    if "refresh_token" not in tok:
        sys.exit("no refresh token returned")
    write_env("GOOGLE_REFRESH_TOKEN", tok["refresh_token"])
    print("stored GOOGLE_REFRESH_TOKEN; granted scopes:", tok.get("scope"))
else:
    sys.exit(__doc__)
