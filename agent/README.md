# DBSentinel agent (Windows)

The agent runs on the laptop you want to monitor. Every few seconds it collects that machine's OS data and, optionally, its local PostgreSQL activity, and sends it over HTTPS to a DBSentinel server. The dashboard then shows **that laptop**, not the server.

```
this laptop: dbsentinel-agent.exe  ──HTTPS, X-API-Key──►  DBSentinel API  ──►  dashboard
```

The agent only makes outbound connections. It opens no port and never changes anything on the machine: no process is killed, no file is touched, and its PostgreSQL session is read-only.

## What it sends

| Data | Details |
|---|---|
| Machine | hostname, Windows version, CPU %, core count, memory, system-drive usage, mounted volumes, system-wide disk I/O |
| Processes | up to 100 per snapshot: the 60 busiest by CPU plus the 40 largest by memory. Name, PID, user, CPU % (of total CPU), memory %, and cumulative disk read/write bytes |
| PostgreSQL (optional) | connection counts, up to 30 sessions with their query text (cut to 500 characters), and blocked/blocking session pairs |
| Identity | a random device id (stored in `agent.state.json`) and the device name |

Not sent: file contents, browsing data, keystrokes, or the PostgreSQL password. Query text can contain sensitive values, so set `"send_query_text": false` if that matters. The agent then sends `<hidden by agent config>` instead of the SQL.

## Quick start

You need two values from whoever runs the DBSentinel server: its **address** (for example `https://dbsentinel.onrender.com`) and the **ingest key** (the server's `INGEST_KEY`).

1. Put `dbsentinel-agent.exe` in a folder you will keep, for example `C:\DBSentinel-Agent`.
2. Double-click it (or run it from a terminal). With no configuration yet it opens a short **guided setup**:

   | Step | What it asks |
   |---|---|
   | 1 Server | the server address and the ingest key (typing is masked) |
   | 2 This computer | the name to show in the dashboard (default: the computer name) |
   | 3 PostgreSQL | whether a PostgreSQL runs on this computer. If yes: host, port, user, password and database. It connects right away and shows the result before saving |
   | 4 Connection check | sends one snapshot to the server and reports whether it was accepted |
   | 5 Startup | whether to start the agent at every Windows login (installs the scheduled task for you) |

   It then shows a summary and asks whether to run the agent now. The settings go into `agent.config.json` next to the exe, and the file is restricted to your Windows user because it holds the key and the database password.
3. Open the dashboard. Your laptop appears in the device dropdown within a few seconds.

To change the settings later, run `dbsentinel-agent.exe --setup`, or edit `agent.config.json` by hand (see below). To check the connection at any time: `dbsentinel-agent.exe --once` should print `Accepted by the API: HTTP 200`.

The wizard uses colours when the terminal supports them and falls back to plain text otherwise (for example when output is redirected, or when the `NO_COLOR` environment variable is set).

Windows may show a "Windows protected your PC" (SmartScreen) or antivirus warning for the first run, because the exe is unsigned. Only run an exe you built yourself or got from someone you trust. If the exe arrived in a zip downloaded from a browser or chat app, right-click the zip, choose Properties and tick **Unblock** before extracting.

### Start automatically at login

The guided setup offers this in step 5. To do it later, in the folder that holds the exe and `agent.config.json` run:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
```

This creates a Scheduled Task called **DBSentinel Agent** that starts the agent at every logon, in the background with no window, and starts it once right now. It writes `agent.log` next to the exe. Remove it with:

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1
```

By default the agent runs with your normal rights, which is enough for most data. Windows hides a few protected system processes from non-administrators, so their CPU and I/O read as 0 and snapshots take slightly longer. To read everything, run `install-autostart.ps1 -RunElevated` from an **Administrator** PowerShell.

## Configuration (`agent.config.json`)

| Key | Default | Meaning |
|---|---|---|
| `api_url` | required | Server address, no trailing path. Must be `https://...` unless it points to localhost |
| `api_key` | required | The server's `INGEST_KEY` |
| `device_name` | computer name | Name shown in the dashboard |
| `interval_seconds` | `5` | Seconds between snapshots (minimum 2) |
| `request_timeout_seconds` | `30` | How long to wait for the server. Free hosting can take up to a minute to wake up |
| `allow_insecure_http` | `false` | Allow plain `http://` to a remote server. This sends the key unencrypted. Avoid it |
| `postgres.enabled` | `true` | Set `false` to monitor the OS only |
| `postgres.dsn` | empty | Connection string of the local PostgreSQL. With an empty DSN the dashboard shows "not configured" and raises no alert |
| `postgres.send_query_text` | `true` | `false` hides SQL text |

The environment variables `DBSENTINEL_API_URL`, `DBSENTINEL_API_KEY` and `DBSENTINEL_PG_DSN` override the file. That lets you keep secrets out of it.

### Monitoring PostgreSQL

Use a dedicated read-only role instead of the `postgres` superuser. Run this as an administrator, with your own password:

```sql
CREATE ROLE dbsentinel_monitor LOGIN PASSWORD 'choose-a-password';
GRANT pg_monitor TO dbsentinel_monitor;
```

Then set `"dsn": "postgresql://dbsentinel_monitor:choose-a-password@localhost:5432/postgres"`. URL-encode any `@ # / :` in the password.

`pg_monitor` (PostgreSQL 10 or newer) lets the role see every user's sessions and queries. Without it the agent still connects, but PostgreSQL hides other users' activity, and the dashboard says "limited visibility". The agent opens one connection, tagged `application_name=dbsentinel-agent`, with a 3-second statement timeout, and sets the session read-only.

## Command line

| Command | What it does |
|---|---|
| `dbsentinel-agent.exe` | Run until stopped |
| `dbsentinel-agent.exe --setup` | Run the guided setup again |
| `dbsentinel-agent.exe --once` | Send one snapshot and exit. Exit code 0 means the server accepted it |
| `dbsentinel-agent.exe --dry-run` | Print one snapshot as JSON without sending it. This is exactly what would be sent |
| `dbsentinel-agent.exe --background` | No window, log to file only (what the auto-start task uses) |
| `dbsentinel-agent.exe --config FILE` | Use another config file |
| `dbsentinel-agent.exe --verbose` | More logging |

Files created next to the exe: `agent.state.json` (the device id; keep it, or the laptop appears as a new device), `agent.log` (rotates at 1 MB, 2 backups) and `agent.lock` (prevents two agents running from the same folder).

## Behaviour to know about

- If the server is unreachable, the agent logs a warning and waits longer between attempts (up to 60 seconds), then resumes by itself. Snapshots taken during an outage are **not** replayed later; the server stamps data with the time it arrives.
- Per-process CPU is shown as a percentage of the whole machine, so it matches the system CPU figure.
- The dashboard marks a device offline when it has not reported for 15 seconds.

## Troubleshooting

| Message or symptom | Cause and fix |
|---|---|
| `HTTP 401 ... the API key was rejected` | `api_key` does not match the server's `INGEST_KEY` |
| `HTTP 503 ... INGEST_KEY is not configured` | The server has no `INGEST_KEY` set yet |
| `cannot reach https://...` | No internet, wrong `api_url`, or the free server is waking up. The agent keeps retrying |
| `config file not found` | `agent.config.json` is not in the same folder as the exe, or pass `--config` |
| `Another DBSentinel agent is already running` | Stop the other one (or the scheduled task) first |
| Dashboard says PostgreSQL "disconnected" | Read the message next to it, such as a wrong password or a PostgreSQL service that is not running |
| Dashboard says "limited visibility" | Grant `pg_monitor` to the monitoring role |
| Laptop missing from the dropdown | Run `--once` and read the result. Check `agent.log` |

## Build the exe yourself

You need Windows and Python 3.10 or newer.

```powershell
cd agent
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

This creates `agent\dist\` with `dbsentinel-agent.exe`, the config example, the two auto-start scripts and this README. Zip that folder to give it to someone else.

To run from source instead: `pip install -r requirements.txt`, then `python agent.py`.
