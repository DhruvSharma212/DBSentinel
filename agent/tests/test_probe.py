"""The PostgreSQL probe against a real server."""
import threading
import time
import uuid

import psycopg2
import pytest

from collector import DatabaseProbe


def only_auth(uri, user, password):
    """The same server address with different credentials."""
    host_port_db = uri.split("@", 1)[1]
    return f"postgresql://{user}:{password}@{host_port_db}"


@pytest.fixture
def conns(pg_uri):
    opened = []

    def connect(autocommit=True):
        conn = psycopg2.connect(pg_uri)
        conn.autocommit = autocommit
        opened.append(conn)
        return conn

    yield connect
    for conn in opened:
        try:
            conn.close()
        except Exception:
            pass


def test_disabled_probe_is_not_an_error():
    result = DatabaseProbe(False, "").snapshot()
    assert result["configured"] is False and result["connected"] is False and result["queries"] == []


def test_counts_only_client_sessions(pg_uri, conns):
    sessions = [conns() for _ in range(3)]
    for conn in sessions:
        conn.cursor().execute("SELECT 1")
    probe = DatabaseProbe(True, pg_uri)
    result = probe.snapshot()
    probe.close()
    assert result["connected"] and result["message"].startswith("Connected to PostgreSQL")
    assert result["counts"]["total"] == 3                                 # not the probe itself, not background workers
    assert len(result["queries"]) == 3 and all(q["state"] for q in result["queries"])


def test_lock_wait_is_reported_with_both_sides(pg_uri, conns):
    table = "t_" + uuid.uuid4().hex[:8]
    setup = conns()
    setup.cursor().execute(f"CREATE TABLE {table}(id int)")
    holder = conns(autocommit=False)
    holder.cursor().execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
    waiter = conns()
    threading.Thread(target=lambda: waiter.cursor().execute(f"SELECT count(*) FROM {table}"), daemon=True).start()
    time.sleep(1.0)
    probe = DatabaseProbe(True, pg_uri)
    result = probe.snapshot()
    probe.close()
    holder.rollback()
    (lock,) = result["locks"]
    assert lock["blocked_pid"] == waiter.get_backend_pid() and lock["blocking_pid"] == holder.get_backend_pid()
    assert table in lock["blocked_query"]
    assert result["queries"][0]["state"] == "active"                      # active sessions are listed first


def test_query_text_can_be_hidden(pg_uri, conns):
    conns().cursor().execute("SELECT 'secret-value'")
    probe = DatabaseProbe(True, pg_uri, send_query_text=False)
    result = probe.snapshot()
    probe.close()
    assert all(q["query"] == DatabaseProbe.HIDDEN for q in result["queries"])


def test_role_without_pg_monitor_gets_a_visibility_warning(pg_uri, conns):
    role = "lim_" + uuid.uuid4().hex[:8]
    admin = conns()
    admin.cursor().execute(f"CREATE ROLE {role} LOGIN PASSWORD 'pw'")
    try:
        probe = DatabaseProbe(True, only_auth(pg_uri, role, "pw"))
        result = probe.snapshot()
        probe.close()
        assert result["connected"] and "limited visibility" in result["message"] and "pg_monitor" in result["message"]
        admin.cursor().execute(f"GRANT pg_monitor TO {role}")
        probe = DatabaseProbe(True, only_auth(pg_uri, role, "pw"))
        assert "limited visibility" not in probe.snapshot()["message"]
        probe.close()
    finally:
        admin.cursor().execute(f"DROP ROLE IF EXISTS {role}")


def test_a_dropped_connection_is_replaced_in_the_same_snapshot(pg_uri, conns):
    probe = DatabaseProbe(True, pg_uri)
    assert probe.snapshot()["connected"]
    conns().cursor().execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='dbsentinel-agent'")
    assert probe.snapshot()["connected"]
    probe.close()


def test_the_session_is_read_only(pg_uri):
    probe = DatabaseProbe(True, pg_uri)
    probe.snapshot()
    cur = probe._conn.cursor()
    cur.execute("SHOW default_transaction_read_only")
    assert cur.fetchone()[0] == "on"
    probe.close()


def test_unreachable_server_never_leaks_the_password():
    result = DatabaseProbe(True, "postgresql://postgres:SECRETPW@127.0.0.1:1/postgres").snapshot()
    assert result["configured"] and not result["connected"] and result["message"]
    assert "SECRETPW" not in result["message"]
