"""One-off, stdlib only: get a Google refresh token and store it in the health-sync secret file on the machine where
this runs ($HEALTH_SYNC_ENV, default ~/.config/health-sync/secrets.env), without printing it.

Least privilege: one token per feature.
  drive      GOOGLE_REFRESH_TOKEN           scope: drive (Drive + Sheets)
  calendar   GOOGLE_CALENDAR_REFRESH_TOKEN  scopes: calendar.events + calendar.calendarlist.readonly
  all        GOOGLE_REFRESH_TOKEN           all of the above in one token (legacy, broader than needed)

  python3 get_google_token.py url [drive|calendar|all]               prints the consent URL (default: drive)
  python3 get_google_token.py exchange '<URL>' [drive|calendar|all]  <URL> = the address the browser ends up on after
                                                                     consent (http://127.0.0.1:1/?code=... fails to
                                                                     load; that is expected)

GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must already be in the env file. The OAuth project needs the Google Drive,
Google Sheets and Google Calendar APIs enabled.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

ENV_FILE = os.path.expanduser(os.environ.get("HEALTH_SYNC_ENV", "~/.config/health-sync/secrets.env"))
G = "https://www.googleapis.com/auth/"
SETS = {
    "drive": ([G + "drive"], "GOOGLE_REFRESH_TOKEN"),
    "calendar": ([G + "calendar.events", G + "calendar.calendarlist.readonly"], "GOOGLE_CALENDAR_REFRESH_TOKEN"),
    "all": ([G + "drive", G + "calendar.events", G + "calendar.calendarlist.readonly"], "GOOGLE_REFRESH_TOKEN"),
}
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


def feature(argv: list, index: int) -> str:
    name = argv[index] if len(argv) > index else "drive"
    if name not in SETS:
        sys.exit(f"unknown feature {name!r}; use one of {', '.join(SETS)}")
    return name


env = read_env()
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "url":
    scopes, _ = SETS[feature(sys.argv, 2)]
    print("https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode({
        "client_id": env["GOOGLE_CLIENT_ID"], "redirect_uri": REDIRECT, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent"}))
elif cmd == "exchange" and len(sys.argv) >= 3:
    scopes, key = SETS[feature(sys.argv, 3)]
    code = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[2]).query)["code"][0]
    data = urllib.parse.urlencode({
        "code": code, "client_id": env["GOOGLE_CLIENT_ID"], "client_secret": env["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": REDIRECT, "grant_type": "authorization_code"}).encode()
    tok = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data))
    if "refresh_token" not in tok:
        sys.exit("no refresh token returned")
    granted = set(tok.get("scope", "").split())
    missing = [s for s in scopes if s not in granted]
    if missing:
        sys.exit("consent did not grant: " + ", ".join(missing))
    write_env(key, tok["refresh_token"])
    print(f"stored {key}; granted scopes:", tok.get("scope"))
else:
    sys.exit(__doc__)
