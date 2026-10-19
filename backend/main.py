from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
import os, time, platform, socket, datetime, json
import psutil
_process_cpu = {}

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json
except Exception:
    psycopg2 = None
    RealDictCursor = None
    Json = None

BASE = Path(__file__).resolve().parent.parent
app = FastAPI(title="DBSentinel API", version="2.0.0", description="OS + PostgreSQL monitoring, forensic timeline, and safe investigation")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
started = time.time()
_schema_ready = False


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def pg_conn():
    if not psycopg2:
        raise RuntimeError("psycopg2-binary is not installed. Run pip install -r requirements.txt")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not configured. Set it in backend/.env to use PostgreSQL as the project database.")
    return psycopg2.connect(dsn, connect_timeout=3)


def init_store():
    """Create DBSentinel's own evidence tables inside the configured PostgreSQL database."""
    global _schema_ready
    if _schema_ready:
        return True
    try:
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS dbs_snapshots (
                        id BIGSERIAL PRIMARY KEY, timestamp TIMESTAMPTZ NOT NULL,
                        cpu DOUBLE PRECISION, memory DOUBLE PRECISION, disk DOUBLE PRECISION,
                        db_total INTEGER, db_active INTEGER, payload JSONB NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS dbs_events (
                        id BIGSERIAL PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                        timestamp TIMESTAMPTZ NOT NULL, source TEXT NOT NULL,
                        severity TEXT NOT NULL, type TEXT NOT NULL, message TEXT NOT NULL,
                        evidence TEXT NOT NULL, details JSONB NOT NULL DEFAULT '{}'::jsonb,
                        last_seen TIMESTAMPTZ NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS dbs_investigation_notes (
                        id BIGSERIAL PRIMARY KEY, event_id BIGINT REFERENCES dbs_events(id) ON DELETE SET NULL,
                        created_at TIMESTAMPTZ NOT NULL, author VARCHAR(80) NOT NULL, note TEXT NOT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_dbs_snapshots_timestamp ON dbs_snapshots(timestamp DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_dbs_events_timestamp ON dbs_events(timestamp DESC)")
        _schema_ready = True
        return True
    except Exception:
        return False


def store_status():
    ok = init_store()
    return {"connected": ok, "message": "DBSentinel evidence tables ready in PostgreSQL" if ok else "PostgreSQL evidence store unavailable; check DATABASE_URL, PostgreSQL service, database, and permissions."}


def database_snapshot():
    try:
        with pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT count(*) FILTER (WHERE state='active') AS active,
                    count(*) FILTER (WHERE state='idle') AS idle, count(*) AS total
                    FROM pg_stat_activity WHERE pid <> pg_backend_pid()""")
                counts = dict(cur.fetchone())
                cur.execute("""SELECT pid, usename AS username, datname AS database, state,
                    EXTRACT(EPOCH FROM (now()-query_start))::int AS duration_seconds,
                    LEFT(query, 500) AS query
                    FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND state IS NOT NULL
                    ORDER BY query_start NULLS LAST LIMIT 30""")
                queries = [dict(x) for x in cur.fetchall()]
                cur.execute("""SELECT blocked.pid AS blocked_pid, blocking.pid AS blocking_pid,
                    LEFT(blocked.query, 180) AS blocked_query, LEFT(blocking.query, 180) AS blocking_query
                    FROM pg_stat_activity blocked
                    JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
                    WHERE blocked.pid <> pg_backend_pid()""")
                locks = [dict(x) for x in cur.fetchall()]
        return {"connected": True, "message": "Connected to PostgreSQL", "counts": counts, "queries": queries, "locks": locks}
    except Exception as e:
        return {"connected": False, "message": str(e), "counts": {"active": 0, "idle": 0, "total": 0}, "queries": [], "locks": []}


import time
from threading import Lock


_os_cache = None
_os_cache_time = 0.0
_os_cache_lock = Lock()


def _collect_os_snapshot():
    vm = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=None)

    try:
        disk = psutil.disk_usage(os.path.abspath(os.sep))
    except (PermissionError, OSError):
        disk = psutil.disk_usage(str(Path.home().anchor or "/"))

    disk_io = psutil.disk_io_counters()

    partitions = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "filesystem": part.fstype,
                "percent": usage.percent,
                "used": usage.used,
                "total": usage.total,
                "free": usage.free,
            })
        except (PermissionError, OSError):
            continue

    # Collect process information and initialize CPU sampling
    procs = []
    process_objects = []

    for p in psutil.process_iter(
        ["pid", "name", "username", "memory_percent", "status"]
    ):
        try:
            p.cpu_percent(interval=None)
            process_objects.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Allow a short interval so CPU usage can be measured
    time.sleep(0.1)

    for p in process_objects:
        try:
            d = p.as_dict(attrs=[
                "pid", "name", "username", "memory_percent", "status"
            ])

            process_cpu = p.cpu_percent(interval=None)

            procs.append({
                "pid": d["pid"],
                "name": d["name"] or "unknown",
                "username": d["username"] or "—",
                "memory_percent": round(d["memory_percent"] or 0, 2),
                "cpu_percent": round(process_cpu, 1),
                "status": d["status"] or "unknown",
                "disk_read_bytes": None,
                "disk_write_bytes": None,
            })

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Sort by CPU usage so the busiest processes appear first
    procs.sort(key=lambda p: p["cpu_percent"], reverse=True)

    return {
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "cpu_percent": cpu,
        "cpu_count": psutil.cpu_count() or 1,
        "memory_percent": vm.percent,
        "memory_used": vm.used,
        "memory_total": vm.total,
        "memory_available": vm.available,
        "disk_percent": disk.percent,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "disk_free": disk.free,
        "disk_read_bytes": getattr(disk_io, "read_bytes", None),
        "disk_write_bytes": getattr(disk_io, "write_bytes", None),
        "partitions": partitions,
        "process_count": len(procs),
        "processes": procs[:100],
        "timestamp": now(),
    }

def os_snapshot():
    global _os_cache, _os_cache_time

    with _os_cache_lock:
        if (
            _os_cache is not None
            and time.monotonic() - _os_cache_time < 5
        ):
            return _os_cache

        data = _collect_os_snapshot()
        _os_cache = data
        _os_cache_time = time.monotonic()

        return data


def save_snapshot(osdata, db):
    if not init_store():
        return
    payload = {"os": {k: v for k, v in osdata.items() if k != "processes"},
               "database": {"connected": db["connected"], "counts": db["counts"], "locks": db["locks"]}}
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO dbs_snapshots(timestamp,cpu,memory,disk,db_total,db_active,payload)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (osdata["timestamp"], osdata["cpu_percent"], osdata["memory_percent"], osdata["disk_percent"],
                 db["counts"]["total"], db["counts"]["active"], Json(payload)))
            cur.execute("DELETE FROM dbs_snapshots WHERE id NOT IN (SELECT id FROM dbs_snapshots ORDER BY id DESC LIMIT 1000)")


def record_event(source, severity, typ, message, evidence, details=None):
    if not init_store():
        return
    stamp = now(); details = details or {}; fingerprint = f"{source}|{typ}|{message}"
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO dbs_events(fingerprint,timestamp,source,severity,type,message,evidence,details,last_seen)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(fingerprint) DO UPDATE SET last_seen=EXCLUDED.last_seen, severity=EXCLUDED.severity,
                evidence=EXCLUDED.evidence, details=EXCLUDED.details, occurrences=dbs_events.occurrences+1""",
                (fingerprint, stamp, source, severity, typ, message, evidence, Json(details), stamp))


def analyze(osdata, db):
    alerts = []
    if osdata["cpu_percent"] >= 85:
        top = (osdata["processes"] or [{}])[0]
        a = {"id": "OS-CPU", "severity": "high", "source": "OS", "title": "High CPU utilization", "detail": f"CPU reached {osdata['cpu_percent']}%. Top sampled process: {top.get('name','unknown')} (PID {top.get('pid','—')}, {top.get('cpu_percent',0)}% CPU).", "evidence": {"cpu_percent": osdata["cpu_percent"], "top_process": top}}
        alerts.append(a); record_event("OS", "high", "resource_pressure", a["title"], a["detail"], a["evidence"])
    if osdata["memory_percent"] >= 90:
        top = sorted(osdata["processes"], key=lambda p: p.get("memory_percent", 0), reverse=True)[:5]
        a = {"id": "OS-MEM", "severity": "critical", "source": "OS", "title": "Memory pressure detected", "detail": f"Memory usage is {osdata['memory_percent']}%; available {round(osdata['memory_available']/1024**3,2)} GiB.", "evidence": {"memory_percent": osdata["memory_percent"], "top_memory_processes": top}}
        alerts.append(a); record_event("OS", "critical", "resource_pressure", a["title"], a["detail"], a["evidence"])
    if osdata["disk_percent"] >= 90:
        a = {"id": "OS-DISK", "severity": "high", "source": "OS", "title": "Disk space running low", "detail": f"Root/system volume is {osdata['disk_percent']}% used with {round(osdata['disk_free']/1024**3,2)} GiB free.", "evidence": {"disk_percent": osdata["disk_percent"], "disk_free": osdata["disk_free"], "partitions": osdata["partitions"]}}
        alerts.append(a); record_event("OS", "high", "disk_pressure", a["title"], a["detail"], a["evidence"])
    if not db["connected"]:
        a = {"id": "DB-CONN", "severity": "warning", "source": "Database", "title": "PostgreSQL not connected", "detail": db["message"], "evidence": {"database_connected": False, "message": db["message"]}}
        alerts.append(a); record_event("Database", "warning", "connection", a["title"], a["detail"], a["evidence"])
    for l in db["locks"]:
        a = {"id": f"LOCK-{l['blocked_pid']}", "severity": "warning", "source": "Database", "title": "Blocked database session", "detail": f"PID {l['blocked_pid']} is blocked by PID {l['blocking_pid']}. A lock wait is not necessarily a deadlock.", "evidence": l}
        alerts.append(a); record_event("Database", "warning", "lock_wait", a["title"], a["detail"], a["evidence"])
    if osdata["cpu_percent"] >= 85 and db["connected"] and db["counts"].get("active", 0) > 0:
        msg = "CPU pressure coincides with active PostgreSQL sessions"; ev = f"CPU {osdata['cpu_percent']}%; active sessions {db['counts'].get('active',0)}. This is a correlation lead, not proof of causality."
        a = {"id": "CORR-CPU-DB", "severity": "warning", "source": "Correlation", "title": msg, "detail": ev, "evidence": {"cpu_percent": osdata["cpu_percent"], "active_sessions": db["counts"].get("active", 0)}}
        alerts.append(a); record_event("Correlation", "warning", "correlation_lead", msg, ev, a["evidence"])
    if db["connected"] and db["locks"]:
        msg = "Database lock waits observed; inspect affected sessions"; ev = f"Observed {len(db['locks'])} blocking relationship(s). Compare blocked and blocking query evidence with host telemetry."
        record_event("Correlation", "warning", "correlation_lead", msg, ev, {"locks": db["locks"], "cpu_percent": osdata["cpu_percent"], "memory_percent": osdata["memory_percent"]})
    return alerts


def fetch_rows(sql, params=()):
    if not init_store():
        return []
    with pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


@app.get("/api/health")
def health():
    db = database_snapshot(); store = store_status()
    return {"status": "ok", "service": "DBSentinel", "timestamp": now(), "database_connected": db["connected"], "database_message": db["message"], "evidence_store_connected": store["connected"], "evidence_store_message": store["message"]}

@app.get("/api/overview")
def overview():
    osdata = os_snapshot(); db = database_snapshot(); save_snapshot(osdata, db); alerts = analyze(osdata, db)
    hist = fetch_rows("SELECT timestamp,cpu,memory,disk,db_total AS connections,db_active AS active FROM dbs_snapshots ORDER BY id DESC LIMIT 120")
    hist.reverse()
    return {"os": {k: v for k, v in osdata.items() if k != "processes"}, "database": db, "history": hist, "alerts": alerts}

@app.get("/api/os")
def os_monitor(): return os_snapshot()

@app.get("/api/database")
def database_monitor(): return database_snapshot()

@app.get("/api/history")
def get_history():
    points = fetch_rows("SELECT timestamp,cpu,memory,disk,db_total AS connections,db_active AS active FROM dbs_snapshots ORDER BY id DESC LIMIT 1000")
    points.reverse(); return {"points": points, "retention": "Last 1000 snapshots in PostgreSQL"}

@app.get("/api/events")
def events(limit: int = 100):
    rows = fetch_rows("SELECT id,timestamp,source,severity,type,message,evidence,details,last_seen,occurrences FROM dbs_events ORDER BY id DESC LIMIT %s", (max(1, min(limit, 500)),))
    return {"events": rows}

@app.get("/api/forensics")
def forensics():
    snaps = fetch_rows("SELECT id,timestamp,cpu,memory,disk,db_total,db_active,payload FROM dbs_snapshots ORDER BY id DESC LIMIT 100")
    evs = fetch_rows("SELECT id,timestamp,source,severity,type,message,evidence,details,last_seen,occurrences FROM dbs_events ORDER BY id DESC LIMIT 100")
    notes = fetch_rows("SELECT id,event_id,created_at,author,note FROM dbs_investigation_notes ORDER BY id DESC LIMIT 100")
    return {"snapshots": snaps, "events": evs, "notes": notes, "retention": "Last 1000 snapshots; events and notes stored in PostgreSQL"}

@app.post("/api/investigation-notes")
def add_note(body: dict):
    note = str(body.get("note", "")).strip(); event_id = body.get("event_id"); author = str(body.get("author", "Local operator"))[:80]
    if not note: raise HTTPException(status_code=400, detail="Note cannot be empty")
    if len(note) > 4000: raise HTTPException(status_code=400, detail="Note must be 4000 characters or less")
    if not init_store(): raise HTTPException(status_code=503, detail="PostgreSQL evidence store unavailable. Check DATABASE_URL and PostgreSQL permissions.")
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO dbs_investigation_notes(event_id,created_at,author,note) VALUES(%s,%s,%s,%s) RETURNING id,created_at", (event_id, now(), author, note))
            nid, created = cur.fetchone()
    return {"ok": True, "id": nid, "created_at": created.isoformat() if hasattr(created, "isoformat") else created}

@app.get("/api/settings")
def settings():
    store = store_status()
    counts = {"snapshots": 0, "events": 0, "notes": 0}
    if store["connected"]:
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT (SELECT COUNT(*) FROM dbs_snapshots),(SELECT COUNT(*) FROM dbs_events),(SELECT COUNT(*) FROM dbs_investigation_notes)")
                counts["snapshots"], counts["events"], counts["notes"] = cur.fetchone()
    return {"backend": "online", "host": socket.gethostname(), "platform": platform.platform(), "database_configured": bool(os.getenv("DATABASE_URL")), "psycopg2_available": bool(psycopg2), "postgres_log_integration": "Native log ingestion not configured", "history_retention": "PostgreSQL (last 1000 snapshots)", "evidence_store_connected": store["connected"], "evidence_store_message": store["message"], "snapshots_stored": counts["snapshots"], "events_stored": counts["events"], "investigation_notes_stored": counts["notes"], "refresh_seconds": 5, "safe_actions": "Recommendation-only; no destructive action endpoints enabled"}

# Serve a production build if one has been created.
dist = BASE / "frontend" / "dist"
if dist.exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
    @app.get("/{path:path}")
    def frontend(path: str):
        candidate = dist / path
        return FileResponse(candidate if candidate.is_file() else dist / "index.html")
