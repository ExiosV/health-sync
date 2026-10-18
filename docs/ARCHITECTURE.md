# health-sync: architecture and roadmap

Goal: move health/fitness data between services and keep a personal copy
(Drive/Sheets, git digests, RAG for a personal assistant). Garmin Connect is the primary
store. Designed so it can later become a product for other users.

## Principles

- **Connectors, not scripts.** Every service is a plugin with one of two roles:
  - `Source`: `fetch(since) -> Iterable[Record]`
  - `Sink`: `write(records)`; a service may be both (Garmin: source + weight sink).
- **One canonical record model** (`health_sync/core/model.py`): `kind` (activity, sleep,
  hrv, body_battery, stress, blood_pressure, weight, lab_result, note...),
  `source`, `external_id`, `start`/`end` (UTC), `payload` (dict), `raw` (original).
  Dedup key = `(source, kind, external_id)`.
- **Raw first.** Source output is stored as-is (JSON per day) in a local/Drive
  archive; sinks are derived from it and can be rebuilt.
- **State per (connector, kind)**: last-synced cursor, idempotent re-runs.
- **Per-user config, no globals**: `users/<id>.yaml` + secrets via env/secret
  manager. Nothing is hard-wired to one person (multi-tenant later).
- **Privacy**: health data is private by default; digests committed to git
  contain aggregates only, never raw dumps.
- **Pipelines in YAML**: `source -> transforms -> sink(s)`, run by CLI
  (`health-sync run <pipeline>`), cron or GitHub Actions.

## Layout (target)

```
src/
  core/        model, state, pipeline runner, config
  connectors/  garmin/, xiaomi_scale/, gsheets/, gcal/, gdrive/, workflowy/, rag/
  cli.py
docs/
```
Existing `src/sync_stats.py` (activities -> Google Sheet) becomes
`connectors/garmin` (source) + `connectors/gsheets` (sink).

## Workstreams

1. **Garmin health data** (sleep, body battery, stress, HRV, blood pressure,
   resting HR, SpO2, training readiness, weight). Build on the
   `garminconnect` library already used (130+ endpoints; covers all of these).
   Alternative to evaluate: garmin-mcp servers (read-only, same library inside).
   Risk: unofficial API, Garmin can break it; official Health API is partner-only.
2. **Xiaomi Mi Body Composition Scale 2 -> Garmin.** Licenses decide:
   `export2garmin` (the most complete) is GPL-3.0 and `miscale2garmin` forks are old, so
   neither is copied. Own connector from permissive parts: `bleak` (MIT) for BLE,
   body-composition formulas from `lolouk44/xiaomi_mi_scale` (MIT, with attribution),
   and weight upload via `garminconnect` (MIT). Two parts: a small BLE reader on the machine with Bluetooth that POSTs readings, and an
   ingest service that uploads them to Garmin (implemented).
3. **RAG ingestion.** Sources: Drive (analyses, doctors' documents),
   Workflowy, Garmin archive. Chunk + embed into per-person space (see
   `omb-memory` registry, Qdrant). Health data = sensitive: separate
   collection, explicit opt-in per source.
4. **Productisation** (later): config-driven connectors, OAuth instead of
   passwords for Google, a web UI to pick sources/sinks, per-user secrets.

## Status

1. Connectors + canonical model: done (activities -> Sheet).
2. Garmin daily health data -> sheets, raw JSON archive and monthly markdown digests on Drive: done.
3. Mi Scale 2 -> Garmin (BLE reader + ingest, people told apart by weight): done; syncing the second person's
   measurements to their own Garmin account: planned.
4. RAG: the monthly digests are plain markdown in Drive and are picked up by the assistant's indexer: done.
5. Garmin activities and sleep -> Google Calendar (`garmin-to-calendar`): done. The calendar is addressed by id or
   by name (`GOOGLE_CALENDAR_NAME`, e.g. "Дневник") so one code base serves any user; events carry a deterministic id
   (upsert, no duplicates) and a `healthSync*` tag. Needs the Calendar OAuth scope on the refresh token.
6. Next: more sources (other scales, blood-pressure monitors), YAML pipelines, per-user configuration for a hosted service.

## Open points

- Garmin login from a datacenter/home IP can be blocked (Cloudflare); the one-time ticket exchange is the workaround.
- The Garmin API is unofficial and can change without notice.
