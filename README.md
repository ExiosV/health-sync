# health-sync

Sync and export health and fitness data between services: Garmin Connect as the primary store, Google Drive/Sheets,
a Xiaomi Mi Body Composition Scale 2 (Bluetooth), and markdown digests that a personal assistant / RAG can read.
Early-stage and opinionated: it is built for one household first, with connectors kept separate so more can be added.
Design notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); plan to turn it into a product: [docs/PRODUCT.md](docs/PRODUCT.md). License: Apache-2.0 (keep the NOTICE when you copy or fork).

## Pipelines

| Pipeline | What it does |
|---|---|
| `garmin-activities-to-sheet` | Garmin activities -> a Google Sheet (newest first) |
| `garmin-health-to-drive` | daily summary (sleep, HRV, body battery, stress, readiness, steps...), blood pressure and weight tabs, raw JSON per day, and a monthly markdown digest in Drive |
| `garmin-to-calendar` | Garmin activities (with a link to Garmin Connect), sleep and naps, weight and blood-pressure measurements, plus one all-day summary per day, as events in a Google Calendar (target calendar configurable, see below) |
| `scale-ingest` | HTTP endpoint that receives scale measurements and uploads weight and estimated body fat to Garmin; people are told apart by weight (`SCALE_PERSONS`), only `SCALE_GARMIN_PERSONS` are uploaded, the rest are only saved |
| `health_sync/scale/reader.py` | BLE reader for the scale; runs on any machine with Bluetooth and posts to the ingest endpoint |

```bash
cp .env.example .env     # Garmin login, Google OAuth client + refresh token, spreadsheet ids
pip install .
python -m health_sync run garmin-activities-to-sheet
python -m health_sync run garmin-to-calendar            # activities, sleep, naps, weight, pressure, daily summary -> a Google Calendar
python -m health_sync calendar-ls                        # list calendars (id + name) to configure the target
python -m health_sync drive-ls --path "<Drive folder path>"
python -m health_sync init-sheet --path "<Drive folder path>" --name "<file name>"   # prints the spreadsheet id
python3 scripts/get_google_token.py url|exchange [drive|calendar]   # one-off, stdlib only: Google refresh token
```

Google access is a user OAuth refresh token (`GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN`). OAuth refresh, Drive and the
Calendar REST calls come from the shared [google-kit](https://github.com/ExiosV/google-kit) library (Apache-2.0,
pinned to `v0.1.0` and installed straight from its release tarball); health-sync keeps what the library does not cover:
the `gspread` sheet writes, the service-account fallback, Calendar event construction and the write throttling.
Least privilege: one token per
feature - `GOOGLE_REFRESH_TOKEN` with the `drive` scope (Drive + Sheets) and, optionally, `GOOGLE_CALENDAR_REFRESH_TOKEN`
with `calendar.events` + `calendar.calendarlist.readonly` (no full `calendar` scope). Without the second token the first
one is used for the calendar too (it must then carry the calendar scopes). Tokens are refreshed without asking for scopes,
so a token missing a newer scope only fails the feature that needs it. Get a token with
`python3 scripts/get_google_token.py url|exchange [drive|calendar|all]`. A service account works as a fallback. Garmin tokens are kept in `GARMINTOKENS`. If direct Garmin login is blocked
(Cloudflare 429/403), `python -m health_sync garmin-ticket-login` exchanges a one-time SSO ticket from an already
signed-in browser (`GARMIN_TICKET`) for saved tokens. Garmin access uses the unofficial `garminconnect` library and can
break when Garmin changes its API.

The calendar target is not hard-coded, so the same code serves any person or a hosted run:
`GOOGLE_CALENDAR_ID` wins, else `GOOGLE_CALENDAR_NAME` (e.g. `Дневник`) is looked up in the account, else `primary`.
`calendar-ls` prints the id/name pairs. `CALENDAR_KINDS` picks which records become events
(`activity,sleep,nap,weight,blood_pressure,daily` by default), `CALENDAR_TIMEZONE` overrides the calendar's own
timezone, and `CALENDAR_SUMMARY_PREFIX` tags event titles. Activities and sleep are timed events (an activity's
description carries its Garmin Connect link); naps are timed events with their own local offset; weight and pressure are
short timed events (`CALENDAR_POINT_MINUTES`, 10 by default) or all-day when Garmin only gives a date; `daily` is one
all-day event per day with HRV, body battery, stress, readiness, SpO2, respiration and steps.
The first run backfills `GARMIN_CALENDAR_DAYS` of sleep/daily and `GARMIN_CALENDAR_ACTIVITY_DAYS` of activities;
set the latter to `0` (and `GARMIN_ACTIVITY_LIMIT=0`) to import the whole activity history.
Writes are paced (`CALENDAR_WRITE_DELAY`, 0.5 s) and retried on Google's `403 rateLimitExceeded` with exponential
backoff (`CALENDAR_WRITE_RETRIES`); an event that was deleted keeps a cancelled tombstone under its id, and the upsert
revives it with `status=confirmed` instead of failing.
Each event gets a deterministic id from the source record, so re-runs update it in place instead of duplicating.
Enable the Google Calendar API in the OAuth project and issue the calendar token (`get_google_token.py ... calendar`).

Time zones: Garmin gives both local wall time and GMT for activities, sleep, naps, weigh-ins and pressure readings, so
every timed event is written with the exact UTC offset of the place where it happened (RFC 3339, e.g. `+02:00`). Travel
and DST changes need no configuration and the calendar shows the right local time in any viewer's zone.
`CALENDAR_TIMEZONE` is only the fallback for a record without an offset. Sleep and naps are read the same morning, as soon
as the watch has synced the night (the run repeats every few hours); the rest of the day is read once it is over.

Typical deployment is a container (see `Dockerfile`) with two CronJobs and one small Deployment; the image entrypoint is
`python -m health_sync`.

## Layout

`health_sync/core` (record model, connector contracts, state), `health_sync/connectors` (one module per service),
`health_sync/scale` (scale decoder, reader, ingest), `health_sync/digest.py`, `health_sync/cli.py` (pipelines).

## Privacy

This tool handles personal health data. Nothing is sent anywhere except your own Garmin and Google accounts.
Do not commit `.env`, tokens or exported data.

## Scale reader on Windows

The BLE reader (`health_sync/scale/reader.py`) runs on the Windows host `devicex2` as a background
task, independent of the owner's logon session.

### What runs where

- **Task name**: `health-sync-scale` (S4U, runs as `DEVICEX2\igorp` in session 0)
- **Trigger**: `AtStartup` with a 1-minute delay (Bluetooth + network ready)
- **Action**: `cmd.exe /c C:\Tools\health-sync-scale\run-scale-reader.cmd`
- **Wrapper** (`run-scale-reader.cmd`): loops forever, restarts `reader.py` on any exit; rotates `reader.log`
  when it exceeds 10 MB; passes `--idle-exit 600` so the reader exits with code 3 if no advertisement
  arrives for 10 minutes (WinRT BLE watchers can stall silently).
- **Files in `C:\Tools\health-sync-scale\`**: `reader.py`, `miscale.py`, `run-scale-reader.cmd`, `venv/`,
  `token.txt`, `pending.jsonl`, `reader.log` (and `reader.log.1` after rotation).

### Install / update

```powershell
# One-time install (needs an elevated PowerShell):
.\scripts\windows\install-scale-reader-task.ps1 -Deploy -StartNow -SourceDir C:\path\to\health-sync

# Later update of the reader code only:
.\scripts\windows\install-scale-reader-task.ps1 -Deploy -StartNow -SourceDir C:\path\to\health-sync
```

### How to check

```powershell
# Task status
Get-ScheduledTaskInfo -TaskName health-sync-scale

# Process in session 0
Get-CimInstance Win32_Process -Filter "Name like 'python%' AND CommandLine like '%reader.py%'" |
  Select-Object ProcessId, SessionId, CommandLine
```

Expect `SessionId = 0`. `reader.log` grows in the install dir; each restart writes a timestamped
"Starting reader..." line.

### Undo

```powershell
# Find the latest backup XML in the install dir and re-register it:
$bak = Get-ChildItem C:\Tools\health-sync-scale\health-sync-scale.bak-*.xml | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Register-ScheduledTask -Xml (Get-Content $bak.FullName -Raw) -TaskName health-sync-scale -Force
Start-ScheduledTask -TaskName health-sync-scale
```

Or simply unregister the task and fall back to the old interactive `run.cmd`:

```powershell
Unregister-ScheduledTask -TaskName health-sync-scale -Confirm:$false
& C:\Tools\health-sync-scale\run.cmd
```

## Licensing policy

Only permissive dependencies (MIT/BSD/Apache) are used; GPL or unlicensed code is not copied.
