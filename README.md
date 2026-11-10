# DBSentinel — OS + DBMS Integrated Monitoring & Forensics

DBSentinel is an educational project that watches an **operating system** and its **PostgreSQL database** together, raises rule-based alerts, and keeps a forensic trail in PostgreSQL.

A small **agent** runs on the laptop you want to monitor and pushes that laptop's OS and PostgreSQL data to the DBSentinel API. The web dashboard shows whichever laptop you pick, from any browser.

```
Windows laptop(s)                          Server                               Browser
┌────────────────────────────┐   HTTPS    ┌────────────────────────────┐       ┌───────────────────┐
│ dbsentinel-agent.exe        │ ─────────► │ FastAPI  (/api/ingest)      │ ◄──── │ React dashboard    │
│ psutil (OS) + psycopg2      │ every 5 s  │ rules → PostgreSQL evidence │       │ (device dropdown)  │
│ local PostgreSQL (optional) │  X-API-Key │ tables, per device          │       └───────────────────┘
└────────────────────────────┘            └────────────────────────────┘
```

The API server does not measure anything itself, so the dashboard never shows the server's own CPU or memory. It shows the machines that run an agent.

## Features

- **OS monitoring (per device):** CPU, RAM, system-drive and volume usage, disk throughput, and the top 100 processes (busiest by CPU plus largest by memory) with per-process disk read/write counters, collected with `psutil`.
- **PostgreSQL monitoring (per device, optional):** connection counts, active/idle sessions, query text, and blocking/blocked session pairs from `pg_stat_activity` and `pg_blocking_pids`. Only real client sessions are counted, not background workers.
- **Rule-based alerts**, evaluated on every snapshot:

  | Rule | Raised when | Severity |
  |---|---|---|
  | `OS-CPU` | CPU ≥ 85 % | high |
  | `OS-MEM` | memory ≥ 90 % | critical |
  | `OS-DISK` | system volume ≥ 90 % used | high |
  | `DB-CONN` | PostgreSQL is configured on the agent but unreachable | warning |
  | `LOCK-<pid>` | a session is blocked by another | warning |
  | `CORR-CPU-DB` | CPU ≥ 85 % **and** PostgreSQL has active sessions. Names the longest active query, and says when `postgres` is the busiest process | warning |
  | `CORR-MEM-DB` | memory ≥ 90 % and the PostgreSQL processes hold ≥ 10 % of RAM | warning |
  | `CORR-DISK-DB` | disk ≥ 90 % used and PostgreSQL has active sessions | warning |
  | `CORR-IO-DB` | disk throughput (read + write, measured between snapshots) ≥ 50 MB/s and active sessions | warning |
  | `CORR-LOCK-HOST` | lock waits while CPU ≥ 70 % or memory ≥ 85 % | warning |

  Correlation rules (`CORR-*`) are investigative leads, not proof of cause.
- **Incidents:** every alert becomes an incident stored in PostgreSQL. It opens at the first observation, counts the snapshots that showed it, and closes when the condition has not been seen for 30 seconds. If the condition returns, that is a new incident, so history is never merged into one endless row.
- **Investigation notes** are saved against the incident they refer to, with an author name.
- **PostgreSQL evidence store:** snapshots, incidents and notes persist in PostgreSQL (tables below). The last 1,000 snapshots are kept **per device**, about 83 minutes at one snapshot every 5 seconds.
- **Per-device access:** each laptop enrolls with a one-time code and gets its own token, so you can revoke one laptop without touching the others. The shared `INGEST_KEY` still works and can be switched off.
- **Multi-device dashboard:** a dropdown selects the device, with online/offline status. A device counts as offline after 15 seconds without a report.
- **Safe response suggestions:** recommendation-only, generated from the conditions active right now with the evidence behind each. DBSentinel never kills processes, deletes files or terminates database sessions.

## Components

| Folder | What it is |
|---|---|
| `backend/` | FastAPI API: ingest, rules, evidence store, dashboard endpoints, tests |
| `frontend/` | React + Vite dashboard (`src/api.js`, `hooks/`, `components/`, `pages/`, `lib/`), unit tests |
| `agent/` | Windows agent (Python, packaged as `dbsentinel-agent.exe`) with a guided setup. See [agent/README.md](agent/README.md) |

## Requirements

- Python 3.10 or newer
- Node.js 20 or newer and npm (dashboard)
- PostgreSQL for the **evidence store** (server side). The monitored laptop can also have its own PostgreSQL, which is optional.

## 1. Server: evidence database and API

### Prepare PostgreSQL (evidence store)

Run as a PostgreSQL administrator, with a password you choose:

```sql
CREATE USER dbsentinel_user WITH PASSWORD 'ChangeThisPassword123!';
CREATE DATABASE dbsentinel OWNER dbsentinel_user;
```

On first start the API creates its four tables automatically and upgrades an older DBSentinel database in place (every schema statement is safe to repeat). Rows from before devices existed become the device `legacy-data`, readable with `?device_id=legacy-data`. The configured user must be able to create tables.

### Start the API (Windows PowerShell)

```powershell
cd backend
py -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Settings in `backend/.env` (also usable as environment variables on a hosting service):

| Variable | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | yes | Evidence database, e.g. `postgresql://dbsentinel_user:ChangeThisPassword123!@localhost:5432/dbsentinel`. URL-encode `@ # / :` in the password |
| `ADMIN_KEY` | yes, to enroll devices | Protects administration: creating enrollment codes and revoking devices. Without it those endpoints answer 503 and enrollment is off. Use a different value from the other keys |
| `INGEST_KEY` | optional once all devices are enrolled | Shared secret an agent can send in `X-API-Key` instead of enrolling. Without it that way of reporting answers 503. Generate a key: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `SHARED_INGEST_KEY_ENABLED` | no | Set to `false` once every device is enrolled: the shared key is then refused (403) and only per-device tokens work. Default `true` |
| `DASHBOARD_KEY` | no | When set, every dashboard endpoint needs the header `X-Dashboard-Key`, and the dashboard shows a key prompt before any data. Leave it unset for an open dashboard |
| `ALLOWED_ORIGINS` | no | Comma-separated browser origins allowed to call the API. Default: `https://db-sentinel.vercel.app`, `http://localhost:5173`, `http://127.0.0.1:5173` |
| `OFFLINE_AFTER_SECONDS` | no | Seconds without a report before a device shows offline (default 15) |
| `DB_POOL_MAX` | no | Maximum database connections (default 5). Extra requests wait up to 10 s, then get a 503 |

Then start it:

```powershell
uvicorn main:app --reload --app-dir .
```

- API: http://127.0.0.1:8000, documentation: http://127.0.0.1:8000/docs, health: http://127.0.0.1:8000/api/health

## 2. Dashboard

For local development (second PowerShell window):

```powershell
cd frontend
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
npm install
npm run dev
```

Open http://localhost:5173. The Vite dev server proxies `/api` to `http://127.0.0.1:8000`. Until an agent reports, the dashboard shows "No device connected yet".

For a hosted dashboard (for example Vercel), set the build-time variable `VITE_API_URL` to the public address of the API (see `frontend/.env.example`). Without it the page calls `/api` on its own domain. `npm run build` creates `frontend/dist`; if that folder exists, the backend also serves it (and never serves files outside it).

The dashboard polls every 5 seconds with two requests (`/api/devices` and `/api/dashboard`), and pauses while the browser tab is hidden.

## 3. Agent: show a laptop's data

On each Windows laptop you want to monitor, follow [agent/README.md](agent/README.md). In short:

1. Get `dbsentinel-agent.exe` (build it with `agent\build.ps1`; `agent\dist` then holds the exe and a zip of it). It is the only file a laptop needs.
2. Open it. The first time it starts a **guided setup**: server address, enrollment code (or the shared ingest key), display name, optional local PostgreSQL. It tests the connection and can enable start-at-login.
3. Later, opening the exe again shows a small **menu**: run the agent, turn auto-start on or off, change the settings, or uninstall. No other file is needed to remove it.
4. Open the dashboard and pick the laptop in the device dropdown.

To try everything on one machine without building the exe, run `python agent.py` from `agent/` after `pip install -r requirements.txt`, with the server address `http://127.0.0.1:8000`.

## Adding laptops: enrollment codes

Instead of giving every laptop the same shared key, give each one a **one-time enrollment code**. The laptop trades the code for its own private token, so the shared key never leaves the server and a single laptop can be cut off later.

1. Set `ADMIN_KEY` on the server (see the table above).
2. In the dashboard open **Settings → Device access**, enter the admin key once (it is kept only until you close the tab), type who the code is for (it becomes the device name) and click **Create code**. A code like `K7P2-9XQM-4TWH` is shown once; it works one time and expires after 24 hours (or 3 / 7 days if you choose).
3. Get the code onto the laptop, either way:
   - Send your friend **only `dbsentinel-agent.exe`** (the build also makes a zip containing just that file, for chat or e-mail) and the code. They open it, press Enter for the suggested server address, paste the code, and press Enter for the remaining questions. There is nothing else to install or copy.
4. The laptop appears in the device list. In **Device access** you see each device, whether it uses its own token or the shared key, and a **Revoke** button. Revoking stops that laptop immediately and keeps its stored history; to let it back in, create a new code and run the agent with `--setup`.
5. When every laptop is enrolled, set `SHARED_INGEST_KEY_ENABLED=false` (or remove `INGEST_KEY`) so the old shared key no longer works.

How it is protected: codes and tokens are random, only their SHA-256 hashes are stored, a code works once and expires, tokens are bound to the device that enrolled, and after 10 wrong attempts within a minute an address is locked out of enrollment, the admin key and the ingest credentials.

## Deployed setup (Vercel + Render)

The public instance serves the dashboard from Vercel (https://db-sentinel.vercel.app) and the API from Render. A working configuration:

| Service | Setting |
|---|---|
| Render (web service) | Root directory `backend`; build `pip install -r requirements.txt`; start `uvicorn main:app --host 0.0.0.0 --port $PORT`; environment: `DATABASE_URL`, `ADMIN_KEY`, `INGEST_KEY` (optionally `DASHBOARD_KEY`) |
| Vercel (project) | Root directory `frontend`; framework Vite; build `npm run build`; output `dist`; environment `VITE_API_URL=https://dbsentinel.onrender.com` (no trailing slash) |

Redeploy the dashboard after changing `VITE_API_URL`, because Vite bakes it in at build time. Put the same ingest key in each laptop's agent. On free hosting tiers an idle API can take up to a minute to wake up; the agent retries by itself.

## API endpoints

All read endpoints accept `?device_id=...`. Without it, the most recently seen device is used. When `DASHBOARD_KEY` is set, every dashboard endpoint (not `/api/health`, `/api/ingest`, `/api/enroll` or `/api/admin/*`) needs `X-Dashboard-Key`. Admin endpoints use `X-Admin-Key` only.

| Endpoint | Purpose |
|---|---|
| `POST /api/ingest` | Agent → server snapshot. Needs `X-Device-Token` (an enrolled device; it must match the device id in the body) or, if allowed, `X-API-Key`. Body is limited to 1 MB |
| `POST /api/enroll` | Agent → server: trade a one-time code for a device token. No header needed: the code is the credential |
| `GET /api/health` | API status, evidence-store connection, number of devices, whether a dashboard key is required |
| `GET /api/devices` | Devices with last-seen time and online flag, and the API version |
| `GET /api/dashboard` | Everything one dashboard refresh needs, for one device, in one transaction: state, processes, alerts, history, incidents, notes |
| `GET /api/overview`, `/api/os`, `/api/database` | Parts of the same data |
| `GET /api/history` | Up to 1,000 stored snapshots (CPU, memory, disk, connections, disk throughput) |
| `GET /api/events` | Stored incidents with `active`, `resolved_at`, `occurrences` and `duration_seconds` |
| `GET /api/forensics` | Snapshots, incidents and investigation notes |
| `POST /api/investigation-notes` | Save a note, optionally linked to an incident (`event_id`) |
| `GET /api/settings` | Configuration status and record counts, without secrets |
| `POST /api/admin/enroll-codes` | Create a one-time code (`label`, `ttl_hours` 1–168). Needs `X-Admin-Key` |
| `GET /api/admin/enroll-codes` | List codes with their status (never the code itself). `X-Admin-Key` |
| `DELETE /api/admin/enroll-codes/{id}` | Cancel an unused code. `X-Admin-Key` |
| `GET /api/admin/devices` | Devices with how they authenticate, plus enrolled devices that have not reported yet. `X-Admin-Key` |
| `POST /api/admin/devices/{device_id}/revoke` | Cut off a device that uses its own token; its data is kept. `X-Admin-Key` |

`/api/overview`, `/api/os`, `/api/database` and `/api/dashboard` answer 404 until a device has reported. When the evidence database is unreachable, the data endpoints answer 503 with a generic message (no connection details), while `/api/health` still answers 200 and reports the problem.

**Abuse limits:** after 10 failed attempts within a minute, an address is locked out of that credential (ingest credentials, dashboard key, admin key, enrollment codes) for a minute (429); notes are limited to 30 per minute per address. These counters live in memory, per server process.

## PostgreSQL tables used by DBSentinel

| Table | Purpose |
|---|---|
| `dbs_devices` | One row per device: name, host, platform, agent version, first/last seen, and its latest full snapshot (JSONB) |
| `dbs_snapshots` | Timestamped CPU, memory, disk, connection counts and a JSONB payload, per device |
| `dbs_events` | Incidents: start, last sighting, snapshot count, `resolved_at`, evidence, and the rule id (in `details`), per device |
| `dbs_investigation_notes` | Operator notes, linked to a device and optionally an incident |
| `dbs_enroll_codes` | One-time enrollment codes: hash only, label, expiry, used/revoked state |
| `dbs_device_tokens` | Per-device tokens: hash only, device id, created/last used/revoked |

## Tests

```powershell
# backend + agent: need a PostgreSQL. Either set TEST_DATABASE_URL (a server where the user may create
# databases) or install the optional embedded server that the dev requirements include.
pip install -r backend/requirements-dev.txt -r agent/requirements-dev.txt
python -m pytest backend
python -m pytest agent

cd frontend
npm test            # dashboard logic (recommendations, thresholds, formatting)
```

`.github/workflows/ci.yml` runs the same checks on every push and pull request: backend and agent tests against a PostgreSQL 16 service, the agent tests on Windows, and the dashboard tests and build.

## Demo plan for viva

1. Show the evidence database in pgAdmin/psql and `/api/health` reporting it connected.
2. Show the dashboard with no agent: "No device connected yet".
3. Open the agent on the laptop. The guided setup appears; show it appearing in the device dropdown within seconds, and explain that the numbers are the laptop's, not the server's.
4. Walk through Overview (tiles turn amber/red near the alert thresholds) and OS Monitor (processes with I/O, disk throughput chart).
5. If PostgreSQL is configured on the laptop, open Database Monitor. To show a lock wait, open two psql sessions: run `BEGIN; LOCK TABLE some_table IN ACCESS EXCLUSIVE MODE;` in the first and `SELECT count(*) FROM some_table;` in the second. The blocked session appears with both PIDs.
6. Open Incidents & Forensics: the live conditions, the stored incident history (start, duration, snapshots, status), then Investigate an incident and save a note. Show it under Audit Trail with its incident.
7. Open Safe Response: the recommendations come from the conditions active right now.
8. Show `SELECT * FROM dbs_devices;`, `SELECT id, message, timestamp, last_seen, occurrences, resolved_at FROM dbs_events ORDER BY id DESC LIMIT 10;` and `SELECT * FROM dbs_investigation_notes;`.
9. Open Settings → Device access, create a code, enroll a second laptop (or a second agent folder), then **Revoke** it and show that only that device stops while the first keeps reporting.
10. Stop the agent and show the device turn offline within about 15 seconds.

## Troubleshooting

- **"No device connected yet":** no agent has reported. Run `dbsentinel-agent.exe --once` on the laptop and read the message (401 means the key does not match, "cannot reach" means the URL or network).
- **Dashboard says "API disconnected":** the API is down, waking up, or `VITE_API_URL` points to the wrong place.
- **The dashboard asks for a key:** `DASHBOARD_KEY` is set on the server. Use that key. After 10 wrong attempts within a minute the server refuses further attempts from your address for a minute.
- **`/api/ingest` returns 503 "INGEST_KEY is not configured":** the agent is using the shared key but the server has none. Enroll the device with a code instead, or set `INGEST_KEY`.
- **Device access says enrollment is switched off:** set `ADMIN_KEY` on the server and reload the page.
- **"Invalid, used, revoked or expired enrollment code":** a code works once and lasts 24 hours by default. Create a new one.
- **Connection refused / password authentication failed (server):** check that PostgreSQL is running and `DATABASE_URL` in `backend/.env` is right, then restart uvicorn.
- **Permission denied for schema public:** give the configured user create privileges on the schema, or make it the database owner.
- **Few or no PostgreSQL sessions on the dashboard:** the agent's PostgreSQL role needs `pg_monitor` to see other users' sessions. The Database Monitor page says "limited visibility" when it lacks it.
- **The browser blocks API calls (CORS):** add the dashboard's origin to `ALLOWED_ORIGINS`.

## Limitations

This is an educational prototype, not a production security appliance.

- **No user accounts.** The optional `DASHBOARD_KEY` is one shared secret for everyone who may view the dashboard, and `ADMIN_KEY` is one secret for administration. Without a dashboard key, anyone who can reach the API can read the dashboard data (including process names and query text) and save notes. Enrolled laptops have their own revocable tokens, but while `INGEST_KEY` is enabled any holder of it can still report as any device: switch it off once everyone is enrolled. A device token sits in plain text in that laptop's config file (readable only by its Windows user). Use HTTPS only, and put it behind a private network or add real authentication before real use.
- **Windows agent only.** The agent is built and tested on Windows.
- Snapshots sent while the API is unreachable are dropped, not replayed.
- Incidents end 30 seconds after the last sighting, so a condition that flickers faster than that stays one incident.
- Per-process disk I/O counters are cumulative since the process started, and Windows hides a few protected processes unless the agent runs as Administrator.
- Correlation leads do not prove causality, and a lock wait is not automatically a deadlock. `CORR-MEM-DB` can only see PostgreSQL processes among the 100 the agent sends.
- Rate limits are per server process and in memory; they reset on restart.
- There is no log ingestion from PostgreSQL or the OS, and no automatic remediation.
