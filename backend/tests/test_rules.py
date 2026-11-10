"""Detection rules: single-source alerts, correlation leads and disk throughput."""
LOCK1 = {"blocked_pid": 11, "blocking_pid": 12, "blocked_query": "UPDATE a", "blocking_query": "UPDATE b"}
LOCK2 = {"blocked_pid": 21, "blocking_pid": 22, "blocked_query": "UPDATE c", "blocking_query": "UPDATE d"}
MB = 1024 * 1024


def alert_ids(api):
    return sorted(a["id"] for a in api.get("/api/overview").json()["alerts"])


def test_thresholds_raise_single_source_alerts(api, push):
    push(cpu=95, mem=95, disk=95, connected=False)
    assert alert_ids(api) == ["DB-CONN", "OS-CPU", "OS-DISK", "OS-MEM"]
    sev = {a["id"]: a["severity"] for a in api.get("/api/overview").json()["alerts"]}
    assert sev["OS-MEM"] == "critical" and sev["OS-CPU"] == "high"


def test_below_thresholds_is_quiet(api, push):
    push(cpu=84, mem=89, disk=89, active=3)
    assert alert_ids(api) == []


def test_each_blocked_session_is_its_own_alert_and_incident(api, push):
    push(locks=[LOCK1, LOCK2])
    assert alert_ids(api) == ["LOCK-11", "LOCK-21"]
    assert len([e for e in api.get("/api/events").json()["events"] if e["type"] == "lock_wait"]) == 2


def test_cpu_plus_active_sessions_names_the_longest_query(api, push):
    queries = [{"pid": 5, "state": "active", "duration_seconds": 4, "query": "SELECT short"},
               {"pid": 6, "state": "active", "duration_seconds": 90, "query": "SELECT pg_sleep(90)"},
               {"pid": 7, "state": "idle", "duration_seconds": 500, "query": "idle one"}]
    push(cpu=95, active=2, queries=queries)
    alert = next(a for a in api.get("/api/overview").json()["alerts"] if a["id"] == "CORR-CPU-DB")
    assert alert["source"] == "Correlation" and "PID 6" in alert["detail"] and "90s" in alert["detail"]
    assert alert["evidence"]["longest_active_query"]["pid"] == 6
    assert "not proof of causality" in alert["detail"]


def test_cpu_alone_or_database_alone_is_not_a_correlation(api, push):
    push(cpu=95, active=0)
    assert "CORR-CPU-DB" not in alert_ids(api)
    push(cpu=10, active=5)
    assert alert_ids(api) == []
    push(cpu=95, active=5, connected=False)  # database unreachable: no correlation possible
    assert "CORR-CPU-DB" not in alert_ids(api)


def test_cpu_lead_says_when_postgres_is_the_busiest_process(api, push):
    procs = [{"pid": 321, "name": "postgres.exe", "cpu_percent": 80.0, "memory_percent": 2.0},
             {"pid": 4, "name": "System", "cpu_percent": 1.0, "memory_percent": 0.1}]
    push(cpu=95, active=1, processes=procs)
    detail = next(a for a in api.get("/api/overview").json()["alerts"] if a["id"] == "CORR-CPU-DB")["detail"]
    assert "busiest process is PostgreSQL" in detail


def test_memory_pressure_with_postgres_holding_ram(api, push):
    procs = [{"pid": 1, "name": "postgres.exe", "cpu_percent": 1, "memory_percent": 8.0},
             {"pid": 2, "name": "postgres", "cpu_percent": 1, "memory_percent": 6.5},
             {"pid": 3, "name": "chrome.exe", "cpu_percent": 5, "memory_percent": 30.0}]
    push(mem=95, processes=procs)
    alert = next(a for a in api.get("/api/overview").json()["alerts"] if a["id"] == "CORR-MEM-DB")
    assert alert["evidence"]["postgres_memory_percent"] == 14.5 and len(alert["evidence"]["postgres_processes"]) == 2
    push(mem=95)  # default processes contain no postgres
    assert "CORR-MEM-DB" not in alert_ids(api)


def test_low_disk_with_active_sessions(api, push):
    push(disk=95, active=2)
    assert {"OS-DISK", "CORR-DISK-DB"} <= set(alert_ids(api))
    push(disk=95, active=0)
    assert "CORR-DISK-DB" not in alert_ids(api)


def test_lock_wait_while_host_is_loaded(api, push):
    push(cpu=75, locks=[LOCK1])
    assert {"LOCK-11", "CORR-LOCK-HOST"} <= set(alert_ids(api))
    push(cpu=20, mem=40, locks=[LOCK1])
    assert alert_ids(api) == ["LOCK-11"]  # idle host: the lock wait alone, no correlation lead


def test_disk_throughput_is_computed_from_counter_deltas(api, push, clock):
    push(read_bytes=0, write_bytes=0)
    assert api.get("/api/os").json()["disk_read_bps"] is None  # nothing to compare with yet
    clock.advance(10)
    push(read_bytes=100 * MB, write_bytes=600 * MB, active=2)
    os_view = api.get("/api/os").json()
    assert os_view["disk_read_bps"] == 10 * MB and os_view["disk_write_bps"] == 60 * MB
    lead = next(a for a in api.get("/api/overview").json()["alerts"] if a["id"] == "CORR-IO-DB")
    assert "70.0 MB/s" in lead["detail"]


def test_disk_throughput_needs_active_sessions_and_sane_counters(api, push, clock):
    push(read_bytes=0, write_bytes=0)
    clock.advance(10)
    push(read_bytes=100 * MB, write_bytes=600 * MB, active=0)
    assert "CORR-IO-DB" not in alert_ids(api)           # heavy I/O but PostgreSQL idle
    clock.advance(10)
    push(read_bytes=10, write_bytes=10, active=2)       # counters restarted (reboot): no negative rate
    assert api.get("/api/os").json()["disk_read_bps"] is None and "CORR-IO-DB" not in alert_ids(api)
    clock.advance(500)                                  # a long gap is not a rate either
    push(read_bytes=999 * MB, write_bytes=999 * MB, active=2)
    assert api.get("/api/os").json()["disk_write_bps"] is None


def test_history_carries_disk_throughput_for_charts(api, push, clock):
    push(read_bytes=0, write_bytes=0)
    clock.advance(10)
    push(read_bytes=100 * MB, write_bytes=600 * MB)
    first, second = api.get("/api/dashboard").json()["history"]
    assert first["read_bps"] is None and first["write_bps"] is None
    assert second["read_bps"] == 10 * MB and second["write_bps"] == 60 * MB
