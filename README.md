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

### What was kept and what changed (2026-10-26)

Kept, because it works: the reader listens to passive BLE advertisements of the scale (no pairing,
no connection); measurements go to a local queue `pending.jsonl` first and are uploaded from there,
so nothing is lost while the server is down; duplicates are dropped on the server side.

Changed:

- **Idle-exit watchdog**: `reader.py --idle-exit 600` exits with code 3 when no advertisement from
  any device arrives for 10 minutes (WinRT BLE watchers can stall silently); the wrapper restarts it.
- **Background S4U task instead of a logon task with a window**: the old task `health-sync-scale` was
  `Interactive` + `AtLogOn` and ran `run.cmd` in a visible cmd window that could be closed by accident
  and did not run before logon. The new task runs in session 0 from boot. BLE from session 0 was
  verified on devicex2 on 2026-10-26 (25 devices seen vs 27 in the interactive session).
- **Recovery trigger**: besides `AtStartup` (+1 min) the task has a trigger that repeats every
  5 minutes; with `MultipleInstances IgnoreNew` it does nothing while the wrapper runs and brings the
  task back if the wrapper itself died.
- **`ping` instead of `timeout`** in the wrapper: `timeout` needs an interactive console and fails at
  once in a background task.
- **Log rotation**: `reader.log` moves to `reader.log.1` when it exceeds 10 MB.
- **Atomic queue**: `pending.jsonl` is written to a temp file and swapped in with `os.replace`;
  unparsable lines (e.g. cut off by a power loss) are skipped with a log line instead of crashing.

### What runs where

- **Task name**: `health-sync-scale` (S4U, runs as `DEVICEX2\igorp` in session 0)
- **Triggers**: `AtStartup` with a 1-minute delay, and every 5 minutes (recovery, see above)
- **Action**: `cmd.exe /c C:\Tools\health-sync-scale\run-scale-reader.cmd`
- **Wrapper** (`run-scale-reader.cmd`): loops forever, restarts `reader.py` on any exit, rotates the log,
  passes `--idle-exit 600`.
- **Files in `C:\Tools\health-sync-scale\`**: `reader.py`, `miscale.py`, `run-scale-reader.cmd`, `venv\`,
  `token.txt`, `pending.jsonl`, `reader.log` (and `reader.log.1` after rotation), the old `run.cmd`,
  backups `health-sync-scale.bak-<ts>.xml` and `<file>.bak-<ts>`.

### Prerequisites

- An **elevated** PowerShell (Run as administrator) on devicex2; Windows PowerShell 5.1 is enough.
- A checkout of this repo on devicex2, passed as `-SourceDir` (the repo root).
- `C:\Tools\health-sync-scale\venv` (with `bleak`) and `C:\Tools\health-sync-scale\token.txt` already
  exist. The installer never creates or touches `venv`, `token.txt` or `pending.jsonl`.

### Install / update

```powershell
# First install: stop the old run.cmd window, back up the old task and files, deploy, register, start.
.\scripts\windows\install-scale-reader-task.ps1 -Deploy -SourceDir C:\path\to\health-sync

# Update the reader code later (pull the repo first). Same command: re-running is safe, the task is
# re-registered with the same definition and the ORIGINAL task backup (the Interactive one) is kept.
.\scripts\windows\install-scale-reader-task.ps1 -Deploy -SourceDir C:\path\to\health-sync

# Re-register the task only (files already in place), and start it:
.\scripts\windows\install-scale-reader-task.ps1 -StartNow
```

`-Deploy` implies `-StartNow`. Order: back up the task (only if no Interactive backup exists yet) ->
stop the task and every reader process -> back up `reader.py`, `miscale.py`, `run-scale-reader.cmd`,
`run.cmd` as `<name>.bak-<ts>` -> copy -> register -> start -> wait up to 60 s for `python reader.py`
in session 0. If any step fails, the script restores the `.bak` files of this run, re-registers the
backup XML, starts it and prints `ROLLED BACK`. The script prints which backup is the original.

### How to check

```powershell
# Task status (LastTaskResult 267009 = running)
Get-ScheduledTaskInfo -TaskName health-sync-scale

# python reader.py runs in session 0
Get-CimInstance Win32_Process -Filter "Name like 'python%' AND CommandLine like '%reader.py%'" |
  Select-Object ProcessId, SessionId, CommandLine

# Recent log: the wrapper writes "starting reader" and "reader exited with code N" (3 = idle-exit)
Get-Content C:\Tools\health-sync-scale\reader.log -Tail 20

# No cmd/python window left in a user session (expect no output)
Get-Process cmd, python -ErrorAction SilentlyContinue | Where-Object SessionId -ne 0
```

The last command lists every cmd/python in user sessions, including unrelated ones; check that none of
them is `run.cmd` or `reader.py`.

### Undo

```powershell
# 1. Stop the task and the session-0 reader
Stop-ScheduledTask -TaskName health-sync-scale
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*health-sync-scale*' -and
  ($_.CommandLine -like '*reader.py*' -or $_.CommandLine -like '*run-scale-reader.cmd*') } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 2. Restore the ORIGINAL task: the oldest backup, whose principal is Interactive. The cmd window comes
#    back at the next logon (or start it now with Start-ScheduledTask).
$bak = Get-ChildItem C:\Tools\health-sync-scale\health-sync-scale.bak-*.xml | Sort-Object Name |
  Where-Object { (Get-Content $_.FullName -Raw) -match 'InteractiveToken' } | Select-Object -First 1
Register-ScheduledTask -Xml (Get-Content $bak.FullName -Raw) -TaskName health-sync-scale -Force

# 3. If needed, restore the old files, e.g.
#    Copy-Item C:\Tools\health-sync-scale\reader.py.bak-<ts> C:\Tools\health-sync-scale\reader.py -Force
#    (same for miscale.py; run.cmd is never overwritten). Keeping the new reader.py is also fine:
#    it defaults to --idle-exit 600 and the old run.cmd loops (goto loop), so it restarts it.
```

## Licensing policy

Only permissive dependencies (MIT/BSD/Apache) are used; GPL or unlicensed code is not copied.
