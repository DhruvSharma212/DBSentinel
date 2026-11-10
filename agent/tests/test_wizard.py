"""The guided first-run setup."""
import json
from urllib.parse import unquote, urlparse

import pytest

import wizard


class Script:
    """Feeds scripted answers to the wizard's prompts and records what was asked."""

    def __init__(self, monkeypatch, lines=(), secrets=()):
        self.lines, self.secrets, self.asked = list(lines), list(secrets), []
        monkeypatch.setattr(wizard, "_line", self._line)
        monkeypatch.setattr(wizard, "_secret", self._secret)
        monkeypatch.setattr(wizard, "lock_down", lambda path: None)

    def _line(self, prompt):
        self.asked.append(prompt)
        return self.lines.pop(0)

    def _secret(self, prompt):
        self.asked.append(prompt)
        return self.secrets.pop(0)


def run(tmp_path, send_ok=True, probe=None, frozen=False, script_dir=None, enroll=None):
    probe = probe or (lambda dsn: {"connected": True, "message": "Connected to PostgreSQL 16"})
    sent = []

    def send_test(path):
        sent.append(json.loads(path.read_text(encoding="utf-8")))
        return (True, "HTTP 200") if send_ok else (False, "HTTP 401 - key rejected")

    result = wizard.run_setup(tmp_path / "agent.config.json", send_test, probe, script_dir or tmp_path, frozen=frozen, enroll=enroll)
    return result, sent


def saved(tmp_path):
    return json.loads((tmp_path / "agent.config.json").read_text(encoding="utf-8"))


def test_minimal_setup_without_postgres(tmp_path, monkeypatch):
    # url, name, "PostgreSQL here?", "run now?"
    Script(monkeypatch, lines=["", "", "n", "n"], secrets=["the-key"])
    result, sent = run(tmp_path)
    cfg = saved(tmp_path)
    assert result == "done"
    assert cfg["api_url"] == wizard.DEFAULT_API_URL and cfg["api_key"] == "the-key" and cfg["interval_seconds"] == 5
    assert cfg["postgres"] == {"enabled": False, "dsn": "", "send_query_text": True}
    assert sent and sent[0]["api_key"] == "the-key"                       # the connection test used the saved file


def test_invalid_answers_are_asked_again(tmp_path, monkeypatch):
    s = Script(monkeypatch, lines=["not a url", "http://example.com", "https://my.server/", "My Laptop", "maybe", "n", "n"],
               secrets=["", "k"])
    run(tmp_path)
    cfg = saved(tmp_path)
    assert cfg["api_url"] == "https://my.server" and cfg["device_name"] == "My Laptop" and cfg["api_key"] == "k"
    assert sum("Server address" in q for q in s.asked) == 3 and sum("Ingest key" in q for q in s.asked) == 2


def test_postgres_settings_are_tested_and_encoded(tmp_path, monkeypatch):
    seen = []

    def probe(dsn):
        seen.append(dsn)
        return {"connected": True, "message": "Connected to PostgreSQL 16"}

    # url, name, pg yes, host, port, user, database, send query text?, run now?
    Script(monkeypatch, lines=["", "", "y", "localhost", "5433", "mon", "appdb", "n", "n"], secrets=["k", "p@ss:w/rd #1"])
    run(tmp_path, probe=probe)
    cfg = saved(tmp_path)
    parsed = urlparse(cfg["postgres"]["dsn"])
    assert (parsed.hostname, parsed.port, parsed.path) == ("localhost", 5433, "/appdb")
    assert unquote(parsed.username) == "mon" and unquote(parsed.password) == "p@ss:w/rd #1"
    assert cfg["postgres"]["enabled"] is True and cfg["postgres"]["send_query_text"] is False
    assert seen == [cfg["postgres"]["dsn"]]


def test_failed_postgres_test_can_be_discarded(tmp_path, monkeypatch):
    # ... pg yes, host, port, user, database, "save anyway?" no, run now? no
    Script(monkeypatch, lines=["", "", "y", "", "", "", "", "n", "n"], secrets=["k", ""])
    run(tmp_path, probe=lambda dsn: {"connected": False, "message": "password authentication failed"})
    assert saved(tmp_path)["postgres"] == {"enabled": False, "dsn": "", "send_query_text": True}


def test_rejected_key_keeps_the_file_and_explains(tmp_path, monkeypatch, capsys):
    Script(monkeypatch, lines=["", "", "n"], secrets=["wrong"])
    result, _ = run(tmp_path, send_ok=False)
    assert result == "done" and saved(tmp_path)["api_key"] == "wrong"
    assert "key rejected" in capsys.readouterr().out


def test_existing_config_is_not_replaced_without_consent(tmp_path, monkeypatch):
    (tmp_path / "agent.config.json").write_text('{"api_key": "old"}', encoding="utf-8")
    Script(monkeypatch, lines=["n"])
    result, sent = run(tmp_path)
    assert result == "cancel" and saved(tmp_path) == {"api_key": "old"} and not sent


def test_replacing_an_existing_config_reuses_old_answers_as_defaults(tmp_path, monkeypatch):
    (tmp_path / "agent.config.json").write_text(json.dumps({"api_url": "https://old.example", "device_name": "Old Name"}), encoding="utf-8")
    Script(monkeypatch, lines=["y", "", "", "n", "n"], secrets=["k"])
    run(tmp_path)
    assert saved(tmp_path)["api_url"] == "https://old.example" and saved(tmp_path)["device_name"] == "Old Name"


def test_autostart_is_offered_only_for_the_packaged_exe(tmp_path, monkeypatch):
    (tmp_path / "install-autostart.ps1").write_text("# stub", encoding="utf-8")
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "Installed scheduled task", ""

    monkeypatch.setattr(wizard.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())
    monkeypatch.setattr(wizard, "_on_windows", lambda: True)
    Script(monkeypatch, lines=["", "", "n", "y"], secrets=["k"])            # ... auto-start? yes
    result, _ = run(tmp_path, frozen=True)
    assert result == "autostart" and calls and calls[0][-1].endswith("install-autostart.ps1")
    calls.clear()
    (tmp_path / "agent.config.json").unlink()
    Script(monkeypatch, lines=["", "", "n", "n"], secrets=["k"])            # running from source: never asks
    result, _ = run(tmp_path, frozen=False)
    assert result == "done" and not calls


def test_run_now_answer_is_returned(tmp_path, monkeypatch):
    Script(monkeypatch, lines=["", "", "n", "y"], secrets=["k"])
    assert run(tmp_path)[0] == "run"


@pytest.mark.parametrize("url,ok", [("https://a.example", True), ("http://localhost:8000", True), ("http://a.example", False),
                                    ("a.example", False), ("", False)])
def test_url_rules(url, ok):
    assert (wizard.url_problem(url) is None) is ok


# ---------------------------------------------------------------- enrollment codes
def test_an_enrollment_code_replaces_the_shared_key(tmp_path, monkeypatch):
    calls = []

    def enroll(url, code):
        calls.append((url, code))
        return True, "enrolled", "dst_issued", "Rahul"

    # url, code, name (default = the label), PostgreSQL? no, run now? no   -- and no secret is ever asked for
    s = Script(monkeypatch, lines=["", "K7P2-9XQM-4TWH", "", "n", "n"], secrets=[])
    result, sent = run(tmp_path, enroll=enroll)
    cfg = saved(tmp_path)
    assert result == "done" and calls == [(wizard.DEFAULT_API_URL, "K7P2-9XQM-4TWH")]
    assert cfg["device_token"] == "dst_issued" and "api_key" not in cfg and cfg["device_name"] == "Rahul"
    assert not any("Ingest key" in q for q in s.asked)
    assert sent and sent[0]["device_token"] == "dst_issued"                           # the connection check used the token


def test_a_bad_code_can_be_corrected_or_replaced_by_the_key(tmp_path, monkeypatch, capsys):
    attempts = []

    def enroll(url, code):
        attempts.append(code)
        return False, "HTTP 401: Invalid, used, revoked or expired enrollment code.", "", ""

    # code too short (re-asked), a well-formed but refused code, then Enter -> falls back to the ingest key
    s = Script(monkeypatch, lines=["", "abc", "AAAA-AAAA-AAAA", "", "", "n", "n"], secrets=["the-key"])
    run(tmp_path, enroll=enroll)
    cfg = saved(tmp_path)
    assert attempts == ["AAAA-AAAA-AAAA"] and cfg["api_key"] == "the-key" and "device_token" not in cfg
    assert "expired enrollment code" in capsys.readouterr().out
    assert sum("Enrollment code" in q for q in s.asked) == 3


def test_summary_says_how_the_device_authenticates(tmp_path, monkeypatch, capsys):
    Script(monkeypatch, lines=["", "K7P2-9XQM-4TWH", "", "n", "n"], secrets=[])
    run(tmp_path, enroll=lambda url, code: (True, "enrolled", "dst_issued", ""))
    assert "own device token" in capsys.readouterr().out
    (tmp_path / "agent.config.json").unlink()
    Script(monkeypatch, lines=["", "", "", "n", "n"], secrets=["k"])
    run(tmp_path, enroll=lambda url, code: (True, "enrolled", "", ""))
    assert "shared ingest key" in capsys.readouterr().out


def test_code_format_rules():
    assert wizard.code_problem("") is None
    assert wizard.code_problem("K7P2-9XQM-4TWH") is None
    assert wizard.code_problem("k7p2 9xqm 4twh") is None
    assert wizard.code_problem("K7P2") is not None
