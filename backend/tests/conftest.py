"""Test fixtures: a real PostgreSQL server, a fresh database per test, and a FastAPI test client.

Which PostgreSQL is used:
  * TEST_DATABASE_URL (e.g. postgresql://postgres:pw@localhost:5432/postgres): needs CREATE DATABASE rights.
    This is what CI uses.
  * otherwise, if the optional `pixeltable-pgserver` package is installed, a private embedded server is started.
  * otherwise the database tests are skipped.
"""
import datetime
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("INGEST_KEY", "test-ingest-key")
os.environ.pop("DASHBOARD_KEY", None)

import psycopg2  # noqa: E402

import main  # noqa: E402

INGEST_KEY = "test-ingest-key"
KEY_HEADER = {"X-API-Key": INGEST_KEY}
ADMIN_KEY = "test-admin-key"
ADMIN_HEADER = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    """Base URI (pointing at a maintenance database) of a PostgreSQL server we may create databases on."""
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        import pixeltable_pgserver as pgserver
    except ImportError:
        pytest.skip("set TEST_DATABASE_URL or install pixeltable-pgserver to run the database tests")
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
def make_db(pg_server):
    """Factory: creates an empty database and returns its URL. All created databases are dropped afterwards."""
    created = []
    admin = psycopg2.connect(pg_server)
    admin.autocommit = True

    def factory():
        name = "dbs_test_" + uuid.uuid4().hex[:10]
        admin.cursor().execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        return pg_server.rsplit("/", 1)[0] + "/" + name

    yield factory
    for name in created:
        try:
            cur = admin.cursor()
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid <> pg_backend_pid()", (name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        except Exception:
            pass
    admin.close()


def reset_app(database_url):
    """Point the app at a database and forget all connection / schema state."""
    if main._pool is not None:
        try:
            main._pool.closeall()
        except Exception:
            pass
    main._pool = None
    main._schema_ready = False
    main.limiter = main.RateLimiter()
    os.environ["DATABASE_URL"] = database_url
    os.environ["INGEST_KEY"] = INGEST_KEY
    os.environ["ADMIN_KEY"] = ADMIN_KEY
    os.environ.pop("DASHBOARD_KEY", None)
    os.environ.pop("SHARED_INGEST_KEY_ENABLED", None)


class Clock:
    """Controllable replacement for main.utcnow so tests can move time forward."""

    def __init__(self):
        self.now_value = datetime.datetime.now(datetime.timezone.utc)

    def __call__(self):
        return self.now_value

    def advance(self, seconds):
        self.now_value += datetime.timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(main, "utcnow", c)
    return c


@pytest.fixture
def api(make_db):
    from fastapi.testclient import TestClient
    reset_app(make_db())
    yield TestClient(main.app)
    reset_app("")


def make_payload(cpu=10.0, mem=40.0, disk=50.0, active=0, locks=None, connected=True, configured=True,
                 device="laptop-aaaa-1111", processes=None, queries=None, read_bytes=1000, write_bytes=2000):
    """A valid /api/ingest body with sensible defaults."""
    return {
        "device_id": device, "device_name": "Test Laptop", "agent_version": "0.2.0",
        "os": {"host": "LAPTOP-TEST", "platform": "Windows-11", "cpu_percent": cpu, "cpu_count": 8,
               "memory_percent": mem, "memory_used": 8 * 1024 ** 3, "memory_total": 16 * 1024 ** 3,
               "memory_available": 8 * 1024 ** 3, "disk_percent": disk, "disk_used": 100, "disk_total": 200,
               "disk_free": 100, "disk_read_bytes": read_bytes, "disk_write_bytes": write_bytes, "process_count": 120},
        "partitions": [{"device": "C:\\", "mountpoint": "C:\\", "filesystem": "NTFS", "percent": disk,
                        "used": 100, "total": 200, "free": 100}],
        "processes": processes if processes is not None else [
            {"pid": 4, "name": "System", "username": "SYSTEM", "cpu_percent": 1.0, "memory_percent": 0.1},
            {"pid": 999, "name": "chrome.exe", "username": "me", "cpu_percent": 40.0, "memory_percent": 9.0,
             "disk_read_bytes": 5, "disk_write_bytes": 6}],
        "database": {"configured": configured, "connected": connected,
                     "message": "Connected" if connected else "connection refused",
                     "counts": {"active": active, "idle": 2, "total": active + 2},
                     "queries": queries if queries is not None else
                     [{"pid": 77, "username": "u", "database": "d", "state": "active", "duration_seconds": 3,
                       "query": "SELECT 1 " + "x" * 800}],
                     "locks": locks or []},
    }


@pytest.fixture
def push(api):
    """push(**payload_args) -> response of POST /api/ingest with the right key."""
    def _push(**kwargs):
        return api.post("/api/ingest", json=make_payload(**kwargs), headers=KEY_HEADER)
    return _push
