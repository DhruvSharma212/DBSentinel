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

The API server no longer measures anything itself, so the dashboard never shows the server's own CPU or memory. It shows the machines that run an agent.

## Features

- **OS monitoring (per device):** CPU, RAM, system-drive and volume usage, system-wide disk I/O, and the top 100 processes (busiest by CPU plus largest by memory) with per-process disk read/write counters, collected with `psutil`.
- **PostgreSQL monitoring (per device, optional):** connection counts, active/idle sessions, query text, and blocking/blocked session pairs from `pg_stat_activity` and `pg_blocking_pids`. Only real client sessions are counted, not background workers.
- **Rule-based alerts** (evaluated on every snapshot):

  | Condition | Threshold | Severity |
  |---|---|---|
  | High CPU | ≥ 85 % | high |
  | Memory pressure | ≥ 90 % | critical |
  | Low disk space | ≥ 90 % used | high |
  | PostgreSQL configured but unreachable | — | warning |
  | A session blocked by another | — | warning |
  | CPU high **and** PostgreSQL has active sessions | ≥ 85 % + active > 0 | warning (correlation lead) |

  The correlation rule is an investigative hint, not proof of cause. Lock waits are also saved as correlation-lead events, but only the six rows above appear as live alerts.
- **PostgreSQL evidence store:** snapshots, events and investigation notes persist in PostgreSQL (see the tables below). The last 1,000 snapshots are kept **per device**, which is about 83 minutes at one snapshot every 5 seconds.
- **Multi-device dashboard:** a dropdown selects the device, with online/offline status. A device counts as offline after 15 seconds without a report.
- **Safe response suggestions:** recommendation-only. DBSentinel never kills processes, deletes files or terminates database sessions.

## Components

| Folder | What it is |
|---|---|
| `backend/` | FastAPI API: ingest, rules, evidence store, dashboard endpoints |
| `frontend/` | React + Vite dashboard |
| `agent/` | Windows agent (Python, packaged as `dbsentinel-agent.exe`). See [agent/README.md](agent/README.md) |

## Requirements

- Python 3.10 or newer
- Node.js and npm (dashboard)
- PostgreSQL for the **evidence store** (server side). The monitored laptop can also have its own PostgreSQL, which is optional.

## 1. Server: evidence database and API

### Prepare PostgreSQL (evidence store)

Run as a PostgreSQL administrator, with a password you choose:

```sql
CREATE USER dbsentinel_user WITH PASSWORD 'ChangeThisPassword123!';
CREATE DATABASE dbsentinel OWNER dbsentinel_user;
```

On first start the API creates its four tables automatically and upgrades an older DBSentinel database in place. Rows from before devices existed become the device `legacy-data`, readable with `?device_id=legacy-data`. The configured user must be able to create tables.

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
| `INGEST_KEY` | yes, to accept agents | Shared secret agents send in the `X-API-Key` header. Without it `/api/ingest` answers 503. Generate one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
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

For a hosted dashboard (for example Vercel), set the build-time variable `VITE_API_URL` to the public address of the API (see `frontend/.env.example`). Without it the page calls `/api` on its own domain. `npm run build` creates `frontend/dist`; if that folder exists, the backend also serves it.

## 3. Agent: show a laptop's data

On each Windows laptop you want to monitor, follow [agent/README.md](agent/README.md). In short:

1. Get `dbsentinel-agent.exe` (build it with `agent\build.ps1`, which produces a folder in `agent\dist` you can zip and copy).
2. Copy `agent.config.example.json` to `agent.config.json` and set `api_url` (the API address) and `api_key` (the server's `INGEST_KEY`). Optionally set `postgres.dsn` to monitor the laptop's local PostgreSQL.
3. Run `dbsentinel-agent.exe --once` to test, then run it. `install-autostart.ps1` starts it automatically at every login.
4. Open the dashboard and pick the laptop in the device dropdown.

To try everything on one machine without building the exe, run `python agent.py` from `agent/` after `pip install -r requirements.txt`, with `api_url` set to `http://127.0.0.1:8000`.

## Deployed setup (Vercel + Render)

The public instance serves the dashboard from Vercel (https://db-sentinel.vercel.app) and the API from Render. After deploying this version:

1. On the API host, set `INGEST_KEY` (and keep `DATABASE_URL`). Add `ALLOWED_ORIGINS` only if the dashboard is served from another origin.
2. On the dashboard host, set `VITE_API_URL` to the API's public URL and redeploy, because Vite bakes it in at build time.
3. Put the same key in each laptop's `agent.config.json`.

On free hosting tiers an idle API can take up to a minute to wake up, so the first dashboard load or agent request can be slow. The agent retries by itself.

## API endpoints

All read endpoints accept `?device_id=...`. Without it, the most recently seen device is used.

| Endpoint | Purpose |
|---|---|
| `POST /api/ingest` | Agent → server snapshot. Needs the `X-API-Key` header. Body is limited to 1 MB |
| `GET /api/health` | API status, evidence-store connection, number of devices |
| `GET /api/devices` | Devices with last-seen time and online flag |
| `GET /api/overview` | Latest OS/database state, recent history, current alerts |
| `GET /api/os` | OS metrics, volumes and processes |
| `GET /api/database` | PostgreSQL sessions, queries and lock relationships |
| `GET /api/history` | Up to 1,000 stored snapshots (CPU, memory, disk, connections) |
| `GET /api/events` | Saved events |
| `GET /api/forensics` | Snapshots, events and investigation notes |
| `POST /api/investigation-notes` | Save a note, optionally linked to an event |
| `GET /api/settings` | Configuration status and record counts, without secrets |

`/api/overview`, `/api/os`, `/api/database` answer 404 until a device has reported. When the evidence database is unreachable, the data endpoints answer 503 with a generic message (no connection details), while `/api/health` and `/api/settings` still answer 200 and report the problem.

## PostgreSQL tables used by DBSentinel

| Table | Purpose |
|---|---|
| `dbs_devices` | One row per device: name, host, platform, agent version, first/last seen, and its latest full snapshot (JSONB) |
| `dbs_snapshots` | Timestamped CPU, memory, disk, connection counts and a JSONB payload, per device |
| `dbs_events` | Detected conditions with evidence, severity, first/last seen and an occurrence count, per device |
| `dbs_investigation_notes` | Operator notes, linked to a device and optionally an event |

## Demo plan for viva

1. Show the evidence database in pgAdmin/psql and `/api/health` reporting it connected.
2. Show the dashboard with no agent: "No device connected yet".
3. Start the agent on the laptop (`dbsentinel-agent.exe`). Show it appearing in the device dropdown within seconds, and explain that the numbers are the laptop's, not the server's.
4. Walk through Overview and OS Monitor (CPU, memory, disk, processes with I/O).
5. If PostgreSQL is configured on the laptop, open Database Monitor. To show a lock wait, open two psql sessions: run `BEGIN; LOCK TABLE some_table IN ACCESS EXCLUSIVE MODE;` in the first and `SELECT count(*) FROM some_table;` in the second. The blocked session appears with both PIDs.
6. Open Event Correlation to explain how OS and database signals are compared over time.
7. Save an investigation note, then `SELECT * FROM dbs_investigation_notes;` to show it stored.
8. Show `SELECT * FROM dbs_devices;`, `SELECT * FROM dbs_snapshots ORDER BY id DESC LIMIT 5;` and `SELECT * FROM dbs_events ORDER BY id DESC LIMIT 10;`.
9. Stop the agent and show the device turn offline within about 15 seconds.

## Troubleshooting

- **"No device connected yet":** no agent has reported. Run `dbsentinel-agent.exe --once` on the laptop and read the message (401 means the key does not match, "cannot reach" means the URL or network).
- **Dashboard says "API disconnected":** the API is down, waking up, or `VITE_API_URL` points to the wrong place.
- **`/api/ingest` returns 503 "INGEST_KEY is not configured":** set `INGEST_KEY` on the server.
- **Connection refused / password authentication failed (server):** check that PostgreSQL is running and `DATABASE_URL` in `backend/.env` is right, then restart uvicorn.
- **Permission denied for schema public:** give the configured user create privileges on the schema, or make it the database owner.
- **Few or no PostgreSQL sessions on the dashboard:** the agent's PostgreSQL role needs `pg_monitor` to see other users' sessions. The Database Monitor page says "limited visibility" when it lacks it.
- **The browser blocks API calls (CORS):** add the dashboard's origin to `ALLOWED_ORIGINS`.

## Limitations

This is an educational prototype, not a production security appliance.

- **No user login.** Anyone who can reach the API can read the dashboard data (including process names and query text) and save notes. Agents authenticate with one shared `INGEST_KEY`, and the API cannot tell agents apart. Put it behind a private network, or add authentication, before real use.
- **Windows agent only.** The agent is built and tested on Windows.
- Snapshots sent while the API is unreachable are dropped, not replayed.
- Event counts record how many snapshots observed a condition, not how many separate incidents occurred. A long-running condition stays one row.
- Per-process disk I/O counters are cumulative since the process started, and Windows hides a few protected processes unless the agent runs as Administrator.
- Correlation leads do not prove causality, and a lock wait is not automatically a deadlock.
- There is no log ingestion from PostgreSQL or the OS, and no automatic remediation.
