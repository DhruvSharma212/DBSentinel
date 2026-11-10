"""Configuration, device identity, single-instance lock and the HTTP side of the agent."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import agent


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("DBSENTINEL_API_URL", "DBSENTINEL_API_KEY", "DBSENTINEL_PG_DSN"):
        monkeypatch.delenv(name, raising=False)


def write_cfg(tmp_path, **values):
    path = tmp_path / "agent.config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


# ---------------------------------------------------------------- load / validate
def test_defaults_and_overrides(tmp_path, monkeypatch):
    cfg = agent.load_config(write_cfg(tmp_path, api_url="https://x.example/", api_key="k"))
    assert cfg["api_url"] == "https://x.example"                        # trailing slash removed
    assert cfg["device_name"] == socket.gethostname() and cfg["interval_seconds"] == 5
    assert cfg["postgres"] == {"dsn": "", "enabled": True, "send_query_text": True}
    monkeypatch.setenv("DBSENTINEL_API_KEY", "from-env")
    monkeypatch.setenv("DBSENTINEL_PG_DSN", "postgresql://u:p@h/db")
    cfg = agent.load_config(write_cfg(tmp_path, api_url="https://x.example", api_key="from-file", interval_seconds=0.1))
    assert cfg["api_key"] == "from-env" and cfg["postgres"]["dsn"] == "postgresql://u:p@h/db"
    assert cfg["interval_seconds"] == 2.0                               # never faster than every 2 s


def test_unreadable_config_is_a_config_error(tmp_path):
    bad = tmp_path / "agent.config.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(agent.ConfigError):
        agent.load_config(bad)


def test_config_file_with_a_bom_is_accepted(tmp_path):
    path = tmp_path / "agent.config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"api_url": "https://x.example", "api_key": "k"}).encode())
    assert agent.load_config(path)["api_key"] == "k"                    # Notepad adds a BOM on some Windows versions


@pytest.mark.parametrize("values,expected", [
    ({"api_url": "https://ok.example", "api_key": "k"}, None),
    ({"api_url": "http://localhost:8000", "api_key": "k"}, None),
    ({"api_url": "http://127.0.0.1:8000", "api_key": "k"}, None),
    ({"api_url": "http://example.com", "api_key": "k"}, "unencrypted"),
    ({"api_url": "http://example.com", "api_key": "k", "allow_insecure_http": True}, None),
    ({"api_url": "ftp://example.com", "api_key": "k"}, "https://host"),
    ({"api_url": "https://ok.example"}, "no credential"),
    ({"api_url": "https://ok.example", "enroll_code": "K7P2-9XQM-4TWH"}, None),
    ({"api_url": "https://ok.example", "device_token": "dst_abc"}, None),
    ({"api_key": "k"}, "api_url is missing"),
])
def test_validation_rules(tmp_path, values, expected):
    path = write_cfg(tmp_path, **values)
    cfg = agent.load_config(path)
    if expected is None:
        agent.validate_config(cfg, path)
    else:
        with pytest.raises(agent.ConfigError, match=expected):
            agent.validate_config(cfg, path)


def test_missing_config_file_is_reported(tmp_path):
    path = tmp_path / "nope.json"
    with pytest.raises(agent.ConfigError, match="config file not found"):
        agent.validate_config(agent.load_config(path), path)


# ---------------------------------------------------------------- device identity
def test_device_id_is_created_once_and_reused(tmp_path):
    state = tmp_path / "agent.state.json"
    first = agent.load_device_id(state)
    assert 8 <= len(first) <= 64 and first.startswith("dev-")
    assert agent.load_device_id(state) == first
    state.write_text("garbage", encoding="utf-8")                      # a damaged state file is replaced, not fatal
    assert agent.load_device_id(state).startswith("dev-")


# ---------------------------------------------------------------- single instance
def test_only_one_agent_can_hold_the_lock(tmp_path):
    lock = tmp_path / "agent.lock"
    assert agent.acquire_single_instance(lock) is True
    assert agent.acquire_single_instance(lock) is False                 # a second agent in the same folder
    agent._lock_handle.close()                                          # first agent exits
    assert agent.acquire_single_instance(lock) is True
    agent._lock_handle.close()


# ---------------------------------------------------------------- sending
class FakeServer:
    def __init__(self, status=200, body=b'{"ok": true}', routes=None):
        outer = self
        self.requests = []
        routes = routes or {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "key": self.headers.get("X-API-Key"), "token": self.headers.get("X-Device-Token"),
                                       "type": self.headers.get("Content-Type"), "body": json.loads(self.rfile.read(length))})
                status_code, answer = routes.get(self.path, (status, body))
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def test_send_posts_json_with_the_key():
    server = FakeServer()
    try:
        ok, message = agent.send({"api_url": server.url, "api_key": "secret-key", "request_timeout_seconds": 5}, {"hello": "world"})
    finally:
        server.close()
    assert ok and message == "HTTP 200"
    (req,) = server.requests
    assert req["path"] == "/api/ingest" and req["key"] == "secret-key" and req["type"] == "application/json"
    assert req["body"] == {"hello": "world"}


def test_send_explains_a_rejected_key():
    server = FakeServer(status=401, body=b'{"detail": "Invalid or missing X-API-Key."}')
    try:
        ok, message = agent.send({"api_url": server.url, "api_key": "x", "request_timeout_seconds": 5}, {})
    finally:
        server.close()
    assert not ok and "401" in message and "INGEST_KEY" in message


def test_send_reports_an_unreachable_server():
    ok, message = agent.send({"api_url": "http://127.0.0.1:1", "api_key": "x", "request_timeout_seconds": 3}, {})
    assert not ok and "cannot reach" in message


def test_payload_matches_what_the_server_accepts(tmp_path):
    """The agent's snapshot must validate against the backend's ingest model."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
    pytest.importorskip("fastapi")
    import main as backend
    cfg = agent.load_config(write_cfg(tmp_path, api_url="https://x.example", api_key="k"))
    payload = agent.build_payload(cfg, "dev-testtesttest01", agent.OSCollector(), agent.make_probe(cfg))
    backend.IngestPayload(**json.loads(json.dumps(payload)))
    assert payload["agent_version"] == agent.AGENT_VERSION


# ---------------------------------------------------------------- credentials
def test_a_device_token_is_sent_instead_of_the_shared_key():
    server = FakeServer()
    try:
        agent.send({"api_url": server.url, "api_key": "shared", "device_token": "dst_mine", "request_timeout_seconds": 5}, {})
        agent.send({"api_url": server.url, "api_key": "shared", "request_timeout_seconds": 5}, {})
    finally:
        server.close()
    assert server.requests[0]["token"] == "dst_mine" and server.requests[0]["key"] is None
    assert server.requests[1]["key"] == "shared" and server.requests[1]["token"] is None


def failing(status, detail, **cfg):
    server = FakeServer(status=status, body=json.dumps({"detail": detail}).encode())
    try:
        return agent.send({"api_url": server.url, "api_key": "k", "request_timeout_seconds": 5, **cfg}, {})
    finally:
        server.close()


def test_rejections_come_with_a_next_step():
    ok, message = failing(401, "This device's access was revoked.", device_token="dst_x")
    assert not ok and "revoked" in message and "--setup" in message and "enrollment code" in message
    ok, message = failing(401, "Invalid device token.", device_token="dst_x")
    assert "device token was not accepted" in message
    ok, message = failing(403, "This token belongs to a different device; run the agent setup again.", device_token="dst_x")
    assert "another device" in message and "agent.state.json" in message
    ok, message = failing(403, "The shared ingest key is disabled on this server; enroll this device with a code.")
    assert "no longer accepts the shared key" in message


# ---------------------------------------------------------------- enrollment
ENROLL_OK = (200, json.dumps({"ok": True, "token": "dst_issued-token", "device_id": "dev-x", "label": "Rahul"}).encode())


def test_enroll_returns_the_token_and_label():
    server = FakeServer(routes={"/api/enroll": ENROLL_OK})
    try:
        ok, message, token, label = agent.enroll(server.url, "K7P2-9XQM-4TWH", "dev-testtesttest01", "Laptop")
    finally:
        server.close()
    assert ok and token == "dst_issued-token" and label == "Rahul"
    (req,) = server.requests
    assert req["path"] == "/api/enroll" and req["key"] is None and req["token"] is None
    assert req["body"] == {"code": "K7P2-9XQM-4TWH", "device_id": "dev-testtesttest01", "device_name": "Laptop"}


def test_enroll_reports_refusals_and_unreachable_servers():
    server = FakeServer(routes={"/api/enroll": (401, b'{"detail": "Invalid, used, revoked or expired enrollment code."}')})
    try:
        ok, message, token, _ = agent.enroll(server.url, "AAAA-AAAA-AAAA", "dev-x", timeout=5)
    finally:
        server.close()
    assert not ok and "expired enrollment code" in message and token == ""
    ok, message, _, _ = agent.enroll("http://127.0.0.1:1", "AAAA-AAAA-AAAA", "dev-x", timeout=3)
    assert not ok and "cannot reach" in message


def enrolled_config(tmp_path, monkeypatch, routes=None, **values):
    monkeypatch.setattr(agent, "lock_down", lambda path: None)
    server = FakeServer(routes=routes or {"/api/enroll": ENROLL_OK})
    path = write_cfg(tmp_path, api_url=server.url, enroll_code="K7P2-9XQM-4TWH", **values)
    return server, path, agent.load_config(path)


def test_the_code_is_traded_for_a_token_and_removed_from_the_file(tmp_path, monkeypatch):
    server, path, cfg = enrolled_config(tmp_path, monkeypatch)
    try:
        ok, message = agent.ensure_enrolled(cfg, path, "dev-testtesttest01")
    finally:
        server.close()
    assert ok and cfg["device_token"] == "dst_issued-token" and cfg["enroll_code"] == ""
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["device_token"] == "dst_issued-token" and "enroll_code" not in saved           # a used code is never kept
    assert cfg["device_name"] == "Rahul" and saved["device_name"] == "Rahul"                   # the admin's label names the device
    assert agent.ensure_enrolled(agent.load_config(path), path, "dev-testtesttest01") == (True, "")  # nothing left to do next start


def test_a_name_chosen_by_the_user_is_not_overwritten_by_the_label(tmp_path, monkeypatch):
    server, path, cfg = enrolled_config(tmp_path, monkeypatch, device_name="My Own Name")
    try:
        agent.ensure_enrolled(cfg, path, "dev-testtesttest01")
    finally:
        server.close()
    assert cfg["device_name"] == "My Own Name" and json.loads(path.read_text(encoding="utf-8"))["device_name"] == "My Own Name"


def test_a_refused_code_leaves_the_config_untouched(tmp_path, monkeypatch):
    server, path, cfg = enrolled_config(tmp_path, monkeypatch, routes={"/api/enroll": (401, b'{"detail": "used"}')})
    before = path.read_text(encoding="utf-8")
    try:
        ok, message = agent.ensure_enrolled(cfg, path, "dev-testtesttest01")
    finally:
        server.close()
    assert not ok and "used" in message and cfg["device_token"] == "" and path.read_text(encoding="utf-8") == before


def test_nothing_is_sent_when_there_is_no_code_or_a_token_exists(tmp_path):
    server = FakeServer()
    try:
        for values in ({"api_key": "k"}, {"device_token": "dst_x", "enroll_code": "K7P2-9XQM-4TWH"}):
            cfg = agent.load_config(write_cfg(tmp_path, api_url=server.url, **values))
            assert agent.ensure_enrolled(cfg, tmp_path / "agent.config.json", "dev-x") == (True, "")
    finally:
        server.close()
    assert server.requests == []


def test_environment_only_setup_still_saves_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "lock_down", lambda path: None)
    server = FakeServer(routes={"/api/enroll": ENROLL_OK})
    monkeypatch.setenv("DBSENTINEL_API_URL", server.url)
    monkeypatch.setenv("DBSENTINEL_ENROLL_CODE", "K7P2-9XQM-4TWH")
    path = tmp_path / "agent.config.json"                      # no file yet
    cfg = agent.load_config(path)
    try:
        ok, _ = agent.ensure_enrolled(cfg, path, "dev-testtesttest01")
    finally:
        server.close()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert ok and saved["device_token"] == "dst_issued-token" and saved["api_url"] == server.url
