"""Failure handling: dropped connections, outages, load, schema upgrades, static-file safety."""
import concurrent.futures as cf
import os
import time

import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from conftest import KEY_HEADER, make_payload, reset_app

DEV = "laptop-aaaa-1111"


def test_connections_killed_by_the_server_are_replaced(api, push, make_db):
    push()
    admin = psycopg2.connect(os.environ["DATABASE_URL"])
    admin.autocommit = True
    admin.cursor().execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() AND pid <> pg_backend_pid()")
    assert push().status_code == 200
    assert api.get("/api/overview").status_code == 200


def test_database_outage_gives_503_not_500_and_recovers(api, push):
    good = os.environ["DATABASE_URL"]
    push()
    reset_app("postgresql://postgres:pw@127.0.0.1:1/nothing")          # nothing listens on port 1
    assert push().status_code == 503
    assert api.get("/api/overview").status_code == 503
    health = api.get("/api/health")
    assert health.status_code == 200 and health.json()["evidence_store_connected"] is False
    assert "127.0.0.1" not in health.text and "nothing" not in health.text        # connection details never leak
    reset_app(good)
    assert push().status_code == 200


def test_missing_database_url_explains_itself(api):
    reset_app("")
    os.environ.pop("DATABASE_URL")
    r = api.get("/api/overview")
    assert r.status_code == 503 and "DATABASE_URL" in r.text


def test_many_parallel_requests_queue_instead_of_failing(api, push):
    push()

    def hit(i):
        if i % 3 == 0:
            return api.post("/api/ingest", json=make_payload(device=f"load-test-{i % 4:04d}-xx"), headers=KEY_HEADER).status_code
        return api.get(["/api/dashboard", "/api/events", "/api/forensics", "/api/devices", "/api/history"][i % 5]).status_code

    with cf.ThreadPoolExecutor(max_workers=30) as pool:
        codes = list(pool.map(hit, range(150)))
    assert set(codes) == {200}, {c: codes.count(c) for c in set(codes)}
    assert main._slots._value == main.POOL_MAX                          # no connection slot leaked


def test_exhausted_pool_answers_503_after_a_short_wait(api, push, monkeypatch):
    push()
    held = [main._slots.acquire() for _ in range(main.POOL_MAX)]
    monkeypatch.setattr(main, "POOL_WAIT_SECONDS", 1)
    try:
        t0 = time.time()
        r = api.get("/api/devices")
        assert r.status_code == 503 and "busy" in r.text and 0.9 < time.time() - t0 < 5
    finally:
        for _ in held:
            main._slots.release()
    assert api.get("/api/devices").status_code == 200


# ---------------------------------------------------------------- upgrades from older versions
V2_SCHEMA = [
    """CREATE TABLE dbs_snapshots (id BIGSERIAL PRIMARY KEY, timestamp TIMESTAMPTZ NOT NULL, cpu DOUBLE PRECISION,
       memory DOUBLE PRECISION, disk DOUBLE PRECISION, db_total INTEGER, db_active INTEGER, payload JSONB NOT NULL)""",
    """CREATE TABLE dbs_events (id BIGSERIAL PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, timestamp TIMESTAMPTZ NOT NULL,
       source TEXT NOT NULL, severity TEXT NOT NULL, type TEXT NOT NULL, message TEXT NOT NULL, evidence TEXT NOT NULL,
       details JSONB NOT NULL DEFAULT '{}'::jsonb, last_seen TIMESTAMPTZ NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE dbs_investigation_notes (id BIGSERIAL PRIMARY KEY, event_id BIGINT REFERENCES dbs_events(id) ON DELETE SET NULL,
       created_at TIMESTAMPTZ NOT NULL, author VARCHAR(80) NOT NULL, note TEXT NOT NULL)""",
]
V3_EXTRA = [  # what version 3.0.0 added on top of v2
    """CREATE TABLE dbs_devices (device_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '',
       platform TEXT NOT NULL DEFAULT '', agent_version TEXT NOT NULL DEFAULT '', first_seen TIMESTAMPTZ NOT NULL,
       last_seen TIMESTAMPTZ NOT NULL, last_payload JSONB NOT NULL DEFAULT '{}'::jsonb)""",
    "ALTER TABLE dbs_snapshots ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-data'",
    "ALTER TABLE dbs_events ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-data'",
    "ALTER TABLE dbs_investigation_notes ADD COLUMN device_id TEXT",
]


def seeded_database(make_db, statements, event_fingerprint):
    url = make_db()
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    for stmt in statements:
        cur.execute(stmt)
    cur.execute("INSERT INTO dbs_snapshots(timestamp,cpu,memory,disk,db_total,db_active,payload) VALUES(now(),1,2,3,4,5,'{}')")
    cur.execute("INSERT INTO dbs_events(fingerprint,timestamp,source,severity,type,message,evidence,last_seen,occurrences) "
                "VALUES(%s,now(),'OS','high','resource_pressure','High CPU utilization','old evidence',now(),7)", (event_fingerprint,))
    cur.execute("INSERT INTO dbs_investigation_notes(event_id,created_at,author,note) "
                "VALUES((SELECT id FROM dbs_events LIMIT 1),now(),'me','old note')")
    conn.close()
    return url


@pytest.mark.parametrize("statements,name", [(V2_SCHEMA, "v2"), (V2_SCHEMA + V3_EXTRA, "v3")])
def test_old_databases_are_upgraded_in_place(make_db, statements, name):
    url = seeded_database(make_db, statements, "OS|resource_pressure|High CPU utilization")
    reset_app(url)
    client = TestClient(main.app)
    assert client.post("/api/ingest", json=make_payload(cpu=95), headers=KEY_HEADER).status_code == 200
    legacy = client.get("/api/events?device_id=legacy-data").json()["events"]
    assert legacy and legacy[0]["occurrences"] == 7 and legacy[0]["message"] == "High CPU utilization"
    assert len(client.get("/api/history?device_id=legacy-data").json()["points"]) == 1
    assert client.get("/api/settings").json()["investigation_notes_stored"] == 1
    # a new incident for the same condition does not collide with the old row
    assert len(client.get(f"/api/events?device_id={DEV}").json()["events"]) >= 1
    assert client.post("/api/ingest", json=make_payload(cpu=95), headers=KEY_HEADER).status_code == 200
    assert client.get("/api/events?device_id=legacy-data").json()["events"][0]["occurrences"] == 7
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM pg_constraint WHERE conname='dbs_events_fingerprint_key'")
    assert cur.fetchone()[0] == 0                                        # the table-wide UNIQUE is gone
    conn.close()
    reset_app("")


def test_schema_setup_can_run_twice(api, push):
    push()
    main._schema_ready = False       # simulates a second process / a restart: every statement must be idempotent
    assert api.get("/api/devices").status_code == 200


# ---------------------------------------------------------------- built dashboard served by the API
def test_static_files_cannot_escape_the_build_folder(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "a.js").write_text("console.log(1)")
    (tmp_path / "secret.env").write_text("DATABASE_URL=postgresql://user:hunter2@host/db")
    application = FastAPI()
    main.mount_frontend(application, dist)
    client = TestClient(application)
    assert "app" in client.get("/").text
    assert "app" in client.get("/some/client/route").text               # SPA fallback
    assert client.get("/assets/a.js").status_code == 200
    for evil in ("../secret.env", "..%2Fsecret.env", "..\\secret.env", "%2e%2e/secret.env", "assets/../../secret.env",
                 "..%5Csecret.env"):
        response = client.get("/" + evil)
        assert "hunter2" not in response.text, evil
    assert client.get("/api/nothing-here").status_code == 404            # unknown API paths are not the HTML page
