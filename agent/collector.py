"""Telemetry collection for the DBSentinel agent: host (psutil) and local PostgreSQL."""
import os
import platform
import socket

import psutil

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # reported as a database message instead of crashing the agent
    psycopg2 = None
    RealDictCursor = None

TOP_BY_CPU = 60       # processes sent per snapshot: the busiest by CPU ...
TOP_BY_MEMORY = 40    # ... plus the biggest by memory (overlap is sent once)
MAX_SESSIONS = 30


def _safe(fn, default=None):
    """Call fn(); return default when the OS denies access. NoSuchProcess is left to the caller."""
    try:
        return fn()
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError, AttributeError, NotImplementedError):
        return default


class OSCollector:
    """Keeps psutil.Process objects between snapshots so per-process CPU is measured over the
    whole interval (no blocking sleep) and usernames are only looked up once per process."""

    def __init__(self):
        self.cpu_count = psutil.cpu_count() or 1
        self._procs = {}
        self._meta = {}
        self.prime()

    def prime(self):
        """The first CPU reading of any counter is meaningless (0.0); take it and discard it."""
        psutil.cpu_percent(interval=None)
        self.processes()

    def processes(self):
        """Return (selected processes, total process count)."""
        current = set(psutil.pids())
        for pid in set(self._procs) - current:
            self._procs.pop(pid, None)
            self._meta.pop(pid, None)
        rows = []
        for pid in current:
            if pid == 0:
                continue  # Windows "System Idle Process": its "CPU" is the idle time, not load
            try:
                proc = self._procs.get(pid)
                if proc is None or not proc.is_running():
                    proc = psutil.Process(pid)
                    self._procs[pid] = proc
                    self._meta.pop(pid, None)
                with proc.oneshot():
                    cpu = _safe(lambda: proc.cpu_percent(interval=None), 0.0) or 0.0
                    mem = _safe(proc.memory_percent, 0.0) or 0.0
                    io = _safe(proc.io_counters, None)
                    meta = self._meta.get(pid)
                    if meta is None:
                        meta = (_safe(proc.name, None) or "unknown", _safe(proc.username, None))
                        self._meta[pid] = meta
            except psutil.NoSuchProcess:
                self._procs.pop(pid, None)
                self._meta.pop(pid, None)
                continue
            rows.append({
                "pid": pid, "name": meta[0], "username": meta[1],
                # psutil reports percent of ONE core; divide so it is comparable with system CPU (0-100)
                "cpu_percent": round(min(100.0, cpu / self.cpu_count), 1),
                "memory_percent": round(mem, 2),
                # status is not collected: psutil.status() on Windows scans every process's threads
                # (~4 ms per process, ~1.7 s per snapshot) for a value that is almost always "running"
                "status": None,
                "disk_read_bytes": getattr(io, "read_bytes", None),
                "disk_write_bytes": getattr(io, "write_bytes", None),
            })
        chosen = {}
        for r in sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:TOP_BY_CPU]:
            chosen[r["pid"]] = r
        for r in sorted(rows, key=lambda r: r["memory_percent"], reverse=True)[:TOP_BY_MEMORY]:
            chosen.setdefault(r["pid"], r)
        return list(chosen.values()), len(rows)

    def partitions(self):
        out = []
        for part in psutil.disk_partitions(all=False):
            if not part.fstype or "cdrom" in part.opts:
                continue  # empty optical drives raise errors and are not interesting
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            out.append({"device": part.device, "mountpoint": part.mountpoint, "filesystem": part.fstype,
                        "percent": usage.percent, "used": usage.used, "total": usage.total, "free": usage.free})
        return out

    def system(self, process_count):
        vm = psutil.virtual_memory()
        root = (os.environ.get("SystemDrive", "C:") + os.sep) if os.name == "nt" else os.sep
        disk = psutil.disk_usage(root)
        io = _safe(psutil.disk_io_counters, None)
        return {
            "host": socket.gethostname(), "platform": platform.platform(),
            "cpu_percent": psutil.cpu_percent(interval=None), "cpu_count": self.cpu_count,
            "memory_percent": vm.percent, "memory_used": vm.used, "memory_total": vm.total, "memory_available": vm.available,
            "disk_percent": disk.percent, "disk_used": disk.used, "disk_total": disk.total, "disk_free": disk.free,
            "disk_read_bytes": getattr(io, "read_bytes", None), "disk_write_bytes": getattr(io, "write_bytes", None),
            "process_count": process_count,
        }

    def snapshot(self):
        procs, total = self.processes()
        return {"os": self.system(total), "partitions": self.partitions(), "processes": procs}


class DatabaseProbe:
    """Read-only view of a local PostgreSQL server. Keeps one connection and reconnects when it drops."""

    HIDDEN = "<hidden by agent config>"

    def __init__(self, enabled=False, dsn="", send_query_text=True):
        self.enabled = bool(enabled and dsn)
        self.dsn = dsn
        self.send_query_text = send_query_text
        self._conn = None
        self._banner = ""

    @staticmethod
    def _empty(configured, connected, message):
        return {"configured": configured, "connected": connected, "message": message,
                "counts": {"active": 0, "idle": 0, "total": 0}, "queries": [], "locks": []}

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def _connect(self):
        conn = psycopg2.connect(self.dsn, connect_timeout=3, application_name="dbsentinel-agent",
                                options="-c statement_timeout=3000 -c default_transaction_read_only=on")
        conn.autocommit = True
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SHOW server_version")
            version = cur.fetchone()["server_version"]
            cur.execute("SELECT rolsuper OR pg_has_role(current_user, 'pg_read_all_stats', 'member') AS full "
                        "FROM pg_roles WHERE rolname = current_user")
            full = bool(cur.fetchone()["full"])
        self._conn = conn
        self._banner = f"Connected to PostgreSQL {version}" + (
            "" if full else " (limited visibility: grant pg_monitor to this role to see other users' sessions and queries)")

    def _clip_query(self, text):
        if text is None:
            return None
        return text if self.send_query_text else self.HIDDEN

    def _read(self):
        with self._conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE state='active') AS active,
                count(*) FILTER (WHERE state='idle') AS idle, count(*) AS total
                FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND backend_type = 'client backend'""")
            counts = {k: int(v or 0) for k, v in cur.fetchone().items()}
            cur.execute("""SELECT pid, usename AS username, datname AS database, state,
                EXTRACT(EPOCH FROM (now() - query_start))::int AS duration_seconds, LEFT(query, 500) AS query
                FROM pg_stat_activity
                WHERE pid <> pg_backend_pid() AND backend_type = 'client backend' AND state IS NOT NULL
                ORDER BY (state = 'active') DESC, query_start NULLS LAST LIMIT %s""", (MAX_SESSIONS,))
            queries = [dict(r) for r in cur.fetchall()]
            cur.execute("""SELECT blocked.pid AS blocked_pid, blocking.pid AS blocking_pid,
                LEFT(blocked.query, 180) AS blocked_query, LEFT(blocking.query, 180) AS blocking_query
                FROM pg_stat_activity blocked
                JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
                WHERE blocked.pid <> pg_backend_pid() AND blocked.backend_type = 'client backend'""")
            locks = [dict(r) for r in cur.fetchall()]
        for q in queries:
            q["query"] = self._clip_query(q["query"])
        for lock in locks:
            lock["blocked_query"] = self._clip_query(lock["blocked_query"])
            lock["blocking_query"] = self._clip_query(lock["blocking_query"])
        return {"configured": True, "connected": True, "message": self._banner,
                "counts": counts, "queries": queries, "locks": locks}

    def snapshot(self):
        if not self.enabled:
            return self._empty(False, False, "PostgreSQL monitoring is not configured on this agent.")
        if psycopg2 is None:
            return self._empty(True, False, "psycopg2-binary is not installed on the agent.")
        last_error = None
        for _ in range(2):  # a dropped connection is replaced once within the same cycle
            try:
                if self._conn is None or self._conn.closed:
                    self._connect()
                return self._read()
            except psycopg2.Error as exc:
                last_error = exc
                self.close()
        text = (str(last_error).strip().splitlines() or ["connection failed"])[0]
        return self._empty(True, False, text[:300])
