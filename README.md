# DBSentinel — OS + DBMS Integrated Monitoring & Forensics

DBSentinel is an educational project that combines **Operating System resource monitoring** with **PostgreSQL DBMS monitoring**. PostgreSQL is the primary database for both live DB activity and DBSentinel's own persistent evidence records. There is no SQLite dependency.

## Features

- **OS monitoring:** CPU, RAM, disk usage, mounted volumes, running processes, and available process/disk I/O counters using `psutil`.
- **PostgreSQL monitoring:** current connections, active/idle sessions, query snippets, and blocking/blocked session relationships using PostgreSQL system views (`pg_stat_activity` and `pg_blocking_pids`).
- **OS + DBMS correlation:** explainable rule-based leads when OS resource pressure and DB activity coincide. These are investigative hints, not proof of root cause.
- **PostgreSQL evidence store:** telemetry snapshots, events, occurrence counts, and investigation notes are stored in PostgreSQL tables created by the app: `dbs_snapshots`, `dbs_events`, and `dbs_investigation_notes`.
- **Forensic history:** keeps the most recent 1,000 snapshots; event and note records persist across backend restarts.
- **Safe response suggestions:** recommendation-only; the app does not kill processes, delete files, or terminate database sessions.

## Requirements

- Python 3.10 or newer
- Node.js and npm
- PostgreSQL installed and running locally (or a reachable PostgreSQL server)

## 1. Prepare PostgreSQL

Open **SQL Shell (psql)** or pgAdmin Query Tool and run the following as a PostgreSQL administrator. Change the password to one you choose:

```sql
CREATE USER dbsentinel_user WITH PASSWORD 'ChangeThisPassword123!';
CREATE DATABASE dbsentinel OWNER dbsentinel_user;
```

The application connects to the `dbsentinel` database. On first start it creates the three `dbs_*` tables automatically. For the class demo, using your local PostgreSQL administrator account is also possible, but a dedicated project user is a better practice. The configured user must be able to create tables in the target schema and read the PostgreSQL activity views needed for monitoring.

## 2. Start the backend (Windows PowerShell)

Open PowerShell in the extracted `DBSentinel` project folder:

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

In `backend/.env`, set the password you chose above. Example:

```env
DATABASE_URL=postgresql://dbsentinel_user:ChangeThisPassword123!@localhost:5432/dbsentinel
```

If your password contains characters such as `@`, `#`, `/`, or `:`, URL-encode those characters in the connection URL. Save `.env`, then start the API:

```powershell
uvicorn main:app --reload --app-dir .
```

- Backend: http://127.0.0.1:8000
- API documentation: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/api/health

## 3. Start the frontend (second PowerShell window)

From the project folder, open another terminal:

```powershell
cd frontend
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
npm install
npm run dev
```

Open http://localhost:5173. The Vite development server proxies `/api` requests to the backend.

## Main API endpoints

- `GET /api/health` — backend, live PostgreSQL connection, and evidence-store status.
- `GET /api/overview` — combined OS/database snapshot, history, and current conditions.
- `GET /api/os` — host resource telemetry.
- `GET /api/database` — PostgreSQL sessions, queries, and lock relationships.
- `GET /api/history` — recent telemetry history from PostgreSQL.
- `GET /api/events` — saved events from PostgreSQL.
- `GET /api/forensics` — snapshots, events, and investigation notes from PostgreSQL.
- `POST /api/investigation-notes` — save an investigation note.
- `GET /api/settings` — configuration and record counts, without exposing credentials.

## PostgreSQL tables used by DBSentinel

| Table | Purpose |
|---|---|
| `dbs_snapshots` | Timestamped CPU, memory, disk, connection counts, and JSONB evidence payload |
| `dbs_events` | Detected conditions, evidence, severity, first/last seen, and occurrence count |
| `dbs_investigation_notes` | Analyst/operator notes linked to an event when an event ID is supplied |

The app creates these tables and indexes automatically after a successful connection. The `dbs_` prefix helps distinguish application tables from other tables in the same database.

## Demo plan for viva

1. Show PostgreSQL running and the `dbsentinel` database in pgAdmin/psql.
2. Start the FastAPI backend and show `/api/health` reporting that PostgreSQL and the evidence store are connected.
3. Open the dashboard and explain the OS metrics collected through `psutil`.
4. Open Database Monitor and explain connections, `pg_stat_activity`, and blocked-session relationships.
5. Open Event Correlation to explain how OS metrics and database activity are compared over time.
6. Save an investigation note, then query `SELECT * FROM dbs_investigation_notes;` in pgAdmin/psql to demonstrate that the record is stored in PostgreSQL.
7. Show the application tables with `SELECT * FROM dbs_snapshots ORDER BY id DESC LIMIT 5;` and `SELECT * FROM dbs_events ORDER BY id DESC LIMIT 10;`.

## Troubleshooting

- **Connection refused:** confirm the PostgreSQL service is running and port `5432` is correct.
- **Password authentication failed:** check `DATABASE_URL` in `backend/.env` and restart Uvicorn after editing it.
- **Database does not exist:** create `dbsentinel` using the SQL above or update the URL to an existing database.
- **Permission denied for schema public:** grant the configured user create privileges on the target schema, or make that user the database/schema owner for the local class demo.
- **No session/query details:** monitoring visibility depends on PostgreSQL permissions and version/configuration.

## Limitations

This is a local educational prototype, not a production security appliance. It has no authentication/RBAC, TLS configuration, multi-host collection, or native OS/PostgreSQL log ingestion. Correlation leads do not prove causality, a lock wait is not automatically a deadlock, and process I/O counters may be unavailable or cumulative. No automatic remediation is performed.

## Architecture

`React/Vite UI → FastAPI → psutil (OS telemetry) + psycopg2 (PostgreSQL monitoring) → correlation rules → PostgreSQL evidence tables → history / events / forensics`
