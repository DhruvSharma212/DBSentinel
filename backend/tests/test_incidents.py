"""Events are incidents: they open, count the snapshots that showed them, close, and can be reopened as new rows."""
import main

DEV = "laptop-aaaa-1111"


def incidents(api, message=None):
    rows = api.get(f"/api/events?device_id={DEV}").json()["events"]
    return [e for e in rows if message is None or e["message"] == message]


def test_repeated_observations_are_one_incident(api, push, clock):
    for _ in range(3):
        push(cpu=95)
        clock.advance(5)
    (event,) = incidents(api, "High CPU utilization")
    assert event["occurrences"] == 3 and event["active"] is True and event["resolved_at"] is None
    assert event["duration_seconds"] == 10  # first to last sighting


def test_incident_closes_after_the_grace_period_and_a_return_is_a_new_incident(api, push, clock):
    push(cpu=95)
    clock.advance(5)
    push(cpu=95)
    clock.advance(10)
    push(cpu=10)                                       # condition gone, but still inside the grace window
    (event,) = incidents(api, "High CPU utilization")
    assert event["resolved_at"] is None and event["active"] is True
    clock.advance(main.EVENT_GRACE_SECONDS + 10)
    push(cpu=10)                                       # past the grace window: closed
    (event,) = incidents(api, "High CPU utilization")
    assert event["resolved_at"] is not None and event["active"] is False and event["occurrences"] == 2
    first_id = event["id"]
    clock.advance(5)
    push(cpu=95)                                       # comes back
    rows = incidents(api, "High CPU utilization")
    assert len(rows) == 2
    new, old = rows[0], rows[1]
    assert new["id"] != first_id and new["occurrences"] == 1 and new["active"] is True
    assert old["id"] == first_id and old["resolved_at"] is not None                 # history is kept


def test_flapping_inside_the_grace_window_stays_one_incident(api, push, clock):
    for cpu in (95, 70, 95, 70, 95):
        push(cpu=cpu)
        clock.advance(5)
    assert len(incidents(api, "High CPU utilization")) == 1


def test_incident_of_an_offline_device_is_not_active(api, push, clock):
    push(cpu=95)
    clock.advance(main.EVENT_GRACE_SECONDS + 5)
    (event,) = incidents(api, "High CPU utilization")
    assert event["active"] is False  # nobody is reporting any more: not "active", even though never explicitly closed


def test_incidents_do_not_leak_between_devices(api, push, clock):
    push(cpu=95)
    push(cpu=95, device="other-dev-0002")
    a, b = incidents(api, "High CPU utilization"), api.get("/api/events?device_id=other-dev-0002").json()["events"]
    assert len(a) == 1 and len(b) == 1 and a[0]["id"] != b[0]["id"]


def test_alerts_carry_the_incident_id_and_notes_link_to_it(api, push):
    push(cpu=95, locks=[{"blocked_pid": 1, "blocking_pid": 2}])
    alerts = api.get("/api/dashboard").json()["alerts"]
    assert all(isinstance(a["event_id"], int) for a in alerts) and len(alerts) >= 2
    cpu_alert = next(a for a in alerts if a["id"] == "OS-CPU")
    assert api.post("/api/investigation-notes", json={"note": "looking", "event_id": cpu_alert["event_id"], "author": "me"}).status_code == 200
    dash = api.get("/api/dashboard").json()
    (note,) = [n for n in dash["notes"] if n["event_id"] == cpu_alert["event_id"]]
    assert note["author"] == "me" and note["device_id"] == DEV
    assert cpu_alert["event_id"] in {e["id"] for e in dash["events"]}


def test_a_note_survives_its_incident_closing(api, push, clock):
    push(cpu=95)
    event_id = incidents(api, "High CPU utilization")[0]["id"]
    api.post("/api/investigation-notes", json={"note": "kept", "event_id": event_id})
    clock.advance(main.EVENT_GRACE_SECONDS + 10)
    push(cpu=10)
    assert any(n["note"] == "kept" and n["event_id"] == event_id for n in api.get("/api/dashboard").json()["notes"])
