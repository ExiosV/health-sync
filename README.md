# health-sync

Sync and export health and fitness data between services: Garmin Connect as the primary store, Google Drive/Sheets,
a Xiaomi Mi Body Composition Scale 2 (Bluetooth), and markdown digests that a personal assistant / RAG can read.
Early-stage and opinionated: it is built for one household first, with connectors kept separate so more can be added.
Design notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). License: Apache-2.0 (keep the NOTICE when you copy or fork).

## Pipelines

| Pipeline | What it does |
|---|---|
| `garmin-activities-to-sheet` | Garmin activities -> a Google Sheet (newest first) |
| `garmin-health-to-drive` | daily summary (sleep, HRV, body battery, stress, readiness, steps...), blood pressure and weight tabs, raw JSON per day, and a monthly markdown digest in Drive |
| `scale-ingest` | HTTP endpoint that receives scale measurements and uploads weight and estimated body fat to Garmin; people are told apart by weight (`SCALE_PERSONS`), only `SCALE_GARMIN_PERSONS` are uploaded, the rest are only saved |
| `health_sync/scale/reader.py` | BLE reader for the scale; runs on any machine with Bluetooth and posts to the ingest endpoint |

```bash
cp .env.example .env     # Garmin login, Google OAuth client + refresh token, spreadsheet ids
pip install .
python -m health_sync run garmin-activities-to-sheet
python -m health_sync drive-ls --path "<Drive folder path>"
python -m health_sync init-sheet --path "<Drive folder path>" --name "<file name>"   # prints the spreadsheet id
python3 scripts/get_google_token.py url|exchange     # one-off, stdlib only: Google refresh token (Drive + Sheets)
```

Google access is a user OAuth refresh token (`GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN`, scopes Drive + Sheets); a service
account works as a fallback. Garmin tokens are kept in `GARMINTOKENS`. If direct Garmin login is blocked
(Cloudflare 429/403), `python -m health_sync garmin-ticket-login` exchanges a one-time SSO ticket from an already
signed-in browser (`GARMIN_TICKET`) for saved tokens. Garmin access uses the unofficial `garminconnect` library and can
break when Garmin changes its API.

Typical deployment is a container (see `Dockerfile`) with two CronJobs and one small Deployment; the image entrypoint is
`python -m health_sync`.

## Layout

`health_sync/core` (record model, connector contracts, state), `health_sync/connectors` (one module per service),
`health_sync/scale` (scale decoder, reader, ingest), `health_sync/digest.py`, `health_sync/cli.py` (pipelines).

## Privacy

This tool handles personal health data. Nothing is sent anywhere except your own Garmin and Google accounts.
Do not commit `.env`, tokens or exported data.

## Licensing policy

Only permissive dependencies (MIT/BSD/Apache) are used; GPL or unlicensed code is not copied.
