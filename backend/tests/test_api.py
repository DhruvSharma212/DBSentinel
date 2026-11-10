"""HTTP API behaviour: auth, ingest, reads, validation, notes, access key, rate limits."""
import os

import pytest

import main
from conftest import KEY_HEADER, make_payload

DEV = "laptop-aaaa-1111"


# ---------------------------------------------------------------- empty state and auth
def test_health_and_empty_state(api):
    h = api.get("/api/health")
    assert h.status_code == 200
    body = h.json()
    assert body["evidence_store_connected"] is True and body["devices"] == 0
    assert body["version"] == main.API_VERSION and body["dashboard_key_required"] is False
    assert api.get("/api/overview").status_code == 404
    assert api.get("/api/dashboard").status_code == 404
    assert api.get("/api/history").json()["points"] == []
    assert api.get("/api/events").json()["events"] == []
    assert api.get("/api/devices").json()["devices"] == []


def test_ingest_requires_the_key(api, monkeypatch):
    body = make_payload()
    assert api.post("/api/ingest", json=body).status_code == 401
    assert api.post("/api/ingest", json=body, headers={"X-API-Key": "nope"}).status_code == 401
    monkeypatch.delenv("INGEST_KEY")
    assert api.post("/api/ingest", json=body, headers=KEY_HEADER).status_code == 503


def test_bad_ingest_key_attempts_lock_out_the_address(api):
    body = make_payload()
    for _ in range(main.BAD_KEY_LIMIT):
        assert api.post("/api/ingest", json=body, headers={"X-API-Key": "guess"}).status_code == 401
    assert api.post("/api/ingest", json=body, headers={"X-API-Key": "guess"}).status_code == 429
    assert api.post("/api/ingest", json=body, headers=KEY_HEADER).status_code == 429  # locked out even with the right key


# ---------------------------------------------------------------- ingest + reads
def test_ingest_then_read_back(api, push):
    assert push().json()["ok"] is True
    devices = api.get("/api/devices").json()["devices"]
    assert len(devices) == 1 and devices[0]["device_id"] == DEV and devices[0]["online"] and devices[0]["name"] == "Test Laptop"
    overview = api.get("/api/overview").json()
    assert overview["device"]["device_id"] == DEV and overview["os"]["cpu_percent"] == 10.0
    assert "processes" not in overview["os"] and len(overview["history"]) == 1 and overview["alerts"] == []
    os_view = api.get(f"/api/os?device_id={DEV}").json()
    assert os_view["processes"][0]["name"] == "chrome.exe"  # sorted by CPU, busiest first
    assert os_view["processes"][0]["disk_read_bytes"] == 5 and os_view["partitions"][0]["filesystem"] == "NTFS"
    database = api.get("/api/database").json()
    assert database["connected"] and len(database["queries"][0]["query"]) == 500  # clipped server-side


def test_dashboard_endpoint_returns_everything_in_one_request(api, push):
    push(cpu=95)
    d = api.get(f"/api/dashboard?device_id={DEV}").json()
    assert set(d) == {"device", "os", "processes", "database", "alerts", "history", "events", "notes", "version"}
    assert d["alerts"][0]["id"] == "OS-CPU" and d["events"][0]["message"] == "High CPU utilization"
    assert d["processes"][0]["name"] == "chrome.exe" and len(d["history"]) == 1


def test_devices_are_isolated(api, push):
    push(cpu=95)
    push(cpu=5, device="desktop-bbbb-2222")
    assert len(api.get("/api/devices").json()["devices"]) == 2
    assert api.get("/api/overview").json()["device"]["device_id"] == "desktop-bbbb-2222"  # most recent by default
    assert api.get("/api/events?device_id=desktop-bbbb-2222").json()["events"] == []
    assert len(api.get(f"/api/events?device_id={DEV}").json()["events"]) > 0


def test_snapshot_retention_is_per_device(api, push, monkeypatch):
    monkeypatch.setattr(main, "SNAPSHOT_RETENTION", 5)
    for _ in range(9):
        push(device="retain-cccc-3333")
    push()
    assert len(api.get("/api/history?device_id=retain-cccc-3333").json()["points"]) == 5
    assert len(api.get(f"/api/history?device_id={DEV}").json()["points"]) == 1


def test_offline_device_reports_no_live_alerts(api, push, clock):
    push(cpu=95)
    assert api.get("/api/dashboard").json()["alerts"]
    clock.advance(main.OFFLINE_AFTER_SECONDS + 5)
    d = api.get("/api/dashboard").json()
    assert d["device"]["online"] is False and d["alerts"] == []


def test_database_not_configured_is_not_an_alert(api, push):
    push(configured=False, connected=False)
    assert "DB-CONN" not in [a["id"] for a in api.get("/api/overview").json()["alerts"]]
    push(configured=True, connected=False)
    assert "DB-CONN" in [a["id"] for a in api.get("/api/overview").json()["alerts"]]


# ---------------------------------------------------------------- validation, cors, size
@pytest.mark.parametrize("device", ["bad id!", "abc", "x" * 65])
def test_bad_device_id_is_rejected(api, push, device):
    assert push(device=device).status_code == 400


def test_payload_validation(api):
    bad = make_payload()
    del bad["os"]["cpu_percent"]
    assert api.post("/api/ingest", json=bad, headers=KEY_HEADER).status_code == 422
    many = make_payload(processes=[{"pid": i, "name": "p"} for i in range(301)])
    assert api.post("/api/ingest", json=many, headers=KEY_HEADER).status_code == 422
    assert api.get("/api/overview?device_id=nonexistent-1234").status_code == 404
    assert api.get("/api/history?device_id=a;b").status_code == 400


def test_oversized_body_is_rejected(api):
    r = api.post("/api/ingest", content=b"x", headers={**KEY_HEADER, "Content-Length": "2000000", "Content-Type": "application/json"})
    assert r.status_code == 413


def test_cors_only_allows_configured_origins(api):
    evil = api.options("/api/ingest", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers
    ok = api.get("/api/health", headers={"Origin": "https://db-sentinel.vercel.app"})
    assert ok.headers["access-control-allow-origin"] == "https://db-sentinel.vercel.app"
    pre = api.options("/api/devices", headers={"Origin": "https://db-sentinel.vercel.app", "Access-Control-Request-Method": "GET",
                                                "Access-Control-Request-Headers": "x-dashboard-key"})
    assert pre.status_code == 200


# ---------------------------------------------------------------- notes
def test_notes_are_validated_and_stored_per_device(api, push):
    push(cpu=95)
    event_id = api.get(f"/api/events?device_id={DEV}").json()["events"][0]["id"]
    r = api.post("/api/investigation-notes", json={"note": "checked chrome", "author": "tester", "event_id": event_id})
    assert r.status_code == 200
    assert api.post("/api/investigation-notes", json={"note": "x", "event_id": 999999}).status_code == 400
    assert api.post("/api/investigation-notes", json={"note": "   "}).status_code == 400
    assert api.post("/api/investigation-notes", json={"note": "a" * 4001}).status_code == 400
    assert api.post("/api/investigation-notes", json={"note": "general"}).status_code == 200
    notes = api.get(f"/api/forensics?device_id={DEV}").json()["notes"]
    assert any(n["event_id"] == event_id and n["author"] == "tester" for n in notes)
    assert {n["note"] for n in api.get(f"/api/dashboard?device_id={DEV}").json()["notes"]} >= {"checked chrome", "general"}


def test_notes_are_rate_limited(api, push):
    push()
    for _ in range(main.NOTE_LIMIT):
        assert api.post("/api/investigation-notes", json={"note": "spam"}).status_code == 200
    assert api.post("/api/investigation-notes", json={"note": "spam"}).status_code == 429


# ---------------------------------------------------------------- optional dashboard key
def test_dashboard_key_protects_reads_but_not_ingest_or_health(api, push, monkeypatch):
    push()
    monkeypatch.setenv("DASHBOARD_KEY", "open-sesame")
    for path in ("/api/devices", "/api/dashboard", "/api/overview", "/api/os", "/api/database", "/api/history",
                 "/api/events", "/api/forensics", "/api/settings"):
        main.limiter = main.RateLimiter()  # each path is checked on its own; the lock-out has its own test
        assert api.get(path).status_code == 401, path
        assert api.get(path, headers={"X-Dashboard-Key": "wrong"}).status_code == 401, path
        assert api.get(path, headers={"X-Dashboard-Key": "open-sesame"}).status_code == 200, path
    main.limiter = main.RateLimiter()
    assert api.post("/api/investigation-notes", json={"note": "hi"}).status_code == 401
    assert api.post("/api/investigation-notes", json={"note": "hi"}, headers={"X-Dashboard-Key": "open-sesame"}).status_code == 200
    assert api.get("/api/health").json()["dashboard_key_required"] is True       # the page learns it must ask for a key
    assert push().status_code == 200                                             # agents use their own key


def test_wrong_dashboard_keys_lock_out_the_address(api, monkeypatch):
    monkeypatch.setenv("DASHBOARD_KEY", "open-sesame")
    for _ in range(main.BAD_KEY_LIMIT):
        assert api.get("/api/devices", headers={"X-Dashboard-Key": "guess"}).status_code == 401
    assert api.get("/api/devices", headers={"X-Dashboard-Key": "open-sesame"}).status_code == 429


def test_settings_never_reveals_secrets(api, monkeypatch):
    monkeypatch.setenv("DASHBOARD_KEY", "very-secret-dash")
    text = api.get("/api/settings", headers={"X-Dashboard-Key": "very-secret-dash"}).text
    for secret in ("very-secret-dash", os.environ["INGEST_KEY"], os.environ["DATABASE_URL"]):
        assert secret not in text
