# From a household tool to a product

health-sync started as one household's pipeline (Garmin -> Drive/Sheets/Calendar, a Bluetooth scale, a RAG-friendly
digest). This document is the plan for turning it into a service other people can use, possibly paid. It is a plan, not
a promise: items are ordered by risk, and each phase ends with something shippable.

## 1. What the product is

"Your health and fitness data from every device, in the places you already live: your Drive, your calendar, your notes,
your AI assistant - owned by you, not locked in a vendor app."

- **Who**: people who own a Garmin (later Withings, Xiaomi, Polar, Apple Health exports, ...), care about their data, and
  already use Google Workspace / Notion / Obsidian / Telegram. Second audience: households sharing one scale or one
  account; third: coaches/clinicians who need a clean export (consent-based).
- **Not**: another dashboard app. The output is files, sheets, calendar events and markdown digests that other tools and
  assistants can read.
- **Differentiators**: user-owned destinations; digests designed for LLM/RAG use; multi-person scale handling;
  open source core.

## 2. Where we are (October 2026)

Working for one household: Garmin source (activities, sleep, HRV, body battery, stress, readiness, blood pressure,
weight), sinks (Sheets, Drive raw JSON, monthly markdown digests, Google Calendar), Mi Scale 2 reader + ingest that
uploads to Garmin and separates people by weight. Configuration is environment variables; one OAuth client; one Garmin
account; pipelines are Python code; deployment is two CronJobs + one Deployment.

## 3. Gaps between "works for me" and "works for others"

| Area | Today | Needed for a product |
|---|---|---|
| Garmin access | unofficial `garminconnect`, password/MFA login, Cloudflare blocks datacenter IPs | see section 4 |
| Google access | one OAuth client, scopes `drive` + `calendar` (restricted/sensitive) | verified app with narrow scopes (section 5) |
| Users | single tenant, env vars | tenants, per-user config + encrypted secrets, onboarding |
| Pipelines | hard-coded in `cli.py` | declarative (YAML/JSON) pipelines stored per user |
| Scheduling | Kubernetes CronJobs | job queue + per-user schedules, rate limits, retries, backfill jobs |
| Bluetooth scale | reader runs on one PC at home | small agent (Windows/macOS/Linux/Raspberry Pi) or a phone app bridge |
| Observability | pod logs | per-user run history, error surfacing in a UI, alerts |
| Privacy/legal | n/a | consent, DPA/privacy policy, deletion/export, audit log (section 6) |
| Tests | unit tests, no integration | recorded-fixture tests per connector, contract tests for sinks |

## 4. Data sources: reducing the Garmin risk

The unofficial Garmin API is the biggest product risk (can break or be blocked; terms of use; login from cloud IPs).
Options, in the order we would pursue them:

1. **Official route**: apply for the Garmin Connect Developer Program (Health/Activity APIs). Partner-only and slow, but it
   is the only durable and legally clean way for a hosted service. Start the application early.
2. **User-side agent** (works without partner status): a small open-source agent that logs in from the user's own machine
   or home network (where Garmin accepts the login) and pushes data to the user's destinations. The hosted part then
   never sees Garmin credentials. This is also how today's setup behaves in practice (ticket exchange from a browser).
3. **File-based import**: Garmin's data export (FIT/CSV/JSON via the account data-management request) and FIT files from
   the device: slow but fully legitimate, good for first import and as a fallback.
4. **Aggregators** (Terra, Open Wearables-style projects, Health Connect / Apple Health on the phone) for the long tail
   of devices, behind the same `Source` contract.

Connector contract stays the same (`Source.fetch(since)` -> `Record`), so a new source is a module, not a rewrite.

## 5. Google access: verification and scopes

Best practices we follow (and what changes for a product):

- **Least privilege, one token per feature.** Already done: `GOOGLE_REFRESH_TOKEN` (Drive + Sheets) and
  `GOOGLE_CALENDAR_REFRESH_TOKEN` (Calendar) are separate grants, so a revoked or narrower grant breaks one feature, not all.
- **Do not request scopes when refreshing.** The access token then matches exactly what the user granted; a token that
  lacks a newer scope fails only the call that needs it. Request scopes only at consent time, and use incremental
  authorization (`include_granted_scopes`) when a feature is turned on later.
- **Narrowest scope that works.** Calendar: `calendar.events` (read/write events) + `calendar.calendarlist.readonly` (find a
  calendar by name) instead of full `calendar`; for a calendar created by the app itself, `calendar.app.created` is even
  narrower. Sheets: the Drive scope also covers the Sheets API, so no separate `spreadsheets` grant.
- **Drive**: `drive` is a restricted scope (a third-party security assessment is required for public apps). For a product use
  `drive.file` (only files the app creates, or the user picks through Google Picker) or the app-data folder; the personal
  instance keeps `drive` only because it writes into folders the owner created by hand.
- **Verification**: publish the consent screen with a real domain, privacy policy and terms; complete Google verification
  (sensitive scopes) before inviting people outside a test group; keep the app in production status (testing-mode refresh
  tokens expire after 7 days).
- **Other destinations early** (Markdown/Obsidian over Git or WebDAV, Notion, Telegram digest, CSV download), so the product
  does not depend on one vendor's policy.
- **Operational**: store refresh tokens encrypted, rotate on suspicion, log which scopes each token has (the token response
  lists them), and surface "reconnect Google" in the UI when a refresh fails.

## 6. Privacy, security, legal

Health data is special-category personal data (GDPR Art. 9) and may be sensitive health information elsewhere.

- Explicit consent per source and per destination; clear "what we read / where it goes / how to delete" screens.
- Data minimisation: the hosted service should store tokens and run metadata, not health data. Prefer streaming data from
  source to the user's destination; keep only what is needed for retries, encrypted, with short retention.
- Secrets: per-user envelope encryption (KMS), no plaintext tokens in logs, scoped service accounts, rotation.
- User rights: export everything, delete everything (including revoking OAuth tokens), activity log.
- Documents: privacy policy, terms, subprocessor list, data-processing addendum, cookie-free site; legal review before
  charging money. Check Garmin and Google API terms for resale/commercial use.
- Multi-person devices: the scale logic stores other people's data only on the owner's side, never uploads it elsewhere
  without that person's own consent and account.
- Security hygiene: dependency scanning, SAST in CI, signed releases, a security contact (SECURITY.md), pen-test before GA.

## 7. Architecture for multi-tenant

```
web app (onboarding, connections, pipelines, run history)
   |  REST
control plane (tenants, connections, pipeline definitions, schedules, secrets) -- DB + KMS
   |  jobs
worker pool (Source -> transforms -> Sinks; one job = one user + one pipeline; idempotent, resumable)
   |  optional
user-side agent (Bluetooth scale, Garmin login from home network, local files) -- outbound HTTPS only
```

- Pipelines become data: `source`, `transforms` (filters, per-person routing), `sinks`, schedule, backfill window.
- State (cursors) moves from a JSON file to a table keyed by (tenant, pipeline); every sink write is an idempotent upsert
  with deterministic ids (already true for Calendar, Sheets rows by key, Drive files by name).
- Per-user rate limiting and backoff for Garmin/Google; global circuit breaker if Garmin starts returning 403/429.
- Self-hosting stays first-class: same image, `docker compose up`, env-var config. The hosted product is the same code plus
  the control plane.

## 8. Phases

**Phase 0 - harden the single-tenant tool (now)**
Fix known issues (see ARCHITECTURE "Open points"), add recorded-fixture tests for Garmin and Calendar, structured logs and
a failure notification (Telegram/e-mail), SECURITY.md and CONTRIBUTING.md, tagged releases and a changelog.

**Phase 1 - self-hostable package**
Pipelines as YAML, config file instead of scattered env vars, `docker compose` quick start, a documented Google Cloud
setup script (OAuth client, APIs), a second user in the same instance (household), docs site. Goal: a technically inclined
person installs it in under 30 minutes.

**Phase 2 - private beta (hosted, free, 10-20 users)**
Control plane + web onboarding, Google verification with narrow scopes, user-side agent for Garmin login and Bluetooth,
run history UI, alerts. Garmin Developer Program application in parallel. Collect: which sources/destinations people
actually ask for, failure rates, support load.

**Phase 3 - public launch**
Official Garmin access if granted (otherwise the agent-based model is documented and supported), billing (Stripe),
free tier (one source, daily sync, Sheets/Markdown) and paid tier (more sources, calendar, digests for assistants,
multi-person, history backfill, priority support), status page.

**Phase 4 - ecosystem**
More sources (Withings, Polar, Coros, Apple Health export, Oura), more destinations (Notion, Obsidian, Telegram, email
digests), a public connector SDK, community connectors, optional coach/clinician sharing with per-item consent.

## 9. Business model sketch

Open core: the engine and connectors stay Apache-2.0 (self-host is free). Revenue from the hosted service
(convenience, maintenance of fragile integrations, support): subscription per user, family plan for multi-person
setups, optional team/coach plan. Do not sell data; say so on the pricing page.

## 10. Risks and how we handle them

| Risk | Mitigation |
|---|---|
| Garmin blocks or changes the unofficial API | official programme application; user-side agent; file import fallback; no hard dependency in the core |
| Google verification/cost for broad scopes | narrow scopes from the start; alternative destinations |
| Health-data regulation | minimal storage, consent flows, legal review, EU hosting option |
| Support burden from flaky integrations | run history + self-diagnosis in the UI, clear "this is down at the vendor" status |
| Competition from vendor apps and aggregators | focus on user-owned destinations and LLM-friendly digests; do not compete as a dashboard |

## 11. Open questions

- Hosted-only agent-less Garmin access is probably impossible without partner status: is a user-side agent acceptable to
  the target users, or is the self-host route enough for phase 2?
- Which destination first for non-Google users: Markdown/Git, Notion, or Obsidian?
- Pricing and who the first 10 beta users are (households of existing users first).
- Whether to split the repository (engine vs hosted control plane) before phase 2.
