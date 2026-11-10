"""DBSentinel API.

The API does not collect telemetry itself. Agents running on monitored machines push snapshots to
``POST /api/ingest``; the API runs the detection rules, stores the evidence in PostgreSQL (per device)
and serves it to the dashboard.
"""
import datetime
import logging
import os
import re
import secrets
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

try:
    import psycopg2
    import psycopg2.pool
    from psycopg2.extras import Json, RealDictCursor
except Exception:  # surfaced through /api/settings and 503 responses
    psycopg2 = None
    Json = None
    RealDictCursor = None

log = logging.getLogger("dbsentinel")

BASE = Path(__file__).resolve().parent.parent
API_VERSION = "3.1.0"

# --- configuration ---------------------------------------------------------------------
DEFAULT_ORIGINS = "https://db-sentinel.vercel.app,http://localhost:5173,http://127.0.0.1:5173"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if o.strip()]
OFFLINE_AFTER_SECONDS = int(os.getenv("OFFLINE_AFTER_SECONDS", "15"))
MAX_BODY_BYTES = 1_000_000
SNAPSHOT_RETENTION = 1000
POOL_MAX = int(os.getenv("DB_POOL_MAX", "5"))
POOL_WAIT_SECONDS = 10  # how long a request waits for a free database connection before answering 503
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# detection thresholds
CPU_THRESHOLD, MEM_THRESHOLD, DISK_THRESHOLD = 85, 90, 90
HOST_LOAD_CPU, HOST_LOAD_MEM = 70, 85          # "host under load" when a lock wait is seen
IO_BPS_THRESHOLD = 50 * 1024 * 1024            # combined disk read+write throughput (bytes/s)
PG_MEMORY_SHARE = 10.0                         # % of RAM held by postgres processes to call them out
# an incident (event) ends when its condition has not been seen for this long; it is then closed
EVENT_GRACE_SECONDS = 30

app = FastAPI(title="DBSentinel API", version=API_VERSION,
              description="Agent-fed OS + PostgreSQL monitoring, forensic timeline, and safe investigation")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST", "OPTIONS"],
                   allow_headers=["Content-Type", "X-API-Key", "X-Dashboard-Key"])


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


# --- rate limiting (in memory, per client address) -------------------------------------
class RateLimiter:
    """Sliding-window counters. State is per process, which is enough to slow down guessing and spam."""

    def __init__(self):
        self._hits = {}
        self._lock = Lock()

    def _trim(self, key, window, now):
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > window:
            q.popleft()
        return q

    def count(self, key, window):
        with self._lock:
            return len(self._trim(key, window, time.monotonic()))

    def add(self, key, window):
        with self._lock:
            now = time.monotonic()
            if len(self._hits) > 20000:  # never let it grow without bound
                self._hits.clear()
            self._trim(key, window, now).append(now)

    def allow(self, key, limit, window):
        """Record one hit and say whether it is within `limit` hits per `window` seconds."""
        with self._lock:
            now = time.monotonic()
            q = self._trim(key, window, now)
            if len(q) >= limit:
                return False
            q.append(now)
            return True


limiter = RateLimiter()
BAD_KEY_LIMIT, BAD_KEY_WINDOW = 10, 60   # failed key attempts per address per minute
NOTE_LIMIT, NOTE_WINDOW = 30, 60         # notes per address per minute


def client_ip(request: Request):
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_key(request: Request, supplied, expected, bucket, what):
    """Constant-time key check with a lock-out after too many failures from one address."""
    ip = client_ip(request)
    if limiter.count((bucket, ip), BAD_KEY_WINDOW) >= BAD_KEY_LIMIT:
        raise HTTPException(status_code=429, detail="Too many failed attempts; try again in a minute.")
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        limiter.add((bucket, ip), BAD_KEY_WINDOW)
        raise HTTPException(status_code=401, detail=what)


def require_ingest_key(request: Request, x_api_key: Optional[str] = Header(default=None)):
    expected = os.getenv("INGEST_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail="INGEST_KEY is not configured on the server.")
    check_key(request, x_api_key, expected, "badingest", "Invalid or missing X-API-Key.")


def require_dashboard_key(request: Request, x_dashboard_key: Optional[str] = Header(default=None)):
    """Optional access key for the dashboard API: only enforced when DASHBOARD_KEY is set."""
    expected = os.getenv("DASHBOARD_KEY")
    if expected:
        check_key(request, x_dashboard_key, expected, "baddash", "Dashboard key required.")


PROTECTED = [Depends(require_dashboard_key)]


# --- PostgreSQL access ----------------------------------------------------------------
class StoreUnavailable(Exception):
    """The PostgreSQL evidence store cannot be used (not configured or not reachable)."""


_pool = None
_pool_lock = Lock()
# psycopg2's pool raises PoolError when exhausted instead of waiting, and FastAPI runs requests in ~40
# threads. This semaphore makes the surplus requests queue for a connection instead.
_slots = BoundedSemaphore(POOL_MAX)
_schema_ready = False
_schema_lock = Lock()

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS dbs_devices (
        device_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT '', agent_version TEXT NOT NULL DEFAULT '',
        first_seen TIMESTAMPTZ NOT NULL, last_seen TIMESTAMPTZ NOT NULL,
        last_payload JSONB NOT NULL DEFAULT '{}'::jsonb)""",
    """CREATE TABLE IF NOT EXISTS dbs_snapshots (
        id BIGSERIAL PRIMARY KEY, timestamp TIMESTAMPTZ NOT NULL,
        cpu DOUBLE PRECISION, memory DOUBLE PRECISION, disk DOUBLE PRECISION,
        db_total INTEGER, db_active INTEGER, payload JSONB NOT NULL)""",
    # An event is one incident ("episode"): `timestamp` is when it started, `last_seen` the latest snapshot
    # that still showed it, `occurrences` the number of snapshots that showed it. When the condition stops,
    # `resolved_at` is set; if it comes back later that is a new row.
    """CREATE TABLE IF NOT EXISTS dbs_events (
        id BIGSERIAL PRIMARY KEY, fingerprint TEXT NOT NULL,
        timestamp TIMESTAMPTZ NOT NULL, source TEXT NOT NULL,
        severity TEXT NOT NULL, type TEXT NOT NULL, message TEXT NOT NULL,
        evidence TEXT NOT NULL, details JSONB NOT NULL DEFAULT '{}'::jsonb,
        last_seen TIMESTAMPTZ NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
        resolved_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS dbs_investigation_notes (
        id BIGSERIAL PRIMARY KEY, event_id BIGINT REFERENCES dbs_events(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL, author VARCHAR(80) NOT NULL, note TEXT NOT NULL)""",
    # Upgrade paths for databases created by earlier versions (every statement is idempotent).
    # v2 -> v3: old rows become device 'legacy-data' (long enough to satisfy DEVICE_ID_RE, so
    # /api/...?device_id=legacy-data can still read them).
    "ALTER TABLE dbs_snapshots ADD COLUMN IF NOT EXISTS device_id TEXT NOT NULL DEFAULT 'legacy-data'",
    "ALTER TABLE dbs_events ADD COLUMN IF NOT EXISTS device_id TEXT NOT NULL DEFAULT 'legacy-data'",
    "ALTER TABLE dbs_investigation_notes ADD COLUMN IF NOT EXISTS device_id TEXT",
    # v3 -> v3.1: events become incidents. The old table-wide UNIQUE(fingerprint) is replaced by a unique
    # index over OPEN incidents only, so a condition that comes back after being resolved gets a new row.
    "ALTER TABLE dbs_events ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ",
    "ALTER TABLE dbs_events DROP CONSTRAINT IF EXISTS dbs_events_fingerprint_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dbs_events_open ON dbs_events(fingerprint) WHERE resolved_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_dbs_snapshots_timestamp ON dbs_snapshots(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dbs_events_timestamp ON dbs_events(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dbs_snapshots_device ON dbs_snapshots(device_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dbs_events_device ON dbs_events(device_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dbs_notes_device ON dbs_investigation_notes(device_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dbs_notes_event ON dbs_investigation_notes(event_id)",
]


def get_pool():
    global _pool
    if _pool is not None:
        return _pool
    if psycopg2 is None:
        raise StoreUnavailable("psycopg2-binary is not installed. Run pip install -r requirements.txt")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise StoreUnavailable("DATABASE_URL is not configured. Set it in backend/.env.")
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(1, POOL_MAX, dsn, connect_timeout=5)
        return _pool


def _checkout():
    """Get a live connection (waiting for a free slot), discarding any the server has silently closed.
    Every successful _checkout must be paired with _checkin."""
    if not _slots.acquire(timeout=POOL_WAIT_SECONDS):
        raise StoreUnavailable("The database connection pool is busy; try again shortly.")
    try:
        pool = get_pool()
        for _ in range(POOL_MAX + 1):
            conn = pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return pool, conn
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                pool.putconn(conn, close=True)
        raise StoreUnavailable("Could not obtain a working PostgreSQL connection.")
    except BaseException:
        _slots.release()
        raise


def _checkin(pool, conn, broken):
    try:
        pool.putconn(conn, close=broken)
    finally:
        _slots.release()


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        pool, conn = _checkout()
        broken = False
        try:
            with conn.cursor() as cur:
                for stmt in SCHEMA:
                    cur.execute(stmt)
            conn.commit()
            _schema_ready = True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            raise
        finally:
            _checkin(pool, conn, broken)


@contextmanager
def db_cursor():
    """One transaction on a pooled connection; yields a dict cursor and commits on success."""
    ensure_schema()
    pool, conn = _checkout()
    broken = False
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cur
        finally:
            cur.close()
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            broken = True
        raise
    finally:
        _checkin(pool, conn, broken)


@app.exception_handler(StoreUnavailable)
async def _store_unavailable(_request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


if psycopg2 is not None:
    @app.exception_handler(psycopg2.OperationalError)
    @app.exception_handler(psycopg2.InterfaceError)
    async def _store_down(_request, exc):
        global _schema_ready
        _schema_ready = False  # re-run the (idempotent) schema check once the database is back
        log.warning("PostgreSQL unavailable: %s", exc)
        return JSONResponse(status_code=503, content={
            "detail": "PostgreSQL evidence store unavailable; check DATABASE_URL, the PostgreSQL service and permissions."})


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if request.method == "POST" and length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    return await call_next(request)


# --- ingest payload --------------------------------------------------------------------
class OSMetrics(BaseModel):
    host: str = "unknown"
    platform: str = "unknown"
    cpu_percent: float
    cpu_count: int = 1
    memory_percent: float
    memory_used: int = 0
    memory_total: int = 0
    memory_available: int = 0
    disk_percent: float
    disk_used: int = 0
    disk_total: int = 0
    disk_free: int = 0
    disk_read_bytes: Optional[int] = None
    disk_write_bytes: Optional[int] = None
    process_count: int = 0


class Partition(BaseModel):
    device: str = ""
    mountpoint: str = ""
    filesystem: Optional[str] = None
    percent: float = 0
    used: int = 0
    total: int = 0
    free: int = 0


class ProcessInfo(BaseModel):
    pid: int
    name: str = "unknown"
    username: Optional[str] = None
    cpu_percent: float = 0
    memory_percent: float = 0
    status: Optional[str] = None
    disk_read_bytes: Optional[int] = None
    disk_write_bytes: Optional[int] = None


class DbCounts(BaseModel):
    active: int = 0
    idle: int = 0
    total: int = 0


class DbSession(BaseModel):
    pid: int
    username: Optional[str] = None
    database: Optional[str] = None
    state: Optional[str] = None
    duration_seconds: Optional[int] = None
    query: Optional[str] = None


class DbLock(BaseModel):
    blocked_pid: int
    blocking_pid: int
    blocked_query: Optional[str] = None
    blocking_query: Optional[str] = None


class DbInfo(BaseModel):
    configured: bool = True      # False when the agent has no PostgreSQL to watch (not an alert)
    connected: bool = False
    message: str = ""
    counts: DbCounts = DbCounts()
    queries: List[DbSession] = []
    locks: List[DbLock] = []


class IngestPayload(BaseModel):
    device_id: str
    device_name: str = ""
    agent_version: str = ""
    os: OSMetrics
    partitions: List[Partition] = []
    processes: List[ProcessInfo] = []
    database: DbInfo = DbInfo()


class NoteIn(BaseModel):
    note: str
    author: str = "Local operator"
    event_id: Optional[int] = None
    device_id: Optional[str] = None


def to_dict(model):
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def clip(value, size):
    return value[:size] if isinstance(value, str) else value


def check_device_id(value):
    if not value or not DEVICE_ID_RE.match(value):
        raise HTTPException(status_code=400, detail="device_id must be 8-64 characters: letters, digits, '_' or '-'.")
    return value


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# --- evidence recording and detection rules --------------------------------------------
def record_event(cur, device_id, source, severity, typ, message, evidence, details=None, key=""):
    """Record one observation of a condition and return the id of its open incident.

    The first observation opens an incident; later observations update it (last_seen, one more snapshot)
    until the condition has been absent for EVENT_GRACE_SECONDS."""
    stamp = utcnow()
    fingerprint = f"{device_id}|{source}|{typ}|{message}" + (f"|{key}" if key else "")
    cur.execute("""INSERT INTO dbs_events(device_id,fingerprint,timestamp,source,severity,type,message,evidence,details,last_seen)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (fingerprint) WHERE resolved_at IS NULL DO UPDATE SET last_seen=EXCLUDED.last_seen,
        severity=EXCLUDED.severity, evidence=EXCLUDED.evidence, details=EXCLUDED.details,
        occurrences=dbs_events.occurrences+1
        RETURNING id""",
                (device_id, fingerprint, stamp, source, severity, typ, message, evidence, Json(details or {}), stamp))
    return cur.fetchone()["id"]


def close_ended_events(cur, device_id, now):
    """Close incidents of this device that were not seen for EVENT_GRACE_SECONDS (end time = last sighting)."""
    cur.execute("UPDATE dbs_events SET resolved_at = last_seen WHERE device_id=%s AND resolved_at IS NULL AND last_seen < %s",
                (device_id, now - datetime.timedelta(seconds=EVENT_GRACE_SECONDS)))


def disk_rates(prev_row, osd, received):
    """Disk read/write bytes per second since the previous snapshot, or (None, None) when unknown."""
    try:
        prev = prev_row["last_payload"]["os"]
        dt = (received - prev_row["last_seen"]).total_seconds()
        if not 0 < dt <= 120:
            return None, None
        rates = []
        for key in ("disk_read_bytes", "disk_write_bytes"):
            before, after = prev.get(key), osd.get(key)
            if before is None or after is None or after < before:  # missing, or the counter restarted
                return None, None
            rates.append(round((after - before) / dt))
        return rates[0], rates[1]
    except (KeyError, TypeError):
        return None, None


def analyze(cur, device_id, osd, procs, db):
    """Apply the detection rules to one snapshot, persist the incidents and return the active alerts."""
    alerts = []
    ranked = sorted(procs, key=lambda p: p.get("cpu_percent", 0), reverse=True)
    cpu, mem, disk = osd["cpu_percent"], osd["memory_percent"], osd["disk_percent"]
    db_up = db["configured"] and db["connected"]
    active = db["counts"].get("active", 0)

    def raise_alert(alert, typ, key=""):
        alert["event_id"] = record_event(cur, device_id, alert["source"], alert["severity"], typ, alert["title"],
                                         alert["detail"], alert["evidence"], key)
        alerts.append(alert)

    def lead(alert_id, title, detail, evidence):
        raise_alert({"id": alert_id, "severity": "warning", "source": "Correlation", "title": title,
                     "detail": detail + " This is a correlation lead, not proof of causality.", "evidence": evidence},
                    "correlation_lead")

    # --- single-source rules
    if cpu >= CPU_THRESHOLD:
        top = ranked[0] if ranked else {}
        raise_alert({"id": "OS-CPU", "severity": "high", "source": "OS", "title": "High CPU utilization",
                     "detail": f"CPU reached {cpu}%. Top sampled process: {top.get('name', 'unknown')} (PID {top.get('pid', '—')}, {top.get('cpu_percent', 0)}% CPU).",
                     "evidence": {"cpu_percent": cpu, "top_process": top}}, "resource_pressure")
    if mem >= MEM_THRESHOLD:
        top5 = sorted(procs, key=lambda p: p.get("memory_percent", 0), reverse=True)[:5]
        raise_alert({"id": "OS-MEM", "severity": "critical", "source": "OS", "title": "Memory pressure detected",
                     "detail": f"Memory usage is {mem}%; available {round(osd['memory_available'] / 1024 ** 3, 2)} GiB.",
                     "evidence": {"memory_percent": mem, "top_memory_processes": top5}}, "resource_pressure")
    if disk >= DISK_THRESHOLD:
        raise_alert({"id": "OS-DISK", "severity": "high", "source": "OS", "title": "Disk space running low",
                     "detail": f"System volume is {disk}% used with {round(osd['disk_free'] / 1024 ** 3, 2)} GiB free.",
                     "evidence": {"disk_percent": disk, "disk_free": osd["disk_free"], "partitions": osd.get("partitions", [])}},
                    "disk_pressure")
    if db["configured"] and not db["connected"]:
        raise_alert({"id": "DB-CONN", "severity": "warning", "source": "Database", "title": "PostgreSQL not connected",
                     "detail": db["message"] or "The agent could not connect to PostgreSQL.",
                     "evidence": {"database_connected": False, "message": db["message"]}}, "connection")
    for lock in db["locks"]:
        raise_alert({"id": f"LOCK-{lock['blocked_pid']}", "severity": "warning", "source": "Database",
                     "title": "Blocked database session",
                     "detail": f"PID {lock['blocked_pid']} is blocked by PID {lock['blocking_pid']}. A lock wait is not necessarily a deadlock.",
                     "evidence": lock}, "lock_wait", f"{lock['blocked_pid']}>{lock['blocking_pid']}")

    # --- correlation rules: an OS condition and database activity at the same time
    sessions = [q for q in db["queries"] if q.get("state") == "active" and q.get("duration_seconds") is not None]
    longest = max(sessions, key=lambda q: q["duration_seconds"]) if sessions else None
    pg_procs = [p for p in procs if re.match(r"^postgres(\.exe)?$", p.get("name", ""), re.I)]
    pg_mem = round(sum(p.get("memory_percent", 0) for p in pg_procs), 1)

    if cpu >= CPU_THRESHOLD and db_up and active > 0:
        detail = f"CPU {cpu}% while PostgreSQL has {active} active session(s)."
        evidence = {"cpu_percent": cpu, "active_sessions": active}
        if longest:
            detail += f" Longest active query: PID {longest['pid']}, running {longest['duration_seconds']}s ({clip(longest.get('query') or '', 80)})."
            evidence["longest_active_query"] = longest
        if ranked and pg_procs and ranked[0].get("pid") in {p["pid"] for p in pg_procs}:
            detail += " The busiest process is PostgreSQL itself."
        lead("CORR-CPU-DB", "CPU pressure coincides with active PostgreSQL sessions", detail, evidence)
    if mem >= MEM_THRESHOLD and pg_mem >= PG_MEMORY_SHARE:
        lead("CORR-MEM-DB", "Memory pressure while PostgreSQL holds a large share of RAM",
             f"Memory is {mem}% and the PostgreSQL processes seen use {pg_mem}% of RAM ({len(pg_procs)} process(es)).",
             {"memory_percent": mem, "postgres_memory_percent": pg_mem, "postgres_processes": pg_procs[:5]})
    if disk >= DISK_THRESHOLD and db_up and active > 0:
        lead("CORR-DISK-DB", "Low disk space while PostgreSQL has active sessions",
             f"System volume is {disk}% used while PostgreSQL has {active} active session(s); writes, WAL or logs may be at risk.",
             {"disk_percent": disk, "disk_free": osd["disk_free"], "active_sessions": active})
    rate = (osd.get("disk_read_bps") or 0) + (osd.get("disk_write_bps") or 0)
    if rate >= IO_BPS_THRESHOLD and db_up and active > 0:
        lead("CORR-IO-DB", "High disk throughput coincides with active PostgreSQL sessions",
             f"Disk throughput is {human_bytes(rate)}/s (read {human_bytes(osd.get('disk_read_bps') or 0)}/s, write {human_bytes(osd.get('disk_write_bps') or 0)}/s) "
             f"while PostgreSQL has {active} active session(s).",
             {"disk_read_bps": osd.get("disk_read_bps"), "disk_write_bps": osd.get("disk_write_bps"), "active_sessions": active})
    if db_up and db["locks"] and (cpu >= HOST_LOAD_CPU or mem >= HOST_LOAD_MEM):
        lead("CORR-LOCK-HOST", "Database lock waits while the host is under load",
             f"{len(db['locks'])} blocking relationship(s) observed with CPU {cpu}% and memory {mem}%; waiting sessions may be amplifying the load.",
             {"locks": db["locks"][:10], "cpu_percent": cpu, "memory_percent": mem})
    return alerts


# --- device helpers --------------------------------------------------------------------
def device_view(row):
    online = (utcnow() - row["last_seen"]).total_seconds() <= OFFLINE_AFTER_SECONDS
    return {"device_id": row["device_id"], "name": row["name"], "host": row["host"], "platform": row["platform"],
            "agent_version": row["agent_version"], "first_seen": row["first_seen"], "last_seen": row["last_seen"],
            "online": online}


def require_device(cur, device_id):
    """The requested device, or the most recently seen one when none is given."""
    if device_id:
        check_device_id(device_id)
        cur.execute("SELECT * FROM dbs_devices WHERE device_id=%s", (device_id,))
    else:
        cur.execute("SELECT * FROM dbs_devices ORDER BY last_seen DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Unknown device_id." if device_id else "No device has reported yet. Start the DBSentinel agent.")
    return row


def scope_device_id(cur, device_id):
    """device_id for history-style queries: as given, else the most recently seen device (or None)."""
    if device_id:
        return check_device_id(device_id)
    cur.execute("SELECT device_id FROM dbs_devices ORDER BY last_seen DESC LIMIT 1")
    row = cur.fetchone()
    return row["device_id"] if row else None


def fetch_all(cur, sql, params=()):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


EVENT_FIELDS = "id,device_id,timestamp,source,severity,type,message,evidence,details,last_seen,occurrences,resolved_at"


def event_rows(cur, scope, limit):
    """Incidents of a device, newest first. `active` = still being observed; `duration_seconds` = first to last sighting."""
    cutoff = utcnow() - datetime.timedelta(seconds=EVENT_GRACE_SECONDS)
    return fetch_all(cur, f"SELECT {EVENT_FIELDS}, (resolved_at IS NULL AND last_seen >= %s) AS active, "
                          "EXTRACT(EPOCH FROM (last_seen - timestamp))::int AS duration_seconds "
                          "FROM dbs_events WHERE device_id=%s ORDER BY id DESC LIMIT %s", (cutoff, scope, limit))


def note_rows(cur, scope, limit):
    return fetch_all(cur, "SELECT id,event_id,device_id,created_at,author,note FROM dbs_investigation_notes "
                          "WHERE device_id=%s ORDER BY id DESC LIMIT %s", (scope, limit))


def history_rows(cur, scope, limit):
    rows = fetch_all(cur, "SELECT timestamp,cpu,memory,disk,db_total AS connections,db_active AS active "
                          "FROM dbs_snapshots WHERE device_id=%s ORDER BY id DESC LIMIT %s", (scope, limit))
    rows.reverse()
    return rows


# --- ingest ----------------------------------------------------------------------------
@app.post("/api/ingest", dependencies=[Depends(require_ingest_key)])
def ingest(payload: IngestPayload):
    device_id = check_device_id(payload.device_id)
    if len(payload.processes) > 300 or len(payload.database.queries) > 100 or len(payload.database.locks) > 100 or len(payload.partitions) > 64:
        raise HTTPException(status_code=422, detail="Payload exceeds list size limits.")

    received = utcnow()
    osd = to_dict(payload.os)
    osd["host"] = clip(osd["host"], 200)
    osd["platform"] = clip(osd["platform"], 200)
    osd["partitions"] = [to_dict(p) for p in payload.partitions]
    osd["process_count"] = osd["process_count"] or len(payload.processes)
    osd["timestamp"] = received.isoformat()
    procs = []
    for p in payload.processes:
        d = to_dict(p)
        d["name"], d["username"] = clip(d["name"], 200), clip(d["username"], 100)
        procs.append(d)
    db = to_dict(payload.database)
    db["message"] = clip(db["message"], 500)
    for q in db["queries"]:
        q["query"] = clip(q["query"], 500)
    for lock in db["locks"]:
        lock["blocked_query"], lock["blocking_query"] = clip(lock["blocked_query"], 180), clip(lock["blocking_query"], 180)

    with db_cursor() as cur:
        cur.execute("SELECT last_payload, last_seen FROM dbs_devices WHERE device_id=%s", (device_id,))
        previous = cur.fetchone()
        osd["disk_read_bps"], osd["disk_write_bps"] = disk_rates(previous, osd, received) if previous else (None, None)
        alerts = analyze(cur, device_id, osd, procs, db)
        close_ended_events(cur, device_id, received)
        live = {"os": osd, "processes": sorted(procs, key=lambda p: p["cpu_percent"], reverse=True),
                "database": db, "alerts": alerts}
        cur.execute("""INSERT INTO dbs_devices(device_id,name,host,platform,agent_version,first_seen,last_seen,last_payload)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(device_id) DO UPDATE SET name=EXCLUDED.name, host=EXCLUDED.host, platform=EXCLUDED.platform,
            agent_version=EXCLUDED.agent_version, last_seen=EXCLUDED.last_seen, last_payload=EXCLUDED.last_payload""",
                    (device_id, clip(payload.device_name, 80) or osd["host"], osd["host"], osd["platform"],
                     clip(payload.agent_version, 32), received, received, Json(live)))
        compact = {"os": {k: v for k, v in osd.items() if k != "partitions"},
                   "database": {"connected": db["connected"], "counts": db["counts"], "locks": db["locks"]}}
        cur.execute("""INSERT INTO dbs_snapshots(device_id,timestamp,cpu,memory,disk,db_total,db_active,payload)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (device_id, received, osd["cpu_percent"], osd["memory_percent"], osd["disk_percent"],
                     db["counts"]["total"], db["counts"]["active"], Json(compact)))
        cur.execute("""DELETE FROM dbs_snapshots WHERE device_id=%s AND id <
            (SELECT id FROM dbs_snapshots WHERE device_id=%s ORDER BY id DESC OFFSET %s LIMIT 1)""",
                    (device_id, device_id, SNAPSHOT_RETENTION - 1))
    return {"ok": True, "device_id": device_id, "alerts": len(alerts), "server_time": received.isoformat()}


# --- read API for the dashboard --------------------------------------------------------
@app.get("/api/health")
def health():
    try:
        with db_cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM dbs_devices")
            devices = cur.fetchone()["n"]
        connected, message = True, "DBSentinel evidence tables ready in PostgreSQL"
    except Exception as exc:
        devices, connected = 0, False
        message = str(exc) if isinstance(exc, StoreUnavailable) else "PostgreSQL evidence store unavailable; check DATABASE_URL, the PostgreSQL service and permissions."
    return {"status": "ok", "service": "DBSentinel", "version": API_VERSION, "timestamp": utcnow().isoformat(),
            "evidence_store_connected": connected, "evidence_store_message": message, "devices": devices,
            "dashboard_key_required": bool(os.getenv("DASHBOARD_KEY"))}


@app.get("/api/devices", dependencies=PROTECTED)
def devices():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM dbs_devices ORDER BY last_seen DESC")
        return {"devices": [device_view(r) for r in cur.fetchall()], "offline_after_seconds": OFFLINE_AFTER_SECONDS}


@app.get("/api/dashboard", dependencies=PROTECTED)
def dashboard(device_id: Optional[str] = None):
    """Everything the dashboard shows for one device, in a single request and a single transaction."""
    with db_cursor() as cur:
        dev = require_device(cur, device_id)
        view, live = device_view(dev), dev["last_payload"]
        hist = history_rows(cur, dev["device_id"], 120)
        evs = event_rows(cur, dev["device_id"], 100)
        notes = note_rows(cur, dev["device_id"], 100)
    return {"device": view, "os": live["os"], "processes": live["processes"], "database": live["database"],
            "alerts": live["alerts"] if view["online"] else [], "history": hist, "events": evs, "notes": notes}


@app.get("/api/overview", dependencies=PROTECTED)
def overview(device_id: Optional[str] = None):
    with db_cursor() as cur:
        dev = require_device(cur, device_id)
        view, live = device_view(dev), dev["last_payload"]
        hist = history_rows(cur, dev["device_id"], 120)
    return {"device": view, "os": live["os"], "database": live["database"], "history": hist,
            "alerts": live["alerts"] if view["online"] else []}


@app.get("/api/os", dependencies=PROTECTED)
def os_monitor(device_id: Optional[str] = None):
    with db_cursor() as cur:
        dev = require_device(cur, device_id)
    live = dev["last_payload"]
    return {**live["os"], "processes": live["processes"], "device": device_view(dev)}


@app.get("/api/database", dependencies=PROTECTED)
def database_monitor(device_id: Optional[str] = None):
    with db_cursor() as cur:
        dev = require_device(cur, device_id)
    return {**dev["last_payload"]["database"], "device": device_view(dev)}


@app.get("/api/history", dependencies=PROTECTED)
def get_history(device_id: Optional[str] = None):
    with db_cursor() as cur:
        scope = scope_device_id(cur, device_id)
        points = history_rows(cur, scope, SNAPSHOT_RETENTION) if scope else []
    return {"points": points, "device_id": scope, "retention": f"Last {SNAPSHOT_RETENTION} snapshots per device in PostgreSQL"}


@app.get("/api/events", dependencies=PROTECTED)
def events(limit: int = 100, device_id: Optional[str] = None):
    with db_cursor() as cur:
        scope = scope_device_id(cur, device_id)
        rows = event_rows(cur, scope, max(1, min(limit, 500))) if scope else []
    return {"events": rows, "device_id": scope}


@app.get("/api/forensics", dependencies=PROTECTED)
def forensics(device_id: Optional[str] = None):
    with db_cursor() as cur:
        scope = scope_device_id(cur, device_id)
        if scope:
            snaps = fetch_all(cur, "SELECT id,timestamp,cpu,memory,disk,db_total,db_active,payload FROM dbs_snapshots "
                                   "WHERE device_id=%s ORDER BY id DESC LIMIT 100", (scope,))
            evs, notes = event_rows(cur, scope, 100), note_rows(cur, scope, 100)
        else:
            snaps, evs, notes = [], [], []
    return {"snapshots": snaps, "events": evs, "notes": notes, "device_id": scope,
            "retention": f"Last {SNAPSHOT_RETENTION} snapshots per device; events and notes stored in PostgreSQL"}


@app.post("/api/investigation-notes", dependencies=PROTECTED)
def add_note(body: NoteIn, request: Request):
    if not limiter.allow(("note", client_ip(request)), NOTE_LIMIT, NOTE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many notes; wait a minute and try again.")
    note, author = body.note.strip(), (body.author.strip() or "Local operator")[:80]
    if not note:
        raise HTTPException(status_code=400, detail="Note cannot be empty")
    if len(note) > 4000:
        raise HTTPException(status_code=400, detail="Note must be 4000 characters or less")
    with db_cursor() as cur:
        scope = scope_device_id(cur, body.device_id)
        if body.event_id is not None:
            cur.execute("SELECT device_id FROM dbs_events WHERE id=%s", (body.event_id,))
            event = cur.fetchone()
            if not event:
                raise HTTPException(status_code=400, detail="Unknown event_id")
            scope = event["device_id"]
        cur.execute("INSERT INTO dbs_investigation_notes(device_id,event_id,created_at,author,note) "
                    "VALUES(%s,%s,%s,%s,%s) RETURNING id,created_at", (scope, body.event_id, utcnow(), author, note))
        saved = cur.fetchone()
    return {"ok": True, "id": saved["id"], "created_at": saved["created_at"].isoformat()}


@app.get("/api/settings", dependencies=PROTECTED)
def settings():
    counts = {"snapshots": 0, "events": 0, "notes": 0, "devices": 0}
    connected, message = True, "DBSentinel evidence tables ready in PostgreSQL"
    try:
        with db_cursor() as cur:
            cur.execute("SELECT (SELECT COUNT(*) FROM dbs_snapshots) AS snapshots, (SELECT COUNT(*) FROM dbs_events) AS events, "
                        "(SELECT COUNT(*) FROM dbs_investigation_notes) AS notes, (SELECT COUNT(*) FROM dbs_devices) AS devices")
            counts = dict(cur.fetchone())
    except Exception as exc:
        connected = False
        message = str(exc) if isinstance(exc, StoreUnavailable) else "PostgreSQL evidence store unavailable; check DATABASE_URL, the PostgreSQL service and permissions."
    return {"backend": "online", "version": API_VERSION, "database_configured": bool(os.getenv("DATABASE_URL")),
            "ingest_key_configured": bool(os.getenv("INGEST_KEY")), "dashboard_key_required": bool(os.getenv("DASHBOARD_KEY")),
            "psycopg2_available": bool(psycopg2),
            "postgres_log_integration": "Native log ingestion not configured",
            "history_retention": f"PostgreSQL (last {SNAPSHOT_RETENTION} snapshots per device)",
            "evidence_store_connected": connected, "evidence_store_message": message,
            "snapshots_stored": counts["snapshots"], "events_stored": counts["events"],
            "investigation_notes_stored": counts["notes"], "devices_registered": counts["devices"],
            "allowed_origins": ALLOWED_ORIGINS, "offline_after_seconds": OFFLINE_AFTER_SECONDS, "refresh_seconds": 5,
            "incident_grace_seconds": EVENT_GRACE_SECONDS,
            "safe_actions": "Recommendation-only; no destructive action endpoints enabled"}


def mount_frontend(application, dist_dir):
    """Serve a built dashboard (frontend/dist) from the API process."""
    dist_dir = Path(dist_dir)
    dist_root = dist_dir.resolve()
    if (dist_dir / "assets").is_dir():
        application.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")

    @application.get("/{path:path}")
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (dist_dir / path).resolve()
        # never serve anything outside the build folder (blocks ../ and ..\ traversal)
        if candidate.is_file() and dist_root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(dist_dir / "index.html")


# Serve a production build if one has been created.
if (BASE / "frontend" / "dist").exists():
    mount_frontend(app, BASE / "frontend" / "dist")
