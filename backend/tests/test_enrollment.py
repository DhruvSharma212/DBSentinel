"""Enrollment: one-time codes become per-device tokens that can be revoked individually."""
import concurrent.futures as cf
import re

import psycopg2

import main
from conftest import ADMIN_HEADER, KEY_HEADER, make_payload

DEV = "laptop-aaaa-1111"
CODE_FORMAT = re.compile(r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")


def new_code(api, label="Friend One", **extra):
    r = api.post("/api/admin/enroll-codes", json={"label": label, **extra}, headers=ADMIN_HEADER)
    assert r.status_code == 200, r.text
    return r.json()["code"]


def enroll(api, code, device=DEV, name=""):
    return api.post("/api/enroll", json={"code": code, "device_id": device, "device_name": name})


def token_for(api, device=DEV, label="Friend One"):
    r = enroll(api, new_code(api, label), device)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def push_with(api, token, **kwargs):
    return api.post("/api/ingest", json=make_payload(**kwargs), headers={"X-Device-Token": token})


# ---------------------------------------------------------------- admin access
def test_admin_endpoints_need_the_admin_key(api, monkeypatch):
    assert api.post("/api/admin/enroll-codes", json={}).status_code == 401
    assert api.post("/api/admin/enroll-codes", json={}, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert api.get("/api/admin/devices", headers=KEY_HEADER).status_code == 401    # the ingest key is not an admin key
    monkeypatch.delenv("ADMIN_KEY")
    r = api.post("/api/admin/enroll-codes", json={}, headers=ADMIN_HEADER)
    assert r.status_code == 503 and "ADMIN_KEY" in r.text                          # disabled unless configured


def test_dashboard_key_is_not_an_admin_key(api, monkeypatch):
    monkeypatch.setenv("DASHBOARD_KEY", "view-only")
    assert api.get("/api/admin/devices", headers={"X-Dashboard-Key": "view-only"}).status_code == 401


def test_wrong_admin_keys_lock_out_the_address(api):
    for _ in range(main.BAD_KEY_LIMIT):
        assert api.get("/api/admin/devices", headers={"X-Admin-Key": "guess"}).status_code == 401
    assert api.get("/api/admin/devices", headers=ADMIN_HEADER).status_code == 429


# ---------------------------------------------------------------- codes
def test_codes_are_readable_random_and_stored_only_as_hashes(api, make_db):
    a, b = new_code(api), new_code(api)
    assert CODE_FORMAT.match(a) and CODE_FORMAT.match(b) and a != b
    listing = api.get("/api/admin/enroll-codes", headers=ADMIN_HEADER).json()["codes"]
    assert len(listing) == 2 and all(c["status"] == "active" for c in listing)
    assert a not in str(listing)                                                   # the code is shown once, never listed
    conn = psycopg2.connect(main.os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT code_hash FROM dbs_enroll_codes")
    stored = " ".join(r[0] for r in cur.fetchall())
    conn.close()
    assert a.replace("-", "") not in stored and a not in stored and len(stored.split()) == 2


def test_code_options_are_validated(api):
    assert api.post("/api/admin/enroll-codes", json={"ttl_hours": 0}, headers=ADMIN_HEADER).status_code == 400
    assert api.post("/api/admin/enroll-codes", json={"ttl_hours": 500}, headers=ADMIN_HEADER).status_code == 400
    r = api.post("/api/admin/enroll-codes", json={"label": "x" * 200, "ttl_hours": 2}, headers=ADMIN_HEADER).json()
    assert len(r["label"]) == 80 and r["ttl_hours"] == 2


# ---------------------------------------------------------------- enrolling
def test_enrolling_gives_a_token_that_can_ingest_without_the_shared_key(api, push):
    code = new_code(api, "Rahul's laptop")
    r = enroll(api, code)
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("dst_") and len(body["token"]) > 40 and body["label"] == "Rahul's laptop" and body["device_id"] == DEV
    assert push_with(api, body["token"]).status_code == 200
    (device,) = api.get("/api/devices").json()["devices"]
    assert device["device_id"] == DEV and device["online"] is True
    assert push().status_code == 200                                               # the shared key keeps working too


def test_code_can_be_typed_in_any_case_and_with_or_without_dashes(api):
    code = new_code(api)
    r = enroll(api, " " + code.lower().replace("-", " ") + " ")
    assert r.status_code == 200


def test_a_code_works_exactly_once(api):
    code = new_code(api)
    assert enroll(api, code).status_code == 200
    again = enroll(api, code, device="other-dev-0002")
    assert again.status_code == 401 and "used" in again.json()["detail"]


def test_a_code_cannot_be_used_twice_even_in_parallel(api):
    code = new_code(api)
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: enroll(api, code, device=f"parallel-dev-{i:04d}").status_code, range(12)))
    assert results.count(200) == 1 and results.count(401) == 11


def test_expired_revoked_and_unknown_codes_are_refused(api, clock):
    expired = new_code(api, ttl_hours=1)
    clock.advance(3600 + 5)
    assert enroll(api, expired).status_code == 401
    revoked = new_code(api)
    code_id = api.get("/api/admin/enroll-codes", headers=ADMIN_HEADER).json()["codes"][0]["id"]
    assert api.delete(f"/api/admin/enroll-codes/{code_id}", headers=ADMIN_HEADER).status_code == 200
    assert enroll(api, revoked).status_code == 401
    assert enroll(api, "AAAA-AAAA-AAAA").status_code == 401
    assert enroll(api, "short").status_code == 401
    assert enroll(api, "").status_code == 401
    statuses = {c["status"] for c in api.get("/api/admin/enroll-codes", headers=ADMIN_HEADER).json()["codes"]}
    assert statuses == {"expired", "revoked"}


def test_guessing_codes_locks_out_the_address(api):
    good = new_code(api)
    for _ in range(main.BAD_KEY_LIMIT):
        assert enroll(api, "ZZZZ-ZZZZ-ZZZZ").status_code == 401
    assert enroll(api, good).status_code == 429                                   # even a correct code is refused while locked out


def test_enrolling_validates_the_device_id(api):
    assert enroll(api, new_code(api), device="x").status_code == 400


def test_tokens_are_stored_only_as_hashes(api):
    token = token_for(api)
    conn = psycopg2.connect(main.os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT token_hash FROM dbs_device_tokens")
    stored = [r[0] for r in cur.fetchall()]
    conn.close()
    assert len(stored) == 1 and token not in stored[0] and len(stored[0]) == 64


# ---------------------------------------------------------------- using tokens
def test_a_token_only_works_for_its_own_device(api):
    token = token_for(api, device=DEV)
    r = push_with(api, token, device="someone-else-0002")
    assert r.status_code == 403 and "different device" in r.json()["detail"]
    assert api.get("/api/devices").json()["devices"] == []                        # nothing was stored


def test_unknown_tokens_are_rejected_and_counted_against_the_address(api):
    for _ in range(main.BAD_KEY_LIMIT):
        assert push_with(api, "dst_not-a-real-token").status_code == 401
    assert push_with(api, "dst_not-a-real-token").status_code == 429


def test_requests_without_any_credential_are_refused(api):
    r = api.post("/api/ingest", json=make_payload())
    assert r.status_code == 401 and "X-Device-Token" in r.json()["detail"]


def test_using_a_token_records_when_it_was_last_used(api, clock):
    token = token_for(api)
    push_with(api, token)
    clock.advance(30)
    push_with(api, token)
    (device,) = api.get("/api/admin/devices", headers=ADMIN_HEADER).json()["devices"]
    assert device["access"] == "device token" and device["token_last_used_at"] is not None


# ---------------------------------------------------------------- revoking
def test_revoking_cuts_off_one_device_and_keeps_its_data(api, push):
    friend, other = "friend-dev-0001", "friend-dev-0002"
    t1, t2 = token_for(api, friend, "One"), token_for(api, other, "Two")
    assert push_with(api, t1, device=friend, cpu=95).status_code == 200
    assert push_with(api, t2, device=other).status_code == 200
    assert api.post(f"/api/admin/devices/{friend}/revoke", headers=ADMIN_HEADER).json()["revoked"] == 1
    r = push_with(api, t1, device=friend)
    assert r.status_code == 401 and "revoked" in r.json()["detail"]
    assert push_with(api, t2, device=other).status_code == 200                    # the other friend is unaffected
    assert len(api.get(f"/api/events?device_id={friend}").json()["events"]) >= 1  # history is kept
    access = {d["device_id"]: d["access"] for d in api.get("/api/admin/devices", headers=ADMIN_HEADER).json()["devices"]}
    assert access == {friend: "revoked", other: "device token"}


def test_a_revoked_device_can_be_enrolled_again_and_the_old_token_stays_dead(api):
    old = token_for(api)
    push_with(api, old)
    api.post(f"/api/admin/devices/{DEV}/revoke", headers=ADMIN_HEADER)
    new = token_for(api)
    assert push_with(api, new).status_code == 200 and push_with(api, old).status_code == 401


def test_enrolling_again_replaces_the_previous_token(api):
    first = token_for(api)
    second = token_for(api)
    assert push_with(api, second).status_code == 200 and push_with(api, first).status_code == 401


def test_revoking_a_device_without_a_token_explains_itself(api, push):
    push()                                                                         # reports with the shared key
    r = api.post(f"/api/admin/devices/{DEV}/revoke", headers=ADMIN_HEADER)
    assert r.status_code == 404 and "shared key" in r.json()["detail"]
    assert api.post("/api/admin/devices/bad id/revoke", headers=ADMIN_HEADER).status_code in (400, 404)


# ---------------------------------------------------------------- shared key policy and listings
def test_the_shared_key_can_be_switched_off_once_everyone_is_enrolled(api, monkeypatch):
    token = token_for(api)
    monkeypatch.setenv("SHARED_INGEST_KEY_ENABLED", "false")
    r = api.post("/api/ingest", json=make_payload(), headers=KEY_HEADER)
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    assert push_with(api, token).status_code == 200
    assert api.get("/api/settings").json()["shared_ingest_key_enabled"] is False


def test_device_listing_shows_how_each_device_authenticates(api, push):
    push(device="shared-dev-0001")
    token = token_for(api, "enrolled-dev-0001")
    push_with(api, token, device="enrolled-dev-0001")
    token_for(api, "pending-dev-0001", "Not reported yet")
    listing = api.get("/api/admin/devices", headers=ADMIN_HEADER).json()
    access = {d["device_id"]: d["access"] for d in listing["devices"]}
    assert access == {"shared-dev-0001": "shared key", "enrolled-dev-0001": "device token"}
    assert [p["device_id"] for p in listing["pending"]] == ["pending-dev-0001"] and listing["pending"][0]["label"] == "Not reported yet"


def test_settings_reports_admin_status_without_secrets(api):
    body = api.get("/api/settings").json()
    assert body["admin_key_configured"] is True
    assert "test-admin-key" not in api.get("/api/settings").text


def test_enrollment_and_ingest_are_not_blocked_by_the_dashboard_key(api, monkeypatch):
    monkeypatch.setenv("DASHBOARD_KEY", "view-only")
    token = token_for(api)
    assert push_with(api, token).status_code == 200
    assert api.get("/api/devices").status_code == 401                              # viewing still needs the dashboard key


def test_browsers_may_send_the_admin_headers(api):
    pre = api.options("/api/admin/enroll-codes", headers={"Origin": "https://db-sentinel.vercel.app", "Access-Control-Request-Method": "POST",
                                                           "Access-Control-Request-Headers": "x-admin-key,content-type"})
    assert pre.status_code == 200
    pre = api.options("/api/admin/enroll-codes/1", headers={"Origin": "https://db-sentinel.vercel.app", "Access-Control-Request-Method": "DELETE"})
    assert pre.status_code == 200
