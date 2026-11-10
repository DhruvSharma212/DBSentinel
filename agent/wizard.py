"""Interactive first-run setup for the DBSentinel agent.

Started with `--setup`, or automatically when the agent is opened (for example by double-clicking the exe)
and no agent.config.json exists yet. It asks a few questions, writes the config file, tests the connection to
the server and can register the auto-start task.
"""
import getpass
import json
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import quote, urlparse

DEFAULT_API_URL = "https://dbsentinel.onrender.com"

BANNER = """
DBSentinel agent setup
----------------------
This agent sends this computer's CPU, memory, disk and process data (and, if you choose, PostgreSQL
activity) to a DBSentinel server so you can see it on the dashboard. Press Enter to accept a [default].
"""


# These two are looked up at call time so tests can replace them.
def _line(prompt):
    return input(prompt)


def _secret(prompt):
    return getpass.getpass(prompt)


def ask(prompt, default=None, secret=False, validate=None):
    while True:
        shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
        value = (_secret if secret else _line)(shown).strip()
        if not value and default is not None:
            value = default
        problem = validate(value) if validate else None
        if problem:
            print("  ! " + problem)
            continue
        return value


def yes_no(prompt, default=True):
    suffix = "Y/n" if default else "y/N"
    while True:
        answer = _line(f"{prompt} [{suffix}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  ! Please answer y or n.")


def url_problem(url):
    """None when `url` is acceptable as the server address, else a short explanation."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "The address must look like https://your-server.example.com"
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        return "Use https:// (plain http would send the key unencrypted)."
    return None


def build_dsn(host, port, user, password, database):
    """A PostgreSQL connection URL; user and password are percent-encoded so characters like @ : / are safe."""
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(database, safe='')}"


def lock_down(path):
    """Best effort: on Windows, remove other users' access to the file that holds the key and DB password."""
    if os.name != "nt" or not os.environ.get("USERNAME"):
        return
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:F"],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def run_setup(config_path, send_test, probe_snapshot, script_dir, frozen=False):
    """Run the wizard. Returns "run" (start the agent in this window), "autostart" (task installed and started),
    "done" (config saved, nothing started) or "cancel" (nothing changed).

    send_test(config_path) -> (ok, message) sends one snapshot; probe_snapshot(dsn) -> the PostgreSQL probe result."""
    config_path = Path(config_path)
    print(BANNER)
    existing = {}
    if config_path.exists():
        if not yes_no(f"{config_path.name} already exists. Replace it?", default=False):
            print("Keeping the existing configuration.")
            return "cancel"
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            existing = {}

    api_url = ask("Server address", default=existing.get("api_url") or DEFAULT_API_URL, validate=url_problem).rstrip("/")
    api_key = ask("Ingest key from whoever runs the server (typing is hidden)", secret=True,
                  validate=lambda v: None if v else "The key cannot be empty.")
    device_name = ask("Name to show in the dashboard", default=existing.get("device_name") or socket.gethostname())

    postgres = {"enabled": False, "dsn": "", "send_query_text": True}
    if yes_no("Is there a PostgreSQL server on THIS computer that you want to monitor?", default=False):
        host = ask("  PostgreSQL host", default="localhost")
        port = ask("  Port", default="5432", validate=lambda v: None if v.isdigit() else "The port must be a number.")
        user = ask("  User", default="postgres")
        password = ask("  Password (typing is hidden)", secret=True)
        database = ask("  Database", default="postgres")
        dsn = build_dsn(host, port, user, password, database)
        result = probe_snapshot(dsn)
        print("  -> " + ("Connected: " if result["connected"] else "Could not connect: ") + result["message"])
        if result["connected"] or yes_no("  Save these PostgreSQL settings anyway?", default=False):
            postgres.update(enabled=True, dsn=dsn)
            postgres["send_query_text"] = yes_no("  Send the SQL text of queries to the dashboard? (it can contain sensitive values)", default=True)

    config = {"api_url": api_url, "api_key": api_key, "device_name": device_name, "interval_seconds": 5, "postgres": postgres}
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    lock_down(config_path)
    print(f"\nSaved {config_path}")

    print("Testing the connection to the server (the first request can take a minute if the server was asleep)...")
    ok, message = send_test(config_path)
    if not ok:
        print(f"  ! The server did not accept the test snapshot: {message}")
        print("  The settings are saved. Fix the problem (wrong key? wrong address?) and run the agent again,")
        print("  or run it with --setup to enter the values again.")
        return "done"
    print(f"  OK: {message}. Your computer should appear on the dashboard now.")

    script = Path(script_dir) / "install-autostart.ps1"
    if frozen and os.name == "nt" and script.exists() and yes_no("\nStart the agent automatically whenever you log in to Windows?", default=True):
        done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                              capture_output=True, text=True)
        print((done.stdout or done.stderr).strip())
        if done.returncode == 0:
            return "autostart"
    return "run" if yes_no("\nRun the agent now, in this window? (Press Ctrl+C or close the window to stop it)", default=True) else "done"
