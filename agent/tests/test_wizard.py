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


def run(tmp_path, send_ok=True, probe=None, frozen=False, script_dir=None):
    probe = probe or (lambda dsn: {"connected": True, "message": "Connected to PostgreSQL 16"})
    sent = []

    def send_test(path):
        sent.append(json.loads(path.read_text(encoding="utf-8")))
        return (True, "HTTP 200") if send_ok else (False, "HTTP 401 - key rejected")

    result = wizard.run_setup(tmp_path / "agent.config.json", send_test, probe, script_dir or tmp_path, frozen=frozen)
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
    monkeypatch.setattr(wizard.os, "name", "nt")
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
